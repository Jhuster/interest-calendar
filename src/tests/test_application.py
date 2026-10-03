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
def feed(app):
    assert app.state.config.saved_token('calendar')
    return '/public/calendar.ics'
def renewed(p):
    p=copy.deepcopy(p);p['batch_id']=str(uuid.uuid4());return p

def test_full_lifecycle_and_independent_parser(site):
    c,a,t=site;i=interest(c);p=batch(c,[i]);r=upload(c,t,p);assert r.status_code==202,r.text
    publish(a.state.store);first=c.get(feed(a));assert len(IndependentCalendar(first.text).events)==1
    original=Calendar.from_ical(first.content).walk('VEVENT')[0];eid=r.json()['event_mappings'][0]['event_id']
    assert c.get(feed(a),headers={'If-None-Match':first.headers['etag']}).status_code==304
    assert c.head(feed(a)).content==b''
    assert upload(c,t,p).status_code==200
    q=renewed(p);q['events'][0].update(event_id=eid,base_version=1);q['events'][0]['timing']['start_date']=str((now().astimezone(TZ)+timedelta(days=4)).date());q['events'][0]['timing']['end_date_exclusive']=str((now().astimezone(TZ)+timedelta(days=5)).date())
    assert upload(c,t,q).status_code==202;publish(a.state.store)
    updated=Calendar.from_ical(c.get(feed(a)).content).walk('VEVENT')[0];assert original['UID']==updated['UID'];assert updated['SEQUENCE']==original['SEQUENCE']+1
    cancel=renewed(q);cancel['events'][0].update(base_version=2,status='cancelled');cancel['events'][0]['evidence'][0]['excerpt']='明确取消（虚构测试）'
    assert upload(c,t,cancel).status_code==202;publish(a.state.store)
    final=Calendar.from_ical(c.get(feed(a)).content).walk('VEVENT')[0];assert final['STATUS']=='CANCELLED';assert final['UID']==original['UID'];assert final['SEQUENCE']==updated['SEQUENCE']+1
    empty=renewed(p);empty.update(events=[],result='empty');assert upload(c,t,empty).status_code==200;assert publish(a.state.store);assert len(Calendar.from_ical(c.get(feed(a)).content).walk('VEVENT'))==1

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
    for path in ['/data/calendar.sqlite3','/credentials.json','/docs','/openapi.json','/settings']:
        assert c.get(path).status_code==404
    no=c.post('/api/v1/interests',json={},headers={'X-CSRF-Token':'bad'});assert no.status_code==403
    assert c.post('/api/v1/interests',json={},headers={'Origin':'https://evil.test'}).status_code==403
    assert c.post('/api/v1/interests',json={},headers={'Authorization':'Bearer '+t['agent']}).status_code==403
    assert c.post('/api/v1/batches',json={}).status_code==403
    initialize(a.state.config,'admin')
    stale=c.get('/api/v1/status');assert stale.status_code==200 and stale.json()['role'] is None
    assert c.get('/api/v1/agent/token').status_code==401
    initialize(a.state.config,'agent');assert c.get('/api/v1/agent/context',headers={'Authorization':'Bearer '+t['agent']}).status_code==401

def test_unfollow_shared_history_and_restore_uid(site):
    c,a,t=site;i=interest(c);j=interest(c,'共享兴趣');p=batch(c,[i]);q=batch(c,[i,j]);q['events'][0]['title']='共享活动';p['events']+=q['events'];p['generated_at']=now().isoformat();assert upload(c,t,p).status_code==202
    publish(a.state.store);old=c.get('/api/v1/events').json()['items'];s=c.get('/api/v1/status').json()
    assert c.request('DELETE','/api/v1/interests/'+i,json={'state_epoch':s['state_epoch'],'config_version':s['config_version']}).status_code==200
    publish(a.state.store);visible=c.get('/api/v1/events').json()['items'];assert len(visible)==1;assert visible[0]['title']=='共享活动'
    ctx=c.get('/api/v1/agent/context').json();hidden=next(e for e in ctx['events'] if not e['calendar_visible']);r=batch(c,[j]);proposal=copy.deepcopy(p['events'][0]);proposal.update(event_id=hidden['id'],base_version=hidden['version'],interest_ids=[j]);r['events']=[proposal]
    assert upload(c,t,r).status_code==202;assert c.get('/api/v1/events/'+hidden['id']).json()['uid']==hidden['uid']

