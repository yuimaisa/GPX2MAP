import math
import os
import time
import io
import argparse
import requests
import gpxpy
from PIL import Image, ImageDraw, ImageFont

# 地理院タイルのURL定義
MAP_URLS = {
    "std": "https://cyberjapandata.gsi.go.jp/xyz/std/15/{x}/{y}.png",
    "pale": "https://cyberjapandata.gsi.go.jp/xyz/pale/15/{x}/{y}.png",
    "seamlessphoto": "https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/15/{x}/{y}.jpg"
}

# A4横サイズ（300 DPI基準）
TARGET_WIDTH = 3508
TARGET_HEIGHT = 2480
ASPECT_RATIO = TARGET_WIDTH / TARGET_HEIGHT # 約1.4145

ZOOM_LEVEL = 15 # 等高線10mが描画される解像度固定

def latlon_to_tile(lat, lon, zoom=ZOOM_LEVEL):
    """
    緯度経度をタイル座標(x, y)の浮動小数点数に変換する。
    """
    lat_rad = math.radians(lat)
    n = 2.0 ** zoom
    x = (lon + 180.0) / 360.0 * n
    # 緯度のメルカトル投影によるタイルY座標計算
    # 緯度が極端に高い・低い場合のゼロ除算を防ぐためにクリップ
    lat_rad = max(min(lat_rad, 1.48), -1.48)
    y = (1.0 - math.log(math.tan(lat_rad) + (1.0 / math.cos(lat_rad))) / math.pi) / 2.0 * n
    return x, y

def tile_to_latlon(x, y, zoom=ZOOM_LEVEL):
    """
    タイル座標(x, y)を緯度経度に変換する。
    """
    n = 2.0 ** zoom
    lon_deg = x / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * y / n)))
    lat_deg = math.degrees(lat_rad)
    return lat_deg, lon_deg

def parse_gpx(gpx_data: bytes):
    """
    GPXデータのバイト列を解析し、トラックポイントの緯度経度リストのリスト（セグメントごと）を返す。
    """
    gpx = gpxpy.parse(gpx_data)
    tracks_points = []
    
    for track in gpx.tracks:
        for segment in track.segments:
            points = []
            for point in segment.points:
                points.append((point.latitude, point.longitude))
            if points:
                tracks_points.append(points)
                
    # トラックがないがルートやウェイポイントがある場合への簡単なフォールバック
    if not tracks_points:
        points = []
        for route in gpx.routes:
            for point in route.points:
                points.append((point.latitude, point.longitude))
        if points:
            tracks_points.append(points)
            
    if not tracks_points:
        points = []
        for wp in gpx.waypoints:
            points.append((wp.latitude, wp.longitude))
        if points:
            tracks_points.append(points)
            
    return tracks_points

def is_point_in_box(x, y, bx, by, bw, bh):
    """点が矩形内にあるか判定"""
    return bx <= x <= bx + bw and by <= y <= by + bh

def is_segment_intersecting_grid(p1, p2, gx, gy, gw, gh):
    """
    線分p1-p2がグリッド(gx, gy, gw, gh)と交差するか判定する。
    """
    x1, y1 = p1
    x2, y2 = p2
    
    # どちらかの端点がグリッド内にある場合は交差
    if is_point_in_box(x1, y1, gx, gy, gw, gh) or is_point_in_box(x2, y2, gx, gy, gw, gh):
        return True
        
    # 線分のAABB（Bounding Box）とグリッドのAABBが重ならない場合は交差しない
    seg_min_x, seg_max_x = min(x1, x2), max(x1, x2)
    seg_min_y, seg_max_y = min(y1, y2), max(y1, y2)
    
    if seg_max_x < gx or seg_min_x > gx + gw or seg_max_y < gy or seg_min_y > gy + gh:
        return False
        
    # 各辺との交差判定
    # 左辺 x = gx
    if x1 != x2:
        y_left = y1 + (y2 - y1) * (gx - x1) / (x2 - x1)
        if min(x1, x2) <= gx <= max(x1, x2) and gy <= y_left <= gy + gh:
            return True
            
        # 右辺 x = gx + gw
        y_right = y1 + (y2 - y1) * (gx + gw - x1) / (x2 - x1)
        if min(x1, x2) <= gx + gw <= max(x1, x2) and gy <= y_right <= gy + gh:
            return True
            
    if y1 != y2:
        # 上辺 y = gy
        x_top = x1 + (x2 - x1) * (gy - y1) / (y2 - y1)
        if min(y1, y2) <= gy <= max(y1, y2) and gx <= x_top <= gx + gw:
            return True
            
        # 下辺 y = gy + gh
        x_bottom = x1 + (x2 - x1) * (gy + gh - y1) / (y2 - y1)
        if min(y1, y2) <= gy + gh <= max(y1, y2) and gx <= x_bottom <= gx + gw:
            return True
            
    return False

