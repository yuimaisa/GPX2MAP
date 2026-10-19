"""Strict, bounded GSI tile access with atomic cache writes."""
from contextlib import nullcontext
import io
import os
from pathlib import Path
import tempfile
import threading
import time

from PIL import Image, UnidentifiedImageError
import requests

from map_plan import GPX2MapError

MAP_URLS = {
    'std': 'https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png',
    'pale': 'https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png',
    'seamlessphoto': 'https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/{z}/{x}/{y}.jpg',
}
CACHE_MAX_BYTES = 512 * 1024**2
CACHE_TTL_SECONDS = 30 * 24 * 3600
MAX_TILE_BYTES = 2 * 1024**2
_KEY_LOCKS = [threading.Lock() for _ in range(64)]
_CACHE_LOCK = threading.Lock()
_REQUEST_LOCK = threading.Lock()
_last_request_at = 0.0


class TileDownloadError(GPX2MapError):
    def __init__(self, map_type, z, x, y, reason):
        self.failed_tiles = ((map_type, z, x, y),)
        super().__init__(f'地図タイル1件を取得できませんでした。PDFは作成していません。{map_type}/{z}/{x}/{y}: {reason}')


def default_cache_dir():
    return Path(os.environ.get('GPX2MAP_CACHE_DIR', str(Path(tempfile.gettempdir()) / 'gpx2map-tile-cache')))


def prune_cache(cache_dir, max_bytes=CACHE_MAX_BYTES, ttl=CACHE_TTL_SECONDS):
    with _CACHE_LOCK:
        entries, total = [], 0
        now = time.time()
        for path in Path(cache_dir).rglob('*'):
            parts = path.relative_to(cache_dir).parts
            # A custom cache directory may also contain unrelated user images.
            if (len(parts) != 4 or parts[0] not in MAP_URLS
                    or not parts[1].isdigit() or not parts[2].isdigit()
                    or not path.stem.isdigit()
                    or path.suffix != ('.jpg' if parts[0] == 'seamlessphoto' else '.png')):
                continue
            try:
                stat = path.stat()
                if now - stat.st_mtime > ttl:
                    path.unlink(missing_ok=True)
                    continue
                entries.append((stat.st_mtime, path, stat.st_size))
                total += stat.st_size
            except FileNotFoundError:
                continue
        for _, path, size in sorted(entries):
            if total <= max_bytes:
                break
            path.unlink(missing_ok=True)
            total -= size


def _load_image(content):
    with Image.open(io.BytesIO(content)) as source:
        if source.size != (256, 256):
            raise ValueError('256×256ではない画像です')
        source.load()
        return source.convert('RGBA')


def _throttle():
    global _last_request_at
    with _REQUEST_LOCK:
        delay = 0.15 - (time.monotonic() - _last_request_at)
        if delay > 0:
            time.sleep(delay)
        _last_request_at = time.monotonic()


def download_tile(x, y, z, map_type, session=None, cache_dir=None):
    if (map_type not in MAP_URLS or any(isinstance(v, bool) or not isinstance(v, int) for v in (x, y, z))
            or not 0 <= z <= 22 or not (0 <= x < 2**z and 0 <= y < 2**z)):
        raise TileDownloadError(map_type, z, x, y, '地図の種類またはタイル座標が不正です')
    root = Path(cache_dir) if cache_dir is not None else default_cache_dir()
    ext = 'jpg' if map_type == 'seamlessphoto' else 'png'
    path = root / map_type / str(z) / str(x) / f'{y}.{ext}'
    with _KEY_LOCKS[hash(str(path)) % len(_KEY_LOCKS)]:
        try:
            if path.exists() and time.time() - path.stat().st_mtime <= CACHE_TTL_SECONDS:
                if path.stat().st_size <= MAX_TILE_BYTES:
                    return _load_image(path.read_bytes())
            path.unlink(missing_ok=True)
        except (OSError, ValueError, UnidentifiedImageError):
            path.unlink(missing_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        reason = '不明な取得エラー'
        with (nullcontext(session) if session is not None else requests.Session()) as client:
            for attempt in range(3):
                _throttle()
                retry_after = 0.3 * 2**attempt
                try:
                    response = client.get(
                        MAP_URLS[map_type].format(z=z, x=x, y=y),
                        headers={'User-Agent': 'GPX2MAP/2.0 (https://github.com/yuimaisa/GPX2MAP)'},
                        timeout=(5, 15), stream=True,
                    )
                    with response:
                        status = response.status_code
                        if status != 200:
                            reason = f'HTTP {status}'
                            if status not in (429, 500, 502, 503, 504):
                                raise TileDownloadError(map_type, z, x, y, reason)
                            try:
                                retry_after = min(30.0, max(retry_after, float(response.headers.get('Retry-After', 0))))
                            except ValueError:
                                pass
                        else:
                            content = bytearray()
                            for chunk in response.iter_content(65536):
                                content.extend(chunk)
                                if len(content) > MAX_TILE_BYTES:
                                    raise ValueError('画像の容量が上限を超えています')
                            tile = _load_image(content)
                            temporary = None
                            try:
                                with tempfile.NamedTemporaryFile(dir=path.parent, suffix='.tmp', delete=False) as handle:
                                    temporary = Path(handle.name)
                                    # Never save RGBA directly as JPEG.
                                    stored = tile.convert('RGB') if ext == 'jpg' else tile
                                    try:
                                        stored.save(handle, format='JPEG' if ext == 'jpg' else 'PNG')
                                    finally:
                                        if stored is not tile:
                                            stored.close()
                                os.replace(temporary, path)
                                temporary = None
                                prune_cache(root)
                            except Exception:
                                tile.close()
                                raise
                            finally:
                                if temporary is not None:
                                    temporary.unlink(missing_ok=True)
                            return tile
                except TileDownloadError:
                    raise
                except (requests.RequestException, ValueError, OSError, Image.DecompressionBombError) as error:
                    reason = str(error)
                if attempt < 2:
                    time.sleep(retry_after)
        raise TileDownloadError(map_type, z, x, y, reason)
