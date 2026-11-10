import tempfile,unittest
from pathlib import Path
from unittest import mock
import direct_links as dl
import zim_manager as zm

class DirectSwitchTests(unittest.TestCase):
    def setUp(self):
        self.mirror='https://mirror.download.kiwix.org/zim/devdocs/demo_2026-01.zim'
        self.direct='https://download.kiwix.org/zim/devdocs/demo_2026-01.zim'
        self.item={'url':self.mirror,'size':100,'sha256':'a'*64}

    def test_official_path_is_preserved(self):
        self.assertEqual(dl.official_direct_url(self.mirror),self.direct)
        with self.assertRaises(ValueError):dl.official_direct_url('https://other.tld/zim/x.zim')

    def test_matching_publisher_hash_allows_direct(self):
        report=dl.check_one(self.item,checksum_fetch=lambda url:'a'*64,
            probe=lambda url:{'remote_size':100,'resume_supported':True},
            catalog_lookup=lambda row:True)
        self.assertEqual(report['status'],'verified_link_and_publisher_hash')
        self.assertEqual(report['url'],self.direct)

    def test_publisher_mismatch_refused(self):
        report=dl.check_one(self.item,checksum_fetch=lambda url:'b'*64,
            probe=lambda url:None,catalog_lookup=lambda row:True)
        self.assertEqual(report['status'],'unavailable_or_mismatch')

    def test_apply_preserves_queue_choices(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(zm,'BASE',Path(temp)):
                zm.atomic_json(zm.queue_path(),[{**self.item,'title':'demo','status':'retry','backup':True,'storage':'secondary','error':'dns'}])
                records=[{'name':'demo_2026-01.zim','status':'verified_link_and_publisher_hash','url':self.direct,'publisher_sha256':'a'*64}]
                self.assertEqual(dl.apply_records(records),1)
                [item]=zm.read_queue()
                self.assertEqual(item['url'],self.direct)
                self.assertTrue(item['backup'])
                self.assertEqual(item['storage'],'secondary')
                self.assertEqual(item['status'],'pending')

if __name__=='__main__':unittest.main()
