import sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from torrent_import import torrent_info,_bdecode,run_torrent_import

def bencode(x):
    if isinstance(x,int):return b'i'+str(x).encode()+b'e'
    if isinstance(x,bytes):return str(len(x)).encode()+b':'+x
    if isinstance(x,list):return b'l'+b''.join(bencode(i) for i in x)+b'e'
    if isinstance(x,dict):return b'd'+b''.join(bencode(k)+bencode(v) for k,v in sorted(x.items()))+b'e'
    raise TypeError(x)

class TorrentTests(unittest.TestCase):
    def sample(self,name=b'wikipedia_en_all.zim',multifile=False):
        meta={b'name':name,b'length':8,b'piece length':4,b'pieces':b'a'*40}
        if multifile:meta[b'files']=[{b'length':8,b'path':[b'unsafe']}]
        return bencode({b'info':meta})
    def test_one_file_only(self):
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/'example.torrent';path.write_bytes(self.sample())
            self.assertEqual(torrent_info(path)['size'],8)
            path.write_bytes(self.sample(multifile=True))
            with self.assertRaises(ValueError):torrent_info(path)
    def test_reject_traversal(self):
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/'example.torrent';path.write_bytes(self.sample(name=b'../evil.zim'))
            with self.assertRaises(ValueError):torrent_info(path)
    def test_malformed_bencode(self):
        for broken in (b'',b'd3:foo',b'd3:fooi3eejunk',b'd3:fooi03ee'):
            with self.assertRaises(ValueError):_bdecode(broken)
    def test_requires_independent_sha(self):
        with self.assertRaises(ValueError):run_torrent_import('file.torrent','')

if __name__=='__main__':unittest.main()
