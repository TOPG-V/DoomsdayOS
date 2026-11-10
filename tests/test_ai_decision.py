import os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
import decision_router as r
import ai_gateway as g
import local_index as idx
import llama_runtime as l
import recovery_advisor as rec

class DecisionTests(unittest.TestCase):
    def test_fast_text_routes(self):
        for query,route in [('Translate this into Polish','translate'),('Open Wikipedia','search'),('find similar image','image_search'),('Where is Wroclaw on a map','map'),('Cite your sources','rag')]:
            self.assertEqual(r.route_text(query).route,route)
    def test_image_routes(self):
        self.assertEqual(r.route_image('Read the text').route,'ocr')
        self.assertEqual(r.route_image('Find similar images').route,'image_search')
        self.assertEqual(r.route_image('what is in it?').route,'vision')
    def test_embedding_picks_only_confident(self):
        self.assertEqual(r.route_text('unstructured words',{'search':.7,'chat':.48}).route,'search')
        self.assertEqual(r.route_text('unstructured words',{'search':.64,'chat':.61}).route,'chat')
        self.assertEqual(r.route_image('',{'image_search':.81,'vision':.4}).route,'image_search')
    def test_image_types(self):
        self.assertEqual(r.recognize_image(b'\x89PNG\r\n\x1a\n12345'),'image/png')
        with self.assertRaises(ValueError):r.recognize_image(b'evil')
    def test_profile(self):
        self.assertEqual(r.inference_profile(2048,0)['kv'],'q4_0')
        self.assertEqual(r.inference_profile(32000,18000)['embedding_dims'],512)
    def test_reject_remote_embedding_server(self):
        with self.assertRaises(ValueError):r.embedding_route_scores('hi',endpoint='https://example.com')
        with self.assertRaises(ValueError):r.image_embedding_scores('data:image/png;base64,AA==',endpoint='http://example.com')
    def test_llama_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'model.gguf';path.write_bytes(b'gguf')
            args=r.llama_args(str(path),r.inference_profile(8000,0))
            self.assertIn('--cache-type-k',args)
            self.assertIn('127.0.0.1',args)

class GatewayTests(unittest.TestCase):
    def test_sends_image_only_to_vision_backend(self):
        raw='data:image/png;base64,iVBORw0KGgoxMjM0'
        with patch.object(g,'_local_json',return_value='looks like a chart') as local:
            d=g.dispatch({'action':'vision','prompt':'What is this image?','image':raw})
            self.assertEqual(d['answer'],'looks like a chart')
            self.assertEqual(local.call_args.args[0],g.VISION)
    def test_no_index_doesnt_fabricate_rag(self):
        with patch.object(g,'text_search',return_value=[]),patch.object(g,'_local_json') as ai:
            d=g.dispatch({'action':'chat','mode':'rag','prompt':'Where are references?'})
            self.assertEqual(d['mode'],'rag-unavailable')
            ai.assert_not_called()
    def test_search_returns_local_link(self):
        with patch.object(g,'text_search',return_value=[]):
            d=g.dispatch({'action':'search','prompt':'wikipedia and maps'})
            self.assertIn('localhost:8080',d['kiwix'])
    def test_unsupported_action(self):
        with self.assertRaises(ValueError):g.dispatch({'action':'shell'})

class LocalIndexTests(unittest.TestCase):
    def test_fts_only_imported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);doc=root/'articles';doc.mkdir();(doc/'energy.md').write_text('Solar power works with photovoltaic panels and batteries')
            db=root/'idx.sqlite'
            self.assertEqual(idx.ingest(doc,db),1)
            hits=idx.search('solar panels',db)
            self.assertTrue(hits)
            self.assertEqual(idx.article(hits[0]['id'],db)['title'],'energy.md')
            self.assertFalse(idx.search('supercalifragilistic',db))

class RecoveryTests(unittest.TestCase):
    def test_lastgood_priority(self):
        shots=['rpool/ROOT/ubuntu@dvstream_20260101','rpool/ROOT/ubuntu@dvstream_20261001']
        self.assertEqual(rec.rank(shots,shots[0])[0],shots[0])

class ModelRegistryTests(unittest.TestCase):
    def test_register_offline_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'my.gguf';path.write_bytes(b'gguf')
            with patch.object(l,'CONFIG',Path(tmp)/'cfg.json'):
                l.register('text',str(path))
                self.assertEqual(l.load_config()['models']['text']['file'],str(path))
                with self.assertRaises(ValueError):l.register('vision',str(Path(tmp)/'nonexistent'))

if __name__=='__main__':unittest.main()
