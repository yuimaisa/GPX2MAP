import datetime
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from pypdf import PdfReader
from streamlit.testing.v1 import AppTest

import gpx2map
from magnetic_north import MagneticDeclination, MagneticNorthError
from map_plan import GPX2MapError, plan_pdf, tile_to_latlon
from tests.test_ui import upload

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures'
INFO = MagneticDeclination(-8, 35, 139, datetime.date(2026, 10, 19))


def blank_tile(*args, **kwargs):
    return Image.new('RGBA', (256, 256), 'white')


class MagneticPDFTests(unittest.TestCase):
    def test_on_off_use_one_global_grid_and_keep_information_on_every_page(self):
        data = (FIXTURES / 'coverage_gap.gpx').read_bytes()
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                plan = plan_pdf(data, draw_magnetic_north=enabled, magnetic_spacing_m=500)
                with patch('gpx2map.download_tile', side_effect=blank_tile), \
                     patch('gpx2map.calculate_declination', return_value=INFO) as model, \
                     patch('gpx2map.draw_magnetic_lines', wraps=gpx2map.draw_magnetic_lines) as lines, \
                     patch('gpx2map.draw_track_and_credits', wraps=gpx2map.draw_track_and_credits) as legends:
                    result = gpx2map.generate_pdf(data, draw_magnetic_north=enabled, magnetic_spacing_m=500, plan=plan)
                self.assertEqual(len(PdfReader(io.BytesIO(result)).pages), plan.page_count)
                cx = (min(x for x, y in plan.grids) + max(x for x, y in plan.grids) + plan.page_w_tiles) / 2
                cy = (min(y for x, y in plan.grids) + max(y for x, y in plan.grids) + plan.page_h_tiles) / 2
                model.assert_called_once_with(*tile_to_latlon(cx, cy))
                self.assertEqual(lines.call_count, plan.page_count if enabled else 0)
                if enabled:
                    grid = lines.call_args_list[0].args[1]
                    self.assertTrue(all(call.args[1] is grid for call in lines.call_args_list))
                    self.assertEqual(grid.reference_latitude, plan.reference_latitude)
                    self.assertEqual(grid.spacing_m, 500)
                self.assertEqual(legends.call_count, plan.page_count)
                self.assertTrue(all(call.args[-1] is INFO for call in legends.call_args_list))

    def test_failed_model_does_not_download_or_report_completion(self):
        data = (FIXTURES / 'sample.gpx').read_bytes()
        for enabled in (True, False):
            events = []
            with patch('gpx2map.calculate_declination', side_effect=MagneticNorthError('coefficients unavailable')), \
                 patch('gpx2map.download_tile') as tiles, self.assertRaisesRegex(MagneticNorthError, 'coefficients'):
                gpx2map.generate_pdf(data, draw_magnetic_north=enabled, progress_callback=events.append)
            tiles.assert_not_called()
            self.assertNotIn('complete', [event.stage for event in events])

    def test_magnetic_setting_changes_reject_stale_plan(self):
        data = (FIXTURES / 'sample.gpx').read_bytes()
        plan = plan_pdf(data)
        for changes in ({'draw_magnetic_north': False}, {'magnetic_spacing_m': 500}):
            with self.subTest(changes=changes), patch('gpx2map.download_tile') as tiles, self.assertRaises(GPX2MapError):
                gpx2map.generate_pdf(data, plan=plan, **changes)
            tiles.assert_not_called()

    def test_cli_model_failure_preserves_existing_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'previous.pdf'
            output.write_bytes(b'previous output')
            with patch('gpx2map.calculate_declination', side_effect=MagneticNorthError('WMM failed')), \
                 patch('sys.stderr', new_callable=io.StringIO) as error:
                status = gpx2map.main(['-i', str(FIXTURES / 'sample.gpx'), '-o', str(output)])
            self.assertEqual(status, 1)
            self.assertIn('PDF生成に失敗', error.getvalue())
            self.assertIn('WMM failed', error.getvalue())
            self.assertEqual(output.read_bytes(), b'previous output')

    def test_line_alpha_preserves_basemap_and_route_is_on_top(self):
        from magnetic_north import MagneticGrid
        x, y = gpx2map.latlon_to_tile(35, 139)
        with Image.new('RGBA', (1200, 900), 'white') as image:
            gpx2map.draw_magnetic_lines(image, MagneticGrid(0, x, y, 35), x - 2, y - 2, 4, 4)
            red, green, blue, alpha = image.getpixel((600, 450))
            self.assertTrue(180 < red < green < blue < 255)
            self.assertEqual(alpha, 255)
            gpx2map.draw_track_and_credits(image, [[(35, 138.99), (35, 139.01)]], x - 2, y - 2, 4, 4)
            self.assertEqual(image.getpixel((600, 450))[:3], (230, 10, 10))


class MagneticUITests(unittest.TestCase):
    def test_default_spacing_and_magnetic_changes_clear_results(self):
        with patch('streamlit.file_uploader', return_value=upload((FIXTURES / 'sample.gpx').read_bytes())), \
             patch('gpx2map.download_tile', side_effect=blank_tile):
            app = AppTest.from_file(str(ROOT / 'app.py')).run()
            self.assertTrue(app.checkbox(key='draw_magnetic_north_check').value)
            self.assertEqual(app.selectbox(key='magnetic_spacing_select').value, 1000)
            self.assertEqual(app.selectbox(key='magnetic_spacing_select').options, ['500m', '1km', '2km'])
            app.button(key='generate_button').click().run(timeout=20)
            self.assertIsNotNone(app.session_state.pdf_bytes)
            app.selectbox(key='magnetic_spacing_select').set_value(500).run()
            self.assertIsNone(app.session_state.pdf_bytes)
            self.assertEqual(app.session_state.generation_plan.settings.magnetic_spacing_m, 500)
            app.checkbox(key='draw_magnetic_north_check').uncheck().run()
            self.assertFalse(any(widget.key == 'magnetic_spacing_select' for widget in app.selectbox))
            self.assertFalse(app.session_state.generation_plan.settings.draw_magnetic_north)
            self.assertFalse(app.exception)

    def test_model_failure_shows_cause_without_success_or_download(self):
        with patch('streamlit.file_uploader', return_value=upload((FIXTURES / 'sample.gpx').read_bytes())), \
             patch('gpx2map.calculate_declination', side_effect=MagneticNorthError('WMM coefficients unavailable')):
            app = AppTest.from_file(str(ROOT / 'app.py')).run()
            app.button(key='generate_button').click().run(timeout=10)
        self.assertFalse(app.exception)
        self.assertTrue(app.error)
        self.assertIn('PDFの生成に失敗', app.error[0].value)
        self.assertIn('WMM coefficients unavailable', app.session_state.error_details)
        self.assertFalse(app.success)
        self.assertIsNone(app.session_state.pdf_bytes)
        self.assertFalse(app.get('download_button'))


if __name__ == '__main__':
    unittest.main()
