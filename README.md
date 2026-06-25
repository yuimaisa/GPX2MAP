# GPX2MAP

GPXデータを読み込み、国土地理院の地図（地理院タイル）上に移動軌跡（トラック）を重ね合わせて、**A4横サイズ（Landscape）のPDFファイル**として出力するPythonアプリケーションです。

## 主な特徴

- **最大情報量の地図**: 等高線が10m毎に描画される詳細な「ズームレベル15」の地理院タイルを固定で取得します。
- **高密度な縮小表示**: 縮小パラメータ `S` (0.1 〜 1.0) を指定することで、詳細な等高線の情報を維持したまま、地図をA4用紙の中に高密度に縮小配置して広範囲を1ページに詰め込むことができます。
- **格子状（グリッド）自動ページ分割**: GPXデータがA4横1ページの表示範囲を超える広範囲にわたる場合、自動的にグリッド（格子）に分割し、トラックが通過する領域のみを抽出して**複数ページの単一PDF**として出力します。ページ間には適度な重なり（マージン）が設けられます。
- **サーバー負荷への配慮**:
  - `requests.Session` を用いてTCP接続を使い回す（HTTP Keep-Alive）ことで、接続オーバーヘッドとサーバー負荷を最小限に抑えています。
  - タイル取得時に適切なウェイト（0.15秒以上）を設けています。
  - コネクション切断や一時的なネットワークエラー時には、指数バックオフによる自動リトライ（最大3回）を行います。
  - 一度ダウンロードしたタイルはローカルディレクトリ（`tile_cache/`）に自動キャッシュし、再実行時にはダウンロードを行いません。
- **クレジットの自動描画**: 国土地理院の利用規約に基づき、各ページの右下に「地図：国土地理院」の出典表記を半透明の背景付きで見やすく自動描画します。
- **Webアプリケーションへの高い拡張性**: コアロジックが `generate_pdf(gpx_data: bytes, ...)` というバイト列を受け渡しする関数としてモジュール化されているため、将来的にStreamlitやFastAPI等で簡単にWebアプリ化（画面からのアップロード・ダウンロード）が可能です。

## セットアップ手順

1. **必要要件**:
   - Python 3.8 以上

2. **依存ライブラリのインストール**:
   ワークスペースのディレクトリで以下のコマンドを実行します。
   ```bash
   pip install -r requirements.txt
   ```

## 使用方法

コマンドラインから引数を指定して実行します。

```bash
python gpx2map.py -i <入力GPXファイル> -o <出力PDFファイル> [オプション]
```

### 引数一覧

| 短い引数 | 長い引数 | 説明 | デフォルト値 |
| :--- | :--- | :--- | :--- |
| `-i` | `--input` | **[必須]** 入力するGPXファイルのパス | - |
| `-o` | `--output` | 出力するPDFファイルのパス | `output.pdf` |
| `-s` | `--scale` | 縮小パラメータ（`0.1` 〜 `1.0`）※値が小さいほど広範囲を高密度に表示 | `1.0` |
| `-t` | `--type` | 地図の種類（`std`: 標準地図, `pale`: 淡色地図, `seamlessphoto`: シームレス空中写真） | `std` |

### 実行例

1. **標準地図で出力（等倍）**:
   ```bash
   python gpx2map.py -i sample.gpx -o output_std.pdf
   ```

2. **淡色地図で、地図を半分に縮小（高密度で広範囲を表示）**:
   ```bash
   python gpx2map.py -i sample.gpx -o output_pale_scaled.pdf -t pale -s 0.5
   ```

3. **空中写真で出力**:
   ```bash
   python gpx2map.py -i sample.gpx -o output_photo.pdf -t seamlessphoto
   ```

---

## 将来のWebアプリケーション化（Streamlitの例）

本スクリプトの `generate_pdf` 関数はメモリ内のバイトデータをやり取りするため、以下のような非常にシンプルなStreamlitスクリプト（例: `app.py`）を作成するだけで、簡単にWebアプリケーション化することができます。

```python
import streamlit as st
from gpx2map import generate_pdf

st.title("GPX 地理院地図 PDFジェネレーター")

uploaded_file = st.file_uploader("GPXファイルをアップロードしてください", type=["gpx"])
map_type = st.selectbox("地図の種類", ["std", "pale", "seamlessphoto"])
scale = st.slider("縮小パラメータ (S)", min_value=0.1, max_value=1.0, value=1.0, step=0.1)

if uploaded_file is not None:
    if st.button("PDFを生成"):
        with st.spinner("地図画像をダウンロード・構築中..."):
            try:
                gpx_data = uploaded_file.read()
                pdf_data = generate_pdf(gpx_data, map_type=map_type, scale_factor=scale)
                
                st.success("PDFの生成が完了しました！")
                st.download_button(
                    label="PDFをダウンロード",
                    data=pdf_data,
                    file_name="gpx_map.pdf",
                    mime="application/pdf"
                )
            except Exception as e:
                st.error(f"エラーが発生しました: {e}")
```
これを `streamlit run app.py` で動かすだけで、Web画面から誰でも使えるようになります。
