import fcntl, json, logging, os, shutil, signal, sqlite3, tempfile, time, uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from app.models.protocol import canonical, stamp

class RestoreError(RuntimeError):
    pass

def startup_token_message(token, credential_path, interactive):
    if token and interactive:
        return '管理令牌：'+token
    if token:
        return '管理令牌已保存在仅当前用户可读的凭据文件中：'+str(credential_path)
    return '管理令牌原文不可恢复，请使用已保存令牌或在本机轮换管理令牌。'

def record_backup(store, state):
    try:
        store.backup()
    except Exception as exc:
        message=str(exc)
        if getattr(state,'backup_error',None)!=message:
            logging.getLogger('uvicorn.error').exception('备份失败')
        state.backup_error=message
    else:
        state.backup_error=None

def rotate_open_log(path, keep=7, max_bytes=1024*1024):
    """Copy and truncate a log that another descriptor may still have open for append."""
    path=Path(path)
    if not path.exists() or path.stat().st_size<=max_bytes:
        if path.exists(): path.chmod(0o600)
        return path
    oldest=path.with_name(path.name+f'.{keep}')
    if oldest.exists(): oldest.unlink()
    for index in range(keep-1,0,-1):
        src=path.with_name(path.name+f'.{index}')
        if src.exists(): src.replace(path.with_name(path.name+f'.{index+1}'))
    archived=path.with_name(path.name+'.1')
    shutil.copyfile(path, archived)
    archived.chmod(0o600)
    with open(path,'r+b') as handle: handle.truncate(0)
    path.chmod(0o600)
    for index in range(1,keep+1):
        old=path.with_name(path.name+f'.{index}')
        if old.exists(): old.chmod(0o600)
    return path

def rotate_service_log(directory):
    configured=os.environ.get('CALENDAR_LOG_FILE')
    path=Path(configured) if configured else Path(directory)/'server.log'
    if not path.exists(): return None
    try: return rotate_open_log(path)
    except OSError:
        logging.getLogger('uvicorn.error').exception('日志轮转失败')
        return None

def rotate_log(path, keep=7, max_bytes=1024*1024):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists() and path.stat().st_size>max_bytes:
        oldest=path.with_name(path.name+f'.{keep}')
        if oldest.exists(): oldest.unlink()
        for index in range(keep-1,0,-1):
            src=path.with_name(path.name+f'.{index}')
            if src.exists(): src.replace(path.with_name(path.name+f'.{index+1}'))
        path.replace(path.with_name(path.name+'.1'))
    if not path.exists(): path.touch()
    path.chmod(0o600)
    for index in range(1,keep+1):
        old=path.with_name(path.name+f'.{index}')
        if old.exists(): old.chmod(0o600)
    return path

def service_running(directory):
    path=Path(directory)/'service.lock'
    if not path.exists(): return False
    handle=open(path,'a')
    try:
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(handle,fcntl.LOCK_UN)
        return False
    finally:
        handle.close()

@contextmanager
def _held_lock(directory):
    path=Path(directory)/'service.lock'
    path.parent.mkdir(parents=True,exist_ok=True)
    handle=open(path,'a')
    try:
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RestoreError('请先停止服务后再恢复数据')
    try: yield
    finally:
        fcntl.flock(handle,fcntl.LOCK_UN)
        handle.close()

def _connect(path, readonly=False):
    if readonly:
        db=sqlite3.connect('file:'+quote(str(Path(path).resolve()))+'?mode=ro',uri=True)
    else:
        db=sqlite3.connect(path)
    db.row_factory=sqlite3.Row
    return db

def copy_database(source, dest):
    dest=Path(dest)
    dest.parent.mkdir(parents=True,exist_ok=True)
    dest.parent.chmod(0o700)
    if dest.exists(): dest.unlink()
    src=_connect(source,readonly=True)
    try:
        dst=sqlite3.connect(dest)
        try: src.backup(dst)
        finally: dst.close()
    finally: src.close()
    dest.chmod(0o600)
    return dest

