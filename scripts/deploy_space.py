"""Upload only tested runtime files to the existing Hugging Face Space."""
import argparse
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

SPACE_ID = 'yuishin/gpx2map'
DEPLOY_FILES = (
    'app.py',
    'gpx2map.py',
    'map_plan.py',
    'magnetic_north.py',
    'tile_store.py',
    'pdf_output.py',
    'download_component.py',
    'requirements.txt',
    'README.md',
    'LICENSE',
    '.streamlit/config.toml',
    'assets/NotoSansJP-Regular.otf',
    'assets/OFL.txt',
    'assets/README.md',
)


class PayloadError(ValueError):
    pass


def stage_payload(source_dir, destination):
    """Copy an explicit allowlist; never copy a repository or assets recursively."""
    source_dir, destination = Path(source_dir).resolve(), Path(destination)
    for relative in DEPLOY_FILES:
        source = source_dir / relative
        if (not source.is_file() or source.is_symlink()
                or not source.resolve().is_relative_to(source_dir) or source.stat().st_size == 0):
            raise PayloadError(f'Missing, empty or unsafe deployment file: {relative}')
    with (source_dir / 'assets/NotoSansJP-Regular.otf').open('rb') as font:
        if font.read(4) != b'OTTO':
            raise PayloadError('Japanese font must contain OTF data, not a Git LFS pointer.')
    for relative in DEPLOY_FILES:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_dir / relative, target)


def upload_payload(folder, token, source_sha):
    # Keep the SDK out of application and offline-test dependencies.
    from huggingface_hub import HfApi

    message = 'Deploy tested GPX2MAP source'
    if re.fullmatch(r'[0-9a-fA-F]{40}', source_sha):
        message += f' ({source_sha})'
    return HfApi(token=token).upload_folder(
        repo_id=SPACE_ID,
        repo_type='space',
        folder_path=str(folder),
        allow_patterns=list(DEPLOY_FILES),
        commit_message=message,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--dry-run', action='store_true', help='Validate and list files without a token or network access')
    args = parser.parse_args(argv)
    try:
        with tempfile.TemporaryDirectory(prefix='gpx2map-deploy-') as temporary:
            stage_payload(args.source_dir, temporary)
            if args.dry_run:
                print(f'Deployment payload for {SPACE_ID}: {len(DEPLOY_FILES)} files')
                for relative in DEPLOY_FILES:
                    print(relative)
                return 0
            token = os.environ.get('HF_TOKEN')
            if not token or not token.strip():
                print('HF_TOKEN is required. Configure the existing GitHub Actions secret.', file=sys.stderr)
                return 1
            upload_payload(temporary, token, os.environ.get('GITHUB_SHA', ''))
        print(f'Uploaded {len(DEPLOY_FILES)} tested files to Space {SPACE_ID}.')
        return 0
    except PayloadError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        # SDK exceptions may include server/request details; never echo them or a traceback.
        print(f'Space upload failed ({type(error).__name__}). Check network access and HF_TOKEN write permission.',
              file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
