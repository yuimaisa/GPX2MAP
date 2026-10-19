from dataclasses import replace
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from PIL import Image, ImageDraw
from pypdf import PdfReader

import gpx2map
from download_component import BLOCK_BYTES, DownloadArtifact
from map_plan import DEFAULT_LIMITS, ResourceLimitError, TARGET_HEIGHT, TARGET_WIDTH, latlon_to_tile, plan_pdf, verify_coverage
from pdf_output import PDFOutput, PDFPacker

FIXTURES = Path(__file__).parent / 'fixtures'


class PackingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.blank = Path(cls.temporary.name) / 'blank.jpg'
        cls.detailed = Path(cls.temporary.name) / 'detailed.jpg'
        with Image.new('RGB', (TARGET_WIDTH, TARGET_HEIGHT), 'white') as image:
            image.save(cls.blank, quality=95, subsampling=0)
            draw = ImageDraw.Draw(image)
            for x in range(0, image.width, 8):
                draw.line((x, 0, x, image.height), fill=(30, 140, 230), width=2)
            image.save(cls.detailed, quality=95, subsampling=0)

    def pack(self, pages, limit=DEFAULT_LIMITS.pdf_bytes):
        output = PDFOutput()
        self.addCleanup(output.close)
        packer = PDFPacker(output, limit)
        try:
            for page in pages:
                packer.add(page)
            packer.finish()
        finally:
            packer.close()
        return output

    def test_single_pdf_at_exact_limit(self):
        baseline = self.pack([self.blank, self.blank])
        size = baseline.path.stat().st_size
        result = self.pack([self.blank, self.blank], size)
        self.assertFalse(result.is_zip)
        self.assertEqual(result.path.stat().st_size, size)
        self.assertEqual(result.path.read_bytes(), baseline.path.read_bytes())
        self.assertEqual(PdfReader(result.path, strict=True).page_labels, ['1', '2'])

    def test_one_byte_over_limit_splits_at_page_boundary(self):
        size = self.pack([self.blank, self.blank]).path.stat().st_size
        result = self.pack([self.blank, self.blank], size - 1)
        self.assertTrue(result.is_zip)
        self.assertEqual([(p.first_page, p.last_page) for p in result.parts], [(1, 1), (2, 2)])
        with ZipFile(result.path) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.namelist(), [p.filename for p in result.parts])
            for index, entry in enumerate(archive.infolist(), 1):
                self.assertLessEqual(entry.file_size, size - 1)
                reader = PdfReader(io.BytesIO(archive.read(entry)), strict=True)
                self.assertEqual(reader.page_labels, [str(index)])
                self.assertEqual(len(reader.pages), 1)
        self.assertEqual({p.name for p in result.directory.iterdir()}, {'maps.zip'})

    def test_variable_page_sizes_determine_part_sizes(self):
        limit = self.pack([self.detailed]).path.stat().st_size + 1000
        result = self.pack([self.blank] * 4 + [self.detailed] + [self.blank] * 2, limit)
        self.assertEqual([(p.first_page, p.last_page) for p in result.parts], [(1, 4), (5, 5), (6, 7)])
        self.assertTrue(all(p.size_bytes <= limit for p in result.parts))
        with ZipFile(result.path) as archive:
            labels = [label for name in archive.namelist()
                      for label in PdfReader(io.BytesIO(archive.read(name)), strict=True).page_labels]
        self.assertEqual(labels, list(map(str, range(1, 8))))

    def test_single_page_exact_limit_and_one_byte_below(self):
        size = self.pack([self.blank]).path.stat().st_size
        self.assertFalse(self.pack([self.blank], size).is_zip)
        for limit in (size - 1, 0):
            with self.subTest(limit=limit), self.assertRaisesRegex(ResourceLimitError, 'ページ1は1ページだけで'):
                self.pack([self.blank], limit)

    def test_later_oversized_page_identifies_global_page(self):
        limit = self.pack([self.blank, self.blank]).path.stat().st_size
        with self.assertRaisesRegex(ResourceLimitError, 'ページ4は1ページだけで'):
            self.pack([self.blank] * 3 + [self.detailed], limit)

    def test_many_parts_and_last_remainder_preserve_labels_above_nine(self):
        limit = self.pack([self.blank]).path.stat().st_size + 100
        result = self.pack([self.blank] * 12, limit)
        self.assertEqual(len(result.parts), 12)
        with ZipFile(result.path) as archive:
            self.assertEqual(archive.namelist(), sorted(archive.namelist()))
            for index, entry in enumerate(archive.infolist(), 1):
                self.assertLessEqual(entry.file_size, limit)
                self.assertEqual(PdfReader(io.BytesIO(archive.read(entry)), strict=True).page_labels, [str(index)])

    def test_jpeg_stream_is_copied_verbatim(self):
        result = self.pack([self.detailed])
        page = PdfReader(result.path, strict=True).pages[0]
        self.assertEqual(page['/Resources']['/XObject']['/Map'].get_data(), self.detailed.read_bytes())
        self.assertEqual(page.images[0].image.size, (TARGET_WIDTH, TARGET_HEIGHT))

    def test_web_download_uses_bounded_blocks_and_cleans_up(self):
        output = self.pack([self.detailed])
        # A smaller test block forces several requests without a huge fixture.
        with patch('download_component.BLOCK_BYTES', 64 * 1024):
            artifact = DownloadArtifact(output, 'route_map.pdf')
        self.addCleanup(artifact.close)
        self.assertGreater(len(artifact.blocks), 1)
        self.assertTrue(all(p.stat().st_size <= 64 * 1024 for p in artifact.blocks))
        digest = hashlib.sha256()
        for block in artifact.blocks:
            digest.update(block.read_bytes())
        self.assertEqual(digest.digest(), hashlib.sha256(output.path.read_bytes()).digest())
        self.assertLessEqual(BLOCK_BYTES, 8 * 1024**2)
        directory = artifact.directory
        artifact.close()
        self.assertFalse(directory.exists())


