import copy,json,uuid
from datetime import timedelta
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from icalendar import Calendar
from ics import Calendar as IndependentCalendar
from app.config import Config,initialize
from app.main import create_app
from app.models.protocol import now,TZ,canonical,Problem,VALIDATOR,days
from app.services.calendar import publish
from app.services.domain import import_batch,settings,context

@pytest.fixture
def site(tmp_path):
    config=Config(tmp_path,'http://127.0.0.1:8787',True);tokens=initialize(config);app=create_app(config)
    with TestClient(app,base_url=config.base_url) as c:
        c.headers['Origin']=config.base_url
        login=c.post('/api/v1/session',json={'token':tokens['admin']});assert login.status_code==200,login.text
        c.headers['X-CSRF-Token']=login.json()['csrf_token']
        yield c,app,tokens

def interest(c,name='测试兴趣（虚构）'):
    s=c.get('/api/v1/status').json();r=c.post('/api/v1/interests',json={'keyword':name,'conditions':'仅测试','state_epoch':s['state_epoch'],'config_version':s['config_version']});assert r.status_code==201,r.text;return r.json()['id']
def batch(c,ids):
    ctx=c.get('/api/v1/agent/context').json();d=(now().astimezone(TZ)+timedelta(days=3)).date()
    p=json.loads(Path('src/examples/protocol/valid-create.json').read_text());p.update(batch_id=str(uuid.uuid4()),state_epoch=ctx['state_epoch'],config_version=ctx['config_version'],collection_started_at=ctx['server_time'],generated_at=now().isoformat(),window=ctx['window'])
    e=p['events'][0];e['source_key']['id']=str(uuid.uuid4());e['interest_ids']=ids;e['timing'].update(start_date=str(d),end_date_exclusive=str(d+timedelta(days=1)));e['evidence'][0]['verified_at']=ctx['server_time'];return p
def upload(c,t,p):return c.post('/api/v1/batches',json=p,headers={'Authorization':'Bearer '+t['agent']})
def renewed(p):
    p=copy.deepcopy(p);p['batch_id']=str(uuid.uuid4());return p

def test_full_lifecycle_and_independent_parser(site):
    c,a,t=site;i=interest(c);p=batch(c,[i]);r=upload(c,t,p);assert r.status_code==202,r.text
    publish(a.state.store);first=c.get('/calendar.ics');assert len(IndependentCalendar(first.text).events)==1
    original=Calendar.from_ical(first.content).walk('VEVENT')[0];eid=r.json()['event_mappings'][0]['event_id']
    assert c.get('/calendar.ics',headers={'If-None-Match':first.headers['etag']}).status_code==304
    assert c.head('/calendar.ics').content==b''
    assert upload(c,t,p).status_code==200
    q=renewed(p);q['events'][0].update(event_id=eid,base_version=1);q['events'][0]['timing']['start_date']=str((now().astimezone(TZ)+timedelta(days=4)).date());q['events'][0]['timing']['end_date_exclusive']=str((now().astimezone(TZ)+timedelta(days=5)).date())
    assert upload(c,t,q).status_code==202;publish(a.state.store)
    updated=Calendar.from_ical(c.get('/calendar.ics').content).walk('VEVENT')[0];assert original['UID']==updated['UID'];assert updated['SEQUENCE']==original['SEQUENCE']+1
    cancel=renewed(q);cancel['events'][0].update(base_version=2,status='cancelled');cancel['events'][0]['evidence'][0]['excerpt']='明确取消（虚构测试）'
    assert upload(c,t,cancel).status_code==202;publish(a.state.store)
    final=Calendar.from_ical(c.get('/calendar.ics').content).walk('VEVENT')[0];assert final['STATUS']=='CANCELLED';assert final['UID']==original['UID'];assert final['SEQUENCE']==updated['SEQUENCE']+1
    empty=renewed(p);empty.update(events=[],result='empty');assert upload(c,t,empty).status_code==200;assert publish(a.state.store);assert len(Calendar.from_ical(c.get('/calendar.ics').content).walk('VEVENT'))==1

