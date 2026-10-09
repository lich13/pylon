"""Read-only forum adapters; temporary browser profiles never own live state."""
from __future__ import annotations

import contextlib
import datetime as dt
import re
import sqlite3
import json
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs
from pylon.config import profile_root
from . import request_gate
from .request_gate import SourceRateLimited

NGA_URL = 'https://bbs.nga.cn/thread.php?fid=498&order_by=postdatedesc'
NGA_REQUEST_INTERVAL = 15
DATE = re.compile(r'(?<!\d)(20\d{2}|\d{2})[-/](\d{1,2})[-/](\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?(?!\d)')


class HistoryUnavailable(RuntimeError):
    """An observed access boundary, not a transient network failure."""


class ContentUnavailable(RuntimeError):
    """A classified, non-transient content gap without raw page content."""


def topic_identity(url,source):
    parsed=urlparse(url)
    hosts={'chiphell':{'www.chiphell.com','chiphell.com'},'nga':{'bbs.nga.cn','nga.178.com'},'v2ex':{'www.v2ex.com','v2ex.com'}}
    if parsed.scheme not in {'http','https'} or parsed.hostname not in hosts.get(source,set()):return None
    if source=='v2ex':
        m=re.fullmatch(r'/t/(\d+)/?',parsed.path);return m[1] if m else None
    if source=='chiphell':
        m=re.fullmatch(r'/thread-(\d+)-\d+-\d+\.html',parsed.path)
        if m:return m[1]
        if parsed.path not in {'/forum.php','/read.php'}:return None
    elif parsed.path!='/read.php':return None
    return (parse_qs(parsed.query).get('tid') or [None])[0]


def author_context(page,source):
    """A stable pseudonym for same-seller offer deduplication, not a contact."""
    try:
        selector='#postlist .authi a.xw1' if source=='chiphell' else '#postauthor0'
        node=page.locator(selector).first
        link=node.get_attribute('href') or ''
        parsed=urlparse(link)
        actor=(parse_qs(parsed.query).get('uid') or [''])[0]
        if not actor:
            found=re.search(r'(?:space-uid-|uid=)(\d+)',link)
            actor=found[1] if found else ''
        if not actor and source=='nga':actor=node.get_attribute('data-uid') or node.get_attribute('uid') or ''
        if actor:return {'actor':__import__('hashlib').sha256((source+':'+actor).encode()).hexdigest()}
    except Exception:
        pass
    return {}


def check_nga_rate_limit(page, *, in_slot: bool = False) -> None:
    text = page.locator('body').inner_text(timeout=3000)
    if re.search(r'ERROR\s*:\s*2048|刷新过快|访问过于频繁|操作过于频繁|Too Many Requests', text, re.I):
        if not in_slot:
            request_gate.cooldown()
        raise SourceRateLimited('nga_rate_limited')


def nga_goto(page, url: str, ctx=None) -> None:
    with request_gate.request_slot(NGA_REQUEST_INTERVAL):
        response = page.goto(url, wait_until='domcontentloaded', timeout=30000)
        if response is not None and getattr(response, 'status', 0) == 429:
            raise SourceRateLimited('nga_rate_limited')
        check_nga_rate_limit(page, in_slot=True)


def content_gap(text: str) -> str:
    if re.search(r'主题不存在|帖子不存在|指定的主题不存在|已被删除|没有找到这个主题|Topic Not Found',text,re.I):
        return 'deleted_or_not_found'
    if re.search(r'权限不足|没有权限|无权访问|需要.{0,8}登录|登录后.{0,8}查看|阅读权限|你可能需要.{0,8}登录',text):
        return 'permission_required'
    return 'body_missing_or_incomplete'


def date_text(value: str) -> str:
    match = DATE.search(value)
    if not match:
        return ''
    year,month,day,hour,minute = match.groups()
    try:
        return dt.datetime(int(year) + (2000 if len(year)==2 else 0),int(month),int(day),int(hour or 0),int(minute or 0),
                           tzinfo=dt.timezone(dt.timedelta(hours=8))).isoformat()
    except ValueError:
        return ''


def nga_ad_target(current_url: str, requested_url: str) -> str:
    """Validate the destination of NGA's own skip-ad interstitial."""
    current = urlparse(current_url)
    target_url = current.query
    target, requested = urlparse(target_url), urlparse(requested_url)
    if (current.hostname != 'bbs.nga.cn' or target.scheme != 'https'
            or requested.scheme != 'https' or requested.netloc != 'bbs.nga.cn'
            or target.hostname != 'bbs.nga.cn' or target.netloc != 'bbs.nga.cn'
            or target.path != requested.path or target.path not in {'/thread.php', '/read.php'}):
        raise ValueError('unexpected NGA advertisement destination')
    target_query = parse_qs(target.query)
    if target_query != parse_qs(requested.query):
        raise ValueError('NGA advertisement destination changed the requested page')
    return target_url


