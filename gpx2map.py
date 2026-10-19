"""GPX to A4 landscape PDF, with bounded page-at-a-time rendering."""
import argparse
from dataclasses import dataclass
import hashlib
import io
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import threading

import requests
from PIL import Image, ImageDraw, ImageFont
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm

from map_plan import (
    DEFAULT_LIMITS, GPX2MapError, ResourceLimitError, Settings,
    TARGET_WIDTH, TARGET_HEIGHT, ZOOM_LEVEL, calculate_grids, inspect_gpx,
    is_point_in_box, is_segment_intersecting_grid, latlon_to_tile, parse_gpx,
    plan_pdf, tile_bounds, tile_to_latlon,
)
from tile_store import MAP_URLS, TileDownloadError, download_tile

# One render at a time per server process; sessions keep their own immutable plan.
_RENDER_SLOT = threading.BoundedSemaphore(1)


@dataclass(frozen=True)
class Progress:
    stage: str
    page: int
    pages: int
    tiles: int
    total_tiles: int


def parse_declination(value):
    """Parse signed decimal or degree/minute notation; east is positive."""
    text = str(value).strip().replace('−', '-')
    if '°' not in text:
        result = float(text)
    else:
        match = re.fullmatch(r'([+-]?\d+(?:\.\d+)?)\s*°\s*(\d+(?:\.\d+)?)\s*[\'′]?', text)
        if not match or float(match[2]) >= 60:
            raise GPX2MapError('偏角の度分表記が不正です。')
        result = (abs(float(match[1])) + float(match[2]) / 60) * (-1 if match[1].startswith('-') else 1)
    if not math.isfinite(result) or abs(result) > 180:
        raise GPX2MapError('偏角の値が不正です。')
    return result


def fetch_magnetic_declination(lat, lon, year):
    """The legacy GSI endpoint returns 404; no unverified replacement is used."""
    return None


def build_map_image(grid_x, grid_y, page_w_tiles, page_h_tiles, map_type, session=None,
                    *, target_size=(TARGET_WIDTH, TARGET_HEIGHT), cache_dir=None, tile_callback=None):
    """Composite tiles directly into print resolution, without a tile-size mosaic."""
    width, height = target_size
    image = Image.new('RGBA', target_size, 'white')
    x0, x1, y0, y1 = tile_bounds(grid_x, grid_y, page_w_tiles, page_h_tiles)
    try:
        for y in range(y0, y1):
            for x in range(x0, x1):
                tile = download_tile(x, y, ZOOM_LEVEL, map_type, session=session, cache_dir=cache_dir)
                try:
                    left = round((x - grid_x) / page_w_tiles * width)
                    right = round((x + 1 - grid_x) / page_w_tiles * width)
                    top = round((y - grid_y) / page_h_tiles * height)
                    bottom = round((y + 1 - grid_y) / page_h_tiles * height)
                    resized = tile.resize((max(1, right - left), max(1, bottom - top)), Image.Resampling.LANCZOS)
                    try:
                        image.paste(resized, (left, top))
                    finally:
                        resized.close()
                finally:
                    tile.close()
                if tile_callback is not None:
                    tile_callback(x, y)
        return image
    except BaseException:
        image.close()
        raise