class SplitRenderingTests(unittest.TestCase):
    def render(self, data, **kwargs):
        with patch('gpx2map.download_tile', side_effect=lambda *a, **kw: Image.new('RGBA', (256, 256), 'white')):
            output = gpx2map.generate_output(data, **kwargs)
        self.addCleanup(output.close)
        return output

    def test_split_and_unsplit_images_and_sparse_route_are_identical(self):
        data = (FIXTURES / 'coverage_gap.gpx').read_bytes()
        baseline = self.render(data, overlap=0)
        original = PdfReader(baseline.path, strict=True)
        limit = max(len(p['/Resources']['/XObject']['/Map'].get_data()) for p in original.pages) + 2000
        limits = replace(DEFAULT_LIMITS, pdf_bytes=limit)
        plan = plan_pdf(data, overlap=0, limits=limits)
        segments = [[latlon_to_tile(*p) for p in s] for s in plan.document.segments]
        self.assertTrue(verify_coverage(segments, plan.grids, plan.page_w_tiles, plan.page_h_tiles))
        events = []
        original_draw = gpx2map.draw_track_and_credits
        with patch('gpx2map.draw_track_and_credits', wraps=original_draw) as draw, \
             patch.object(Path, 'read_bytes', side_effect=AssertionError('Whole-file read during generation')):
            result = self.render(data, overlap=0, limits=limits, plan=plan, progress_callback=events.append)
        self.assertTrue(result.is_zip)
        self.assertEqual(draw.call_count, plan.page_count)
        self.assertTrue(all(call.args[1] == plan.document.segments for call in draw.call_args_list))
        self.assertEqual([p.page for p in events if p.stage == 'page'], [1, 2])
        self.assertEqual(events[-1].stage, 'complete')
        with ZipFile(result.path) as archive:
            for index, entry in enumerate(archive.infolist()):
                self.assertLessEqual(entry.file_size, limit)
                reader = PdfReader(io.BytesIO(archive.read(entry)), strict=True)
                self.assertEqual(reader.page_labels, [str(index + 1)])
                page = reader.pages[0]
                self.assertEqual(page.mediabox, original.pages[index].mediabox)
                # No resizing, changed scale, recompression, or route clipping at a split.
                self.assertEqual(page['/Resources']['/XObject']['/Map'].get_data(),
                                 original.pages[index]['/Resources']['/XObject']['/Map'].get_data())
                image = page.images[0].image
                margin = round(5 * 300 / 25.4)
                x = image.width - margin - 2 if index == 0 else margin + 2
                y = image.height // 2
                self.assertTrue(any((lambda c: c[0] > 150 and c[0] > c[1] * 1.8)(image.getpixel((x, yy)))
                                    for yy in range(y - 8, y + 9)), 'Sparse route must reach each facing print edge')

    def test_error_after_first_part_leaves_no_download_or_complete_event(self):
        data = (FIXTURES / 'wide.gpx').read_bytes()
        events, directories = [], []
        original_output = PDFOutput
        def create_output():
            output = original_output()
            directories.append(output.directory)
            return output
        def fail(progress):
            events.append(progress)
            if progress.stage == 'page' and progress.page == 2:
                self.assertTrue((directories[0] / 'maps.zip').exists())
                raise OSError('simulated disk error')
        with patch('gpx2map.PDFOutput', side_effect=create_output), self.assertRaisesRegex(OSError, 'disk error'):
            self.render(data, limits=replace(DEFAULT_LIMITS, pdf_bytes=600000), progress_callback=fail)
        self.assertTrue(directories)
        self.assertTrue(all(not path.exists() for path in directories))
        self.assertNotIn('complete', [event.stage for event in events])

    def test_cli_writes_zip_with_correct_extension_and_preserves_existing_pdf(self):
        data = (FIXTURES / 'coverage_gap.gpx').read_bytes()
        output = self.render(data, limits=replace(DEFAULT_LIMITS, pdf_bytes=600000))
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / 'route.pdf'
            target.write_bytes(b'existing pdf')
            with patch('gpx2map.generate_output', return_value=output):
                status = gpx2map.main(['-i', str(FIXTURES / 'coverage_gap.gpx'), '-o', str(target)])
            self.assertEqual(status, 0)
            self.assertEqual(target.read_bytes(), b'existing pdf')
            with ZipFile(target.with_suffix('.zip')) as archive:
                self.assertEqual(len(archive.namelist()), 2)

    def test_zip_disk_failure_removes_partial_output(self):
        data = (FIXTURES / 'coverage_gap.gpx').read_bytes()
        directories, events = [], []
        def create_output():
            result = PDFOutput()
            directories.append(result.directory)
            return result
        with patch('gpx2map.PDFOutput', side_effect=create_output), \
             patch('pdf_output.ZipFile.write', side_effect=OSError('disk full')), \
             self.assertRaisesRegex(OSError, 'disk full'):
            self.render(data, limits=replace(DEFAULT_LIMITS, pdf_bytes=600000), progress_callback=events.append)
        self.assertTrue(directories)
        self.assertTrue(all(not path.exists() for path in directories))
        self.assertNotIn('complete', [event.stage for event in events])
