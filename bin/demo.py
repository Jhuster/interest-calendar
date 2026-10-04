"""Create explicitly fictional preview data in an isolated directory only."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1] / 'src'))
import json, os
from datetime import timedelta
from app.config import Config, initialize
from app.storage.database import Store
from app.services.domain import context, import_batch, canonical, uid, norm, create_invite
from app.services.calendar import publish
from app.models.protocol import stamp, now, TZ

def main():
    folder=Path(os.environ.get('CALENDAR_DATA_DIR', str(Path(__file__).resolve().parent / 'data' / 'demo')));config=Config(folder);tokens=initialize(config)
    if tokens:
        fd=os.open(folder/'preview-tokens.json',os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as f:json.dump(tokens,f)
    store=Store(folder)
    with store.tx(True) as db:
        if db.execute('SELECT count(*) FROM interests').fetchone()[0]:return
        aid=db.execute("SELECT id FROM accounts WHERE kind='public'").fetchone()[0]
        ids=[]
        for keyword,condition in [('足球比赛','成年男子国家队 · 正式比赛'),('演唱会','上海 · 周末 · 喜欢的歌手'),('本地活动','上海 · 市集、展览与城市漫步'),('产品发布会','手机与 AI 工具 · 官方发布')]:
            i=uid();ids.append(i);db.execute('INSERT INTO interests(id,keyword,conditions,normalized_key,created_at,deleted_at,account_id) VALUES(?,?,?,?,?,NULL,?)',(i,keyword,condition,canonical([norm(keyword),norm(condition)]),stamp(),aid))
        db.execute('UPDATE settings SET config_version=5 WHERE account_id=?',(aid,))
        for label in ('林同学','陈先生','周末观众'): create_invite(db,label)
        ctx=context(db)
    events=[];today=now().astimezone(TZ).date()
    for n,(title,place,offset) in enumerate([('中国男足国际邀请赛','上海 · 海风体育场',1),('星光巡回演唱会 · 上海站','上海 · 星河体育馆',2),('秋日生活市集','上海 · 河岸文化街区',3),('新一代智能手机秋季发布会','线上直播',4)]):
        d=today+timedelta(days=offset)
        timing={'kind':'date','start_date':str(d),'end_date_exclusive':str(d+timedelta(days=3))} if n==2 else {'kind':'timed','start':str(d)+'T19:30:00+08:00','end':str(d)+'T21:30:00+08:00'}
        events.append(dict(event_id=None,base_version=0,source_key={'namespace':'example.org','id':f'demo-{n}'},interest_ids=[ids[n%4]],title=title,location=place,status='confirmed',timing=timing,evidence=[{'url':f'https://example.org/demo/{n}','excerpt':'市集汇集独立手作、街头音乐和秋日美食。每日 10:00–20:00 开放，适合周末与朋友一同逛逛。' if n==2 else '活动将于上述日期举行，详情和参与方式以主办方发布为准。','source_timezone':'Asia/Shanghai','verified_at':ctx['server_time']}]))
    batch=dict(protocol_version='1.0',batch_id=uid(),state_epoch=ctx['state_epoch'],config_version=ctx['config_version'],collection_started_at=ctx['server_time'],generated_at=stamp(),window=ctx['window'],result='complete',warnings=[],events=events,candidates=[])
    result,status=import_batch(store,batch)
    if status!=202:raise RuntimeError(result)
    publish(store)
    print(f'已生成隔离的虚构预览数据；凭据保存在 {folder / "preview-tokens.json"}（不进入 Git）。')
if __name__=='__main__':main()
