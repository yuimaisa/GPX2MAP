import contextlib
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts.deploy_space import DEPLOY_FILES, SPACE_ID, main, stage_payload

ROOT = Path(__file__).resolve().parents[1]


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name) / 'source'
        for name in DEPLOY_FILES:
            file = self.source / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b'OTTO-test-font' if name.endswith('.otf') else f'fixture: {name}'.encode())
        for name in ('.git/config', '.github/workflows/deploy.yml', 'assets/cache.png',
                     'assets/__pycache__/x.pyc', '.venv/token', 'tile_cache/15/x.png', 'test-results/map.pdf'):
            file = self.source / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b'not for deployment')

    def test_only_allowlisted_files_are_staged_with_exact_font_and_license(self):
        target = Path(self.temporary.name) / 'payload'
        stage_payload(self.source, target)
        files = {p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file()}
        self.assertEqual(files, set(DEPLOY_FILES))
        self.assertEqual(len(files), 11)
        for name in ('assets/NotoSansJP-Regular.otf', 'assets/OFL.txt'):
            self.assertEqual((target / name).read_bytes(), (self.source / name).read_bytes())
        self.assertFalse((target / '.git').exists())
        self.assertFalse((target / '.github').exists())

    def test_real_checkout_has_complete_payload(self):
        target = Path(self.temporary.name) / 'real-payload'
        stage_payload(ROOT, target)
        self.assertGreater((target / 'assets/NotoSansJP-Regular.otf').stat().st_size, 1000000)
        self.assertIn(b'SIL OPEN FONT LICENSE', (target / 'assets/OFL.txt').read_bytes())
        for name in DEPLOY_FILES:
            self.assertEqual((target / name).read_bytes(), (ROOT / name).read_bytes())

    def test_missing_font_or_license_blocks_upload(self):
        for name in ('assets/NotoSansJP-Regular.otf', 'assets/OFL.txt'):
            with self.subTest(name=name):
                file = self.source / name
                original = file.read_bytes()
                file.unlink()
                with patch('scripts.deploy_space.upload_payload') as upload, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(['--source-dir', str(self.source)]), 1)
                    upload.assert_not_called()
                file.write_bytes(original)

    def test_font_pointer_or_empty_license_blocks_upload(self):
        font = self.source / 'assets/NotoSansJP-Regular.otf'
        font.write_bytes(b'version https://git-lfs.github.com/spec/v1\noid sha256:1234\n')
        with self.assertRaisesRegex(ValueError, 'OTF data'):
            stage_payload(self.source, Path(self.temporary.name) / 'payload')
        font.write_bytes(b'OTTO-test-font')
        (self.source / 'assets/OFL.txt').write_bytes(b'')
        with self.assertRaisesRegex(ValueError, 'assets/OFL.txt'):
            stage_payload(self.source, Path(self.temporary.name) / 'payload')

    def test_dry_run_needs_no_token_or_sdk(self):
        with patch.dict(os.environ, {'HF_TOKEN': ''}), patch('scripts.deploy_space.upload_payload') as upload, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['--source-dir', str(self.source), '--dry-run']), 0)
            upload.assert_not_called()
        self.assertIn('assets/NotoSansJP-Regular.otf', output.getvalue())
        self.assertIn('assets/OFL.txt', output.getvalue())

    def test_sdk_receives_secret_and_only_payload_for_correct_space(self):
        token = 'hf_UNIT_TEST_SECRET'
        captured = {}
        def upload_folder(**kwargs):
            folder = Path(kwargs['folder_path'])
            captured.update(kwargs)
            captured['files'] = {p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_file()}
        client = SimpleNamespace(upload_folder=upload_folder)
        factory = Mock(return_value=client)
        with patch.dict(os.environ, {'HF_TOKEN': token, 'GITHUB_SHA': 'a' * 40}), \
             patch.dict('sys.modules', {'huggingface_hub': SimpleNamespace(HfApi=factory)}), \
             contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(['--source-dir', str(self.source)]), 0)
        factory.assert_called_once_with(token=token)
        self.assertEqual(captured['repo_id'], SPACE_ID)
        self.assertEqual(captured['repo_type'], 'space')
        self.assertEqual(captured['files'], set(DEPLOY_FILES))
        self.assertEqual(captured['allow_patterns'], list(DEPLOY_FILES))
        self.assertIn('a' * 40, captured['commit_message'])
        self.assertNotIn(token, stdout.getvalue() + stderr.getvalue())
        self.assertFalse(Path(captured['folder_path']).exists())

    def test_missing_token_and_sdk_error_return_failure_without_leaking_secret(self):
        with patch.dict(os.environ, {'HF_TOKEN': ''}), patch('scripts.deploy_space.upload_payload') as upload, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--source-dir', str(self.source)]), 1)
            upload.assert_not_called()
        token = 'hf_UNIT_TEST_SECRET'
        client = SimpleNamespace(upload_folder=Mock(side_effect=RuntimeError(f'private request: {token}')))
        with patch.dict(os.environ, {'HF_TOKEN': token}), \
             patch.dict('sys.modules', {'huggingface_hub': SimpleNamespace(HfApi=Mock(return_value=client))}), \
             contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(['--source-dir', str(self.source)]), 1)
        self.assertIn('RuntimeError', stderr.getvalue())
        self.assertNotIn(token, stdout.getvalue() + stderr.getvalue())
