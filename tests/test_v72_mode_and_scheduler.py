import os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
APP=Path(__file__).parents[1]/'app';sys.path.insert(0,str(APP))
import model_scheduler as model
import ai_gateway

class Proc:
    def __init__(self):self.ended=False
    def poll(self):return 0 if self.ended else None
    def terminate(self):self.ended=True
    def kill(self):self.ended=True
    def wait(self,timeout=5):return 0

class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.files={}
        for role in ('text','decision','vision'):
            p=Path(self.temp.name)/(role+'.gguf')
            with p.open('wb') as f:f.truncate(1250*model.MIB)
            self.files[role]={'file':str(p)}
        self.procs=[]
    def make(self,total=8192,available=6000):
        self.manager=model.Manager(spawn=lambda argv:self.fake_spawn(),health=lambda role:True,
              ram=lambda:max(0,available-len([p for p in self.procs if not p.ended])*model.estimate_mib(self.files['text'])),
              total=lambda:total)
        return self.manager
    def fake_spawn(self):
        p=Proc();self.procs.append(p);return p
    def test_policy_one_on_low_ram_both_if_capacity(self):
        self.assertEqual('switch-one-at-a-time',model.determine_policy(self.files,8192,6000)['policy'])
        self.assertEqual('keep-both',model.determine_policy(self.files,32768,24576)['policy'])
    def test_switch_only_after_lease_release(self):
        m=self.make()
        with patch.object(model,'load_config',return_value={'models':self.files}),patch.object(model,'command_for',return_value=['llama-server']):
            text=m.acquire('text')
            with self.assertRaisesRegex(RuntimeError,'another model is answering'):m.acquire('decision')
            self.assertEqual(['text'],m.status()['loaded'])
            self.assertTrue(m.release(text['token']))
            decision=m.acquire('decision')
            self.assertEqual(['decision'],m.status()['loaded'])
            self.assertFalse(self.procs[1].ended)
            self.assertTrue(self.procs[0].ended)
            m.release(decision['token']);m.close()
    def test_two_loaded_when_enough_memory(self):
        m=self.make(total=32768,available=24576)
        with patch.object(model,'load_config',return_value={'models':self.files}),patch.object(model,'command_for',return_value=['llama-server']):
            t=m.acquire('text');d=m.acquire('decision')
            self.assertEqual(['decision','text'],m.status()['loaded'])
            self.assertFalse(self.procs[0].ended)
            m.release(t['token']);m.release(d['token']);m.close()
    def test_missing_model_clear_error(self):
        m=self.make()
        with patch.object(model,'load_config',return_value={'models':{}}):
            with self.assertRaisesRegex(RuntimeError,'No registered'):m.acquire('text')
    def test_core_never_starts_ai(self):
        with tempfile.TemporaryDirectory() as t:
            file=Path(t)/'mode';file.write_text('core\n')
            with patch.object(ai_gateway,'INSTALL_MODE_FILE',file):
                self.assertFalse(ai_gateway.ai_enabled())
                with self.assertRaisesRegex(RuntimeError,'Full mode'):ai_gateway.agent_chat('plan a route')
                self.assertFalse(ai_gateway.dispatch({'action':'status'})['ai_enabled'])
    def test_agent_tools_location_not_inferred(self):
        from agent_tools import execute_tool
        with self.assertRaisesRegex(ValueError,'Current location unavailable'):
            execute_tool('plan_trip',{'start':'current','destination':'Manchester','mode':'car'})
    def test_language_list_covers_english_russian_eu(self):
        from speech_local import LANGUAGES
        for code in ('en','ru','uk','pl','de','fr','it','es','pt','nl','fi','et','el','mt','ga'):
            self.assertIn(code,LANGUAGES)
    def test_memory_budget_keeps_os_reserve(self):
        self.assertLessEqual(model.memory_budget(8192,8192),8192-2048)
        self.assertEqual(0,model.memory_budget(2048,400))

if __name__=='__main__':unittest.main()
