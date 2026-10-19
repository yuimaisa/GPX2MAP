"""Validated GPX input and a single page plan shared by UI, rendering and CLI."""
from dataclasses import dataclass
import hashlib
import io
import math

from defusedxml import ElementTree
import gpxpy
from magnetic_north import SPACING_OPTIONS_M, ground_meters_per_tile

ZOOM_LEVEL = 15
TARGET_WIDTH, TARGET_HEIGHT = 3508, 2480
MAX_LATITUDE = 85.0511287798066
MAP_TYPES = ('std', 'pale', 'seamlessphoto')


class GPX2MapError(ValueError):
    """An input, resource or generation error with a user-facing explanation."""


class ResourceLimitError(GPX2MapError):
    pass


@dataclass(frozen=True)
class Limits:
    input_bytes: int = 5 * 1024 * 1024
    points: int = 50000
    candidate_cells: int = 2000
    intersection_checks: int = 2000000
    pages: int = 24
    tiles: int = 800
    memory_bytes: int = 384 * 1024 * 1024
    pdf_bytes: int = 64 * 1024 * 1024


DEFAULT_LIMITS = Limits()


@dataclass(frozen=True)
class GPXDocument:
    segments: tuple
    track_count: int
    route_count: int
    track_segment_count: int
    waypoint_count: int
    point_count: int
    distance_m: float
    size_bytes: int
    digest: str


@dataclass(frozen=True)
class Settings:
    map_type: str = 'std'
    scale_factor: float = 1.0
    overlap: float = 0.5
    page_margin_mm: float = 5.0
    draw_magnetic_north: bool = True
    magnetic_spacing_m: int = 1000


@dataclass(frozen=True)
class GenerationPlan:
    document: GPXDocument
    settings: Settings
    grids: tuple
    page_w_tiles: float
    page_h_tiles: float
    tiles: tuple
    reference_latitude: float
    scale_error: float
    estimated_memory_bytes: int
    limits: Limits

    @property
    def page_count(self):
        return len(self.grids)

    @property
    def tile_count(self):
        return len(self.tiles)


def _number(value, name, lower, upper):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise GPX2MapError(f'{name}には有限の数値を指定してください。')
    if not lower <= value <= upper:
        raise GPX2MapError(f'{name}は{lower:g}〜{upper:g}の範囲で指定してください。')


def validate_settings(settings):
    if settings.map_type not in MAP_TYPES:
        raise GPX2MapError('地図の種類はstd、pale、seamlessphotoから選んでください。')
    _number(settings.scale_factor, '縮尺パラメータ', 0.1, 2.0)
    _number(settings.overlap, 'ページの重なり', 0.0, 1.0)
    _number(settings.page_margin_mm, '印刷用の余白（mm）', 0.0, 20.0)
    if not isinstance(settings.draw_magnetic_north, bool):
        raise GPX2MapError('磁北線の表示はONまたはOFFで指定してください。')
    if settings.magnetic_spacing_m not in SPACING_OPTIONS_M:
        raise GPX2MapError('磁北線の間隔は500m、1km、2kmから選択してください。')


def validate_coordinate(lat, lon):
    _number(lat, '緯度', -MAX_LATITUDE, MAX_LATITUDE)
    _number(lon, '経度', -180.0, 180.0)


def latlon_to_tile(lat, lon, zoom=ZOOM_LEVEL):
    validate_coordinate(lat, lon)
    n = 2.0 ** zoom
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return max(0.0, min(n, x)), max(0.0, min(n, y))


def tile_to_latlon(x, y, zoom=ZOOM_LEVEL):
    n = 2.0 ** zoom
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n)))), x / n * 360.0 - 180.0


def _distance(a, b):
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(min(1.0, max(0.0, h))))