def test_publish_failure_race_restart_and_backup(site,monkeypatch):
    c,a,t=site;i=interest(c);p=batch(c,[i]);upload(c,t,p);publish(a.state.store);old=c.get(feed(a)).content
    q=batch(c,[i]);q['events'][0]['title']='另一个活动';upload(c,t,q)
    import app.services.calendar as module
    original=module.render
    def fail(rows):raise RuntimeError('fault')
    monkeypatch.setattr(module,'render',fail)
    with pytest.raises(RuntimeError):publish(a.state.store)
    assert c.get(feed(a)).content==old
    def raced(rows):
        with a.state.store.tx(True) as db:db.execute('UPDATE settings SET data_revision=data_revision+1')
        return original(rows)
    monkeypatch.setattr(module,'render',raced);assert publish(a.state.store) is False;assert c.get(feed(a)).content==old
    monkeypatch.setattr(module,'render',original);assert publish(a.state.store);assert c.get(feed(a)).content!=old
    backup=a.state.store.backup(force=True);import sqlite3
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
    for path in ['/','/interests','/subscribe','/model','/admin','/candidates','/runs']:
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
        output=capsys.readouterr().out
        assert token not in output
        assert '凭据文件' in output
        assert credentials.stat().st_mode & 0o777 == 0o600
        assert config.saved_token('admin')==token
        anonymous=c.get('/api/v1/status');assert anonymous.status_code==200
        assert anonymous.json()['role'] is None and anonymous.json()['subscription_url']=='http://192.168.1.20:8787/public/calendar.ics'
        home=c.get('/',follow_redirects=False);assert home.status_code==200
        assert 'session' not in home.headers.get('set-cookie','')
        response=c.post('/api/v1/session',json={'token':token},headers={'Origin':config.base_url})
        assert response.status_code==200
        assert 'Secure' not in response.headers['set-cookie']
        assert c.get('/api/v1/status').status_code==200
    with TestClient(create_app(config),base_url=config.base_url):
        output=capsys.readouterr().out
        assert token not in output
        assert config.saved_token('admin')==token

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
    assert upload(c,t,p).status_code==202;publish(a.state.store);blob=c.get(feed(a)).content
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
        assert len(Calendar.from_ical(c.get(feed(app)).content).walk('VEVENT'))==0
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
    first=c.get(feed(a));e=Calendar.from_ical(first.content).walk('VEVENT')[0]
    assert e.decoded('DTSTART')==start
    assert e.decoded('DTEND')==start+timedelta(days=1)
    assert '共30天' in str(e['SUMMARY'])
    assert str(start+timedelta(days=29)) in str(e['DESCRIPTION'])
    assert c.get('/api/v1/events/'+eid).json()['timing']==p['events'][0]['timing']
    with a.state.store.tx(True) as db:
        db.execute("UPDATE publications SET ics_blob=replace(CAST(ics_blob AS TEXT), 'X-INTEREST-DISPLAY-VERSION:3', 'X-INTEREST-DISPLAY-VERSION:1')")
        db.execute('UPDATE publications SET ics_blob=CAST(ics_blob AS BLOB)')
    assert publish(a.state.store)
    assert b'X-INTEREST-DISPLAY-VERSION:3' in c.get(feed(a)).content
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
    assert not Calendar.from_ical(c.get(feed(a)).content).walk('VEVENT')
    assert len(c.get('/api/v1/interests').json()['items'])==(1 if scope=='calendar' else 0)
    assert c.post('/api/v1/admin/reset',json=body).status_code==409
    assert upload(c,t,p).json()['errors'][0]['code']=='STATE_RESET'
    with a.state.store.tx() as db:
        assert db.execute("SELECT count(*) FROM audit_log WHERE operation='reset'").fetchone()[0]==1

def test_subscription_sequence_increases_when_multiday_shortened(site):
    c,a,t=site;i=interest(c);p=batch(c,[i]);start=days(p['events'][0]['timing'])[0]
    p['events'][0]['timing']['end_date_exclusive']=str(start+timedelta(days=30))
    r=upload(c,t,p);publish(a.state.store)
    before=Calendar.from_ical(c.get(feed(a)).content).walk('VEVENT')[0]
    q=renewed(p);q['events'][0].update(event_id=r.json()['event_mappings'][0]['event_id'],base_version=1)
    q['events'][0]['timing']['end_date_exclusive']=str(start+timedelta(days=1))
    assert upload(c,t,q).status_code==202;publish(a.state.store)
    after=Calendar.from_ical(c.get(feed(a)).content).walk('VEVENT')[0]
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

def test_startup_token_stays_out_of_noninteractive_output():
    from io import StringIO
    from app.services.maintenance import startup_token_message
    class Tty(StringIO):
        def isatty(self): return True
    token='secret-admin-token'
    assert token in startup_token_message(token,Path('/tmp/credentials.json'),Tty().isatty())
    hidden=startup_token_message(token,Path('/tmp/credentials.json'),StringIO().isatty())
    assert token not in hidden and '凭据文件' in hidden

def test_backup_failure_is_visible(tmp_path,caplog,monkeypatch):
    import logging
    from app.services.maintenance import record_backup
    config=Config(tmp_path,'http://127.0.0.1:8787',True);tokens=initialize(config)
    def boom(self,force=False): raise OSError('disk full')
    monkeypatch.setattr('app.storage.database.Store.backup',boom)
    with caplog.at_level(logging.ERROR,logger='uvicorn.error'):
        with TestClient(create_app(config),base_url=config.base_url) as c:
            assert '备份失败' in caplog.text and 'disk full' in caplog.text
            logged=len([r for r in caplog.records if r.message=='备份失败'])
            c.headers['Origin']=config.base_url
            c.headers['X-CSRF-Token']=c.post('/api/v1/session',json={'token':tokens['admin']}).json()['csrf_token']
            assert c.get('/api/v1/status').json()['backup_error']=='disk full'
            state=type('State',(),{'backup_error':'disk full'})()
            class Failed:
                def backup(self,force=False): raise OSError('disk full')
            record_backup(Failed(),state)
            assert state.backup_error=='disk full'
            assert len([r for r in caplog.records if r.message=='备份失败'])==logged

