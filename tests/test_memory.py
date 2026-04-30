from __future__ import annotations

import unittest

from varscope.core.memory import (
    _can_super_merge_region,
    extract_raw_values,
    plan_read_regions,
    _read_region_gap_budget,
    _read_region_size_budget,
    _super_read_region_size_budget,
)
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

    def test_plan_merges_small_gaps_to_reduce_read_commands(self) -> None:
        w0 = _watch("pitch", 0x20000000, 4)
        w1 = _watch("yaw", 0x2000000C, 4)

        regions = plan_read_regions([w0, w1])

        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0].address, 0x20000000)
        self.assertEqual(regions[0].size, 16)
        self.assertEqual(
            [(entry.watch_index, entry.offset, entry.size) for entry in regions[0].slices],
            [(0, 0, 4), (1, 12, 4)],
        )

    def test_plan_keeps_large_gaps_split(self) -> None:
        w0 = _watch("pitch", 0x20000000, 4)
        w1 = _watch("yaw", 0x20000280, 4)

        regions = plan_read_regions([w0, w1])

        self.assertEqual(len(regions), 2)
        self.assertEqual((regions[0].address, regions[0].size), (0x20000000, 4))
        self.assertEqual((regions[1].address, regions[1].size), (0x20000280, 4))

    def test_plan_merges_wider_gaps_when_many_watches_are_active(self) -> None:
        watches = [
            _watch(f"v{i}", 0x20000000 + i * 0x80, 4)
            for i in range(9)
        ]

        regions = plan_read_regions(watches)

        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0].address, 0x20000000)
        self.assertEqual(regions[0].size, 0x404)
        self.assertEqual(len(regions[0].slices), 9)

    def test_region_budgets_scale_with_watch_count(self) -> None:
        self.assertLess(_read_region_gap_budget(1), _read_region_gap_budget(9))
        self.assertLess(_read_region_size_budget(1), _read_region_size_budget(9))
        self.assertLess(_super_read_region_size_budget(1), _super_read_region_size_budget(9))

    def test_plan_super_merges_safe_ram_spans_for_many_watches(self) -> None:
        watches = [
            _watch(f"v{i}", 0x20000000 + i * 0x400, 4)
            for i in range(5)
        ]

        regions = plan_read_regions(watches)

        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0].address, 0x20000000)
        self.assertEqual(regions[0].size, 0x1004)

    def test_super_merge_skips_peripheral_like_ranges(self) -> None:
        self.assertFalse(
            _can_super_merge_region(
                current_start=0x40000000,
                next_start=0x40001000,
                next_end=0x40001004,
                watch_count=8,
            )
        )


if __name__ == "__main__":
    unittest.main()
