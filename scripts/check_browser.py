"""Run real Chromium at desktop/mobile sizes; tiles are mocked unless --live."""
import argparse
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Use the actual app and real GSI requests')
    args = parser.parse_args()
    output = ROOT / 'test-results'
    output.mkdir(exist_ok=True)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    app = ROOT / ('app.py' if args.live else 'tests/browser_app.py')
    log_path = output / ('browser-live.log' if args.live else 'browser.log')
    with log_path.open('w') as log:
        process = subprocess.Popen([sys.executable, '-m', 'streamlit', 'run', str(app),
                                    '--server.headless=true', '--server.address=127.0.0.1',
                                    f'--server.port={port}', '--browser.gatherUsageStats=false'],
                                   cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(150):
                try:
                    with opener.open(url + '/_stcore/health', timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    if process.poll() is not None:
                        raise RuntimeError(f'Streamlit stopped; inspect {log_path}')
                    time.sleep(0.2)
            else:
                raise RuntimeError('Streamlit did not start')
            results = []
            with sync_playwright() as playwright:
                executable = os.environ.get('GPX2MAP_CHROMIUM_PATH') or shutil.which('chromium')
                browser = playwright.chromium.launch(executable_path=executable, headless=True,
                                                      args=['--no-sandbox', '--disable-dev-shm-usage'])
                for name, width, height in [('desktop', 1280, 960), ('mobile', 390, 844)]:
                    context = browser.new_context(viewport={'width': width, 'height': height}, accept_downloads=True)
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(url)
                    page.get_by_role('heading', name='GPX2MAP', exact=True).wait_for()
                    button = page.get_by_role('button', name='PDFを作成', exact=True)
                    assert button.is_disabled()
                    uploader = page.locator('input[type=file]')
                    uploader.set_input_files({'name': 'invalid.gpx', 'mimeType': 'application/gpx+xml', 'buffer': b'not GPX'})
                    page.get_by_text('GPXを読み込めません。XMLと緯度・経度を確認し、GPXを書き出し直してください。', exact=True).wait_for()
                    assert button.is_disabled()
                    fixture = 'sample.gpx' if args.live else 'coverage_gap.gpx'
                    expected_pages = 1 if args.live else 2
                    uploader.set_input_files(str(ROOT / 'tests/fixtures' / fixture))
                    page.get_by_text(f'{expected_pages}ページ', exact=True).wait_for()
                    button.click()
                    download = page.get_by_role('button', name='PDFをダウンロード', exact=True)
                    download.wait_for(timeout=180000 if args.live else 30000)
                    with page.expect_download() as pending:
                        download.click()
                    downloaded = pending.value
                    file_path = output / f'{name}{"-live" if args.live else ""}.pdf'
                    downloaded.save_as(file_path)
                    reader = PdfReader(io.BytesIO(file_path.read_bytes()))
                    assert len(reader.pages) == expected_pages
                    assert downloaded.suggested_filename == f'{Path(fixture).stem}_map.pdf'
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
                    page.screenshot(path=str(output / f'{name}{"-live" if args.live else ""}.png'), full_page=True)
                    page.get_by_role('heading', name='GPX2MAP', exact=True).scroll_into_view_if_needed()
                    page.screenshot(path=str(output / f'{name}{"-live" if args.live else ""}-top.png'), full_page=True)
                    assert not errors, errors
                    results.append({'viewport': name, 'width': width, 'pdf_pages': len(reader.pages),
                                    'horizontal_overflow': False, 'page_errors': len(errors), 'live_tiles': args.live})
                    context.close()
                browser.close()
            print(json.dumps(results, ensure_ascii=False))
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    main()