def calculate_grids(tracks_points, scale_factor=1.0):
    """
    GPXトラックポイント全体を網羅するA4横比率のグリッドを生成し、
    トラックが通過するグリッドセルのリストを返す。
    """
    # 1ページあたりのタイル数（DPI 300のA4ピクセル数 3508x2480 を基準に、縮小パラメータSを考慮）
    page_w_tiles = max(1, int(round((TARGET_WIDTH / scale_factor) / 256)))
    page_h_tiles = max(1, int(round((TARGET_HEIGHT / scale_factor) / 256)))
    
    # すべてのトラックポイントのタイル座標を計算
    all_tile_coords = []
    for segment in tracks_points:
        segment_coords = []
        for lat, lon in segment:
            tx, ty = latlon_to_tile(lat, lon, ZOOM_LEVEL)
            segment_coords.append((tx, ty))
        all_tile_coords.append(segment_coords)
        
    if not all_tile_coords or not any(all_tile_coords):
        return [], page_w_tiles, page_h_tiles
        
    # 全体の範囲を求める
    flat_coords = [pt for seq in all_tile_coords for pt in seq]
    xs = [pt[0] for pt in flat_coords]
    ys = [pt[1] for pt in flat_coords]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    
    # 隣接するページ間の重なり（マージン）を2タイル分とする
    overlap = 2
    step_w = max(1, page_w_tiles - overlap)
    step_h = max(1, page_h_tiles - overlap)
    
    # グリッドの開始位置をトラックの最小値より少し外側に設定（マージン）
    start_x = math.floor(min_x) - 1
    end_x = math.ceil(max_x) + 1
    start_y = math.floor(min_y) - 1
    end_y = math.ceil(max_y) + 1
    
    grids = []
    
    # 格子状に走査
    curr_y = start_y
    while curr_y < end_y:
        curr_x = start_x
        while curr_x < end_x:
            # このグリッド領域にトラックが交差するか判定
            intersects = False
            for segment in all_tile_coords:
                if len(segment) == 1:
                    if is_point_in_box(segment[0][0], segment[0][1], curr_x, curr_y, page_w_tiles, page_h_tiles):
                        intersects = True
                        break
                else:
                    for i in range(len(segment) - 1):
                        if is_segment_intersecting_grid(segment[i], segment[i+1], curr_x, curr_y, page_w_tiles, page_h_tiles):
                            intersects = True
                            break
                    if intersects:
                        break
            
            if intersects:
                grids.append((curr_x, curr_y))
                
            curr_x += step_w
        curr_y += step_h
        
    return grids, page_w_tiles, page_h_tiles

def download_tile(x, y, z, map_type, session=None, cache_dir="tile_cache"):
    """
    国土地理院からタイルをダウンロードする。ローカルキャッシュを優先し、
    新規リクエスト時はセッションを使い回し、かつリトライ機構（指数バックオフ）で接続を安定化させる。
    """
    # 座標の検証 (ズーム15のタイル範囲チェック)
    max_tile_idx = 2**z - 1
    if not (0 <= x <= max_tile_idx and 0 <= y <= max_tile_idx):
        # 範囲外の場合は空の透明な画像を返す
        return Image.new("RGBA", (256, 256), (255, 255, 255, 0))
        
    ext = "jpg" if map_type == "seamlessphoto" else "png"
    tile_cache_path = os.path.join(cache_dir, map_type, str(z), str(x), f"{y}.{ext}")
    
    if os.path.exists(tile_cache_path):
        try:
            return Image.open(tile_cache_path).convert("RGBA")
        except Exception:
            # キャッシュ破損時は削除して再ダウンロード
            os.remove(tile_cache_path)
            
    # 新規ダウンロード
    os.makedirs(os.path.dirname(tile_cache_path), exist_ok=True)
    
    url = MAP_URLS.get(map_type, MAP_URLS["std"]).format(x=x, y=y)
    
    headers = {
        "User-Agent": "GPX2MAP/1.0 (https://github.com/yuimaisa/GPX2MAP; Python-requests)"
    }
    
    client = session if session is not None else requests.Session()
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # サーバー負荷防止のため待機（リトライ時は徐々に長くする）
            wait_time = 0.15 * (2 ** attempt)
            time.sleep(wait_time)
            
            response = client.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                with open(tile_cache_path, "wb") as f:
                    f.write(response.content)
                return Image.open(io.BytesIO(response.content)).convert("RGBA")
            elif response.status_code == 404:
                # 地図データが存在しない領域（海など）は白色画像で埋める
                img = Image.new("RGBA", (256, 256), (240, 240, 240, 255))
                # キャッシュ保存して無駄なリクエストを防ぐ
                img.save(tile_cache_path)
                return img
            else:
                print(f"警告: タイルの取得に失敗しました (HTTP {response.status_code}): {url}")
        except (requests.RequestException, ConnectionResetError) as e:
            if attempt < max_retries - 1:
                # 警告ログを出して再試行
                print(f"警告: 接続エラーが発生しました。再試行します ({attempt + 1}/{max_retries}): {e}")
            else:
                print(f"エラー: タイル取得中に回復不能なエラーが発生しました: {e}")
        
    # エラー時のフォールバック（薄いグレーのダミータイル）
    return Image.new("RGBA", (256, 256), (230, 230, 230, 255))

