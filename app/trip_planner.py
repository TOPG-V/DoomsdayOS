#!/usr/bin/env python3
"""Offline multi-stop routing: car on roads, pedestrian, or STRICT trail-only.

- Up to three intermediate waypoints (five coordinates total).
- Car: explicitly enrolled OSRM driving graph, loopback port 5000.
- Walk: explicitly enrolled OSRM walking graph, loopback port 5001.
- Strict off-road: graph built from supplied local GeoJSON walking trails AND
  intersecting motor-road features. NEVER substitutes a direct line route.
- No Internet calls; missing or disconnected networks cause a clear refusal.
"""
from __future__ import annotations
import argparse,collections,heapq,json,math,os,sqlite3,tempfile,urllib.error,urllib.parse,urllib.request
from pathlib import Path
from offline_maps import config,validate_router

TRAIL_DB=Path(os.environ.get('DOOMSDAY_TRAIL_DB','/var/lib/doomsday-vault/offroad-trails.sqlite'))
MAX_POINTS=5
MAX_GRAPH_BYTES=150*1024*1024
MAX_FEATURES=140000
MAX_EDGES=250000
ROAD_TYPES=frozenset('motorway motorway_link trunk trunk_link primary primary_link secondary secondary_link tertiary tertiary_link residential unclassified living_street road service track cycleway'.split())
TRAIL_TYPES=frozenset('path footway steps bridleway'.split())


def point(raw):
    if not isinstance(raw,dict) or set(raw)-{'lat','lon','name'}:raise ValueError('Use points containing lat and lon')
    if type(raw.get('lat')) not in (int,float) or type(raw.get('lon')) not in (int,float):raise ValueError('Numeric lat/lon required')
    lat,lon=float(raw['lat']),float(raw['lon'])
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90<=lat<=90 or not -180<=lon<=180:
        raise ValueError('Invalid geographic coordinates')
    return {'lat':lat,'lon':lon,'name':str(raw.get('name',''))[:80]}


def points_check(points):
    if not isinstance(points,list) or not 2<=len(points)<=MAX_POINTS:
        raise ValueError('Provide start, destination, and up to three intermediate stops (5 points maximum)')
    return [point(p) for p in points]


def _haversine(a,b):
    R=6371000.0;p1,p2=math.radians(a[0]),math.radians(b[0]);dphi=p2-p1;dl=math.radians(b[1]-a[1])
    k=math.sin(dphi/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R*2*math.asin(min(1,math.sqrt(k)))


def _local_osrm(points,profile,graph,port,opener=None):
    if not graph or not Path(graph).is_file():
        raise FileNotFoundError('No verified offline '+profile+' network; enroll a prepared local routing graph first')
    if opener is None:opener=urllib.request.urlopen
    coords=';'.join(f"{p['lon']:.6f},{p['lat']:.6f}" for p in points)
    url=f'http://127.0.0.1:{port}/route/v1/{profile}/{coords}?overview=full&geometries=geojson&steps=true'
    try:
        with opener(url,timeout=30) as resp:result=json.loads(resp.read(10*1024*1024))
    except (OSError,TimeoutError,urllib.error.URLError) as exc:
        raise RuntimeError('The local '+profile+' route engine is not running (no Internet fallback)') from exc
    if result.get('code')!='Ok' or not result.get('routes'):
        raise ValueError('No continuous offline route. The road/trail network may be disconnected, outside coverage, or separated by ocean. No straight-line route was generated.')
    best=result['routes'][0]
    if len(best.get('legs',[]))!=len(points)-1:
        raise ValueError('Offline route did not contain every requested intermediate destination')
    geometry=best.get('geometry',{})
    if geometry.get('type')!='LineString':raise ValueError('Unsupported route geometry')
    coords=geometry.get('coordinates',[])
    if not isinstance(coords,list) or len(coords)>120000:raise ValueError('Route geometry too large')
    if any(not isinstance(p,list) or len(p)<2 or not all(type(v) in (int,float) and math.isfinite(v) for v in p[:2]) for p in coords):
        raise ValueError('Invalid route geometry from the local router')
    legs=[]
    for i,leg in enumerate(best.get('legs',[])):
        if i>=MAX_POINTS-1:break
        steps=[]
        for item in leg.get('steps',[])[:100]:
            steps.append({'instruction':str(item.get('maneuver',{}).get('type','continue'))[:60],
                          'name':str(item.get('name',''))[:130], 'distance_m':float(item.get('distance',0))})
        legs.append({'from':i,'to':i+1,'distance_m':float(leg.get('distance',0)),
                     'duration_s':float(leg.get('duration',0)), 'steps':steps})
    return {'mode':profile,'engine':'local-osrm','distance_m':float(best['distance']),
            'duration_s':float(best['duration']),'coordinates':coords,'legs':legs,
            'stops':points,'note':'Offline travel estimate; no live traffic or ocean-crossing transport.'}


def _orientation(a,b,c):
    return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])


def _between(a,b,c):
    return min(a[0],b[0])-1e-10<=c[0]<=max(a[0],b[0])+1e-10 and min(a[1],b[1])-1e-10<=c[1]<=max(a[1],b[1])+1e-10


