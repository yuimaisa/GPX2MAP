import hashlib
import os

import streamlit as st

from download_component import DownloadArtifact, show_download
from gpx2map import build_layout_preview, generate_output
from map_plan import DEFAULT_LIMITS, GPX2MapError, inspect_gpx, plan_pdf
from tile_store import TileDownloadError
from magnetic_north import MagneticNorthError, SPACING_OPTIONS_M

st.set_page_config(page_title='GPX2MAP | 印刷用の地図を作成', layout='centered')

RESULT_DEFAULTS = {
    'download_artifact': None,
    'pdf_filename': 'generated_map.pdf',
    'pdf_summary': None,
    'error_message': None,
    'error_details': None,
}
STATE_DEFAULTS = {
    'generating': False, 'run_generation': False,
    'document': None, 'document_digest': None,
    'generation_plan': None, 'plan_key': None,
    **RESULT_DEFAULTS,
}
for key, value in STATE_DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = value


def reset_generation_state():
    clear_result()
    st.session_state.generation_plan = None
    st.session_state.plan_key = None


def clear_result():
    if st.session_state.download_artifact is not None:
        st.session_state.download_artifact.close()
    for key, value in RESULT_DEFAULTS.items():
        st.session_state[key] = value


def reset_upload_state():
    reset_generation_state()
    st.session_state.document = None
    st.session_state.document_digest = None


def start_generation():
    clear_result()
    st.session_state.generating = True
    st.session_state.run_generation = True


def show_result(artifact):
    # Keep ownership in session_state, so releasing a session also releases files.
    if artifact.is_zip:
        st.success(f'容量上限に合わせて{len(artifact.parts)}個のPDFに自動分割しました。ZIPでまとめて保存できます。')
        st.caption('ZIPを展開し、ファイル名の連番順に印刷してください。ページ番号は全体の通し番号です。')
    else:
        st.success('必要な地図タイルを確認し、PDFを作成しました。下のボタンから保存してください。')
    st.write(st.session_state.pdf_summary)
    show_download(artifact)
    st.caption('印刷するときは「A4・横向き・倍率100%」を指定してください。')


MAP_LABELS = {'std': '標準地図', 'pale': '淡色地図', 'seamlessphoto': '空中写真'}
MAP_DESCRIPTIONS = {
    'std': '地名や道路、等高線を確認するのに適しています。',
    'pale': '背景の色が薄く、重ねたルートが見やすい地図です。',
    'seamlessphoto': '写真で周辺の様子を確認できます。',
}
SCALE_OPTIONS = {'1:25,000（標準）': 25000, '1:50,000（広い範囲）': 50000,
                 '1:100,000（さらに広い範囲）': 100000, 'その他の縮尺': None}
OVERLAP_OPTIONS = {'なし': 0.0, '少なめ': 0.25, '標準': 0.5, '多め': 1.0}

st.title('GPX2MAP')
st.write('GPXのルートを国土地理院の地図に重ねて、A4横のPDFにします。')
st.caption('GPXを選ぶ → 印刷設定を決める → 地図を確認 → PDFを作成して保存')

document, gpx_data = None, None
with st.container(border=True):
    st.subheader('1. GPXファイルを選ぶ')
    uploaded_file = st.file_uploader(
        'GPXファイル', type=['gpx'], key='gpx_uploader',
        help='GPSや登山アプリから書き出した .gpx ファイル（5MiB・50,000点まで）を選んでください。',
        on_change=reset_upload_state, disabled=st.session_state.generating,
    )
    if uploaded_file is None:
        st.info('最初にGPXファイルを選んでください。印刷設定は先に変更できます。')
    else:
        st.caption(f'選択中：{uploaded_file.name}（{uploaded_file.size / 1024:,.1f} KiB）')
        try:
            if uploaded_file.size > DEFAULT_LIMITS.input_bytes:
                raise GPX2MapError('GPXの容量は5MiB以下にしてください。対象区間を分けてください。')
            gpx_data = uploaded_file.getvalue()
            digest = hashlib.sha256(gpx_data).hexdigest()
            if st.session_state.document_digest != digest:
                st.session_state.document = inspect_gpx(gpx_data)
                st.session_state.document_digest = digest
            document = st.session_state.document
            a, b, c = st.columns(3)
            a.metric('座標点数', f'{document.point_count:,}点')
            b.metric('概算距離', f'{document.distance_m / 1000:,.1f}km')
            c.metric('描画する区間・点', f'{len(document.segments):,}件')
            st.caption(f'トラック {document.track_count}件 / トラック内の区間 {document.track_segment_count}件 / '
                       f'ルート {document.route_count}件 / ウェイポイント {document.waypoint_count}件')
            if document.waypoint_count:
                st.caption('ウェイポイントは独立した点として表示します。点同士は線でつなぎません。')
        except GPX2MapError as error:
            st.error(str(error))

