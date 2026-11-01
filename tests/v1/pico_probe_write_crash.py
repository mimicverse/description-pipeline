"""Crash a real startup owner at selected writes of its existing lock inode."""

import asyncio
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import time

from description_pipeline.owned_process import process_identity, resource_state
from description_pipeline.simulation.service import SimulationService


async def main():
    service = SimulationService(None, None, Path(sys.argv[1]), Path(sys.argv[2]))
    marker, point = Path(sys.argv[3]), sys.argv[4]
    original = service._write_probe_identity
    writes, last_identity = 0, None
    target = 1 if point == "intent_truncate" else 2 if point.startswith("receipt_") else 4

    def crash(stream):
        identity = last_identity
        if service._probe is not None:
            identity = process_identity(service._probe.pid, service._probe_identity["nonce"])
        if identity is not None and resource_state(identity) != "dead":
            descriptor = os.pidfd_open(identity["pid"])
            try:
                if process_identity(identity["pid"], identity["nonce"]) != identity:
                    raise ValueError("Fixture process identity changed")
                signal.pidfd_send_signal(descriptor, signal.SIGSTOP)
            finally:
                os.close(descriptor)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                stat = Path(f"/proc/{identity['pid']}/stat").read_text()
                if stat[stat.rfind(")") + 2 :].split()[0] == "T":
                    break
                time.sleep(0.005)
            else:
                raise TimeoutError("Fixture probe did not stop")
        with marker.open("w") as output:
            json.dump(
                {
                    "point": point,
                    "identity": identity,
                    "inode": os.fstat(stream.fileno()).st_ino,
                    "append": bool(fcntl.fcntl(stream.fileno(), fcntl.F_GETFL) & os.O_APPEND),
                },
                output,
            )
            output.flush()
            os.fsync(output.fileno())
        os._exit(73)

    class CrashStream:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def write(self, payload):
            if writes == target and point in {"receipt_partial", "clear_before_truncate"}:
                self.stream.write(payload[:-1] if point == "receipt_partial" else payload)
                self.stream.flush()
                os.fsync(self.stream.fileno())
                crash(self.stream)
            return self.stream.write(payload)

        def truncate(self, *args, **kwargs):
            result = self.stream.truncate(*args, **kwargs)
            if writes == target and point.endswith("truncate"):
                self.stream.flush()
                os.fsync(self.stream.fileno())
                crash(self.stream)
            return result

    def intercepted_write():
        nonlocal writes, last_identity
        writes += 1
        if writes == 1:
            service._lock = CrashStream(service._lock)
        if service._probe_identity is not None and "start_ticks" in service._probe_identity:
            last_identity = dict(service._probe_identity)
        original()

    service._write_probe_identity = intercepted_write
    await service.start()


if __name__ == "__main__":
    asyncio.run(main())