def test_atomic_rejection_replay_and_stale(site):
    c,a,t=site;i=interest(c);p=batch(c,[i]);bad=copy.deepcopy(p['events'][0]);bad['source_key']['id']='another';bad['interest_ids']=[str(uuid.uuid4())];p['events'].append(bad)
    r=upload(c,t,p);assert r.status_code==409,r.text;assert c.get('/api/v1/events').json()['total']==0
    assert upload(c,t,p).status_code==409
    p['events'].pop();assert upload(c,t,p).status_code==409
    p=renewed(p);r=upload(c,t,p);assert r.status_code==202
    interest(c,'第二兴趣');assert upload(c,t,p).status_code==200
    q=renewed(p);assert upload(c,t,q).status_code==409
    q['state_epoch']=str(uuid.uuid4());assert upload(c,t,q).json()['errors'][0]['code']=='STATE_RESET'

def test_privileges_origin_revocation_and_private_paths(site):
    c,a,t=site
    for path in ['/data/calendar.sqlite3','/credentials.json','/docs','/openapi.json']:
        assert c.get(path).status_code==404
    no=c.post('/api/v1/interests',json={},headers={'X-CSRF-Token':'bad'});assert no.status_code==403
    assert c.post('/api/v1/interests',json={},headers={'Origin':'https://evil.test'}).status_code==403
    assert c.post('/api/v1/interests',json={},headers={'Authorization':'Bearer '+t['agent']}).status_code==403
    assert c.post('/api/v1/batches',json={}).status_code==403
    initialize(a.state.config,'admin');assert c.get('/api/v1/status').status_code==401
    initialize(a.state.config,'agent');assert c.get('/api/v1/agent/context',headers={'Authorization':'Bearer '+t['agent']}).status_code==401

def test_unfollow_shared_history_and_restore_uid(site):
    c,a,t=site;i=interest(c);j=interest(c,'共享兴趣');p=batch(c,[i]);q=batch(c,[i,j]);q['events'][0]['title']='共享活动';p['events']+=q['events'];p['generated_at']=now().isoformat();assert upload(c,t,p).status_code==202
    publish(a.state.store);old=c.get('/api/v1/events').json()['items'];s=c.get('/api/v1/status').json()
    assert c.request('DELETE','/api/v1/interests/'+i,json={'state_epoch':s['state_epoch'],'config_version':s['config_version']}).status_code==200
    publish(a.state.store);visible=c.get('/api/v1/events').json()['items'];assert len(visible)==1;assert visible[0]['title']=='共享活动'
    ctx=c.get('/api/v1/agent/context').json();hidden=next(e for e in ctx['events'] if not e['calendar_visible']);r=batch(c,[j]);proposal=copy.deepcopy(p['events'][0]);proposal.update(event_id=hidden['id'],base_version=hidden['version'],interest_ids=[j]);r['events']=[proposal]
    assert upload(c,t,r).status_code==202;assert c.get('/api/v1/events/'+hidden['id']).json()['uid']==hidden['uid']

def test_publish_failure_race_restart_and_backup(site,monkeypatch):
    c,a,t=site;i=interest(c);p=batch(c,[i]);upload(c,t,p);publish(a.state.store);old=c.get('/calendar.ics').content
    q=batch(c,[i]);q['events'][0]['title']='另一个活动';upload(c,t,q)
    import app.services.calendar as module
    original=module.render
    def fail(rows):raise RuntimeError('fault')
    monkeypatch.setattr(module,'render',fail)
    with pytest.raises(RuntimeError):publish(a.state.store)
    assert c.get('/calendar.ics').content==old
    def raced(rows):
        with a.state.store.tx(True) as db:db.execute('UPDATE settings SET data_revision=data_revision+1')
        return original(rows)
    monkeypatch.setattr(module,'render',raced);assert publish(a.state.store) is False;assert c.get('/calendar.ics').content==old
    monkeypatch.setattr(module,'render',original);assert publish(a.state.store);assert c.get('/calendar.ics').content!=old
    backup=a.state.store.backup();import sqlite3
    with sqlite3.connect(backup) as db:assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok';assert db.execute('SELECT count(*) FROM events').fetchone()[0]==2

