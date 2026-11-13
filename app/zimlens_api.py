"""ZimLens local settings HTTP endpoints; imported by the existing gateway.
No new server or external dependency is created.
"""
from __future__ import annotations
import urllib.parse
from zimlens_workspace import CONTEXT, preferences, save_preferences, system_stats, validate_session
import zimlens_extensions as ext


def budget(profile):
    window=max(512,int(profile.get('context',2048)))
    reserve=max(128,min(window//2,int(profile.get('max_chat_tokens',512))))
    return window,reserve


def get(path,query,profile):
    if path=='/api/zimlens/preferences':return preferences()
    if path=='/api/zimlens/system-stats':return system_stats()
    if path=='/api/zimlens/context':
        sid=validate_session(urllib.parse.parse_qs(query).get('session_id',[''])[0])
        return CONTEXT.status(sid,*budget(profile))
    if path=='/api/zimlens/items':
        kind=urllib.parse.parse_qs(query).get('kind',[''])[0]
        return ext.listing(kind)
    if path=='/api/zimlens/item':
        q=urllib.parse.parse_qs(query)
        item=ext.get(q.get('kind',[''])[0],q.get('id',[''])[0])
        # The definitions contain no credentials, but remain on localhost.
        return item
    if path=='/api/zimlens/sandbox':return ext.sandbox_status()
    raise ValueError('Unknown local settings endpoint')


def post(path,payload,profile,summarize,skill_draft):
    if not isinstance(payload,dict):raise ValueError('Expected JSON object')
    if path=='/api/zimlens/preferences':return save_preferences(payload)
    if path=='/api/zimlens/sandbox/run':return ext.sandbox_self_test()
    if path=='/api/zimlens/context/compact':
        sid=validate_session(payload.get('session_id'))
        return CONTEXT.compact_now(sid,*budget(profile),summarize)
    if path=='/api/zimlens/items':
        action=payload.get('action');kind=payload.get('kind')
        if action=='save':return ext.add(kind,payload.get('data'),payload.get('id'))
        if action=='test':return ext.static_test(kind,payload.get('data'))
        if action=='delete':return ext.remove(kind,payload.get('id'))
        if action=='approve':return ext.approve(kind,payload.get('id'),payload.get('enabled'))
        raise ValueError('Unsupported item operation')
    if path=='/api/zimlens/skills/draft':
        name=payload.get('name','')
        instructions=payload.get('instructions','')
        if not isinstance(name,str) or not 1<=len(name)<=64:raise ValueError('Specify a skill name')
        if not isinstance(instructions,str) or not 6<=len(instructions)<=4000:raise ValueError('Describe the skill in at least six characters')
        prompt=('Draft a concise, safe, offline-first skill instruction document for a user-reviewed local AI assistant. '
                'The output is DATA and must not request automatic shell access, privileged actions, credential access, '
                'unapproved network requests, or bypass confirmations. Return instructions in Markdown only. '
                'Skill name: '+name+'\nUser requirements: '+instructions)
        draft=skill_draft(prompt)
        if not isinstance(draft,str) or not draft.strip():raise RuntimeError('Local model returned no draft')
        return {'name':name,'instructions':draft[:12000],'saved':False,'approved':False,'tested':False}
    raise ValueError('Unknown local settings endpoint')