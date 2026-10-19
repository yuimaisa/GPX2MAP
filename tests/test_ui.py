import io
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
from pypdf import PdfReader
from streamlit.testing.v1 import AppTest
from zipfile import ZipFile

from map_plan import DEFAULT_LIMITS
from tile_store import TileDownloadError

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures'


def upload(data, name='test-route.gpx'):
    return SimpleNamespace(name=name, size=len(data), getvalue=lambda: data)


def download_bytes(app):
    return b''.join(block.read_bytes() for block in app.session_state['download_artifact'].blocks)


class UITests(unittest.TestCase):
    def test_startup_has_no_external_request_and_generate_is_disabled(self):
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Unexpected network')):
            app = AppTest.from_file(str(ROOT / 'app.py')).run()
        self.assertFalse(app.exception)
        self.assertTrue(app.button(key='generate_button').disabled)
        self.assertEqual(len(app.subheader), 4)

    def test_invalid_gpx_blocks_generation(self):
        with patch('streamlit.file_uploader', return_value=upload(b'not GPX')):
            app = AppTest.from_file(str(ROOT / 'app.py')).run()
        self.assertFalse(app.exception)
        self.assertTrue(app.error)
        self.assertTrue(app.button(key='generate_button').disabled)

    def test_estimate_matches_real_pdf_and_settings_clear_results(self):
        with patch('streamlit.file_uploader', return_value=upload((FIXTURES / 'coverage_gap.gpx').read_bytes())), \
             patch('gpx2map.download_tile', side_effect=lambda *a, **kw: Image.new('RGBA', (256, 256), 'white')):
            app = AppTest.from_file(str(ROOT / 'app.py')).run()
            self.assertFalse(app.exception)
            planned = app.session_state['generation_plan'].page_count
            app.button(key='generate_button').click().run(timeout=20)
            self.assertFalse(app.exception)
            self.assertTrue(app.success)
            self.assertEqual(len(PdfReader(io.BytesIO(download_bytes(app))).pages), planned)
            self.assertEqual(app.session_state['pdf_filename'], 'test-route_map.pdf')
            directory = app.session_state['download_artifact'].directory
            app.selectbox(key='scale_select').select('1:50,000（広い範囲）').run()
            self.assertFalse(app.exception)
            self.assertIsNone(app.session_state['download_artifact'])
            self.assertFalse(directory.exists())
            self.assertEqual(app.session_state['generation_plan'].settings.scale_factor, 0.5)
            app.selectbox(key='scale_select').select('その他の縮尺').run()
            app.number_input(key='custom_scale_denominator').set_value(75000).run()
            self.assertFalse(app.exception)
            self.assertAlmostEqual(app.session_state['generation_plan'].settings.scale_factor, 1 / 3)

    def test_tile_failure_has_no_download_and_can_retry(self):
        with patch('streamlit.file_uploader', return_value=upload((FIXTURES / 'sample.gpx').read_bytes())):
            app = AppTest.from_file(str(ROOT / 'app.py')).run()
            with patch('gpx2map.download_tile', side_effect=TileDownloadError('std', 15, 1, 2, 'HTTP 404')):
                app.button(key='generate_button').click().run(timeout=10)
            self.assertFalse(app.exception)
            self.assertTrue(app.error)
            self.assertFalse(app.success)
            self.assertIn('HTTP 404', app.session_state['error_details'])
            self.assertIsNone(app.session_state['download_artifact'])
            self.assertFalse(app.button(key='generate_button').disabled)
            self.assertEqual(len(app.get('download_button')), 0)
            with patch('gpx2map.download_tile', side_effect=lambda *a, **kw: Image.new('RGBA', (256, 256), 'white')):
                app.button(key='generate_button').click().run(timeout=20)
            self.assertFalse(app.exception)
            self.assertTrue(app.success)
            self.assertFalse(app.error)

    def test_resource_limit_is_visible_before_generation(self):
        with patch('streamlit.file_uploader', return_value=upload((FIXTURES / 'sample.gpx').read_bytes())):
            app = AppTest.from_file(str(ROOT / 'app.py')).run()
            app.selectbox(key='scale_select').select('その他の縮尺').run()
            app.number_input(key='custom_scale_denominator').set_value(250000).run()
            self.assertFalse(app.exception)
            self.assertTrue(app.error)
            self.assertTrue(app.button(key='generate_button').disabled)
            self.assertIsNone(app.session_state['generation_plan'])

    def test_split_download_is_zip_with_global_page_numbers(self):
        limits = replace(DEFAULT_LIMITS, pdf_bytes=600000)
        with patch('map_plan.DEFAULT_LIMITS', limits), \
             patch('streamlit.file_uploader', return_value=upload((FIXTURES / 'coverage_gap.gpx').read_bytes())), \
             patch('gpx2map.download_tile', side_effect=lambda *a, **kw: Image.new('RGBA', (256, 256), 'white')):
            app = AppTest.from_file(str(ROOT / 'app.py')).run()
            app.button(key='generate_button').click().run(timeout=20)
            self.assertFalse(app.exception)
            self.assertTrue(app.success)
            artifact = app.session_state['download_artifact']
            self.assertTrue(artifact.is_zip)
            self.assertEqual(artifact.filename, 'test-route_map.zip')
            with ZipFile(io.BytesIO(download_bytes(app))) as archive:
                self.assertEqual(len(archive.namelist()), 2)
                labels = []
                for entry in archive.infolist():
                    self.assertLessEqual(entry.file_size, limits.pdf_bytes)
                    labels.extend(PdfReader(io.BytesIO(archive.read(entry)), strict=True).page_labels)
                self.assertEqual(labels, ['1', '2'])

    def test_single_page_over_limit_has_no_download_and_can_retry(self):
        with patch('map_plan.DEFAULT_LIMITS', replace(DEFAULT_LIMITS, pdf_bytes=100)), \
             patch('streamlit.file_uploader', return_value=upload((FIXTURES / 'sample.gpx').read_bytes())), \
             patch('gpx2map.download_tile', side_effect=lambda *a, **kw: Image.new('RGBA', (256, 256), 'white')):
            app = AppTest.from_file(str(ROOT / 'app.py')).run()
            app.button(key='generate_button').click().run(timeout=20)
            self.assertFalse(app.exception)
            self.assertTrue(app.error)
            self.assertIn('ページ1は1ページだけで', app.session_state['error_message'])
            self.assertIn('上限100バイト', app.error[0].value)
            self.assertIsNone(app.session_state['download_artifact'])
            self.assertFalse(app.button(key='generate_button').disabled)
