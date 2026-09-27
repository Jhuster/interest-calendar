"""Create explicitly fictional preview data in an isolated directory only."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1] / 'src'))
import json, uuid, os
from datetime import timedelta
from app.config import Config, initialize
from app.storage.database import Store
from app.services.domain import context, import_batch, canonical, uid, norm
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
        ids=[]
        for keyword,condition in [('艺术展览','上海 · 周末'),('现场音乐','小型现场与音乐节'),('网球赛事','关注决赛日程')]:
            i=uid();ids.append(i);db.execute('INSERT INTO interests VALUES(?,?,?,?,?,NULL)',(i,keyword,condition,canonical([norm(keyword),norm(condition)]),stamp()))
        db.execute('UPDATE settings SET config_version=4')
        ctx=context(db)
    events=[];today=now().astimezone(TZ).date()
    for n,(title,place,offset) in enumerate([('光与空间：秋日艺术展','上海 · 演示艺术空间',2),('城市露台音乐现场','上海 · 演示音乐厅',5),('秋季网球公开赛决赛','演示网球中心',4),('周末摄影展','上海 · 演示画廊',6)]):
        d=today+timedelta(days=offset)
        timing={'kind':'date','start_date':str(d),'end_date_exclusive':str(d+timedelta(days=2))} if n in (0,3) else {'kind':'timed','start':str(d)+'T19:30:00+08:00','end':str(d)+'T21:30:00+08:00'}
        events.append(dict(event_id=None,base_version=0,source_key={'namespace':'example.org','id':f'demo-{n}'},interest_ids=[ids[n%3]],title=title,location=place,status='confirmed',timing=timing,evidence=[{'url':f'https://example.org/demo/{n}','excerpt':'仅用于网站界面和协议演示，活动、地点及时间均为虚构。','source_timezone':'Asia/Shanghai','verified_at':ctx['server_time']}]))
    batch=dict(protocol_version='1.0',batch_id=uid(),state_epoch=ctx['state_epoch'],config_version=ctx['config_version'],collection_started_at=ctx['server_time'],generated_at=stamp(),window=ctx['window'],result='complete',warnings=[],events=events,candidates=[])
    result,status=import_batch(store,batch)
    if status!=202:raise RuntimeError(result)
    publish(store)
    print(f'已生成隔离的虚构预览数据；凭据保存在 {folder / "preview-tokens.json"}（不进入 Git）。')
if __name__=='__main__':main()
