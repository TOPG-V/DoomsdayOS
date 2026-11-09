import importlib.util, os, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('backup_policy', str(Path(__file__).resolve().parents[1]/'app/backup_policy.py'))
bp=importlib.util.module_from_spec(spec);spec.loader.exec_module(bp)
class BackupPolicyTests(unittest.TestCase):
    def test_due_after_14_days(self):
        with tempfile.TemporaryDirectory() as d, patch.object(bp,'STATE',Path(d)), patch.object(bp,'external_ready',return_value=True):
            with patch.object(bp,'settings',return_value=dict(bp.DEFAULTS)):
                bp.mark('local',now=100)
                with patch.dict(os.environ,{'DOOMSDAY_IGNORE_LOAD':'1'}):
                    self.assertFalse(bp.due('local',now=100+13*86400)[0])
                    self.assertTrue(bp.due('local',now=100+14*86400)[0])
    def test_missing_external_does_not_mark_success(self):
        with patch.object(bp,'external_ready',return_value=False):
            self.assertFalse(bp.due('external',now=100)[0])
    def test_throttler_is_bounded(self):
        import io
        sp=importlib.util.spec_from_file_location('throttle', str(Path(__file__).resolve().parents[1]/'app/throttle_stream.py'))
        m=importlib.util.module_from_spec(sp);sp.loader.exec_module(m)
        a=io.BytesIO(b'hello'*1000);b=io.BytesIO();m.pipe(a,b,0)
        self.assertEqual(b.getvalue(),b'hello'*1000)
if __name__=='__main__':unittest.main()
