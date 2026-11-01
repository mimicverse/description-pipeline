"""One managed recording verification path for portal and independent CLI consumers."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
import secrets
import shutil

from ..owned_process import terminate_owned
from .recording import (
    VerificationUnresolved,
    prepare_bound,
    prepare_source,
    recording_binding,
    run_verification,
    verification_execution,
)
from .resources import SESSION_QUOTA_BYTES, StorageGate, admit_storage, verification_partial
from .store import SimulationConflict
from .tasks import settle_task, wait_task, run_thread


class ManagedRecordingVerification:
    """Own the existing PG command slot, exact partial, allocation gate and native helper."""

    def __init__(self, store, artifacts, root: Path, python: Path):
        self.store, self.artifacts = store, artifacts
        self.root, self.python = Path(root), Path(python)
        self.owner = secrets.token_hex(24)
        self._consumer_execution = verification_execution()
        self._storage_gate = StorageGate(self.root)

    async def _acquire_verification_slot(self):
        acquiring = asyncio.create_task(asyncio.to_thread(self.store.acquire_verification_slot))
        interrupted = await settle_task(acquiring)
        connection = acquiring.result()
        if interrupted:
            if connection is not None:
                await run_thread(connection.close)
            raise asyncio.CancelledError
        return connection

    async def verify_recording(self, session_id, members, read_member):
        slot = await self._acquire_verification_slot()
        if slot is None:
            raise SimulationConflict("Recording verification capacity is occupied or unresolved")
        try:
            await self._reconcile_owned()
            return await self._verify_recording(session_id, members, read_member, slot=slot)
        finally:
            await run_thread(slot.close)

    async def _reconcile_verifications(self):
        slot = await self._acquire_verification_slot()
        if slot is None:
            return
        try:
            await self._reconcile_owned()
        finally:
            await run_thread(slot.close)

    def _admit_storage(self, destination, additional_bytes, excluded_session=None, *, excluded_verification=None):
        return admit_storage(
            self.store,
            self.root,
            destination,
            additional_bytes,
            excluded_session,
            excluded_verification=excluded_verification,
        )

    @staticmethod
    async def _terminate_identity(identity):
        return await run_thread(terminate_owned, identity)

    async def _verify_recording(self, session_id, members, read_member, *, slot=None):
        row = await run_thread(self.store.get, session_id)
        binding = recording_binding(row)
        command_id = "verify-" + secrets.token_hex(16)
        request = {
            "type": "verify_recording",
            "binding": binding,
            "partial": "recording-verification-" + command_id,
            "execution": deepcopy(self._consumer_execution),
        }
        temporary = self._verification_partial(command_id, request)
        admitted = False
        retained = False
        try:
            admitting = asyncio.create_task(
                asyncio.to_thread(
                    self.store.begin_verification,
                    session_id,
                    command_id,
                    request,
                    self.owner,
                    slot=slot,
                )
            )
            interrupted = await settle_task(admitting)
            admitting.result()
            admitted = True
            if interrupted:
                raise asyncio.CancelledError
            temporary.mkdir(mode=0o700)
            source = temporary / "source"
            async with self._storage_gate:
                await run_thread(self._admit_storage, temporary, SESSION_QUOTA_BYTES, excluded_verification=command_id)
                await prepare_source(source, row["request"]["source"], members, read_member)

            async def register_process(identity):
                await self._verification_transition(session_id, command_id, self.owner, "running", process=identity)

            preparing = asyncio.create_task(
                asyncio.to_thread(
                    prepare_bound,
                    binding,
                    self.artifacts,
                    source,
                    commands=await run_thread(self.store.worker_commands, session_id),
                    execution=request["execution"],
                )
            )
            payload = await wait_task(preparing)
            result = await run_verification(payload, self.python, register_process=register_process)
            await self._verification_transition(session_id, command_id, self.owner, "completed", value=result)
            return result
        except BaseException as error:
            if admitted:
                retained = isinstance(error, VerificationUnresolved)
                await wait_task(asyncio.create_task(self._fail_verification(session_id, command_id, error)))
            raise
        finally:
            if admitted and not retained:
                await wait_task(asyncio.create_task(self._cleanup_verification_partial(temporary)))

    async def _fail_verification(self, session_id, command_id, error):
        state = (
            "unknown"
            if isinstance(error, VerificationUnresolved)
            else "cancelled"
            if isinstance(error, asyncio.CancelledError)
            else "failed"
        )
        latest = next(
            item
            for item in await run_thread(self.store.verification_attempts, session_id)
            if item["command_id"] == command_id
        )
        if latest["result"]["state"] not in {"completed", "failed", "cancelled"}:
            await self._verification_transition(
                session_id, command_id, self.owner, state, error=str(error) or type(error).__name__
            )

    async def _cleanup_verification_partial(self, partial):
        async with self._storage_gate:
            await run_thread(shutil.rmtree, partial, True)

    def _verification_partial(self, command_id, request):
        return verification_partial(self.root, command_id, request)

    async def _verification_transition(self, *args, **kwargs):
        changing = asyncio.create_task(asyncio.to_thread(self.store.transition_verification, *args, **kwargs))
        return await wait_task(changing)

    async def _reconcile_owned(self):
        for attempt in await run_thread(self.store.verification_attempts):
            result = attempt["result"]
            partial = self._verification_partial(attempt["command_id"], attempt["request"])
            if result["state"] in {"completed", "failed", "cancelled"}:
                await wait_task(asyncio.create_task(self._cleanup_verification_partial(partial)))
                continue
            identity = result["process_identity"]
            quiescent = identity is not None and await self._terminate_identity(identity)
            await self._verification_transition(
                attempt["session_id"],
                attempt["command_id"],
                result["owner_nonce"],
                "failed" if quiescent else "unknown",
                error="Verification interrupted; process group is absent"
                if quiescent
                else "Verification process group ownership is unresolved",
            )
            if quiescent:
                await wait_task(asyncio.create_task(self._cleanup_verification_partial(partial)))
