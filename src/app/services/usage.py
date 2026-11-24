"""Administrator-wide aggregate metrics, without exposing account contents."""
from datetime import datetime, time, timedelta, timezone
from app.models.protocol import now, TZ
from app.storage.usage import RETENTION_DAYS


def overview(store, usage, days=7):
    current = now()
    today = current.astimezone(TZ).date()
    first = today - timedelta(days=days-1)
    start = datetime.combine(first, time.min, TZ).astimezone(timezone.utc).isoformat()
    end = datetime.combine(today + timedelta(days=1), time.min, TZ).astimezone(timezone.utc).isoformat()
    daily = {str(first + timedelta(days=i)): dict(date=str(first + timedelta(days=i)), active_accounts=0,
             batches_succeeded=0, batches_failed=0, events_created=0, subscription_requests=0) for i in range(days)}
    with store.tx() as db:
        total = db.execute('SELECT count(*) FROM accounts').fetchone()[0]
        available = db.execute('SELECT count(*) FROM accounts WHERE revoked_at IS NULL').fetchone()[0]
        pending = db.execute("SELECT count(*) FROM candidates c JOIN accounts a ON a.id=c.account_id WHERE c.state='pending' AND a.revoked_at IS NULL").fetchone()[0]
        agent_accounts = db.execute('SELECT count(DISTINCT account_id) FROM batches WHERE julianday(received_at)>=julianday(?) AND julianday(received_at)<julianday(?)', (start, end)).fetchone()[0]
        latest = db.execute('SELECT max(received_at) FROM batches').fetchone()[0]
        for row in db.execute("""SELECT date(received_at,'+8 hours') AS day,
                sum(CASE WHEN status<400 THEN 1 ELSE 0 END) AS succeeded,
                sum(CASE WHEN status>=400 THEN 1 ELSE 0 END) AS failed
                FROM batches WHERE julianday(received_at)>=julianday(?) AND julianday(received_at)<julianday(?) GROUP BY day""", (start, end)):
            daily[row['day']].update(batches_succeeded=row['succeeded'], batches_failed=row['failed'])
        for row in db.execute("SELECT date(created_at,'+8 hours') AS day,count(*) AS count FROM events WHERE julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?) GROUP BY day", (start, end)):
            daily[row['day']]['events_created'] = row['count']
        resolutions = dict(approve=0, reject=0, merge=0, withdraw=0, reopen=0)
        for row in db.execute("SELECT operation,count(*) AS count FROM audit_log WHERE result='ok' AND julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?) GROUP BY operation", (start, end)):
            if row['operation'] in resolutions:
                resolutions[row['operation']] = row['count']
    started, rows = usage.snapshot(str(first), str(today))
    active, personal = set(), set()
    public_requests = personal_requests = not_modified = 0
    for row in rows:
        daily[row['day']]['active_accounts'] += row['active']
        daily[row['day']]['subscription_requests'] += row['subscriptions']
        if row['active']:
            active.add(row['account_id'])
        if row['is_public']:
            public_requests += row['subscriptions']
        else:
            personal_requests += row['subscriptions']
            if row['subscriptions']:
                personal.add(row['account_id'])
        not_modified += row['not_modified']
    traffic_started, views, visitors, unique = usage.traffic_snapshot(str(first), str(today))
    traffic_first_day = datetime.fromisoformat(traffic_started).astimezone(TZ).date()
    for day, row in daily.items():
        covered = day >= str(traffic_first_day)
        row['page_views'] = views.get(day, 0) if covered else None
        row['unique_visitors'] = visitors.get(day, 0) if covered else None
    window_days = days
    days = list(daily.values())
    return dict(as_of=current.isoformat(), timezone='Asia/Shanghai', window_days=window_days, period=dict(start_date=str(first), end_date=str(today)),
        tracking_started_at=started, retention_days=RETENTION_DAYS,
        traffic=dict(page_views=sum(views.values()), unique_visitors=unique, started_at=traffic_started),
        accounts=dict(total=total, available=available, revoked=total-available, active=len(active)),
        agent=dict(accounts=agent_accounts, succeeded=sum(d['batches_succeeded'] for d in days),
                   failed=sum(d['batches_failed'] for d in days), last_received_at=latest),
        calendar=dict(created=sum(d['events_created'] for d in days), pending=pending, resolutions=resolutions),
        subscriptions=dict(requests=public_requests+personal_requests, public_requests=public_requests,
                           personal_requests=personal_requests, personal_accounts=len(personal), not_modified=not_modified), daily=days)
