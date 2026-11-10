#!/usr/bin/env python3
"""Bounded, offline tool execution for a local llama.cpp assistant.

The model can request read-only tools. Only the user's explicit browser location
is used; no OS geolocation is silently queried. No shell, arbitrary files, HTTP
fetching, filesystem writes, or user-controlled URL access is exposed.
"""
from __future__ import annotations
import datetime, json, re
from offline_maps import find_places, status as map_status
from trip_planner import plan as plan_route, point as verify_point
from zim_index import article_title_search
import research_library
import system_maintenance

TOOL_DEFS = [
 {'type':'function','function':{'name':'current_time','description':'Current date, time and UTC offset of the local DoomsdayOS machine. Not the time at a remote destination.','parameters':{'type':'object','properties':{}}}},
 {'type':'function','function':{'name':'find_places','description':'Search enrolled offline gazetteer by exact or partial town/place name. Do not guess coordinates.','parameters':{'type':'object','properties':{'query':{'type':'string'}},'required':['query']}}},
 {'type':'function','function':{'name':'plan_trip','description':'Calculate an actual offline car/walking/off-road route with optional stops (maximum three). start can be "current" only when the user explicitly shares saved manual coordinates or browser GPS with this request; otherwise supply coordinates lat/lon or an unambiguous offline place name. Routing requires enrolled road/trail data. No live traffic.','parameters':{'type':'object','properties':{'start':{'type':'string','description':'current, city name or lat,lon'},'destination':{'type':'string'},'stops':{'type':'array','items':{'type':'string'},'maxItems':3},'mode':{'type':'string','enum':['car','walk','offroad']}},'required':['start','destination','mode']}}},
 {'type':'function','function':{'name':'map_coverage','description':'Inspect installed offline tile/road/walking graph availability without making a route.','parameters':{'type':'object','properties':{}}}},
 {'type':'function','function':{'name':'find_articles','description':'Search indexed ZIM article titles (not general internet search). Returns article links and excerpts.','parameters':{'type':'object','properties':{'query':{'type':'string'}},'required':['query']}}},
 {'type':'function','function':{'name':'find_documents','description':'Search previously indexed local PDFs, imported websites, and ZIM articles.','parameters':{'type':'object','properties':{'query':{'type':'string'}},'required':['query']}}},
 {'type':'function','function':{'name':'system_diagnostics','description':'Read-only health analysis of local OS, ZFS, package state and failed services. Suggest safe next steps, never execute terminal commands or repairs.','parameters':{'type':'object','properties':{}}}},
 {'type':'function','function':{'name':'read_document','description':'Read an existing indexed local document excerpt by its source_id returned by find_documents. Not arbitrary files or URLs.','parameters':{'type':'object','properties':{'source_id':{'type':'string'}},'required':['source_id']}}},
 {'type':'function','function':{'name':'semantic_find_articles','description':'Use the optional EmbeddingGemma 2 index for semantic lookup of indexed ZIM articles. Falls back to title matching if model/index unavailable.','parameters':{'type':'object','properties':{'query':{'type':'string'}},'required':['query']}}},
 {'type':'function','function':{'name':'article_images','description':'Find images ONLY in indexed ZIM articles matching the given title query, with links to those specific articles.','parameters':{'type':'object','properties':{'title_query':{'type':'string'}},'required':['title_query']}}},
 {'type':'function','function':{'name':'zim_download_status','description':'Read-only status and counts of queued ZIMs, completed downloads and retry errors. No mutations.','parameters':{'type':'object','properties':{}}}},
 {'type':'function','function':{'name':'storage_fleet_status','description':'Read-only ZFS fleet capacity report; does not format, move or modify any disk.','parameters':{'type':'object','properties':{}}}},
 {'type':'function','function':{'name':'recovery_status','description':'Read safe boot-failure watcher state and whether a recovery plan exists; never restore automatically.','parameters':{'type':'object','properties':{}}}}

]
NAMES = {x['function']['name'] for x in TOOL_DEFS}


def _small_text(value,limit=180):
    if not isinstance(value,str) or not value.strip() or len(value)>limit:
        raise ValueError('Supply a non-empty, bounded text value')
    return value.strip()


def _coords(text):
    mat=re.fullmatch(r'\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*',text)
    return verify_point({'lat':float(mat[1]),'lon':float(mat[2])}) if mat else None


