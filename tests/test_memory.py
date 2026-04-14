from __future__ import annotations

import unittest

from varscope.core.memory import extract_raw_values, plan_read_regions
from varscope.core.typesystem import TypeInfo
from varscope.models import ResolvedWatch, WatchDefinition


def _watch(expression: str, address: int, byte_size: int = 2) -> ResolvedWatch:
    type_info = TypeInfo(kind="base", name=f"uint{byte_size * 8}_t", byte_size=byte_size)
    return ResolvedWatch(
        definition=WatchDefinition(expression=expression),
        address=address,
        byte_size=byte_size,
        type_name=type_info.name,
        decoder_kind="uint",
        numeric=True,
        description="",
        type_info=type_info,
    )


class MemoryPlanningTests(unittest.TestCase):
    def test_plan_and_extract(self) -> None:
        w0 = _watch("a", 0x20000000)
        w1 = _watch("b", 0x20000002)

        regions = plan_read_regions([w0, w1])
        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0].address, 0x20000000)
        self.assertEqual(regions[0].size, 4)

        values = extract_raw_values(regions, [b"\x11\x22\x33\x44"], 2)
        self.assertEqual(values, [b"\x11\x22", b"\x33\x44"])

    def test_plan_sorts_by_address_not_watch_order(self) -> None:
        watches = [
            _watch("Eulr.pitch", 0x2000A468, 4),
            _watch("Eulr.roll", 0x2000A46C, 4),
            _watch("Eulr.yaw", 0x2000A464, 4),
        ]

        regions = plan_read_regions(watches)

        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0].address, 0x2000A464)
        self.assertEqual(regions[0].size, 12)
        self.assertEqual(
            [(entry.watch_index, entry.offset, entry.size) for entry in regions[0].slices],
            [(2, 0, 4), (0, 4, 4), (1, 8, 4)],
        )
        values = extract_raw_values(regions, [b"yyyypppprrrr"], 3)
        self.assertEqual(values, [b"pppp", b"rrrr", b"yyyy"])


if __name__ == "__main__":
    unittest.main()
