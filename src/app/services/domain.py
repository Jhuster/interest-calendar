import hashlib, json, sqlite3, unicodedata, uuid
from datetime import timedelta
from app.models.protocol import *

def uid(): return str(uuid.uuid4())
def norm(s): return unicodedata.normalize('NFKC',s.strip())
def settings(db): return dict(db.execute('SELECT * FROM settings').fetchone())
def epoch(db,value):
    if value!=settings(db)['state_epoch']: raise Problem('STATE_RESET','数据状态已变化，请刷新上下文',409,action='REFETCH_CONFIG')
def precondition(db,b):
    epoch(db,b.get('state_epoch'))
    if b.get('config_version')!=settings(db)['config_version']: raise Problem('CONFIG_STALE','兴趣配置已更新，请刷新',409,expected={'config_version':settings(db)['config_version']},action='REFETCH_CONFIG')
def active(db,ids):
    for i in ids:
        if not db.execute('SELECT 1 FROM interests WHERE id=? AND deleted_at IS NULL',(i,)).fetchone(): raise Problem('INTEREST_INACTIVE','兴趣已删除或不存在',409,action='REFETCH_CONFIG')
def event(db,eid):
    row=db.execute('SELECT * FROM events WHERE id=?',(eid,)).fetchone()
    if not row: raise Problem('NOT_FOUND','事件不存在',404)
    return dict(row)
def exposed(row):
    r=dict(row); r.update(json.loads(r.pop('payload_json'))); r['event_id']=r['id']; r['base_version']=r['version']
    r['start_date'],r['end_date_exclusive']=map(str,days(r['timing']))
    return r

def projected(p,visible):
    return canonical([p['title'],p['location'],p['status'],p['timing'],p['evidence'][0]['url'],bool(visible)])

def save_event(db,p,old=None,reason='batch',batch_id=None,force_visible=None):
    p=json.loads(canonical(p)); eid=old['id'] if old else uid(); current=json.loads(old['payload_json']) if old else None
    # Keep archived associations as history, even when Agent submits only active interests.
    if current:
        archived=[i for i in current['interest_ids'] if db.execute('SELECT 1 FROM interests WHERE id=? AND deleted_at IS NOT NULL',(i,)).fetchone()]
        p['interest_ids']=sorted(set(p['interest_ids']+archived))
    else: p['interest_ids']=sorted(p['interest_ids'])
    p['event_id']=eid; p['base_version']=0
    has_active=any(db.execute('SELECT 1 FROM interests WHERE id=? AND deleted_at IS NULL',(i,)).fetchone() for i in p['interest_ids'])
    visible=bool(has_active) or not future(p)
    if old and not old['calendar_visible'] and not any(db.execute('SELECT 1 FROM interests WHERE id=? AND deleted_at IS NULL',(i,)).fetchone() for i in p['interest_ids']): visible=False
    if force_visible is not None: visible=force_visible
    if old and canonical(current)==canonical(p) and bool(old['calendar_visible'])==visible: return old,False,False
    changed=not old or projected(current,old['calendar_visible'])!=projected(p,visible)
    version=old['version']+1 if old else 1; seq=old['sequence']+int(changed) if old else 0
    modified=stamp() if changed else old['ics_modified_at']; ts=stamp()
    withdrawal=None if visible else ('postponed' if reason=='withdraw' else 'unfollowed')
    db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,sequence=excluded.sequence,payload_json=excluded.payload_json,calendar_visible=excluded.calendar_visible,withdrawal_reason=excluded.withdrawal_reason,ics_modified_at=excluded.ics_modified_at,updated_at=excluded.updated_at',(eid,old['uid'] if old else eid+'@interest-calendar.local',version,seq,canonical(p),visible,withdrawal,modified,old['created_at'] if old else ts,ts))
    key=p['source_key']
    db.execute('INSERT OR IGNORE INTO event_sources VALUES(?,?,?,?)',(key['namespace'],key['id'],eid,p['evidence'][0]['url']))
    db.execute('UPDATE event_interests SET unlinked_at=? WHERE event_id=?',(ts,eid))
    for i in p['interest_ids']:
        db.execute('INSERT INTO event_interests VALUES(?,?,?,NULL) ON CONFLICT(event_id,interest_id) DO UPDATE SET unlinked_at=NULL',(eid,i,ts))
    db.execute('INSERT INTO event_versions VALUES(?,?,?,?,?)',(eid,version,canonical({'event':p,'visible':visible,'sequence':seq}),reason,batch_id))
    return event(db,eid),changed,True

