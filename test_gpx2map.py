import unittest
import os
import io
from gpx2map import (
    latlon_to_tile,
    tile_to_latlon,
    parse_gpx,
    is_point_in_box,
    is_segment_intersecting_grid,
    calculate_grids,
    generate_pdf
)

class TestGPX2Map(unittest.TestCase):
    
    def setUp(self):
        self.sample_gpx_path = "sample.gpx"
        self.sample_wide_gpx_path = "sample_wide.gpx"
        
    def test_coordinate_conversion(self):
        # 富士山頂付近の緯度経度
        lat, lon = 35.3606, 138.7273
        zoom = 15
        
        # 緯度経度からタイル座標へ
        tx, ty = latlon_to_tile(lat, lon, zoom)
        self.assertGreater(tx, 0)
        self.assertGreater(ty, 0)
        
        # タイル座標から緯度経度に戻す
        lat_back, lon_back = tile_to_latlon(tx, ty, zoom)
        
        # ほぼ一致するか検証 (誤差がごくわずかであることを保証)
        self.assertAlmostEqual(lat, lat_back, places=4)
        self.assertAlmostEqual(lon, lon_back, places=4)

    def test_parse_gpx(self):
        # sample.gpx の解析テスト
        if os.path.exists(self.sample_gpx_path):
            with open(self.sample_gpx_path, "rb") as f:
                data = f.read()
            tracks = parse_gpx(data)
            
            self.assertEqual(len(tracks), 1)  # 1つのトラックセグメント
            self.assertEqual(len(tracks[0]), 4)  # 4つのポイント
            
            # 最初のポイントが東京タワー(35.6586, 139.7454)に近いか
            self.assertAlmostEqual(tracks[0][0][0], 35.6586, places=4)
            self.assertAlmostEqual(tracks[0][0][1], 139.7454, places=4)

    def test_segment_intersection(self):
        # グリッド (10, 10, 5, 5) => x: 10~15, y: 10~15
        gx, gy, gw, gh = 10, 10, 5, 5
        
        # ケース1: 完全に外側にある線分
        p1 = (5, 5)
        p2 = (8, 8)
        self.assertFalse(is_segment_intersecting_grid(p1, p2, gx, gy, gw, gh))
        
        # ケース2: 一方の端点がグリッド内にある線分
        p1 = (12, 12)
        p2 = (8, 8)
        self.assertTrue(is_segment_intersecting_grid(p1, p2, gx, gy, gw, gh))
        
        # ケース3: グリッドを横切る線分 (端点は両方外側)
        p1 = (8, 12)
        p2 = (17, 12)
        self.assertTrue(is_segment_intersecting_grid(p1, p2, gx, gy, gw, gh))
        
        # ケース4: 対角線上に横切る線分
        p1 = (8, 8)
        p2 = (17, 17)
        self.assertTrue(is_segment_intersecting_grid(p1, p2, gx, gy, gw, gh))

    def test_calculate_grids_single_page(self):
        # sample.gpx (東京タワーから皇居) のグリッド計算
        # 3km程度の短い距離なので、スケール1.0であれば1ページに収まるはず
        if os.path.exists(self.sample_gpx_path):
            with open(self.sample_gpx_path, "rb") as f:
                data = f.read()
            tracks = parse_gpx(data)
            
            grids, pw, ph = calculate_grids(tracks, scale_factor=1.0)
            # グリッドの数
            self.assertEqual(len(grids), 1)

    def test_calculate_grids_multi_page(self):
        # sample_wide.gpx (東京から横浜) のグリッド計算
        # 30kmの距離なので、スケール1.0なら確実に複数ページに分割されるはず
        if os.path.exists(self.sample_wide_gpx_path):
            with open(self.sample_wide_gpx_path, "rb") as f:
                data = f.read()
            tracks = parse_gpx(data)
            
            grids, pw, ph = calculate_grids(tracks, scale_factor=1.0)
            # グリッド数が2ページ以上であることを確認
            self.assertGreater(len(grids), 1)
            print(f"テスト情報: 東京-横浜ルートは {len(grids)} ページに分割されました。")

    def test_generate_pdf_structure(self):
        # sample.gpx からのPDF生成が成功し、有効なPDFヘッダ（%PDF）を持っているか
        if os.path.exists(self.sample_gpx_path):
            with open(self.sample_gpx_path, "rb") as f:
                data = f.read()
            
            # テスト時間短縮のため、地図タイプはキャッシュが速そうな pale に
            try:
                pdf_data = generate_pdf(data, map_type="pale", scale_factor=1.0)
                self.assertTrue(pdf_data.startswith(b"%PDF"))
                self.assertGreater(len(pdf_data), 1000) # 十分なサイズがあること
            except Exception as e:
                self.fail(f"generate_pdf が例外をスローしました: {e}")

if __name__ == "__main__":
    unittest.main()
