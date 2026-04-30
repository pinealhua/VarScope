from __future__ import annotations

from dataclasses import dataclass, field

from varscope.models import ResolvedWatch


READ_REGION_BASE_GAP_BYTES = 256
READ_REGION_MAX_GAP_BYTES = 1024
READ_REGION_GAP_BYTES_PER_WATCH = 64
READ_REGION_BASE_MAX_SIZE = 4096
READ_REGION_MAX_SIZE_CAP = 16384
READ_REGION_SUPER_MERGE_MIN_WATCHES = 4
READ_REGION_SUPER_BASE_MAX_SIZE = 16384
READ_REGION_SUPER_MAX_SIZE_CAP = 65536


@dataclass(slots=True)
class ReadSlice:
    watch_index: int
    offset: int
    size: int


@dataclass(slots=True)
class ReadRegion:
    address: int
    size: int
    slices: list[ReadSlice] = field(default_factory=list)


def plan_read_regions(watches: list[ResolvedWatch]) -> list[ReadRegion]:
    if not watches:
        return []

    watch_count = len(watches)
    gap_budget = _read_region_gap_budget(len(watches))
    max_region_size = _read_region_size_budget(len(watches))

    spans = sorted(
        (
            (
                idx,
                watch.address,
                watch.address + watch.byte_size,
                watch.byte_size,
            )
            for idx, watch in enumerate(watches)
        ),
        key=lambda span: span[1],
    )

    regions: list[ReadRegion] = []
    current_start: int | None = None
    current_end: int | None = None

    for _, start, end, _ in spans:
        if current_start is None:
            current_start = start
            current_end = end
            continue

        if start <= current_end:
            current_end = max(current_end, end)
        elif (
            start - current_end <= gap_budget
            and end - current_start <= max_region_size
        ):
            current_end = end
        elif _can_super_merge_region(
            current_start=current_start,
            next_start=start,
            next_end=end,
            watch_count=watch_count,
        ):
            current_end = end
        else:
            regions.append(ReadRegion(address=current_start, size=current_end - current_start))
            current_start = start
            current_end = end

    if current_start is not None and current_end is not None:
        regions.append(ReadRegion(address=current_start, size=current_end - current_start))

    for idx, start, end, byte_size in spans:
        for region in regions:
            region_end = region.address + region.size
            if start >= region.address and end <= region_end:
                region.slices.append(ReadSlice(watch_index=idx, offset=start - region.address, size=byte_size))
                break

    return regions


def _read_region_gap_budget(watch_count: int) -> int:
    return min(
        READ_REGION_MAX_GAP_BYTES,
        max(READ_REGION_BASE_GAP_BYTES, watch_count * READ_REGION_GAP_BYTES_PER_WATCH),
    )


def _read_region_size_budget(watch_count: int) -> int:
    gap_budget = _read_region_gap_budget(watch_count)
    return min(
        READ_REGION_MAX_SIZE_CAP,
        max(READ_REGION_BASE_MAX_SIZE, gap_budget * 8),
    )


def _super_read_region_size_budget(watch_count: int) -> int:
    return min(
        READ_REGION_SUPER_MAX_SIZE_CAP,
        max(READ_REGION_SUPER_BASE_MAX_SIZE, watch_count * 4096),
    )


def _can_super_merge_region(
    *,
    current_start: int,
    next_start: int,
    next_end: int,
    watch_count: int,
) -> bool:
    if watch_count < READ_REGION_SUPER_MERGE_MIN_WATCHES:
        return False
    if not _is_super_merge_safe_address(current_start) or not _is_super_merge_safe_address(next_start):
        return False
    if _address_merge_bucket(current_start) != _address_merge_bucket(next_start):
        return False
    return next_end - current_start <= _super_read_region_size_budget(watch_count)


def _is_super_merge_safe_address(address: int) -> bool:
    top_nibble = (address >> 28) & 0xF
    return top_nibble in {0x0, 0x1, 0x2, 0x3}


def _address_merge_bucket(address: int) -> int:
    return address >> 24


def extract_raw_values(regions: list[ReadRegion], payloads: list[bytes], watch_count: int) -> list[bytes]:
    if len(regions) != len(payloads):
        raise ValueError("Region/payload count mismatch.")

    output: list[bytes | None] = [None] * watch_count

    for region, payload in zip(regions, payloads, strict=True):
        if len(payload) != region.size:
            raise ValueError(
                f"Payload size {len(payload)} does not match region size {region.size} at 0x{region.address:X}."
            )

        for entry in region.slices:
            raw = payload[entry.offset : entry.offset + entry.size]
            if len(raw) != entry.size:
                raise ValueError(
                    f"Slice overflow for watch {entry.watch_index} in region 0x{region.address:X}."
                )
            output[entry.watch_index] = bytes(raw)

    missing = [i for i, value in enumerate(output) if value is None]
    if missing:
        raise ValueError(f"Missing raw bytes for watch indexes: {missing}")

    return [value for value in output if value is not None]
