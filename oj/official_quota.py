"""Cached account-wide daily counts from official read-only history."""
from datetime import datetime, timedelta, timezone
import json
import re
import time

from .config import ROOT

BEIJING = timezone(timedelta(hours=8))


def policy():
    return json.loads((ROOT/'config/official_quota.json').read_text(encoding='utf-8'))


def today(now):
    return datetime.fromtimestamp(now, BEIJING).date().isoformat()


class OfficialQuota:
    def __init__(self, store):
        self.store = store
        with store.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS official_account_quota (
                account_id TEXT PRIMARY KEY, result TEXT NOT NULL DEFAULT '{}',
                last_attempt REAL NOT NULL DEFAULT 0, next_due REAL NOT NULL DEFAULT 0)''')

    def snapshot(self, account_id, ready=True):
        with self.store.db() as db:
            row = db.execute('SELECT * FROM official_account_quota WHERE account_id=?', (account_id,)).fetchone()
        now = time.time()
        value = json.loads(row['result']) if row else {}
        config = policy()
        result = dict(date=today(now), timezone=config['timezone'], scope='account',
                      limit=config['daily_limit'], limit_source=config['limit_source'],
                      estimated=True, source='official_history', used=None, remaining=None,
                      checked_at=None, reset_at=None, status='pending' if ready else 'login_required')
        result.update(value)
        if not ready:
            result.update(status='login_required', remaining=None, used=None)
        elif value.get('date') != today(now):
            result.update(date=today(now), status='pending', remaining=None, used=None, reset_at=None)
        elif result.get('status') == 'ready' and now - (result.get('checked_at') or 0) > 120:
            result['status'] = 'stale'
        if row and row['next_due'] == 0 and result['status'] == 'ready':
            result['status'] = 'stale'
        result['refresh_pending'] = ready and (not row or row['next_due'] <= now)
        result['last_attempt_at'] = row['last_attempt'] if row else None
        return result

    def due_account(self, accounts):
        now = time.time()
        with self.store.db() as db:
            rows = {row['account_id']: row for row in db.execute('SELECT * FROM official_account_quota')}
        candidates = [a for a in accounts if a['ready'] and (a['id'] not in rows or rows[a['id']]['next_due'] <= now)]
        return min(candidates, key=lambda a: rows[a['id']]['last_attempt'] if a['id'] in rows else 0)['id'] if candidates else None

    def request_refresh(self, account_id, invalidate=False):
        with self.store.db() as db:
            db.execute('INSERT OR IGNORE INTO official_account_quota(account_id) VALUES(?)', (account_id,))
            # Coalesce repeated UI refreshes; accepted uploads always invalidate.
            db.execute('UPDATE official_account_quota SET next_due=0 WHERE account_id=? AND (last_attempt<? OR ?)',
                       (account_id, time.time()-15, invalidate))

    def save(self, account_id, outcome):
        now = time.time()
        status = outcome.get('status', 'unavailable')
        with self.store.db() as db:
            row = db.execute('SELECT result FROM official_account_quota WHERE account_id=?', (account_id,)).fetchone()
            value = json.loads(row[0]) if row else {}
            if status == 'ready':
                quota = outcome.get('quota', {})
                used, limit = quota.get('used'), quota.get('limit')
                if (type(used) is not int or used < 0 or type(limit) is not int or limit < 1
                        or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(quota.get('date', '')))
                        or not isinstance(quota.get('checked_at'), (int, float))):
                    raise ValueError('Invalid official daily count')
                value = {k: quota[k] for k in ('date','used','limit','checked_at','reset_at','source','limit_source','timezone','scope','estimated')}
                value.update(remaining=max(0, limit-used), status='ready')
            else:
                value['status'] = status if status in ('login_required','profile_busy') else 'unavailable'
            delay = 60 if status == 'ready' else 15 if status == 'profile_busy' else 180
            db.execute('INSERT OR REPLACE INTO official_account_quota VALUES(?,?,?,?)',
                       (account_id, json.dumps(value, ensure_ascii=False), now, now+delay))
