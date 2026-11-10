import base64
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
import research_library as rl
import ai_gateway as gw
import zim_index


def sample_pdf():
    from reportlab.pdfgen import canvas
    bio=io.BytesIO();c=canvas.Canvas(bio)
    c.drawString(40,700,'Solar panels can charge batteries.')
    c.showPage();c.drawString(40,700,'Test PDF: the second page explains charge controller voltage.')
    c.save();return bio.getvalue()


class ResearchLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'research'
        self.data=sample_pdf()

    def test_import_pdf_search_and_cited_pages(self):
        result=rl.import_pdf(self.data,'solar.pdf',root=self.root)
        self.assertEqual(result['pages'],2)
        self.assertEqual(result['chunks'],2)
        found=rl.search('controller voltage',root=self.root,include_zim=False)
        self.assertEqual(len(found),1)
        self.assertEqual(found[0]['page'],2)
        self.assertIn('#page=2',found[0]['link'])
        selected=rl.read(found[0]['id'],root=self.root)
        self.assertIn('Page 2',selected['text'])
        self.assertNotIn('Page 1',selected['text'])
        self.assertIn('#page=2',selected['link'])
        prepared=rl.prepare_answer('voltage',root=self.root)
        self.assertEqual(prepared[0]['link'],found[0]['link'])

    def test_deduplicate_and_sha(self):
        a=rl.import_pdf(self.data,'solar.pdf',root=self.root)
        b=rl.import_pdf(self.data,'solar-copy.pdf',root=self.root)
        self.assertEqual(a['id'],b['id'])
        self.assertTrue(b['existing'])
        self.assertEqual(rl.status(self.root)['pdfs'],1)
        digest=a['id'].split(':')[1]
        self.assertEqual(rl.pdf_path(digest,self.root).read_bytes(),self.data)

    def test_reject_wrong_type_scanned_and_traversal(self):
        with self.assertRaises(ValueError):rl.import_pdf(b'not a pdf at all','foo.pdf',self.root)
        with self.assertRaises(ValueError):rl.import_pdf(self.data,'foo.jpg',self.root)
        with self.assertRaises(ValueError):rl.pdf_path('../etc/passwd',self.root)
        with self.assertRaises(ValueError):rl.import_pdf(b'%PDF-1.6'+b'0'*(rl.PDF_MAX+1),'large.pdf',self.root)

    def test_safe_url_rules(self):
        bad=['http://example.org/article','https://127.0.0.1/',
             'https://localhost/','https://example.local/a','https://user:pw@example.org/',
             'https://example.org:1234/file','file:///etc/shadow','https://[::1]/']
        for url in bad:
            with self.subTest(url=url),self.assertRaises(ValueError):rl._web_address(url)

    def test_html_import_optin_and_visible_content(self):
        url='https://example.org/my-research'
        with self.assertRaises(ValueError):rl.import_web(url,False,root=self.root)
        html=b'<html><head><title>Article</title><style>secret</style></head><body><nav>navigation junk</nav><article><h1>Renewable power</h1><p>Solar cells generate electrical current when exposed to sunlight.</p></article><script>malicious instructions</script></body></html>'
        with mock.patch.object(rl.socket,'getaddrinfo',return_value=[(2,1,6,'',('93.184.215.14',443))]):
            result=rl.import_web(url,True,root=self.root,fetcher=lambda u:('text/html',html))
        doc=rl.read(result['id'],root=self.root)
        self.assertIn('Solar cells generate',doc['text'])
        self.assertNotIn('malicious',doc['text'])
        self.assertEqual(doc['link'],url)
        self.assertEqual(rl.status(self.root)['webpages'],1)

    def test_web_rejects_private_dns_even_with_fake_fetcher(self):
        with mock.patch.object(rl.socket,'getaddrinfo',return_value=[(2,1,6,'',('192.168.0.1',443))]):
            with self.assertRaises(ValueError):
                rl.import_web('https://example.org/a',True,root=self.root,fetcher=lambda u:('text/plain',b'NEVER'))

    def test_fts_search_no_injections(self):
        rl.import_pdf(self.data,'solar.pdf',root=self.root)
        self.assertEqual(rl.search('" OR *',root=self.root,include_zim=False),[])
        self.assertEqual(rl.search('',root=self.root,include_zim=False),[])

    def test_zim_indexed_link_resolves_without_http(self):
        db=Path(self.tmp.name)/'zim.sqlite'
        with zim_index.connect(db) as c:
            c.execute("INSERT INTO articles(title,text,zim,path) VALUES(?,?,?,?)",
                      ('Solar energy','Solar energy can make electricity','/archive/wiki.zim','A/Solar_energy'))
        with mock.patch.object(zim_index,'DB',db):
            sources=rl.search('solar energy',root=self.root)
            self.assertTrue(any(a['kind']=='zim' for a in sources))
            z=[x for x in sources if x['kind']=='zim'][0]
            self.assertTrue(z['link'].startswith('http://localhost:8080/'))
            source=rl.read(z['id'],root=self.root)
            self.assertIn('Solar energy can make electricity',source['text'])
            self.assertEqual(rl.read('zim:////etc/passwd',root=self.root),None)

    def test_gateway_dispatch_research_fallback_and_sources(self):
        rl.import_pdf(self.data,'solar.pdf',root=self.root)
        with mock.patch.object(rl,'ROOT',self.root),mock.patch.object(gw,'_local_json',side_effect=ConnectionError('llama not running')):
            result=gw.dispatch({'action':'research_answer','prompt':'controller voltage'})
        self.assertEqual(result['mode'],'research-sources-only')
        self.assertTrue(result['sources'])
        self.assertIn('#page=2',result['sources'][0]['link'])

    def test_gateway_uses_numbered_citations_and_restricts_to_selected(self):
        rl.import_pdf(self.data,'solar.pdf',root=self.root)
        selected=rl.search('voltage',root=self.root,include_zim=False)[0]['id']
        captures=[]
        def fake_llama(endpoint,payload,timeout=90):
            captures.append(payload);return 'Charge voltage [1].'
        with mock.patch.object(rl,'ROOT',self.root),mock.patch.object(gw,'_local_json',side_effect=fake_llama):
            d=gw.dispatch({'action':'research_answer','prompt':'What is charge voltage?','source_ids':[selected]})
        self.assertEqual(d['mode'],'research-cited')
        self.assertEqual(len(d['sources']),1)
        self.assertIn('REFERENCE EXCERPTS',captures[0]['messages'][1]['content'])
        self.assertNotIn('Page 1',captures[0]['messages'][1]['content'])

    def test_reject_bad_selected_sources(self):
        with self.assertRaises(ValueError):gw.dispatch({'action':'research_answer','prompt':'hello','source_ids':'wrong'})


class ResearchServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'research'
        rl.import_pdf(sample_pdf(),'source.pdf',self.root)
        self.patch=mock.patch.object(rl,'ROOT',self.root);self.patch.start();self.addCleanup(self.patch.stop)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),gw.Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.origin='http://127.0.0.1:'+str(self.server.server_address[1])

    def test_research_endpoints_and_pdf_stream(self):
        with urllib.request.urlopen(self.origin+'/api/research/search?q=voltage') as response:
            data=json.load(response)
        self.assertIn('#page=2',data['results'][0]['link'])
        digest=data['results'][0]['id'].split(':')[1].split('@')[0]
        with urllib.request.urlopen(self.origin+'/api/research/file/'+digest) as response:
            self.assertEqual(response.headers['Content-Type'],'application/pdf')
            self.assertEqual(response.read()[:5],b'%PDF-')

    def test_pdf_upload_and_csrf_origin(self):
        req=urllib.request.Request(self.origin+'/api/research/upload',self.data(),method='POST',headers={'Content-Type':'application/pdf','X-File-Name':'new.pdf','Origin':self.origin})
        with urllib.request.urlopen(req) as response:self.assertEqual(response.status,201)
        req=urllib.request.Request(self.origin+'/api/research/upload',self.data(),method='POST',headers={'Content-Type':'application/pdf','X-File-Name':'new.pdf','Origin':'https://evil.example'})
        with self.assertRaises(HTTPError) as cm:urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code,403)

    def data(self):return sample_pdf()


if __name__=='__main__':unittest.main()
