import json, os, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1] / 'app'))
import archive_guard as ag
import zim_manager as zm

class ArchiveGuardTests(unittest.TestCase):
    def test_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'a.zim';p.write_bytes(b'example')
            self.assertEqual(len(ag.sha256(p)),64)
    def test_rejects_os_root_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            config={'zim_dir':tmp,'zim_dataset':'rpool/ROOT/ubuntu'}
            with patch.object(ag,'exact_dataset',return_value='rpool/ROOT/ubuntu'):
                with self.assertRaisesRegex(RuntimeError,'running system|system dataset'):
                    ag.checked_archive(config)
    def test_mismatch_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib=Path(tmp)/'library';lib.mkdir();st=Path(tmp)/'stage';st.mkdir()
            f=st/'wikipedia.zim';f.write_bytes(b'archive bytes')
            cfg={'zim_dir':str(lib),'staging_dir':str(st),'zim_dataset':'tank/zims','staging_dataset':'tank/zim-staging'}
            with patch.object(ag,'current_config',return_value=cfg),patch.object(ag,'checked_archive',return_value=(lib,'tank/zims')),patch.object(ag,'stage_dir',return_value=st):
                with self.assertRaisesRegex(RuntimeError,'checksum mismatch'):
                    ag.publish_verified(f,'0'*64)
            self.assertEqual(list(lib.iterdir()),[])
    def test_valid_publish_restores_readonly_and_records_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib=Path(tmp)/'library';lib.mkdir();st=Path(tmp)/'stage';st.mkdir()
            f=st/'wikipedia.zim';f.write_bytes(b'archive bytes')
            sha=ag.sha256(f)
            cfg={'zim_dir':str(lib),'staging_dir':str(st),'zim_dataset':'tank/zims','staging_dataset':'tank/zim-staging','min_free_gib':0}
            operations=[]
            with patch.object(ag,'current_config',return_value=cfg), patch.object(ag,'checked_archive',return_value=(lib,'tank/zims')),patch.object(ag,'stage_dir',return_value=st),patch.object(ag,'must',return_value='1'),patch.object(ag,'set_ro',side_effect=lambda ds,ro: operations.append(ro)),patch.object(ag,'record_manifest') as record,patch.object(ag,'run'):
                out=ag.publish_verified(f,sha,'https://download.kiwix.org/test.zim')
                self.assertEqual(out.read_bytes(),b'archive bytes')
                self.assertEqual(operations,[False,True])
                record.assert_called_once()
    def test_absent_usb_archive_defers_without_writing_other_mounts(self):
        from contextlib import nullcontext
        with patch.object(ag,'current_config',return_value={'immutable_zims':True}), \
             patch('dual_storage.locations',return_value=[{'key':'secondary'}]), \
             patch('dual_storage.archive_config',return_value={'zim_dir':'/tank/zims','zim_dataset':'tank/zims'}), \
             patch.object(ag,'archive_lock',return_value=nullcontext()), \
             patch.object(ag,'checked_archive',side_effect=RuntimeError('Dedicated ZIM ZFS dataset missing; refusing to alter other filesystems')), \
             patch.object(ag,'set_ro') as ro:
            self.assertIn('deferred',ag.protect().lower())
            ro.assert_not_called()

    def test_unsafe_root_dataset_never_becomes_a_soft_warning(self):
        from contextlib import nullcontext
        with patch.object(ag,'current_config',return_value={'immutable_zims':True}), \
             patch('dual_storage.locations',return_value=[{'key':'primary'}]), \
             patch('dual_storage.archive_config',return_value={'zim_dir':'/','zim_dataset':'rpool/ROOT/os'}), \
             patch.object(ag,'archive_lock',return_value=nullcontext()), \
             patch.object(ag,'checked_archive',side_effect=RuntimeError('Refusing read-only on a pool root or system dataset')):
            with self.assertRaisesRegex(RuntimeError,'Refusing read-only'):
                ag.protect()

    def test_safe_url_rejects_private_network(self):
        with self.assertRaises(ValueError):zm.safe_url('http://localhost/example.zim')
    def test_copies_peak_estimation(self):
        self.assertEqual(zm.size_text(1024**3),'1.00 GiB')

if __name__=='__main__':unittest.main()