def draw_track_and_credits(image, tracks_points, grid_x, grid_y, page_w_tiles, page_h_tiles, scale_factor=1.0, page_margin_mm=5.0, declination=None):
    """
    画像上にGPXの軌跡と国土地理院のクレジットおよび縮尺（スケールバー）を描画する。
    """
    draw = ImageDraw.Draw(image)
    img_w, img_h = image.size
    printable_width_mm = 297.0 - 2 * page_margin_mm
    
    # 磁北線の描画 (赤線)
    if declination is not None:
        # 東偏を正とする。北は画面の上、東は画面の右。
        angle_rad = math.radians(declination)
        spacing = 800  # 線の間隔(ピクセル)
        diag = math.hypot(img_w, img_h)
        cx, cy = img_w / 2, img_h / 2
        
        for offset in range(-int(diag), int(diag), spacing):
            # 垂直線に対する法線方向にオフセットを移動
            base_x = cx + offset * math.cos(angle_rad)
            base_y = cy + offset * math.sin(angle_rad)
            
            # 線分の方向ベクトル (上下にdiag伸ばす)
            dx = math.sin(angle_rad) * diag
            dy = math.cos(angle_rad) * diag
            
            # Y軸は下方向が正であるため、北方向は -dy、南方向は +dy
            x1, y1 = base_x + dx, base_y - dy
            x2, y2 = base_x - dx, base_y + dy
            
            draw.line([(x1, y1), (x2, y2)], fill=(255, 50, 50, 150), width=3)
    
    # GPX軌跡の描画
    # すべてのセグメントごとに線を描く
    for segment in tracks_points:
        points_px = []
        for lat, lon in segment:
            tx, ty = latlon_to_tile(lat, lon, ZOOM_LEVEL)
            # 画像サイズへのマッピング
            px = (tx - grid_x) / page_w_tiles * img_w
            py = (ty - grid_y) / page_h_tiles * img_h
            points_px.append((px, py))
            
        if len(points_px) >= 2:
            # 軌跡を描画: 赤色 (255, 0, 0, 200), 太さ 6px
            # joint="round" で角を丸くして滑らかにする
            draw.line(points_px, fill=(230, 10, 10, 220), width=6, joint="round")
        elif len(points_px) == 1:
            # ポイントが1つだけの場合は円を描画
            px, py = points_px[0]
            draw.ellipse([px-5, py-5, px+5, py+5], fill=(230, 10, 10, 220))
            
    # 日本語フォントの読み込みを試行
    font_large = None
    font_medium = None
    local_font_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
    font_paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "NotoSansJP-Regular.otf"),
        os.path.join(local_font_dir, "NotoSansJP-Regular.otf"),
        os.path.join(local_font_dir, "NotoSansJP-Regular.ttf"),
        "C:\\Windows\\Fonts\\msgothic.ttc",
        "C:\\Windows\\Fonts\\msmincho.ttc",
        "C:\\Windows\\Fonts\\meiryo.ttc"
    ]
    for fp in font_paths:
        if os.path.exists(fp):
            try:
                font_large = ImageFont.truetype(fp, 36)
                font_medium = ImageFont.truetype(fp, 28)
                break
            except Exception:
                pass
                
    if font_large is None:
        raise GPX2MapError('日本語フォントを読み込めません。assetsのフォントを配置し直してください。')
        
    # 右下の出典クレジットの描画
    credit_text = "地図：国土地理院"
    
    # テキストサイズを取得して右下に配置
    # Pillowのバージョン差異に対応
    try:
        left, top, right, bottom = draw.textbbox((0, 0), credit_text, font=font_large)
        text_w = right - left
        text_h = bottom - top
    except AttributeError:
        # 古いPillow用のフォールバック
        text_w, text_h = draw.textsize(credit_text, font=font_large)
        
    margin = 20
    rect_padding = 10
    
    # クレジットテキストの座標
    tx = img_w - text_w - margin - rect_padding
    ty = img_h - text_h - margin - rect_padding
    
    # クレジット背景の白い半透明四角形
    bg_coords = [
        tx - rect_padding, 
        ty - rect_padding, 
        img_w - margin, 
        img_h - margin
    ]
    draw.rectangle(bg_coords, fill=(255, 255, 255, 180), outline=(200, 200, 200, 255), width=1)
    
    # クレジットテキスト描画
    draw.text((tx, ty), credit_text, fill=(50, 50, 50, 255), font=font_large)

    # 左下の縮尺とスケールバーの算出と描画
    # 縮尺比率の算出
    scale_denom = int(round(25000.0 / scale_factor))
    scale_text = f"1:{scale_denom:,}"
    
    # 1ピクセルあたりの地上距離（メートル）
    target_width_m = (printable_width_mm / 1000.0) * scale_denom
    pixel_ground_meter = target_width_m / img_w
    
    # 適切なスケールバーの距離の選択 (目標: 300px程度の長さ)
    candidates = [10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000]
    best_candidate = candidates[0]
    min_diff = float('inf')
    for c in candidates:
        w_px = c / pixel_ground_meter
        if 150 <= w_px <= 500:
            diff = abs(w_px - 300)
            if diff < min_diff:
                min_diff = diff
                best_candidate = c
        else:
            diff = abs(w_px - 300)
            if diff < min_diff:
                min_diff = diff
                best_candidate = c
                
    bar_px = best_candidate / pixel_ground_meter
    
    if best_candidate >= 1000:
        bar_text = f"{best_candidate / 1000:.0f} km" if best_candidate % 1000 == 0 else f"{best_candidate / 1000:.1f} km"
    else:
        bar_text = f"{best_candidate} m"
        
    # テキストサイズ等の取得
    try:
        left, top, right, bottom = draw.textbbox((0, 0), scale_text, font=font_large)
        scale_w = right - left
        scale_h = bottom - top
    except AttributeError:
        scale_w, scale_h = draw.textsize(scale_text, font=font_large)
        
    try:
        left, top, right, bottom = draw.textbbox((0, 0), bar_text, font=font_medium)
        bar_text_w = right - left
        bar_text_h = bottom - top
    except AttributeError:
        bar_text_w, bar_text_h = draw.textsize(bar_text, font=font_medium)
        
    left_margin = 20
    left_padding = 15
    
    # 全体の幅と高さの計算
    content_width = max(scale_w, bar_px, bar_text_w)
    box_width = content_width + left_padding * 2
    
    # 縦方向レイアウト:
    # 縮尺比率テキスト + 間隔(10) + スケールバーテキスト + 間隔(5) + スケールバー領域(10px) + 余白
    box_height = scale_h + 10 + bar_text_h + 5 + 10 + left_padding * 2
    
    box_left = left_margin
    box_bottom = img_h - left_margin
    box_top = box_bottom - box_height
    box_right = box_left + box_width
    
    # 背景の白い半透明四角形
    left_bg_coords = [
        box_left,
        box_top,
        box_right,
        box_bottom
    ]
    draw.rectangle(left_bg_coords, fill=(255, 255, 255, 180), outline=(200, 200, 200, 255), width=1)
    
    # 1. 縮尺値テキスト描画
    scale_x = box_left + left_padding
    scale_y = box_top + left_padding
    draw.text((scale_x, scale_y), scale_text, fill=(50, 50, 50, 255), font=font_large)
    
    # 2. スケールバーテキスト描画 (中央揃え)
    bar_center_x = box_left + left_padding + bar_px / 2
    bar_text_x = bar_center_x - bar_text_w / 2
    bar_text_y = scale_y + scale_h + 10
    draw.text((bar_text_x, bar_text_y), bar_text, fill=(50, 50, 50, 255), font=font_medium)
    
    # 3. スケールバーの線描画
    bar_line_y = bar_text_y + bar_text_h + 5
    bar_start_x = box_left + left_padding
    bar_end_x = bar_start_x + bar_px
    
    # メイン横線
    draw.line(((bar_start_x, bar_line_y), (bar_end_x, bar_line_y)), fill=(50, 50, 50, 255), width=3)
    # 左縦線
    draw.line(((bar_start_x, bar_line_y - 5), (bar_start_x, bar_line_y + 5)), fill=(50, 50, 50, 255), width=3)
    # 右縦線
    draw.line(((bar_end_x, bar_line_y - 5), (bar_end_x, bar_line_y + 5)), fill=(50, 50, 50, 255), width=3)

