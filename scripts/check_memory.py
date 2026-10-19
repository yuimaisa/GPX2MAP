"""Measure peak RSS for 50,000 points/24 pages, including a disk-backed ZIP."""
import argparse
import json
from pathlib import Path
import resource
import shutil
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw
from pypdf import PdfReader
from zipfile import ZipFile
from gpx2map import generate_output
from download_component import DownloadArtifact
from map_plan import DEFAULT_LIMITS, plan_pdf

NOISE = None

def tile(*args, **kwargs):
    if NOISE is not None:
        return NOISE.copy()
    image = Image.new('RGBA', (256, 256), (242, 246, 240, 255))
    draw = ImageDraw.Draw(image)
    for y in range(0, 256, 16):
        draw.line([(0, y), (256, y + 30)], fill=(180, 200, 170, 255), width=2)
    return image


def main():
    global NOISE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--split', action='store_true', help='Use detailed tiles to exceed the total PDF limit')
    args = parser.parse_args()
    if args.split:
        NOISE = Image.effect_noise((256, 256), 100).convert('RGBA')
    points = ''.join(f'<trkpt lat="35" lon="{139 + i / 49999 * 0.8:.8f}"/>' for i in range(50000))
    data = f'<gpx><trk><trkseg>{points}</trkseg></trk></gpx>'.encode()
    del points
    limits = DEFAULT_LIMITS
    plan = plan_pdf(data, scale_factor=2.0, limits=limits)
    with patch('gpx2map.download_tile', side_effect=tile):
        with generate_output(data, scale_factor=2.0, plan=plan, limits=limits) as result:
            if result.is_zip:
                with ZipFile(result.path) as archive:
                    # Inspect one part at a time; do not load all PDFs to count pages.
                    for part in result.parts:
                        assert part.size_bytes <= limits.pdf_bytes
                        with archive.open(part.filename) as source, tempfile.TemporaryFile('w+b') as temporary:
                            shutil.copyfileobj(source, temporary, length=64 * 1024)
                            temporary.seek(0)
                            assert len(PdfReader(temporary, strict=True).pages) == part.last_page - part.first_page + 1
                pages = result.parts[-1].last_page
            else:
                pages = len(PdfReader(result.path, strict=True).pages)
            assert pages == plan.page_count == 24
            assert result.is_zip == args.split
            with_download = DownloadArtifact(result, 'memory-check.zip' if result.is_zip else 'memory-check.pdf')
            with_download.close()
            peak_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
            assert peak_mib < DEFAULT_LIMITS.memory_bytes / 1024**2
            print(json.dumps({'points': plan.document.point_count, 'pages': pages, 'unique_tiles': plan.tile_count,
                              'output_bytes': result.path.stat().st_size, 'pdf_parts': len(result.parts),
                              'measured_peak_rss_mib': round(peak_mib, 1),
                              'estimated_render_mib': round(plan.estimated_memory_bytes / 1024**2, 1),
                              'synthetic_tiles': True}, ensure_ascii=False))


if __name__ == '__main__':
    main()
