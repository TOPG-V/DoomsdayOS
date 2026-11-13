import contextlib,io,json,sys,tempfile,unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0,str((Path(__file__).resolve().parents[1]/'scripts') if (Path(__file__).resolve().parents[1]/'scripts').is_dir() else Path(__file__).resolve().parents[1]))
import portable_zim_protector as z

class TestPortable(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.zims=self.base/'zims';self.zims.mkdir()
        self.file=self.zims/'valid.zim';self.file.write_bytes(b'ZIMtest123')
        self.entry={'valid.zim':{'sha256':z.digest(self.file),'bytes':self.file.stat().st_size}}
        self.manifest=self.base/'manifest.json'
        self.manifest.write_text(json.dumps(self.entry))
    def test_valid_and_corrupt(self):
        self.assertTrue(z.verify(self.zims,self.entry,all_files=True)['ok'])
        self.file.write_bytes(b'ZIMtest124')
        self.assertEqual(z.verify(self.zims,self.entry)['results'][0]['status'],'sha256-mismatch')
    def test_no_untracked(self):
        (self.zims/'other.zim').write_bytes(b'foo')
        self.assertFalse(z.verify(self.zims,self.entry,all_files=True)['ok'])
    def test_bad_manifest_path(self):
        self.manifest.write_text(json.dumps({'../escape.zim':self.entry['valid.zim']}))
        with self.assertRaises(ValueError):z.manifest_entries(str(self.manifest))
    def test_bad_manifest_hash(self):
        self.manifest.write_text(json.dumps({'valid.zim':{'sha256':'abc'}}))
        with self.assertRaises(ValueError):z.manifest_entries(str(self.manifest))
    def test_symlink_escape_refused(self):
        (self.base/'link').symlink_to(self.zims,target_is_directory=True)
        with self.assertRaises(ValueError):z.choose_archive(str(self.base/'link'))
    def test_baseline_no_overwrite_and_not_authoritative(self):
        target=self.base/'baseline.json'
        z.baseline(self.zims,str(target))
        self.assertEqual(json.loads(target.read_text())['trust'],'LOCAL_BASELINE_NOT_PUBLISHER_AUTHENTICATED')
        with self.assertRaises(ValueError):z.baseline(self.zims,str(target))
        out=io.StringIO();err=io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code=z.main(['protect','--archive',str(self.zims),'--manifest',str(target),'--yes'])
        self.assertEqual(code,2)
        self.assertIn('unauthenticated',err.getvalue())
    def test_protect_requires_yes(self):
        with self.assertRaises(ValueError):z.protect_zfs(self.zims,self.entry)
    def test_protect_zfs_rejects_root_and_nonarchive_names(self):
        with mock.patch.object(z,'status',return_value={'dataset':'rpool/ROOT/ubuntu','readonly':False}):
            with self.assertRaises(RuntimeError):z.protect_zfs(self.zims,self.entry,confirmed=True)
        with mock.patch.object(z,'status',return_value={'dataset':'tank/my-root','readonly':False}),mock.patch.object(z,'run',return_value=mock.Mock(stdout='tank/other',returncode=0)):
            with self.assertRaises(RuntimeError):z.protect_zfs(self.zims,self.entry,confirmed=True)
    def test_protect_happy_mocked(self):
        calls=[]
        def fake_run(*args):
            calls.append(args)
            if args[:2]==('findmnt','-n'):
                return mock.Mock(returncode=0,stdout='rpool/ROOT/ubuntu\n')
            return mock.Mock(returncode=0,stdout='')
        with mock.patch.object(z,'status',side_effect=[{'dataset':'tank/zims','readonly':False},{'dataset':'tank/zims','readonly':True}]),mock.patch.object(z,'run',side_effect=fake_run):
            got=z.protect_zfs(self.zims,self.entry,confirmed=True)
            self.assertTrue(got['readonly'])
            self.assertIn(('zfs','set','readonly=on','tank/zims'),calls)
            self.assertFalse(any('readonly=off' in q for call in calls for q in call))
    def test_multi_pool_manifest_filter(self):
        data={**self.entry,'backup.zim':{'sha256':self.entry['valid.zim']['sha256'],
             'bytes':10,'dataset':'otherpool/zims'}}
        data['valid.zim']['dataset']='tank/zims'
        selected=z.filter_dataset(data,'tank/zims')
        self.assertEqual(list(selected),['valid.zim'])
        self.assertTrue(z.verify(self.zims,selected,all_files=True)['ok'])
        with self.assertRaises(ValueError):z.filter_dataset(data,'unmounted/zims')

    def test_missing_archive(self):
        self.file.unlink()
        self.assertEqual(z.verify(self.zims,self.entry)['results'][0]['status'],'missing')

if __name__=='__main__':unittest.main()