def test_log_rotation_permissions(tmp_path):
    from app.services.maintenance import rotate_log
    path=tmp_path/'server.log';path.write_text('x'*80);path.chmod(0o644)
    rotate_log(path,keep=2,max_bytes=50)
    assert path.read_text()=='' and path.stat().st_mode & 0o777==0o600
    assert (tmp_path/'server.log.1').read_text()=='x'*80 and (tmp_path/'server.log.1').stat().st_mode & 0o777==0o600
    path.write_text('y'*80);rotate_log(path,keep=2,max_bytes=50)
    assert (tmp_path/'server.log.1').read_text()=='y'*80 and (tmp_path/'server.log.2').read_text()=='x'*80
    path.write_text('z'*80);rotate_log(path,keep=2,max_bytes=50)
    assert (tmp_path/'server.log.1').read_text()=='z'*80 and (tmp_path/'server.log.2').read_text()=='y'*80
    assert not (tmp_path/'server.log.3').exists()
    path.write_text('ok');path.chmod(0o644);rotate_log(path,keep=2,max_bytes=50)
    assert path.read_text()=='ok' and path.stat().st_mode & 0o777==0o600

def test_deleted_interest_remains_archived_on_future_events(site):
    c,a,t=site;i=interest(c);j=interest(c,'共享兴趣');today=now().astimezone(TZ).date();p=batch(c,[i])
    exclusive=p['events'][0];exclusive['title']='独占未来活动'
    shared=copy.deepcopy(exclusive);shared['source_key']=dict(shared['source_key'],id=str(uuid.uuid4()));shared['title']='共享未来活动';shared['interest_ids']=[i,j]
    current=copy.deepcopy(exclusive);current['source_key']=dict(current['source_key'],id=str(uuid.uuid4()));current['title']='今天的活动';current['interest_ids']=[i]
    current['timing']={'kind':'date','start_date':str(today),'end_date_exclusive':str(today+timedelta(days=1))}
    p['events']=[exclusive,shared,current];p['generated_at']=now().isoformat();assert upload(c,t,p).status_code==202
    s=c.get('/api/v1/status').json()
    assert c.request('DELETE','/api/v1/interests/'+i,json={'state_epoch':s['state_epoch'],'config_version':s['config_version']}).status_code==200
    visible={e['title']:e for e in c.get('/api/v1/events').json()['items']}
    assert '独占未来活动' not in visible and i in visible['共享未来活动']['interest_ids'] and j in visible['共享未来活动']['interest_ids']
    assert i in visible['今天的活动']['interest_ids'] and visible['今天的活动']['calendar_visible']
    listed={item['keyword']:item for item in c.get('/api/v1/interests').json()['items']}
    assert '共享兴趣' in listed and i not in {item['id'] for item in listed.values()}
    assert listed['共享兴趣']['exclusive_count']==1
    ctx=c.get('/api/v1/agent/context').json();hidden=next(e for e in ctx['events'] if e['title']=='独占未来活动')
    assert not hidden['calendar_visible'] and i in hidden['interest_ids']
    shared_row=next(e for e in ctx['events'] if e['title']=='共享未来活动');q=batch(c,[j]);proposal=copy.deepcopy(shared)
    proposal.update(event_id=shared_row['id'],base_version=shared_row['version'],interest_ids=[j]);q['events']=[proposal]
    assert upload(c,t,q).status_code in (200,202)
    again=c.get('/api/v1/events/'+shared_row['id']).json();assert i in again['interest_ids'] and j in again['interest_ids']

def test_empty_interest_ids_withdraw_future_event(site):
    c,a,t=site;i=interest(c);p=batch(c,[i]);r=upload(c,t,p);assert r.status_code==202
    eid=r.json()['event_mappings'][0]['event_id'];q=renewed(p);q['events'][0].update(event_id=eid,base_version=1,interest_ids=[])
    assert upload(c,t,q).status_code==202
    assert c.get('/api/v1/events').json()['total']==0
    publish(a.state.store);assert not Calendar.from_ical(c.get(feed(a)).content).walk('VEVENT')
    row=c.get('/api/v1/events/'+eid).json();assert not row['calendar_visible'] and row['interest_ids']==[] and row['withdrawal_reason']=='unfollowed'
    fresh=renewed(p);fresh['events'][0].update(event_id=None,base_version=0,interest_ids=[]);fresh['events'][0]['source_key']['id']=str(uuid.uuid4())
    rejected=upload(c,t,fresh);assert rejected.status_code==422 and rejected.json()['errors'][0]['code']=='SCHEMA_INVALID'

