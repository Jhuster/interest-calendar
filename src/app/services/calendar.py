import hashlib, json, logging, uuid
from datetime import date, timezone, timedelta
from icalendar import Calendar, Event
from app.models.protocol import dt, stamp

def render(rows):
    cal=Calendar()
    cal.add('prodid','-//Interest Calendar//Local MVP//ZH')
    cal.add('version','2.0')
    cal.add('x-interest-display-version','3')
    cal.add('x-wr-calname','兴趣日历')
    for row in sorted(rows,key=lambda x:x['uid']):
        if not row['calendar_visible']: continue
        p=json.loads(row['payload_json']); t=p['timing']; e=Event()
        duration=(date.fromisoformat(t['end_date_exclusive'])-date.fromisoformat(t['start_date'])).days if t['kind']=='date' else 1
        summary=f"{p['title']}（共{duration}天）" if duration>1 else p['title']
        for key,value in [('uid',row['uid']),('summary',summary),('location',p['location']),('sequence',row['sequence']+2),('status',p['status'].upper())]: e.add(key,value)
        e.add('dtstamp',dt(row['ics_modified_at']))
        e.add('last-modified',dt(row['ics_modified_at']))
        note=''
        if t['kind']=='date':
            e.add('dtstart',date.fromisoformat(t['start_date']))
            e.add('dtend',date.fromisoformat(t['start_date'])+timedelta(days=1))
            note='具体时间待定'
            if duration>1:
                last=date.fromisoformat(t['end_date_exclusive'])-timedelta(days=1)
                note=f"活动日期：{t['start_date']} 至 {last}，共{duration}天；日历仅在首日展示。具体时间待定"
        else:
            e.add('dtstart',dt(t['start']).astimezone(timezone.utc))
            if t['end']: e.add('dtend',dt(t['end']).astimezone(timezone.utc))
            else: note='结束时间待定'
        url=p['evidence'][0]['url']
        e.add('url',url); e.add('description',note+'\n来源：'+url)
        cal.add_component(e)
    blob=cal.to_ical()
    Calendar.from_ical(blob)
    return blob

def _publish_account(store, account_id):
    try:
        with store.tx() as db:
            s=db.execute('SELECT * FROM settings WHERE account_id=?',(account_id,)).fetchone()
            old=db.execute('SELECT * FROM publications WHERE id=?',(s['active_publication_id'],)).fetchone() if s['active_publication_id'] else None
            if old and old['data_revision']==s['data_revision'] and b'X-INTEREST-DISPLAY-VERSION:3' in old['ics_blob']: return False
            revision=s['data_revision']
            rows=[dict(r) for r in db.execute('SELECT * FROM events WHERE account_id=?',(account_id,))]
        blob=render(rows)
        with store.tx(True) as db:
            if db.execute('SELECT data_revision FROM settings WHERE account_id=?',(account_id,)).fetchone()[0]!=revision: return None
            previous=db.execute('SELECT active_publication_id FROM settings WHERE account_id=?',(account_id,)).fetchone()[0]
            pid=str(uuid.uuid4())
            db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?)',(pid,revision,blob,hashlib.sha256(blob).hexdigest(),stamp(),account_id))
            db.execute('UPDATE settings SET active_publication_id=?,publish_error=NULL WHERE account_id=?',(pid,account_id))
            keep=[pid]
            if previous: keep.append(previous)
            deleted=db.execute('DELETE FROM publications WHERE account_id=? AND id NOT IN ('+','.join('?'*len(keep))+')',[account_id,*keep]).rowcount
            return deleted>0
    except Exception:
        try:
            with store.tx(True) as db:
                db.execute("UPDATE settings SET publish_error='发布失败，保留最后成功日历；稍后自动重试' WHERE account_id=?",(account_id,))
        except Exception:
            logging.getLogger('uvicorn.error').exception('记录发布失败状态失败')
        raise

def publish(store):
    with store.tx() as db:
        accounts=[row[0] for row in db.execute('SELECT account_id FROM settings ORDER BY account_id')]
    pruned=False
    for account_id in accounts:
        result=_publish_account(store, account_id)
        if result is None: return False
        pruned=pruned or result
    if pruned:
        try: store.vacuum()
        except Exception:
            logging.getLogger('uvicorn.error').exception('压缩数据库失败')
    return True
