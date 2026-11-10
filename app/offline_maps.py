#!/usr/bin/env python3
"""Offline raster/vector MBTiles serving and locally prepared OSRM route adapter.

No public geolocation, map, or routing APIs are called. An MBTiles map is NOT
an OSRM routing graph; the user enrolls each existing dataset independently.
"""
from __future__ import annotations
import argparse, csv, json, math, os, re, sqlite3, subprocess, tempfile, urllib.error, urllib.parse, urllib.request
from pathlib import Path

CONFIG=Path(os.environ.get('DOOMSDAY_MAP_CONFIG','/etc/doomsday-vault/maps.json'))
ROUTER='http://127.0.0.1:5000'
MAX_TILE=2*1024*1024
PLACES=Path(os.environ.get('DOOMSDAY_PLACES_DB','/var/lib/doomsday-vault/offline-places.sqlite'))


def _write_config(config):
    CONFIG.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(dir=CONFIG.parent,prefix='.maps-')
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w') as out:
            json.dump(config,out,indent=2);out.write('\n');out.flush();os.fsync(out.fileno())
        os.replace(temp,CONFIG)
    finally:
        if os.path.exists(temp):os.unlink(temp)


def config():
    try:return json.loads(CONFIG.read_text())
    except (FileNotFoundError,ValueError):return {}


def _read_tiles(path):
    uri='file:'+urllib.parse.quote(str(path))+'?mode=ro&immutable=1'
    return sqlite3.connect(uri,uri=True,timeout=6)


def validate_mbtiles(filename):
    orig=Path(filename)
    if not orig.is_absolute() or orig.is_symlink():raise ValueError('Provide an absolute non-symlink MBTiles path')
    path=orig.resolve(strict=True)
    if path.suffix.lower()!='.mbtiles' or not path.is_file():raise ValueError('Expected an existing .mbtiles file')
    with _read_tiles(path) as db:
        metadata=dict(db.execute('SELECT name,value FROM metadata LIMIT 100').fetchall())
        db.execute('SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles LIMIT 1').fetchone()
    fmt=metadata.get('format','').lower()
    if fmt not in ('png','jpg','jpeg','webp','pbf','mvt'):
        raise ValueError('Supported offline MBTiles: PNG/JPEG/WEBP or OpenMapTiles PBF/MVT vector tiles.')
    return path,metadata


def enroll_tiles(filename):
    path,meta=validate_mbtiles(filename)
    cfg=config();cfg['tiles']=str(path);cfg['format']=meta['format'].lower()
    cfg['name']=meta.get('name',path.name);cfg['minzoom']=int(meta.get('minzoom','0'))
    cfg['maxzoom']=int(meta.get('maxzoom','18'))
    cfg['bounds']=meta.get('bounds','-180,-85,180,85')
    _write_config(cfg)
    return status()


def enroll_terrain(filename):
    # Only an explicitly supplied terrain-RGB raster PNG is accepted.
    path,meta=validate_mbtiles(filename)
    if meta.get('format','').lower()!='png':raise ValueError('Terrain-RGB elevation must be a PNG MBTiles file')
    cfg=config()
    cfg['terrain_tiles']=str(path);cfg['terrain_maxzoom']=int(meta.get('maxzoom','13'))
    _write_config(cfg);return status()


def validate_router(prefix):
    p=Path(prefix)
    if not p.is_absolute() or p.is_symlink() or p.suffix!='.osrm':
        raise ValueError('Use an absolute .osrm prefix from a prepared OSRM routing graph')
    p=p.resolve(strict=True)
    if not p.is_file():raise ValueError('Missing .osrm file')
    # MLD partitions/customization required, unless a CH preparation exists.
    algo='mld' if Path(str(p)+'.mldgr').is_file() else ('ch' if Path(str(p)+'.hsgr').is_file() else None)
    if not algo:raise ValueError('OSRM dataset not prepared (missing .osrm.mldgr or .osrm.hsgr)')
    return p,algo


def enroll_walking_router(prefix):
    p,algo=validate_router(prefix)
    cfg=config();cfg.update({'walking_router':str(p),'walking_algorithm':algo})
    _write_config(cfg);return status()


def enroll_router(prefix):
    p,algo=validate_router(prefix)
    cfg=config();cfg.update({'router':str(p),'algorithm':algo})
    _write_config(cfg);return status()


