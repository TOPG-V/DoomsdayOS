"""ZIM backup location, space, exclusions and requeue policy — no network or ZFS required."""
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT=Path(__file__).resolve().parents[1]/'app'/'zim_manager.py'
spec=importlib.util.spec_from_file_location('zim_manager_backup_choice',SCRIPT)
z=importlib.util.module_from_spec(spec)
spec.loader.exec_module(z)
GIB=1024**3


class BackupChoiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=str(Path(__file__).resolve().parent))
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.archive=self.root/'archive'
        self.staging=self.root/'staging'
        self.archive.mkdir();self.staging.mkdir()
        self.backup=self.root/'backup'
        self.cfg={'zim_dir':str(self.archive),'staging_dir':str(self.staging),
                  'zim_dataset':'rpool/DOOMSDAY_ZIMS',
                  'staging_dataset':'rpool/DOOMSDAY_ZIM_STAGING',
                  'backup_dir':'', 'backup_dataset':'', 'immutable_zims':True}

    def test_os_path_staging_does_not_create_before_confirmation(self):
        with mock.patch.object(z,'dataset_at',return_value='rpool/ROOT/os'):
            path,probe,ds=z._safe_backup_path(self.backup,self.cfg,os_folder=True)
        self.assertEqual(path,self.backup)
        self.assertFalse(path.exists())
        self.assertEqual(probe,self.root)
        self.assertEqual(ds,'rpool/ROOT/os')

    def test_archive_and_stage_cannot_be_backup_folder(self):
        for bad in (self.archive,self.archive/'child',self.staging,self.staging/'child',self.root):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    z._safe_backup_path(bad,self.cfg)

    def test_symlink_redirection_refused(self):
        sym=self.root/'linked'
        sym.symlink_to(self.archive,target_is_directory=True)
        with self.assertRaises(ValueError):
            z._safe_backup_path(sym/'some',self.cfg)

    def test_user_can_exclude_big_zims_until_backup_fits(self):
        chosen=z.load_preset('full')
        # Using the same pool as the primary archive with 888 GiB of free space
        # and copies=1: most backups cannot fit, but excluding 4 largest does.
        plan={'required_final':int(744.57*GIB),'required_stage':int(744.57*GIB)}
        inputs=iter(['all','13,29,33,34'])
        output=io.StringIO()
        with mock.patch.object(z,'dataset_at',return_value='rpool/ROOT/os'),\
             mock.patch.object(z,'_pool_identity',return_value='zfs:rpool'),\
             mock.patch.object(z,'zfs_copies',return_value=1),\
             contextlib.redirect_stdout(output):
            updated=z._select_backups(chosen,self.cfg,str(self.backup),'rpool/ROOT/os',
                         plan,self.archive,self.staging,888*GIB,888*GIB,
                         input_fn=lambda _:next(inputs))
        self.assertIsNotNone(updated)
        self.assertEqual(sum(x['backup'] for x in updated),35)
        self.assertIn('INSUFFICIENT BACKUP SPACE',output.getvalue())
        self.assertIn('4 ZIM(s) excluded',output.getvalue())
        self.assertFalse(any(updated[n-1]['backup'] for n in (13,29,33,34)))
        self.assertEqual(len(updated),39)

    def test_no_backups_is_valid_and_does_not_cancel_originals(self):
        chosen=z.load_preset('wikipedia')
        updated=z._select_backups(chosen,self.cfg,'','',{},self.archive,self.staging,0,0)
        self.assertEqual(len(updated),1)
        self.assertFalse(updated[0].get('backup',False))

    def test_number_validation_and_exclude(self):
        self.assertEqual(z._parse_numbers('2,5,2',5),{2,5})
        for bad in ('0','6','1,-2','1,abc','1,,3'):
            with self.assertRaises(ValueError):z._parse_numbers(bad,5)

    def test_requeue_existing_done_for_new_backup_choice(self):
        with mock.patch.object(z,'BASE',self.root):
            item=z.load_preset('wikipedia')[0]
            z.enqueue([{**item,'backup':False}])
            saved=z.read_queue();saved[0]['status']='done'
            z.atomic_json(z.queue_path(),saved)
            z.enqueue([{**item,'backup':True}])
            q=z.read_queue()
            self.assertEqual(len(q),1)
            self.assertTrue(q[0]['backup'])
            self.assertEqual(q[0]['status'],'downloaded')
            z.enqueue([{**item,'backup':False}])
            q=z.read_queue()
            self.assertFalse(q[0]['backup'])
            self.assertEqual(q[0]['backup_result'],'excluded-by-user')

    def test_backup_destination_cannot_be_system_area(self):
        with self.assertRaises(ValueError):z._safe_backup_path('/etc/doomsday-backup',self.cfg)