with st.container(border=True):
    st.subheader('2. 印刷設定を決める')
    map_column, scale_column = st.columns(2)
    with map_column:
        map_type = st.selectbox('地図の種類', list(MAP_LABELS), format_func=MAP_LABELS.get,
                                key='map_type_select', on_change=reset_generation_state,
                                disabled=st.session_state.generating)
        st.caption(MAP_DESCRIPTIONS[map_type])
    with scale_column:
        scale_choice = st.selectbox(
            '縮尺', list(SCALE_OPTIONS), key='scale_select', on_change=reset_generation_state,
            help='1:25,000は詳しく、1:50,000はその2倍の距離を同じ長さで印刷できます。',
            disabled=st.session_state.generating,
        )
        scale_denominator = SCALE_OPTIONS[scale_choice]
        if scale_denominator is None:
            scale_denominator = st.number_input(
                '縮尺を指定（1:入力した数値）', min_value=12500, max_value=250000, value=25000, step=2500,
                key='custom_scale_denominator', on_change=reset_generation_state,
                disabled=st.session_state.generating,
            )
        st.caption(f'地図上の1cmが、実際の{scale_denominator / 100:,.0f}mに相当します。')
    scale_factor = 25000.0 / scale_denominator
    with st.expander('詳細設定（余白・ページの重なり・磁北線）'):
        page_margin = st.slider(
            '印刷用の余白（mm）', min_value=0.0, max_value=20.0, value=5.0, step=1.0,
            help='四辺に設ける余白です。コンビニなどで印刷するときは5mmを目安にしてください。',
            key='margin_slider', on_change=reset_generation_state, disabled=st.session_state.generating,
        )
        overlap_choice = st.selectbox(
            'ページの重なり', list(OVERLAP_OPTIONS), index=2, key='overlap_select',
            help='隣のページと同じ範囲を載せます。迷った場合は「標準」を選んでください。',
            on_change=reset_generation_state, disabled=st.session_state.generating,
        )
        draw_magnetic_north = st.checkbox(
            '磁北線を表示する', value=True, key='draw_magnetic_north_check',
            on_change=reset_generation_state, disabled=st.session_state.generating,
        )
        magnetic_spacing_m = 1000
        if draw_magnetic_north:
            magnetic_spacing_m = st.selectbox(
                '磁北線の間隔', SPACING_OPTIONS_M, index=1,
                format_func=lambda value: {500: '500m', 1000: '1km', 2000: '2km'}[value],
                key='magnetic_spacing_select', on_change=reset_generation_state,
                disabled=st.session_state.generating,
            )
    overlap = OVERLAP_OPTIONS[overlap_choice]

plan, plan_error = None, None
if document is not None:
    try:
        plan_key = (document.digest, map_type, scale_factor, overlap, page_margin, draw_magnetic_north, magnetic_spacing_m)
        if st.session_state.plan_key != plan_key:
            st.session_state.generation_plan = plan_pdf(
                gpx_data, map_type, scale_factor, overlap, page_margin, draw_magnetic_north,
                magnetic_spacing_m=magnetic_spacing_m, document=document, limits=DEFAULT_LIMITS,
            )
            st.session_state.plan_key = plan_key
        plan = st.session_state.generation_plan
    except GPX2MapError as error:
        plan_error = str(error)

with st.container(border=True):
    st.subheader('3. 地図の配置を確認する')
    st.write(f'**{MAP_LABELS[map_type]} ／ 1:{scale_denominator:,} ／ A4横**')
    st.caption(f'四辺の余白：{page_margin:g}mm　・　ページの重なり：{overlap_choice}')
    if plan_error:
        st.error(plan_error)
    elif plan is not None:
        a, b = st.columns(2)
        a.metric('予定ページ数', f'{plan.page_count}ページ')
        b.metric('概算タイル数', f'{plan.tile_count}枚')
        with build_layout_preview(plan) as preview:
            st.image(preview, caption='青い枠が1ページ、赤い線・点がGPXです。番号はPDFのページ順です。', width='stretch')
        with st.expander('生成量と対応範囲'):
            st.write(f'処理用メモリの見込み：{plan.estimated_memory_bytes / 1024**2:,.0f}MiB '
                     f'（設定上限 {DEFAULT_LIMITS.memory_bytes / 1024**2:,.0f}MiB）')
            st.caption('実行環境全体の使用量とは異なる推算です。同じサーバーでのPDF作成は順番に処理します。')
            st.write(f'最大{DEFAULT_LIMITS.pages}ページ / {DEFAULT_LIMITS.tiles}タイル / PDF 1ファイルあたり{DEFAULT_LIMITS.pdf_bytes / 1024**2:g}MiB')
            st.caption('PDF容量の上限を超える場合は、ページ単位で自動分割し、ZIPでまとめて保存できます。')
            st.caption('縮尺は対象範囲の中央緯度を基準にします。範囲内の投影による変動が1%を超える場合は作成しません。')
    else:
        st.caption('有効なGPXを選ぶと、ページ数と地図の配置を確認できます。')

