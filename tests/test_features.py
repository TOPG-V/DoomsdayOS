"""Offline regression tests for ZimLens vNext UI/backend additions."""
import importlib
import os
import pathlib
import sys
import tempfile
import unittest

ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'))
sys.path.insert(0,str(ROOT))
from zimlens_workspace import ContextSessions,estimated_tokens
import zimlens_extensions as ext
from zimlens_api import budget,get,post

class ContextTests(unittest.TestCase):
 def test_threshold_15_and_compaction(self):
  s=ContextSessions()
  sid='a'*24
  # Fill multiple turns to trigger compaction with a small 1024-token budget.
  for j in range(3):
   prompt='Message '+str(j)+' '+('x'*500)
   messages=[{'role':'system','content':'System'}, {'role':'user','content':prompt}]
   out,compacted=s.begin(sid,messages,prompt,1024,128,15,False,lambda _: 'Summary')
   self.assertFalse(compacted)
   s.finish(sid,prompt,'Response '+('y'*180),1024,128)
  messages=[{'role':'system','content':'System'},{'role':'user','content':'final question'}]
  out,compacted=s.begin(sid,messages,'final question',1024,128,15,True,lambda _: 'Trusted summary facts')
  self.assertTrue(compacted)
  self.assertTrue(any('Trusted summary facts' in x['content'] for x in out))
  s.finish(sid,'final question','answer',1024,128)
  self.assertEqual(s.status(sid,1024,128)['compactions'],1)
 def test_session_id_rejects_path(self):
  with self.assertRaises(ValueError):ContextSessions().status('../etc/passwd',1024,128)

class ExtensionTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory()
  self.orig=ext.ROOT
  ext.ROOT=pathlib.Path(self.temp.name)
 def tearDown(self):
  ext.ROOT=self.orig
  self.temp.cleanup()
 def test_theme_contrast(self):
  with self.assertRaises(ValueError):ext.static_test('theme',{'name':'bad','colors':{'text':'#000000','bg':'#000000'}})
  good=ext.add('theme',{'name':'Good theme','colors':{'bg':'#171918','text':'#fafafa'}})
  self.assertEqual(ext.get('theme',good['id'])['colors']['bg'],'#171918')
  self.assertEqual(ext.listing('theme')['items'][0]['id'],'built-in-dark')
  with self.assertRaises(ValueError):ext.remove('theme','built-in-dark')
 def test_mcp_does_not_execute(self):
  item=ext.add('mcp',{'name':'Example','transport':'stdio','command':'/usr/bin/false','args':['--help']})
  self.assertFalse(item['approved'])
  self.assertFalse(item['enabled'])
  result=ext.approve('mcp',item['id'],True)
  self.assertFalse(result['enabled'])
  self.assertIn('external MCP broker',result['status'])
 def test_skill_approval(self):
  record=ext.add('skill',{'name':'Read sources','instructions':'Always cite the local article title.'})
  with self.assertRaises(ValueError):ext.approved_skill_instructions([record['id']])
  ext.approve('skill',record['id'],True)
  self.assertIn('cite',ext.approved_skill_instructions([record['id']]))
 def test_attachment_storage(self):
  project=ext.add('project',{'name':'Test project','description':'Local files'})
  attachment=ext.save_attachment(b'hello','../../test.mp4',project['id'])
  self.assertEqual(attachment['name'],'test.mp4')
  self.assertTrue((ext.ROOT/'attachments'/attachment['id']).is_file())
  with self.assertRaises(ValueError):ext.save_attachment(b'x'*(24*1024*1024+1),'large.txt')
 def test_api_dispatch(self):
  self.assertIn('items',get('/api/zimlens/items','kind=theme',{'context':2048}))
  r=post('/api/zimlens/items',{'action':'test','kind':'theme','data':{'name':'New','colors':{'bg':'#191b1a'}}},
         {'context':2048},lambda _:'',lambda _:'')
  self.assertTrue(r['passed'])
  self.assertEqual(budget({'context':4096,'max_chat_tokens':512}),(4096,512))

if __name__=='__main__':unittest.main()