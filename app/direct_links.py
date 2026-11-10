#!/usr/bin/env python3
"""Validate pinned Kiwix ZIMs against HTTPS publisher sidecars and direct endpoints.

No payload is downloaded by preflight (only the checksum sidecar, optional
catalog entry and at most one streamed byte of each ZIM).  A full SHA-256 is
still required after the complete ZIM has been downloaded before publication.
"""
from __future__ import annotations
import argparse
import contextlib
import json
import re
import urllib.error
import urllib.parse
from pathlib import Path

import zim_manager as zm

DOWNLOAD_HOST = 'download.kiwix.org'
MAX_SIDECAR = 4096
SIZE_RE = re.compile(r'^bytes\s+0-0/(\d+)$', re.I)
DATE_RE = re.compile(r'_20\d\d-\d\d$')


def official_direct_url(url):
    """Change *only* the official Kiwix mirror hostname, not the file path."""
    zm.safe_url(url)
    p = urllib.parse.urlsplit(url)
    if not p.path.startswith('/zim/') or not p.path.endswith('.zim') or '%2f' in p.path.lower():
        raise ValueError('Not a direct official /zim/*.zim archive path')
    candidate = urllib.parse.urlunsplit(('https', DOWNLOAD_HOST, p.path, '', ''))
    zm.safe_url(candidate)
    return candidate


def archive_name(url):
    return Path(urllib.parse.urlsplit(url).path).name


def publisher_sha(url, fetch=zm.request):
    official_direct_url(url)
    with fetch(url + '.sha256', timeout=30) as response:
        data = response.read(MAX_SIDECAR + 1)
    if len(data) > MAX_SIDECAR:
        raise ValueError('Checksum sidecar too large')
    text = data.decode('utf-8', 'strict')
    hashes = re.findall(r'(?i)\b[0-9a-f]{64}\b', text)
    if len(hashes) != 1:
        raise ValueError('Checksum sidecar missing or ambiguous')
    return hashes[0].lower()


def probe_direct(url, fetch=zm.request):
    """Check first byte and best-effort total size, not the full multi-GB file."""
    with fetch(url, headers={'Range': 'bytes=0-0'}, timeout=40) as response:
        status = int(getattr(response, 'status', 200))
        ctype = response.headers.get('Content-Type', '').lower()
        if 'text/html' in ctype or 'xml' in ctype:
            raise ValueError('Direct URL served a page, not a ZIM stream')
        if status not in (200, 206):
            raise ValueError(f'Direct URL returned HTTP {status}')
        if len(response.read(1)) != 1:
            raise ValueError('Direct URL returned empty content')
        size = None
        if status == 206:
            found = SIZE_RE.match(response.headers.get('Content-Range',''))
            if not found:
                raise ValueError('Invalid 206 byte-range response')
            size = int(found.group(1))
        elif response.headers.get('Content-Length'):
            with contextlib.suppress(ValueError):
                size = int(response.headers['Content-Length'])
        return {'http_status':status,'remote_size':size,'resume_supported':status==206}


def catalog_find(item, fetch=zm.request):
    """Best-effort OPDS lookup. Historical versions may not appear in current catalog."""
    stem = archive_name(item['url']).removesuffix('.zim')
    name = DATE_RE.sub('', stem)
    endpoint = zm.CATALOG + '?' + urllib.parse.urlencode({'name':name,'count':100})
    with fetch(endpoint, timeout=35) as response:
        payload = response.read(3 * 1024 * 1024 + 1)
    if len(payload) > 3 * 1024 * 1024:
        raise ValueError('Catalog response too large')
    for hit in zm.parse_catalog(payload):
        try:
            if official_direct_url(hit['url']) == official_direct_url(item['url']):
                return True
        except ValueError:
            continue
    return False


def check_one(item, *, checksum_fetch=publisher_sha, probe=probe_direct, catalog_lookup=catalog_find):
    name = archive_name(item['url'])
    output = {'name':name,'url':'','status':'unverified','catalog':'not_checked',
              'expected_sha256':item.get('sha256',''),'publisher_sha256':'',
              'bytes':item.get('size',0),'details':''}
    try:
        direct = official_direct_url(item['url'])
        output['url'] = direct
        official = checksum_fetch(direct)
        output['publisher_sha256'] = official
        if not re.fullmatch('[0-9a-f]{64}',official):
            raise ValueError('Unparseable SHA-256 digest')
        pinned=item.get('sha256','')
        if pinned and official.lower()!=pinned.lower():
            raise ValueError('Publisher SHA-256 differs from the pinned archival checksum')
        p=probe(direct)
        if (p.get('remote_size') is not None and item.get('size') and
                int(p['remote_size'])!=int(item['size'])):
            raise ValueError('Remote ZIM byte count differs from archive manifest')
        output['remote_bytes']=p.get('remote_size')
        output['resume_supported']=p.get('resume_supported')
        try:
            output['catalog'] = 'listed' if catalog_lookup(item) else 'not_listed'
        except (OSError, ValueError, urllib.error.URLError) as exc:
            output['catalog']='catalog_unreachable'
            output['catalog_note']=str(exc)[:180]
        output['status']='verified_link_and_publisher_hash'
        if not pinned:
            output['details']='SHA-256 obtained from Kiwix publisher sidecar; full download still needs hashing'
    except Exception as exc:
        output['status']='unavailable_or_mismatch'
        output['details']=str(exc)[:300]
    return output