def inspect_gpx(gpx_data, limits=DEFAULT_LIMITS):
    if not isinstance(gpx_data, bytes):
        raise GPX2MapError('GPXファイルの内容をバイト列で指定してください。')
    if len(gpx_data) > limits.input_bytes:
        raise ResourceLimitError(f'GPXの容量は{limits.input_bytes / 1024**2:g}MiB以下にしてください。対象区間を分けてください。')
    if not gpx_data.strip():
        raise GPX2MapError('GPXファイルが空です。GPSや登山アプリから書き出したGPXを選んでください。')
    try:
        count, elements, depth = 0, 0, 0
        for event, element in ElementTree.iterparse(io.BytesIO(gpx_data), events=('start', 'end'), forbid_dtd=True):
            if event == 'end':
                depth -= 1
                element.clear()
                continue
            depth += 1
            elements += 1
            if elements == 1 and element.tag.split('}')[-1] != 'gpx':
                raise GPX2MapError('GPX形式のファイルを選んでください。')
            if depth > 32 or elements > 200000:
                raise ResourceLimitError('GPXの構造が複雑すぎます。拡張データを減らして書き出し直してください。')
            if element.tag.split('}')[-1] in ('trkpt', 'rtept', 'wpt'):
                count += 1
                if count > limits.points:
                    raise ResourceLimitError(f'座標点は{limits.points:,}点以下にしてください。対象区間を分けるか、点数を減らしてください。')
                validate_coordinate(float(element.attrib['lat']), float(element.attrib['lon']))
        gpx = gpxpy.parse(gpx_data)
    except GPX2MapError:
        raise
    except Exception as error:
        raise GPX2MapError('GPXを読み込めません。XMLと緯度・経度を確認し、GPXを書き出し直してください。') from error
    segments = []
    track_segments = 0
    for track in gpx.tracks:
        for segment in track.segments:
            if segment.points:
                segments.append(tuple((p.latitude, p.longitude) for p in segment.points))
                track_segments += 1
    for route in gpx.routes:
        if route.points:
            segments.append(tuple((p.latitude, p.longitude) for p in route.points))
    # Waypoints have no implied ordering or connecting line.
    segments.extend(((p.latitude, p.longitude),) for p in gpx.waypoints)
    if not segments:
        raise GPX2MapError('有効な座標点がありません。トラック、ルート、またはウェイポイントを含むGPXを選んでください。')
    return GPXDocument(
        tuple(segments), len(gpx.tracks), len(gpx.routes), track_segments, len(gpx.waypoints),
        sum(map(len, segments)), sum(_distance(a, b) for s in segments for a, b in zip(s, s[1:])),
        len(gpx_data), hashlib.sha256(gpx_data).hexdigest(),
    )


def parse_gpx(gpx_data):
    return [list(segment) for segment in inspect_gpx(gpx_data).segments]


def is_point_in_box(x, y, bx, by, bw, bh):
    return bx - 1e-9 <= x <= bx + bw + 1e-9 and by - 1e-9 <= y <= by + bh + 1e-9


def segment_box_interval(p1, p2, gx, gy, gw, gh):
    """Liang–Barsky clipping, returning the covered parameter interval on [0, 1]."""
    lower, upper = 0.0, 1.0
    for origin, delta, minimum, maximum in (
        (p1[0], p2[0] - p1[0], gx, gx + gw),
        (p1[1], p2[1] - p1[1], gy, gy + gh),
    ):
        if abs(delta) < 1e-14:
            if origin < minimum - 1e-9 or origin > maximum + 1e-9:
                return None
        else:
            a, b = sorted(((minimum - origin) / delta, (maximum - origin) / delta))
            lower, upper = max(lower, a), min(upper, b)
            if lower > upper + 1e-10:
                return None
    return lower, upper


def is_segment_intersecting_grid(p1, p2, gx, gy, gw, gh):
    return segment_box_interval(p1, p2, gx, gy, gw, gh) is not None


def verify_coverage(tile_segments, grids, width, height):
    """Check whole line intervals, including crossings whose endpoints are outside."""
    for segment in tile_segments:
        if len(segment) == 1:
            if not any(is_point_in_box(*segment[0], x, y, width, height) for x, y in grids):
                return False
        for a, b in zip(segment, segment[1:]):
            intervals = sorted(i for x, y in grids if (i := segment_box_interval(a, b, x, y, width, height)) is not None)
            cursor = 0.0
            for lower, upper in intervals:
                if lower > cursor + 1e-9:
                    return False
                cursor = max(cursor, upper)
            if cursor < 1.0 - 1e-9:
                return False
    return True


def _geometry(tracks_points, scale_factor, page_margin_mm):
    segments = tuple(tuple(latlon_to_tile(*point) for point in s) for s in tracks_points if s)
    if not segments:
        return (), 1.0, 1.0, 35.0
    points = [point for s in segments for point in s]
    center_y = (min(p[1] for p in points) + max(p[1] for p in points)) / 2
    reference_lat = tile_to_latlon(0, center_y)[0]
    tile_m = ground_meters_per_tile(reference_lat, ZOOM_LEVEL)
    denominator = 25000 / scale_factor
    width = (297 - 2 * page_margin_mm) / 1000 * denominator / tile_m
    height = (210 - 2 * page_margin_mm) / 1000 * denominator / tile_m
    return segments, width, height, reference_lat


