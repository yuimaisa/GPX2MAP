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
from zipfile import ZipFile

from playwright.sync_api import sync_playwright
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Use the actual app and real GSI requests')
    parser.add_argument('--split', action='store_true', help='Exercise ZIP downloads with a smaller PDF limit')
    parser.add_argument('--base-path', default='', help='Exercise serving under a URL prefix')
    args = parser.parse_args()
    if args.live and args.split:
        parser.error('--split uses deterministic test tiles; do not combine with --live')
    output = ROOT / 'test-results'
    output.mkdir(exist_ok=True)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    url = f'http://127.0.0.1:{port}' + (f'/{args.base_path.strip("/")}' if args.base_path else '')
    app = ROOT / ('app.py' if args.live else 'tests/browser_app.py')
    suffix = '-live' if args.live else '-split' if args.split else ''
    log_path = output / f'browser{suffix}.log'
    with log_path.open('w') as log:
        environment = os.environ.copy()
        if args.split:
            environment['GPX2MAP_TEST_PDF_BYTES'] = '600000'
        process = subprocess.Popen([sys.executable, '-m', 'streamlit', 'run', str(app),
                                    '--server.headless=true', '--server.address=127.0.0.1',
                                    f'--server.port={port}', f'--server.baseUrlPath={args.base_path}',
                                    '--browser.gatherUsageStats=false'],
                                   cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT)
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
                    extension = 'zip' if args.split else 'pdf'
                    download = page.frame_locator('iframe[title="download_component.files"]').get_by_role(
                        'button', name='ZIPをダウンロード' if args.split else 'PDFをダウンロード', exact=True)
                    download.wait_for(timeout=180000 if args.live else 30000)
                    if args.split and name == 'desktop':
                        failed = []
                        def fail_once(route):
                            if not failed:
                                failed.append(True)
                                route.fulfill(status=503, body='temporary test failure')
                            else:
                                route.continue_()
                        context.route('**/component/download_component.files/*/*.bin', fail_once)
                        download.click()
                        page.frame_locator('iframe[title="download_component.files"]').get_by_text(
                            '保存できませんでした。もう一度お試しください。', exact=True).wait_for()
                        assert not download.is_disabled()
                    with page.expect_download() as pending:
                        download.click()
                    downloaded = pending.value
                    file_path = output / f'{name}{suffix}.{extension}'
                    downloaded.save_as(file_path)
                    if args.split:
                        with ZipFile(file_path) as archive:
                            assert len(archive.namelist()) == 2
                            labels = []
                            for entry in archive.infolist():
                                assert entry.file_size <= 600000
                                labels.extend(PdfReader(io.BytesIO(archive.read(entry)), strict=True).page_labels)
                            assert labels == ['1', '2']
                        pages = len(labels)
                    else:
                        pages = len(PdfReader(file_path, strict=True).pages)
                    assert pages == expected_pages
                    assert downloaded.suggested_filename == f'{Path(fixture).stem}_map.{extension}'
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
                    page.screenshot(path=str(output / f'{name}{suffix}.png'), full_page=True)
                    page.get_by_role('heading', name='GPX2MAP', exact=True).scroll_into_view_if_needed()
                    page.screenshot(path=str(output / f'{name}{suffix}-top.png'), full_page=True)
                    assert not errors, errors
                    results.append({'viewport': name, 'width': width, 'pdf_pages': pages, 'format': extension,
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
