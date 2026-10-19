"""Offline WMM2025 declination and a shared Web Mercator magnetic grid.

Declination is always east-positive, west-negative. Tile coordinates have x to
the east and y to the south; magnetic north is therefore (sin(D), -cos(D)).
"""

import datetime
import math
from dataclasses import dataclass


MODEL_NAME = "WMM2025"
MODEL_START = datetime.date(2025, 1, 1)
MODEL_END = datetime.date(2030, 1, 1)  # exclusive
SPACING_OPTIONS_M = (500, 1000, 2000)
JST = datetime.timezone(datetime.timedelta(hours=9))


class MagneticNorthError(ValueError):
    """The PDF cannot be generated with trustworthy magnetic information."""


def generation_date():
    """Use the PDF generation date in Japan, also on UTC-hosted Spaces."""
    return datetime.datetime.now(JST).date()


def decimal_year(day):
    start = datetime.date(day.year, 1, 1)
    end = datetime.date(day.year + 1, 1, 1)
    return day.year + (day - start).days / (end - start).days


def wmm_declination(latitude, longitude, year, altitude_km=0.0):
    """Evaluate the pinned standard WMM2025, without network access.

    Altitude is in kilometres above the WGS84 ellipsoid. Production maps use
    zero altitude; an explicit altitude also permits NOAA test-vector checks.
    """
    if not MODEL_START.year <= year < MODEL_END.year:
        raise MagneticNorthError(
            f"WMM2025の適用期間外です（計算年: {year}）。"
            "有効期間は2025-01-01〜2029-12-31です。"
            "対応する地磁気モデルへの更新が必要です。外挿は行いません。"
        )
    if not (math.isfinite(latitude) and -90 <= latitude <= 90
            and math.isfinite(longitude) and -180 <= longitude <= 180
            and math.isfinite(altitude_km) and -1 <= altitude_km <= 850):
        raise MagneticNorthError("磁気偏角の計算地点または標高がWMM2025の範囲外です。")
    try:
        from pygeomag import GeoMag
        from pygeomag.wmm.wmm_2025 import WMM_2025

        model = GeoMag(coefficients_data=WMM_2025)
        result = model.calculate(
            glat=latitude, glon=longitude, alt=altitude_km, time=year,
            allow_date_outside_lifespan=False,
        )
        if not math.isfinite(result.d):
            raise ValueError("偏角が有限値ではありません")
        return result.d
    except Exception as exc:
        raise MagneticNorthError(
            f"磁気偏角の計算に失敗しました（WMM2025 / pygeomag）: {exc}"
        ) from exc


@dataclass(frozen=True)
class MagneticDeclination:
    degrees: float
    latitude: float
    longitude: float
    calculation_date: datetime.date
    model: str = MODEL_NAME

    def legend_lines(self, japanese=True):
        # Convert the magnitude, then apply the direction/sign to the whole
        # value. This also handles -0.x degrees and minute rounding to 60.
        minutes = int(math.floor(abs(self.degrees) * 60 + 0.5))
        deg, minute = divmod(minutes, 60)
        if japanese:
            direction = "東偏" if self.degrees > 0 else "西偏" if self.degrees < 0 else "偏角なし"
            label = f"磁気偏角: {direction} {deg}°{minute:02d}′ ({self.degrees:+.2f}°)"
            date_label = "計算日"
        else:
            direction = "East" if self.degrees > 0 else "West" if self.degrees < 0 else "Zero"
            label = f"Declination: {deg}d{minute:02d}' {direction} ({self.degrees:+.2f} deg)"
            date_label = "Date"
        return label, f"{self.model} | {date_label}: {self.calculation_date.isoformat()}"


def calculate_declination(latitude, longitude, day=None):
    day = generation_date() if day is None else day
    degrees = wmm_declination(latitude, longitude, decimal_year(day))
    return MagneticDeclination(degrees, latitude, longitude, day)


def ground_meters_per_tile(latitude, zoom=15):
    """Spherical Web Mercator ground scale, matching the map page planner."""
    return 2 * math.pi * 6378137.0 * math.cos(math.radians(latitude)) / 2**zoom


@dataclass(frozen=True)
class MagneticGrid:
    """One origin, angle and ground scale for every page of a PDF.

    Spacing is measured perpendicular to the parallel lines at the same
    reference latitude used by calculate_grids for the printed map scale.
    Web Mercator has vertical meridians (no meridian convergence).
    """

    declination: float
    origin_x: float
    origin_y: float
    reference_latitude: float
    spacing_m: int = 1000
    zoom: int = 15

    def __post_init__(self):
        if self.spacing_m not in SPACING_OPTIONS_M:
            raise MagneticNorthError("磁北線の間隔は500m、1km、2kmから選択してください。")
        if not math.isfinite(self.declination):
            raise MagneticNorthError("磁北線の偏角が有限値ではありません。")

    def segments(self, grid_x, grid_y, width_tiles, height_tiles, image_size):
        """Return line segments clipped to this map's rectangle, in pixels."""
        angle = math.radians(self.declination)
        nx, ny = math.cos(angle), math.sin(angle)
        spacing = self.spacing_m / ground_meters_per_tile(self.reference_latitude, self.zoom)
        phase = nx * (grid_x - self.origin_x) + ny * (grid_y - self.origin_y)
        corners = (0, nx * width_tiles, ny * height_tiles,
                   nx * width_tiles + ny * height_tiles)
        first = math.ceil((phase + min(corners)) / spacing - 1e-10)
        last = math.floor((phase + max(corners)) / spacing + 1e-10)
        img_w, img_h = image_size
        result = []
        epsilon = 1e-9
        for index in range(first, last + 1):
            offset = index * spacing - phase
            intersections = []
            if abs(ny) > 1e-12:
                for x in (0.0, width_tiles):
                    y = (offset - nx * x) / ny
                    if -epsilon <= y <= height_tiles + epsilon:
                        intersections.append((x, max(0.0, min(height_tiles, y))))
            if abs(nx) > 1e-12:
                for y in (0.0, height_tiles):
                    x = (offset - ny * y) / nx
                    if -epsilon <= x <= width_tiles + epsilon:
                        intersections.append((max(0.0, min(width_tiles, x)), y))
            if len(intersections) < 2:
                continue
            start = intersections[0]
            end = max(intersections, key=lambda p: math.hypot(p[0] - start[0], p[1] - start[1]))
            if math.hypot(end[0] - start[0], end[1] - start[1]) < epsilon:
                continue
            result.append(tuple(
                (x / width_tiles * img_w, y / height_tiles * img_h)
                for x, y in (start, end)
            ))
        return result