if __name__=='__main__':unittest.main()

# These extra tests are deliberately top-level to exercise the same public
# wizard entry points used by the installed command.
class FullBackupWizardIntegrationTests(BackupChoiceTests):
    def test_os_folder_exclusion_wizard_commits_only_after_yes(self):
        location=self.backup
        answers=iter(['2','', 'all', '13,29,33,34', 'YES'])
        seen=[]
        def send(items):seen.extend(items)
        cfg_path=self.root/'firstboot.json'
        with mock.patch.object(z,'config',return_value=dict(self.cfg)),\
             mock.patch.object(z,'CONF',cfg_path),\
             mock.patch.object(z,'DEFAULT_OS_BACKUP_DIR',location),\
             mock.patch.object(z,'dataset_at',return_value='rpool/ROOT/os'),\
             mock.patch.object(z,'_pool_identity',return_value='zfs:rpool'),\
             mock.patch.object(z,'zfs_copies',return_value=1),\
             mock.patch.object(z,'disk_free',return_value=888*GIB),\
             mock.patch.object(z,'enqueue',side_effect=send),\
             contextlib.redirect_stdout(io.StringIO()):
            result=z.queue_chosen(z.load_preset('full'),input_fn=lambda _:next(answers),all_selected=True)
        self.assertEqual(len(result),39)
        self.assertEqual(len(seen),39)
        self.assertEqual(len([x for x in seen if x['backup']]),35)
        self.assertTrue(location.is_dir())
        cfg=json.loads(cfg_path.read_text())
        self.assertEqual(cfg['backup_dir'],str(location))
        self.assertEqual(cfg['backup_dataset'],'rpool/ROOT/os')
        self.assertFalse(cfg['auto_restore_verified_zims'])

    def test_refused_final_confirmation_creates_no_folder_or_config(self):
        answers=iter(['2','', 'none', 'NO'])
        cfg_path=self.root/'firstboot.json'
        with mock.patch.object(z,'config',return_value=dict(self.cfg)),\
             mock.patch.object(z,'CONF',cfg_path),\
             mock.patch.object(z,'DEFAULT_OS_BACKUP_DIR',self.backup),\
             mock.patch.object(z,'dataset_at',return_value='rpool/ROOT/os'),\
             mock.patch.object(z,'zfs_copies',return_value=1),\
             mock.patch.object(z,'disk_free',return_value=888*GIB),\
             mock.patch.object(z,'enqueue') as send,\
             contextlib.redirect_stdout(io.StringIO()):
            result=z.queue_chosen(z.load_preset('wikipedia'),input_fn=lambda _:next(answers),all_selected=True)
        self.assertEqual(result,[])
        self.assertFalse(cfg_path.exists())
        self.assertFalse(self.backup.exists())
        send.assert_not_called()

    def test_completed_published_zim_is_not_double_counted(self):
        item=z.load_preset('wikipedia')[0]
        original=z._storage_estimate([item],self.archive,self.staging,1,1)
        self.assertGreater(original['final_bytes'],0)
        # Do not allocate 127 GB: use a mock of Path.stat for a matching archive.
        real_is_file=Path.is_file
        real_stat=Path.stat
        published=self.archive/Path(item['url']).name
        def fake_is_file(p):return True if p==published else real_is_file(p)
        def fake_stat(p,*args,**kwargs):
            if p==published:return type('Stat',(),{'st_size':item['size']})()
            return real_stat(p,*args,**kwargs)
        with mock.patch.object(Path,'is_file',fake_is_file),mock.patch.object(Path,'stat',fake_stat):
            new=z._storage_estimate([item],self.archive,self.staging,1,1)
        self.assertEqual(new['already_present'],1)
        self.assertEqual(new['new_payload'],0)
