"""Measure core-process peak RSS for 50,000 points and 24 synthetic pages."""
import json
from pathlib import Path
import resource
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw
from pypdf import PdfReader
import io
from gpx2map import generate_pdf
from map_plan import DEFAULT_LIMITS, plan_pdf


def tile(*args, **kwargs):
    image = Image.new('RGBA', (256, 256), (242, 246, 240, 255))
    draw = ImageDraw.Draw(image)
    for y in range(0, 256, 16):
        draw.line([(0, y), (256, y + 30)], fill=(180, 200, 170, 255), width=2)
    return image


def main():
    points = ''.join(f'<trkpt lat="35" lon="{139 + i / 49999 * 0.8:.8f}"/>' for i in range(50000))
    data = f'<gpx><trk><trkseg>{points}</trkseg></trk></gpx>'.encode()
    del points
    plan = plan_pdf(data, scale_factor=2.0)
    with patch('gpx2map.download_tile', side_effect=tile):
        result = generate_pdf(data, scale_factor=2.0, plan=plan)
    pages = len(PdfReader(io.BytesIO(result)).pages)
    peak_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    assert pages == plan.page_count == 24
    assert peak_mib < DEFAULT_LIMITS.memory_bytes / 1024**2
    print(json.dumps({'points': plan.document.point_count, 'pages': pages, 'unique_tiles': plan.tile_count,
                      'pdf_bytes': len(result), 'measured_peak_rss_mib': round(peak_mib, 1),
                      'estimated_render_mib': round(plan.estimated_memory_bytes / 1024**2, 1),
                      'synthetic_tiles': True}, ensure_ascii=False))


if __name__ == '__main__':
    main()