def calculate_grids(tracks_points, scale_factor=1.0, overlap=0.5, page_margin_mm=5.0, *, limits=DEFAULT_LIMITS):
    validate_settings(Settings('std', scale_factor, overlap, page_margin_mm))
    if sum(len(s) for s in tracks_points) > limits.points:
        raise ResourceLimitError('座標点数が上限を超えています。対象区間を分けてください。')
    segments, width, height, _ = _geometry(tracks_points, scale_factor, page_margin_mm)
    if not segments:
        return [], width, height
    points = [p for s in segments for p in s]
    min_x, max_x = min(p[0] for p in points), max(p[0] for p in points)
    min_y, max_y = min(p[1] for p in points), max(p[1] for p in points)
    step_w, step_h = width - overlap, height - overlap
    if min(step_w, step_h) <= 0:
        raise GPX2MapError('ページの重なりを小さくしてください。')
    nw = max(1, math.ceil(max(0, max_x - min_x - width) / step_w) + 1)
    nh = max(1, math.ceil(max(0, max_y - min_y - height) / step_h) + 1)
    if nw * nh > limits.candidate_cells:
        raise ResourceLimitError('地図の対象範囲が広すぎます。対象区間を分けてください。')
    total_w, total_h = (nw - 1) * step_w + width, (nh - 1) * step_h + height
    world = 2**ZOOM_LEVEL
    if total_w > world or total_h > world:
        raise ResourceLimitError('地図の対象範囲が広すぎます。対象区間を分けてください。')
    # One common origin; never shift individual pages after selecting them.
    start_x = min(max(0.0, (min_x + max_x - total_w) / 2), world - total_w)
    start_y = min(max(0.0, (min_y + max_y - total_h) / 2), world - total_h)
    occupied, checks = set(), 0
    for segment in segments:
        lines = zip(segment, segment[1:]) if len(segment) > 1 else [(segment[0], segment[0])]
        for a, b in lines:
            first_x = max(0, math.ceil((min(a[0], b[0]) - start_x - width - 1e-9) / step_w))
            last_x = min(nw - 1, math.floor((max(a[0], b[0]) - start_x + 1e-9) / step_w))
            first_y = max(0, math.ceil((min(a[1], b[1]) - start_y - height - 1e-9) / step_h))
            last_y = min(nh - 1, math.floor((max(a[1], b[1]) - start_y + 1e-9) / step_h))
            for row in range(first_y, last_y + 1):
                for col in range(first_x, last_x + 1):
                    if (row, col) in occupied:
                        continue
                    checks += 1
                    if checks > limits.intersection_checks:
                        raise ResourceLimitError('ページ計算の処理量が上限を超えました。対象区間か座標点数を減らしてください。')
                    if is_segment_intersecting_grid(a, b, start_x + col * step_w, start_y + row * step_h, width, height):
                        occupied.add((row, col))
                        if len(occupied) > limits.pages:
                            raise ResourceLimitError(f'地図は{limits.pages}ページまでです。対象区間を分けてください。')
    grids = [(start_x + col * step_w, start_y + row * step_h) for row, col in sorted(occupied)]
    if not verify_coverage(segments, grids, width, height):
        raise GPX2MapError('経路全体を含むページを計算できませんでした。対象区間を分けてください。')
    return grids, width, height


def tile_bounds(gx, gy, width, height):
    return (math.floor(gx + 1e-10), math.ceil(gx + width - 1e-10),
            math.floor(gy + 1e-10), math.ceil(gy + height - 1e-10))


def plan_pdf(gpx_data, map_type='std', scale_factor=1.0, overlap=0.5, page_margin_mm=5.0,
             draw_magnetic_north=True, *, magnetic_spacing_m=1000, document=None, limits=DEFAULT_LIMITS):
    settings = Settings(map_type, scale_factor, overlap, page_margin_mm, draw_magnetic_north, magnetic_spacing_m)
    validate_settings(settings)
    if document is None:
        document = inspect_gpx(gpx_data, limits)
    elif document.digest != hashlib.sha256(gpx_data).hexdigest() or document.size_bytes > limits.input_bytes or document.point_count > limits.points:
        raise GPX2MapError('GPXの内容が変わりました。ファイルを選び直してください。')
    grids, width, height = calculate_grids(document.segments, scale_factor, overlap, page_margin_mm, limits=limits)
    _, _, _, reference_lat = _geometry(document.segments, scale_factor, page_margin_mm)
    tiles = set()
    for gx, gy in grids:
        x0, x1, y0, y1 = tile_bounds(gx, gy, width, height)
        if (x1 - x0) * (y1 - y0) > limits.tiles:
            raise ResourceLimitError(f'1ページの取得量が上限の{limits.tiles}タイルを超えます。1:25,000など、より詳しい縮尺を選んでください。')
        tiles.update((x, y) for x in range(x0, x1) for y in range(y0, y1))
        if len(tiles) > limits.tiles:
            raise ResourceLimitError(f'取得量が上限の{limits.tiles}タイルを超えます。対象区間を分けてください。')
    latitudes = [tile_to_latlon(0, y)[0] for _, gy in grids for y in (gy, gy + height)]
    scale_error = max(abs(math.cos(math.radians(lat)) / math.cos(math.radians(reference_lat)) - 1) for lat in latitudes)
    if scale_error > 0.01:
        raise ResourceLimitError('南北の範囲が広く、縮尺の誤差が1%を超えます。対象区間を分けてください。')
    # Three 4-byte Pillow page buffers plus 64 MiB for GPX/tiles/encoders.
    # JPEG/PDF/ZIP bytes are copied between disk files in bounded blocks.
    estimate = TARGET_WIDTH * TARGET_HEIGHT * 4 * 3 + 64 * 1024**2
    if estimate > limits.memory_bytes:
        raise ResourceLimitError('推定メモリ使用量が上限を超えます。実行環境の設定を確認してください。')
    return GenerationPlan(document, settings, tuple(grids), width, height, tuple(sorted(tiles)), reference_lat, scale_error, estimate, limits)