def test_candidates_duplicate_reject_reopen_merge(site):
    c,a,t=site;i=interest(c);p=batch(c,[i]);r=upload(c,t,p);assert r.status_code==202
    q=batch(c,[i]);assert upload(c,t,q).status_code==200
    candidate=c.get('/api/v1/candidates').json()['items'][0];assert candidate['payload']['reason']=='possible_duplicate'
    assert upload(c,t,renewed(q)).status_code==200;assert len(c.get('/api/v1/candidates').json()['items'])==1
    s=c.get('/api/v1/status').json();b={'state_epoch':s['state_epoch'],'version':1,'action':'reject'}
    assert c.post('/api/v1/candidates/'+candidate['id']+'/resolve',json=b).status_code==200
    assert upload(c,t,renewed(q)).status_code==409
    b.update(version=2,action='reopen');assert c.post('/api/v1/candidates/'+candidate['id']+'/resolve',json=b).status_code==200
    b.update(version=3,action='merge',target_event_id=r.json()['event_mappings'][0]['event_id'],target_version=1,proposed_event=q['events'][0]);assert c.post('/api/v1/candidates/'+candidate['id']+'/resolve',json=b).status_code==200
    assert c.get('/api/v1/events').json()['total']==1
    assert len(c.get('/api/v1/agent/context').json()['source_mappings'])==2

def test_json_limits_and_html_escape(site):
    c,a,t=site;h={'Authorization':'Bearer '+t['agent'],'Content-Type':'application/json'}
    for raw in ['{"a":1,"a":2}','{"a":NaN}','[]']:
        assert c.post('/api/v1/batches',content=raw,headers=h).status_code==400
    assert c.post('/api/v1/batches',content=b'x'*(2*1024*1024+1),headers=h).status_code==413
    for path in ['/','/interests','/settings','/candidates','/runs']:
        r=c.get(path);assert r.status_code==200;assert '<html lang="zh-CN">' in r.text;assert 'Content-Security-Policy' in r.headers

def test_contract_examples():
    for case in json.loads(Path('src/examples/protocol/cases.json').read_text()):
        data=json.loads((Path('src/examples/protocol')/case['file']).read_text())
        assert VALIDATOR.is_valid(data)==case['schema_valid'],case['file']

def test_first_start_token_and_http_login(tmp_path,capsys):
    config=Config(tmp_path,'http://192.168.1.20:8787',False)
    with TestClient(create_app(config),base_url=config.base_url) as c:
        credentials=tmp_path/'first-run-credentials.txt'
        token=dict(line.split('=',1) for line in credentials.read_text().splitlines())['ADMIN_TOKEN']
        assert token in capsys.readouterr().out
        assert credentials.stat().st_mode & 0o777 == 0o600
        assert c.get('/api/v1/status').status_code==401
        assert c.get('/',follow_redirects=False).status_code==303
        response=c.post('/api/v1/session',json={'token':token},headers={'Origin':config.base_url})
        assert response.status_code==200
        assert 'Secure' not in response.headers['set-cookie']
        assert c.get('/api/v1/status').status_code==200
    with TestClient(create_app(config),base_url=config.base_url):
        assert token in capsys.readouterr().out

def test_optimistic_version_and_evidence_only_sequence(site):
    c,a,t=site;i=interest(c);p=batch(c,[i]);r=upload(c,t,p);eid=r.json()['event_mappings'][0]['event_id'];q=renewed(p);q['events'][0].update(event_id=eid,base_version=1);q['events'][0]['evidence'][0]['excerpt']='重新核验，时间未变化'
    assert upload(c,t,q).status_code==200
    e=c.get('/api/v1/events/'+eid).json();assert e['version']==2;assert e['sequence']==0
    stale=renewed(q);stale['events'][0]['title']='过期覆盖';assert upload(c,t,stale).status_code==409
    same=renewed(q);same['events'][0]['base_version']=2;assert upload(c,t,same).json()['counts']['unchanged']==1
    assert c.get('/api/v1/events/'+eid).json()['version']==2

