"""Standard-library tests for safe code relocation and manifest verification."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, BASE / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

DEPLOY = load('deploy_atomic')
INTEGRITY = load('runtime_integrity')
WAYLAND = load('wayland_doctor')


def fake_release(path, title='first'):
    for name in DEPLOY.ESSENTIAL:
        file = path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(title + ' ' + name)
    (path / 'app' / 'notes.py').write_text(title)
    (path / '.git').mkdir()
    (path / '.git' / 'config').write_text('do not copy')


class InstallSafety(unittest.TestCase):
    def test_verified_self_copy_and_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d);src = root / 'downloaded';src.mkdir();fake_release(src)
            target = root / 'installed'
            token = DEPLOY.stage(src, target)
            self.assertEqual((target / 'app' / 'notes.py').read_text(), 'first')
            self.assertFalse((target / '.git').exists())
            DEPLOY.complete(target, token)
            self.assertFalse((target / DEPLOY.MARKER).exists())
            self.assertTrue(src.exists(), 'Never delete user-provided installer folder')
            self.assertFalse(any(root.glob('.doomsday-rollback-*')))
            self.assertFalse(any(root.glob('.doomsday-stage-*')))

    def test_update_preserves_optional_offline_maplibre_assets(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); old=root/'installed';old.mkdir();fake_release(old)
            vendor=old/'app'/'web'/'vendor';vendor.mkdir(parents=True)
            (vendor/'maplibre-gl.js').write_text('offline maps')
            src=root/'updated';src.mkdir();fake_release(src,'new')
            token=DEPLOY.stage(src,old)
            self.assertEqual((old/'app'/'web'/'vendor'/'maplibre-gl.js').read_text(), 'offline maps')
            DEPLOY.complete(old,token)

    def test_failed_update_rolls_back_existing_code(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d);old = root / 'installed';old.mkdir();fake_release(old)
            src = root / 'new';src.mkdir();fake_release(src, 'updated')
            token = DEPLOY.stage(src, old)
            self.assertEqual((old / 'app' / 'notes.py').read_text(), 'updated')
            DEPLOY.rollback(old, token)
            self.assertEqual((old / 'app' / 'notes.py').read_text(), 'first')
            self.assertFalse(any(root.glob('.doomsday-failed-*')))
            self.assertTrue(src.exists())

    def test_reject_corrupt_source_or_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d);src = root / 'source';src.mkdir();fake_release(src)
            (src / 'app' / 'link').symlink_to('/etc/passwd')
            with self.assertRaises(ValueError):DEPLOY.stage(src, root / 'installed')
            self.assertFalse((root / 'installed').exists())

    def test_source_missing_is_not_installed(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);src=root/'source';src.mkdir();(src/'install.sh').write_text('hello')
            with self.assertRaises(ValueError):DEPLOY.stage(src,root/'installed')

    def test_wrong_token_cannot_delete_or_revert(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);src=root/'source';src.mkdir();fake_release(src)
            target=root/'installed';token=DEPLOY.stage(src,target)
            with self.assertRaises(ValueError):DEPLOY.complete(target,'0'*24)
            self.assertTrue(target.exists())
            DEPLOY.complete(target,token)

    def test_integrity_manifest_detects_edits_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);src=root/'installed';src.mkdir();fake_release(src)
            manifest=root/'state'/'manifest.json'
            result=INTEGRITY.create_snapshot(src,manifest)
            self.assertGreater(result['tracked_files'],3)
            self.assertTrue(INTEGRITY.verify(src,manifest)['ok'])
            (src/'app'/'notes.py').write_text('tampered')
            self.assertEqual(INTEGRITY.verify(src,manifest)['changed'],['app/notes.py'])
            (src/'app'/'notes.py').unlink()
            self.assertEqual(INTEGRITY.verify(src,manifest)['missing'],['app/notes.py'])

    def test_integrity_manifest_never_repairs_or_deletes(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);src=root/'installed';src.mkdir();fake_release(src)
            manifest=root/'state'/'manifest.json'
            INTEGRITY.create_snapshot(src,manifest)
            (src/'app'/'new-file.py').write_text('user modification')
            result=INTEGRITY.verify(src,manifest)
            self.assertFalse(result['ok'])
            self.assertTrue((src/'app'/'new-file.py').exists())

    def test_wayland_diagnostics_do_not_modify_session(self):
        calls=[]
        def runner(argv):
            calls.append(argv)
            return 0,'active'
        result=WAYLAND.status(env={'XDG_SESSION_TYPE':'wayland','WAYLAND_DISPLAY':'wayland-0','XDG_RUNTIME_DIR':'/run/user/1000'},which=lambda x:'/usr/bin/'+x, runner=runner)
        self.assertEqual(result['session_type'],'wayland')
        self.assertNotIn('repair',result)
        self.assertFalse(any('restart' in str(argv) for argv in calls))


if __name__ == '__main__':unittest.main()