def validate_identity(db,p):
    active(db,p['interest_ids']); key=p['source_key']
    mapped=db.execute('SELECT event_id FROM event_sources WHERE source_namespace=? AND source_id=?',(key['namespace'],key['id'])).fetchone()
    if not p['event_id']:
        if mapped:
            old=event(db,mapped[0]); raise Problem('EVENT_EXISTS','来源已有事件，请读取后更新',409,expected={'event_id':old['id'],'version':old['version']},action='REFETCH_EVENT')
        return None
    old=event(db,p['event_id'])
    if old['version']!=p['base_version']: raise Problem('EVENT_VERSION_STALE','事件版本已变化',409,expected={'version':old['version']},action='REFETCH_EVENT')
    if not mapped or mapped[0]!=old['id']: raise Problem('EVENT_EXISTS','更新不能改写稳定来源身份',409)
    previous=json.loads(old['payload_json'])
    if p['status']=='cancelled' and previous['timing']!=p['timing']:
        raise Problem('TIME_INVALID','取消时必须保留最近确认的时间')
    if previous['status']!=p['status'] and previous['evidence']==p['evidence']: raise Problem('EVIDENCE_REQUIRED','取消或恢复需要新的明确证据')
    return old

def candidate_save(db,c):
    active(db,c['interest_ids'])
    if c['related_event_id'] and event(db,c['related_event_id'])['version']!=c['base_version']: raise Problem('EVENT_VERSION_STALE','候选引用的事件版本已变化',409)
    old=db.execute('SELECT * FROM candidates WHERE client_candidate_id=?',(c['client_candidate_id'],)).fetchone()
    if old and old['state']!='pending': raise Problem('CANDIDATE_RESOLVED','候选已处理，需要人工重新打开',409)
    if (old['version'] if old else 0)!=c['base_candidate_version']: raise Problem('CANDIDATE_VERSION_STALE','候选版本已变化',409)
    p=dict(c);p['base_candidate_version']=0
    if old and old['payload_json']==canonical(p): return dict(old)
    cid=old['id'] if old else uid(); version=old['version']+1 if old else 1
    db.execute('INSERT INTO candidates(id,client_candidate_id,version,payload_json,state) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,payload_json=excluded.payload_json',(cid,c['client_candidate_id'],version,canonical(p),'pending'))
    return dict(db.execute('SELECT * FROM candidates WHERE id=?',(cid,)).fetchone())

def receipt(db,row):
    result=json.loads(row['receipt_json']);s=settings(db)
    pub=db.execute('SELECT data_revision FROM publications WHERE id=?',(s['active_publication_id'],)).fetchone()
    published=pub[0] if pub else -1; target=row['target_revision']
    result['published_revision']=published
    result['publication_status']='not_required' if target is None else ('published' if published>=target else 'pending')
    if row['state_epoch']!=s['state_epoch']: result['publication_status']='before_restore'
    return result