def test_unknown_end_unicode_and_midnight(site,monkeypatch):
    import app.models.protocol as protocol
    c,a,t=site;i=interest(c);p=batch(c,[i]);day=p['events'][0]['timing']['start_date'];p['events'][0]['title']='中文；逗号,换行\n'+('非常长的日程标题'*15);p['events'][0]['timing']={'kind':'timed','start':day+'T19:00:00-04:00','end':None}
    assert upload(c,t,p).status_code==202;publish(a.state.store);blob=c.get('/calendar.ics').content
    assert all(len(line)<=75 for line in blob.split(b'\r\n'));parsed=Calendar.from_ical(blob).walk('VEVENT')[0];assert 'DTEND' not in parsed;assert str(parsed['SUMMARY'])==p['events'][0]['title'];assert len(IndependentCalendar(blob.decode()).events)==1
    sample=json.loads(Path('src/examples/protocol/valid-midnight.json').read_text());monkeypatch.setattr(protocol,'now',lambda:protocol.dt('2026-09-28T00:15:00+08:00'));protocol.batch_check(sample)

def test_postponement_and_restart(tmp_path):
    config=Config(tmp_path);tokens=initialize(config);app=create_app(config)
    with TestClient(app,base_url=config.base_url) as c:
        c.headers['Origin']=config.base_url;c.headers['X-CSRF-Token']=c.post('/api/v1/session',json={'token':tokens['admin']}).json()['csrf_token'];i=interest(c);p=batch(c,[i]);r=upload(c,tokens,p);eid=r.json()['event_mappings'][0]['event_id'];q=renewed(p);q['events']=[];q['candidates']=[dict(client_candidate_id='postponed-example',base_candidate_version=0,related_event_id=eid,base_version=1,title='明确延期（虚构）',interest_ids=[i],reason='postponed',proposed_event=None,evidence=p['events'][0]['evidence'],notes='官方延期，新日期未知')]
        assert upload(c,tokens,q).status_code==200
        candidate=c.get('/api/v1/candidates').json()['items'][0];assert c.get('/api/v1/events').json()['total']==1
        assert c.post('/api/v1/candidates/'+candidate['id']+'/resolve',json={'state_epoch':p['state_epoch'],'version':1,'action':'withdraw','target_version':1}).status_code==200
        assert c.get('/api/v1/events').json()['total']==0
    with TestClient(create_app(config),base_url=config.base_url) as c:
        assert len(Calendar.from_ical(c.get('/calendar.ics').content).walk('VEVENT'))==0
        with app.state.store.tx() as db:
            row=db.execute('SELECT * FROM events').fetchone();assert not row['calendar_visible'];assert json.loads(row['payload_json'])['status']=='confirmed';assert row['withdrawal_reason']=='postponed'

def test_single_process_lock_and_lan_configuration(site):
    c,a,t=site
    with pytest.raises(RuntimeError):
        with TestClient(create_app(a.state.config),base_url=a.state.config.base_url):pass
    with pytest.raises(ValueError):Config(Path('/tmp/unused'),'http://192.168.1.5:8787',True).validate()
    Config(Path('/tmp/unused'),'http://192.168.1.5:8787',False).validate()