def skip_nga_ad(page, requested_url: str, ctx=None) -> None:
    # getJump() on this page navigates to the URL in location.search. Follow
    # only the original forum destination, never an advertisement's own link.
    for _ in range(2):
        if not page.get_by_text('点此跳过广告', exact=True).count():
            return
        target = nga_ad_target(page.url, requested_url)
        nga_goto(page, target, ctx)
    if page.get_by_text('点此跳过广告', exact=True).count():
        raise RuntimeError('nga_ad_interstitial')


@contextlib.contextmanager
def browser(source: str):
    from playwright.sync_api import sync_playwright
    from pylon.shared import paths, playwright_env
    from pylon.tasks.chiphell.runtime import APPROVED_PROXY_URL, CHIPHELL_USER_AGENT
    playwright_env.ensure()
    with tempfile.TemporaryDirectory(prefix='pylon-marketplace-') as temporary, sync_playwright() as p:
        if source == 'chiphell':
            # Snapshot just the cookie database. Do not attach to or lock the live profile.
            original = paths.CHIPHELL_DATA_ROOT/'browser-profile/Default/Cookies'
            target = Path(temporary)/'Default/Cookies'
            target.parent.mkdir(mode=0o700)
            with sqlite3.connect(original.resolve().as_uri()+'?mode=ro',uri=True) as source_db:
                with sqlite3.connect(target) as target_db:
                    source_db.backup(target_db)
            ctx = p.chromium.launch_persistent_context(temporary,headless=True,
                    proxy={'server':APPROVED_PROXY_URL},user_agent=CHIPHELL_USER_AGENT)
        else:
            engine = p.chromium.launch(headless=True)
            ctx = engine.new_context()
            cookie_path = Path(os.environ.get('PYLON_NGA_COOKIES') or profile_root('webops')/'secrets/nga-cookies.json')
            if source=='nga' and cookie_path.is_file():
                value = json.loads(cookie_path.read_text())
                cookies = value.get('cookies',[]) if isinstance(value,dict) else []
                if not cookies or any(str(c.get('domain','')).lstrip('.') not in {'nga.cn','bbs.nga.cn'} for c in cookies):
                    ctx.close();engine.close()
                    raise ValueError('invalid NGA cookie scope')
                ctx.add_cookies(cookies)
        try:
            yield ctx
        finally:
            ctx.close()
            if source != 'chiphell':
                engine.close()


def list_page(ctx, source: str, page_number: int = 1) -> list[dict]:
    from pylon.tasks.chiphell.runtime import CHIPHELL_FORUM_URL
    url = (NGA_URL if source == 'nga' else CHIPHELL_FORUM_URL) + f'&page={page_number}'
    page = ctx.new_page()
    try:
        if source == 'nga':
            nga_goto(page, url, ctx)
            skip_nga_ad(page, url, ctx)
        else:
            page.goto(url,wait_until='domcontentloaded',timeout=30000)
        selector = 'a.topic[id^="t_tt"]' if source == 'nga' else 'tbody[id^="normalthread_"] a.s.xst'
        if source=='chiphell':
            pager = page.locator('.pg input[name="custompage"]')
            if pager.count() and int(pager.first.input_value()) != page_number:
                raise HistoryUnavailable('history_page_out_of_range')
        try:
            page.locator(selector).first.wait_for(timeout=12000)
        except Exception:
            if source=='nga':
                check_nga_rate_limit(page)
            text = page.locator('body').inner_text()
            if source=='nga' and re.search(r'你可能需要\s*\[?登录\]?\s*后访问',text):
                raise HistoryUnavailable('login_required') from None
            raise
        records = page.locator(selector).evaluate_all('''nodes=>nodes.map(e=>{
            const row=e.closest('tr')||e.parentElement;
            const posted=row.querySelector('.postdate, [id^="t_pt"], td.by em span');
            return {title:e.innerText,url:e.href,rowText:row.innerText,platform_category:[...row.querySelectorAll('a[href*=typeid], .topic_tag')].map(n=>n.innerText).join(' / '),
                posted:posted?(posted.getAttribute('title')||posted.innerText):'',
                dates:[...row.querySelectorAll('[title]')].map(n=>n.getAttribute('title')).join(' ')};})''')
        result = []
        for item in records:
            if re.search(r'(?:^|[\]】])\s*[\[【](?:通知|公告|版规|必读)[\]】]', item['title']):
                continue
            query = parse_qs(urlparse(item['url']).query)
            sid = (query.get('tid') or [None])[0]
            if not sid:
                match = re.search(r'thread-(\d+)',item['url'])
                sid = match[1] if match else None
            if not sid:
                continue
            result.append({'id':sid,'title':item['title'],'url':item['url'],
                           'posted_at':date_text(item.get('posted','') or item['dates']+' '+item['rowText']),
                           'body_full':'','body_complete':False,'platform_category':item.get('platform_category','')})
        return result
    finally:
        page.close()