def import_batch(store,b):
    bid=b.get('batch_id')
    try: uuid.UUID(bid)
    except (ValueError,TypeError,AttributeError): raise Problem('SCHEMA_INVALID','batch_id 必须是 UUID',path='/batch_id')
    digest=hashlib.sha256(canonical(b).encode()).hexdigest()
    def prior(db):
        epoch(db,b.get('state_epoch'))
        r=db.execute('SELECT * FROM batches WHERE batch_id=?',(bid,)).fetchone()
        if r and r['payload_hash']!=digest: raise Problem('BATCH_ID_CONFLICT','相同批次 ID 的内容不同',409)
        return r
    try:
        with store.tx(True) as db:
            r=prior(db)
            if r: return receipt(db,r),r['status'] if r['status']>=400 else 200
            batch_check(b); precondition(db,b)
            counts=dict(created=0,updated=0,unchanged=0,candidates=0); maps=[]; cmaps=[]; changed=False; seen=set(); candidate_keys=set()
            for p in b['events']:
                key=canonical(p['source_key']); old=validate_identity(db,p); identity=old['id'] if old else key
                if identity in seen: raise Problem('DUPLICATE_OPERATION','同一事件在批次中只能操作一次')
                seen.add(identity)
                if not old:
                    auto='auto:'+hashlib.sha256(key.encode()).hexdigest()
                    existing=db.execute('SELECT * FROM candidates WHERE client_candidate_id=?',(auto,)).fetchone()
                    if existing:
                        previous=json.loads(existing['payload_json'])
                        if existing['state']!='pending': raise Problem('CANDIDATE_RESOLVED','该来源的候选已处理',409)
                        if canonical(previous['proposed_event'])!=canonical(p): raise Problem('CANDIDATE_VERSION_STALE','请通过已有候选键和版本更新提案',409)
                        cmaps.append({'id':existing['id'],'version':existing['version']}); counts['candidates']+=1;continue
                    duplicate=next((r for r in db.execute('SELECT * FROM events') if norm(json.loads(r['payload_json'])['title'])==norm(p['title']) and days(json.loads(r['payload_json'])['timing'])[0]==days(p['timing'])[0] and p['location'].strip() and norm(json.loads(r['payload_json'])['location'])==norm(p['location'])),None)
                    if duplicate:
                        c=dict(client_candidate_id=auto,base_candidate_version=0,related_event_id=duplicate['id'],base_version=duplicate['version'],title=p['title'],interest_ids=p['interest_ids'],reason='possible_duplicate',proposed_event=p,evidence=p['evidence'],notes='标题、开始日期和地点与已知事件相同，请核验。')
                        row=candidate_save(db,c);cmaps.append({'id':row['id'],'version':row['version']}); counts['candidates']+=1;continue
                row,ics_change,actual=save_event(db,p,old,batch_id=bid)
                changed|=ics_change; counts['created' if not old else ('updated' if actual else 'unchanged')]+=1
                maps.append({'source_key':p['source_key'],'event_id':row['id'],'version':row['version']})
            for c in b['candidates']:
                if c['client_candidate_id'] in candidate_keys: raise Problem('DUPLICATE_OPERATION','候选键重复')
                candidate_keys.add(c['client_candidate_id']); row=candidate_save(db,c);counts['candidates']+=1
                cmaps.append({'client_candidate_id':c['client_candidate_id'],'id':row['id'],'version':row['version']})
            if changed: db.execute('UPDATE settings SET data_revision=data_revision+1')
            target=settings(db)['data_revision'] if changed else None
            result=dict(batch_id=bid,state_epoch=b['state_epoch'],import_status='imported',target_revision=target,counts=counts,event_mappings=maps,candidate_mappings=cmaps,errors=[])
            status=202 if changed else 200
            db.execute('INSERT INTO batches VALUES(?,?,?,?,?,?,?,?)',(bid,b['state_epoch'],digest,b['config_version'],status,target,canonical(result),stamp()))
            return receipt(db,db.execute('SELECT * FROM batches WHERE batch_id=?',(bid,)).fetchone()),status
    except Problem as exc:
        if exc.error['code'] in ('STATE_RESET','BATCH_ID_CONFLICT'): raise
        with store.tx(True) as db:
            r=prior(db)
            if r: return receipt(db,r),r['status'] if r['status']>=400 else 200
            result=dict(batch_id=bid,state_epoch=b['state_epoch'],import_status='rejected',target_revision=None,errors=[exc.error])
            db.execute('INSERT INTO batches VALUES(?,?,?,?,?,?,?,?)',(bid,b['state_epoch'],digest,b.get('config_version') if type(b.get('config_version')) is int else None,exc.status,None,canonical(result),stamp()))
            return receipt(db,db.execute('SELECT * FROM batches WHERE batch_id=?',(bid,)).fetchone()),exc.status

def context(db):
    s=settings(db); today=now().astimezone(TZ).date()
    return dict(server_time=stamp(),state_epoch=s['state_epoch'],config_version=s['config_version'],window={'start_date':str(today),'end_date':str(today+timedelta(days=7))},interests=[dict(r) for r in db.execute('SELECT * FROM interests WHERE deleted_at IS NULL ORDER BY created_at,id')],events=[exposed(r) for r in db.execute('SELECT * FROM events ORDER BY id')],source_mappings=[dict(r) for r in db.execute('SELECT * FROM event_sources ORDER BY source_namespace,source_id')],candidates=[dict(r)|{'payload':json.loads(r['payload_json'])} for r in db.execute('SELECT * FROM candidates ORDER BY id')])