def test_multiday_date_shown_on_first_day_and_old_snapshot_refreshed(site):
    c,a,t=site;i=interest(c);p=batch(c,[i]);start=days(p['events'][0]['timing'])[0]
    p['events'][0]['timing']['end_date_exclusive']=str(start+timedelta(days=30))
    r=upload(c,t,p);assert r.status_code==202
    eid=r.json()['event_mappings'][0]['event_id'];publish(a.state.store)
    first=c.get('/calendar.ics');e=Calendar.from_ical(first.content).walk('VEVENT')[0]
    assert e.decoded('DTSTART')==start
    assert e.decoded('DTEND')==start+timedelta(days=1)
    assert '共30天' in str(e['SUMMARY'])
    assert str(start+timedelta(days=29)) in str(e['DESCRIPTION'])
    assert c.get('/api/v1/events/'+eid).json()['timing']==p['events'][0]['timing']
    with a.state.store.tx(True) as db:
        db.execute("UPDATE publications SET ics_blob=replace(CAST(ics_blob AS TEXT), 'X-INTEREST-DISPLAY-VERSION:3', 'X-INTEREST-DISPLAY-VERSION:1')")
        db.execute('UPDATE publications SET ics_blob=CAST(ics_blob AS BLOB)')
    assert publish(a.state.store)
    assert b'X-INTEREST-DISPLAY-VERSION:3' in c.get('/calendar.ics').content
    with a.state.store.tx() as db: count=db.execute('SELECT count(*) FROM publications').fetchone()[0]
    assert publish(a.state.store)
    with a.state.store.tx() as db: assert db.execute('SELECT count(*) FROM publications').fetchone()[0]==count

@pytest.mark.parametrize('scope', ['calendar', 'all'])
def test_admin_reset_atomic_and_stale_request(site,scope):
    c,a,t=site;i=interest(c);p=batch(c,[i]);assert upload(c,t,p).status_code==202
    publish(a.state.store);ctx=c.get('/api/v1/agent/context').json()
    body={'scope':scope,'confirmation':'RESET','state_epoch':ctx['state_epoch'],'config_version':ctx['config_version']}
    assert c.post('/api/v1/admin/reset',json=body,headers={'Authorization':'Bearer '+t['agent']}).status_code==403
    assert c.post('/api/v1/admin/reset',json=body,headers={'X-CSRF-Token':'bad'}).status_code==403
    assert c.post('/api/v1/admin/reset',json={**body,'scope':'invalid'}).status_code==422
    assert c.get('/api/v1/events').json()['total']==1
    assert c.post('/api/v1/admin/reset',json=body).status_code==200
    assert not Calendar.from_ical(c.get('/calendar.ics').content).walk('VEVENT')
    assert len(c.get('/api/v1/interests').json()['items'])==(1 if scope=='calendar' else 0)
    assert c.post('/api/v1/admin/reset',json=body).status_code==409
    assert upload(c,t,p).json()['errors'][0]['code']=='STATE_RESET'
    with a.state.store.tx() as db:
        assert db.execute("SELECT count(*) FROM audit_log WHERE operation='reset'").fetchone()[0]==1

def test_subscription_sequence_increases_when_multiday_shortened(site):
    c,a,t=site;i=interest(c);p=batch(c,[i]);start=days(p['events'][0]['timing'])[0]
    p['events'][0]['timing']['end_date_exclusive']=str(start+timedelta(days=30))
    r=upload(c,t,p);publish(a.state.store)
    before=Calendar.from_ical(c.get('/calendar.ics').content).walk('VEVENT')[0]
    q=renewed(p);q['events'][0].update(event_id=r.json()['event_mappings'][0]['event_id'],base_version=1)
    q['events'][0]['timing']['end_date_exclusive']=str(start+timedelta(days=1))
    assert upload(c,t,q).status_code==202;publish(a.state.store)
    after=Calendar.from_ical(c.get('/calendar.ics').content).walk('VEVENT')[0]
    assert after['UID']==before['UID'] and after['SEQUENCE']>before['SEQUENCE']


def test_saved_admin_token_restart_rotation_and_legacy(tmp_path):
    config=Config(tmp_path)
    tokens=initialize(config)
    assert config.saved_token('admin')==tokens['admin']
    assert initialize(config)=={}
    assert Config(tmp_path).saved_token('admin')==tokens['admin']
    legacy=config.credentials();legacy.pop('admin_token')
    (tmp_path/'credentials.json').write_text(json.dumps(legacy))
    assert config.saved_token('admin') is None
    (tmp_path/'first-run-credentials.txt').write_text('ADMIN_TOKEN='+tokens['admin'])
    assert config.saved_token('admin')==tokens['admin']
    rotated=initialize(config,'admin')
    assert config.saved_token('admin')==rotated['admin']!=tokens['admin']
