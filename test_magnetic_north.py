import datetime
import math
from pathlib import Path
import unittest
from unittest.mock import patch

from gpx2map import calculate_grids, latlon_to_tile
from magnetic_north import (
    MagneticDeclination,
    MagneticGrid,
    MagneticNorthError,
    calculate_declination,
    decimal_year,
    generation_date,
    wmm_declination,
)


class TestWMM2025(unittest.TestCase):
    def test_official_noaa_vectors(self):
        path = Path(__file__).parent / "tests/fixtures/WMM2025_TEST_VALUES.txt"
        rows = [line.split() for line in path.read_text().splitlines() if not line.startswith("#")]
        self.assertEqual(len(rows), 100)
        for row in rows:
            year, altitude, lat, lon, expected = map(float, row[:5])
            with self.subTest(year=year, altitude=altitude, lat=lat, lon=lon):
                actual = wmm_declination(lat, lon, year, altitude)
                # The published declination column is rounded to 0.01 degree.
                self.assertAlmostEqual(actual, expected, delta=0.005)

    def test_no_http_requests(self):
        with patch("requests.sessions.Session.request", side_effect=AssertionError("network access")):
            value = calculate_declination(35.36, 138.73, datetime.date(2026, 10, 19))
        self.assertLess(value.degrees, 0)
        self.assertEqual(value.model, "WMM2025")

    def test_generation_date_is_in_japan(self):
        with patch("magnetic_north.datetime.datetime") as clock:
            clock.now.return_value.date.return_value = datetime.date(2026, 10, 20)
            self.assertEqual(generation_date(), datetime.date(2026, 10, 20))
            self.assertEqual(clock.now.call_args.args[0].utcoffset(None), datetime.timedelta(hours=9))

    def test_decimal_year_uses_full_generation_date(self):
        self.assertEqual(decimal_year(datetime.date(2028, 7, 2)), 2028.5)
        self.assertAlmostEqual(decimal_year(datetime.date(2026, 10, 19)), 2026 + 291 / 365)
        day = datetime.date(2026, 10, 19)
        with patch("magnetic_north.generation_date", return_value=day):
            with patch("magnetic_north.wmm_declination", return_value=-8) as model:
                value = calculate_declination(35, 139)
        model.assert_called_once_with(35, 139, decimal_year(day))
        self.assertEqual(value.calculation_date, day)

    def test_dates_outside_model_lifespan_are_rejected(self):
        for day in (datetime.date(2024, 12, 31), datetime.date(2030, 1, 1)):
            with self.subTest(day=day), self.assertRaisesRegex(MagneticNorthError, "適用期間外"):
                calculate_declination(35, 139, day)
        for day in (datetime.date(2025, 1, 1), datetime.date(2029, 12, 31)):
            self.assertTrue(math.isfinite(calculate_declination(35, 139, day).degrees))

    def test_model_failure_is_explicit(self):
        with patch("pygeomag.GeoMag.calculate", side_effect=OSError("broken coefficients")):
            with self.assertRaisesRegex(MagneticNorthError, "broken coefficients"):
                calculate_declination(35, 139, datetime.date(2026, 10, 19))

    def test_invalid_coordinates_rejected(self):
        for lat, lon in ((91, 139), (35, 181), (float("nan"), 139)):
            with self.subTest(lat=lat, lon=lon), self.assertRaises(MagneticNorthError):
                calculate_declination(lat, lon, datetime.date(2026, 10, 19))

    def test_negative_degrees_and_minutes_legend(self):
        day = datetime.date(2026, 10, 19)
        for degrees, text in ((-8.5, "西偏 8°30′"), (-0.5, "西偏 0°30′"),
                              (8.5, "東偏 8°30′"), (-8.9999, "西偏 9°00′")):
            with self.subTest(degrees=degrees):
                value = MagneticDeclination(degrees, 35, 139, day)
                self.assertIn(text, value.legend_lines()[0])
                self.assertIn("WMM2025", value.legend_lines()[1])
                self.assertIn(day.isoformat(), value.legend_lines()[1])
        self.assertIn("West", MagneticDeclination(-8.5, 35, 139, day).legend_lines(False)[0])