def test_restore_reports_differences_and_keeps_credentials(tmp_path):
    import sqlite3
    from app.services.maintenance import RestoreError, restore_database
    config=Config(tmp_path,'http://127.0.0.1:8787',True);tokens=initialize(config);app=create_app(config)
    with TestClient(app,base_url=config.base_url) as c:
        c.headers['Origin']=config.base_url;c.headers['X-CSRF-Token']=c.post('/api/v1/session',json={'token':tokens['admin']}).json()['csrf_token']
        i=interest(c);p=batch(c,[i]);p['events'][0]['title']='备份点活动';assert upload(c,tokens,p).status_code==202
        publish(app.state.store);backup=app.state.store.backup(force=True);epoch=c.get('/api/v1/status').json()['state_epoch']
        extra=batch(c,[i]);extra['events'][0]['title']='备份后的活动';assert upload(c,tokens,extra).status_code==202
        ctx=c.get('/api/v1/agent/context').json();original=next(e for e in ctx['events'] if e['title']=='备份点活动')
        withdrawn=renewed(p);withdrawn['events'][0].update(event_id=original['id'],base_version=original['version'],interest_ids=[],title='备份点活动')
        assert upload(c,tokens,withdrawn).status_code==202
        with app.state.store.tx() as db: current_sequence=db.execute('SELECT sequence FROM events WHERE id=?',(original['id'],)).fetchone()[0]
        with pytest.raises(RestoreError,match='停止'): restore_database(tmp_path,backup,apply=True)
    rotated=initialize(config,'admin');credentials=(tmp_path/'credentials.json').read_bytes()
    preview=restore_database(tmp_path,backup,apply=False)
    assert preview['applied'] is False and preview['baseline']=='present' and '尚未修改' in preview['text']
    assert any(item['title']=='备份后的活动' for item in preview['lost_uids'])
    assert any(item['policy']=='保留' and not item['current_visible'] and item['restored_visible'] for item in preview['withdrawals'])
    assert any(item['to_sequence']==item['known_max']+1 and item['to_sequence']>current_sequence for item in preview['sequence_adjustments'])
    assert preview['source_aliases']['lost']
    with sqlite3.connect(tmp_path/'calendar.sqlite3') as db: assert db.execute('SELECT state_epoch FROM settings').fetchone()[0]==epoch
    report=restore_database(tmp_path,backup,apply=True)
    assert report['applied'] is True and report['credentials_restored'] is False and report['state_epoch']!=epoch
    assert (tmp_path/'credentials.json').read_bytes()==credentials
    assert '高于已知最大值' in report['text'] and '旧令牌不会复活' in report['text']
    assert Path(report['preserved']).exists() and Path(report['report_path']).stat().st_mode & 0o777==0o600
    with sqlite3.connect(report['preserved']) as db: assert db.execute("SELECT count(*) FROM events WHERE payload_json LIKE '%备份后的活动%'").fetchone()[0]==1
    with sqlite3.connect(tmp_path/'calendar.sqlite3') as db:
        assert db.execute("SELECT count(*) FROM events WHERE payload_json LIKE '%备份后的活动%'").fetchone()[0]==0
        restored_sequence=db.execute('SELECT sequence FROM events WHERE uid=?',(original['uid'],)).fetchone()[0]
    assert restored_sequence>current_sequence
    with TestClient(create_app(config),base_url=config.base_url) as c:
        c.headers['Origin']=config.base_url
        assert c.post('/api/v1/session',json={'token':tokens['admin']}).status_code==401
        c.headers['X-CSRF-Token']=c.post('/api/v1/session',json={'token':rotated['admin']}).json()['csrf_token']
        assert c.get('/api/v1/status').json()['state_epoch']==report['state_epoch']
        titles={e['title'] for e in c.get('/api/v1/events').json()['items']}
        assert '备份点活动' in titles and '备份后的活动' not in titles
        batches=c.get('/api/v1/batches').json()['items']
        assert batches and all(item['receipt']['publication_status']=='before_restore' for item in batches)
        stale=batch(c,[i]);stale['state_epoch']=epoch
        assert upload(c,{'agent':tokens['agent']},stale).json()['errors'][0]['code']=='STATE_RESET'
        sequence=Calendar.from_ical(c.get(feed(app)).content).walk('VEVENT')[0]['SEQUENCE']
        assert int(sequence)>=restored_sequence+2
    (tmp_path/'calendar.sqlite3').unlink()
    missing=restore_database(tmp_path,backup,apply=True)
    assert missing['baseline']=='missing' and missing['summary']=='数据恢复到备份点。'
    assert any('不能承诺手机无重复或状态无倒退' in item for item in missing['limitations'])
    assert '缺少版本基线' in missing['text'] and '数据恢复到备份点' in missing['text']
    garbage=tmp_path/'garbage.sqlite3';garbage.write_bytes(b'not a database')
    with pytest.raises(RestoreError): restore_database(tmp_path,garbage,apply=True)

def test_publications_keep_current_and_previous_only(site):
    c,a,t=site;i=interest(c);p=batch(c,[i]);r=upload(c,t,p);assert r.status_code==202
    eid=r.json()['event_mappings'][0]['event_id'];version=1;actives=[];current=p
    for step in range(3):
        if step:
            current=renewed(current);current['events'][0].update(event_id=eid,base_version=version,title=f'第{step}次修订')
            assert upload(c,t,current).status_code==202;version+=1
        assert publish(a.state.store) is True
        with a.state.store.tx() as db: actives.append(db.execute('SELECT active_publication_id FROM settings').fetchone()[0])
    with a.state.store.tx() as db:
        ids={row['id'] for row in db.execute('SELECT id FROM publications')}
        assert ids=={actives[-1],actives[-2]} and actives[0] not in ids
        assert db.execute('SELECT count(*) FROM events').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM batches').fetchone()[0]==3

def test_vacuum_reclaims_pages_and_failure_keeps_snapshot(site,monkeypatch):
    c,a,t=site;i=interest(c);p=batch(c,[i]);assert upload(c,t,p).status_code==202;assert publish(a.state.store)
    with a.state.store.tx(True) as db:
        aid=db.execute("SELECT id FROM accounts WHERE kind='public'").fetchone()[0]
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?)',('big',-1,b'x'*200000,'b'*64,'2000-01-01T00:00:00Z',aid))
    with a.state.store.tx() as db: before=db.execute('PRAGMA page_count').fetchone()[0]
    q=renewed(p);q['events'][0].update(event_id=c.get('/api/v1/events').json()['items'][0]['id'],base_version=1,title='真空修订')
    assert upload(c,t,q).status_code==202;assert publish(a.state.store) is True
    with a.state.store.tx() as db:
        assert db.execute("SELECT count(*) FROM publications WHERE id='big'").fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM publications').fetchone()[0]==2
        assert db.execute('PRAGMA freelist_count').fetchone()[0]==0
        assert db.execute('PRAGMA page_count').fetchone()[0]<before
    def boom(self): raise OSError('vacuum failed')
    monkeypatch.setattr('app.storage.database.Store.vacuum',boom)
    again=renewed(q);again['events'][0].update(base_version=2,title='压缩失败仍发布')
    assert upload(c,t,again).status_code==202
    assert publish(a.state.store) is True
    assert c.get(feed(a)).status_code==200 and '压缩失败仍发布' in c.get(feed(a)).text

