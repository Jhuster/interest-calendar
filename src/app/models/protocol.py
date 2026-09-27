"""The checked-in JSON Schema is the single strict wire-format authority."""
import json
from pathlib import Path
from datetime import datetime, timezone, date, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((ROOT / 'schemas/batch-1.0.schema.json').read_text())
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
TZ = ZoneInfo('Asia/Shanghai')

def now():
    return datetime.now(timezone.utc)

def stamp():
    return now().isoformat()

def dt(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)

class Problem(Exception):
    def __init__(self, code, message, status=422, path='', expected=None, action='FIX_AND_RESUBMIT'):
        self.status = status
        self.error = dict(code=code, message=message, path=path, expected=expected, action=action, retryable=status in (429,503))
        super().__init__(message)

def strict_json(raw):
    def pairs(items):
        result = {}
        for k,v in items:
            if k in result:
                raise ValueError('duplicate key')
            result[k] = v
        return result
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeDecodeError):
        raise Problem('INVALID_JSON', '需要有效的 JSON 对象，不能包含重复键或非有限数字',400)

def schema_validate(value, definition=None):
    validator = VALIDATOR if not definition else Draft202012Validator({'$ref':f'#/$defs/{definition}', '$defs':SCHEMA['$defs']}, format_checker=FormatChecker())
    error = next(validator.iter_errors(value), None)
    if error:
        raise Problem('SCHEMA_INVALID',error.message,path='/'+'/'.join(str(x).replace('~','~0').replace('/','~1') for x in error.absolute_path))

def days(timing):
    if timing['kind']=='date':
        return date.fromisoformat(timing['start_date']),date.fromisoformat(timing['end_date_exclusive'])
    start = dt(timing['start']).astimezone(TZ).date()
    end = dt(timing['end']).astimezone(TZ) if timing['end'] else None
    return start, (end-timedelta(microseconds=1)).date()+timedelta(days=1) if end else start+timedelta(days=1)

def future(payload):
    t=payload['timing']
    return dt(t['start'])>now() if t['kind']=='timed' else date.fromisoformat(t['start_date'])>now().astimezone(TZ).date()

def evidence_check(evidence, generated):
    for item in evidence:
        if dt(item['verified_at'])>generated:
            raise Problem('TIME_INVALID','证据核验时间不能晚于生成时间')
        if item['source_timezone']:
            try: ZoneInfo(item['source_timezone'])
            except (ZoneInfoNotFoundError, ValueError): raise Problem('TIME_INVALID','来源时区必须是有效 IANA 时区')

def event_check(e, generated, window=None):
    if not e['title'].strip(): raise Problem('SCHEMA_INVALID','标题不能为空')
    t=e['timing']
    if t['kind']=='timed':
        if t['end'] and dt(t['end'])<=dt(t['start']): raise Problem('TIME_INVALID','结束时间必须晚于开始时间')
    elif t['end_date_exclusive']<=t['start_date']:
        raise Problem('TIME_INVALID','结束日期必须晚于开始日期')
    evidence_check(e['evidence'],generated)
    if window and not e['event_id']:
        start,end=days(t)
        if start>=date.fromisoformat(window['end_date']) or end<=date.fromisoformat(window['start_date']):
            raise Problem('TIME_INVALID','新事件必须与采集窗口相交')

def batch_check(b):
    schema_validate(b)
    start,generated=dt(b['collection_started_at']),dt(b['generated_at'])
    if not now()-timedelta(hours=24)<=start<=generated<=now()+timedelta(minutes=5):
        raise Problem('TIME_INVALID','采集必须在24小时内，生成时间必须在采集之后且不超前5分钟')
    day=start.astimezone(TZ).date()
    if b['window']!={'start_date':str(day),'end_date':str(day+timedelta(days=7))}:
        raise Problem('TIME_INVALID','采集窗口应从采集日开始，共7个自然日')
    if len(b['events'])+len(b['candidates'])>500:
        raise Problem('SCHEMA_INVALID','单批合计最多500条')
    for e in b['events']: event_check(e,generated,b['window'])
    for c in b['candidates']:
        evidence_check(c['evidence'],generated)
        if c['proposed_event']: event_check(c['proposed_event'],generated,b['window'])
