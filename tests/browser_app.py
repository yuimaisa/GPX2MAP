"""Browser-only harness: actual app and rendering, deterministic local tiles."""
from pathlib import Path
from dataclasses import replace
import os
import sys
from unittest.mock import patch

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from map_plan import DEFAULT_LIMITS


def tile(*args, **kwargs):
    image = Image.new('RGBA', (256, 256), (242, 246, 240, 255))
    draw = ImageDraw.Draw(image)
    draw.line([(0, 180), (256, 60)], fill=(200, 210, 190), width=2)
    return image


with patch('gpx2map.download_tile', side_effect=tile), \
     patch('download_component.BLOCK_BYTES', 64 * 1024), \
     patch('map_plan.DEFAULT_LIMITS', replace(DEFAULT_LIMITS, pdf_bytes=int(os.environ.get('GPX2MAP_TEST_PDF_BYTES', DEFAULT_LIMITS.pdf_bytes)))), \
     patch('requests.sessions.Session.request', side_effect=AssertionError('Unexpected browser-test network')):
    exec(compile((ROOT / 'app.py').read_text(), str(ROOT / 'app.py'), 'exec'),
         {'__file__': str(ROOT / 'app.py'), '__name__': '__main__'})
