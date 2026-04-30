from __future__ import annotations

import unittest

from varscope.core.decoder import compile_payload_decoder, decode_value
from varscope.core.typesystem import TypeInfo


class DecoderCompilerTests(unittest.TestCase):
    def test_compiled_integer_decoder_matches_decode_value(self) -> None:
        type_info = TypeInfo(kind="base", name="int32_t", byte_size=4, signed=True)
        payload = b"\xd6\xff\xff\xff"

        decoder = compile_payload_decoder(type_info)

        self.assertEqual(decoder(payload, 0), decode_value(payload, type_info))

    def test_compiled_float_decoder_reads_from_offset(self) -> None:
        type_info = TypeInfo(kind="base", name="float", byte_size=4, floating=True)
        payload = b"\x00\x00\x00\x00\x00\x00\x80?"

        decoder = compile_payload_decoder(type_info)

        self.assertEqual(decoder(payload, 4), 1.0)

    def test_compiled_enum_decoder_preserves_named_values(self) -> None:
        type_info = TypeInfo(
            kind="enum",
            name="Mode",
            byte_size=4,
            signed=False,
            enum_values={2: "RUN"},
        )
        payload = b"\x02\x00\x00\x00"

        decoder = compile_payload_decoder(type_info)

        self.assertEqual(decoder(payload, 0), "RUN (2)")

    def test_compiled_pointer_decoder_preserves_hex_format(self) -> None:
        type_info = TypeInfo(kind="pointer", name="void*", byte_size=4)
        payload = b"\x78\x56\x34\x12"

        decoder = compile_payload_decoder(type_info)

        self.assertEqual(decoder(payload, 0), "0x12345678")


if __name__ == "__main__":
    unittest.main()