def tile(z,x,y,kind='map'):
    cfg=config()
    if kind not in ('map','terrain'):raise ValueError('Unknown tile type')
    key='terrain_tiles' if kind=='terrain' else 'tiles'
    if key not in cfg:raise FileNotFoundError('No offline MBTiles enrolled for '+kind)
    if not all(isinstance(v,int) for v in (z,x,y)) or not 0<=z<=23 or not 0<=x<2**z or not 0<=y<2**z:
        raise ValueError('Tile coordinates out of range')
    path,meta=validate_mbtiles(cfg[key])
    with _read_tiles(path) as db:
        row=db.execute('SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=? LIMIT 1',
                       (z,x,(2**z-1-y))).fetchone()
    if not row:raise FileNotFoundError('Tile not present in offline map')
    blob=bytes(row[0]);fmt=meta['format'].lower()
    if len(blob)>MAX_TILE or len(blob)<8:raise ValueError('Unreasonable tile size')
    if fmt=='png' and not blob.startswith(b'\x89PNG\r\n\x1a\n'):raise ValueError('Invalid PNG tile')
    if fmt in ('jpg','jpeg') and not blob.startswith(b'\xff\xd8'):raise ValueError('Invalid JPEG tile')
    if fmt=='webp' and not (blob.startswith(b'RIFF') and blob[8:12]==b'WEBP'):raise ValueError('Invalid WEBP tile')
    # PBF MBTiles are often gzipped. The web API sends Content-Encoding:gzip.
    return blob,{'png':'image/png','jpg':'image/jpeg','jpeg':'image/jpeg','webp':'image/webp',
                 'pbf':'application/x-protobuf','mvt':'application/x-protobuf'}[fmt]


def route(lat1,lon1,lat2,lon2):
    cfg=config()
    if not cfg.get('router'):
        raise FileNotFoundError('No prepared offline OSRM routing graph enrolled')
    if not Path(cfg['router']).is_file():raise FileNotFoundError('Previously enrolled OSRM graph is disconnected')
    for lat,lon in [(lat1,lon1),(lat2,lon2)]:
        if not all(math.isfinite(x) for x in (lat,lon)) or not(-90<=lat<=90 and -180<=lon<=180):
            raise ValueError('Coordinates out of WGS84 bounds')
    # OSRM uses lon,lat ordering; never contact public routing services.
    uri=f'{ROUTER}/route/v1/driving/{lon1:.6f},{lat1:.6f};{lon2:.6f},{lat2:.6f}?overview=full&geometries=geojson&steps=true'
    try:
        with urllib.request.urlopen(uri,timeout=10) as r:
            answer=json.loads(r.read(4*1024*1024))
    except (OSError,urllib.error.URLError) as e:
        raise RuntimeError('Local osrm-routed unavailable; run the Doomsday OSRM service') from e
    if answer.get('code')!='Ok' or not answer.get('routes'):
        raise ValueError('No route in the enrolled region; check road network coverage')
    best=answer['routes'][0];geometry=best['geometry']
    if geometry.get('type')!='LineString':raise ValueError('Unexpected OSRM geometry')
    # Bound response rather than passing untrusted unlimited coordinates to browser.
    coords=geometry.get('coordinates',[])
    if len(coords)>25000:coords=coords[::max(1,math.ceil(len(coords)/25000))]
    steps=[{'distance_m':int(s['distance']),'name':s.get('name','')[:160],
            'instruction':s.get('maneuver',{}).get('type','')[:60]}
           for leg in best.get('legs',[]) for s in leg.get('steps',[])][:100]
    return {'mode':'offline','profile':'driving','distance_m':best['distance'],'duration_s':best['duration'],
            'coordinates':coords,'steps':steps,'note':'Offline graph only, no live traffic; distances are OSRM estimates.'}



def import_places(csv_file,db_path=PLACES,max_rows=1000000):
    """Offline gazetteer from user-supplied UTF-8 CSV: name,lat,lon.

    Existing maps and original CSV are untouched. Uses modest batch sizes,
    no online geocoder, and stores coordinates for route selection.
    """
    f=Path(csv_file)
    if not f.is_absolute() or f.is_symlink() or not f.is_file() or f.stat().st_size>500*1024**2:
        raise ValueError('Provide an existing absolute CSV, no symlinks, 500 MB maximum')
    if not 1<=max_rows<=2000000:raise ValueError('Invalid row cap')
    db_path=Path(db_path);db_path.parent.mkdir(parents=True,exist_ok=True)
    imported=0
    with sqlite3.connect(db_path) as db, f.open(encoding='utf-8-sig',newline='') as source:
        db.execute('CREATE TABLE IF NOT EXISTS place_data (id INTEGER PRIMARY KEY,name TEXT NOT NULL,lat REAL,lon REAL)')
        db.execute('CREATE VIRTUAL TABLE IF NOT EXISTS place_fts USING fts5(name, content="place_data", content_rowid="id")')
        reader=csv.DictReader(source)
        if not {'name','lat','lon'}.issubset(set(reader.fieldnames or [])):
            raise ValueError('CSV must have name,lat,lon header columns')
        db.execute('DELETE FROM place_data')
        for item in reader:
            if imported>=max_rows:break
            try:
                name=item['name'].strip()[:256];lat=float(item['lat']);lon=float(item['lon'])
            except (ValueError,KeyError,AttributeError,TypeError):continue
            if not name or not(math.isfinite(lat) and math.isfinite(lon) and -90<=lat<=90 and -180<=lon<=180):continue
            db.execute('INSERT INTO place_data(name,lat,lon) VALUES (?,?,?)',(name,lat,lon))
            imported+=1
            if imported%5000==0:db.commit()
        db.execute('INSERT INTO place_fts(place_fts) VALUES ("rebuild")')
        db.commit()
    return {'imported':imported,'database':str(db_path)}