class TestMagneticGrid(unittest.TestCase):
    def test_west_east_and_zero_directions(self):
        for degrees in (-8, 0, 8):
            with self.subTest(degrees=degrees):
                grid = MagneticGrid(degrees, 10, 10, 35)
                segments = grid.segments(7, 7, 6, 6, (1200, 1200))
                self.assertTrue(segments)
                for segment in segments:
                    top, bottom = sorted(segment, key=lambda point: point[1])
                    dx, dy = top[0] - bottom[0], bottom[1] - top[1]
                    self.assertAlmostEqual(dx / dy, math.tan(math.radians(degrees)), places=9)
                    if degrees < 0:
                        self.assertLess(dx, 0)  # West: upper end is to the left.
                    elif degrees > 0:
                        self.assertGreater(dx, 0)
                    else:
                        self.assertEqual(dx, 0)

    def test_spacing_matches_printed_scale_and_latitude(self):
        for lat in (0, 35, 60):
            for scale in (0.5, 1, 2):
                for spacing in (500, 1000, 2000):
                    with self.subTest(lat=lat, scale=scale, spacing=spacing):
                        _, width, height = calculate_grids([[(lat, 139)]], scale_factor=scale)
                        x, y = latlon_to_tile(lat, 139)
                        grid = MagneticGrid(0, x, y, lat, spacing)
                        segments = grid.segments(x - width, y - height / 2, width * 2, height, (5740, 2000))
                        xs = sorted(start[0] for start, end in segments)
                        self.assertGreaterEqual(len(xs), 2)
                        # 2870px represent 287mm, excluding the two 5mm margins.
                        expected_mm = spacing / (25000 / scale) * 1000
                        for a, b in zip(xs, xs[1:]):
                            self.assertAlmostEqual((b - a) / 10, expected_mm, places=7)

    def test_perpendicular_spacing_for_tilted_lines(self):
        grid = MagneticGrid(-8, 10, 10, 35)
        segments = grid.segments(5, 5, 10, 10, (2000, 2000))
        nx, ny = math.cos(math.radians(-8)), math.sin(math.radians(-8))
        offsets = sorted(nx * a[0] + ny * a[1] for a, b in segments)
        # Ground distance per pixel at 35 degrees, z15, 200 pixels/tile.
        expected = 1000 / (2 * math.pi * 6378137 * math.cos(math.radians(35)) / 2**15) * 200
        for a, b in zip(offsets, offsets[1:]):
            self.assertAlmostEqual(b - a, expected, places=7)

    def test_shared_lines_on_shifted_overlapping_pages(self):
        tracks = [[(35.36, 138.7), (35.42, 138.8), (35.45, 138.86)]]
        pages, width, height = calculate_grids(tracks)
        self.assertGreater(len(pages), 1)
        ox, oy = latlon_to_tile(35.4, 138.78)
        for degrees in (-8, 0, 8):
            grid = MagneticGrid(degrees, ox, oy, 35.41)
            nx, ny = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
            per_page = []
            for gx, gy in pages:
                world_segments = []
                for segment in grid.segments(gx, gy, width, height, (1600, 1100)):
                    values = []
                    world_points = []
                    for px, py in segment:
                        tx, ty = gx + px / 1600 * width, gy + py / 1100 * height
                        values.append(nx * (tx - ox) + ny * (ty - oy))
                        world_points.append((tx, ty))
                    self.assertAlmostEqual(*values, places=8)
                    world_segments.append(world_points)
                per_page.append(world_segments)
            nonempty_overlaps = 0
            for i, (ax, ay) in enumerate(pages):
                for j in range(i + 1, len(pages)):
                    bx, by = pages[j]
                    left, right = max(ax, bx), min(ax, bx) + width
                    top, bottom = max(ay, by), min(ay, by) + height
                    if left >= right or top >= bottom:
                        continue
                    mid_y = (top + bottom) / 2

                    def crossings(segments):
                        result = []
                        for (x1, y1), (x2, y2) in segments:
                            if min(y1, y2) <= mid_y <= max(y1, y2):
                                x = x1 + (mid_y - y1) * (x2 - x1) / (y2 - y1)
                                if left <= x <= right:
                                    result.append(round(x, 7))
                        return sorted(result)

                    a_crossings, b_crossings = crossings(per_page[i]), crossings(per_page[j])
                    self.assertEqual(a_crossings, b_crossings)
                    nonempty_overlaps += bool(a_crossings)
            self.assertGreater(nonempty_overlaps, 0)

    def test_segments_stay_inside_map_even_at_extreme_angles(self):
        for degrees in (-180, -90, -8, 0, 8, 90, 180):
            grid = MagneticGrid(degrees, 10, 10, 35)
            segments = grid.segments(8, 8, 4, 4, (1200, 900))
            self.assertTrue(segments)
            for segment in segments:
                for x, y in segment:
                    self.assertTrue(0 <= x <= 1200 and 0 <= y <= 900)

    def test_invalid_spacing_rejected(self):
        with self.assertRaisesRegex(MagneticNorthError, "500m"):
            MagneticGrid(-8, 10, 10, 35, 800)


if __name__ == "__main__":
    unittest.main()
