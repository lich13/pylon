#!/usr/bin/env python3
# Chartoo iTunes JP/HK daily monitor.
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import subprocess
import time
import unicodedata
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sys


from pylon.shared import output, paths

DEFAULT_ROOT = paths.CHARTOO_DATA_ROOT
DEFAULT_DB = DEFAULT_ROOT / 'state.sqlite'
MARKETS = ('jp', 'hk')
CHARTOO_PAGE = {'jp': 'https://jp.chartoo.com/itunes/topsongs', 'hk': 'https://hk.chartoo.com/itunes/topsongs'}
USER_AGENT = 'Mozilla/5.0 PylonChartooMonitor/1.0'
CHARTOO_REQUEST_TIMEOUT_SECONDS = 5
CURL_BIN = '/usr/bin/curl'

def utcnow() -> dt.datetime: return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
def iso_now() -> str: return utcnow().isoformat().replace('+00:00','Z')
BJ_TZ = dt.timezone(dt.timedelta(hours=8))
def iso_to_bj_label(value: Optional[str]) -> Optional[str]:
    if not value: return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(BJ_TZ).strftime('%Y-%m-%d %H:%M:%S')
    except Exception: return None
def format_switch_window(start: Optional[str], end: Optional[str]) -> Optional[str]:
    return f'{start} → {end}' if start and end else None
def format_switch_window_bj(start: Optional[str], end: Optional[str]) -> Optional[str]:
    s=iso_to_bj_label(start); e=iso_to_bj_label(end)
    return f'{s} → {e} 北京时间' if s and e else None

def switch_cutoff_bj(release_date: str) -> Optional[dt.datetime]:
    """Fixed user-facing date switch: release date D closes at D+1 21:30 Beijing."""
    try:
        d=dt.date.fromisoformat(str(release_date))
        return dt.datetime.combine(d + dt.timedelta(days=1), dt.time(21, 30), tzinfo=BJ_TZ)
    except Exception:
        return None

def fixed_switch_time_bj(release_date: str) -> Optional[str]:
    cutoff=switch_cutoff_bj(release_date)
    return cutoff.strftime('%Y-%m-%d %H:%M 北京时间') if cutoff else None

def next_date_iso(release_date: str) -> Optional[str]:
    try:
        return (dt.date.fromisoformat(str(release_date)) + dt.timedelta(days=1)).isoformat()
    except Exception:
        return None

def prev_date_iso(release_date: str) -> Optional[str]:
    try:
        return (dt.date.fromisoformat(str(release_date)) - dt.timedelta(days=1)).isoformat()
    except Exception:
        return None

def date_lag_days(active_date: str, latest_date: str) -> Optional[int]:
    try:
        return (dt.date.fromisoformat(str(active_date)) - dt.date.fromisoformat(str(latest_date))).days
    except Exception:
        return None

def parse_utc_iso(value: str) -> Optional[dt.datetime]:
    try:
        parsed=dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if parsed.tzinfo is None: parsed=parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except Exception:
        return None

def should_switch_date(release_date: str, observed_at_utc: str) -> bool:
    cutoff=switch_cutoff_bj(release_date); observed=parse_utc_iso(observed_at_utc)
    return bool(cutoff and observed and observed.astimezone(BJ_TZ) >= cutoff)

def choose_initial_active_date(rows: List[Dict[str,Any]], latest: Optional[str], observed_at_utc: str) -> Optional[str]:
    """Initialize using the fixed 21:30 BJ rule, not merely the newest visible release date."""
    if not latest: return latest
    if should_switch_date(latest, observed_at_utc):
        return next_date_iso(latest) or latest
    dates={str(r.get('release_date')) for r in rows if r.get('release_date')}
    try:
        prev=(dt.date.fromisoformat(str(latest)) - dt.timedelta(days=1)).isoformat()
    except Exception:
        return latest
    if prev in dates and not should_switch_date(prev, observed_at_utc):
        return prev
    return latest

