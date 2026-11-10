"""Safety tests for split-pool ZIMs, cross-pool backups and read-only serving."""
import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP = Path(__file__).resolve().parents[1] / 'app'
import sys
sys.path.insert(0, str(APP))
import dual_storage as d
import zim_manager as z
import serve_zims as serve

GIB = 1024**3

class DualPlanningTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t=Path(self.tmp.name)
        self.cfg={
            'zim_dir':str(t/'a'),'zim_dataset':'poolA/DOOMSDAY_ZIMS',
            'staging_dir':str(t/'astage'),'staging_dataset':'poolA/DOOMSDAY_ZIM_STAGING',
            'zim_dir_2':str(t/'b'),'zim_dataset_2':'poolB/DOOMSDAY_ZIMS_SECONDARY',
            'staging_dir_2':str(t/'bstage'),'staging_dataset_2':'poolB/DOOMSDAY_ZIM_STAGING_SECONDARY',
            'dual_backup_primary_dir':str(t/'aback'), 'dual_backup_primary_dataset':'poolA/DOOMSDAY_ZIM_BACKUPS',
            'dual_backup_secondary_dir':str(t/'bback'),'dual_backup_secondary_dataset':'poolB/DOOMSDAY_ZIM_BACKUPS',
            'os_pool':'poolA','immutable_zims':True,
        }
        for key in ['zim_dir','staging_dir','zim_dir_2','staging_dir_2',
                    'dual_backup_primary_dir','dual_backup_secondary_dir']:
            Path(self.cfg[key]).mkdir()
        self.items=[{'url':'https://download.kiwix.org/zim/test/a.zim','size':200*GIB,'title':'a'},
                    {'url':'https://download.kiwix.org/zim/test/b.zim','size':100*GIB,'title':'b'},
                    {'url':'https://download.kiwix.org/zim/test/c.zim','size':40*GIB,'title':'c'}]

    def test_partition_succeeds_and_backups_cross_pools(self):
        placement,budgets=d.plan_originals(self.items,self.cfg,
            {'primary':700*GIB,'secondary':520*GIB},
            {'primary':(1,1),'secondary':(1,1)})
        self.assertEqual(len(placement),3)
        for key in budgets:
            b=budgets[key]
            self.assertLessEqual(b['used']+b['stage_peak']+b['reserve'],b['free'])
        # Backup only a small item; the destination must be opposite its original.
        selected={self.items[2]['url']}
        bplan,cost=d.backup_plan(self.items,placement,budgets,
                                {'primary':700*GIB,'secondary':520*GIB},
                                {'primary':1,'secondary':1},selected)
        opposite='secondary' if placement[self.items[2]['url']]=='primary' else 'primary'
        self.assertEqual(bplan[self.items[2]['url']],opposite)
        self.assertEqual(cost[opposite],40*GIB)

    def test_backup_after_download_not_counted_with_staging(self):
        single=[{'url':'u','title':'only','size':100*GIB}]
        placement={'u':'primary'}
        budgets={'primary':{'used':100*GIB,'stage_peak':100*GIB,'reserve':20*GIB},
                 'secondary':{'used':0,'stage_peak':0,'reserve':3*GIB}}
        # With 110 GiB secondary free, a 100 GiB backup fits AFTER downloads.
        planned,_=d.backup_plan(single,placement,budgets,
                                  {'primary':250*GIB,'secondary':110*GIB},
                                  {'primary':1,'secondary':1},{'u'})
        self.assertEqual(planned,{'u':'secondary'})

    def test_reject_if_not_enough_room_even_divided(self):
        with self.assertRaisesRegex(ValueError,'INSUFFICIENT SPACE'):
            d.plan_originals(self.items,self.cfg,{'primary':200*GIB,'secondary':150*GIB},
                             {'primary':(1,1),'secondary':(1,1)})

    def test_resumable_partial_stays_on_original_pool(self):
        # This is intentionally smaller than the largest item but should stay on pool B.
        partial=self.items[2]['url']
        placed,_=d.plan_originals(self.items,self.cfg,
                    {'primary':500*GIB,'secondary':350*GIB},
                    {'primary':(1,1),'secondary':(1,1)},existing_parts={partial:'secondary'})
        self.assertEqual(placed[partial],'secondary')

    def test_existing_file_is_never_migrated(self):
        item={'url':'https://download.kiwix.org/zim/test/a.zim','size':1,'title':'a'}
        (Path(self.cfg['zim_dir_2'])/'a.zim').write_bytes(b'x')
        placed,_=d.plan_originals([item],self.cfg,
                    {'primary':10*GIB,'secondary':10*GIB},
                    {'primary':(1,1),'secondary':(1,1)})
        self.assertEqual(placed[item['url']],'secondary')

    def test_no_second_pool_refuses_dual_optimizer(self):
        without={k:v for k,v in self.cfg.items() if not k.endswith('_2')}
        with mock.patch.object(z,'config',return_value=without):
            self.assertEqual(z.optimize_two_drives(input_fn=lambda _:'APPLY PLAN'),[])

    def test_secondary_pool_missing_never_falls_back_to_primary(self):
        bad={'url':'https://download.kiwix.org/zim/test/a.zim','status':'pending','storage':'secondary'}
        with mock.patch.object(z,'config',return_value=self.cfg),\
             mock.patch.object(z,'read_queue',return_value=[bad]),\
             mock.patch.object(d,'verify_locations',side_effect=ValueError('second missing')),\
             self.assertRaisesRegex(ValueError,'second missing'):
            z.run_queue()

    def test_serving_skips_offline_secondary_if_primary_is_present(self):
        a=Path(self.cfg['zim_dir']);(a/'test.zim').write_bytes(b'z')
        def checked(cfg):
            if cfg['zim_dir']==self.cfg['zim_dir_2']:raise RuntimeError('disconnected')
            return a,self.cfg['zim_dataset']
        with mock.patch.object(serve,'config',return_value=self.cfg),\
             mock.patch.object(serve,'checked_archive',side_effect=checked),\
             mock.patch.object(serve,'guard_path'),\
             mock.patch.object(serve.shutil,'which',return_value='/usr/bin/kiwix-serve'),\
             mock.patch.object(serve.os,'execv') as exe,\
             contextlib.redirect_stderr(io.StringIO()):
            serve.main()
        args=exe.call_args.args[1]
        self.assertIn(str(a),args)
        self.assertNotIn(str(self.cfg['zim_dir_2']),args)

    def test_multidrive_archive_is_verified_before_using_downloaded_copy(self):
        item=dict(self.items[0],storage='primary',sha256='0'*64)
        (Path(self.cfg['zim_dir_2'])/'a.zim').write_bytes(b'bad')
        with mock.patch.object(z,'config',return_value=self.cfg),\
             mock.patch('archive_guard.checked_archive',return_value=(Path(self.cfg['zim_dir']),self.cfg['zim_dataset'])),\
             mock.patch('archive_guard.stage_dir',return_value=Path(self.cfg['staging_dir'])),\
             self.assertRaisesRegex(ValueError,'checksum mismatch'):
            z._queue_original(item,self.cfg)

if __name__=='__main__':unittest.main()
