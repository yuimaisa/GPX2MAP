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

# セッション状態の初期化
if "generating" not in st.session_state:
    st.session_state.generating = False
if "run_generation" not in st.session_state:
    st.session_state.run_generation = False
if "pdf_bytes" not in st.session_state:
    st.session_state.pdf_bytes = None
if "success_message" not in st.session_state:
    st.session_state.success_message = None
if "error_message" not in st.session_state:
    st.session_state.error_message = None

def reset_generation_state():
    st.session_state.pdf_bytes = None
    st.session_state.success_message = None
    st.session_state.error_message = None

def start_generation():
    st.session_state.generating = True
    st.session_state.run_generation = True
    st.session_state.pdf_bytes = None
    st.session_state.success_message = None
    st.session_state.error_message = None

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
st.write("利用方法: <https://github.com/yuimaisa/GPX2MAP>")

uploaded_file = st.file_uploader(
    "Upload GPX file", 
    type=["gpx"],
    key="gpx_uploader",
    on_change=reset_generation_state,
    disabled=st.session_state.generating
)

col1, col2 = st.columns(2)
with col1:
    map_type = st.selectbox(
        "マップの種類",
        options=["std", "pale", "seamlessphoto"],
        format_func=lambda x: {
            "std": "標準地図",
            "pale": "淡色地図",
            "seamlessphoto": "シームレス空中写真"
        }[x],
        key="map_type_select",
        on_change=reset_generation_state,
        disabled=st.session_state.generating
    )
    scale_factor = st.slider(
        "縮尺パラメータ  (1.0のとき1:25,000縮尺、0.5のとき1:50,000縮尺)", 
        min_value=0.1, 
        max_value=2.0, 
        value=1.0, 
        step=0.1,
        key="scale_slider",
        on_change=reset_generation_state,
        disabled=st.session_state.generating
    )

with col2:
    overlap = st.slider(
        "ページ間重複(タイル数)", 
        min_value=0.0, 
        max_value=1.0, 
        value=0.5, 
        step=0.1,
        key="overlap_slider",
        on_change=reset_generation_state,
        disabled=st.session_state.generating
    )
    page_margin = st.slider(
        "余白(mm)", 
        min_value=0.0, 
        max_value=20.0, 
        value=5.0, 
        step=1.0,
        key="margin_slider",
        on_change=reset_generation_state,
        disabled=st.session_state.generating
    )

if uploaded_file is not None:
    gpx_data = uploaded_file.read()
    
    st.button(
        "PDFを生成(時間がかかります！)", 
        type="primary",
        key="generate_button",
        on_click=start_generation,
        disabled=st.session_state.generating
    )
    
    if st.session_state.run_generation:
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
                
                st.session_state.pdf_bytes = pdf_bytes
                st.session_state.success_message = "PDF generation completed successfully."
                st.session_state.error_message = None
            except Exception as e:
                st.session_state.error_message = f"Error during PDF generation: {e}"
                st.session_state.pdf_bytes = None
                st.session_state.success_message = None
            finally:
                st.session_state.generating = False
                st.session_state.run_generation = False
                st.rerun()

    # メッセージとダウンロードボタンの表示
    if st.session_state.success_message:
        st.success(st.session_state.success_message)
    if st.session_state.error_message:
        st.error(st.session_state.error_message)
        
    if st.session_state.pdf_bytes is not None:
        st.download_button(
            label="Download PDF",
            data=st.session_state.pdf_bytes,
            file_name="generated_map.pdf",
            mime="application/pdf"
        )


# フッター
st.divider()
st.caption("Developed by [yuimaisa](https://zetekton.com/ja/members/yuishinwada)")