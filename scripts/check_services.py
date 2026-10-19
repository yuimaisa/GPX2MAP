"""Explicit live integration check; never imported by unittest discovery."""
import argparse
import io
import json
import math
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requests
from gpx2map import generate_pdf
from map_plan import latlon_to_tile
from tile_store import download_tile


class CountingSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.calls = 0
    def get(self, *args, **kwargs):
        self.calls += 1
        return super().get(*args, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pdf', action='store_true')
    parser.add_argument('--check-magnetic', action='store_true')
    args = parser.parse_args()
    results = {}
    with tempfile.TemporaryDirectory() as cache, CountingSession() as session:
        x, y = map(math.floor, latlon_to_tile(35.6586, 139.7454))
        for kind in ('std', 'pale', 'seamlessphoto'):
            before = session.calls
            with download_tile(x, y, 15, kind, session=session, cache_dir=cache) as tile:
                assert tile.size == (256, 256)
            calls = session.calls
            with download_tile(x, y, 15, kind, session=session, cache_dir=cache):
                pass
            assert session.calls == calls
            results[kind] = {'live_requests': calls - before, 'cache_reused': True}
        if args.pdf:
            from pypdf import PdfReader
            data = (ROOT / 'tests/fixtures/sample.gpx').read_bytes()
            result = generate_pdf(data, cache_dir=cache)
            reader = PdfReader(io.BytesIO(result))
            assert len(reader.pages) == 1
            assert abs(float(reader.pages[0].mediabox.width) / 72 * 25.4 - 297) < 0.01
            output = ROOT / 'test-results/network-smoke.pdf'
            output.parent.mkdir(exist_ok=True)
            output.write_bytes(result)
            results['pdf'] = {'pages': 1, 'bytes': len(result), 'actual_tiles': True}
        if args.check_magnetic:
            response = session.get('https://vldb.gsi.go.jp/sokuchi/geomag/math/cgimath/sigmath_m.cgi',
                                   params={'use_of_data': 'xml', 'year': 2026, 'month': 1, 'day': 1,
                                           'lat': 35.6812, 'lon': 139.7671, 'alt': 0}, timeout=(5, 15))
            results['magnetic'] = {'legacy_endpoint_http_status': response.status_code, 'enabled': False}
    print(json.dumps(results, ensure_ascii=False))


if __name__ == '__main__':
    main()
