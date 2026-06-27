---
title: GPX2MAP
emoji: 🗺
colorFrom: gray
colorTo: green
sdk: streamlit
sdk_version: "1.30.0"
app_file: app.py
pinned: false
---

# GPX2MAP

GPXデータを読み込み、国土地理院の地図上にトラックを重ね合わせて、A4横サイズのPDFファイルとして出力するPythonアプリケーションです。

等高線が10m毎に描画されるズームレベル15の地理院タイルを取得します。さらに地図の縮小を行うことができ、縮小パラメータ `s (0.1 ~ 1.0)` が`1.0`のとき1:25,000縮尺、`0.5`のとき1:50,000縮尺になります。
地図が複数ページにわたる場合、重複マージンのタイル数を`m`で選択できます。コンビニ印刷で見切れないように、四辺に余白を5mm取ります。

## セットアップ手順

1. 必要要件:
   - Python 3.8 以上

2. 依存関係:
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
| `-s` | `--scale` | 縮小パラメータ（`0.1` 〜 `1.0`）`1.0`で1:25,000縮尺、0.5で1:50,000縮尺 | `1.0` |
| `-m` | `--margin` | ページ間重複（タイル数） | `0.5` |
| `-t` | `--type` | 地図の種類（`std`: 標準地図, `pale`: 淡色地図, `seamlessphoto`: シームレス空中写真） | `std` |

### 実行例

1. 標準地図で出力（等倍）:
   ```bash
   python gpx2map.py -i sample.gpx
   ```

2. 個人的なコピペ :
   ```bash
   python gpx2map.py -i sample.gpx -m 0.2
   ```

   
### 出力例

<img width="841" height="593" alt="image" src="https://github.com/user-attachments/assets/8d46c5b7-2ad2-4856-ae2c-47612d295c42" />


