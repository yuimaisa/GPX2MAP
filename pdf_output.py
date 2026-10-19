"""Disk-backed PDF packing for the renderer's full-resolution JPEG pages.

Only the PDF envelope is written here: JPEG streams are copied verbatim, without
decoding or recompression. A trial page and the complete xref/trailer are written
before checking the actual file size. A rejected page is rolled back on disk.
"""
from dataclasses import dataclass
from pathlib import Path
import shutil
import tempfile
from zipfile import ZIP_STORED, ZipFile

from map_plan import ResourceLimitError, TARGET_HEIGHT, TARGET_WIDTH


@dataclass(frozen=True)
class PDFPart:
    filename: str
    first_page: int
    last_page: int
    size_bytes: int


class PDFOutput:
    """An owned temporary download; close it when the UI/CLI releases it."""

    def __init__(self):
        self._temporary = tempfile.TemporaryDirectory(prefix='gpx2map-output-')
        self.directory = Path(self._temporary.name)
        self.path = self.directory / 'map.pdf'
        self.parts = []

    @property
    def is_zip(self):
        return self.path.suffix == '.zip'

    def close(self):
        self._temporary.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class _JPEGDocument:
    """A small PDF 1.4 writer for our single-JPEG A4 landscape pages."""

    def __init__(self, path, first_page):
        self.path = Path(path)
        self.file = self.path.open('w+b')
        self.first_page = first_page
        self.offsets = {}
        self.pages = []
        self.file.write(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
        self._object(1, f'<< /Type /Catalog /Pages 2 0 R /PageLabels << /Nums [0 << /S /D /St {first_page} >>] >> >>'.encode())
        self._object(3, b'<< /Title (GPX2MAP) /Author (GPX2MAP) >>')

    def _object(self, number, body):
        self.offsets[number] = self.file.tell()
        self.file.write(f'{number} 0 obj\n'.encode() + body + b'\nendobj\n')

    def _footer(self):
        # Object 2 is intentionally last: the page tree needs only page refs.
        offsets = {**self.offsets, 2: self.file.tell()}
        kids = ' '.join(f'{number} 0 R' for number in self.pages)
        tree = f'2 0 obj\n<< /Type /Pages /Count {len(self.pages)} /Kids [{kids}] >>\nendobj\n'.encode()
        startxref = self.file.tell() + len(tree)
        count = max(offsets) + 1
        xref = f'xref\n0 {count}\n0000000000 65535 f \n'.encode()
        xref += b''.join(f'{offsets[number]:010d} 00000 n \n'.encode() for number in range(1, count))
        return tree + xref + f'trailer\n<< /Size {count} /Root 1 0 R /Info 3 0 R >>\nstartxref\n{startxref}\n%%EOF\n'.encode()

    def append(self, jpeg_path, limit):
        checkpoint = self.file.tell()
        image_number = 4 + 3 * len(self.pages)
        content_number, page_number = image_number + 1, image_number + 2
        self.offsets[image_number] = checkpoint
        self.file.write((f'{image_number} 0 obj\n<< /Type /XObject /Subtype /Image /Width {TARGET_WIDTH} '
                         f'/Height {TARGET_HEIGHT} /ColorSpace /DeviceRGB /BitsPerComponent 8 '
                         f'/Filter /DCTDecode /Length {jpeg_path.stat().st_size} >>\nstream\n').encode())
        with jpeg_path.open('rb') as source:
            shutil.copyfileobj(source, self.file, length=64 * 1024)
        self.file.write(b'\nendstream\nendobj\n')
        # Same physical page/image dimensions as the former ReportLab canvas.
        width, height = 297 * 72 / 25.4, 210 * 72 / 25.4
        content = f'q\n{width:.8f} 0 0 {height:.8f} 0 0 cm\n/Map Do\nQ\n'.encode()
        self._object(content_number, f'<< /Length {len(content)} >>\nstream\n'.encode() + content + b'endstream')
        self._object(page_number, (f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width:.8f} {height:.8f}] '
                                   f'/Resources << /XObject << /Map {image_number} 0 R >> >> '
                                   f'/Contents {content_number} 0 R >>').encode())
        self.pages.append(page_number)
        end = self.file.tell()
        self.file.write(self._footer())
        self.file.flush()
        actual_size = self.path.stat().st_size
        accepted = actual_size <= limit
        if not accepted:
            self.pages.pop()
            for number in (image_number, content_number, page_number):
                del self.offsets[number]
        self.file.seek(end if accepted else checkpoint)
        self.file.truncate()
        return accepted, actual_size

    def finish(self):
        self.file.write(self._footer())
        self.file.close()

    def close(self):
        self.file.close()


class PDFPacker:
    """Greedily pack complete print pages by finalized PDF byte size."""

    def __init__(self, output, limit):
        self.output, self.limit = output, limit
        self.document = None
        self.archive = None
        self.page = 0

    def _start(self):
        self.document = _JPEGDocument(self.output.directory / 'current.pdf', self.page + 1)

    def _archive_part(self):
        document = self.document
        document.finish()
        size = document.path.stat().st_size
        if size > self.limit:
            raise ResourceLimitError('PDFの容量を上限以内に収められませんでした。')
        filename = f'map_part{len(self.output.parts) + 1:03d}_pages{document.first_page:03d}-{self.page:03d}.pdf'
        self.archive.write(document.path, filename)
        self.output.parts.append(PDFPart(filename, document.first_page, self.page, size))
        document.path.unlink()
        self.document = None

    def add(self, jpeg_path):
        if self.document is None:
            self._start()
        accepted, size = self.document.append(jpeg_path, self.limit)
        if not accepted and self.document.pages:
            if self.archive is None:
                self.output.path = self.output.directory / 'maps.zip'
                self.archive = ZipFile(self.output.path, 'w', compression=ZIP_STORED, allowZip64=True)
            self._archive_part()
            self._start()
            accepted, size = self.document.append(jpeg_path, self.limit)
        if not accepted:
            raise ResourceLimitError(
                f'ページ{self.page + 1}は1ページだけでPDF容量の上限を超えます'
                f'（{size:,}バイト / 上限{self.limit:,}バイト）。'
                '画質と縮尺を保つため生成を中止しました。PDF容量の上限設定を増やすか、地図の種類を変更してください。')
        self.page += 1

    def finish(self):
        if self.archive is not None:
            self._archive_part()
            self.archive.close()
            self.archive = None
        else:
            self.document.finish()
            size = self.document.path.stat().st_size
            if size > self.limit:
                raise ResourceLimitError('PDFの容量を上限以内に収められませんでした。')
            self.document.path.replace(self.output.path)
            self.output.parts.append(PDFPart('map.pdf', 1, self.page, size))
            self.document = None

    def close(self):
        try:
            if self.document is not None:
                self.document.close()
        finally:
            if self.archive is not None:
                self.archive.close()