def test_reset_vacuums_only_after_success(site,monkeypatch):
    c,a,t=site;i=interest(c);p=batch(c,[i]);assert upload(c,t,p).status_code==202;publish(a.state.store)
    calls=[]
    monkeypatch.setattr('app.storage.database.Store.vacuum',lambda self: calls.append('vacuum'))
    s=c.get('/api/v1/status').json();body={'scope':'calendar','confirmation':'NO','state_epoch':s['state_epoch'],'config_version':s['config_version']}
    assert c.post('/api/v1/admin/reset',json=body).status_code==400 and calls==[]
    body['confirmation']='RESET';assert c.post('/api/v1/admin/reset',json=body).status_code==200 and calls==['vacuum']
    with a.state.store.tx() as db:
        assert db.execute('SELECT count(*) FROM events').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM batches').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM publications').fetchone()[0]==1

def test_restore_copies_and_reports_are_capped(tmp_path):
    from app.services.maintenance import restore_database
    from app.storage.database import Store
    config=Config(tmp_path,'http://127.0.0.1:8787',True);initialize(config);backup=Store(tmp_path).backup(force=True)
    for _ in range(8): restore_database(tmp_path,backup,apply=False)
    assert len(list((tmp_path/'restore-reports').glob('*.txt')))==7
    assert not (tmp_path/'restore-preserved').exists()
    restore_database(tmp_path,backup,apply=True);restore_database(tmp_path,backup,apply=True)
    assert len(list((tmp_path/'restore-preserved').glob('*.sqlite3')))==1
    assert len(list((tmp_path/'restore-reports').glob('*.txt')))==7

def test_open_log_rotates_without_stranding_appends(tmp_path,monkeypatch):
    from app.services.maintenance import rotate_open_log, rotate_service_log
    path=tmp_path/'server.log';path.write_bytes(b'x'*80);path.chmod(0o644)
    handle=open(path,'ab')
    try:
        rotate_open_log(path,keep=2,max_bytes=50)
        handle.write(b'NEW');handle.flush()
    finally: handle.close()
    assert path.read_bytes()==b'NEW' and path.stat().st_mode & 0o777==0o600
    assert (tmp_path/'server.log.1').read_bytes()==b'x'*80
    path.write_bytes(b'y'*80);rotate_open_log(path,keep=2,max_bytes=50)
    path.write_bytes(b'z'*80);rotate_open_log(path,keep=2,max_bytes=50)
    assert (tmp_path/'server.log.1').read_bytes()==b'z'*80 and (tmp_path/'server.log.2').read_bytes()==b'y'*80
    assert not (tmp_path/'server.log.3').exists()
    assert rotate_service_log(tmp_path/'missing') is None and not (tmp_path/'missing'/'server.log').exists()
    monkeypatch.setenv('CALENDAR_LOG_FILE',str(path));path.write_bytes(b'q'*(1024*1024+1))
    assert rotate_service_log(tmp_path/'ignored')==path
    assert path.read_bytes()==b'' and (tmp_path/'server.log.1').read_bytes()==b'q'*(1024*1024+1)

def test_calendar_secret_path_and_hidden_from_first_run(tmp_path,capsys):
    from app.main import calendar_feed
    assert calendar_feed('/public/calendar.ics') and calendar_feed('/c/abc/calendar.ics') and not calendar_feed('/calendar.ics') and not calendar_feed('/c/abc/other.ics')
    config=Config(tmp_path,'http://192.168.1.20:8787',False)
    with TestClient(create_app(config),base_url=config.base_url) as c:
        output=capsys.readouterr().out
        token=config.saved_token('calendar')
        first=(tmp_path/'first-run-credentials.txt').read_text()
        admin=dict(line.split('=',1) for line in first.splitlines())['ADMIN_TOKEN']
        assert token and token not in output and token not in first and 'CALENDAR_TOKEN' not in first
        c.headers['Origin']=config.base_url
        c.headers['X-CSRF-Token']=c.post('/api/v1/session',json={'token':admin}).json()['csrf_token']
        assert c.get('/calendar.ics').status_code==404
        assert c.get('/c/not-the-token/calendar.ics').status_code==404
        url=c.get('/api/v1/status').json()['subscription_url']
        assert url==config.subscription_url()=='http://192.168.1.20:8787/public/calendar.ics'
        assert c.get('/c/'+token+'/calendar.ics').status_code==404
        body=c.get('/public/calendar.ics')
        assert body.status_code==200 and body.headers['cache-control']=='no-cache' and 'BEGIN:VCALENDAR' in body.text
        assert c.get('/public/calendar.ics',headers={'If-None-Match':body.headers['etag']}).status_code==304
        again=initialize(config);assert again=={} and config.saved_token('calendar')==token