def intersects(a,b,c,d):
    """2D closed-segment test: conservatively forbids touching roads too."""
    o1,o2,o3,o4=_orientation(a,b,c),_orientation(a,b,d),_orientation(c,d,a),_orientation(c,d,b)
    if ((o1>1e-12 and o2<-1e-12) or (o1<-1e-12 and o2>1e-12)) and ((o3>1e-12 and o4<-1e-12) or (o3<-1e-12 and o4>1e-12)):
        return True
    return any(abs(o)<1e-12 and _between(v,w,t) for o,v,w,t in [(o1,a,b,c),(o2,a,b,d),(o3,c,d,a),(o4,c,d,b)])


def _grid_cells(a,b,spacing=.015):
    lo,hi=sorted((int(math.floor(a[0]/spacing)),int(math.floor(b[0]/spacing))))
    bot,top=sorted((int(math.floor(a[1]/spacing)),int(math.floor(b[1]/spacing))))
    if (hi-lo+1)*(top-bot+1)>2500:raise ValueError('A map segment is too broad for bounded local indexing')
    for x in range(lo,hi+1):
        for y in range(bot,top+1):yield x,y


def _coord(pair):
    if not isinstance(pair,(list,tuple)) or len(pair)<2 or any(type(v) not in (float,int) or not math.isfinite(v) for v in pair[:2]):return None
    lon,lat=pair[:2]
    if not(-180<=lon<=180 and -90<=lat<=90):return None
    return (round(float(lon),6),round(float(lat),6))


def _paths(feature):
    g=feature.get('geometry') or {}
    if g.get('type')=='LineString':return [g.get('coordinates') or []]
    if g.get('type')=='MultiLineString':return g.get('coordinates') or []
    return []


def import_trails(path, db_path=TRAIL_DB):
    """Import OSM-tagged GeoJSON; reject/no route if road features absent.

    Walkable edges touching road geometries are removed. Data cannot prove that
    unmapped roads do not exist; callers see this explicit limitation.
    """
    file=Path(path)
    if not file.is_absolute() or file.is_symlink() or file.suffix.lower() not in ('.geojson','.json') or not file.is_file():
        raise ValueError('Provide an existing absolute non-symlink local GeoJSON')
    if file.stat().st_size>MAX_GRAPH_BYTES:raise ValueError('GeoJSON is too large; split into regional extracts')
    data=json.loads(file.read_text(encoding='utf-8'))
    if data.get('type')!='FeatureCollection' or not isinstance(data.get('features'),list) or len(data['features'])>MAX_FEATURES:
        raise ValueError('Expected a bounded GeoJSON FeatureCollection')
    roads=[];trail=[]
    for feat in data['features']:
        if not isinstance(feat,dict):continue
        prop=feat.get('properties') or {}; tags=prop.get('tags') or {}
        klass=str(prop.get('highway',tags.get('highway',''))).lower()
        is_road=klass in ROAD_TYPES
        is_trail=klass in TRAIL_TYPES and not any(str(prop.get(k,tags.get(k,''))).lower() in ('yes','crossing','sidewalk','marked') for k in ('crossing','sidewalk','footway'))
        if not (is_road or is_trail):continue
        for path_coords in _paths(feat):
            previous=None
            for raw in path_coords:
                cur=_coord(raw)
                if cur is not None and previous is not None and cur!=previous:
                    if is_road:roads.append((previous,cur))
                    if is_trail:trail.append((previous,cur))
                    if len(trail)>MAX_EDGES or len(roads)>MAX_EDGES:raise ValueError('Regional network too large; split GeoJSON')
                previous=cur
    if not roads:raise ValueError('Strict road avoidance requires mapped motor-road geometry in the supplied region')
    if not trail:raise ValueError('No explicitly tagged off-road trails were found')
    spatial=collections.defaultdict(list)
    for i,(a,b) in enumerate(roads):
        for cell in _grid_cells(a,b):spatial[cell].append(i)
    nodes={};valid=[];removed=0
    for a,b in trail:
        nearby=set()
        for cell in _grid_cells(a,b):nearby.update(spatial.get(cell,()))
        if any(intersects(a,b,*roads[i]) for i in nearby):removed+=1;continue
        for p in (a,b):
            if p not in nodes:nodes[p]=len(nodes)+1
        dist=_haversine((a[1],a[0]),(b[1],b[0]))
        if dist>0:valid.append((nodes[a],nodes[b],round(dist,2)))
    if not valid:raise ValueError('No safe trail edges remain after road crossings were removed')
    dest=Path(db_path)
    dest.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.offroad-',suffix='.sqlite',dir=dest.parent);os.close(fd)
    try:
        with sqlite3.connect(tmp) as db:
            db.execute('CREATE TABLE nodes(id INTEGER PRIMARY KEY,lat REAL,lon REAL)')
            db.execute('CREATE TABLE edges(a INTEGER,b INTEGER,meters REAL)')
            db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)')
            db.executemany('INSERT INTO nodes VALUES(?,?,?)',[(id,p[1],p[0]) for p,id in nodes.items()])
            db.executemany('INSERT INTO edges VALUES(?,?,?)',valid)
            db.executemany('INSERT INTO metadata VALUES(?,?)',[('roads_examined',str(len(roads))),('trails_rejected',str(removed)),('graph_source',str(file))])
            db.commit()
        os.chmod(tmp,0o600);os.replace(tmp,dest)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return {'trail_edges':len(valid),'trail_nodes':len(nodes),'road_edges_examined':len(roads),'rejected_crossing_edges':removed,'database':str(dest)}