def build_map_image(grid_x, grid_y, page_w_tiles, page_h_tiles, map_type, session=None):
    """
    指定されたグリッド範囲のタイル画像を結合し、1枚の大きな画像を作成する。
    """
    map_img = Image.new("RGBA", (page_w_tiles * 256, page_h_tiles * 256), (255, 255, 255, 255))
    
    for dy in range(page_h_tiles):
        for dx in range(page_w_tiles):
            tx = grid_x + dx
            ty = grid_y + dy
            tile = download_tile(tx, ty, ZOOM_LEVEL, map_type, session=session)
            map_img.paste(tile, (dx * 256, dy * 256))
            
    return map_img

def draw_track_and_credits(image, tracks_points, grid_x, grid_y, page_w_tiles, page_h_tiles):
    """
    画像上にGPXの軌跡と国土地理院のクレジットを描画する。
    """
    draw = ImageDraw.Draw(image)
    
    # GPX軌跡の描画
    # すべてのセグメントごとに線を描く
    for segment in tracks_points:
        points_px = []
        for lat, lon in segment:
            tx, ty = latlon_to_tile(lat, lon, ZOOM_LEVEL)
            # A4横サイズ(TARGET_WIDTH x TARGET_HEIGHT)へのマッピング
            px = (tx - grid_x) / page_w_tiles * TARGET_WIDTH
            py = (ty - grid_y) / page_h_tiles * TARGET_HEIGHT
            points_px.append((px, py))
            
        if len(points_px) >= 2:
            # 軌跡を描画: 赤色 (255, 0, 0, 200), 太さ 6px
            # joint="round" で角を丸くして滑らかにする
            draw.line(points_px, fill=(230, 10, 10, 220), width=6, joint="round")
        elif len(points_px) == 1:
            # ポイントが1つだけの場合は円を描画
            px, py = points_px[0]
            draw.ellipse([px-5, py-5, px+5, py+5], fill=(230, 10, 10, 220))
            
    # 出典クレジットの描画
    credit_text = "地図：国土地理院"
    
    # Windows用の日本語フォントの読み込みを試行
    font = None
    font_paths = [
        "C:\\Windows\\Fonts\\msgothic.ttc",
        "C:\\Windows\\Fonts\\msmincho.ttc",
        "C:\\Windows\\Fonts\\meiryo.ttc"
    ]
    for fp in font_paths:
        if os.path.exists(fp):
            try:
                font = ImageFont.truetype(fp, 36)
                break
            except Exception:
                pass
                
    if font is None:
        font = ImageFont.load_default()
        
    # テキストサイズを取得して右下に配置
    # Pillowのバージョン差異に対応
    try:
        left, top, right, bottom = draw.textbbox((0, 0), credit_text, font=font)
        text_w = right - left
        text_h = bottom - top
    except AttributeError:
        # 古いPillow用のフォールバック
        text_w, text_h = draw.textsize(credit_text, font=font)
        
    margin = 20
    rect_padding = 10
    
    # クレジットテキストの座標
    tx = TARGET_WIDTH - text_w - margin - rect_padding
    ty = TARGET_HEIGHT - text_h - margin - rect_padding
    
    # クレジット背景の白い半透明四角形
    bg_coords = [
        tx - rect_padding, 
        ty - rect_padding, 
        TARGET_WIDTH - margin, 
        TARGET_HEIGHT - margin
    ]
    draw.rectangle(bg_coords, fill=(255, 255, 255, 180), outline=(200, 200, 200, 255), width=1)
    
    # クレジットテキスト描画
    draw.text((tx, ty), credit_text, fill=(50, 50, 50, 255), font=font)

