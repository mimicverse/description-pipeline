"""Recording transport streams bytes and closes readers on every HTTP exit."""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from anyio import CancelScope
from starlette.requests import ClientDisconnect, Request

from description_pipeline.orchestration.pico_portal import PicoPortal, RecordingResponse
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

    async def blocked_response(self, spec_version="2.4"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "archive.bin"
        path.write_bytes(b"actual-reader")
        started, release, closed = (threading.Event() for _ in range(3))
        self.addCleanup(release.set)
        handles = []

        def archive():
            try:
                with path.open("rb") as stream:
                    handles.append(stream)
                    started.set()
                    if not release.wait(2):
                        raise TimeoutError("The blocked download test was not released")
                    yield stream.read()
            finally:
                closed.set()

        scope = {"type": "http", "asgi": {"spec_version": spec_version}}
        return RecordingResponse(archive()), scope, started, release, closed, handles

    async def test_repeated_cancellation_waits_for_the_read_before_closing(self):
        response, scope, started, release, closed, handles = await self.blocked_response()
        task = asyncio.create_task(response(scope, AsyncMock(), AsyncMock()))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            for reason in ("first", "second", "third"):
                task.cancel(reason)
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
                self.assertFalse(closed.is_set())
                self.assertFalse(handles[0].closed)
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(closed.is_set())
        self.assertTrue(all(handle.closed for handle in handles))

    async def test_anyio_scope_cancellation_closes_the_inflight_reader(self):
        response, scope, started, release, closed, handles = await self.blocked_response()
        with CancelScope() as cancellation:
            async def cancel_during_read():
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                cancellation.cancel()
                await asyncio.sleep(0.03)
                self.assertFalse(closed.is_set())
                release.set()

            controller = asyncio.create_task(cancel_during_read())
            await response(scope, AsyncMock(), AsyncMock())
        await controller
        self.assertTrue(cancellation.cancelled_caught)
        self.assertTrue(closed.is_set())
        self.assertTrue(all(handle.closed for handle in handles))

    async def test_legacy_asgi_disconnect_waits_for_the_inflight_reader(self):
        response, scope, started, release, closed, handles = await self.blocked_response("2.3")

        async def receive():
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            return {"type": "http.disconnect"}

        async def release_after_disconnect():
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            await asyncio.sleep(0.03)
            self.assertFalse(closed.is_set())
            release.set()

        controller = asyncio.create_task(release_after_disconnect())
        await response(scope, receive, AsyncMock())
        await controller
        self.assertTrue(closed.is_set())
        self.assertTrue(all(handle.closed for handle in handles))