def _load(path):
    path=Path(path)
    if not path.exists(): return None
    try: db=_connect(path,readonly=True)
    except sqlite3.Error: return None
    try:
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok': return None
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone() is None: return None
        return dict(
            events=[dict(r) for r in db.execute('SELECT * FROM events')],
            sources=[dict(r) for r in db.execute('SELECT * FROM event_sources')],
            interests=[dict(r) for r in db.execute('SELECT * FROM interests')],
            settings=dict(db.execute('SELECT * FROM settings').fetchone()),
        )
    except sqlite3.Error:
        return None
    finally: db.close()

def _title(row):
    try: return json.loads(row['payload_json']).get('title') or row['uid']
    except (json.JSONDecodeError,KeyError,TypeError): return row['uid']

def _signature(row):
    payload=json.loads(row['payload_json'])
    url=payload['evidence'][0]['url'] if payload.get('evidence') else ''
    return (payload.get('title'),payload.get('location'),payload.get('status'),canonical(payload.get('timing')),url,bool(row['calendar_visible']))

def _validate_backup(path):
    from icalendar import Calendar
    db=_connect(path)
    try:
        check=db.execute('PRAGMA integrity_check').fetchone()[0]
        if check!='ok': raise RestoreError('备份完整性检查失败')
        if db.execute('PRAGMA user_version').fetchone()[0] not in (1,2,3,4): raise RestoreError('备份数据库版本不受支持')
        count=0
        for row in db.execute('SELECT id,ics_blob FROM publications'):
            try: parsed=Calendar.from_ical(row['ics_blob'])
            except Exception as exc: raise RestoreError('备份中的日历快照无法解析：'+row['id']) from exc
            if parsed is None: raise RestoreError('备份中的日历快照无法解析：'+row['id'])
            count+=1
        return count
    finally: db.close()

def _source_key(row):
    return row['source_namespace'],row['source_id']

def _build_report(baseline, restored, snapshots):
    restored_events={row['uid']:row for row in restored['events']}
    current_events={row['uid']:row for row in baseline['events']} if baseline else {}
    lost=[{'uid':row['uid'],'title':_title(row)} for uid,row in sorted(current_events.items()) if uid not in restored_events]
    current_sources={_source_key(row):row for row in (baseline['sources'] if baseline else [])}
    restored_sources={_source_key(row):row for row in restored['sources']}
    aliases=dict(
        lost=[{'namespace':k[0],'id':k[1],'event_id':v['event_id']} for k,v in sorted(current_sources.items()) if k not in restored_sources],
        returning=[{'namespace':k[0],'id':k[1],'event_id':v['event_id']} for k,v in sorted(restored_sources.items()) if k not in current_sources],
        conflicts=[{'namespace':k[0],'id':k[1],'current_event_id':current_sources[k]['event_id'],'backup_event_id':v['event_id']} for k,v in sorted(restored_sources.items()) if k in current_sources and current_sources[k]['event_id']!=v['event_id']],
    )
    current_interests={row['id']:row for row in (baseline['interests'] if baseline else [])}
    restored_interests={row['id']:row for row in restored['interests']}
    lost_interests=[]
    revoked=[]
    for row in current_interests.values():
        other=restored_interests.get(row['id'])
        if other is None:
            effect='备份后新增，恢复后不再包含'
            if row['deleted_at']: effect='备份后新增且已有删除记录，恢复后不再包含'
            lost_interests.append({'id':row['id'],'keyword':row['keyword'],'effect':effect})
        elif row['deleted_at'] and not other['deleted_at']:
            revoked.append({'id':row['id'],'keyword':row['keyword'],'effect':'当前已删除，备份中仍有效；恢复后会重新生效'})
    for row in restored_interests.values():
        current=current_interests.get(row['id'])
        if row['deleted_at'] and current and not current['deleted_at']:
            revoked.append({'id':row['id'],'keyword':row['keyword'],'effect':'备份中已删除，当前仍有效；恢复后重新变为已删除'})
    withdrawals=[]
    adjustments=[]
    for uid,row in sorted(restored_events.items()):
        current=current_events.get(uid)
        if not current or bool(current['calendar_visible'])==bool(row['calendar_visible']): continue
        policy='保留' if row['calendar_visible'] else '撤下'
        withdrawals.append(dict(uid=uid,title=_title(row),current_visible=bool(current['calendar_visible']),restored_visible=bool(row['calendar_visible']),policy=policy,withdrawal_reason=row['withdrawal_reason']))
    if baseline:
        for uid,row in sorted(restored_events.items()):
            current=current_events.get(uid)
            if not current: continue
            try: differs=_signature(current)!=_signature(row)
            except (json.JSONDecodeError,KeyError,TypeError): differs=True
            if not differs: continue
            known=max(int(current['sequence']),int(row['sequence']))
            updated=known+1
            if updated!=int(row['sequence']):
                adjustments.append(dict(uid=uid,title=_title(row),from_sequence=int(row['sequence']),to_sequence=updated,known_max=known))
        summary='已对照当前数据库列出版本、新增事件、来源别名、删除记录和撤下差异。'
        limitations=[]
    else:
        summary='数据恢复到备份点。'
        limitations=['无法取得当前库或最新快照作为版本基线，不能承诺手机无重复或状态无倒退。']
    return dict(baseline='present' if baseline else 'missing',summary=summary,limitations=limitations,integrity='ok',ics_snapshots=snapshots,lost_uids=lost,lost_interests=lost_interests,source_aliases=aliases,revoked_interests=revoked,withdrawals=withdrawals,sequence_adjustments=adjustments,credentials_restored=False,applied=False,state_epoch=None,preserved=None)

