"""Offline smoke/regression tests; no ZFS hardware, AI weights, or OSRM data needed."""
import importlib,io,json,os,sqlite3,sys,tempfile,unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
import zim_index,offline_maps,recovery_watch


class FakeItem:
    def __init__(self,mime,data):self.mimetype=mime;self.size=len(data);self.content=memoryview(data)
class FakeEntry:
    is_redirect=False
    def __init__(self,path,title,mime,data):self.path=path;self.title=title;self.item=FakeItem(mime,data)
    def get_item(self):return self.item
class FakeArchive:
    def __init__(self,fn):
        self.entries=[FakeEntry('A/Mars','Mars Base','text/html',b'<h1>Mars shelter</h1><p>solar batteries and oxygen storage</p><img src="/I/rocket.jpg" alt="rocket blueprint">'),
                      FakeEntry('I/rocket.jpg','Rocket photo','image/jpeg',b'\xff\xd8fake_image'),
                      FakeEntry('V/escape.mp4','Rescue video','video/mp4',b'audio_data'),
                      FakeEntry('A/Wind','Wind shelter','text/html',b'<p>turbine generator</p>')]
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def iter_by_path(self):return iter(self.entries)


class ZimIndexTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.folder=Path(self.temp.name)
    def test_html_ignores_script_and_media_link(self):
        text,links=zim_index.plain_html(b'<h1>Solar setup</h1><script>private secret</script><img src="../I/test.jpg" alt="solar panel"><p>12 volt</p>')
        self.assertIn('Solar setup',text);self.assertNotIn('private secret',text)
        self.assertEqual(links,[('../I/test.jpg','solar panel')])
    def test_chunked_resumable_index_with_link(self):
        zim=self.folder/'test.zim';zim.write_bytes(b'zims')
        db=self.folder/'index.sqlite'
        a=zim_index.index_archive(zim,db_path=db,max_entries=2,reader_factory=FakeArchive)
        self.assertFalse(a['complete']);self.assertEqual(a['scanned'],2)
        b=zim_index.index_archive(zim,db_path=db,max_entries=10,reader_factory=FakeArchive)
        self.assertTrue(b['complete']);self.assertEqual(b['scanned'],2)
        self.assertTrue(zim_index.index_archive(zim,db_path=db,max_entries=10,reader_factory=FakeArchive)['already_indexed'])
        articles=zim_index.search('oxygen',db_path=db)
        self.assertEqual(len(articles),1);self.assertIn('/test/A/Mars',articles[0]['link'])
        media=zim_index.media_search('rocket',db_path=db)
        self.assertTrue(media)
        self.assertIn('/test/A/Mars',media[0]['link'])
        self.assertEqual(zim_index.status(db)['media'],2)
        with zim_index.connect(db) as c:
            c.execute('INSERT INTO media_notes(note,zim,path,tool) VALUES (?,?,?,?)',('Spaceship engines and people','test.zim','V/escape.mp4','whisper.cpp'))
        self.assertIn('ZIM media',zim_index.search('spaceship',db_path=db)[0]['source'])
    def test_refuse_symlinked_zim(self):
        file=self.folder/'test.zim';file.write_bytes(b'z')
        alias=self.folder/'alias.zim';alias.symlink_to(file)
        with self.assertRaises(ValueError):zim_index.index_archive(alias,db_path=self.folder/'x.sqlite',reader_factory=FakeArchive)
    def test_repeated_zim_refresh_clears_old_records(self):
        file=self.folder/'test.zim';file.write_bytes(b'z')
        db=self.folder/'index.sqlite'
        zim_index.index_archive(file,db,max_entries=10,reader_factory=FakeArchive)
        file.write_bytes(b'new data')
        zim_index.index_archive(file,db,max_entries=10,reader_factory=FakeArchive)
        self.assertEqual(len(zim_index.search('oxygen',db)),1)

class OfflineMapTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name);self.map=self.folder/'mini.mbtiles'
        with sqlite3.connect(self.map) as db:
            db.execute('CREATE TABLE metadata(name,value)');db.execute('CREATE TABLE tiles(zoom_level,tile_column,tile_row,tile_data)')
            db.executemany('INSERT INTO metadata VALUES (?,?)',[('name','Test world map'),('format','png'),('minzoom','0'),('maxzoom','5')])
            db.execute('INSERT INTO tiles VALUES (?,?,?,?)',(1,1,1,b'\x89PNG\r\n\x1a\nMOCK_TILE'))
        self.config=self.folder/'mapconf.json'
        patch=mock.patch.object(offline_maps,'CONFIG',self.config);patch.start();self.addCleanup(patch.stop)
    def test_mbtiles_tms_flip_and_validation(self):
        self.assertTrue(offline_maps.enroll_tiles(str(self.map))['tiles_ready'])
        blob,mime=offline_maps.tile(1,1,0)
        self.assertEqual(mime,'image/png');self.assertIn(b'MOCK_TILE',blob)
        with self.assertRaises(FileNotFoundError):offline_maps.tile(1,1,1)
        with self.assertRaises(ValueError):offline_maps.tile(27,0,0)
    def test_vector_tiles_enrolled_without_online_assets(self):
        with sqlite3.connect(self.map) as db:
            db.execute("UPDATE metadata SET value='pbf' WHERE name='format'")
            db.execute("UPDATE tiles SET tile_data=?",(b'\x1f\x8b'+b'ABCDEF',))
        result=offline_maps.enroll_tiles(str(self.map))
        self.assertTrue(result['vector_tiles_ready'])
        blob,mime=offline_maps.tile(1,1,0)
        self.assertEqual(mime,'application/x-protobuf')
        self.assertTrue(blob.startswith(b'\x1f\x8b'))
    def test_route_is_local_and_geojson(self):
        graph=self.folder/'poland.osrm';graph.write_bytes(b'x');Path(str(graph)+'.mldgr').write_bytes(b'mld')
        offline_maps.enroll_router(str(graph))
        response={'code':'Ok','routes':[{'distance':1200,'duration':85,
                 'geometry':{'type':'LineString','coordinates':[[17.0,51.1],[17.03,51.12]]},
                 'legs':[{'steps':[{'distance':1200,'name':'Street','maneuver':{'type':'turn'}}]}]}]}
        class Stub:
            def __enter__(self):return self
            def __exit__(self,*args):return None
            def read(self,*args):return json.dumps(response).encode()
        def opening(uri,timeout):
            self.assertTrue(uri.startswith('http://127.0.0.1:5000/route/v1/driving/'))
            return Stub()
        with mock.patch.object(offline_maps.urllib.request,'urlopen',side_effect=opening):
            result=offline_maps.route(51.1,17.0,51.12,17.03)
        self.assertEqual(result['distance_m'],1200)
        self.assertEqual(len(result['coordinates']),2)
    def test_requires_router_without_external_api(self):
        with self.assertRaises(FileNotFoundError):offline_maps.route(51.1,17,51.12,17.03)
    def test_import_local_gazetteer(self):
        csv_path=self.folder/'places.csv'
        csv_path.write_text('name,lat,lon\nWrocław,51.1079,17.0385\nGdańsk,54.352,18.646\nNope,900,1000\n',encoding='utf-8')
        db=self.folder/'places.sqlite'
        result=offline_maps.import_places(str(csv_path),db)
        self.assertEqual(result['imported'],2)
        hit=offline_maps.find_places('Wrocław',db)
        self.assertEqual(len(hit),1);self.assertAlmostEqual(hit[0]['lon'],17.0385)
        self.assertFalse(offline_maps.find_places('nowhere',db))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        path=Path(temp.name)
        for prop,name in [('WATCH','watch.json'),('PLAN','plan.json'),('MARK','mark.json')]:
            p=mock.patch.object(recovery_watch,prop,path/name);p.start();self.addCleanup(p.stop)
    def test_detect_three_abandoned_boots_without_touching_zfs(self):
        with mock.patch.object(recovery_watch,'make_plan',return_value={'safe_candidate':'rpool/ROOT/root@healthy'}) as plan:
            for k in range(1,5):recovery_watch.watch_boot(boot_id='boot'+str(k))
        state=recovery_watch._read(recovery_watch.WATCH)
        self.assertEqual(state['consecutive_incomplete_boots'],3)
        self.assertTrue(state['recovery_plan_ready'])
        self.assertEqual(recovery_watch._read(recovery_watch.PLAN)['safe_candidate'],'rpool/ROOT/root@healthy')
        plan.assert_called_once()
        self.assertEqual(recovery_watch.watch_boot(boot_id='boot4')['consecutive_incomplete_boots'],3)
        assert not list(recovery_watch.WATCH.parent.glob('*.zfs'))
    def test_health_clears_failed_boot_count(self):
        recovery_watch.watch_boot(boot_id='a');recovery_watch.watch_boot(boot_id='b')
        status=recovery_watch.mark_healthy(boot_id='b',health_check=lambda:{'pool_guid':'456'})
        self.assertTrue(status['ok']);self.assertEqual(status['consecutive_incomplete_boots'],0)
    def test_fail_safe_marked_snapshot(self):
        with mock.patch.object(recovery_watch,'mounted_root',return_value='rpool/ROOT/ubuntu'),\
             mock.patch.object(recovery_watch,'pool_guid',return_value='pool1'),\
             mock.patch.object(recovery_watch,'snapshots',return_value=['rpool/ROOT/ubuntu@good','rpool/ROOT/ubuntu@new']):
            recovery_watch._atomic_write(recovery_watch.MARK,{'snapshot':'rpool/ROOT/ubuntu@good','pool_guid':'pool1'})
            result=recovery_watch.make_plan('crash')
        self.assertEqual(result['safe_candidate'],'rpool/ROOT/ubuntu@good')
        self.assertFalse(result['automatic_live_root_rollback'])
    def test_receive_rejects_same_pool_before_any_change(self):
        snap='rpool/ROOT/ubuntu@good'
        with mock.patch.object(recovery_watch,'snapshots',return_value=[snap]),\
             mock.patch.object(recovery_watch,'pool_guid',return_value='same'),\
             mock.patch.object(recovery_watch.subprocess,'run',return_value=mock.Mock(returncode=0,stdout='same\n')):
            with self.assertRaisesRegex(ValueError,'same ZFS pool'):
                recovery_watch.check_receive(snap,'backup/recovered/root')

