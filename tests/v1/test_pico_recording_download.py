"""Recording transport streams bytes and closes readers on every HTTP exit."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from starlette.requests import ClientDisconnect, Request

from description_pipeline.orchestration.pico_portal import PicoPortal
from description_pipeline.pipeline.contracts import ArtifactRef


class RecordingDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def response(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "archive.bin"
        path.write_bytes(b"archive-bytes")
        handles = []

        def archive():
            with path.open("rb") as stream:
                handles.append(stream)
                while chunk := stream.read(3):
                    yield chunk

        ref = ArtifactRef(
            artifact_id="a" * 64, kind="teleop_recording", locator="cas://artifact/" + "a" * 64,
            manifest_sha256="b" * 64, set_sha256="c" * 64, producer_run_id="session",
            producer_stage=None, producer_tool_sha256="d" * 64,
        )
        service = SimpleNamespace(recording_archive=Mock(return_value=archive()))
        routes = PicoPortal(None, service)
        routes.authorized = AsyncMock(return_value=(None, None, {"recording_artifact": ref.to_record()}))
        scope = {"type": "http", "asgi": {"spec_version": "2.4"}, "headers": [],
                 "path_params": {"session_id": "session"}}
        response = await routes.recording(Request(scope))
        service.recording_archive.assert_called_once_with(ref)
        return response, scope, handles

    async def test_download_streams_content_and_preserves_headers(self):
        response, scope, handles = await self.response()
        messages = []

        async def send(message):
            messages.append(message)

        await response(scope, AsyncMock(), send)
        self.assertEqual(b"".join(item.get("body", b"") for item in messages), b"archive-bytes")
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["content-disposition"], 'attachment; filename="teleop-recording.zip"')
        self.assertTrue(handles and all(item.closed for item in handles))

    async def test_disconnect_and_cancellation_close_the_actual_open_reader(self):
        for error, expected in ((OSError, ClientDisconnect), (asyncio.CancelledError, asyncio.CancelledError)):
            with self.subTest(error=error):
                response, scope, handles = await self.response()

                async def send(message, error=error):
                    if message["type"] == "http.response.body" and message.get("body"):
                        raise error()

                with self.assertRaises(expected):
                    await response(scope, AsyncMock(), send)
                self.assertTrue(handles and all(item.closed for item in handles))