def _lines(title, rows, render):
    if not rows: return [title,'- 无']
    return [title,*[render(item) for item in rows]]

def _diff_lines(report):
    lines=[]
    lines.extend(_lines('备份后新增、恢复后将不再包含的事件：',report['lost_uids'],lambda item:f"- {item['uid']} {item['title']}"))
    aliases=report['source_aliases']
    alias_rows=[f"- 将丢失 {item['namespace']}/{item['id']} → {item['event_id']}" for item in aliases['lost']]
    alias_rows.extend(f"- 将恢复 {item['namespace']}/{item['id']} → {item['event_id']}" for item in aliases['returning'])
    alias_rows.extend(f"- 冲突 {item['namespace']}/{item['id']}：当前 {item['current_event_id']}，备份 {item['backup_event_id']}；保留备份中的身份映射" for item in aliases['conflicts'])
    lines.extend(_lines('来源别名差异：',alias_rows,lambda item:item))
    lines.extend(_lines('备份后新增、恢复后将不再包含的兴趣：',report['lost_interests'],lambda item:f"- {item['keyword']}：{item['effect']}"))
    lines.extend(_lines('删除记录与已撤销兴趣：',report['revoked_interests'],lambda item:f"- {item['keyword']}：{item['effect']}"))
    lines.append('撤下差异与保留/撤下策略：')
    if report['withdrawals']:
        for item in report['withdrawals']:
            lines.append(f"- {item['uid']} {item['title']}：当前{'保留' if item['current_visible'] else '已撤下'}，备份中{'保留' if item['restored_visible'] else '已撤下'}。恢复后策略：{item['policy']}")
    else: lines.append('- 无')
    lines.append('SEQUENCE：')
    if report['sequence_adjustments']:
        lines.extend(f"- {item['uid']} {item['title']}：从 {item['from_sequence']} 提升到 {item['to_sequence']}（高于已知最大值 {item['known_max']}）" for item in report['sequence_adjustments'])
    else: lines.append('- 无需把事件 SEQUENCE 提升到已知最大值之上。')
    return lines

def _render(report):
    lines=['数据恢复报告',f"完整性检查：{'通过' if report['integrity']=='ok' else report['integrity']}",f"日历快照：{report['ics_snapshots']} 份解析通过",f"基线：{'当前数据库' if report['baseline']=='present' else '缺失'}","摘要："+report['summary']]
    lines.extend('限制：'+item for item in report['limitations'])
    if report['baseline']=='missing':
        lines.append('无法取得当前库或最新快照，不能列出备份后的新增事件、来源别名、删除记录或撤下差异。')
        lines.append('SEQUENCE：')
        lines.append('- 缺少版本基线，不调整 SEQUENCE，也不能仅靠把已有事件 SEQUENCE 加一来保证手机状态。')
    else: lines.extend(_diff_lines(report))
    lines.append('数据库备份不恢复凭据，旧令牌不会复活。')
    lines.append('会话只保存在进程内存中；恢复前须停止服务，停止后会话即清空。')
    lines.append('每次恢复生成新的 state_epoch，并增加一次 data_revision 以触发新快照。')
    lines.append('旧 state_epoch 的批次只作为恢复前记录显示，不能与新的发布进度比较；未完成的旧批次需要重新采集核验。')
    if report['applied']:
        lines.append('已执行恢复。')
        lines.append('新 state_epoch：'+report['state_epoch'])
        if report['preserved']: lines.append('恢复前数据库已保留：'+report['preserved'])
    else: lines.append('尚未修改当前数据库。核对后使用 --apply 执行恢复。')
    return '\n'.join(lines)+'\n'

