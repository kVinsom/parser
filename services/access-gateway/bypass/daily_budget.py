"""Calendar-day accounting, migrated without inventing historical daily usage."""

import calendar
from datetime import datetime, timedelta


def migrate(db):
    columns = {row[1] for row in db.execute('PRAGMA table_info(config)')}
    if 'daily_version' not in columns:
        db.execute('ALTER TABLE config ADD COLUMN daily_version INTEGER NOT NULL DEFAULT 0')
    version = db.execute('SELECT daily_version FROM config WHERE id=1').fetchone()[0]
    if version == 0:
        db.execute('CREATE TABLE daily_usage (day TEXT PRIMARY KEY, charged INTEGER NOT NULL CHECK(charged>=0))')
        db.execute('CREATE TABLE daily_baseline (month TEXT PRIMARY KEY, charged INTEGER NOT NULL CHECK(charged>=0))')
        db.execute('INSERT INTO daily_baseline SELECT month,charged FROM months')
        db.execute('UPDATE config SET daily_version=1 WHERE id=1')
    elif version != 1:
        raise ValueError('Unknown daily budget version')
    # Missing tables after migration cannot silently create a new baseline.
    db.execute('SELECT day,charged FROM daily_usage LIMIT 1')
    db.execute('SELECT month,charged FROM daily_baseline LIMIT 1')


def state(db, now, month, charged, stop):
    day = now.date().isoformat()
    latest = db.execute('SELECT MAX(day) FROM daily_usage').fetchone()[0]
    if latest and latest > day:
        raise ValueError('Daily budget clock rollback')
    rows = db.execute('SELECT day,charged FROM daily_usage WHERE day LIKE ? ORDER BY day', (month + '-%',)).fetchall()
    baseline = db.execute('SELECT charged FROM daily_baseline WHERE month=?', (month,)).fetchone()
    legacy = baseline[0] if baseline else 0
    if legacy + sum(row[1] for row in rows) != charged:
        raise ValueError('Daily and monthly budget totals disagree')
    days = calendar.monthrange(now.year, now.month)[1]
    cap = stop // days
    used = next((value for date, value in rows if date == day), 0)
    tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), now.tzinfo)
    remaining = max(0, min(cap - used, stop - charged))
    return {'day': day, 'timezone': str(now.tzinfo), 'days_in_month': days,
            'daily_limit_bytes': cap, 'daily_charged_bytes': used,
            'daily_remaining_bytes': remaining, 'daily_resets_at': tomorrow.isoformat(),
            'daily_reset_seconds': max(1, tomorrow.timestamp() - now.timestamp()),
            'historical_unallocated_bytes': legacy,
            'daily_history': [{'day': date, 'charged_bytes': value} for date, value in rows]}


def charge(db, day, amount):
    db.execute('INSERT INTO daily_usage VALUES (?,?) ON CONFLICT(day) DO UPDATE SET charged=charged+excluded.charged',
               (day, amount))
