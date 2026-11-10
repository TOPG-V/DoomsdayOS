import contextlib
import sys
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import torrent_import as ti
import zim_manager as zm
import archive_guard
import dual_storage
import transfer_optimizer as optim
from test_torrent_import import bencode


class BatchTorrentTests(unittest.TestCase):
    def prepare(self,root,n=3):
        root=Path(root)
        stage=root/'staging';archive=root/'archive';stage.mkdir();archive.mkdir()
        rows=[]
        for i in range(n):
            name=f'example_{i}.zim'
            payload=(f'valid torrent {i}').encode()
            t=root/f'example_{i}.torrent'
            info={b'name':name.encode(),b'length':len(payload),b'piece length':len(payload),b'pieces':b'a'*20}
            t.write_bytes(bencode({b'info':info}))
            rows.append({'torrent_file':str(t),'sha256':hashlib.sha256(payload).hexdigest(),'storage':'primary'})
        batch=root/'batch.json';batch.write_text(json.dumps(rows))
        return batch,rows,stage,archive

    def test_batch_rejects_weak_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch,rows,stage,archive=self.prepare(tmp)
            rows[0]['sha256']='invalid'
            batch.write_text(json.dumps(rows))
            with self.assertRaises(ValueError):ti._batch_entries(batch)

    def test_parallel_torrents_verified_before_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch,rows,stage,archive=self.prepare(tmp)
            calls=[]
            def fake_runner(args,check=False):
                calls.append(args)
                for torrent in args:
                    if not str(torrent).endswith('.torrent'):continue
                    i=int(Path(torrent).stem.split('_')[-1])
                    (stage/f'example_{i}.zim').write_bytes(f'valid torrent {i}'.encode())
                return mock.Mock(returncode=0)
            def publish(path,sha,url,cfg_override):
                dst=archive/path.name
                dst.write_bytes(path.read_bytes())
                return dst
            with mock.patch.dict(sys.modules, {'zim_manager':zm, 'archive_guard':archive_guard, 'dual_storage':dual_storage, 'transfer_optimizer':optim}),\
                 mock.patch.object(ti.shutil,'which',return_value='/usr/bin/aria2c'),\
                 mock.patch.object(zm,'exclusive_download_worker',return_value=contextlib.nullcontext()),\
                 mock.patch.object(dual_storage,'archive_config',return_value={'zim_dir':str(archive),'staging_dir':str(stage)}),\
                 mock.patch.object(archive_guard,'checked_archive',return_value=(archive,'tank/archive')),\
                 mock.patch.object(archive_guard,'stage_dir',return_value=stage),\
                 mock.patch.object(archive_guard,'publish_verified',side_effect=publish),\
                 mock.patch.object(zm,'_pool_identity',return_value='tank'),\
                 mock.patch.object(zm,'disk_free',return_value=10*1024**3),\
                 mock.patch.object(zm,'zfs_copies',return_value=1),\
                 mock.patch.object(optim,'get_budget',return_value=80),\
                 mock.patch.object(optim,'read_policy',return_value={'datasets':{}}),\
                 mock.patch.object(optim,'dataset_identity',return_value='tank/stage'):
                result=ti.run_torrent_batch(batch,max_parallel=4,cfg={},runner=fake_runner,record_speed=False)
            self.assertEqual(len(result),3)
            self.assertEqual(len(calls),2)
            self.assertIn('--max-concurrent-downloads=2',calls[0])
            self.assertIn('--max-overall-download-limit=80M',calls[0])
            self.assertTrue(all(p.exists() for p in result))
            self.assertEqual(list(stage.glob('*.zim')),[])

    def test_checksum_mismatch_never_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch,rows,stage,archive=self.prepare(tmp,1)
            def fake_runner(args,check=False):
                (stage/'example_0.zim').write_bytes(b'wrong file')
                return mock.Mock(returncode=0)
            with mock.patch.dict(sys.modules, {'zim_manager':zm, 'archive_guard':archive_guard, 'dual_storage':dual_storage, 'transfer_optimizer':optim}),\
                 mock.patch.object(ti.shutil,'which',return_value='/usr/bin/aria2c'),\
                 mock.patch.object(zm,'exclusive_download_worker',return_value=contextlib.nullcontext()),\
                 mock.patch.object(dual_storage,'archive_config',return_value={}),\
                 mock.patch.object(archive_guard,'checked_archive',return_value=(archive,'tank/archive')),\
                 mock.patch.object(archive_guard,'stage_dir',return_value=stage),\
                 mock.patch.object(archive_guard,'publish_verified') as publisher,\
                 mock.patch.object(zm,'_pool_identity',return_value='tank'),\
                 mock.patch.object(zm,'disk_free',return_value=10*1024**3),\
                 mock.patch.object(zm,'zfs_copies',return_value=1),\
                 mock.patch.object(optim,'get_budget',return_value=50),\
                 mock.patch.object(optim,'read_policy',return_value={'datasets':{}}),\
                 mock.patch.object(optim,'dataset_identity',return_value='tank/stage'):
                with self.assertRaises(RuntimeError):
                    ti.run_torrent_batch(batch,cfg={},runner=fake_runner,record_speed=False)
                publisher.assert_not_called()
            self.assertTrue((stage/'example_0.zim').exists())

if __name__=='__main__':unittest.main()

class OriginalTorrentPackTests(unittest.TestCase):
    def test_pinned_torrents_from_extracted_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp=Path(tmp);name='sample_2026-01.zim';payload=b'abcd'
            info={b'name':name.encode(),b'length':len(payload),b'piece length':len(payload),b'pieces':b'a'*20}
            (tmp/(name+'.torrent')).write_bytes(bencode({b'info':info}))
            manifest=tmp/'manifest.json'
            digest=hashlib.sha256(payload).hexdigest()
            manifest.write_text(json.dumps({'items':[{'id':name,'url':'https://download.kiwix.org/zim/test/'+name,
                                                         'size':4,'sha256':digest}]}))
            with mock.patch.object(zm,'BASE',tmp),mock.patch.object(zm,'read_queue',return_value=[]), \
                 mock.patch.object(ti,'run_torrent_batch',return_value=[]) as run:
                result=ti.run_torrent_pack(tmp,manifest=manifest)
                self.assertEqual(result,[])
                rows=json.loads(Path(run.call_args.args[0]).read_text()) if Path(run.call_args.args[0]).exists() else None
                # The temporary batch manifest is securely deleted on return.
                self.assertFalse(Path(run.call_args.args[0]).exists())
                self.assertEqual(run.call_args.kwargs['max_parallel'],4)

    def test_missing_pinned_hash_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp=Path(tmp);name='sample_2026-01.zim'
            info={b'name':name.encode(),b'length':4,b'piece length':4,b'pieces':b'a'*20}
            (tmp/(name+'.torrent')).write_bytes(bencode({b'info':info}))
            manifest=tmp/'manifest.json'
            manifest.write_text(json.dumps({'items':[{'id':name,'size':4}]}))
            with mock.patch.object(zm,'BASE',tmp),mock.patch.object(zm,'read_queue',return_value=[]):
                with self.assertRaises(ValueError):ti.run_torrent_pack(tmp,manifest=manifest)