def detail(ctx, source: str, post: dict, *, page=None) -> dict:
    from pylon.shared.html_text import body_text, MAX_BODY_CHARS
    from pylon.tasks.chiphell.monitor import post_body_fields
    allowed = {'nga':'bbs.nga.cn','chiphell':'www.chiphell.com','v2ex':'www.v2ex.com'}
    if urlparse(post['url']).hostname not in {allowed[source], 'v2ex.com' if source=='v2ex' else allowed[source]}:
        raise ValueError('untrusted forum detail URL')
    if source == 'v2ex':
        from pylon.tasks.forums.v2ex import fetch, extract_topic_document
        from urllib.error import HTTPError
        try:
            if ctx is None:
                raw = fetch(post['url'],timeout=8,attempts=1,phase='marketplace_detail')
            else:
                response = ctx.get(post['url'])
                if response.status_code in {401,403,404,410}:
                    raise ContentUnavailable('permission_required' if response.status_code in {401,403} else 'deleted_or_not_found')
                if response.status_code == 429:
                    raise SourceRateLimited('v2ex_rate_limited')
                response.raise_for_status()
                raw = response.text
        except HTTPError as error:
            if error.code in {401,403,404,410}:
                raise ContentUnavailable('permission_required' if error.code in {401,403} else 'deleted_or_not_found') from None
            raise
        try:
            fields = extract_topic_document(raw,expected_url=post['url'],
                final_url=str(response.url) if ctx is not None and hasattr(response,'url') else None)
        except ValueError as error:
            raise ContentUnavailable(str(error)) from None
        return {**post,**fields,'title_refreshed':True}
    owned_page = page is None
    page = ctx.new_page() if owned_page else page
    try:
        if source == 'nga':
            nga_goto(page, post['url'], ctx)
            skip_nga_ad(page, post['url'], ctx)
        else:
            page.goto(post['url'],wait_until='domcontentloaded',timeout=30000)
        if not topic_identity(post['url'],source) or topic_identity(page.url,source)!=topic_identity(post['url'],source):
            raise ContentUnavailable('unexpected_topic_redirect')
        selector = '#postcontent0' if source=='nga' else 'td[id^="postmessage_"]'
        if source=='chiphell' and page.locator('#messagetext').count():
            reason = content_gap(page.locator('#messagetext').inner_text())
            if reason != 'body_missing_or_incomplete':
                raise ContentUnavailable(reason)
        try:
            page.locator(selector).first.wait_for(timeout=12000)
        except Exception:
            if source=='nga':
                check_nga_rate_limit(page)
            reason = content_gap(page.locator('body').inner_text())
            if reason != 'body_missing_or_incomplete':
                raise ContentUnavailable(reason) from None
            raise
        if source=='chiphell':
            fields = post_body_fields(page,260,expected_url=post['url'])
        else:
            raw = page.locator(selector).inner_html() if hasattr(page.locator(selector),'inner_html') else page.locator(selector).evaluate('e=>e.innerHTML')
            body = body_text(raw)
            fields = dict(body_full=body[:MAX_BODY_CHARS],body_complete=bool(body) and len(body)<=MAX_BODY_CHARS)
        dates = page.locator('[id^="postdate"], [id^="authorposton"]').all_text_contents()
        if not fields.get('platform_category') and post.get('platform_category'):
            fields['platform_category']=post['platform_category']
        title=''
        heading=page.locator('#postsubject0' if source=='nga' else '#thread_subject')
        if heading.count():title=heading.first.inner_text().strip()
        if not title:raise ContentUnavailable('topic_identity_missing')
        fields.update(title=title,title_refreshed=True,body_provenance={
            'version':1,'topic_id':topic_identity(post['url'],source),'container':selector,'identity_verified':True})
        fields['source_context']=author_context(page,source)
        return {**post,**fields,'posted_at':date_text(' '.join(dates)) or post.get('posted_at','')}
    finally:
        if owned_page:
            page.close()