def test_dev_subscription_uses_lan_ip_without_replacing_explicit_base_url(tmp_path,monkeypatch):
    config=Config(tmp_path,'http://127.0.0.1:9090',True);initialize(config);token=config.saved_token('calendar')
    assert config.listen_host()=='0.0.0.0'
    serve=Path('src/app/__main__.py').read_text()
    assert 'host=config.listen_host()' in serve and 'port=urlparse(a.base_url).port or 8787' in serve
    monkeypatch.setattr('app.config.detect_local_ip',lambda:'192.168.9.9')
    lan='http://192.168.9.9:9090'
    with TestClient(create_app(config),base_url=config.base_url) as c:
        c.headers['Origin']=config.base_url
        c.headers['X-CSRF-Token']=c.post('/api/v1/session',json={'token':config.saved_token('admin')}).json()['csrf_token']
        assert c.get('/api/v1/status').json()['subscription_url']==lan+'/public/calendar.ics'
        assert c.get('/api/v1/status').json()['base_url']=='http://127.0.0.1:9090'
        phone={'Host':'192.168.9.9:9090'}
        body=c.get('/public/calendar.ics',headers=phone)
        assert body.status_code==200 and 'BEGIN:VCALENDAR' in body.text
        assert c.get('/c/'+token+'/calendar.ics',headers=phone).status_code==404
        assert c.get('/calendar.ics',headers=phone).status_code==404
        assert c.get('/c/not-the-token/calendar.ics',headers=phone).status_code==404
        assert c.get('/public/calendar.ics',headers={'Host':'203.0.113.50:9090'}).status_code==400
        assert c.post('/api/v1/session',json={'token':config.saved_token('admin')},headers={**phone,'Origin':lan}).status_code==200
        assert c.post('/api/v1/session',json={'token':config.saved_token('admin')},headers={**phone,'Origin':'https://evil.test'}).status_code==403
    monkeypatch.setattr('app.config.detect_local_ip',lambda:'127.0.0.1')
    assert config.subscription_url() is None and config.listen_host()=='0.0.0.0'
    assert config.allowed_hosts()==['127.0.0.1'] and config.accepted_origins()==['http://127.0.0.1:9090']
    with TestClient(create_app(config),base_url=config.base_url) as missed:
        assert missed.get('/public/calendar.ics',headers={'Host':'192.168.9.9:9090'}).status_code==400
        assert missed.get('/public/calendar.ics').status_code==200
    def fail(): raise AssertionError('explicit origin must not be detected')
    monkeypatch.setattr('app.config.detect_local_ip',fail)
    chosen=Config(tmp_path,'http://10.1.2.3:9000',False)
    assert chosen.subscription_url()=='http://10.1.2.3:9000/public/calendar.ics'
    assert chosen.listen_host()=='0.0.0.0' and chosen.allowed_hosts()==['10.1.2.3']
    dev_chosen=Config(tmp_path,'http://10.4.5.6:8787',True)
    assert dev_chosen.subscription_url()=='http://10.4.5.6:8787/public/calendar.ics'
    with pytest.raises(ValueError,match='Development mode is loopback only'): dev_chosen.validate()
    note=Path('src/app/static/app.js').read_text()
    assert '127.0.0.1 只能由这台电脑访问' in note and '订阅密钥' in note
    assert '连续三天' not in note and '监听 0.0.0.0 不是防火墙' not in note

def login_as(c, token):
    response=c.post('/api/v1/session',json={'token':token})
    assert response.status_code==200,response.text
    c.headers['X-CSRF-Token']=response.json()['csrf_token']
    return response

def relogin(c, token):
    c.delete('/api/v1/session')
    return login_as(c, token)

def test_anonymous_public_calendar(site):
    c,a,t=site
    assert c.delete('/api/v1/session').status_code==200
    home=c.get('/',follow_redirects=False)
    assert home.status_code==200 and 'session' not in home.headers.get('set-cookie','')
    status=c.get('/api/v1/status').json()
    assert status['role'] is None and status['subscription_url'] and status['subscription_url'].endswith('/public/calendar.ics') and '/c/' not in status['subscription_url']
    assert 'home-subscribe-url' not in home.text and 'href="/subscribe"' in home.text and '公共日历' not in home.text and '公开日历' not in home.text
    assert 'href="/admin"' not in home.text and 'href="/model"' not in home.text and 'href="/settings"' not in home.text
    subscribe=c.get('/subscribe',follow_redirects=False)
    assert subscribe.status_code==200 and 'subscribe-url' in subscribe.text and '日历地址' in subscribe.text and 'session' not in subscribe.headers.get('set-cookie','')
    assert '公开日历' not in subscribe.text and '重试日历发布' not in subscribe.text and '监听 0.0.0.0' not in subscribe.text
    assert '添加订阅日历' in subscribe.text and '通过网址添加' in subscribe.text and 'CalDAV' in subscribe.text
    interests_page=c.get('/interests',follow_redirects=False)
    assert interests_page.status_code==200 and '<h1>兴趣</h1>' in interests_page.text and 'interest-form' not in interests_page.text
    assert '公共日历' not in interests_page.text and '公开日历' not in interests_page.text and 'session' not in interests_page.headers.get('set-cookie','')
    assert c.get('/admin',follow_redirects=False).status_code==403
    assert c.get('/model',follow_redirects=False).status_code==403
    assert c.get('/settings',follow_redirects=False).status_code==404
    public=c.get('/public/calendar.ics')
    assert public.status_code==200 and 'BEGIN:VCALENDAR' in public.text
    assert c.get('/api/v1/events').status_code==200
    assert c.get('/api/v1/interests').status_code==200
    assert c.post('/api/v1/interests',json={'keyword':'匿名','conditions':'','state_epoch':status['state_epoch'],'config_version':status['config_version']}).status_code==401
    assert c.get('/api/v1/admin/accounts').status_code==401
    relogin(c,t['admin']);interest(c,'公开演示')
    assert 'href="/admin"' in c.get('/').text and 'href="/model"' in c.get('/').text
    assert c.get('/admin').status_code==200 and c.get('/model').status_code==200
    assert c.delete('/api/v1/session').status_code==200
    assert {item['keyword'] for item in c.get('/api/v1/interests').json()['items']}=={'公开演示'}
    again=c.get('/interests',follow_redirects=False)
    assert again.status_code==200 and '<h1>兴趣</h1>' in again.text and 'interest-form' not in again.text