def _apply_adjustments(path, adjustments, epoch):
    db=_connect(path)
    try:
        for item in adjustments:
            db.execute('UPDATE events SET sequence=?,ics_modified_at=? WHERE uid=?',(item['to_sequence'],stamp(),item['uid']))
        db.execute('UPDATE settings SET state_epoch=?,data_revision=data_revision+1',(epoch,))
        db.commit()
    finally: db.close()

def _preserve(directory, source):
    folder=Path(directory)/'restore-preserved'
    folder.mkdir(parents=True,exist_ok=True)
    folder.chmod(0o700)
    dest=folder/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.sqlite3')
    try: copied=str(copy_database(source,dest))
    except sqlite3.Error:
        shutil.copy2(source,dest)
        dest.chmod(0o600)
        copied=str(dest)
    for old in folder.glob('*.sqlite3'):
        if old.resolve()!=Path(copied).resolve(): old.unlink()
    return copied

def _install(source, live):
    temporary=live.with_name(live.name+'.restore')
    copy_database(source,temporary)
    for suffix in ('-wal','-shm'):
        extra=Path(str(live)+suffix)
        if extra.exists(): extra.unlink()
    temporary.replace(live)
    live.chmod(0o600)

def restore_database(directory, backup, apply=False):
    directory=Path(directory)
    backup=Path(backup)
    if not backup.is_file(): raise RestoreError('备份文件不存在')
    live=directory/'calendar.sqlite3'
    with _held_lock(directory):
        with tempfile.TemporaryDirectory() as tmp:
            checked=Path(tmp)/'backup.sqlite3'
            try: copy_database(backup,checked)
            except sqlite3.Error as exc: raise RestoreError('备份无法读取，未修改当前数据库') from exc
            snapshots=_validate_backup(checked)
            restored=_load(checked)
            if restored is None: raise RestoreError('备份完整性检查失败')
            baseline=_load(live)
            report=_build_report(baseline,restored,snapshots)
            if apply:
                epoch=str(uuid.uuid4())
                if live.exists(): report['preserved']=_preserve(directory,live)
                _apply_adjustments(checked,report['sequence_adjustments'],epoch)
                _install(checked,live)
                report['applied']=True
                report['state_epoch']=epoch
        report['text']=_render(report)
        folder=directory/'restore-reports'
        folder.mkdir(parents=True,exist_ok=True)
        folder.chmod(0o700)
        destination=folder/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.txt')
        destination.write_text(report['text'],encoding='utf-8')
        destination.chmod(0o600)
        for old in sorted(folder.glob('*.txt'))[:-7]: old.unlink()
        report['report_path']=str(destination)
        return report

def stop_service(directory, timeout=15):
    directory=Path(directory)
    pid_path=directory/'server.pid'
    if not service_running(directory):
        pid_path.unlink(missing_ok=True)
        return '服务未运行'
    if not pid_path.exists():
        raise RestoreError('服务正在运行，但未找到 server.pid。请结束占用该数据目录的进程后再恢复。')
    pid=int(pid_path.read_text().strip())
    command=Path(f'/proc/{pid}/cmdline')
    if command.exists():
        text=command.read_bytes().replace(b'\x00',b' ').decode(errors='replace')
        if 'app' not in text and 'uvicorn' not in text:
            raise RestoreError('server.pid 指向的进程不是兴趣日历服务')
    os.kill(pid,signal.SIGTERM)
    deadline=time.time()+timeout
    while time.time()<deadline:
        if not service_running(directory):
            pid_path.unlink(missing_ok=True)
            return '服务已停止'
        time.sleep(0.1)
    raise RestoreError('等待服务停止超时')