def delete_interest(db,i,b):
    precondition(db,b);active(db,[i]);changed=False
    for row in list(db.execute('SELECT * FROM events')):
        p=json.loads(row['payload_json'])
        if i not in p['interest_ids'] or not future(p): continue
        p['interest_ids'].remove(i)
        _,ch,_=save_event(db,p,dict(row),reason='unfollow',force_visible=bool(p['interest_ids']));changed|=ch
    db.execute('UPDATE interests SET deleted_at=? WHERE id=?',(stamp(),i))
    db.execute('UPDATE settings SET config_version=config_version+1,data_revision=data_revision+?',(int(changed),))

def reset_data(db, scope):
    """Clear calendar state while keeping the database and credentials intact."""
    if scope not in ('calendar', 'all'):
        raise Problem('SCHEMA_INVALID', 'reset scope 必须是 calendar 或 all')
    for table in ('event_interests', 'event_sources', 'event_versions', 'events', 'candidates', 'batches', 'publications'):
        db.execute(f'DELETE FROM {table}')
    if scope == 'all':
        db.execute('DELETE FROM interests')
    db.execute("UPDATE settings SET state_epoch=?,config_version=config_version+1,data_revision=data_revision+1,active_publication_id=NULL,publish_error=NULL", (str(uuid.uuid4()),))

    # Publish the empty calendar in the same transaction as deletion.
    from app.services.calendar import render
    blob=render([]);pid=uid();revision=settings(db)['data_revision']
    db.execute('INSERT INTO publications VALUES(?,?,?,?,?)',(pid,revision,blob,hashlib.sha256(blob).hexdigest(),stamp()))
    db.execute('UPDATE settings SET active_publication_id=?',(pid,))

def resolve(db,cid,b):
    epoch(db,b.get('state_epoch')); row=db.execute('SELECT * FROM candidates WHERE id=?',(cid,)).fetchone()
    if not row: raise Problem('NOT_FOUND','候选不存在',404)
    if b.get('version')!=row['version']: raise Problem('CANDIDATE_VERSION_STALE','候选已变化，请刷新',409)
    action=b.get('action'); c=json.loads(row['payload_json']);target=None;changed=False
    if action=='reopen':
        if row['state']=='pending': raise Problem('CANDIDATE_EXISTS','候选已处于待核验状态',409)
        db.execute("UPDATE candidates SET state='pending',version=version+1 WHERE id=?",(cid,))
    else:
        if row['state']!='pending': raise Problem('CANDIDATE_RESOLVED','候选已处理',409)
        if action in ('approve','merge'):
            p=b.get('proposed_event');schema_validate(p,'event');event_check(p,now());active(db,p['interest_ids'])
            if action=='merge':
                old=event(db,b.get('target_event_id'));target=old['id']
                if b.get('target_version')!=old['version']: raise Problem('EVENT_VERSION_STALE','目标事件已变化',409)
                key=p['source_key']; existing=db.execute('SELECT event_id FROM event_sources WHERE source_namespace=? AND source_id=?',(key['namespace'],key['id'])).fetchone()
                if existing and existing[0]!=target: raise Problem('EVENT_EXISTS','来源已属于其他事件',409)
                p['event_id']=target;p['base_version']=old['version']
                db.execute('INSERT OR IGNORE INTO event_sources VALUES(?,?,?,?)',(key['namespace'],key['id'],target,p['evidence'][0]['url']))
            else: old=validate_identity(db,p)
            r,changed,_=save_event(db,p,old,reason=action);target=r['id']
        elif action=='withdraw':
            if c['reason']!='postponed' or not c['evidence']: raise Problem('EVIDENCE_REQUIRED','仅有明确延期证据的候选可以撤下')
            old=event(db,c['related_event_id']);target=old['id']
            if b.get('target_version')!=old['version'] or c['base_version']!=old['version']: raise Problem('EVENT_VERSION_STALE','目标版本已变化，请重新核验候选',409)
            _,changed,_=save_event(db,json.loads(old['payload_json']),old,reason='withdraw',force_visible=False)
        elif action!='reject': raise Problem('SCHEMA_INVALID','未知候选操作')
        db.execute("UPDATE candidates SET state='resolved',version=version+1,resolved_at=?,resolution=?,resolved_event_id=? WHERE id=?",(stamp(),action,target,cid))
    db.execute('INSERT INTO audit_log(operation,object_id,created_at,result) VALUES(?,?,?,?)',(action,cid,stamp(),'ok'))
    if changed: db.execute('UPDATE settings SET data_revision=data_revision+1')