def find_places(query,db_path=PLACES,limit=15):
    if not Path(db_path).is_file():return []
    words=re.findall(r'\w+',query[:180],flags=re.U)[:8]
    if not words:return []
    q=' OR '.join('"'+w.replace('"','')+'"' for w in words)
    with sqlite3.connect(db_path) as db:
        rows=db.execute('SELECT p.name,p.lat,p.lon FROM place_fts f JOIN place_data p ON p.id=f.rowid WHERE place_fts MATCH ? ORDER BY rank LIMIT ?',
                        (q,max(1,min(limit,30)))).fetchall()
    return [{'name':name,'lat':lat,'lon':lon} for name,lat,lon in rows]

def status():
    cfg=config();present=bool(cfg.get('tiles') and Path(cfg['tiles']).is_file())
    ready=bool(cfg.get('router') and Path(cfg['router']).is_file())
    terrain=bool(cfg.get('terrain_tiles') and Path(cfg['terrain_tiles']).is_file())
    vector=present and cfg.get('format') in ('pbf','mvt')
    return {'tiles_ready':present,'vector_tiles_ready':vector,'terrain_ready':terrain,'terrain_maxzoom':cfg.get('terrain_maxzoom',13),'router_data_ready':ready,
            'walking_graph_ready':bool(cfg.get('walking_router') and Path(cfg['walking_router']).is_file()),
            'offroad_trail_data_ready':Path(os.environ.get('DOOMSDAY_TRAIL_DB','/var/lib/doomsday-vault/offroad-trails.sqlite')).is_file(),
            'gazetteer_ready':PLACES.is_file(),'tile_name':cfg.get('name'),
            'minzoom':cfg.get('minzoom',0),'maxzoom':cfg.get('maxzoom',18),'bounds':cfg.get('bounds'),
            'algorithm':cfg.get('algorithm'),
            'note':'Map ZIMs and MBTiles render offline, but standalone road navigation requires a local routing graph. The StreetZim archive includes its own separate map and route viewer.'}


def serve_router():
    cfg=config();prefix=cfg.get('router')
    if not prefix:raise RuntimeError('Enroll a prepared OSRM .osrm graph first')
    path,algo=validate_router(prefix)
    # Exec only a fixed program with fixed flags and a validated dataset path.
    os.execvp('osrm-routed',['osrm-routed','--algorithm',algo,'--ip','127.0.0.1','--port','5000',str(path)])


def serve_walker():
    cfg=config();prefix=cfg.get('walking_router')
    if not prefix:raise RuntimeError('Enroll a foot-profile OSRM .osrm graph first')
    path,algo=validate_router(prefix)
    os.execvp('osrm-routed',['osrm-routed','--algorithm',algo,'--ip','127.0.0.1','--port','5001',str(path)])


def main():
    p=argparse.ArgumentParser(description='Local raster/vector MBTiles, OSRM and strict footpath manager')
    p.add_argument('action',choices=['tiles','terrain','router','walk-router','offroad','places','find','status','route','serve-router','serve-walker'])
    p.add_argument('values',nargs='*')
    a=p.parse_args()
    if a.action=='tiles':result=enroll_tiles(a.values[0])
    elif a.action=='terrain':result=enroll_terrain(a.values[0])
    elif a.action=='router':result=enroll_router(a.values[0])
    elif a.action=='walk-router':result=enroll_walking_router(a.values[0])
    elif a.action=='offroad':
        from trip_planner import import_trails
        result=import_trails(a.values[0])
    elif a.action=='places':result=import_places(a.values[0])
    elif a.action=='find':result=find_places(' '.join(a.values))
    elif a.action=='serve-router':serve_router();return
    elif a.action=='serve-walker':serve_walker();return
    elif a.action=='route':
        if len(a.values)!=4:p.error('route LAT_FROM LON_FROM LAT_TO LON_TO')
        result=route(*map(float,a.values))
    else:result=status()
    print(json.dumps(result,indent=2))
if __name__=='__main__':main()
