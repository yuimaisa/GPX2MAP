from dataclasses import replace
import math
from pathlib import Path
import random
import unittest
from unittest.mock import patch

from map_plan import (
    DEFAULT_LIMITS, GPX2MapError, ResourceLimitError, inspect_gpx, latlon_to_tile,
    plan_pdf, verify_coverage, tile_to_latlon,
)

FIXTURES = Path(__file__).parent / 'fixtures'


def gpx(body):
    return f'<gpx version="1.1" creator="test">{body}</gpx>'.encode()


class PlanningTests(unittest.TestCase):
    def test_summary_keeps_all_boundaries(self):
        doc = inspect_gpx((FIXTURES / 'boundaries.gpx').read_bytes())
        self.assertEqual((doc.track_count, doc.track_segment_count, doc.route_count, doc.point_count), (1, 2, 2, 8))
        self.assertEqual(len(doc.segments), 4)
        self.assertLess(doc.distance_m, 1000, 'Distance must exclude artificial links')

    def test_multiple_tracks_single_point_and_waypoints(self):
        doc = inspect_gpx(gpx('<trk><trkseg><trkpt lat="35" lon="139"/></trkseg></trk>'
                              '<trk><trkseg><trkpt lat="36" lon="140"/></trkseg></trk><wpt lat="37" lon="141"/>'))
        self.assertEqual(doc.track_count, 2)
        self.assertEqual(doc.point_count, 3)
        self.assertEqual(doc.waypoint_count, 1)
        self.assertEqual(doc.distance_m, 0)

    def test_invalid_xml_empty_and_no_points(self):
        for data in (b'', b' ', b'<', b'<root/>', gpx(''), gpx('<trk><trkseg/></trk>')):
            with self.subTest(data=data), self.assertRaises(GPX2MapError):
                inspect_gpx(data)

    def test_dtd_and_entities_are_rejected(self):
        with self.assertRaises(GPX2MapError):
            inspect_gpx(b'<!DOCTYPE gpx [<!ENTITY p "abc">]><gpx><name>&p;</name></gpx>')

    def test_excessively_nested_xml_is_rejected(self):
        with self.assertRaises(ResourceLimitError):
            inspect_gpx(gpx('<extensions>' * 40 + '</extensions>' * 40))

    def test_invalid_coordinates(self):
        for lat, lon in (('nan', '139'), ('inf', '139'), ('90', '139'), ('-90', '139'), ('35', '181'), ('35', '-181'), ('x', '139')):
            with self.subTest(lat=lat, lon=lon), self.assertRaises(GPX2MapError):
                inspect_gpx(gpx(f'<wpt lat="{lat}" lon="{lon}"/>'))
        with self.assertRaises(GPX2MapError):
            inspect_gpx(gpx('<wpt lat="35"/>'))

    def test_mercator_coordinate_boundaries_round_trip(self):
        for point in ((0, 0), (85.0511287798066, 180), (-85.0511287798066, -180)):
            result = tile_to_latlon(*latlon_to_tile(*point))
            self.assertAlmostEqual(result[0], point[0], places=7)
            self.assertAlmostEqual(result[1], point[1], places=7)

    def test_byte_and_point_limits(self):
        data = (FIXTURES / 'sample.gpx').read_bytes()
        for limits in (replace(DEFAULT_LIMITS, input_bytes=10), replace(DEFAULT_LIMITS, points=3)):
            with self.subTest(limits=limits), self.assertRaises(ResourceLimitError):
                inspect_gpx(data, limits)

    def test_invalid_settings_are_rejected(self):
        data = (FIXTURES / 'sample.gpx').read_bytes()
        for name, values in [('scale_factor', [0, -1, math.nan, math.inf, 0.01, 5, True]),
                             ('overlap', [-1, 2, math.nan]), ('page_margin_mm', [-1, 21, math.inf]),
                             ('map_type', ['unknown'])]:
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaises(GPX2MapError):
                    plan_pdf(data, **{name: value})

    def test_resource_rejection_happens_before_download(self):
        data = (FIXTURES / 'sample.gpx').read_bytes()
        with patch('gpx2map.download_tile') as download:
            from gpx2map import generate_pdf
            for limits in (replace(DEFAULT_LIMITS, tiles=1), replace(DEFAULT_LIMITS, memory_bytes=1), replace(DEFAULT_LIMITS, candidate_cells=0)):
                with self.subTest(limits=limits), self.assertRaises(ResourceLimitError):
                    generate_pdf(data, limits=limits)
            download.assert_not_called()

    def test_large_page_and_many_pages_are_rejected(self):
        with self.assertRaises(ResourceLimitError):
            plan_pdf((FIXTURES / 'sample.gpx').read_bytes(), scale_factor=0.1)
        with self.assertRaises(ResourceLimitError):
            plan_pdf((FIXTURES / 'wide.gpx').read_bytes(), limits=replace(DEFAULT_LIMITS, pages=1))

    def test_world_spanning_track_is_rejected_before_large_grid(self):
        with self.assertRaises(ResourceLimitError):
            plan_pdf(gpx('<rte><rtept lat="35" lon="-179"/><rtept lat="36" lon="179"/></rte>'))

    def test_sparse_diagonal_and_random_tracks_are_covered(self):
        rng = random.Random(42)
        for _ in range(12):
            points = [(35 + rng.uniform(-0.03, 0.03), 139 + rng.uniform(-0.1, 0.1)) for _ in range(5)]
            data = gpx('<rte>' + ''.join(f'<rtept lat="{lat}" lon="{lon}"/>' for lat, lon in points) + '</rte>')
            plan = plan_pdf(data)
            tile_points = [latlon_to_tile(*point) for point in points]
            # Independently sample every line, including sections with no recorded points.
            for a, b in zip(tile_points, tile_points[1:]):
                for sample in range(101):
                    t = sample / 100
                    x, y = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
                    self.assertTrue(any(gx - 1e-8 <= x <= gx + plan.page_w_tiles + 1e-8 and gy - 1e-8 <= y <= gy + plan.page_h_tiles + 1e-8 for gx, gy in plan.grids))

    def test_coverage_checker_detects_interior_gap(self):
        self.assertFalse(verify_coverage((((0, 0), (10, 0)),), [(0, -1), (6, -1)], 4, 2))
        self.assertTrue(verify_coverage((((0, 0), (10, 0)),), [(0, -1), (4, -1), (8, -1)], 4, 2))

    def test_page_edges_and_zero_overlap_are_covered(self):
        plan = plan_pdf((FIXTURES / 'coverage_gap.gpx').read_bytes(), overlap=0)
        segments = [[latlon_to_tile(*p) for p in s] for s in plan.document.segments]
        self.assertTrue(verify_coverage(segments, plan.grids, plan.page_w_tiles, plan.page_h_tiles))
        self.assertEqual(plan.page_count, 2)
        self.assertAlmostEqual(plan.grids[1][0], plan.grids[0][0] + plan.page_w_tiles)

    def test_north_south_scale_variation_is_bounded(self):
        data = gpx('<rte><rtept lat="34" lon="139"/><rtept lat="36" lon="139"/></rte>')
        with self.assertRaises(ResourceLimitError):
            plan_pdf(data, scale_factor=0.3, limits=replace(DEFAULT_LIMITS, tiles=10000, pages=100))

    def test_unverified_magnetic_north_is_rejected(self):
        with self.assertRaisesRegex(GPX2MapError, '磁北線'):
            plan_pdf((FIXTURES / 'sample.gpx').read_bytes(), draw_magnetic_north=True)

    def test_wrong_document_is_rejected(self):
        data = (FIXTURES / 'sample.gpx').read_bytes()
        doc = inspect_gpx((FIXTURES / 'coverage_gap.gpx').read_bytes())
        with self.assertRaises(GPX2MapError):
            plan_pdf(data, document=doc)