def test_invite_login_is_isolated(site):
    from urllib.parse import urlparse
    c,a,t=site
    interest(c,'公开兴趣')
    created_response=c.post('/api/v1/admin/invites',json={'label':'甲'})
    assert created_response.status_code==201
    created=created_response.json()
    assert 'subscription_url' not in created and 'calendar_token' not in created
    relogin(c,created['token'])
    me=c.get('/api/v1/status').json()
    assert me['role']=='invitee' and '/c/' in me['subscription_url']
    personal=me['subscription_url'];secret=personal.rstrip('/').split('/c/')[1].split('/')[0]
    assert created['token']!=secret and secret not in c.get('/').text and 'home-subscribe-url' not in c.get('/').text
    assert 'href="/admin"' not in c.get('/').text and 'href="/model"' not in c.get('/').text
    assert c.get('/admin',follow_redirects=False).status_code==403 and c.get('/model',follow_redirects=False).status_code==403
    assert c.get('/api/v1/admin/accounts').status_code==403
    assert {item['keyword'] for item in c.get('/api/v1/interests').json()['items']}==set()
    interest(c,'受邀兴趣')
    mine=c.get('/interests').text
    assert '<h1>兴趣</h1>' in mine and '公共日历' not in mine and '公开日历' not in mine and '我的兴趣' not in mine
    subscribed=c.get('/subscribe').text
    assert '<h1>日历地址</h1>' in subscribed and '公开日历' not in subscribed and '重试日历发布' not in subscribed
    assert c.get('/api/v1/agent/context',headers={'Authorization':'Bearer '+t['agent']}).status_code==403
    assert c.post('/api/v1/admin/reset',json={'scope':'calendar','confirmation':'RESET','state_epoch':'x','config_version':1}).status_code==403
    with a.state.store.tx(True) as db:
        db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?)',('evt-invite','uid-invite',1,0,json.dumps({'title':'受邀者的秘密日程','location':'隐秘地点','status':'confirmed','interest_ids':[],'timing':{'kind':'date','start_date':'2026-10-10','end_date_exclusive':'2026-10-11'},'evidence':[{'url':'https://example.test/hidden'}]}),1,None,'2026-10-03T00:00:00+08:00','2026-10-03T00:00:00+08:00','2026-10-03T00:00:00+08:00',created['id']))
        db.execute('UPDATE settings SET data_revision=data_revision+1 WHERE account_id=?',(created['id'],))
    assert c.get('/api/v1/events/evt-invite').json()['title']=='受邀者的秘密日程'
    publish(a.state.store)
    assert '受邀者的秘密日程' in c.get(urlparse(personal).path).text
    assert c.delete('/api/v1/session').status_code==200
    assert {item['keyword'] for item in c.get('/api/v1/interests').json()['items']}=={'公开兴趣'}
    assert '受邀者的秘密日程' not in c.get('/public/calendar.ics').text
    relogin(c,t['admin'])
    assert {item['keyword'] for item in c.get('/api/v1/interests').json()['items']}=={'公开兴趣'}
    admin_interests=c.get('/interests').text
    assert '<h1>兴趣</h1>' in admin_interests and '公共日历' not in admin_interests and '公开日历' not in admin_interests
    admin_home=c.get('/').text
    assert '公共日历' not in admin_home and '公开日历' not in admin_home
    assert '<h1>日历地址</h1>' in c.get('/subscribe').text
    assert c.get('/api/v1/events/evt-invite').status_code==404
    assert all(item.get('title')!='受邀者的秘密日程' for item in c.get('/api/v1/events').json()['items'])
    listed=c.get('/api/v1/admin/accounts')
    assert listed.status_code==200 and secret not in listed.text and 'subscription_url' not in listed.text
    assert c.post('/api/v1/admin/session/account',json={'account_id':created['id']}).status_code==404
    assert c.get('/api/v1/status').json()['subscription_url'].endswith('/public/calendar.ics') and secret not in c.get('/api/v1/status').text
    assert c.delete('/api/v1/session').status_code==200
    home=c.get('/',follow_redirects=False)
    assert home.status_code==200 and 'session' not in home.headers.get('set-cookie','')
    assert {item['keyword'] for item in c.get('/api/v1/interests').json()['items']}=={'公开兴趣'}

def test_login_token_is_not_calendar_secret(site):
    c,a,t=site
    created=c.post('/api/v1/admin/invites',json={'label':'乙'})
    assert created.status_code==201,created.text
    created=created.json()
    assert 'subscription_url' not in created and 'calendar_token' not in created
    publish(a.state.store)
    with a.state.store.tx() as db:
        counts=list(db.execute('SELECT account_id,count(*) FROM publications GROUP BY account_id'))
    assert len(counts)==2 and all(row[1]<=2 for row in counts)
    relogin(c,created['token'])
    url=c.get('/api/v1/status').json()['subscription_url']
    secret=url.rstrip('/').split('/c/')[1].split('/')[0]
    assert created['token']!=secret and created['token'] not in url
    relogin(c,t['admin'])
    again=c.post('/api/v1/admin/invites/'+created['id']+'/reissue').json()
    assert again['token']!=created['token'] and 'subscription_url' not in again and secret not in c.get('/api/v1/admin/accounts').text
    assert c.delete('/api/v1/session').status_code==200
    assert c.post('/api/v1/session',json={'token':created['token']}).status_code==401
    relogin(c,again['token'])
    assert c.get('/api/v1/status').json()['subscription_url']==url

