from __future__ import annotations

from dataclasses import dataclass, field

from varscope.models import ResolvedWatch


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
