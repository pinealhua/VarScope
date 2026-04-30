from __future__ import annotations

import unittest

from varscope.ui.plot_history import PlotHistoryBuffer


class PlotHistoryBufferTests(unittest.TestCase):
    def test_append_after_exposing_views_does_not_raise(self) -> None:
        history = PlotHistoryBuffer(1)
        history.append_batch([0.0, 1.0], [[5.0, 6.0]])

        old_times = history.times_view()
        old_values = history.values_view(0)

        history.append_batch([2.0, 3.0], [[7.0, 8.0]])

        self.assertEqual(list(old_times), [0.0, 1.0])
        self.assertEqual(list(old_values), [5.0, 6.0])
        self.assertEqual(list(history.times_view()), [0.0, 1.0, 2.0, 3.0])
        self.assertEqual(list(history.values_view(0)), [5.0, 6.0, 7.0, 8.0])

    def test_trim_keeps_tail_window_and_latest_points(self) -> None:
        history = PlotHistoryBuffer(2)
        history.append_batch(
            [0.0, 0.5, 1.0, 1.5, 2.0],
            [
                [10.0, 11.0, 12.0, 13.0, 14.0],
                [20.0, 21.0, 22.0, 23.0, 24.0],
            ],
        )

        history.trim(history_seconds=1.0, max_points=3)

        self.assertEqual(list(history.times_view()), [1.0, 1.5, 2.0])
        self.assertEqual(list(history.values_view(0)), [12.0, 13.0, 14.0])
        self.assertEqual(list(history.values_view(1)), [22.0, 23.0, 24.0])

    def test_time_and_value_access_follow_trimmed_offset(self) -> None:
        history = PlotHistoryBuffer(1)
        history.append_batch([0.0, 1.0, 2.0], [[5.0, 6.0, 7.0]])

        history.trim(history_seconds=0.6, max_points=10)

        self.assertEqual(len(history), 1)
        self.assertEqual(history.time_at(0), 2.0)
        self.assertEqual(history.value_at(0, 0), 7.0)


if __name__ == "__main__":
    unittest.main()