def build_layout_preview(plan, size=(720, 420)):
    image = Image.new('RGB', size, 'white')
    draw = ImageDraw.Draw(image, 'RGBA')
    min_x = min(x for x, _ in plan.grids)
    min_y = min(y for _, y in plan.grids)
    max_x = max(x + plan.page_w_tiles for x, _ in plan.grids)
    max_y = max(y + plan.page_h_tiles for _, y in plan.grids)
    factor = min((size[0] - 40) / (max_x - min_x), (size[1] - 40) / (max_y - min_y))
    offset_x = (size[0] - (max_x - min_x) * factor) / 2
    offset_y = (size[1] - (max_y - min_y) * factor) / 2
    def pixel(x, y):
        return offset_x + (x - min_x) * factor, offset_y + (y - min_y) * factor
    for index, (x, y) in enumerate(plan.grids, 1):
        left, top = pixel(x, y)
        right, bottom = pixel(x + plan.page_w_tiles, y + plan.page_h_tiles)
        draw.rectangle((left, top, right, bottom), fill=(75, 130, 190, 25), outline=(45, 100, 170, 255), width=2)
        draw.text((left + 5, top + 5), str(index), fill=(20, 50, 90, 255))
    for segment in plan.document.segments:
        points = [pixel(*latlon_to_tile(*point)) for point in segment]
        if len(points) > 1:
            draw.line(points, fill=(220, 25, 25, 255), width=3)
        else:
            x, y = points[0]
            draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=(220, 25, 25, 255))
    return image


