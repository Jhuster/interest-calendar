import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from app.config import Config, initialize
from app.main import create_app
from app.services.domain import create_invite
from app.services.usage import overview
from app.storage.database import Store
from app.storage.usage import UsageStore


@pytest.fixture
def site(tmp_path):
    config=Config(tmp_path,'http://127.0.0.1:8787',True)
    tokens=initialize(config); app=create_app(config)
    with TestClient(app,base_url=config.base_url) as c:
        c.headers['Origin']=config.base_url
        yield c,app,tokens


def login(c, token):
    result=c.post('/api/v1/session',json={'token':token})
    assert result.status_code==200
    c.headers['X-CSRF-Token']=result.json()['csrf_token']


def test_usage_is_admin_only_and_does_not_expose_account_content(site):
    c,app,t=site
    assert c.get('/usage').status_code==403
    assert c.get('/api/v1/admin/usage').status_code==401
    assert 'href="/usage"' not in c.get('/').text
    login(c,t['admin'])
    assert c.get('/usage').status_code==200
    assert 'href="/usage"' in c.get('/').text
    invited=c.post('/api/v1/admin/invites',json={'label':'private-label-must-not-leak'}).json()
    response=c.get('/api/v1/admin/usage')
    assert response.status_code==200
    assert response.json()['accounts']['total']==2
    assert 'private-label-must-not-leak' not in response.text
    assert invited['id'] not in response.text and invited['token'] not in response.text and t['agent'] not in response.text
    assert c.delete('/api/v1/session').status_code==200
    login(c,invited['token'])
    assert c.get('/usage').status_code==403
    assert c.get('/api/v1/admin/usage').status_code==403
    assert 'href="/usage"' not in c.get('/').text
    c.cookies.clear()
    assert c.get('/api/v1/admin/usage',headers={'Authorization':'Bearer '+t['agent']}).status_code==403


def test_usage_counts_conditional_get_not_head_invalid_or_automatic_activity(site):
    c,app,t=site
    first=c.get('/public/calendar.ics');assert first.status_code==200
    assert c.get('/public/calendar.ics',headers={'If-None-Match':first.headers['etag']}).status_code==304
    assert c.head('/public/calendar.ics').status_code==200
    assert c.get('/c/invalid-secret/calendar.ics').status_code==404
    with app.state.store.tx(True) as db: invited=create_invite(db,'private')
    personal='/c/'+invited['calendar_token']+'/calendar.ics'
    assert c.get(personal).status_code==200
    assert c.head(personal).status_code==200
    result=overview(app.state.store,app.state.usage)
    assert result['accounts']['active']==0  # Subscription polling is not a person using the website.
    assert result['subscriptions']==dict(requests=3,public_requests=2,personal_requests=1,personal_accounts=1,not_modified=1)
    assert c.get('/api/v1/agent/context',headers={'Authorization':'Bearer '+t['agent']}).status_code==200
    assert overview(app.state.store,app.state.usage)['accounts']['active']==0
    login(c,invited['token'])
    assert c.get('/interests').status_code==200
    assert c.get('/api/v1/status').status_code==200
    assert c.get('/interests').status_code==200
    result=overview(app.state.store,app.state.usage)
    assert result['accounts']['active']==1
    assert sum(row['active_accounts'] for row in result['daily'])==1
    data=app.state.usage.path.read_bytes()
    assert b'invalid-secret' not in data and invited['calendar_token'].encode() not in data
    assert t['agent'].encode() not in data and b'127.0.0.1' not in data
    assert app.state.usage.path.stat().st_mode & 0o777==0o600


def test_usage_beijing_window_business_counts_and_90_day_retention(tmp_path,monkeypatch):
    fixed=datetime(2026,11,24,8,tzinfo=timezone.utc)
    monkeypatch.setattr('app.services.usage.now',lambda:fixed)
    monkeypatch.setattr('app.storage.usage.now',lambda:fixed)
    store=Store(tmp_path);usage=UsageStore(tmp_path);usage.initialize()
    with store.tx(True) as db:
        public=db.execute("SELECT id FROM accounts WHERE kind='public'").fetchone()[0]
        for bid,when,status in [('outside','2026-11-17T15:59:59+00:00',202),('first','2026-11-17T16:00:00+00:00',202),('failed','2026-11-23T17:00:00+00:00',409)]:
            db.execute('INSERT INTO batches VALUES(?,?,?,?,?,?,?,?,?)',(bid,'epoch','hash',1,status,None,'{}',when,public))
        when='2026-11-23T17:00:00+00:00'
        db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?)',('secret-event','secret-uid',1,1,'{"title":"private content"}',1,None,when,when,when,public))
        db.execute('INSERT INTO audit_log(operation,object_id,created_at,result) VALUES(?,?,?,?)',('reject','secret-candidate',when,'ok'))
    with usage.connect() as db:
        db.execute('INSERT INTO daily_usage VALUES(?,?,?,?,?,?)',('2026-08-26','obsolete',0,1,7,0))
        db.execute('INSERT INTO daily_usage VALUES(?,?,?,?,?,?)',('2026-08-27','old-but-retained',0,1,2,0))
    usage.record(public,is_public=True,subscription=True)
    usage.record(public,is_public=True,subscription=True,not_modified=True)
    result=overview(store,usage)
    assert result['period']==dict(start_date='2026-11-18',end_date='2026-11-24')
    assert len(result['daily'])==7
    assert result['agent']['succeeded']==1 and result['agent']['failed']==1 and result['agent']['accounts']==1
    assert result['daily'][0]['batches_succeeded']==1
    assert result['daily'][-1]['batches_failed']==1 and result['daily'][-1]['events_created']==1
    assert result['calendar']['resolutions']['reject']==1
    assert result['subscriptions']['requests']==2
    assert 'private content' not in json.dumps(result) and 'secret-event' not in json.dumps(result)
    with usage.connect() as db: assert [r[0] for r in db.execute('SELECT day FROM daily_usage ORDER BY day')]==['2026-08-27','2026-11-24']


