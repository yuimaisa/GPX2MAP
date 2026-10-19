"""Serve downloads in bounded blocks through Streamlit's component file route.

st.download_button reads even file-like data completely into server RAM. This
component fetches at most 8 MiB per request and joins the download in the browser.
Only completed results are published, under an unpredictable per-result path.
"""
from pathlib import Path
import tempfile

from streamlit.components.v1 import declare_component

_ROOT = tempfile.TemporaryDirectory(prefix='gpx2map-downloads-')
BLOCK_BYTES = 8 * 1024**2

_HTML = """<!doctype html><html lang="ja"><meta charset="utf-8">
<style>
body{margin:0;font-family:system-ui,sans-serif;color:#31333f}
button{width:100%;padding:10px;border:1px solid #ccc;border-radius:8px;background:white;
color:inherit;font:inherit;cursor:pointer}button:hover{border-color:#ff4b4b;color:#ff4b4b}
button:disabled{opacity:.6;cursor:wait}p{font-size:13px;margin:6px 0}
</style><button id="download" disabled>ダウンロードを準備しています</button><p id="status" role="status"></p>
<script>
let args;
const button=document.getElementById('download'), status=document.getElementById('status');
function send(type, extra={}) { parent.postMessage({isStreamlitMessage:true,type,...extra},'*'); }
window.addEventListener('message', event => {
  if(event.source!==parent || event.data.type!=='streamlit:render') return;
  args=event.data.args; button.textContent=args.label; button.disabled=false;
});
button.addEventListener('click', async () => {
  button.disabled=true; status.textContent='ダウンロードを準備しています…';
  let url;
  try {
    const blobs=[];
    for(const file of args.files) {
      const response=await fetch(file,{cache:'no-store'});
      if(!response.ok) throw new Error('download failed');
      blobs.push(await response.blob());
    }
    url=URL.createObjectURL(new Blob(blobs,{type:args.mime}));
    const anchor=document.createElement('a'); anchor.href=url; anchor.download=args.filename;
    document.body.appendChild(anchor); anchor.click(); anchor.remove();
    status.textContent='保存を開始しました。';
    // Allow mobile browsers time to start consuming the Blob.
    const downloadUrl=url;
    setTimeout(()=>URL.revokeObjectURL(downloadUrl),60000); url=null;
  } catch(error) {
    if(url) URL.revokeObjectURL(url);
    status.textContent='保存できませんでした。もう一度お試しください。';
  } finally { button.disabled=false; }
});
send('streamlit:componentReady',{apiVersion:1});
send('streamlit:setFrameHeight',{height:90});
</script></html>"""
(Path(_ROOT.name) / 'index.html').write_text(_HTML, encoding='utf-8')


class DownloadArtifact:
    def __init__(self, output, filename):
        self._temporary = tempfile.TemporaryDirectory(dir=_ROOT.name)
        self.directory = Path(self._temporary.name)
        self.filename = filename
        self.is_zip = output.is_zip
        self.parts = tuple(output.parts)
        self.blocks = []
        try:
            with output.path.open('rb') as source:
                while block := source.read(BLOCK_BYTES):
                    target = self.directory / f'{len(self.blocks):04d}.bin'
                    target.write_bytes(block)
                    self.blocks.append(target)
        except BaseException:
            self.close()
            raise

    def close(self):
        self._temporary.cleanup()


def show_download(artifact):
    # Register on each script run, including after a session reconnect.
    component = declare_component('files', path=_ROOT.name)
    component(files=[f'{block.parent.name}/{block.name}' for block in artifact.blocks],
              filename=artifact.filename, mime='application/zip' if artifact.is_zip else 'application/pdf',
              label='ZIPをダウンロード' if artifact.is_zip else 'PDFをダウンロード',
              key='download_output', default=None)
