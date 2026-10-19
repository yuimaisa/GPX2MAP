import io
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from pypdf import PdfReader
from gpx2map import (
    latlon_to_tile, tile_to_latlon, parse_gpx, is_point_in_box,
    is_segment_intersecting_grid, calculate_grids, generate_pdf,
)

FIXTURES = Path(__file__).parent / 'tests' / 'fixtures'


class TestGPX2Map(unittest.TestCase):
    def test_coordinate_conversion(self):
        lat, lon = 35.3606, 138.7273
        tx, ty = latlon_to_tile(lat, lon)
        lat_back, lon_back = tile_to_latlon(tx, ty)
        self.assertAlmostEqual(lat, lat_back, places=8)
        self.assertAlmostEqual(lon, lon_back, places=8)

    def test_parse_gpx(self):
        tracks = parse_gpx((FIXTURES / 'sample.gpx').read_bytes())
        self.assertEqual(len(tracks), 1)
        self.assertEqual(len(tracks[0]), 4)
        self.assertEqual(tracks[0][0], (35.6586, 139.7454))

    def test_segment_intersection(self):
        for p1, p2, expected in [((5, 5), (8, 8), False), ((12, 12), (8, 8), True),
                                  ((8, 12), (17, 12), True), ((8, 8), (17, 17), True),
                                  ((10, 5), (10, 20), True), ((5, 10), (20, 10), True)]:
            with self.subTest(p1=p1, p2=p2):
                self.assertEqual(is_segment_intersecting_grid(p1, p2, 10, 10, 5, 5), expected)
        self.assertTrue(is_point_in_box(10, 10, 10, 10, 5, 5))

    def test_calculate_grids_single_page(self):
        tracks = parse_gpx((FIXTURES / 'sample.gpx').read_bytes())
        self.assertEqual(len(calculate_grids(tracks)[0]), 1)

    def test_calculate_grids_multi_page(self):
        tracks = parse_gpx((FIXTURES / 'wide.gpx').read_bytes())
        self.assertGreater(len(calculate_grids(tracks)[0]), 1)

    def test_generate_pdf_structure(self):
        with patch('gpx2map.download_tile', side_effect=lambda *a, **kw: Image.new('RGBA', (256, 256), 'white')):
            pdf = generate_pdf((FIXTURES / 'sample.gpx').read_bytes())
        reader = PdfReader(io.BytesIO(pdf))
        self.assertEqual(len(reader.pages), 1)
        self.assertAlmostEqual(float(reader.pages[0].mediabox.width) / 72 * 25.4, 297, delta=0.1)
        self.assertAlmostEqual(float(reader.pages[0].mediabox.height) / 72 * 25.4, 210, delta=0.1)
        self.assertGreater(len(reader.pages[0].images), 0)

    def test_route_boundaries(self):
        tracks = parse_gpx((FIXTURES / 'boundaries.gpx').read_bytes())
        self.assertEqual(len(tracks), 4)
        self.assertEqual([len(segment) for segment in tracks], [2, 2, 2, 2])

    def test_waypoints_stay_independent(self):
        self.assertEqual([len(s) for s in parse_gpx((FIXTURES / 'waypoints.gpx').read_bytes())], [1, 1])

    def test_known_page_gap_is_covered(self):
        tracks = parse_gpx((FIXTURES / 'coverage_gap.gpx').read_bytes())
        grids, width, height = calculate_grids(tracks)
        start, end = [latlon_to_tile(*p)[0] for p in tracks[0]]
        y = latlon_to_tile(*tracks[0][0])[1]
        cursor = start
        for left, right in sorted((x, x + width) for x, top in grids if top <= y <= top + height):
            if right < cursor:
                continue
            self.assertLessEqual(left, cursor + 1e-9, 'Track has an uncovered interval between pages')
            cursor = max(cursor, right)
        self.assertGreaterEqual(cursor, end)


if __name__ == '__main__':
    unittest.main()