def _trail_routing(points,db_path=TRAIL_DB):
    path=Path(db_path)
    if not path.is_file():raise FileNotFoundError('Strict off-road trail database absent: import a regional GeoJSON containing trails AND roads')
    with sqlite3.connect('file:'+urllib.parse.quote(str(path))+'?mode=ro',uri=True) as db:
        nodes={int(i):(float(lat),float(lon)) for i,lat,lon in db.execute('SELECT id,lat,lon FROM nodes')}
        adj=collections.defaultdict(list)
        for a,b,meters in db.execute('SELECT a,b,meters FROM edges'):
            adj[a].append((b,meters));adj[b].append((a,meters))
    if not nodes:raise ValueError('No walkable trail network')
    ids=[]
    for p in points:
        node=min(nodes,key=lambda i:_haversine(nodes[i],(p['lat'],p['lon'])))
        distance=_haversine(nodes[node],(p['lat'],p['lon']))
        if distance>120:
            raise ValueError(f"Stop near {p.get('name') or 'coordinates'} is {round(distance)} m from a mapped off-road trail; strict mode refuses a straight line or road approach")
        ids.append(node)
    segments=[];all_coords=[];total=0
    for index,(a,b) in enumerate(zip(ids,ids[1:])):
        dist={a:0.0};previous={};heap=[(0.0,a)]
        while heap:
            length,here=heapq.heappop(heap)
            if length>dist.get(here,math.inf):continue
            if here==b:break
            for nbr,meters in adj[here]:
                cost=length+meters
                if cost<dist.get(nbr,math.inf):dist[nbr]=cost;previous[nbr]=here;heapq.heappush(heap,(cost,nbr))
        if b not in dist:raise ValueError('Strict off-road trail sections are disconnected or would need a road crossing; no unsafe shortcut generated')
        chain=[b]
        while chain[-1]!=a:chain.append(previous[chain[-1]])
        chain.reverse();geometry=[[nodes[n][1],nodes[n][0]] for n in chain]
        segments.append({'from':index,'to':index+1,'distance_m':round(dist[b],1),'duration_s':round(dist[b]/1.24),
                         'steps':[{'instruction':'Follow mapped trail','name':'off-road path','distance_m':round(dist[b])}]})
        if all_coords:geometry=geometry[1:]
        all_coords.extend(geometry);total+=dist[b]
    return {'mode':'offroad','engine':'local-strict-trails','distance_m':round(total,1),'duration_s':round(total/1.24),
            'coordinates':all_coords,'legs':segments,'stops':points,
            'note':'Strictly mapped trail-only; crossings with the PROVIDED mapped road geometries are removed. Unmapped hazards/roads or legal access are not guaranteed.'}


def plan(points,mode='car',cfg=None,opener=None,trail_db=TRAIL_DB):
    pts=points_check(points)
    if mode not in ('car','walk','offroad'):raise ValueError('Choose car, walk, or strict offroad walking')
    if mode=='offroad':return _trail_routing(pts,trail_db)
    cfg=config() if cfg is None else cfg
    if mode=='car':return _local_osrm(pts,'driving',cfg.get('router'),5000,opener)
    return _local_osrm(pts,'foot',cfg.get('walking_router'),5001,opener)


def main():
    parser=argparse.ArgumentParser(description='DoomsdayOS offline multi-stop trip planner')
    parser.add_argument('command',choices=['import-trails','route','status'])
    parser.add_argument('args',nargs='*')
    parser.add_argument('--mode',choices=['car','walk','offroad'],default='car')
    parsed=parser.parse_args()
    if parsed.command=='import-trails':
        if len(parsed.args)!=1:parser.error('import-trails requires an absolute .geojson path')
        print(json.dumps(import_trails(parsed.args[0]),indent=2));return
    if parsed.command=='status':
        cfg=config();print(json.dumps({'car_graph':bool(cfg.get('router') and Path(cfg['router']).exists()),
                                      'walk_graph':bool(cfg.get('walking_router') and Path(cfg['walking_router']).exists()),
                                      'offroad_graph':TRAIL_DB.exists(),'max_intermediate_stops':3},indent=2));return
    if len(parsed.args)<2:parser.error('route needs at least two lat,lon arguments')
    coords=[]
    for text in parsed.args:
        bits=text.split(',')
        if len(bits)!=2:parser.error('Enter lat,lon pairs')
        coords.append({'lat':float(bits[0]),'lon':float(bits[1])})
    print(json.dumps(plan(coords,mode=parsed.mode),indent=2))

if __name__=='__main__':main()
