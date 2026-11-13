import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import smart_backup as sb
import portable_os as po

class SmartBackupTests(unittest.TestCase):
    def test_reject_non_absolute_sources_and_invalid_toggles(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(sb,'POLICY',Path(tmp)/'cfg.json'):
            p=Path(tmp)/'cfg.json'
            p.write_text(json.dumps({'sources':['relative/foo']}))
            with self.assertRaisesRegex(ValueError,'absolute'):sb.settings()
            p.write_text(json.dumps({'auto_restore_missing':'yes'}))
            with self.assertRaisesRegex(ValueError,'boolean'):sb.settings()

    def test_source_inventory_skips_symlink_to_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'data';root.mkdir();(root/'alpha.txt').write_bytes(b'alpha')
            (root/'link').symlink_to('/etc/passwd')
            result,size,_=sb._source_files([str(root)],100,1024)
            self.assertEqual(size,5)
            self.assertEqual([r['relative'] for r in result],['0/alpha.txt'])

    def test_independence_and_mount_guid_and_free_space(self):
        d={'key':'approved','dataset':'tank/safe-backup','mount':'/tank/safe-backup','expected_guid':'777'}
        policy=sb.DEFAULTS
        with patch.object(sb,'_enrolled',return_value=[d]),patch.object(sb,'_mount_identity',return_value=True), \
             patch.object(sb,'_guid',return_value='777'),patch.object(sb,'_zpool_members',side_effect=[{'/dev/sda'},{'/dev/sda'}]), \
             patch.object(sb,'_cmd_ok',return_value='off'):
            out=sb.destinations(root='rpool',policy=policy)
            self.assertFalse(out[0]['eligible']);self.assertIn('physical disk',out[0]['reason'])
        with patch.object(sb,'_enrolled',return_value=[d]),patch.object(sb,'_mount_identity',return_value=True), \
             patch.object(sb,'_guid',return_value='999'),patch.object(sb,'_zpool_members',return_value={'/dev/sda'}):
            out=sb.destinations(root='rpool',policy=policy)
            self.assertFalse(out[0]['eligible']);self.assertIn('GUID changed',out[0]['reason'])
        with patch.object(sb,'_enrolled',return_value=[d]),patch.object(sb,'_mount_identity',return_value=True), \
             patch.object(sb,'_guid',return_value='777'),patch.object(sb,'_zpool_members',side_effect=[{'/dev/sda'},{'/dev/sdb'}]), \
             patch.object(sb,'_cmd_ok',return_value='off'), patch.object(sb,'_run',return_value=str(100*sb.GIB)):
            out=sb.destinations(root='rpool',policy=policy)
            self.assertTrue(out[0]['eligible']);self.assertGreater(out[0]['capacity_bytes'],80*sb.GIB)

    def test_exact_dataset_required_and_fstab_advice(self):
        with patch.object(sb,'_run',return_value='tank/other zfs'):
            self.assertFalse(sb._mount_identity('tank/safe','/tank/safe'))
        f=po._fstab_audit('/dev/sda2 /home ext4 defaults 0 2\nUUID=123 /boot ext4 defaults 0 2')
        self.assertEqual(len(f['device_path_risks']),1)
        self.assertEqual(len(f['stable_mounts']),1)

    def test_verified_generation_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'2026';folder.mkdir()
            (folder/sb.MARKER).write_text('format 1')
            (folder/'files'/'0').mkdir(parents=True)
            (folder/'files'/'0'/'a.txt').write_bytes(b'good')
            sb._write_json(folder/sb.MANIFEST,{'format':1,'records':[{'relative':'0/a.txt','original':'/src/a.txt',
                'size':4,'sha256':hashlib.sha256(b'good').hexdigest()}]})
            self.assertTrue(sb._verify_generation(folder)['valid'])
            (folder/'files'/'0'/'a.txt').write_bytes(b'evil')
            self.assertFalse(sb._verify_generation(folder)['valid'])

    def test_restore_requires_confirmation_allowlist_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'src';source.mkdir()
            target=source/'a.txt'
            gen=Path(tmp)/'gen';gen.mkdir()
            (gen/sb.MARKER).write_text('format 1')
            (gen/'files'/'0').mkdir(parents=True)
            (gen/'files'/'0'/'a.txt').write_bytes(b'expected')
            sb._write_json(gen/sb.MANIFEST,{'format':1,'records':[{'original':str(target),'relative':'0/a.txt',
               'size':8,'sha256':hashlib.sha256(b'expected').hexdigest()}]})
            policy={**sb.DEFAULTS,'sources':[str(source)]}
            with patch.object(sb,'settings',return_value=policy),patch.object(sb,'list_generations',return_value=[{'path':str(gen)}]),patch('os.geteuid',return_value=0):
                with self.assertRaisesRegex(ValueError,'confirmation'):sb.restore_file(gen,target,'bad')
                good=sb.restore_file(gen,target,'RESTORE FILE')
                self.assertEqual(target.read_bytes(),b'expected')
                with self.assertRaisesRegex(ValueError,'exists'):sb.restore_file(gen,target,'RESTORE FILE')
                self.assertFalse(good['replaced_existing'])

    def test_auto_heal_is_opt_in(self):
        with patch.object(sb,'settings',return_value={**sb.DEFAULTS,'auto_restore_missing':False}):
            self.assertEqual(sb.heal_missing()['status'],'disabled')

    def test_copy_end_to_end_creates_verified_generation_without_touching_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp=Path(tmp);state=tmp/'state';state.mkdir();src=tmp/'src';src.mkdir()
            (src/'settings.json').write_bytes(b'{"test":true}')
            target=tmp/'approved-dataset';target.mkdir()
            pool={'key':'external','pool':'tank','dataset':'tank/DVBACKUP','mount':str(target),
                  'eligible':True,'capacity_bytes':500*1024*1024,'guid':'123'}
            cfg={**sb.DEFAULTS,'sources':[str(src)]}
            chosen={k:pool[k] for k in ('dataset','mount','capacity_bytes','guid')}
            with patch.object(sb,'STATE',state),patch.object(sb,'settings',return_value=cfg), \
                 patch.object(sb,'plan',return_value={'recommended':chosen}), \
                 patch.object(sb,'destinations',return_value=[pool]), \
                 patch.object(sb,'_mount_identity',return_value=True), \
                 patch.object(sb,'_guid',return_value='123'), \
                 patch.object(sb,'root_pool',return_value='rpool'), \
                 patch('os.geteuid',return_value=0), \
                 patch.dict(sys.modules,{'backup_policy':types.SimpleNamespace(mark=lambda _: None)}):
                result=sb.perform_backup()
                self.assertEqual(result['status'],'ok')
                self.assertTrue(result['verified'])
                self.assertEqual((src/'settings.json').read_bytes(),b'{"test":true}')
                generation=Path(result['path'])
                self.assertEqual((generation/'files'/'0'/'settings.json').read_bytes(),b'{"test":true}')
                self.assertTrue(sb._verify_generation(generation)['valid'])
                self.assertTrue((state/'smart-backup.lock').exists())

    def test_unenrolled_pool_cannot_be_used_for_automatic_backup(self):
        with patch.object(sb,'_enrolled',return_value=[]):
            self.assertEqual(sb.destinations(root='rpool',policy=sb.DEFAULTS),[])

    def test_os_capacity_reports_missing_zfs_without_writes(self):
        with patch.object(sb,'root_pool',return_value=''):
            self.assertFalse(sb.system_capacity()['zfs_os'])

if __name__=='__main__':unittest.main()