if __name__=='__main__':unittest.main()

class MediaCaptionTests(unittest.TestCase):
    def setUp(self):
        from media_enrich import index_media_notes
        import media_enrich
        self.media=media_enrich
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)
        self.zim=self.folder/'collection.zim';self.zim.write_bytes(b'mock zim')
        self.db=self.folder/'text.sqlite'
        with zim_index.connect(self.db) as db:
            db.execute('INSERT INTO media(zim,path,mime,title,article,alt,hash) VALUES (?,?,?,?,?,?,?)',
                       ('collection.zim','I/solar.png','image/png','solar panel','A/Solar','a panel',None))

    def test_caption_enters_linked_media_rag(self):
        import types
        class Archive:
            def __init__(self,path):pass
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def get_entry_by_path(self,path):return FakeEntry(path,'photo','image/png',b'\x89PNG\r\n\x1a\nexample')
        reader=types.ModuleType('libzim.reader');reader.Archive=Archive
        lib=types.ModuleType('libzim');lib.reader=reader
        with mock.patch.dict(sys.modules,{'libzim':lib,'libzim.reader':reader}),mock.patch.object(self.media,'_caption',return_value='Solar photovoltaic cells on a rooftop') as caption:
            result=self.media.index_media_notes(str(self.zim),limit=2,db_path=self.db)
        self.assertEqual(result['indexed'],1)
        hit=zim_index.search('photovoltaic',db_path=self.db)
        self.assertEqual(len(hit),1)
        self.assertIn('/collection/A/Solar',hit[0]['link'])
        caption.assert_called_once()
