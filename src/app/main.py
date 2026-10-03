import asyncio, fcntl, hmac, secrets, sqlite3, sys, time
import logging
import os
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, HTMLResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware
from app.config import Config, token_hash, initialize
from app.models.protocol import *
from app.storage.database import Store
from app.services.domain import *
from app.services.calendar import publish
from app.services.maintenance import record_backup, rotate_service_log, startup_token_message

ROOT=Path(__file__).parent

def _same(left, right):
    if not isinstance(left,str) or not isinstance(right,str) or len(left)!=len(right): return False
    return hmac.compare_digest(left, right)

def calendar_feed(path):
    if path=='/public/calendar.ics': return True
    parts=path.split('/')
    return len(parts)==4 and parts[0]=='' and parts[1]=='c' and bool(parts[2]) and parts[3]=='calendar.ics'

def anonymous_ok(path, method):
    if path in ('/login','/health') or calendar_feed(path) or path.startswith('/static/'): return True
    if path=='/api/v1/session' and method=='POST': return True
    if method in ('GET','HEAD') and path in ('/','/interests','/subscribe','/api/v1/events','/api/v1/interests','/api/v1/status'): return True
    if method in ('GET','HEAD') and path.startswith('/api/v1/events/'): return True
    return False

def create_app(config=None):
    config=config or Config.environment();config.validate();sessions={};limits=defaultdict(deque)
    def attempt_publish():
        try: publish(app.state.store)
        except Exception:
            logging.getLogger('uvicorn.error').exception('发布失败，保留最后成功日历；稍后自动重试')
    @asynccontextmanager
    async def lifespan(app):
        config.data_dir.mkdir(exist_ok=True,parents=True)
        first_tokens=initialize(config)
        shown={k:v for k,v in first_tokens.items() if k in ('admin','agent')}
        if shown:
            first_run=config.data_dir/'first-run-credentials.txt'
            fd=os.open(first_run,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(fd,'w') as credentials_file:
                credentials_file.write('\n'.join(f'{k.upper()}_TOKEN={v}' for k,v in shown.items())+'\n')
        first_run=config.data_dir/'first-run-credentials.txt'
        if first_run.exists():
            logging.getLogger('uvicorn.error').info('首次凭据文件：%s', first_run.resolve())
        lock=open(config.data_dir/'service.lock','a')
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close();raise RuntimeError('This data directory is already served by another process')
        task=None
        pid_path=config.data_dir/'server.pid'
        try:
            credential_path=config.data_dir/'credentials.json'
            print(startup_token_message(config.saved_token('admin'),credential_path.resolve(),sys.stdout.isatty()),flush=True)
            app.state.store=Store(config.data_dir,config.saved_token('calendar'));app.state.backup_error=None;attempt_publish();record_backup(app.state.store,app.state)
            pid_path.write_text(str(os.getpid())+'\n')
            pid_path.chmod(0o600)
            logging.getLogger('uvicorn.error').info('浏览器访问地址：%s/', config.base_url.rstrip('/'))
            async def worker():
                delay=5
                while True:
                    await asyncio.sleep(delay)
                    await asyncio.to_thread(attempt_publish)
                    await asyncio.to_thread(rotate_service_log,config.data_dir)
                    await asyncio.to_thread(record_backup,app.state.store,app.state)
                    with app.state.store.tx() as db: failed=db.execute('SELECT 1 FROM settings WHERE publish_error IS NOT NULL').fetchone()
                    delay=min(300,30 if delay==5 else delay*4) if failed else 5
            task=asyncio.create_task(worker())
            yield
        finally:
            if task:
                task.cancel()
                try: await task
                except asyncio.CancelledError: pass
            lock.close();sessions.clear();pid_path.unlink(missing_ok=True)
    app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.config=config
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=config.allowed_hosts())
    templates=Jinja2Templates(directory=ROOT/'templates')
    app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')
    def answer(request,data,status=200):
        return JSONResponse(dict(data,request_id=request.state.request_id),status_code=status)
    def rate(key,maximum):
        queue=limits[key];current=time.monotonic()
        while queue and queue[0]<current-60: queue.popleft()
        if len(queue)>=maximum: raise Problem('RATE_LIMITED','操作过于频繁，请稍后重试',429,action='WAIT_AND_RETRY')
        queue.append(current)
    @app.middleware('http')
    async def boundary(request,call_next):
        request.state.request_id=uid();request.state.role=None;request.state.clear_session=False
        try:
            public=anonymous_ok(request.url.path, request.method)
            credentials=config.credentials(); cookie=request.cookies.get('session');session=sessions.get(cookie)
            request.state.clear_session=False
            if session and not session_ok(session, credentials):
                sessions.pop(cookie,None);session=None;request.state.clear_session=True
            if session: request.state.role=session['kind'];request.state.session=session
            authorization=request.headers.get('authorization','')
            if authorization.startswith('Bearer ') and hmac.compare_digest(token_hash(authorization[7:]),credentials['agent']) and (not session or session['kind']!='invitee'): request.state.role='agent'
            if request.url.path=='/settings': raise Problem('NOT_FOUND','页面不存在',404)
            if request.url.path in ('/admin','/model') and request.state.role!='admin': raise Problem('FORBIDDEN','没有权限查看此页面',403)
            if not public and not request.state.role:
                if not request.url.path.startswith('/api/'):
                    redirected=RedirectResponse('/login',303)
                    if request.state.clear_session: redirected.delete_cookie('session')
                    return redirected
                raise Problem('AUTH_REQUIRED','请先登录或配置有效 Agent 凭据',401,action='CONTACT_USER')
            if request.method not in ('GET','HEAD','OPTIONS'):
                if request.state.role!='agent':
                    if request.headers.get('origin') not in config.accepted_origins(): raise Problem('FORBIDDEN','请求来源不匹配',403)
                    if request.state.role in ('admin','invitee') and not hmac.compare_digest(request.headers.get('x-csrf-token',''),session['csrf']): raise Problem('FORBIDDEN','页面凭据已过期，请刷新',403)
                if request.headers.get('content-encoding'): raise Problem('SCHEMA_INVALID','不支持压缩上传',415)
                chunks=[];size=0
                async for chunk in request.stream():
                    size+=len(chunk)
                    if size>2*1024*1024: raise Problem('PAYLOAD_TOO_LARGE','请求超过2 MiB',413)
                    chunks.append(chunk)
                request._body=b''.join(chunks)
            response=await call_next(request)
        except Problem as exc:
            response=answer(request,{'import_status':'rejected','publication_status':'not_required','errors':[exc.error]},exc.status)
            if exc.status==429: response.headers['Retry-After']='60'
        except sqlite3.OperationalError:
            response=answer(request,{'errors':[Problem('TEMPORARILY_UNAVAILABLE','数据暂不可写，请稍后重试',503).error]},503)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        if not (calendar_feed(request.url.path) and response.status_code in (200,304)): response.headers['Cache-Control']='no-store'
        if getattr(request.state,'clear_session',False): response.delete_cookie('session')
        return response
    def session_ok(session, credentials):
        if session.get('expires',0)<time.time(): return False
        if session.get('kind')=='admin':
            return _same(session.get('admin_hash'), credentials['admin'])
        if session.get('kind')=='invitee':
            with app.state.store.tx() as db:
                row=db.execute('SELECT login_hash,revoked_at FROM accounts WHERE id=?',(session.get('account_id'),)).fetchone()
            if not row or row['revoked_at'] or not row['login_hash']: return False
            return _same(session.get('login_hash'), row['login_hash'])
        return False
    @app.exception_handler(Problem)
    async def errors(request,exc):
        response=answer(request,{'import_status':'rejected','publication_status':'not_required','errors':[exc.error]},exc.status)
        if exc.status==429: response.headers['Retry-After']='60'
        return response
    @app.exception_handler(RequestValidationError)
    async def validation_errors(request,exc):
        return answer(request,{'errors':[Problem('SCHEMA_INVALID','请求参数类型或格式不正确',path='/'+'/'.join(str(x) for x in e['loc'])).error for e in exc.errors()]},422)
    def admin(request):
        if request.state.role!='admin': raise Problem('FORBIDDEN','此操作需要管理员登录',403)
    def agent(request):
        if request.state.role!='agent': raise Problem('FORBIDDEN','上传请使用独立 Agent 凭据',403)
    async def body(request): return strict_json(await request.body())
    @app.get('/health')
    def health(): return {'ok':True}
    def signed_in(request):
        if request.state.role=='agent': raise Problem('FORBIDDEN','此操作需要登录',403)
        if request.state.role not in ('admin','invitee'): raise Problem('AUTH_REQUIRED','请先登录',401)
        return request.state.session
    def subscription_for(account):
        if account['kind']=='public': return config.subscription_url()
        return config.subscription_url_for(account['calendar_token'])
    def account_id_for(request, db):
        if request.state.role=='invitee': return request.state.session['account_id']
        return db.execute("SELECT id FROM accounts WHERE kind='public'").fetchone()[0]
    @app.post('/api/v1/session')
    async def login(request:Request):
        rate(('login',request.client.host),5); b=await body(request)
        token=b.get('token'); creds=config.credentials()
        if not isinstance(token,str): raise Problem('AUTH_REQUIRED','令牌不正确',401)
        digest=token_hash(token); sid=secrets.token_urlsafe(32); csrf=secrets.token_urlsafe(32)
        if _same(digest, creds['admin']):
            with app.state.store.tx() as db: account_id=db.execute("SELECT id FROM accounts WHERE kind='public'").fetchone()[0]
            sessions[sid]={'kind':'admin','account_id':account_id,'csrf':csrf,'expires':time.time()+8*3600,'admin_hash':creds['admin']}
        else:
            with app.state.store.tx() as db:
                row=db.execute("SELECT id,login_hash,revoked_at FROM accounts WHERE kind='invite' AND login_hash=?",(digest,)).fetchone()
            if not row or row['revoked_at'] or not _same(digest, row['login_hash']): raise Problem('AUTH_REQUIRED','令牌不正确',401)
            sessions[sid]={'kind':'invitee','account_id':row['id'],'csrf':csrf,'expires':time.time()+8*3600,'login_hash':row['login_hash']}
        response=answer(request,{'csrf_token':csrf});response.set_cookie('session',sid,httponly=True,secure=urlparse(config.base_url).scheme=='https',samesite='strict',max_age=8*3600)
        return response
    @app.delete('/api/v1/session')
    def logout(request:Request):
        signed_in(request);sessions.pop(request.cookies.get('session'),None);r=answer(request,{'ok':True});r.delete_cookie('session');return r
    @app.get('/api/v1/agent/schema/1.0')
    def schema(): return SCHEMA
    @app.get('/api/v1/agent/token')
    def agent_token(request:Request):
        admin(request)
        token=config.agent_token()
        if not token: raise Problem('TOKEN_UNAVAILABLE','旧令牌原文已不存在，请在本机重新生成 Agent 令牌后再复制',409)
        return answer(request,{'token':token})
    @app.get('/api/v1/agent/guide')
    def guide():
        return Response((ROOT/'agent-guide.md').read_text().replace('{{BASE_URL}}',config.base_url),media_type='text/markdown')
    @app.get('/api/v1/agent/config')
    @app.get('/api/v1/agent/context')
    def get_context(request:Request):
        if request.state.role not in ('admin','agent'): raise Problem('FORBIDDEN','此操作需要管理员或 Agent 凭据',403)
        with app.state.store.tx() as db: result=context(db)
        result.update(schema_url=config.base_url+'/api/v1/agent/schema/1.0',upload_url=config.base_url+'/api/v1/batches')
        return answer(request,result)
    @app.get('/api/v1/interests')
    def interests(request:Request):
        if request.state.role=='agent': raise Problem('FORBIDDEN','此操作需要登录',403)
        with app.state.store.tx() as db:
            aid=account_id_for(request, db)
            s=settings(db, aid)
            items=[without_account(r) for r in db.execute('SELECT * FROM interests WHERE account_id=? AND deleted_at IS NULL ORDER BY created_at,id',(aid,))]
            events=[exposed(r) for r in db.execute('SELECT * FROM events WHERE account_id=?',(aid,))]
            result={'interests':items,'events':events,'state_epoch':s['state_epoch'],'config_version':s['config_version']}
            active_ids={item['id'] for item in result['interests']}
            for interest in result['interests']:
                matching=[e for e in result['events'] if interest['id'] in e['interest_ids'] and future(e)]
                interest['future_count']=len(matching)
                interest['exclusive_count']=sum(not any(other in e['interest_ids'] for other in active_ids if other!=interest['id']) for e in matching)
            return answer(request,{'items':result['interests'],'state_epoch':result['state_epoch'],'config_version':result['config_version']})
    @app.post('/api/v1/interests')
    async def add_interest(request:Request):
        session=signed_in(request);b=await body(request);keyword=b.get('keyword');conditions=b.get('conditions','')
        if not isinstance(keyword,str) or not keyword.strip() or len(keyword)>200 or not isinstance(conditions,str) or len(conditions)>2000: raise Problem('SCHEMA_INVALID','关键词必填且不超过200字，补充条件不超过2000字')
        with app.state.store.tx(True) as db:
            aid=account_id_for(request, db);precondition(db,b,aid);i=uid()
            try: db.execute('INSERT INTO interests VALUES(?,?,?,?,?,NULL,?)',(i,keyword.strip(),conditions.strip(),canonical([norm(keyword),norm(conditions)]),stamp(),aid))
            except sqlite3.IntegrityError: raise Problem('INTEREST_EXISTS','这个兴趣及条件已经添加',409)
            db.execute('UPDATE settings SET config_version=config_version+1 WHERE account_id=?',(aid,))
        return answer(request,{'id':i},201)
    @app.delete('/api/v1/interests/{interest_id}')
    async def remove_interest(interest_id:str,request:Request):
        session=signed_in(request);b=await body(request)
        with app.state.store.tx(True) as db: delete_interest(db,interest_id,b,account_id_for(request, db))
        return answer(request,{'ok':True})
    @app.post('/api/v1/admin/reset')
    async def reset_admin(request:Request):
        admin(request); b=await body(request)
        if b.get('confirmation') != 'RESET':
            raise Problem('CONFIRMATION_REQUIRED','请输入 RESET 确认此操作',400)
        with app.state.store.tx(True) as db:
            chosen=b.get('account_id', None)
            if chosen is None: aid=account_id_for(request, db)
            else:
                if not isinstance(chosen,str) or not chosen: raise Problem('SCHEMA_INVALID','account_id 必须是账户 id')
                row=db.execute('SELECT id FROM accounts WHERE id=?',(chosen,)).fetchone()
                if not row: raise Problem('NOT_FOUND','账户不存在',404)
                aid=row['id']
            precondition(db,b,aid)
            reset_data(db, b.get('scope'), aid)
            db.execute('INSERT INTO audit_log(operation,created_at,result,request_id) VALUES(?,?,?,?)',('reset',stamp(),b['scope'],request.state.request_id))
        try: app.state.store.vacuum()
        except Exception: logging.getLogger('uvicorn.error').exception('压缩数据库失败')
        return answer(request,{'ok':True,'scope':b.get('scope'),'account_id':aid})
    @app.get('/api/v1/events')
    def events(request:Request,interest_id:str|None=None,start_date:str|None=None,end_date:str|None=None,limit:int=50,offset:int=0):
        if not 1<=limit<=200 or offset<0: raise Problem('SCHEMA_INVALID','分页范围错误')
        try: start=date.fromisoformat(start_date) if start_date else None;end=date.fromisoformat(end_date) if end_date else None
        except ValueError: raise Problem('TIME_INVALID','日期格式应为 YYYY-MM-DD')
        with app.state.store.tx() as db: rows=[exposed(r) for r in db.execute('SELECT * FROM events WHERE account_id=?',(account_id_for(request,db),))]
        rows=[e for e in rows if (request.state.role=='agent' or e['calendar_visible']) and (not interest_id or interest_id in e['interest_ids']) and (not start or days(e['timing'])[1]>start) and (not end or days(e['timing'])[0]<end)]
        rows.sort(key=lambda e:(e['start_date'],e['id']))
        return answer(request,{'items':rows[offset:offset+limit],'total':len(rows)})
    @app.get('/api/v1/events/{eid}')
    def get_event(eid:str,request:Request):
        with app.state.store.tx() as db:
            result=exposed(event(db,eid,account_id_for(request,db)))
        if request.state.role not in ('admin','invitee','agent') and not result['calendar_visible']: raise Problem('NOT_FOUND','事件不存在',404)
        return answer(request,result)
    @app.post('/api/v1/batches')
    async def upload(request:Request):
        agent(request);rate(('upload','agent'),10);result,status=import_batch(app.state.store,await body(request));return answer(request,result,status)
    @app.get('/api/v1/batches')
    def batches(request:Request):
        signed_in(request)
        with app.state.store.tx() as db: rows=[without_account(r)|{'receipt':receipt(db,r)} for r in db.execute('SELECT * FROM batches WHERE account_id=? ORDER BY received_at DESC,batch_id LIMIT 200',(account_id_for(request,db),))]
        return answer(request,{'items':rows})
    @app.get('/api/v1/batches/{bid}')
    def get_batch(bid:str,request:Request,state_epoch:str|None=None):
        with app.state.store.tx() as db:
            if request.state.role=='agent':
                epoch(db,state_epoch); aid=account_of(db)
            elif request.state.role in ('admin','invitee'): aid=account_id_for(request, db)
            else: raise Problem('AUTH_REQUIRED','请先登录',401)
            row=db.execute('SELECT * FROM batches WHERE batch_id=? AND account_id=?',(bid,aid)).fetchone()
            if not row: raise Problem('NOT_FOUND','未找到该批次',404)
            return answer(request,receipt(db,row),row['status'] if row['status']>=400 else 200)
    @app.get('/api/v1/candidates')
    def candidates(request:Request):
        with app.state.store.tx() as db:
            if request.state.role=='agent': aid=account_of(db)
            elif request.state.role in ('admin','invitee'): aid=account_id_for(request, db)
            else: raise Problem('AUTH_REQUIRED','请先登录',401)
            rows=[without_account(r)|{'payload':json.loads(r['payload_json'])} for r in db.execute('SELECT * FROM candidates WHERE account_id=? ORDER BY state,id',(aid,))]
        return answer(request,{'items':rows})
    @app.post('/api/v1/candidates/{cid}/resolve')
    async def candidate_resolve(cid:str,request:Request):
        admin(request);b=await body(request)
        with app.state.store.tx(True) as db: resolve(db,cid,b,account_id_for(request, db))
        return answer(request,{'ok':True})
    @app.get('/api/v1/status')
    def status(request:Request):
        if request.state.role=='agent': raise Problem('FORBIDDEN','此操作需要登录',403)
        with app.state.store.tx() as db:
            aid=account_id_for(request, db)
            role=request.state.role if request.state.role in ('admin','invitee') else None
            account=dict(db.execute('SELECT * FROM accounts WHERE id=?',(aid,)).fetchone())
            s=settings(db,aid);pub=db.execute('SELECT data_revision,created_at FROM publications WHERE id=?',(s['active_publication_id'],)).fetchone()
            s.update(published_revision=pub['data_revision'] if pub else -1,published_at=pub['created_at'] if pub else None,candidate_count=db.execute("SELECT count(*) FROM candidates WHERE account_id=? AND state='pending'",(aid,)).fetchone()[0],last_received=db.execute('SELECT max(received_at) FROM batches WHERE account_id=?',(aid,)).fetchone()[0],last_imported=db.execute('SELECT max(received_at) FROM batches WHERE account_id=? AND status<400',(aid,)).fetchone()[0],base_url=config.base_url,subscription_url=subscription_for(account),development=config.development,backup_error=app.state.backup_error,role=role,account_kind=account['kind'],account_label=account['label'])
        return answer(request,s)
    @app.get('/api/v1/admin/accounts')
    def account_list(request:Request):
        admin(request)
        with app.state.store.tx() as db:
            items=[]
            query='''SELECT a.id,a.kind,a.label,a.revoked_at,a.created_at,s.state_epoch,s.config_version
                FROM accounts a JOIN settings s ON s.account_id=a.id
                ORDER BY CASE a.kind WHEN 'public' THEN 0 ELSE 1 END, a.created_at, a.id'''
            for row in db.execute(query):
                items.append(dict(id=row['id'],kind=row['kind'],label=row['label'],revoked_at=row['revoked_at'],created_at=row['created_at'],state_epoch=row['state_epoch'],config_version=row['config_version']))
        return answer(request,{'items':items})
    @app.get('/api/v1/admin/accounts/{aid}/token')
    def account_token(aid:str,request:Request):
        admin(request)
        with app.state.store.tx() as db:
            row=db.execute('SELECT id,kind,login_token FROM accounts WHERE id=?',(aid,)).fetchone()
        if not row: raise Problem('NOT_FOUND','账户不存在',404)
        if row['kind']=='public':
            token=config.saved_token('admin')
            if not token: raise Problem('TOKEN_UNAVAILABLE','旧令牌原文已不存在，请在本机重新生成管理令牌后再查看',409)
        else:
            token=row['login_token']
            if not token: raise Problem('TOKEN_UNAVAILABLE','这份登录令牌的原文没有留下，请重新签发后再查看',409)
        return answer(request,{'token':token})
    @app.post('/api/v1/admin/invites')
    async def add_invite(request:Request):
        admin(request);b=await body(request)
        with app.state.store.tx(True) as db: created=create_invite(db,b.get('label'))
        return answer(request,{'id':created['id'],'label':created['label'],'token':created['token']},201)
    @app.post('/api/v1/admin/invites/{aid}/revoke')
    def revoke(aid:str,request:Request):
        admin(request)
        with app.state.store.tx(True) as db: revoke_invite(db,aid)
        return answer(request,{'ok':True})
    @app.post('/api/v1/admin/invites/{aid}/reissue')
    def reissue(aid:str,request:Request):
        admin(request)
        with app.state.store.tx(True) as db: created=reissue_invite(db,aid)
        return answer(request,{'id':created['id'],'token':created['token']})
    @app.post('/api/v1/publications/retry')
    async def retry(request:Request):
        admin(request);b=await body(request)
        with app.state.store.tx() as db: epoch(db,b.get('state_epoch'),account_id_for(request, db))
        attempt_publish();return answer(request,{'ok':True})
    def calendar_body(request, row):
        if not row: raise Problem('TEMPORARILY_UNAVAILABLE','日历尚未初始化',503)
        tag='"'+row['sha256']+'"';headers={'ETag':tag,'Cache-Control':'no-cache'}
        tags=[x.strip().removeprefix('W/') for x in request.headers.get('if-none-match','').split(',')]
        if tag in tags or '*' in tags: return Response(status_code=304,headers=headers)
        return Response(b'' if request.method=='HEAD' else row['ics_blob'],media_type='text/calendar; charset=utf-8',headers=headers)
    def publication_for(db, account_id):
        return db.execute('SELECT p.* FROM publications p JOIN settings s ON s.account_id=? AND s.active_publication_id=p.id',(account_id,)).fetchone()
    @app.api_route('/public/calendar.ics',methods=['GET','HEAD'])
    def public_calendar(request:Request):
        with app.state.store.tx() as db:
            account_id=db.execute("SELECT id FROM accounts WHERE kind='public'").fetchone()[0]
            row=publication_for(db, account_id)
        return calendar_body(request, row)
    @app.api_route('/c/{token}/calendar.ics',methods=['GET','HEAD'])
    def calendar(token:str,request:Request):
        digest=token_hash(token)
        with app.state.store.tx() as db:
            account=db.execute("SELECT id,calendar_hash FROM accounts WHERE kind='invite' AND revoked_at IS NULL AND calendar_hash=?",(digest,)).fetchone()
            if not account or not _same(digest, account['calendar_hash']): raise Problem('NOT_FOUND','未找到日历',404)
            row=publication_for(db, account['id'])
        return calendar_body(request, row)
    @app.get('/login',response_class=HTMLResponse)
    @app.get('/',response_class=HTMLResponse)
    @app.get('/interests',response_class=HTMLResponse)
    @app.get('/subscribe',response_class=HTMLResponse)
    @app.get('/model',response_class=HTMLResponse)
    @app.get('/admin',response_class=HTMLResponse)
    @app.get('/candidates',response_class=HTMLResponse)
    @app.get('/runs',response_class=HTMLResponse)
    def page(request:Request):
        if request.url.path in ('/admin','/model') and request.state.role!='admin':
            raise Problem('FORBIDDEN','没有权限查看此页面',403)
        if request.url.path not in ('/','/login','/interests','/subscribe') and request.state.role not in ('admin','invitee'):
            raise Problem('FORBIDDEN','此操作需要登录',403)
        role=request.state.role if request.state.role in ('admin','invitee') else ''
        label='';kind=''
        if role=='admin':
            with app.state.store.tx() as db:
                row=db.execute("SELECT label,kind FROM accounts WHERE kind='public'").fetchone()
                label=row['label'];kind=row['kind']
        elif role=='invitee':
            with app.state.store.tx() as db:
                row=db.execute('SELECT label,kind FROM accounts WHERE id=?',(request.state.session['account_id'],)).fetchone()
                label=row['label'];kind=row['kind']
        return templates.TemplateResponse(request=request,name='index.html',context={'page':request.url.path,'csrf':getattr(request.state,'session',{}).get('csrf',''),'role':role,'account_label':label,'account_kind':kind})
    return app
