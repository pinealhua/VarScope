from __future__ import annotations

import os
import subprocess
import unittest

from varscope.core.openocd import (
    READ_CHUNK_DELIMITER,
    _build_batch_read_command,
    _encode_gdb_packet,
    _gdb_memory_read_size_from_supported,
    _hidden_subprocess_kwargs,
    _pack_read_values,
    _parse_gdb_memory_response,
    _parse_batched_read_responses,
    _parse_read_values,
    _parse_target_state,
    _plan_gdb_read_requests,
    _plan_read_chunks,
)


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

    def test_build_batch_read_command_embeds_all_chunks(self) -> None:
        command = _build_batch_read_command([(0x20000000, 32, 2), (0x20000008, 16, 1)])

        self.assertIn("read_memory 0x20000000 32 2", command)
        self.assertIn("read_memory 0x20000008 16 1", command)
        self.assertEqual(command.count(READ_CHUNK_DELIMITER), 2)

    def test_parse_batched_read_responses(self) -> None:
        response = f"{{0x1 0x2}}{READ_CHUNK_DELIMITER}{{0x3}}{READ_CHUNK_DELIMITER}"

        self.assertEqual(
            _parse_batched_read_responses(response, 2),
            ["{0x1 0x2}", "{0x3}"],
        )

    def test_encode_gdb_packet(self) -> None:
        self.assertEqual(_encode_gdb_packet("m20000000,10"), b"$m20000000,10#7c")

    def test_parse_gdb_memory_response(self) -> None:
        self.assertEqual(_parse_gdb_memory_response("0102a0ff", 4), b"\x01\x02\xa0\xff")

    def test_gdb_memory_read_size_uses_packet_size(self) -> None:
        self.assertGreater(
            _gdb_memory_read_size_from_supported("PacketSize=2000;QStartNoAckMode+"),
            _gdb_memory_read_size_from_supported("PacketSize=0400"),
        )

    def test_plan_gdb_read_requests_splits_large_reads(self) -> None:
        self.assertEqual(
            _plan_gdb_read_requests(0x20000000, 10, 4),
            [(0x20000000, 4), (0x20000004, 4), (0x20000008, 2)],
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