with st.container(border=True):
    st.subheader('4. PDFを作成して保存')
    st.caption('地図の取得には時間がかかる場合があります。作成中は設定を変更できません。')
    st.button('PDFを作成', type='primary', key='generate_button', on_click=start_generation,
              disabled=plan is None or st.session_state.generating, width='stretch')
    if st.session_state.run_generation:
        if plan is None:
            st.session_state.generating = False
            st.session_state.run_generation = False
            st.session_state.error_message = '生成内容を確認してから、もう一度お試しください。'
        else:
            status = st.empty()
            tiles_progress = st.progress(0.0, text=f'地図タイルの準備：0 / {plan.tile_count}')
            pages_progress = st.progress(0.0, text=f'ページの作成：0 / {plan.page_count}')
            def on_progress(progress):
                tiles_progress.progress(progress.tiles / progress.total_tiles,
                                         text=f'地図タイルの準備：{progress.tiles} / {progress.total_tiles}')
                completed = progress.page if progress.stage in ('page', 'pdf', 'complete') else max(0, progress.page - 1)
                pages_progress.progress(completed / progress.pages, text=f'ページの作成：{completed} / {progress.pages}')
                messages = {
                    'waiting': '作成の順番を待っています。',
                    'tiles': f'ページ {progress.page} / {progress.pages} の地図を取得しています。',
                    'page': f'ページ {progress.page} / {progress.pages} を作成しました。',
                    'pdf': 'PDFを出力しています。', 'complete': 'PDFの作成が完了しました。',
                }
                status.info(messages[progress.stage])
            try:
                name = os.path.splitext(os.path.basename(uploaded_file.name))[0]
                with generate_output(
                    gpx_data, map_type, scale_factor, overlap, page_margin, draw_magnetic_north,
                    magnetic_spacing_m=magnetic_spacing_m,
                    progress_callback=on_progress, plan=plan, limits=plan.limits,
                ) as output:
                    st.session_state.pdf_filename = f'{name}_map{".zip" if output.is_zip else ".pdf"}'
                    st.session_state.download_artifact = DownloadArtifact(output, st.session_state.pdf_filename)
                st.session_state.pdf_summary = f'{plan.page_count}ページ / {MAP_LABELS[map_type]} / 1:{scale_denominator:,} / A4横 / 余白{page_margin:g}mm'
            except Exception as error:
                clear_result()
                st.session_state.error_details = str(error)
                if isinstance(error, MagneticNorthError):
                    st.session_state.error_message = '磁気偏角を計算できなかったため、PDFの生成に失敗しました。エラーの詳細を確認してください。'
                elif isinstance(error, TileDownloadError):
                    st.session_state.error_message = '地図を取得できなかったため、PDFは作成していません。通信状況とエラーの詳細を確認して再試行してください。'
                elif isinstance(error, GPX2MapError):
                    st.session_state.error_message = str(error)
                    st.session_state.error_details = None
                else:
                    st.session_state.error_message = 'PDFを作成できませんでした。ファイルや設定、エラーの詳細を確認して再試行してください。'
            finally:
                st.session_state.generating = False
                st.session_state.run_generation = False
                st.rerun()
    if st.session_state.error_message is not None:
        st.error(st.session_state.error_message)
        if st.session_state.error_details:
            with st.expander('エラーの詳細'):
                st.code(st.session_state.error_details, language=None)
    if st.session_state.download_artifact is not None:
        show_result(st.session_state.download_artifact)

st.divider()
st.markdown('[詳しい使い方・ソースコード](https://github.com/yuimaisa/GPX2MAP)')
st.caption('地図の出典：国土地理院 / 作成者：[yuimaisa](https://zetekton.com/ja/members/yuishinwada)')