def test_revoked_invite_cannot_login_and_calendar_is_404(site):
    from urllib.parse import urlparse
    c,a,t=site
    created=c.post('/api/v1/admin/invites',json={'label':'丙'}).json()
    relogin(c,created['token'])
    path=urlparse(c.get('/api/v1/status').json()['subscription_url']).path
    assert path.startswith('/c/') and c.get(path).status_code==200 and 'BEGIN:VCALENDAR' in c.get(path).text
    saved=c.cookies.get('session')
    assert saved
    login_as(c,t['admin'])
    assert c.post('/api/v1/admin/invites/'+created['id']+'/revoke').status_code==200
    c.cookies.set('session',saved)
    assert c.get('/api/v1/status').json()['role'] is None
    assert c.post('/api/v1/session',json={'token':created['token']}).status_code==401
    assert c.get(path).status_code==404
    assert c.get(feed(a)).status_code==200

def test_admin_edits_public_interests(site):
    c,a,t=site
    assert c.delete('/api/v1/session').status_code==200
    assert c.post('/api/v1/interests',json={'keyword':'不行','conditions':'','state_epoch':'x','config_version':1}).status_code==401
    relogin(c,t['admin'])
    interest(c,'公开账户兴趣')
    created=c.post('/api/v1/admin/invites',json={'label':'丁'}).json()
    relogin(c,created['token'])
    assert {item['keyword'] for item in c.get('/api/v1/interests').json()['items']}==set()
    interest(c,'只属于受邀者')
    relogin(c,t['admin'])
    listed={item['keyword'] for item in c.get('/api/v1/interests').json()['items']}
    assert listed=={'公开账户兴趣'}
    assert c.delete('/api/v1/session').status_code==200
    assert {item['keyword'] for item in c.get('/api/v1/interests').json()['items']}=={'公开账户兴趣'}

def test_admin_views_token_and_resets_selected_account(site):
    c,a,t=site
    interest(c,'公开保留')
    created=c.post('/api/v1/admin/invites',json={'label':'戊'}).json()
    assert 'subscription_url' not in created
    relogin(c,created['token'])
    interest(c,'受邀将被清空')
    relogin(c,t['admin'])
    page=c.get('/admin').text
    assert '管理账号' in page and '创建账户' in page and '邀请别人使用自己的日历' not in page and '对方登录后' not in page
    assert '定期搜索' in c.get('/model').text
    listed=c.get('/api/v1/admin/accounts')
    assert listed.status_code==200 and created['token'] not in listed.text and t['admin'] not in listed.text
    accounts=listed.json()['items']
    assert accounts[0]['kind']=='public' and all('login_token' not in item and 'subscription_url' not in item for item in accounts)
    invite=next(item for item in accounts if item['id']==created['id'])
    viewed=c.get('/api/v1/admin/accounts/'+created['id']+'/token')
    assert viewed.status_code==200 and viewed.json()['token']==created['token']
    assert '受邀将被清空' not in viewed.text and 'subscription_url' not in viewed.text
    own=c.get('/api/v1/admin/accounts/'+accounts[0]['id']+'/token')
    assert own.status_code==200 and own.json()['token']==t['admin']
    assert c.get('/api/v1/admin/accounts/missing/token').status_code==404
    body={'scope':'all','confirmation':'RESET','account_id':invite['id'],'state_epoch':invite['state_epoch'],'config_version':invite['config_version']}
    reset=c.post('/api/v1/admin/reset',json=body)
    assert reset.status_code==200 and reset.json()['account_id']==invite['id'] and 'interests' not in reset.json()
    assert c.post('/api/v1/admin/reset',json={**body,'account_id':'missing'}).status_code==404
    relogin(c,created['token'])
    assert c.get('/api/v1/interests').json()['items']==[]
    relogin(c,t['admin'])
    assert {item['keyword'] for item in c.get('/api/v1/interests').json()['items']}=={'公开保留'}
    assert '受邀将被清空' not in c.get('/api/v1/interests').text

def test_v1_database_opens_as_public_account(tmp_path):
    import sqlite3
    from app.storage.database import Store
    db_path=tmp_path/'calendar.sqlite3'
    with sqlite3.connect(db_path) as db:
        db.executescript(Path('src/app/storage/001_initial.sql').read_text())
        db.execute("INSERT INTO settings(id,state_epoch) VALUES(1,'epoch-1')")
        db.execute("INSERT INTO interests VALUES('i1','网球','','k','t',NULL)")
        db.execute('PRAGMA user_version=1')
    Store(tmp_path,'calendar-secret-value')
    with sqlite3.connect(db_path) as db:
        db.row_factory=sqlite3.Row
        assert db.execute('PRAGMA user_version').fetchone()[0]==3
        assert 'login_token' in [row[1] for row in db.execute('PRAGMA table_info(accounts)')]
        account=db.execute("SELECT * FROM accounts WHERE kind='public'").fetchone()
        assert account['calendar_token']=='calendar-secret-value' and account['login_hash'] is None
        assert db.execute('SELECT account_id FROM interests').fetchone()[0]==account['id']
        assert db.execute('SELECT state_epoch FROM settings').fetchone()[0]=='epoch-1'
    Store(tmp_path,'rotated-calendar-secret')
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT calendar_token FROM accounts WHERE kind='public'").fetchone()[0]=='rotated-calendar-secret'
        assert db.execute('SELECT count(*) FROM accounts').fetchone()[0]==1