def resolve_place(text, location=None):
    name=_small_text(text,160)
    if name.lower() in ('current','my location','here'):
        if location is None:
            raise ValueError('Current location unavailable. Set a manual location in World Maps and share it for the request, or enter start coordinates.')
        return verify_point(location)
    coords=_coords(name)
    if coords:return coords
    if name.lower() in ('usa','united states','uk','united kingdom','europe','asia'):
        raise ValueError('Choose a particular city or map point, not a whole country or continent')
    matches=find_places(name,limit=12)
    exact=[x for x in matches if x['name'].casefold()==name.casefold()]
    choices=exact if exact else matches
    if len(choices)!=1:raise ValueError('Place is ambiguous or not indexed: '+name+'. Search offline places and choose an exact match or coordinates.')
    return verify_point(choices[0])


def execute_tool(name,args,location=None):
    if name not in NAMES:raise ValueError('Tool is not permitted: '+str(name)[:50])
    if not isinstance(args,dict):raise ValueError('Tool arguments must be an object')
    if name=='system_diagnostics':return system_maintenance.analyze()
    if name=='current_time':
        now=datetime.datetime.now().astimezone()
        return {'local_iso':now.isoformat(timespec='seconds'),'timezone':str(now.tzinfo),'source':'local device clock'}
    if name=='find_places':
        return {'results':find_places(_small_text(args.get('query')),limit=10),'source':'offline gazetteer'}
    if name=='map_coverage':
        return {'coverage':map_status(),'note':'Map tiles and road graph files must be enrolled separately; no online routing is used.'}
    if name=='plan_trip':
        mode=args.get('mode')
        if mode not in ('car','walk','offroad'):raise ValueError('Invalid travel mode')
        stops=args.get('stops',[])
        if not isinstance(stops,list) or len(stops)>3 or any(not isinstance(s,str) for s in stops):
            raise ValueError('Up to three named or coordinate stops only')
        names=[_small_text(args.get('start'))]+[_small_text(s) for s in stops]+[_small_text(args.get('destination'))]
        points=[resolve_place(n,location) for n in names]
        result=plan_route(points,mode=mode)
        # Route duration is returned by the enrolled offline graph, not guessed by the model.
        now=datetime.datetime.now().astimezone()
        duration=result['duration_s']
        if not isinstance(duration,(int,float)) or not 0<=duration<365*86400:raise ValueError('Router returned invalid duration')
        result['estimated_arrival_local']=(now+datetime.timedelta(seconds=duration)).isoformat(timespec='minutes')
        result['estimate_type']='offline modeled time, NO traffic/weather/closures or ocean transport'
        return result
    if name=='find_articles':
        return {'results':article_title_search(_small_text(args.get('query'),240),limit=8),'source':'indexed ZIM titles only'}
    if name=='semantic_find_articles':
        import semantic_rag
        return semantic_rag.search(_small_text(args.get('query'),240),limit=8)
    if name=='article_images':
        from zim_index import article_images
        articles=article_title_search(_small_text(args.get('title_query'),240),limit=6)
        return {'articles':articles,'images':article_images(articles,limit=10)}
    if name=='zim_download_status':
        from zim_manager import read_queue
        from collections import Counter
        queue=read_queue();return {'total':len(queue),'statuses':dict(Counter(x.get('status','unknown') for x in queue)),
                                   'retry_examples':[{'title':x.get('title'),'error':x.get('error','')[:150]} for x in queue if x.get('status')=='retry'][:4]}
    if name=='storage_fleet_status':
        import storage_fleet, zim_manager
        return {'drives':storage_fleet.drive_info(zim_manager.config()),'note':'No disk changes; preview capacities only.'}
    if name=='recovery_status':
        from recovery_watch import WATCH,PLAN,_read
        return {'watch':_read(WATCH),'plan_ready':bool(_read(PLAN).get('reason')),'note':'Unattended OS rollback is intentionally not enabled'}
    if name=='find_documents':
        results=research_library.search(_small_text(args.get('query'),240),limit=8)
        return {'results':results,'source':'locally indexed documents only'}
    source=_small_text(args.get('source_id'),400)
    document=research_library.read(source,max_chars=3500)
    if not document:raise ValueError('Indexed source not found')
    return {'source':document}


def bounded_tool_output(name,result):
    """Avoid sending huge route geometries or extracted PDFs into the LLM context."""
    if name=='plan_trip':
        result={k:v for k,v in result.items() if k!='coordinates'}
        if 'legs' in result:
            result['legs']=[{k:v for k,v in leg.items() if k!='steps'} for leg in result['legs'][:4]]
    serialized=json.dumps(result,ensure_ascii=False)
    return serialized[:12500]