def _refill_live_source(store, source: str, *, limit: int, deadline: float, ctx=None):
    from .collection import client, INTERVALS
    posts=store.pending(history=False,source=source,limit=limit)
    if not posts or (store.checkpoint(source+':cooldown') or {}).get('until',0)>time.time():
        return []
    errors=[]
    last_request=0.0
    owned=ctx is None
    cm=client(source) if owned else contextlib.nullcontext((ctx,None))
    try:
        with cm as (active,page):
            if page is None and source!='v2ex':
                page=active.new_page()
            try:
                for row in posts:
                    remaining=last_request+INTERVALS[source]-time.monotonic()
                    if remaining>0:
                        if time.monotonic()+remaining>=deadline:
                            break
                        time.sleep(remaining)
                    if time.monotonic()>=deadline:
                        break
                    last_request=time.monotonic()
                    try:
                        post=detail(active,source,{**row,'id':row['source_id']},page=page)
                        if not post['body_complete']:
                            store.body_error(row,'body_missing_or_incomplete',permanent=row['attempts']>=4)
                        else:
                            store.ingest(source,[post])
                    except SourceRateLimited:
                        store.body_error(row,'source_rate_limited')
                        store.checkpoint(source+':cooldown',{'until':time.time()+60})
                        errors.append(source+':source_rate_limited')
                        break
                    except ContentUnavailable as error:
                        store.body_error(row,str(error),permanent=True)
                    except Exception as error:
                        reason=type(error).__name__
                        store.body_error(row,reason,permanent=row['attempts']>=4)
                        errors.append(source+':'+reason)
            finally:
                if not owned and page is not None:
                    page.close()
    except Exception as error:
        reason='client_'+type(error).__name__
        errors.append(source+':'+reason)
        for row in posts:
            store.body_error(row,reason,permanent=row['attempts']>=4)
    return list(dict.fromkeys(errors))


def refill(store, *, history: bool = False, limit: int = 12,
           selected_sources: tuple[str,...] = ('v2ex','nga','chiphell'), ctx=None,
           deadline: float | None = None):
    if not selected_sources or set(selected_sources)-{'v2ex','chiphell','nga'}:
        raise ValueError('invalid body refill sources')
    if not history:
        from concurrent.futures import ThreadPoolExecutor
        if ctx is not None and len(selected_sources)!=1:
            raise ValueError('a client belongs to exactly one source')
        deadline=deadline if deadline is not None else time.monotonic()+120
        def run(source):
            return _refill_live_source(store,source,limit=limit,deadline=deadline,ctx=ctx)
        if len(selected_sources)==1:
            return run(selected_sources[0])
        with ThreadPoolExecutor(max_workers=len(selected_sources),thread_name_prefix='marketplace-live') as pool:
            results=list(pool.map(run,selected_sources))
        return [error for result in results for error in result]
    return _refill_history_compat(store,limit=limit)


def _refill_history_compat(store, *, limit: int = 12):
    """Bounded legacy helper; the archival coordinator uses collection.py."""
    import time
    history=True
    posts = store.pending(history=history,limit=limit)
    errors = []
    deadline = None if history else time.monotonic()+120
    source_order = ('v2ex','nga','chiphell')
    if posts and not history:
        offset = int((store.checkpoint('details:turn') or {}).get('offset',0)) % len(source_order)
        store.checkpoint('details:turn',{'offset':(offset+1)%len(source_order)})
        source_order = source_order[offset:] + source_order[:offset]
    for source in source_order:
        if source=='nga' and (store.checkpoint('nga:cooldown') or {}).get('until',0)>time.time():
            continue
        subset = [p for p in posts if p['source']==source]
        if not subset:
            continue
        if deadline is not None and time.monotonic()>=deadline:
            break
        cm = contextlib.nullcontext(None) if source=='v2ex' else browser(source)
        try:
            with cm as ctx:
                for row in subset:
                    if deadline is not None and time.monotonic()>=deadline:
                        break
                    try:
                        post = detail(ctx,source,{**row,'id':row['source_id']})
                        if not post['body_complete']:
                            store.body_error(row,'body_missing_or_incomplete',permanent=history or row['attempts']>=4)
                        else:
                            store.ingest(source,[post],history=history)
                    except SourceRateLimited:
                        store.body_error(row,'source_rate_limited')
                        store.checkpoint('nga:cooldown',{'until':time.time()+60})
                        errors.append(source+':source_rate_limited')
                        break
                    except ContentUnavailable as error:
                        store.body_error(row,str(error),permanent=True)
                    except Exception as error:
                        # Transient failures get a bounded, durable retry even during archival work.
                        store.body_error(row,type(error).__name__,permanent=row['attempts'] >= (2 if history else 4))
                    if history:
                        time.sleep(1)
        except Exception as error:
            reason = 'browser_'+type(error).__name__
            errors.append(source+':'+reason)
            for row in subset:
                store.body_error(row,reason,permanent=row['attempts'] >= (2 if history else 4))
    return errors