def generate_pdf(gpx_data: bytes, map_type='std', scale_factor=1.0, overlap=0.5,
                 page_margin_mm=5.0, draw_magnetic_north=False, *, cache_dir=None,
                 progress_callback=None, plan=None, limits=DEFAULT_LIMITS):
    settings = Settings(map_type, scale_factor, overlap, page_margin_mm)
    if plan is None:
        plan = plan_pdf(gpx_data, map_type, scale_factor, overlap, page_margin_mm, draw_magnetic_north, limits=limits)
    elif (draw_magnetic_north or plan.settings != settings or plan.document.digest != hashlib.sha256(gpx_data).hexdigest()
          or plan.limits != limits):
        raise GPX2MapError('GPXまたは設定が変更されました。生成内容を確認してやり直してください。')
    margin_px = round(page_margin_mm * 300 / 25.4)
    printable_size = (TARGET_WIDTH - 2 * margin_px, TARGET_HEIGHT - 2 * margin_px)
    prepared = set()
    def report(stage, page):
        if progress_callback is not None:
            progress_callback(Progress(stage, page, plan.page_count, len(prepared), plan.tile_count))
    report('waiting', 0)
    with _RENDER_SLOT, tempfile.TemporaryDirectory(prefix='gpx2map-pdf-') as temporary, requests.Session() as session:
        pdf_path = Path(temporary) / 'map.pdf'
        writer = canvas.Canvas(str(pdf_path), pagesize=(297 * mm, 210 * mm), pageCompression=1, invariant=1)
        writer.setTitle('GPX2MAP')
        writer.setAuthor('GPX2MAP')
        encoded_bytes = 0
        for index, (gx, gy) in enumerate(plan.grids, 1):
            report('tiles', index)
            def tile_ready(x, y):
                if (x, y) not in prepared:
                    prepared.add((x, y))
                    report('tiles', index)
            with build_map_image(gx, gy, plan.page_w_tiles, plan.page_h_tiles, map_type, session,
                                 target_size=printable_size, cache_dir=cache_dir, tile_callback=tile_ready) as printable:
                draw_track_and_credits(printable, plan.document.segments, gx, gy, plan.page_w_tiles, plan.page_h_tiles,
                                       scale_factor, page_margin_mm)
                # RGB pages are encoded and closed immediately; no list of raster pages.
                with Image.new('RGB', (TARGET_WIDTH, TARGET_HEIGHT), 'white') as page:
                    page.paste(printable, (margin_px, margin_px), mask=printable.getchannel('A'))
                    page_path = Path(temporary) / f'page-{index}.jpg'
                    page.save(page_path, format='JPEG', quality=95, subsampling=0)
            encoded_bytes += page_path.stat().st_size
            if encoded_bytes * 1.3 + index * 4096 > limits.pdf_bytes:
                raise ResourceLimitError('PDFの容量が上限を超えます。対象区間を分けてください。')
            writer.drawImage(str(page_path), 0, 0, width=297 * mm, height=210 * mm)
            writer.showPage()
            page_path.unlink()
            report('page', index)
        report('pdf', plan.page_count)
        writer.save()
        if pdf_path.stat().st_size > limits.pdf_bytes:
            raise ResourceLimitError('PDFの容量が上限を超えました。対象区間を分けてください。')
        result = pdf_path.read_bytes()
    report('complete', plan.page_count)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='GPXから国土地理院地図のA4横PDFを作成します。')
    parser.add_argument('-i', '--input', required=True, help='入力GPXファイル')
    parser.add_argument('-o', '--output', default='output.pdf', help='出力PDFファイル')
    parser.add_argument('-s', '--scale', type=float, default=1.0, help='縮尺パラメータ（0.1〜2.0、1.0で1:25,000）')
    parser.add_argument('-m', '--margin', type=float, default=0.5, help='ページ間の重なり（0〜1タイル）')
    parser.add_argument('-t', '--type', choices=['std', 'pale', 'seamlessphoto'], default='std')
    parser.add_argument('--page-margin', type=float, default=5.0, help='四辺の余白（0〜20mm）')
    parser.add_argument('--draw-magnetic-north', action='store_true', help='磁北線（現在利用不可）')
    parser.add_argument('--cache-dir', help='公開タイルのキャッシュ保存先')
    args = parser.parse_args(argv)
    try:
        path = Path(args.input)
        if path.stat().st_size > DEFAULT_LIMITS.input_bytes:
            raise ResourceLimitError('GPXの容量は5MiB以下にしてください。')
        with path.open('rb') as source:
            data = source.read(DEFAULT_LIMITS.input_bytes + 1)
        result = generate_pdf(data, args.type, args.scale, args.margin, args.page_margin,
                              args.draw_magnetic_north, cache_dir=args.cache_dir)
        output = Path(args.output)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as handle:
                temporary_path = Path(handle.name)
                handle.write(result)
            os.replace(temporary_path, output)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        print(f'PDFを作成しました: {output}')
        return 0
    except (GPX2MapError, OSError, requests.RequestException) as error:
        print(f'エラー: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
