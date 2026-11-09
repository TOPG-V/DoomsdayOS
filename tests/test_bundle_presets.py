import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPT=Path(__file__).resolve().parents[1] / 'app' / 'zim_manager.py'
spec=importlib.util.spec_from_file_location('zim_manager_presets', SCRIPT)
z=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=z
spec.loader.exec_module(z)

class BundlePresetTests(unittest.TestCase):
    def test_exact_presets(self):
        full=z.load_preset('full')
        partial=z.load_preset('partial')
        wiki=z.load_preset('wikipedia')
        self.assertEqual(len(full),39)
        self.assertEqual(len(partial),38)
        self.assertEqual(len(wiki),1)
        self.assertEqual({x['id'] for x in full}-{x['id'] for x in partial},{'ted_mul_all_2026-09.zim'})
        self.assertEqual(sum(x['size'] for x in full),574998498054)
        self.assertEqual(sum(x['size'] for x in partial),483822701360)
        self.assertEqual(wiki[0]['id'],'wikipedia_en_all_maxi_2026-08.zim')
        self.assertEqual(sum(x['size'] for x in wiki),127418087648)

    def test_preset_menu_without_network(self):
        for key,n in [('1',39),('2',38),('3',1)]:
            with self.subTest(key=key), mock.patch.object(z,'queue_chosen',return_value=['queued']) as submit, mock.patch.object(z,'catalog',side_effect=AssertionError('no network needed')):
                self.assertEqual(z.interactive_wizard(input_fn=lambda _:key),['queued'])
                self.assertEqual(len(submit.call_args.args[0]),n)

    def test_abort(self):
        with mock.patch.object(z,'queue_chosen') as submit:
            self.assertEqual(z.interactive_wizard(input_fn=lambda _:'bad'),[])
            submit.assert_not_called()

if __name__=='__main__':unittest.main()