def generate_pdf(gpx_data: bytes, map_type: str = "std", scale_factor: float = 1.0) -> bytes:
    """
    GPXデータのバイト列を受け取り、A4横マルチページPDFを生成して、そのPDFデータのバイト列を返す。
    WebアプリおよびCLIから共通利用されるコア関数。
    """
    # 1. GPXデータのパース
    tracks_points = parse_gpx(gpx_data)
    if not tracks_points or not any(tracks_points):
        raise ValueError("有効なGPX軌跡データがありませんでした。")
        
    # 2. グリッドセルの計算
    grids, page_w_tiles, page_h_tiles = calculate_grids(tracks_points, scale_factor)
    if not grids:
        raise ValueError("トラックを包含する地図範囲の計算に失敗しました。")
        
    # 安全装置: 合計ダウンロードタイル数のチェック
    total_tiles = len(grids) * page_w_tiles * page_h_tiles
    print(f"情報: 生成ページ数={len(grids)}, 1ページあたりのタイル数={page_w_tiles}x{page_h_tiles} ({page_w_tiles*page_h_tiles}枚)")
    print(f"情報: 想定ダウンロード最大タイル数 = {total_tiles} 枚")
    
    # 1回の処理で500タイルを超える場合はウェイト制御の警告を出しつつ、進行
    if total_tiles > 500:
        print("警告: タイル数が非常に多いため、ダウンロード完了までに時間がかかります。")
        
    pages = []
    
    # 3. セッションを利用して接続を使い回す
    with requests.Session() as session:
        for idx, (grid_x, grid_y) in enumerate(grids):
            print(f"進捗: ページ {idx + 1} / {len(grids)} の地図画像を構築中... (タイル基準左上: {grid_x}, {grid_y})")
            
            # タイル画像を結合
            map_img = build_map_image(grid_x, grid_y, page_w_tiles, page_h_tiles, map_type, session=session)
            
            # A4ターゲット解像度（3508 x 2480）にリサイズ
            # 高品質リサンプリングフィルター（LANCZOS）を使用
            a4_img = map_img.resize((TARGET_WIDTH, TARGET_HEIGHT), Image.Resampling.LANCZOS)
            
            # 軌跡とクレジットを描画
            draw_track_and_credits(a4_img, tracks_points, grid_x, grid_y, page_w_tiles, page_h_tiles)
            
            # PDF用にRGBモードに変換（PDF保存時はアルファチャンネル不可のため背景白で合成）
            pdf_page = Image.new("RGB", (TARGET_WIDTH, TARGET_HEIGHT), (255, 255, 255))
            pdf_page.paste(a4_img, mask=a4_img.split()[3]) # アルファチャンネルをマスクとして使用
            
            pages.append(pdf_page)
            
            # サーバー負荷防止のため、ページ切り替え時に少し余分にスリープ（1.5秒）
            if len(grids) > 1 and idx < len(grids) - 1:
                print("サーバー負荷軽減のため、1.5秒待機しています...")
                time.sleep(1.5)
                
    if not pages:
        raise ValueError("PDFページの生成に失敗しました。")
        
    # 4. PillowのマルチページPDF書き出し機能を使用してバイトデータに変換
    pdf_buffer = io.BytesIO()
    pages[0].save(
        pdf_buffer,
        format="PDF",
        resolution=300.0,
        save_all=True,
        append_images=pages[1:]
    )
    
    return pdf_buffer.getvalue()

def main():
    parser = argparse.ArgumentParser(description="GPXデータから国土地理院地図のA4横PDFを出力します。")
    parser.add_argument("-i", "--input", required=True, help="入力GPXファイルのパス")
    parser.add_argument("-o", "--output", default="output.pdf", help="出力PDFファイルのパス (デフォルト: output.pdf)")
    parser.add_argument("-s", "--scale", type=float, default=1.0, help="縮小パラメータ (0.1〜1.0, デフォルト: 1.0)")
    parser.add_argument("-t", "--type", choices=["std", "pale", "seamlessphoto"], default="std", 
                        help="地図の種類: std(標準地図), pale(淡色地図), seamlessphoto(シームレス空中写真) (デフォルト: std)")
                        
    args = parser.parse_args()
    
    if not os.path.exists(args.input):
        print(f"エラー: 入力ファイル {args.input} が存在しません。")
        return
        
    if not (0.01 <= args.scale <= 5.0):
        print("エラー: 縮小パラメータ --scale は 0.01 以上である必要があります。")
        return
        
    print(f"解析中: {args.input}")
    try:
        with open(args.input, "rb") as f:
            gpx_data = f.read()
            
        print("PDF生成処理を開始します。しばらくお待ちください...")
        pdf_data = generate_pdf(gpx_data, args.type, args.scale)
        
        with open(args.output, "wb") as out_f:
            out_f.write(pdf_data)
            
        print(f"成功: PDFファイルが生成されました -> {args.output}")
        
    except Exception as e:
        print(f"エラーが発生しました: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
