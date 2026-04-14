from __future__ import annotations

import os
import subprocess
import unittest

from varscope.core.openocd import _hidden_subprocess_kwargs, _pack_read_values, _parse_read_values, _parse_target_state, _plan_read_chunks


class OpenOcdHelpersTests(unittest.TestCase):
    def test_plan_read_chunks_prefers_32bit_reads(self) -> None:
        self.assertEqual(_plan_read_chunks(0x20000000, 12), [(0x20000000, 32, 3)])

    def test_plan_read_chunks_handles_misaligned_prefix(self) -> None:
        self.assertEqual(
            _plan_read_chunks(0x20000001, 7),
            [
                (0x20000001, 8, 1),
                (0x20000002, 16, 1),
                (0x20000004, 32, 1),
            ],
        )

    def test_parse_and_pack_values(self) -> None:
        values = _parse_read_values("{0x11223344 0xAABBCCDD}")
        self.assertEqual(values, [0x11223344, 0xAABBCCDD])
        self.assertEqual(
            _pack_read_values(values, 32),
            b"\x44\x33\x22\x11\xDD\xCC\xBB\xAA",
        )

    def test_hidden_subprocess_kwargs_hides_windows_console(self) -> None:
        kwargs = _hidden_subprocess_kwargs()
        if os.name != "nt":
            self.assertEqual(kwargs, {})
            return

        self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
        startupinfo = kwargs["startupinfo"]
        self.assertTrue(startupinfo.dwFlags & subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(startupinfo.wShowWindow, subprocess.SW_HIDE)

    def test_parse_target_state(self) -> None:
        self.assertEqual(
            _parse_target_state("* stm32f4x.cpu cortex_m little running"),
            "running",
        )
        self.assertEqual(
            _parse_target_state("* stm32f4x.cpu cortex_m little halted"),
            "halted",
        )
        self.assertIsNone(_parse_target_state("no target info"))


if __name__ == "__main__":
    unittest.main()
