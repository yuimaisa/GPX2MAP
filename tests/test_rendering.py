from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import io
import math
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw
from pypdf import PdfReader
import gpx2map
from map_plan import DEFAULT_LIMITS, GPX2MapError, ResourceLimitError, latlon_to_tile, plan_pdf
from tile_store import TileDownloadError

FIXTURES = Path(__file__).parent / 'fixtures'
ROOT = Path(__file__).resolve().parents[1]


def blank_tile(*args, **kwargs):
    return Image.new('RGBA', (256, 256), 'white')


class RenderingTests(unittest.TestCase):
    def test_pdf_pages_match_plan_and_are_a4_landscape(self):
        data = (FIXTURES / 'coverage_gap.gpx').read_bytes()
        plan = plan_pdf(data)
        events = []
        with patch('gpx2map.download_tile', side_effect=blank_tile):
            result = gpx2map.generate_pdf(data, plan=plan, progress_callback=events.append)
        reader = PdfReader(io.BytesIO(result))
        self.assertEqual(len(reader.pages), plan.page_count)
        for page in reader.pages:
            self.assertAlmostEqual(float(page.mediabox.width) / 72 * 25.4, 297, places=4)
            self.assertAlmostEqual(float(page.mediabox.height) / 72 * 25.4, 210, places=4)
            self.assertEqual(len(page.images), 1)
            self.assertEqual(page.images[0].image.size, (3508, 2480))
        self.assertEqual(events[0].stage, 'waiting')
        self.assertEqual(events[-1].stage, 'complete')
        self.assertEqual(events[-1].tiles, plan.tile_count)
        self.assertEqual([p.page for p in events if p.stage == 'page'], list(range(1, plan.page_count + 1)))
        self.assertEqual(sorted(p.tiles for p in events), [p.tiles for p in events])

    def test_printed_track_distance_has_requested_scale(self):
        # 0.01 degree of longitude at latitude 35: about 912 m horizontally.
        data = b'<gpx><rte><rtept lat="35" lon="139"/><rtept lat="35" lon="139.01"/></rte></gpx>'
        with patch('gpx2map.download_tile', side_effect=blank_tile):
            result = gpx2map.generate_pdf(data)
        image = PdfReader(io.BytesIO(result)).pages[0].images[0].image.convert('RGB')
        # The route is centered vertically and is well away from the legends.
        xs = [x for x in range(image.width) if any(
            (lambda c: c[0] > 150 and c[0] > c[1] * 1.8 and c[0] > c[2] * 1.8)(image.getpixel((x, y)))
            for y in range(image.height // 2 - 8, image.height // 2 + 9))]
        self.assertTrue(xs)
        printed_m = (max(xs) - min(xs)) / image.width * 0.297
        expected_ground_m = 6378137 * math.cos(math.radians(35)) * math.radians(0.01)
        self.assertAlmostEqual(printed_m * 25000 / expected_ground_m, 1.0, delta=0.01)
        image.close()

    def test_disconnected_routes_do_not_get_a_connecting_line(self):
        data = b'<gpx><rte><rtept lat="35" lon="138.99"/><rtept lat="35" lon="138.995"/></rte><rte><rtept lat="35" lon="139.005"/><rtept lat="35" lon="139.01"/></rte></gpx>'
        with patch('gpx2map.download_tile', side_effect=blank_tile):
            result = gpx2map.generate_pdf(data)
        image = PdfReader(io.BytesIO(result)).pages[0].images[0].image
        for x in range(image.width // 2 - 10, image.width // 2 + 11):
            r, g, b = image.getpixel((x, image.height // 2))
            self.assertLess(abs(r - g), 10)
            self.assertGreater(b, 240)

    def test_missing_tile_produces_no_pdf_and_no_complete_event(self):
        events = []
        with patch('gpx2map.download_tile', side_effect=TileDownloadError('std', 15, 1, 2, 'HTTP 404')):
            with self.assertRaises(TileDownloadError):
                gpx2map.generate_pdf((FIXTURES / 'sample.gpx').read_bytes(), progress_callback=events.append)
        self.assertNotIn('complete', [event.stage for event in events])

    def test_stale_plan_is_rejected_before_rendering(self):
        data = (FIXTURES / 'sample.gpx').read_bytes()
        plan = plan_pdf(data)
        with patch('gpx2map.download_tile') as download:
            with self.assertRaises(GPX2MapError):
                gpx2map.generate_pdf(data, scale_factor=0.5, plan=plan)
            download.assert_not_called()

    def test_compressed_pdf_budget_is_enforced(self):
        limits = replace(DEFAULT_LIMITS, pdf_bytes=100)
        with patch('gpx2map.download_tile', side_effect=blank_tile), self.assertRaises(ResourceLimitError):
            gpx2map.generate_pdf((FIXTURES / 'sample.gpx').read_bytes(), limits=limits)

    def test_layout_uses_all_planned_pages(self):
        plan = plan_pdf((FIXTURES / 'coverage_gap.gpx').read_bytes())
        with gpx2map.build_layout_preview(plan) as image:
            colors = image.get_flattened_data()
            self.assertTrue(any(r > 180 and g < 80 for r, g, b in colors))
            self.assertTrue(any(b > r + 30 for r, g, b in colors))

    def test_signed_declination_notation(self):
        for value, expected in [('-7°30′', -7.5), ('-0°30\'', -0.5), ('+7°30', 7.5), ('-7.5', -7.5)]:
            with self.subTest(value=value):
                self.assertEqual(gpx2map.parse_declination(value), expected)
        for value in ('nan', '7°70', '190'):
            with self.assertRaises(ValueError):
                gpx2map.parse_declination(value)

    def test_east_and_west_magnetic_lines_have_correct_direction(self):
        original = ImageDraw.ImageDraw.line
        for angle in (-7.5, 7.5):
            lines = []
            def capture(draw, xy, *args, **kwargs):
                if kwargs.get('fill') == (255, 50, 50, 150):
                    lines.append(xy)
                return original(draw, xy, *args, **kwargs)
            with Image.new('RGBA', (1000, 700), 'white') as image, patch.object(ImageDraw.ImageDraw, 'line', capture):
                gpx2map.draw_track_and_credits(image, [], 0, 0, 1, 1, declination=angle)
            self.assertTrue(lines)
            for north, south in lines:
                self.assertLess(north[1], south[1])
                self.assertEqual(north[0] > south[0], angle > 0)
        self.assertIsNone(gpx2map.fetch_magnetic_declination(35, 139, 2026))

    def test_two_sessions_keep_settings_and_cache_paths_separate(self):
        data = (FIXTURES / 'sample.gpx').read_bytes()
        original_environment = os.environ.get('GPX2MAP_CACHE_DIR')
        calls = []
        def tile(x, y, z, kind, **kwargs):
            calls.append((kind, kwargs['cache_dir']))
            return Image.new('RGBA', (256, 256), 'white' if kind == 'std' else (160, 190, 220))
        with tempfile.TemporaryDirectory() as root, patch('gpx2map.download_tile', side_effect=tile):
            paths = [str(Path(root) / name) for name in ('a', 'b')]
            with ThreadPoolExecutor(2) as executor:
                futures = [executor.submit(gpx2map.generate_pdf, data, kind, scale, cache_dir=path)
                           for kind, scale, path in [('std', 1.0, paths[0]), ('pale', 0.5, paths[1])]]
                results = [f.result() for f in futures]
            self.assertTrue(all(p.startswith(b'%PDF') for p in results))
            self.assertEqual(set(calls), {('std', paths[0]), ('pale', paths[1])})
        self.assertEqual(os.environ.get('GPX2MAP_CACHE_DIR'), original_environment)


class CLITests(unittest.TestCase):
    def test_missing_file_is_nonzero_with_stderr(self):
        result = subprocess.run([os.sys.executable, str(ROOT / 'gpx2map.py'), '-i', '/nonexistent-gpx2map.gpx'], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('エラー'.encode(), result.stderr)
        self.assertEqual(result.stdout, b'')

    def test_invalid_scale_does_not_write_pdf(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'map.pdf'
            self.assertEqual(gpx2map.main(['-i', str(FIXTURES / 'sample.gpx'), '-o', str(output), '-s', '0']), 1)
            self.assertFalse(output.exists())

    def test_failed_generation_does_not_replace_existing_output(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'map.pdf'
            output.write_bytes(b'previous output')
            with patch('gpx2map.download_tile', side_effect=TileDownloadError('std', 15, 1, 2, 'HTTP 403')):
                self.assertEqual(gpx2map.main(['-i', str(FIXTURES / 'sample.gpx'), '-o', str(output)]), 1)
            self.assertEqual(output.read_bytes(), b'previous output')

    def test_success_exit_code_and_pdf(self):
        with tempfile.TemporaryDirectory() as folder, patch('gpx2map.download_tile', side_effect=blank_tile):
            output = Path(folder) / 'map.pdf'
            self.assertEqual(gpx2map.main(['-i', str(FIXTURES / 'sample.gpx'), '-o', str(output)]), 0)
            self.assertEqual(len(PdfReader(output).pages), 1)
