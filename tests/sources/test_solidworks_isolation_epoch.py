"""Windows boot-epoch tick selection (isolation) without a Windows host.

Coverage note (test blind spot, third window case): the Windows-only branch -
ctypes calling the machine's real ``kernel32`` - cannot run in Linux CI and is
covered by the P3 machine window (staged runtime calls GetTickCount64 and
compares it with a known boot moment); the tests here inject the kernel32
surface so the selection logic and the fail-closed path are Linux-runnable.
"""

from __future__ import annotations

import unittest

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.sources.solidworks import isolation  # noqa: E402


class WindowsBootEpochTests(unittest.TestCase):
    def test_boot_epoch_math_from_injected_ticks(self) -> None:
        self.assertEqual(isolation._windows_boot_epoch(now=1_000.0, uptime_ms=250_000), 750.0)
        self.assertEqual(isolation._windows_boot_epoch(now=1_000.0, uptime_ms=0), 1_000.0)

    def test_uptime_reads_the_64_bit_tick(self) -> None:
        class Full:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def GetTickCount64(self) -> int:  # noqa: N802 - Windows API name
                self.calls.append("64")
                return 123_456_789

            def GetTickCount(self) -> int:  # noqa: N802 - must not be used
                self.calls.append("32")
                raise AssertionError("the 32-bit tick is never used")

        kernel32 = Full()
        self.assertEqual(isolation._windows_uptime_ms(kernel32), 123_456_789)
        self.assertEqual(kernel32.calls, ["64"])

    def test_missing_64_bit_tick_fails_closed(self) -> None:
        # The 32-bit tick wraps every ~49.7 days; a wrapped value would corrupt
        # the boot/creation discrimination, so it is refused outright.
        class Legacy:
            def GetTickCount(self) -> int:  # noqa: N802 - Windows API name
                return 4_000_000_000

        class Broken:
            pass

        for kernel32 in (Legacy(), Broken()):
            with self.assertRaises(AttributeError) as raised:
                isolation._windows_uptime_ms(kernel32)
            self.assertIn("GetTickCount64", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
