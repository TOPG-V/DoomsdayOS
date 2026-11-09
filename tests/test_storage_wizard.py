import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPT=Path(__file__).resolve().parents[1]/'app'/'zim_manager.py'
spec=importlib.util.spec_from_file_location('zim_manager_storage',SCRIPT)
z=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=z
spec.loader.exec_module(z)
GIB=1024**3

class StorageWizardTests(unittest.TestCase):
    def simulate(self,inputs,free_gib=888):
        cfg={'zim_dir':'/srv/doomsday-zims','staging_dir':'/srv/doomsday-zim-staging',
             'zim_dataset':'rpool/DOOMSDAY_ZIMS','staging_dataset':'rpool/DOOMSDAY_ZIM_STAGING',
             'backup_dir':''}
        state={'copies':2}
        def copies(_):return state['copies']
        def reduce(_):state['copies']=1
        ans=iter(inputs)
        with mock.patch.object(z,'config',return_value=cfg),\
             mock.patch.object(z,'zfs_copies',side_effect=copies),\
             mock.patch.object(z,'disk_free',return_value=free_gib*GIB),\
             mock.patch.object(z,'dataset_at',side_effect=lambda p:'rpool/DOOMSDAY_ZIMS'),\
             mock.patch.object(z,'_enable_single_copy_for_zims',side_effect=reduce) as enable,\
             mock.patch.object(z,'enqueue') as enqueue:
            out=z.queue_chosen(z.load_preset('full'), input_fn=lambda _:next(ans), all_selected=True)
            return out,state['copies'],enable.call_count,enqueue.call_count

    def test_storage_math_explains_large_peak(self):
        chosen=z.load_preset('full')
        with mock.patch.object(z,'dataset_at',return_value='rpool/DOOMSDAY_ZIMS'):
            p=z._storage_estimate(chosen,Path('/archive'),Path('/stage'),2,2)
            self.assertEqual(p['final_bytes'],574998498054*2)
            self.assertEqual(p['staging_bytes'],221251590584*2)
            self.assertTrue(1500*10**9 < p['required_final'] < 1600*10**9)
            efficient=z._storage_estimate(chosen,Path('/archive'),Path('/stage'),1,1)
            self.assertLess(efficient['required_final'],888*GIB)

    def test_space_mode_requires_two_confirmations(self):
        out,copies,enables,queues=self.simulate(['SINGLE COPY','','YES'])
        self.assertEqual(len(out),39)
        self.assertEqual((copies,enables,queues),(1,1,1))

    def test_cancel_at_one_copy_question_changes_nothing(self):
        out,copies,enables,queues=self.simulate([''])
        self.assertEqual(out,[])
        self.assertEqual((copies,enables,queues),(2,0,0))

    def test_declining_final_yes_changes_nothing(self):
        out,copies,enables,queues=self.simulate(['SINGLE COPY','','NO'])
        self.assertEqual(out,[])
        self.assertEqual((copies,enables,queues),(2,0,0))

    def test_even_one_copy_unaffordable(self):
        out,copies,enables,queues=self.simulate([],free_gib=700)
        self.assertEqual(out,[])
        self.assertEqual((copies,enables,queues),(2,0,0))

if __name__=='__main__': unittest.main()
