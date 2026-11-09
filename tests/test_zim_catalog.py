import importlib.util
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

SCRIPT=Path(__file__).resolve().parents[1] / 'app' / 'zim_manager.py'
spec=importlib.util.spec_from_file_location('zim_manager',SCRIPT)
z=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=z
spec.loader.exec_module(z)

def feed(entries):
    return ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'+''.join(entries)+'</feed>').encode()

def entry(name, suffix='.zim.meta4', size='1500000000'):
    return ('<entry><title>'+name+'</title><id>'+name+'</id>'
            '<link rel="http://opds-spec.org/acquisition/open-access" type="application/x-zim" '
            'href="https://download.kiwix.org/zim/wikipedia/'+name+suffix+'" length="'+size+'" /></entry>')

class CatalogParserTests(unittest.TestCase):
    def test_metalink_only_feed_is_visible_and_normalized(self):
        items=z.parse_catalog(feed([entry('wikipedia_en_all_maxi_2026-10')]))
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['url'],'https://download.kiwix.org/zim/wikipedia/wikipedia_en_all_maxi_2026-10.zim')
        self.assertEqual(items[0]['size'],1500000000)

    def test_prefer_direct_zim_link(self):
        base=entry('wiktionary_en_all_2026-09')
        direct='<link rel="http://opds-spec.org/acquisition/open-access" href="https://download.kiwix.org/zim/wiktionary/wiktionary_en_all_2026-09.zim" length="12345678" />'
        xml=base.replace('</entry>',direct+'</entry>')
        it=z.parse_catalog(feed([xml]))[0]
        self.assertEqual(it['size'],12345678)
        self.assertTrue(it['url'].endswith('.zim'))

    def test_ignore_small_metalink_length(self):
        items=z.parse_catalog(feed([entry('wikivoyage_en_all_2026-01',size='4310')]))
        self.assertEqual(items[0]['size'],0)

    def test_reject_untrusted_host(self):
        a=entry('bad_en_all_2026-01').replace('download.kiwix.org','127.0.0.1')
        self.assertEqual(z.parse_catalog(feed([a])),[])

    def test_filter_can_reach_later_pages(self):
        pages=[]
        for start in [0,100]:
            names=[f'irrelevant_{i}' for i in range(start,start+100)]
            if start==100:names[0]='Wikipedia_English'
            pages.append(feed([entry(n) for n in names]))
        pages.append(feed([]))
        def fetcher(url):
            start=int(parse_qs(urlsplit(url).query)['start'][0]);return pages[start//100]
        items=z.catalog('Wikipedia',limit=50,fetcher=fetcher)
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['title'],'Wikipedia_English')

if __name__=='__main__':unittest.main()
