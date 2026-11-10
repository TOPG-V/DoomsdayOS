"""Offline multi-stop, crossing-free strict pedestrian, and vector-map regression."""
import io,json,sqlite3,sys,tempfile,unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
import trip_planner,offline_maps,ai_gateway

class Responder:
    def __init__(self,content):self.content=content
    def __enter__(self):return self
    def __exit__(self,*a):return False
    def read(self,*a):return json.dumps(self.content).encode()

class MultiStopTripTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.base=Path(temp.name)
        self.car=self.base/'roads.osrm';self.car.write_bytes(b'data')
        self.foot=self.base/'walking.osrm';self.foot.write_bytes(b'data')
        self.config={'router':str(self.car),'walking_router':str(self.foot)}
        self.points=[{'lat':51.1,'lon':17.0},{'lat':52.0,'lon':17.7},{'lat':52.4,'lon':18.4},
                     {'lat':53.3,'lon':18.8},{'lat':54.3,'lon':18.9}]
    def result(self):
        return {'code':'Ok','routes':[{'distance':140000,'duration':7700,
            'geometry':{'type':'LineString','coordinates':[[17,51.1],[17.7,52],[18.4,52.4],[18.8,53.3],[18.9,54.3]]},
            'legs':[{'distance':35000,'duration':1900,'steps':[{'maneuver':{'type':'turn'},'name':'Road','distance':35000}]} for _ in range(4)]}]}
    def test_max_three_middle_stops(self):
        seen=[]
        def opener(url,timeout):seen.append(url);return Responder(self.result())
        result=trip_planner.plan(self.points,'car',cfg=self.config,opener=opener)
        self.assertEqual(len(result['stops']),5)
        self.assertEqual(len(result['legs']),4)
        self.assertEqual(len(seen),1)
        self.assertTrue(seen[0].startswith('http://127.0.0.1:5000/route/v1/driving/'))
        self.assertEqual(seen[0].count(';'),4)
    def test_foot_uses_only_local_walking_profile(self):
        seen=[]
        def opener(url,timeout):
            seen.append(url);response=self.result();response['routes'][0]['legs']=response['routes'][0]['legs'][:1]
            return Responder(response)
        result=trip_planner.plan(self.points[:2],'walk',cfg=self.config,opener=opener)
        self.assertEqual(result['mode'],'foot')
        self.assertIn('127.0.0.1:5001/route/v1/foot/',seen[0])
    def test_refuses_more_than_three_stops(self):
        with self.assertRaisesRegex(ValueError,'three intermediate'):trip_planner.plan(self.points+[self.points[0]],cfg=self.config)
    def test_rejects_unsafe_coordinates_before_network(self):
        with self.assertRaises(ValueError):trip_planner.plan([{'lat':0,'lon':0},{'lat':91,'lon':0}],cfg=self.config)
    def test_no_route_is_not_drawn_over_ocean(self):
        with self.assertRaisesRegex(ValueError,'No continuous offline route'):
            trip_planner.plan([self.points[0],self.points[1]],'car',cfg=self.config,
                              opener=lambda url,timeout:Responder({'code':'NoRoute','routes':[]}))
    def test_missing_local_route_data_is_explicit(self):
        with self.assertRaises(FileNotFoundError):trip_planner.plan(self.points[:2],'car',cfg={})
        with self.assertRaises(FileNotFoundError):trip_planner.plan(self.points[:2],'walk',cfg={})
    def test_gateway_route_multi_dispatch(self):
        with mock.patch.object(ai_gateway,'plan_trip',return_value={'mode':'car'}) as routed:
            response=ai_gateway.dispatch({'action':'route_multi','points':self.points,'mode':'car'})
        self.assertEqual(response['mode'],'car')
        routed.assert_called_once()
    def test_gateway_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):ai_gateway.dispatch({'action':'route_multi','points':self.points,'mode':'airplane'})

class StrictOffroadTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.base=Path(tmp.name)
        self.json=self.base/'trails.geojson';self.db=self.base/'safe.sqlite'
    def write(self,roads=True,trails=True):
        feats=[]
        if roads:feats.append({'type':'Feature','properties':{'highway':'residential'},
                              'geometry':{'type':'LineString','coordinates':[[0,-.001],[.02,-.001]]}})
        if trails:
            # Safe connected trail on the other side of the road.
            feats.append({'type':'Feature','properties':{'highway':'path'},
                          'geometry':{'type':'LineString','coordinates':[[0,.002],[.002,.002],[.004,.002],[.006,.002]]}})
            # Forbidden crossing near x=.01, through road lat=-.001
            feats.append({'type':'Feature','properties':{'highway':'footway','footway':'crossing'},
                          'geometry':{'type':'LineString','coordinates':[[.01,-.004],[.01,.004]]}})
            feats.append({'type':'Feature','properties':{'highway':'path'},
                          'geometry':{'type':'LineString','coordinates':[[.012,-.004],[.012,.003]]}})
        self.json.write_text(json.dumps({'type':'FeatureCollection','features':feats}))
    def test_safe_graph_excludes_road_crossings(self):
        self.write();r=trip_planner.import_trails(str(self.json),self.db)
        self.assertTrue(r['trail_edges']>=3)
        self.assertGreaterEqual(r['rejected_crossing_edges'],1)
        trip=[{'lat':.002,'lon':0},{'lat':.002,'lon':.002},{'lat':.002,'lon':.006}]
        result=trip_planner.plan(trip,'offroad',trail_db=self.db)
        self.assertEqual(len(result['legs']),2)
        self.assertEqual(result['mode'],'offroad')
        self.assertFalse(any(x[1]<0 for x in result['coordinates']))
    def test_strict_trail_graph_does_not_allow_disconnected_shortcuts(self):
        self.write();trip_planner.import_trails(str(self.json),self.db)
        with self.assertRaisesRegex(ValueError,'mapped off-road trail|disconnected'):
            trip_planner.plan([{'lat':.002,'lon':.001},{'lat':-.002,'lon':.01}],'offroad',trail_db=self.db)
    def test_refuses_dataset_without_road_geometry(self):
        self.write(roads=False)
        with self.assertRaisesRegex(ValueError,'requires mapped motor-road'):trip_planner.import_trails(str(self.json),self.db)
    def test_reject_crossing_geometry(self):
        self.assertTrue(trip_planner.intersects((0,0),(1,1),(0,1),(1,0)))
        self.assertFalse(trip_planner.intersects((0,0),(1,0),(0,1),(1,1)))
        self.assertTrue(trip_planner.intersects((0,0),(1,0),(1,0),(2,0)))
    def test_road_rejects_direct_footpath_crossing(self):
        self.json.write_text(json.dumps({'type':'FeatureCollection','features':[
            {'type':'Feature','properties':{'highway':'primary'},'geometry':{'type':'LineString','coordinates':[[0,0],[.01,0]]}},
            {'type':'Feature','properties':{'highway':'path'},'geometry':{'type':'LineString','coordinates':[[.005,-.01],[.005,.01]]}}]}))
        with self.assertRaisesRegex(ValueError,'No safe trail edges'):trip_planner.import_trails(str(self.json),self.db)

if __name__=='__main__':unittest.main()
