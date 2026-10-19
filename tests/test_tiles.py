from concurrent.futures import ThreadPoolExecutor
import io
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image
import requests
from tile_store import download_tile, prune_cache, TileDownloadError


def image_bytes(mode='RGB', format='PNG', size=(256, 256)):
    with Image.new(mode, size, 'white') as image:
        buffer = io.BytesIO()
        image.save(buffer, format=format)
        return buffer.getvalue()


class Response:
    def __init__(self, status=200, content=None, headers=None):
        self.status_code = status
        self.content = image_bytes() if content is None else content
        self.headers = headers or {}
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def iter_content(self, size):
        yield self.content


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0
    def get(self, *args, **kwargs):
        self.calls += 1
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class TileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        throttle = patch('tile_store._throttle')
        throttle.start()
        self.addCleanup(throttle.stop)
        sleep = patch('tile_store.time.sleep')
        sleep.start()
        self.addCleanup(sleep.stop)

    def fetch(self, session, kind='std'):
        return download_tile(29000, 13000, 15, kind, session=session, cache_dir=self.root)

    def test_cache_reuse_and_atomic_write(self):
        session = Session([Response()])
        with self.fetch(session) as first, self.fetch(session) as second:
            self.assertEqual(first.tobytes(), second.tobytes())
        self.assertEqual(session.calls, 1)
        self.assertEqual(len(list(self.root.rglob('*.tmp'))), 0)

    def test_aerial_rgba_response_is_cached_as_rgb_jpeg(self):
        session = Session([Response(content=image_bytes('RGBA'))])
        with self.fetch(session, 'seamlessphoto') as tile:
            self.assertEqual(tile.mode, 'RGBA')
        with Image.open(next(self.root.rglob('*.jpg'))) as stored:
            self.assertEqual(stored.mode, 'RGB')

    def test_missing_and_forbidden_tiles_fail_without_placeholder(self):
        for status in (404, 403):
            session = Session([Response(status)])
            with self.subTest(status=status), self.assertRaises(TileDownloadError) as raised:
                self.fetch(session, 'seamlessphoto')
            self.assertEqual(session.calls, 1)
            self.assertEqual(raised.exception.failed_tiles, (('seamlessphoto', 15, 29000, 13000),))
        self.assertFalse(list(self.root.rglob('*.jpg')))

    def test_rate_limit_and_transient_error_retry(self):
        for failure in (Response(429, headers={'Retry-After': '0'}), Response(503), requests.Timeout('timeout')):
            session = Session([failure, Response()])
            with self.subTest(failure=failure), self.fetch(session) as tile:
                self.assertEqual(tile.size, (256, 256))
            self.assertEqual(session.calls, 2)
            for path in self.root.rglob('*.png'):
                path.unlink()

    def test_exhausted_retries_are_failure(self):
        session = Session([requests.Timeout('timeout')] * 3)
        with self.assertRaises(TileDownloadError):
            self.fetch(session)
        self.assertEqual(session.calls, 3)

    def test_invalid_image_is_not_cached(self):
        for content in (b'<html>not an image</html>', image_bytes(size=(1, 1)), b'x' * (2 * 1024**2 + 1)):
            session = Session([Response(content=content)] * 3)
            with self.subTest(size=len(content)), self.assertRaises(TileDownloadError):
                self.fetch(session)
            self.assertEqual(session.calls, 3)
        self.assertFalse(list(self.root.rglob('*.png')))

    def test_corrupt_cache_is_replaced(self):
        path = self.root / 'std/15/29000/13000.png'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'corrupt')
        session = Session([Response()])
        with self.fetch(session) as image:
            self.assertEqual(image.size, (256, 256))
        self.assertEqual(session.calls, 1)

    def test_expired_cache_is_refetched(self):
        session = Session([Response(), Response()])
        self.fetch(session).close()
        path = next(self.root.rglob('*.png'))
        old = time.time() - 31 * 24 * 3600
        os.utime(path, (old, old))
        self.fetch(session).close()
        self.assertEqual(session.calls, 2)

    def test_cache_size_and_expiry_pruning(self):
        paths = [self.root / f'std/15/{i}/13000.png' for i in range(3)]
        for i, path in enumerate(paths):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'x' * 10)
            os.utime(path, (time.time() - (3 - i) * 10,) * 2)
        prune_cache(self.root, max_bytes=20)
        self.assertFalse(paths[0].exists())
        self.assertEqual(sum(p.stat().st_size for p in self.root.rglob('*.png')), 20)
        prune_cache(self.root, ttl=0)
        self.assertFalse(list(self.root.rglob('*.png')))

    def test_pruning_does_not_delete_unrelated_user_images(self):
        photo = self.root / 'holiday.png'
        photo.write_bytes(image_bytes())
        prune_cache(self.root, max_bytes=0, ttl=0)
        self.assertTrue(photo.exists())

    def test_simultaneous_requests_share_one_complete_cached_tile(self):
        session = Session([Response()])
        with ThreadPoolExecutor(2) as workers:
            futures = [workers.submit(self.fetch, session) for _ in range(2)]
            images = [future.result() for future in futures]
        try:
            self.assertEqual(images[0].tobytes(), images[1].tobytes())
            self.assertEqual(session.calls, 1)
        finally:
            for image in images:
                image.close()

    def test_invalid_tile_coordinates(self):
        session = Session([])
        for x, y, z in ((-1, 1, 15), (1, 32768, 15), (1.5, 1, 15), (True, 1, 15), (1, 1, -1)):
            with self.subTest(x=x, y=y, z=z), self.assertRaises(TileDownloadError):
                download_tile(x, y, z, 'std', session=session, cache_dir=self.root)
        self.assertEqual(session.calls, 0)