def test_usage_write_failure_does_not_break_subscriptions_or_login(site,monkeypatch):
    c,app,t=site
    def fail(*args,**kwargs):raise sqlite3.OperationalError('disk full')
    monkeypatch.setattr(app.state.usage,'record',fail)
    assert c.get('/public/calendar.ics').status_code==200
    assert app.state.usage_error is True
    assert c.get('/health').status_code==200 and app.state.usage_error is True
    login(c,t['admin'])
    result=c.get('/api/v1/admin/usage')
    assert result.status_code==200 and result.json()['tracking_error'] is True


def test_usage_persists_on_restart_and_calendar_restore(tmp_path):
    from app.services.maintenance import restore_database
    config=Config(tmp_path,'http://127.0.0.1:8787',True);initialize(config);app=create_app(config)
    with TestClient(app,base_url=config.base_url) as c:
        assert c.get('/public/calendar.ics').status_code==200
        before=overview(app.state.store,app.state.usage)
        backup=app.state.store.backup(force=True)
    restore_database(tmp_path,backup,apply=True)
    app=create_app(config)
    with TestClient(app,base_url=config.base_url):
        after=overview(app.state.store,app.state.usage)
        assert after['tracking_started_at']==before['tracking_started_at']
        assert after['subscriptions']['requests']==1


def test_health_checks_and_static_files_do_not_count_as_activity(site):
    c,app,t=site
    login(c,t['admin'])
    assert c.get('/health').status_code==200
    assert c.get('/static/app.js').status_code==200
    assert overview(app.state.store,app.state.usage)['accounts']['active']==0


def test_page_views_and_anonymous_visitors_exclude_background_requests(site):
    c,app,t=site
    response=c.get('/')
    visitor=c.cookies.get('calendar_visitor')
    assert visitor and len(visitor)==32
    cookie=response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'SameSite=lax' in cookie and 'Max-Age=7776000' in cookie
    assert c.get('/').status_code==200
    assert c.get('/interests').status_code==200
    assert c.get('/apply').status_code==200
    assert c.get('/api/v1/status').status_code==200
    assert c.get('/static/app.js').status_code==200
    assert c.get('/health').status_code==200
    c.head('/')
    assert c.get('/public/calendar.ics').status_code==200
    assert c.get('/missing-page',follow_redirects=False).status_code==303
    before=overview(app.state.store,app.state.usage)
    assert before['traffic']['page_views']==4 and before['traffic']['unique_visitors']==1
    assert before['accounts']['active']==0
    assert visitor.encode() not in app.state.usage.path.read_bytes()
    c.cookies.clear()
    assert c.get('/').status_code==200
    after=overview(app.state.store,app.state.usage)
    assert after['traffic']['page_views']==5 and after['traffic']['unique_visitors']==2
    login(c,t['admin'])
    assert c.get('/api/v1/admin/usage?days=30').json()['window_days']==30
    assert len(c.get('/api/v1/admin/usage?days=30').json()['daily'])==30
    assert c.get('/api/v1/admin/usage?days=0').status_code==422
    assert c.get('/api/v1/admin/usage?days=10000').status_code==422


def test_traffic_unique_visitors_are_deduplicated_across_days(tmp_path,monkeypatch):
    clock=[datetime(2026,11,23,8,tzinfo=timezone.utc)]
    monkeypatch.setattr('app.storage.usage.now',lambda:clock[0])
    monkeypatch.setattr('app.services.usage.now',lambda:clock[0])
    store=Store(tmp_path);usage=UsageStore(tmp_path);usage.initialize()
    with usage.connect() as db:
        db.execute("UPDATE metadata SET value=? WHERE key='traffic_started_at'",(clock[0].isoformat(),))
        db.execute('INSERT INTO website_daily VALUES(?,?)',('2026-08-25',100))
        db.execute('INSERT INTO daily_visitors VALUES(?,?)',('2026-08-25','expired-hash'))
    usage.record_pageview('a'*32);usage.record_pageview('a'*32)
    clock[0]=datetime(2026,11,24,8,tzinfo=timezone.utc)
    usage.record_pageview('a'*32);usage.record_pageview('b'*32)
    result=overview(store,usage)
    assert result['traffic']['page_views']==4 and result['traffic']['unique_visitors']==2
    assert result['daily'][-2]['unique_visitors']==1 and result['daily'][-1]['unique_visitors']==2
    assert result['daily'][0]['page_views'] is None
    assert result['daily'][-2]['page_views']==2 and result['daily'][-1]['page_views']==2
    with usage.connect() as db:
        assert not db.execute("SELECT 1 FROM daily_visitors WHERE visitor_hash='expired-hash'").fetchone()
        assert not db.execute("SELECT 1 FROM website_daily WHERE day='2026-08-25'").fetchone()


def test_traffic_write_failure_does_not_break_page(site,monkeypatch):
    c,app,t=site
    def fail(*args,**kwargs): raise sqlite3.OperationalError('disk full')
    monkeypatch.setattr(app.state.usage,'record_pageview',fail)
    assert c.get('/').status_code==200
    assert app.state.usage_error is True
