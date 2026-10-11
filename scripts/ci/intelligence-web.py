"""Exercise both workspaces against an isolated database and a real browser."""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path


def main():
    with tempfile.TemporaryDirectory(prefix='pylon-web-ci-') as directory:
        root=Path(directory)
        os.environ.update(PYLON_HOME=str(root),PYLON_OUTBOX=str(root/'outbox.sqlite3'),
                          PYLON_PROFILE_ROOT=str(root/'profiles/webops'))
        from pylon.outbox import Outbox
        from pylon.tasks.intelligence.store import Store as IntelStore
        from pylon.tasks.marketplace import web,web_auth
        from pylon.tasks.marketplace.store import Store
        import uvicorn
        from playwright.sync_api import sync_playwright,expect

        box=Outbox();store=Store(box);intel=IntelStore(box)
        for number,(title,decision) in enumerate((('保留样本','push'),('排除样本','push'),('条件待确认','hold')),1):
            intel.ingest('linuxdo',str(number),title=title,body=title+'：Codex 提供可复用方法。',
                         url='https://example.invalid/topic/'+str(number),
                         evidence_scope='helper_excerpt',historical=True)
            result={'decision':decision,'categories':['B1'],'content_type':'tutorial',
                    'reason':title,'missing_evidence':['适用条件'] if decision=='hold' else [],
                    'facts':[{'category':'B1','kind':'method','subject':title,'value':title}]}
            with box.connect() as db:
                db.execute("UPDATE intel_items SET status='historical',result=? WHERE source_id=?",
                           (json.dumps(result,ensure_ascii=False),str(number)))

        app=web.create_app(store,secure=False)
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(128)
        port=listener.getsockname()[1];origin=f'http://127.0.0.1:{port}'
        server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
        thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True)
        thread.start()
        deadline=time.monotonic()+10
        while not server.started:
            if not thread.is_alive() or time.monotonic()>deadline:raise RuntimeError('web fixture did not start')
            time.sleep(.05)

        def token():
            with box.connect() as db:return web_auth.issue_login(db)

        def version():
            with box.connect() as db:
                row=db.execute('SELECT id,content FROM intel_versions ORDER BY id DESC LIMIT 1').fetchone()
                return row['id'],json.loads(row['content'])

        blocked=[]
        def route(request):
            if request.request.url.startswith(origin+'/'):request.continue_()
            else:blocked.append(request.request.resource_type);request.abort()

        def saved(page,action):
            with page.expect_response(lambda r:r.url.endswith('/pylon/intel/api/draft') and r.request.method=='POST') as response:
                action()
            assert response.value.status==200
            expect(page.locator('#save-state')).to_have_text('')

        def apply(page):
            page.get_by_role('button',name='预览影响',exact=True).click()
            expect(page.locator('#preview-dialog')).to_be_visible()
            with page.expect_response(lambda r:r.url.endswith('/pylon/intel/api/apply')) as response:
                page.get_by_role('button',name='确认应用',exact=True).click()
            assert response.value.status==200
            expect(page.locator('#preview-dialog')).not_to_be_visible()
            return response.value.json()['version']

        try:
            with sync_playwright() as playwright:
                browser=playwright.chromium.launch()
                try:
                    desktop=browser.new_context(viewport={'width':1360,'height':960})
                    desktop.route('**/*',route)
                    page=desktop.new_page();errors=[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    login=token()
                    page.goto(origin+'/pylon/intel/#login='+login)
                    expect(page.locator('#workspace')).to_be_visible()
                    assert '#' not in page.url
                    initial,_=version()

                    group=page.locator('details[data-group="B"]')
                    group.locator('summary').click()
                    saved(page,lambda:group.locator('[data-mode="block"]').click())
                    expect(group.locator('[data-category="B1"]')).to_be_disabled()
                    assert version()[0]==initial
                    saved(page,lambda:group.locator('[data-mode="partial"]').click())
                    saved(page,lambda:group.locator('[data-category="B1"]').uncheck())
                    saved(page,lambda:group.locator('[data-category="B1"]').check())
                    saved(page,lambda:group.locator('[data-source="linuxdo"][data-key="B1"]').uncheck())
                    saved(page,lambda:group.locator('[data-source="linuxdo"][data-key="B1"]').check())
                    field=page.locator('#global-exclude');field.fill('排除样本')
                    saved(page,lambda:field.press('Tab'))
                    page.reload();expect(page.locator('#workspace')).to_be_visible()
                    expect(page.locator('#global-exclude')).to_have_value('排除样本')
                    assert version()[0]==initial

                    page.get_by_role('button',name='预览影响',exact=True).click()
                    expect(page.locator('#preview-dialog')).to_be_visible()
                    expect(page.locator('.counts strong')).to_have_text(['1','1','1'])
                    assert version()[0]==initial
                    page.get_by_role('button',name='返回调整',exact=True).click()
                    assert apply(page)==initial+1
                    assert version()[1]['exclude_terms']['*']==['排除样本']

                    # Navigation must wait for the last blurred field to save.
                    page.locator('#global-exclude').fill('排除样本、尚未应用的草稿')
                    # The shared cookie opens the second workspace without a new login.
                    page.get_by_role('link',name='交易筛选',exact=True).click()
                    expect(page.locator('#workspace')).to_be_visible()
                    page.get_by_role('link',name='情报筛选',exact=True).click()
                    expect(page.locator('#global-exclude')).to_have_value('排除样本、尚未应用的草稿')
                    assert version()[0]==initial+1
                    page.get_by_role('button',name='恢复上一版本到草稿',exact=True).click()
                    expect(page.locator('#global-exclude')).to_have_value('')
                    assert version()[0]==initial+1
                    assert apply(page)==initial+2
                    assert not version()[1]['exclude_terms'].get('*')

                    state=desktop.request.get(origin+'/pylon/intel/api/state').json()
                    result=desktop.request.post(origin+'/pylon/intel/api/draft',
                        headers={'Origin':origin,'X-CSRF-Token':'wrong-fixture-token'},
                        data={'id':state['draft']['id'],'revision':state['draft']['revision'],
                              'content':state['draft']['content']})
                    assert result.status==403
                    assert desktop.request.post(origin+'/pylon/intel/api/login',data={'token':login}).status==401

                    mobile=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
                    mobile.route('**/*',route)
                    phone=mobile.new_page();phone.on('pageerror',lambda error:errors.append(str(error)))
                    phone.goto(origin+'/pylon/intel/#login='+token())
                    expect(phone.locator('#workspace')).to_be_visible()
                    assert phone.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                    phone.locator('#search').fill('支付')
                    expect(phone.locator('details[data-group="I"]')).to_be_visible()
                    assert phone.locator('details[data-group="I"]').get_attribute('open') is not None
                    expect(phone.locator('details[data-group="B"]')).not_to_be_visible()
                    phone.get_by_role('button',name='判断与待补信息',exact=True).click()
                    expect(phone.locator('#items .review')).to_have_count(3)
                    phone.get_by_role('button',name='退出登录',exact=True).click()
                    expect(phone.locator('#login')).to_be_visible()
                    expect(phone.locator('#workspace')).not_to_be_visible()
                    assert mobile.request.get(origin+'/pylon/intel/api/state').status==401
                    assert desktop.request.get(origin+'/pylon/intel/api/state').status==200
                    assert not errors,errors
                    assert not blocked,blocked
                    with box.connect() as db:assert db.execute('SELECT count(*) FROM events').fetchone()[0]==0
                    print('Desktop/mobile: draft, preview, apply, rollback, shared login, CSRF and logout passed; no notifications or external requests.')
                finally:browser.close()
        finally:
            server.should_exit=True;thread.join(timeout=10);listener.close()
            if thread.is_alive():raise RuntimeError('web fixture did not stop')


if __name__=='__main__':main()
