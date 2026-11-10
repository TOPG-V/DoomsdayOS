#!/usr/bin/env python3
"""Explicit import of a user-selected, single-file .zim torrent with SHA-256.

Trust is never inferred from the torrent's SHA-1 piece hashes. The publisher
or user must supply an independently trusted 64-digit SHA-256 digest.
"""
from __future__ import annotations
import hashlib,re,shutil,subprocess
from pathlib import Path

MAX_META=4*1024*1024
GIB=1024**3


def _bdecode(data:bytes):
    """Restricted bencode parser, bounded by MAX_META and nesting depth."""
    if len(data)>MAX_META:raise ValueError('Torrent metadata file too large')
    def parse(pos,depth=0):
        if depth>24 or pos>=len(data):raise ValueError('Invalid bencode')
        t=data[pos:pos+1]
        if t==b'i':
            j=data.find(b'e',pos+1)
            if j<0:raise ValueError('Unterminated integer')
            raw=data[pos+1:j]
            if not re.fullmatch(rb'(0|-[1-9][0-9]*|[1-9][0-9]*)',raw):raise ValueError('Malformed integer')
            return int(raw),j+1
        if t==b'l':
            out=[];pos+=1
            while data[pos:pos+1]!=b'e':
                x,pos=parse(pos,depth+1);out.append(x)
                if len(out)>10000:raise ValueError('Torrent too complex')
            return out,pos+1
        if t==b'd':
            out={};pos+=1
            while data[pos:pos+1]!=b'e':
                key,pos=parse(pos,depth+1)
                if not isinstance(key,bytes) or key in out:raise ValueError('Malformed torrent dictionary')
                out[key],pos=parse(pos,depth+1)
                if len(out)>10000:raise ValueError('Torrent too complex')
            return out,pos+1
        if b'0'<=t<=b'9':
            sep=data.find(b':',pos)
            if sep<0 or sep-pos>14:raise ValueError('Malformed string')
            raw=data[pos:sep]
            if not re.fullmatch(rb'(0|[1-9][0-9]*)',raw):raise ValueError('Malformed byte length')
            length=int(raw);start=sep+1;end=start+length
            if end>len(data):raise ValueError('Truncated string')
            return data[start:end],end
        raise ValueError('Bad bencode token')
    root,end=parse(0)
    if end!=len(data) or not isinstance(root,dict):raise ValueError('Invalid torrent root')
    return root


def torrent_info(path):
    f=Path(path).resolve(strict=True)
    if not f.is_file() or f.suffix.lower()!='.torrent' or f.stat().st_size>MAX_META:
        raise ValueError('Expected a local .torrent metadata file (max 4 MiB)')
    root=_bdecode(f.read_bytes())
    info=root.get(b'info')
    if not isinstance(info,dict) or b'files' in info:raise ValueError('Only single-file ZIM torrents are supported; multi-file torrents are rejected')
    try:name=info[b'name.utf-8'] if b'name.utf-8' in info else info[b'name'];name=name.decode('utf-8','strict')
    except (KeyError,UnicodeError,AttributeError) as e:raise ValueError('Invalid torrent filename') from e
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*\.zim',name) or
            name in ('.','..') or '..' in name):raise ValueError('Unsafe torrent filename')
    length=info.get(b'length',0)
    piece_size=info.get(b'piece length',0)
    pieces=info.get(b'pieces')
    if (type(length) is not int or not 0<length<4*1024**4 or type(piece_size) is not int or
            piece_size<=0 or piece_size>64*1024**2 or not isinstance(pieces,bytes) or
            len(pieces)!=20*((length+piece_size-1)//piece_size)):
        raise ValueError('Invalid single-file torrent piece metadata')
    return {'name':name,'size':length,'torrent':str(f)}


def run_torrent_import(path,sha256,storage='primary',cfg=None,runner=subprocess.run):
    if not re.fullmatch(r'[0-9a-fA-F]{64}',sha256 or ''):
        raise ValueError('Explicit independently trusted SHA-256 is required')
    expected=sha256.lower()
    info=torrent_info(path)
    import zim_manager
    from dual_storage import archive_config
    from archive_guard import checked_archive,stage_dir,publish_verified,sha256 as file_sha
    cfg=cfg or zim_manager.config()
    if storage not in ('primary','secondary'):raise ValueError('Invalid storage choice')
    targetcfg=archive_config(cfg,storage)
    archive,archive_ds=checked_archive(targetcfg)
    stage=stage_dir(targetcfg)
    final=archive/info['name'];staged=stage/info['name']
    if final.is_file():
        if file_sha(final)==expected:return final
        raise ValueError('A different archive with this filename already exists; leaving it untouched')
    if staged.is_symlink() or (staged.exists() and not staged.is_file()):
        raise ValueError('Unsafe staged path; no files were changed')
    a_factor=zim_manager.zfs_copies(archive);s_factor=zim_manager.zfs_copies(stage)
    same_pool=archive_ds.split('/')[0]==targetcfg['staging_dataset'].split('/')[0]
    needed_archive=info['size']*a_factor+3*GIB+(info['size']*s_factor if same_pool and not staged.exists() else 0)
    needed_stage=info['size']*s_factor+3*GIB
    if (zim_manager.disk_free(archive)<needed_archive or
            (not same_pool and zim_manager.disk_free(stage)<needed_stage)):
        raise ValueError('INSUFFICIENT SPACE for original plus temporary torrent staging; nothing started')
    if not staged.exists() or file_sha(staged)!=expected:
        exe=shutil.which('aria2c')
        if not exe:raise RuntimeError('Install aria2c to import a custom torrent: sudo apt install aria2')
        args=[exe,'--dir='+str(stage),'--seed-time=0','--continue=true',
              '--file-allocation=none','--auto-file-renaming=false','--allow-overwrite=false',
              '--enable-rpc=false','--max-connection-per-server=2',info['torrent']]
        runner(args,check=True)
    if not staged.is_file() or staged.is_symlink():raise RuntimeError('Torrent did not produce a safe ZIM file')
    if staged.stat().st_size!=info['size'] or file_sha(staged)!=expected:
        raise RuntimeError('Downloaded torrent did not match expected size/SHA-256; retained in staging for investigation')
    out=publish_verified(staged,expected,'user-torrent:'+Path(path).name,cfg_override=targetcfg)
    staged.unlink(missing_ok=True)
    subprocess.run(['systemctl','try-restart','doomsday-kiwix.service'],check=False,capture_output=True)
    return out