def parse_epoch(value: Any) -> Optional[str]:
    try: return dt.datetime.fromtimestamp(int(value), tz=dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z')
    except Exception: return None

def duration_to_seconds(label: Optional[str]) -> Optional[int]:
    if label is None: return None
    s=str(label).strip()
    if not s: return None
    if s.isdigit():
        n=int(s); return n//1000 if n>1000 else n
    parts=s.split(':')
    if not all(p.isdigit() for p in parts): return None
    vals=[int(p) for p in parts]
    if len(vals)==2: return vals[0]*60+vals[1]
    if len(vals)==3: return vals[0]*3600+vals[1]*60+vals[2]
    return None

def preview_url_from_qm(qm: Optional[str]) -> Optional[str]:
    if not qm: return None
    s=str(qm).strip()
    if not s: return None
    if s.startswith('http://') or s.startswith('https://'): return s
    return 'https://audio-ssl.itunes.apple.com/' + s.lstrip('/')

def apple_track_url(market: str, track_id: Optional[Any], album_id: Optional[Any]=None) -> Optional[str]:
    if track_id is None or str(track_id).strip()=='': return None
    tid=str(track_id).strip()
    if album_id is not None and str(album_id).strip()!='':
        return f'https://music.apple.com/{market}/album/{str(album_id).strip()}?i={tid}'
    return f'https://music.apple.com/{market}/song/{tid}'

def artwork_url_from_gp(gp: Optional[str]) -> Optional[str]:
    if not gp: return None
    s=str(gp).strip()
    if not s: return None
    if s.startswith('http://') or s.startswith('https://'):
        base=s
    else:
        base='https://is1-ssl.mzstatic.com/image/thumb/' + s.lstrip('/')
    if re.search(r'/\d+x\d+bb\.(?:jpg|jpeg|png|webp)$', base, re.I):
        return re.sub(r'/\d+x\d+bb\.(?:jpg|jpeg|png|webp)$','/1000x1000bb.jpg',base,flags=re.I)
    return base.rstrip('/') + '/1000x1000bb.jpg'

def rss_artwork_url(entry: Dict[str,Any]) -> Optional[str]:
    images=entry.get('im:image') or []
    if isinstance(images,dict): images=[images]
    labels=[]
    for img in images:
        if isinstance(img,dict) and img.get('label'): labels.append(str(img.get('label')))
    if not labels: return None
    return artwork_url_from_gp(labels[-1])

def norm_text(s: Optional[str]) -> str:
    if s is None: return ''
    return re.sub(r'\s+',' ',unicodedata.normalize('NFKC',str(s)).casefold()).strip()
def strip_album_noise(s: Optional[str]) -> str:
    return re.sub(r'\s*-\s*(single|ep)$','',norm_text(s)).strip()
def canonical_key(row: Dict[str,Any]) -> str:
    if row.get('apple_track_id'): return f"apple:{row['apple_track_id']}"
    return 'fp:'+'|'.join([norm_text(row.get('title')),norm_text(row.get('artist')),strip_album_noise(row.get('album')),str(row.get('duration_seconds') or '')])

def http_get_bytes(url: str, timeout: int=30) -> bytes:
    timeout=max(1,int(timeout or 30))
    proc=subprocess.run(
        [
            CURL_BIN,
            '-4',
            '-sS',
            '-L',
            '--connect-timeout',
            str(min(timeout, 5)),
            '--max-time',
            str(timeout),
            '-H',
            'Accept: application/json,text/html,*/*;q=0.8',
            '-A',
            USER_AGENT,
            url,
        ],
        text=False,
        capture_output=True,
        timeout=timeout + 2,
    )
    if proc.returncode != 0:
        stderr=(proc.stderr or b'').decode('utf-8', errors='replace').strip()
        raise RuntimeError(f'curl failed rc={proc.returncode}: {stderr[:300]}')
    return proc.stdout or b''
def fetch_json(url: str, timeout: int=30) -> Dict[str,Any]:
    return json.loads(http_get_bytes(url,timeout).decode('utf-8'))

def fetch_chartoo_bytes(url: str, timeout: int=CHARTOO_REQUEST_TIMEOUT_SECONDS) -> bytes:
    return http_get_bytes(url, timeout=max(1,int(timeout or CHARTOO_REQUEST_TIMEOUT_SECONDS)))

def fetch_chartoo_json(url: str, timeout: int=CHARTOO_REQUEST_TIMEOUT_SECONDS) -> Dict[str,Any]:
    return json.loads(fetch_chartoo_bytes(url, timeout=timeout).decode('utf-8'))

def find_pointer(obj: Any, market: str) -> Optional[str]:
    target=f'{market}/topsongs'
    if isinstance(obj,dict):
        if isinstance(obj.get(target),str): return obj[target]
        if isinstance(obj.get('mN'),dict) and isinstance(obj['mN'].get(target),str): return obj['mN'][target]
        for v in obj.values():
            r=find_pointer(v,market)
            if r: return r
    elif isinstance(obj,list):
        for v in obj:
            r=find_pointer(v,market)
            if r: return r
    return None

def find_chartoo_module_version(init_data: Dict[str,Any], country_data: Dict[str,Any], module_df: str) -> Optional[str]:
    rb=None
    for item in country_data.get('lo') or []:
        if isinstance(item,dict) and item.get('df')==module_df and item.get('rb'):
            rb=str(item.get('rb')); break
    versions=init_data.get('v') or {}
    if rb and isinstance(versions,dict) and isinstance(versions.get(rb),str):
        return versions[rb]
    return None

def resolve_chartoo_versioned_pointer(market: str, version: str, errors: List[str], method: str) -> Optional[Tuple[str,Dict[str,Any]]]:
    m=re.match(r'^(\d+)-([A-Za-z0-9_-]+)$', str(version or ''))
    if not m: return None
    its,ikey=m.group(1),m.group(2)
    for u in [f'https://{market}.chartoo.com/json/init/{market}/itunes-{its}-{ikey}.json',f'https://{market}.chartoo.com/json/country/{market}/itunes-{its}-{ikey}.json']:
        try:
            p=find_pointer(fetch_chartoo_json(u, timeout=CHARTOO_REQUEST_TIMEOUT_SECONDS),market)
            if p: return f'https://itunes.chartoo.com/{market}/topsongs-{p}.json', {'init_url':u,'pointer':p,'method':method}
        except Exception as e: errors.append(f'{u}: {e!r}')
    return None

def resolve_chartoo_topsongs_url(market: str) -> Tuple[str,Dict[str,Any]]:
    errors=[]
    for init_url in [f'https://{market}.chartoo.com/json/init/{market}/itunes.json', f'https://{market}.chartoo.com/json/country/{market}/itunes.json']:
        try:
            data=fetch_chartoo_json(init_url, timeout=CHARTOO_REQUEST_TIMEOUT_SECONDS); p=find_pointer(data,market)
            if p: return f'https://itunes.chartoo.com/{market}/topsongs-{p}.json', {'init_url':init_url,'pointer':p,'method':'init-json'}
            errors.append(init_url+': no pointer')
        except Exception as e: errors.append(f'{init_url}: {e!r}')
    html=fetch_chartoo_bytes(CHARTOO_PAGE[market], timeout=CHARTOO_REQUEST_TIMEOUT_SECONDS).decode('utf-8',errors='replace')
    m=re.search(r'/script/base-(\d+)-([A-Za-z0-9_-]+)\.js',html)
    if not m: raise RuntimeError('could not resolve Chartoo pointer; '+ '; '.join(errors))
    bts,bkey=m.group(1),m.group(2)
    base_jsons={}
    for kind in ('init','country'):
        url=f'https://{market}.chartoo.com/json/{kind}/{market}/base-{bts}-{bkey}.json'
        try:
            base_jsons[kind]=fetch_chartoo_json(url, timeout=CHARTOO_REQUEST_TIMEOUT_SECONDS)
            text=json.dumps(base_jsons[kind])
            im=re.search(r'/script/itunes-(\d+)-([A-Za-z0-9_-]+)\.js',text) or re.search(r'itunes-(\d+)-([A-Za-z0-9_-]+)',text)
            if im:
                resolved=resolve_chartoo_versioned_pointer(market, f'{im.group(1)}-{im.group(2)}', errors, 'versioned-json')
                if resolved: return resolved
        except Exception as e: errors.append(f'{url}: {e!r}')
    version=find_chartoo_module_version(base_jsons.get('init') or {}, base_jsons.get('country') or {}, 'itunes')
    if version:
        resolved=resolve_chartoo_versioned_pointer(market, version, errors, 'base-version-map')
        if resolved: return resolved
    raise RuntimeError('could not resolve Chartoo pointer; '+ '; '.join(errors))

def apply_static(row: Dict[str,Any], y: Dict[str,Any], spec: Any) -> None:
    if not isinstance(spec,list): return
    if len(spec)>0 and isinstance(spec[0],dict): row.update(spec[0])
    if len(spec)>1 and isinstance(spec[1],dict):
        for k,idx in spec[1].items():
            try: row[k]=y[k][idx]
            except Exception: row[k]=idx
    if len(spec)>2 and isinstance(spec[2],dict):
        for k,idxs in spec[2].items():
            try: row[k]=[y[k][i] for i in idxs]
            except Exception: row[k]=idxs

def apply_row(row: Dict[str,Any], y: Dict[str,Any], spec: Any, i: int) -> None:
    if not isinstance(spec,list): return
    if len(spec)>0 and isinstance(spec[0],dict):
        for k,arr in spec[0].items():
            try: row[k]=arr[i]
            except Exception: pass
    if len(spec)>1 and isinstance(spec[1],dict):
        for k,arr in spec[1].items():
            try: row[k]=y[k][arr[i]]
            except Exception:
                try: row[k]=arr[i]
                except Exception: pass
    if len(spec)>2 and isinstance(spec[2],dict):
        for k,arrs in spec[2].items():
            try: row[k]=[y[k][j] for j in arrs[i]]
            except Exception:
                try: row[k]=arrs[i]
                except Exception: pass

def row_count_from_spec(spec: Any) -> int:
    if not isinstance(spec,list) or len(spec)<2 or not isinstance(spec[1],list): return 0
    for part in spec[1]:
        if isinstance(part,dict):
            for arr in part.values():
                if isinstance(arr,list): return len(arr)
    return 0

def decode_compact_collection(data: Dict[str,Any], key: str) -> List[Dict[str,Any]]:
    spec=data.get(key); y=data.get('Y') or {}; n=row_count_from_spec(spec); out=[]
    if not isinstance(spec,list) or len(spec)<2: return out
    for i in range(n):
        row={}; apply_static(row,y,spec[0]); apply_row(row,y,spec[1],i); out.append(row)
    return out

def decode_chartoo_rows(data: Dict[str,Any], market: str, data_url: str, observed_at: str) -> List[Dict[str,Any]]:
    tracks=decode_compact_collection(data,'R'); albums=decode_compact_collection(data,'C'); updated=parse_epoch(data.get('u')); rows=[]
    for idx,raw in enumerate(tracks,1):
        album=None; album_id=None; lp=raw.get('lP')
        if isinstance(lp,int) and 0<=lp<len(albums):
            album=albums[lp].get('db'); album_id=albums[lp].get('k')
        elif isinstance(lp,str) and lp.isdigit() and int(lp)<len(albums):
            album=albums[int(lp)].get('db'); album_id=albums[int(lp)].get('k')
        dur=raw.get('EM')
        rows.append({'market':market,'rank':idx,'apple_track_id':str(raw.get('k')) if raw.get('k') is not None else None,'title':raw.get('db'),'artist':raw.get('dB'),'album':album,'duration_label':str(dur) if dur is not None else None,'duration_seconds':duration_to_seconds(str(dur)) if dur is not None else None,'release_date':raw.get('ge'),'source_url':CHARTOO_PAGE[market],'source_track_url':apple_track_url(market, raw.get('k'), album_id),'preview_url':preview_url_from_qm(raw.get('qm')),'artwork_url':artwork_url_from_gp(raw.get('gp')),'data_url':data_url,'observed_at_utc':observed_at,'data_updated_at_utc':updated,'source_quality':'chartoo_full'})
    return rows

def fetch_chartoo_rows(market: str, observed_at: str):
    url,info=resolve_chartoo_topsongs_url(market); data=fetch_chartoo_json(url, timeout=CHARTOO_REQUEST_TIMEOUT_SECONDS); rows=decode_chartoo_rows(data,market,url,observed_at)
    return rows, {'market':market,'status':'ok','source':'chartoo','data_url':url,'pointer':info.get('pointer'),'pointer_method':info.get('method'),'init_url':info.get('init_url'),'data_updated_at_utc':parse_epoch(data.get('u')),'row_count':len(rows),'raw_u':data.get('u'),'raw_x':data.get('x'),'raw_t':data.get('t'),'raw_o':data.get('o')}

def fetch_rss_rows(market: str, observed_at: str):
    url=f'https://itunes.apple.com/{market}/rss/topsongs/limit=100/json'; data=fetch_json(url); entries=data.get('feed',{}).get('entry',[]) or []; rows=[]
    for idx,e in enumerate(entries,1):
        link=e.get('link'); href=None
        if isinstance(link,list) and link: href=link[0].get('attributes',{}).get('href')
        elif isinstance(link,dict): href=link.get('attributes',{}).get('href')
        album=((e.get('im:collection') or {}).get('im:name') or {}).get('label')
        release=e.get('im:releaseDate',{}).get('attributes',{}).get('label') or e.get('im:releaseDate',{}).get('label')
        m=re.match(r'^(\d{4})年(\d{1,2})月(\d{1,2})日$',str(release or ''))
        if m: release=f'{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}'
        rows.append({'market':market,'rank':idx,'apple_track_id':str(e.get('id',{}).get('attributes',{}).get('im:id')) if e.get('id',{}).get('attributes',{}).get('im:id') else None,'title':(e.get('im:name') or {}).get('label'),'artist':(e.get('im:artist') or {}).get('label'),'album':album,'duration_label':None,'duration_seconds':None,'release_date':release,'source_url':href or url,'source_track_url':href,'preview_url':None,'artwork_url':rss_artwork_url(e),'data_url':url,'observed_at_utc':observed_at,'data_updated_at_utc':data.get('feed',{}).get('updated',{}).get('label'),'source_quality':'rss_degraded'})
    enrich_rss_rows_from_lookup(rows, market)
    return rows, {'market':market,'status':'degraded','source':'itunes_rss','data_url':url,'data_updated_at_utc':data.get('feed',{}).get('updated',{}).get('label'),'row_count':len(rows)}

def fetch_itunes_lookup_map(market: str, track_ids: List[str]) -> Dict[str,Dict[str,Any]]:
    ids=[]; seen=set()
    for track_id in track_ids:
        tid=str(track_id or '').strip()
        if tid and tid not in seen:
            ids.append(tid); seen.add(tid)
    if not ids: return {}
    data=fetch_json(f'https://itunes.apple.com/lookup?id={",".join(ids)}&country={market}', timeout=10)
    out={}
    for idx,item in enumerate(data.get('results') or []):
        if not isinstance(item,dict): continue
        tid=item.get('trackId') or item.get('collectionId') or (ids[idx] if idx < len(ids) else None)
        if tid is not None: out[str(tid)]=item
    return out

def enrich_rss_rows_from_lookup(rows: List[Dict[str,Any]], market: str) -> None:
    ids=[str(r.get('apple_track_id')) for r in rows if r.get('apple_track_id') and (not r.get('duration_seconds') or not r.get('preview_url'))]
    if not ids: return
    try:
        lookup=fetch_itunes_lookup_map(market, ids)
    except Exception:
        return
    for row in rows:
        result=lookup.get(str(row.get('apple_track_id') or ''))
        if not result: continue
        if not row.get('duration_seconds') and result.get('trackTimeMillis') is not None:
            seconds=duration_to_seconds(str(result.get('trackTimeMillis')))
            if seconds is not None:
                row['duration_seconds']=seconds
                row['duration_label']=f'{seconds//60}:{seconds%60:02d}'
        if not row.get('preview_url') and result.get('previewUrl'):
            row['preview_url']=str(result.get('previewUrl'))

def fetch_market_rows(market: str, observed_at: str):
    try: return fetch_chartoo_rows(market,observed_at)
    except Exception as ce:
        try:
            rows,meta=fetch_rss_rows(market,observed_at); meta['chartoo_error']=repr(ce); return rows,meta
        except Exception as re_: return [], {'market':market,'status':'failed','source':'none','row_count':0,'chartoo_error':repr(ce),'rss_error':repr(re_)}

def latest_release_date(rows: List[Dict[str,Any]]) -> Optional[str]:
    vals=[r.get('release_date') for r in rows if r.get('release_date') and re.match(r'^\d{4}-\d{2}-\d{2}$',str(r.get('release_date')))]
    return max(vals) if vals else None

def ensure_db(conn: sqlite3.Connection) -> None:
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, started_at_utc TEXT NOT NULL, payload_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS observations (
      id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, market TEXT NOT NULL, release_date TEXT NOT NULL, rank INTEGER NOT NULL,
      apple_track_id TEXT, title TEXT, artist TEXT, album TEXT, duration_seconds INTEGER, duration_label TEXT,
      source_url TEXT, source_track_url TEXT, preview_url TEXT, artwork_url TEXT, data_url TEXT, observed_at_utc TEXT NOT NULL, data_updated_at_utc TEXT, source_quality TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_obs_market_date ON observations(market, release_date);
    CREATE INDEX IF NOT EXISTS idx_obs_run ON observations(run_id);
    CREATE TABLE IF NOT EXISTS finalizations (
      market TEXT NOT NULL, release_date TEXT NOT NULL, finalized_at_utc TEXT NOT NULL, first_seen_at_utc TEXT, last_seen_at_utc TEXT,
      switch_detected_at_utc TEXT, switch_to_date TEXT, report_md TEXT, report_json TEXT, status TEXT NOT NULL DEFAULT 'finalized',
      PRIMARY KEY (market, release_date));
    ''');
    for col in ('source_track_url','preview_url','artwork_url'):
        try: conn.execute(f'ALTER TABLE observations ADD COLUMN {col} TEXT')
        except sqlite3.OperationalError: pass
    conn.commit()

def get_meta(conn,key):
    r=conn.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone(); return r[0] if r else None
def set_meta(conn,key,value):
    if value is None: conn.execute('DELETE FROM meta WHERE key=?',(key,))
    else: conn.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,value))
    conn.commit()
def has_finalized_transition(conn, market, release_date, switch_to_date):
    if not release_date or not switch_to_date: return False
    r=conn.execute('''SELECT 1 FROM finalizations WHERE market=? AND release_date=? AND switch_to_date=? AND finalized_at_utc IS NOT NULL LIMIT 1''',(market,release_date,switch_to_date)).fetchone()
    return bool(r)
def record_observations(conn, run_id, rows):
    conn.executemany('''INSERT INTO observations(run_id,market,release_date,rank,apple_track_id,title,artist,album,duration_seconds,duration_label,source_url,source_track_url,preview_url,artwork_url,data_url,observed_at_utc,data_updated_at_utc,source_quality) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',[(run_id,r['market'],r['release_date'],int(r['rank']),r.get('apple_track_id'),r.get('title'),r.get('artist'),r.get('album'),r.get('duration_seconds'),r.get('duration_label'),r.get('source_url'),r.get('source_track_url'),r.get('preview_url'),r.get('artwork_url'),r.get('data_url'),r.get('observed_at_utc'),r.get('data_updated_at_utc'),r.get('source_quality') or 'unknown') for r in rows if r.get('release_date')]); conn.commit()

def aggregate_market_date(conn, market, release_date):
    cur=conn.execute('''SELECT market,release_date,rank,apple_track_id,title,artist,album,duration_seconds,duration_label,source_url,source_track_url,preview_url,artwork_url,data_url,observed_at_utc,data_updated_at_utc,source_quality FROM observations WHERE market=? AND release_date=? ORDER BY observed_at_utc ASC, rank ASC''',(market,release_date))
    cols=[d[0] for d in cur.description]; groups={}
    for tup in cur.fetchall():
        row=dict(zip(cols,tup)); key=canonical_key(row)
        g=groups.setdefault(key,{'canonical_key':key,'market':market,'release_date':release_date,'apple_track_id':row.get('apple_track_id'),'title':row.get('title'),'artist':row.get('artist'),'album':row.get('album'),'duration_seconds':row.get('duration_seconds'),'duration_label':row.get('duration_label'),'source_url':row.get('source_url'),'source_track_url':row.get('source_track_url'),'preview_url':row.get('preview_url'),'artwork_url':row.get('artwork_url'),'total_points':0,'best_rank':9999,'occurrence_count':0,'first_seen_at_utc':row.get('observed_at_utc'),'last_seen_at_utc':row.get('observed_at_utc'),'ranks':[],'data_urls':[],'source_qualities':[]})
        rank=int(row['rank']); g['total_points']+=max(0,101-rank); old_best=g['best_rank']; g['best_rank']=min(old_best,rank); g['occurrence_count']+=1; g['last_seen_at_utc']=row.get('observed_at_utc'); g['ranks'].append(rank)
        if row.get('data_url') and row.get('data_url') not in g['data_urls']: g['data_urls'].append(row.get('data_url'))
        if row.get('source_quality') and row.get('source_quality') not in g['source_qualities']: g['source_qualities'].append(row.get('source_quality'))
        if rank <= old_best:
            for fld in ('apple_track_id','title','artist','album','duration_seconds','duration_label','source_url','source_track_url','preview_url','artwork_url'):
                if row.get(fld): g[fld]=row.get(fld)
    all_tracks=list(groups.values()); kept=[g for g in all_tracks if not (g['occurrence_count']==1 and g['best_rank']>50)]; dropped=[g for g in all_tracks if (g['occurrence_count']==1 and g['best_rank']>50)]
    kept.sort(key=lambda g:(-g['total_points'],g['best_rank'],-g['occurrence_count'],g.get('first_seen_at_utc') or '',norm_text(g.get('title'))))
    for i,g in enumerate(kept,1): g['final_rank']=i
    return {'market':market,'release_date':release_date,'unique_tracks_before_filter':len(all_tracks),'tracks_after_filter':len(kept),'dropped_count':len(dropped),'tracks':kept,'dropped_tracks':dropped}

def write_reports(root: Path, aggregation: Dict[str,Any], switch_info: Dict[str,Any]):
    d=root/'reports'; d.mkdir(parents=True,exist_ok=True); market=aggregation['market']; date=aggregation['release_date']; stem=f'{date}-{market}'
    jp=d/f'{stem}.json'; mp=d/f'{stem}.md'; payload={'aggregation':aggregation,'switch_info':switch_info}
    jp.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=[f'# Chartoo iTunes {market.upper()} Daily — {date}','','## Date switch',f"- switch_to_date: {switch_info.get('switch_to_date')}",f"- first_seen_at_utc: {switch_info.get('first_seen_at_utc')}",f"- last_seen_at_utc: {switch_info.get('last_seen_at_utc')}",f"- switch_detected_at_utc: {switch_info.get('switch_detected_at_utc')}",f"- switch_window_utc: {switch_info.get('switch_window_utc')}",f"- switch_window_bj: {switch_info.get('switch_window_bj')}",'','## Stats',f"- unique before filter: {aggregation['unique_tracks_before_filter']}",f"- tracks after filter: {aggregation['tracks_after_filter']}",f"- dropped: {aggregation['dropped_count']}",'','## Final ranking','']
    for t in aggregation['tracks']: lines.append(f"{t['final_rank']}. {t.get('title')} — {t.get('artist')} | {t.get('album')} | {t.get('duration_label')} | score {t['total_points']} | best #{t['best_rank']} | obs {t['occurrence_count']}")
    mp.write_text('\n'.join(lines)+'\n',encoding='utf-8'); return {'report_json':str(jp),'report_md':str(mp)}

def observation_window(conn,market,release_date):
    r=conn.execute('SELECT MIN(observed_at_utc), MAX(observed_at_utc) FROM observations WHERE market=? AND release_date=?',(market,release_date)).fetchone(); return (r[0],r[1]) if r else (None,None)
def finalize_market_date(conn, root, market, release_date, switch_to_date, switch_detected_at):
    agg=aggregate_market_date(conn,market,release_date)
    first,last=observation_window(conn,market,release_date)
    observed_switch_window_bj=format_switch_window_bj(last,switch_detected_at)
    switch={
        'market':market,
        'release_date':release_date,
        'switch_to_date':switch_to_date,
        'first_seen_at_utc':first,
        'last_seen_at_utc':last,
        'switch_detected_at_utc':switch_detected_at,
        'switch_window':f'({last}, {switch_detected_at}]' if last else None,
        'switch_window_utc':format_switch_window(last,switch_detected_at),
        'observed_switch_window_bj':observed_switch_window_bj,
        'switch_window_bj':fixed_switch_time_bj(release_date),
        'first_seen_bj':iso_to_bj_label(first),
        'last_seen_bj':iso_to_bj_label(last),
        'switch_detected_bj':iso_to_bj_label(switch_detected_at),
    }
    paths=write_reports(root,agg,switch)
    conn.execute('''INSERT INTO finalizations(market,release_date,finalized_at_utc,first_seen_at_utc,last_seen_at_utc,switch_detected_at_utc,switch_to_date,report_md,report_json,status) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(market,release_date) DO UPDATE SET finalized_at_utc=excluded.finalized_at_utc, first_seen_at_utc=excluded.first_seen_at_utc, last_seen_at_utc=excluded.last_seen_at_utc, switch_detected_at_utc=excluded.switch_detected_at_utc, switch_to_date=excluded.switch_to_date, report_md=excluded.report_md, report_json=excluded.report_json, status=excluded.status''',(market,release_date,iso_now(),first,last,switch_detected_at,switch_to_date,paths['report_md'],paths['report_json'],'finalized')); conn.commit()
    return {'market':market,'release_date':release_date,'filter_rule':'drop if occurrence_count == 1 and best_rank > 50','sort_rule':'total_points desc, best_rank asc, occurrence_count desc, first_seen asc, title asc','switch_info':switch,'reports':paths,'summary':{k:agg[k] for k in ('unique_tracks_before_filter','tracks_after_filter','dropped_count')},'tracks':agg['tracks']}

def slim_track_for_prompt(t: Dict[str,Any]) -> Dict[str,Any]:
    return output.sanitize_for_prompt({k:t.get(k) for k in ('final_rank','title','artist','best_rank','occurrence_count','source_track_url','preview_url') if t.get(k) is not None})

def slim_finalization_for_prompt(fin: Dict[str,Any]) -> Dict[str,Any]:
    return output.sanitize_for_prompt({
        'market': fin.get('market'),
        'release_date': fin.get('release_date'),
        'reports': {'report_json': (fin.get('reports') or {}).get('report_json')},
        'tracks': [slim_track_for_prompt(t) for t in (fin.get('tracks') or [])],
    })


def save_raw(root, run_id, payload):
    d=root/'raw'; d.mkdir(parents=True,exist_ok=True); p=d/f'{run_id}.json'; p.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8'); return str(p)

def run_once(args):
    root=Path(args.root); root.mkdir(parents=True,exist_ok=True); db=Path(args.db); db.parent.mkdir(parents=True,exist_ok=True); observed=iso_now(); run_id=observed.replace(':','').replace('-','').replace('Z','Z')+'-'+str(int(time.time()*1000)%100000); fins=[]; anomalies=[]; payload={}
    with sqlite3.connect(db) as conn:
        ensure_db(conn)
        for market in MARKETS:
            rows,meta=fetch_market_rows(market,observed); latest=latest_release_date(rows); latest_rows=[r for r in rows if r.get('release_date')==latest] if latest else []; key=f'{market}.active_date'; active=get_meta(conn,key); payload[market]={'meta':meta,'latest_release_date':latest,'latest_row_count':len(latest_rows),'active_date_before':active}
            if meta.get('status')=='failed' or not latest:
                anomalies.append({'market':market,'type':'source_failed_or_no_latest_date','meta':meta}); continue
            if active is None:
                initial=choose_initial_active_date(rows,latest,observed)
                if initial is None:
                    anomalies.append({'market':market,'type':'source_failed_or_no_initial_date','meta':meta}); continue
                initial_rows=[r for r in rows if r.get('release_date')==initial]
                set_meta(conn,key,initial); record_observations(conn,run_id,initial_rows); payload[market].update({'active_date_after':initial,'event':'initialized','initialization_rule':'fixed_21:30_bj'})
            elif latest==active:
                record_observations(conn,run_id,latest_rows)
                next_date=next_date_iso(active)
                if next_date and should_switch_date(active,observed):
                    fins.append(finalize_market_date(conn,root,market,active,next_date,observed)); set_meta(conn,key,next_date)
                    next_rows=[r for r in rows if r.get('release_date')==next_date]
                    if next_rows:
                        record_observations(conn,run_id,next_rows)
                    payload[market].update({'active_date_after':next_date,'event':'date_switched_forced','switch_rule':'fixed_21:30_bj','visible_latest_release_date':latest})
                else:
                    payload[market].update({'active_date_after':active,'event':'collected'})
            elif latest>active:
                active_rows=[r for r in rows if r.get('release_date')==active]
                if active_rows:
                    record_observations(conn,run_id,active_rows)
                if should_switch_date(active,observed):
                    next_active=next_date_iso(active) or latest
                    fins.append(finalize_market_date(conn,root,market,active,next_active,observed)); set_meta(conn,key,next_active)
                    next_rows=[r for r in rows if r.get('release_date')==next_active]
                    if next_rows:
                        record_observations(conn,run_id,next_rows)
                    payload[market].update({'active_date_after':next_active,'event':'date_switched','switch_rule':'fixed_21:30_bj','visible_latest_release_date':latest})
                else:
                    payload[market].update({'active_date_after':active,'event':'collected_until_fixed_21:30_bj','visible_latest_release_date':latest,'switch_after_bj':fixed_switch_time_bj(active)})
            else:
                # After a fixed 21:30 BJ switch, Chartoo may lag or temporarily
                # regress. If the previous date was finalized into the current
                # active date, keep waiting for the source instead of paging every run.
                lag_days=date_lag_days(active,latest)
                prev_active=next_date_iso(latest) if latest else None
                if prev_active==active:
                    payload[market].update({'active_date_after':active,'event':'waiting_for_chartoo_after_fixed_switch','visible_latest_release_date':latest,'source_lag_days':lag_days})
                elif has_finalized_transition(conn,market,prev_date_iso(active),active):
                    payload[market].update({'active_date_after':active,'event':'waiting_for_chartoo_source_catchup','visible_latest_release_date':latest,'source_lag_days':lag_days,'finalized_transition_release_date':prev_date_iso(active)})
                else:
                    anomalies.append({'market':market,'type':'latest_date_older_than_active','active_date':active,'latest_date':latest,'source_lag_days':lag_days,'meta':meta}); payload[market].update({'active_date_after':active,'event':'anomaly','source_lag_days':lag_days})
        raw=save_raw(root,run_id,payload); conn.execute('INSERT INTO runs(run_id,started_at_utc,payload_json) VALUES(?,?,?)',(run_id,observed,json.dumps({'markets':payload,'raw_path':raw},ensure_ascii=False))); conn.commit()
    if fins or anomalies:
        out={'action':'finalize' if fins else 'anomaly','run_id':run_id,'observed_at_utc':observed,'finalizations':[slim_finalization_for_prompt(f) for f in fins],'anomalies':anomalies}
        return output.json_dumps_compact(out)
    return output.wake_agent_false()

def dry_run_fetch(args):
    observed=iso_now(); out={'observed_at_utc':observed,'markets':{}}
    for market in MARKETS:
        rows,meta=fetch_market_rows(market,observed); latest=latest_release_date(rows); latest_rows=[r for r in rows if r.get('release_date')==latest] if latest else []
        out['markets'][market]={'meta':meta,'latest_release_date':latest,'row_count':len(rows),'latest_row_count':len(latest_rows),'field_completeness':{'title':all(bool(r.get('title')) for r in rows),'artist':all(bool(r.get('artist')) for r in rows),'album':all(bool(r.get('album')) for r in rows),'duration':all(r.get('duration_seconds') is not None for r in rows) if rows else False},'latest_top10':[{k:r.get(k) for k in ('rank','apple_track_id','title','artist','album','duration_label','duration_seconds','release_date')} for r in latest_rows[:10]]}
    return output.json_dumps_pretty(out)

def cmd_aggregate(args):
    with sqlite3.connect(args.db) as conn: ensure_db(conn); p=aggregate_market_date(conn,args.market,args.date)
    return output.json_dumps_pretty(p)

def build_parser():
    p=argparse.ArgumentParser(description='Chartoo iTunes JP/HK daily monitor'); p.add_argument('--db',default=str(DEFAULT_DB)); p.add_argument('--root',default=str(DEFAULT_ROOT)); sub=p.add_subparsers(dest='cmd'); sub.add_parser('run'); sub.add_parser('dry-run-fetch'); ap=sub.add_parser('aggregate'); ap.add_argument('--market',choices=MARKETS,required=True); ap.add_argument('--date',required=True); return p

def main(argv=None):
    parser=build_parser(); args=parser.parse_args(argv); args.cmd=args.cmd or 'run'
    try:
        if args.cmd=='run': print(run_once(args))
        elif args.cmd=='dry-run-fetch': print(dry_run_fetch(args))
        elif args.cmd=='aggregate': print(cmd_aggregate(args))
        else: parser.error(f'unknown command {args.cmd}')
    except Exception as e:
        print(output.json_dumps_pretty({'action':'error','error':repr(e)})); return 2
    return 0
if __name__=='__main__': raise SystemExit(main())
