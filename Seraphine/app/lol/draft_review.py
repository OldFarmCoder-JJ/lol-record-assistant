"""Local, account-scoped draft snapshots joined only by confirmed game and queue ID."""
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path


class DraftReview:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.path = None
        self.last = None

    def set_account(self, puuid):
        path = self.folder / (hashlib.sha256(puuid.encode()).hexdigest() + '.db') if puuid else None
        if path == self.path:
            return
        self.path, self.last = path, None
        if path:
            self.folder.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("""CREATE TABLE IF NOT EXISTS review (
                    game INTEGER PRIMARY KEY, queue INTEGER NOT NULL, stamp INTEGER NOT NULL,
                    lane TEXT NOT NULL, version TEXT NOT NULL, picks TEXT NOT NULL,
                    locked INTEGER, played INTEGER, result TEXT, followed INTEGER)""")

    def observe(self, game, queue, lane, version, picks, locked=0):
        if not self.path or not game or not queue:
            return
        # Store exactly the order presented to the user. A lock never replaces
        # this list, including when startup/reconnect first sees an existing lock.
        encoded = json.dumps(picks, ensure_ascii=False, separators=(',', ':'))
        key = (game, queue, lane, version, encoded, locked)
        if key == self.last:
            return
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""INSERT INTO review(game,queue,stamp,lane,version,picks,locked)
                       VALUES(?,?,?,?,?,?,?) ON CONFLICT(game) DO UPDATE SET
                       picks=CASE WHEN excluded.locked IS NULL THEN excluded.picks ELSE review.picks END,
                       lane=CASE WHEN excluded.locked IS NULL THEN excluded.lane ELSE review.lane END,
                       locked=excluded.locked
                       WHERE review.locked IS NULL AND review.result IS NULL AND review.queue=excluded.queue""",
                       (game, queue, int(time.time()*1000), lane, version, encoded, locked or None))
        self.last = key

    def mark(self, game, followed):
        if self.path:
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute('UPDATE review SET followed=? WHERE game=?', (followed, game))

    def pending(self):
        if not self.path:
            return []
        with closing(sqlite3.connect(self.path)) as db:
            return [r[0] for r in db.execute("""SELECT game FROM review WHERE result IS NULL
                ORDER BY stamp DESC LIMIT 12""")]

    def reconcile(self, records):
        if not self.path or not records:
            return
        updates = []
        for row in records:
            hero = row.get('champion')
            if not hero or not row.get('gameId'):
                continue
            result = '重开' if row.get('remake') else {True:'胜', False:'负'}.get(row.get('win'))
            if result:
                updates.append((hero, result, row['gameId'], row['queue']))
        if updates:
            with closing(sqlite3.connect(self.path)) as db, db:
                db.executemany("""UPDATE review SET played=?, result=? WHERE game=? AND queue=?
                                  AND result IS NULL""", updates)

    def rows(self):
        if not self.path:
            return []
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute('SELECT * FROM review ORDER BY stamp DESC, game DESC')]
        for r in rows:
            r['picks'] = json.loads(r['picks'])
            hero = r['played'] or r['locked']
            ids = [p[0] for p in r['picks']]
            r['rank'] = (ids.index(hero)+1 if hero in ids else 0) if hero and ids else None
        return rows


def group_summary(rows):
    def summary(group):
        wins = sum(r['result']=='胜' for r in group)
        losses = sum(r['result']=='负' for r in group)
        rate = f'{wins/(wins+losses):.0%}' if wins+losses else '—'
        return f'{wins}胜 {losses}负（{rate}）'
    valid = [r for r in rows if r['result'] in ('胜','负') and r['rank'] is not None]
    text = '   ·   '.join(label + ' ' + summary(group) for label, group in (
        ('推荐内', [r for r in valid if r['rank']>0]),
        ('其中前三', [r for r in valid if 1<=r['rank']<=3]),
        ('榜外', [r for r in valid if r['rank']==0])))
    marked = [r for r in rows if r['followed'] is not None and r['result'] in ('胜', '负')]
    if marked:
        text += '\n本人标记参考 ' + summary([r for r in marked if r['followed']])
        text += '   ·   本人标记未参考 ' + summary([r for r in marked if not r['followed']])
    return text
