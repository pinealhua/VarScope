from __future__ import annotations

import numpy as np


BUFFER_COMPACT_MIN_DROP = 2048
INITIAL_BUFFER_CAPACITY = 1024


class PlotHistoryBuffer:
    def __init__(self, watch_count: int = 0) -> None:
        self._times = np.empty(0, dtype=np.float64)
        self._values: list[np.ndarray] = []
        self._start = 0
        self._length = 0
        self.reset(watch_count)

    def __bool__(self) -> bool:
        return len(self) > 0

    def __len__(self) -> int:
        return self._length - self._start

    @property
    def watch_count(self) -> int:
        return len(self._values)

    def reset(self, watch_count: int) -> None:
        self._times = np.empty(0, dtype=np.float64)
        self._values = [np.empty(0, dtype=np.float64) for _ in range(max(0, watch_count))]
        self._start = 0
        self._length = 0

    def append_batch(self, timestamps: list[float], values_by_watch: list[list[float]]) -> None:
        if not timestamps:
            return
        if len(values_by_watch) != self.watch_count:
            raise ValueError("Watch/value column count mismatch.")

        sample_count = len(timestamps)
        for values in values_by_watch:
            if len(values) != sample_count:
                raise ValueError("Per-watch sample count mismatch.")

        self._ensure_capacity(sample_count)
        end = self._length
        new_end = end + sample_count
        self._times[end:new_end] = timestamps
        for target, values in zip(self._values, values_by_watch, strict=True):
            target[end:new_end] = values
        self._length = new_end

    def trim(self, history_seconds: float, max_points: int) -> int:
        if not self:
            return 0

        latest = float(self._times[self._length - 1])
        cutoff = latest - history_seconds
        new_start = int(np.searchsorted(self._times[self._start : self._length], cutoff, side="left"))
        new_start += self._start
        remaining = self._length - new_start
        if remaining > max_points:
            new_start += remaining - max_points
        if new_start <= self._start:
            return 0

        dropped = new_start - self._start
        self._start = new_start
        self._compact_if_needed()
        return dropped

    def times_view(self) -> np.ndarray:
        return self._times[self._start : self._length]

    def values_view(self, watch_index: int) -> np.ndarray:
        return self._values[watch_index][self._start : self._length]

    def time_at(self, index: int) -> float:
        return float(self._times[self._absolute_index(index)])

    def value_at(self, watch_index: int, index: int) -> float:
        return float(self._values[watch_index][self._absolute_index(index)])

    def _absolute_index(self, index: int) -> int:
        if index < 0 or index >= len(self):
            raise IndexError(index)
        return self._start + index

    def _ensure_capacity(self, append_count: int) -> None:
        required = self._length + append_count
        if required <= self._times.size:
            return

        active_length = len(self)
        if self._start > 0:
            compacted_capacity = max(required - self._start, active_length)
            self._reallocate(compacted_capacity)
            required = self._length + append_count
            if required <= self._times.size:
                return

        grown_capacity = max(required, INITIAL_BUFFER_CAPACITY, max(self._times.size * 2, active_length * 2))
        self._reallocate(grown_capacity)

    def _compact_if_needed(self) -> None:
        if self._start < BUFFER_COMPACT_MIN_DROP:
            return
        if self._start * 2 < self._length:
            return

        self._reallocate(max(len(self), INITIAL_BUFFER_CAPACITY))

    def _reallocate(self, new_capacity: int) -> None:
        active_length = len(self)
        capacity = max(new_capacity, active_length)
        new_times = np.empty(capacity, dtype=np.float64)
        active_times = self._times[self._start : self._length]
        if active_length:
            new_times[:active_length] = active_times

        new_values: list[np.ndarray] = []
        for values in self._values:
            new_column = np.empty(capacity, dtype=np.float64)
            active_values = values[self._start : self._length]
            if active_length:
                new_column[:active_length] = active_values
            new_values.append(new_column)

        self._times = new_times
        self._values = new_values
        self._start = 0
        self._length = active_length