def load_items(selected='queue'):
    manifest=json.loads(zm.BUNDLE_MANIFEST.read_text(encoding='utf-8'))
    originals={x['id']:x for x in manifest['items']}
    if selected=='bundle':return list(originals.values())
    queue=zm.read_queue()
    if not queue:return list(originals.values())
    items=[]
    for item in queue:
        name=archive_name(item['url'])
        if name in originals:
            # The pinned hashes are considered a trust anchor. Never replace one
            # based solely on an untrusted queue row.
            pinned=originals[name]
            if item.get('sha256') and pinned.get('sha256') and item['sha256'].lower()!=pinned['sha256'].lower():
                raise ValueError('Queue SHA-256 differs from bundled hash: '+name)
            items.append(dict(pinned))
    return items


def assess(items, *, check=check_one, workers=4):
    """Probe independent official links in parallel, without downloading ZIM bodies."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    records=[None]*len(items)
    with ThreadPoolExecutor(max_workers=max(1,min(4,int(workers)))) as pool:
        futures={pool.submit(check,item):i for i,item in enumerate(items)}
        for future in as_completed(futures):
            i=futures[future]
            try:record=future.result()
            except Exception as exc:
                record={'name':archive_name(items[i]['url']),'status':'unavailable_or_mismatch',
                        'details':str(exc)[:300]}
            records[i]=record
            ok=record['status']=='verified_link_and_publisher_hash'
            print(f'[{i+1}/{len(items)}] {"OK" if ok else "NOT VERIFIED":<13} {record["name"]}',flush=True)
            if not ok:print('   ',record.get('details',''),flush=True)
    return records


def report_path():return zm.BASE / 'direct-links-report.json'


def save_report(records):
    zm.atomic_json(report_path(),{'schema':1,'entries':records})
    print('Saved preflight report:',report_path())


def apply_records(records):
    """Switch queued official files only, never edit archived ZIMs or partials."""
    with zm.exclusive_download_worker():
        with zm.locked_queue():
            queue=zm.read_queue()
            by_name={r['name']:r for r in records if r['status']=='verified_link_and_publisher_hash'}
            changed=0
            for item in queue:
                name=archive_name(item.get('url',''))
                record=by_name.get(name)
                if record is None or item.get('status') in ('done','downloaded'):
                    continue
                old=official_direct_url(item['url'])
                if old!=record['url']:
                    raise ValueError('Queue archive path changed during check: '+name)
                item['url']=record['url']
                item['sha256']=record['publisher_sha256']
                if item.get('status') in ('retry','downloading'):
                    item['status']='pending'
                    item['error']=''
                changed+=1
            zm.atomic_json(zm.queue_path(),queue)
    print(f'Updated {changed} queued ZIM(s) to direct HTTPS; existing files and .part files untouched.')
    return changed


def main():
    parser=argparse.ArgumentParser(description='Preflight Kiwix direct archive links and SHA-256 checksums')
    parser.add_argument('command',choices=['check','apply','list'])
    parser.add_argument('--source',choices=['queue','bundle'],default='queue')
    a=parser.parse_args()
    items=load_items(a.source)
    if a.command=='list':
        for x in items:
            url=official_direct_url(x['url'])
            print(url+'\t'+(x.get('sha256') or 'FETCH_PUBLISHER_CHECKSUM'))
        return
    if not items:raise SystemExit('No bundled or queued ZIM files found')
    records=assess(items)
    save_report(records)
    good=sum(r['status']=='verified_link_and_publisher_hash' for r in records)
    print(f'Direct source available with matching publisher checksum: {good}/{len(records)}')
    if a.command=='apply':apply_records(records)
    if good<len(records):
        print('Unavailable/mismatched entries were NOT switched; see report for details.')
        raise SystemExit(2)

if __name__=='__main__':main()
