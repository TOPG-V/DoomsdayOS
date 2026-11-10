"""Regression tests for v6.8 title-first RAG and traditional ZIM image matching."""
import io,sys,tempfile,unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
import zim_index,ai_gateway

try:
    from PIL import Image
except ImportError:
    Image=None

class Item:
    def __init__(self,mime,data):self.mimetype=mime;self.size=len(data);self.content=memoryview(data)
class Entry:
    is_redirect=False
    def __init__(self,path,title,mime,blob):
        self.path,self.title=path,title
        self.item=Item(mime,blob)
    def get_item(self):return self.item

class Archive:
    picture=b''
    def __init__(self,filename):
        self.entries=[
            Entry('A/Solar','Solar Energy','text/html',b'<h1>Solar energy</h1><p>Photovoltaic sunlight to electricity</p><img src="/I/shared.png" alt="Solar panel diagram"><video src="/V/movie.mp4"></video>'),
            Entry('A/Wind','Wind Turbine','text/html',b'<h1>Wind energy</h1><p>Solar batteries are sometimes stored here, but this is a wind article.</p><img src="/I/shared.png" alt="Turbine picture">'),
            Entry('A/Water','Water Purification','text/html',b'<h1>Water purification</h1><img src="/I/water.png">'),
            Entry('I/shared.png','Shared image','image/png',self.picture),
            Entry('I/water.png','Water image','image/png',b'non-png'),
            Entry('V/movie.mp4','Audio/Video','video/mp4',b'media')]
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def iter_by_path(self):return iter(self.entries)

@unittest.skipIf(Image is None,'Pillow is optional')
class ArticleScopeTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup)
        self.root=Path(t.name);self.db=self.root/'zim.sqlite';self.zim=self.root/'science.zim'
        self.zim.write_bytes(b'tiny mock')
        im=Image.new('RGB',(80,64))
        for x in range(80):
            for y in range(64):im.putpixel((x,y),(x*3%256,y*3%256,(x+y)*2%256))
        buf=io.BytesIO();im.save(buf,format='PNG');self.raw=buf.getvalue();Archive.picture=self.raw
        zim_index.index_archive(self.zim,db_path=self.db,max_entries=30,reader_factory=Archive)

    def test_title_rag_does_not_pull_text_from_other_articles(self):
        hits=zim_index.article_title_search('How do solar energy panels work?',self.db)
        self.assertEqual(len(hits),1)
        self.assertEqual(hits[0]['title'],'Solar Energy')
        self.assertIn('Photovoltaic',hits[0]['snippet'])
        self.assertNotIn('Wind energy',hits[0]['snippet'])
        # Text-only matches deliberately cannot nominate an article as a RAG source.
        self.assertEqual(zim_index.article_title_search('Photovoltaic',self.db),[])

    def test_images_only_from_matching_article(self):
        solar=zim_index.article_title_search('solar energy',self.db)
        images=zim_index.article_images(solar,self.db)
        self.assertEqual(len(images),1)
        self.assertIn('I/shared.png',images[0]['image_url'])
        self.assertIn('/A/Solar',images[0]['article_link'])
        self.assertNotIn('movie.mp4',str(images))
        self.assertNotIn('water.png',str(images))

    def test_shared_resource_keeps_both_correct_article_links(self):
        all_chosen=zim_index.article_title_search('solar wind',self.db)
        images=zim_index.article_images(all_chosen,self.db)
        links={x['article_link'] for x in images}
        self.assertEqual(len(links),2)
        self.assertTrue(any('/A/Solar' in link for link in links))
        self.assertTrue(any('/A/Wind' in link for link in links))
        self.assertEqual(len({x['image_url'] for x in images}),1)

    def test_classic_image_search_is_scoped_by_title(self):
        solar=zim_index.similar_image(self.raw,db_path=self.db,title_query='solar energy')
        wind=zim_index.similar_image(self.raw,db_path=self.db,title_query='wind turbine')
        self.assertTrue(solar and wind)
        self.assertEqual(solar[0]['distance'],0)
        self.assertIn('/A/Solar',solar[0]['link'])
        self.assertIn('/A/Wind',wind[0]['link'])
        self.assertIn('dHash',solar[0]['method'])
        self.assertEqual(zim_index.similar_image(self.raw,db_path=self.db,title_query='unfindablexyz'),[])

    def test_existing_v67_db_without_dhash_migrates(self):
        old=self.root/'legacy.sqlite'
        import sqlite3
        with sqlite3.connect(old) as db:
            db.execute('CREATE TABLE media (zim TEXT, path TEXT, mime TEXT, title TEXT, article TEXT, alt TEXT, hash TEXT, PRIMARY KEY(zim,path))')
            db.execute('CREATE VIRTUAL TABLE articles USING fts5(title,text,zim UNINDEXED,path UNINDEXED)')
            db.execute('INSERT INTO articles VALUES (?,?,?,?)',('Solar Energy','sunlight converts','old.zim','A/Solar'))
            db.execute('INSERT INTO media VALUES (?,?,?,?,?,?,?)',('old.zim','I/shared.png','image/png','photo','A/Solar','sun',zim_index._image_hashes(self.raw)[0]))
        hits=zim_index.article_title_search('solar energy',old)
        self.assertEqual(len(hits),1)
        self.assertEqual(len(zim_index.article_images(hits,old)),1)
        self.assertEqual(zim_index.similar_image(self.raw,db_path=old,title_query='solar')[0]['distance'],0)

    def test_gateway_keeps_index_scoped_and_ai_context_bounded(self):
        def articles(query,limit):return zim_index.article_title_search(query,self.db,limit)
        def images(hits,limit):return zim_index.article_images(hits,self.db,limit)
        calls=[]
        def respond(endpoint,payload,timeout=90):calls.append(payload);return 'Solar conversion [1]'
        with mock.patch.object(ai_gateway,'article_title_search',side_effect=articles),\
             mock.patch.object(ai_gateway,'article_images',side_effect=images),\
             mock.patch.object(ai_gateway,'_local_json',side_effect=respond):
            response=ai_gateway.dispatch({'action':'chat','mode':'rag','prompt':'solar energy'})
        self.assertEqual(response['mode'],'rag')
        self.assertEqual(len(response['sources']),1)
        self.assertEqual(len(response['images']),1)
        context=' '.join(str(msg['content']) for msg in calls[0]['messages'])
        self.assertIn('Solar Energy',context)
        self.assertNotIn('Water Purification',context)
        self.assertNotIn('Wind Turbine',context)

    def test_gateway_image_search_is_algorithm_only(self):
        with mock.patch.object(ai_gateway,'_data_image',return_value=self.raw),\
             mock.patch.object(ai_gateway,'zim_similar_image',return_value=[]) as matcher,\
             mock.patch.object(ai_gateway,'_local_json',side_effect=AssertionError('No inference permitted')):
            response=ai_gateway.dispatch({'action':'image_search','prompt':'solar energy','image':'stub'})
        self.assertEqual(response['mode'],'classic_ahash_dhash')
        self.assertEqual(matcher.call_args.kwargs['title_query'],'solar energy')

if __name__=='__main__':unittest.main()
