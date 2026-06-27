# pyrefly: ignore [missing-import]
import streamlit as st
import os
import requests
import tempfile
from gpx2map import generate_pdf

# ページ設定 (絵文字やアイコンを排除)
st.set_page_config(
    page_title="GPX2MAP",
    layout="centered"
)

# 日本語フォントの自動ダウンロード
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
FONT_PATH = os.path.join(FONT_DIR, "NotoSansJP-Regular.otf")
FONT_URL = "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/SubsetOTF/JP/NotoSansJP-Regular.otf"

def ensure_font_installed():
    if not os.path.exists(FONT_PATH):
        os.makedirs(FONT_DIR, exist_ok=True)
        with st.spinner("Downloading Japanese font (Noto Sans JP)..."):
            try:
                response = requests.get(FONT_URL, timeout=60)
                response.raise_for_status()
                with open(FONT_PATH, "wb") as f:
                    f.write(response.content)
                st.success("Font download completed.")
            except Exception as e:
                st.warning(f"Failed to download font: {e}. Fallback to system fonts.")

ensure_font_installed()

# UIの構築
st.title("GPX2MAP")
st.write("GPXファイルをアップロードすると、PDF地図が作成されます。")

uploaded_file = st.file_uploader("Upload GPX file", type=["gpx"])

col1, col2 = st.columns(2)
with col1:
    map_type = st.selectbox(
        "マップの種類",
        options=["std", "pale", "seamlessphoto"],
        format_func=lambda x: {
            "std": "標準地図",
            "pale": "淡色地図",
            "seamlessphoto": "シームレス空中写真"
        }[x]
    )
    scale_factor = st.slider("縮尺パラメータ  (1.0のとき1:25,000縮尺、0.5のとき1:50,000縮尺)", min_value=0.1, max_value=2.0, value=1.0, step=0.1)

with col2:
    overlap = st.slider("ページ間重複(タイル数)", min_value=0.0, max_value=1.0, value=0.5, step=0.1)
    page_margin = st.slider("余白(mm)", min_value=0.0, max_value=20.0, value=5.0, step=1.0)

if uploaded_file is not None:
    gpx_data = uploaded_file.read()
    
    if st.button("Generate PDF Map", type="primary"):
        with st.spinner("Downloading tiles and generating PDF..."):
            try:
                # 一時フォルダをキャッシュディレクトリとして設定し、書き込み権限エラーを防ぐ
                with tempfile.TemporaryDirectory() as tmp_dir:
                    os.environ["GPX2MAP_CACHE_DIR"] = tmp_dir
                    
                    pdf_bytes = generate_pdf(
                        gpx_data=gpx_data,
                        map_type=map_type,
                        scale_factor=scale_factor,
                        overlap=overlap,
                        page_margin_mm=page_margin
                    )
                
                st.success("PDF generation completed successfully.")
                
                st.download_button(
                    label="Download PDF",
                    data=pdf_bytes,
                    file_name="generated_map.pdf",
                    mime="application/pdf"
                )
                
            except Exception as e:
                st.error(f"Error during PDF generation: {e}")
