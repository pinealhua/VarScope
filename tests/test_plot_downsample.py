from __future__ import annotations

import unittest

import numpy as np

from varscope.ui.main_window import (
    _downsample,
    _format_plain_tick,
    _latest_window_span,
    _nearest_index,
    _next_color,
    _main_plot_point_budget,
    _overview_plot_point_budget,
    _plot_refresh_interval_ns,
)


class PlotDownsampleTests(unittest.TestCase):
    def test_periodic_signal_keeps_extrema(self) -> None:
        x = np.arange(10_000, dtype=float)
        y = np.sin(2.0 * np.pi * x / 100.0)

        down_x, down_y = _downsample(x, y, 100)

        self.assertLessEqual(down_x.size, 100)
        self.assertGreaterEqual(down_x.size, 10)
        self.assertGreater(float(np.nanmax(down_y)), 0.9)
        self.assertLess(float(np.nanmin(down_y)), -0.9)

    def test_plain_tick_format_avoids_scientific_notation(self) -> None:
        self.assertEqual(_format_plain_tick(0.001234, 0.0001), "0.00123")
        self.assertEqual(_format_plain_tick(141.5, 0.5), "141.5")
        self.assertNotIn("e", _format_plain_tick(0.0000123, 0.000001).lower())

    def test_nearest_index_uses_sorted_times(self) -> None:
        self.assertEqual(_nearest_index([0.0, 0.1, 0.2], 0.14), 1)
        self.assertEqual(_nearest_index([0.0, 0.1, 0.2], 0.16), 2)
        self.assertIsNone(_nearest_index([], 1.0))

    def test_plot_budget_scales_down_for_many_series(self) -> None:
        self.assertLess(_main_plot_point_budget(20), _main_plot_point_budget(2))
        self.assertLess(_overview_plot_point_budget(20), _overview_plot_point_budget(2))
        self.assertGreater(_plot_refresh_interval_ns(20), _plot_refresh_interval_ns(2))

    def test_next_color_reuses_free_palette_slot(self) -> None:
        used = ["#3BA7FF", "#53C267"]

        color = _next_color(used)

        self.assertEqual(color, "#FF7A59")
        self.assertNotIn(color.upper(), {item.upper() for item in used})

    def test_next_color_generates_unique_colors_after_palette(self) -> None:
        used: list[str] = []
        for _ in range(20):
            color = _next_color(used)
            self.assertNotIn(color.upper(), {item.upper() for item in used})
            used.append(color)

        self.assertEqual(len({color.upper() for color in used}), len(used))

    def test_all_nan_bucket_is_safe(self) -> None:
        x = np.arange(512, dtype=float)
        y = np.full(512, np.nan, dtype=float)

        down_x, down_y = _downsample(x, y, 64)

        self.assertGreater(down_x.size, 0)
        self.assertEqual(down_x.size, down_y.size)
        self.assertTrue(np.isnan(down_y).all())

    def test_latest_window_span_jumps_to_fixed_tail_window(self) -> None:
        self.assertEqual(_latest_window_span([0.0, 2.0, 8.0], 5.0), (3.0, 8.0))
        self.assertEqual(_latest_window_span([2.0, 3.0, 4.0], 5.0), (2.0, 4.0))
        self.assertIsNone(_latest_window_span([], 5.0))


if __name__ == "__main__":
    unittest.main()
