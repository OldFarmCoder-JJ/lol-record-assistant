"""Ranking snapshot identity, restart/failure safety, public-only payloads and bounds."""
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'Seraphine'))
from app.lol.ranking_cache import RankingCache


class Clock:
    now = 10000.0
    def __call__(self): return self.now


def hero(champion=11, mayhem=False):
    return dict(championId=champion, name='无极剑圣', rank=1, tier=2,
                winRate=.5123, pickRate=.1, banRate=None,
                icon='C:/cache/英雄头像.png', mayhem=mayhem, counters=[])


def native():
    return dict(version='16.21', data={lane: [hero()] for lane in ('TOP', 'JUNGLE', 'MID', 'ADC', 'SUPPORT')})


def analysis(**kwargs):
    return dict(version='16.21', source='rift-analysis', region=kwargs.get('region', 'cn'),
                position='MID', data=[hero()], legacyRows=[hero()], dates={'cn': '20261030'},
                enemyId=0, fraction=.3, onlyStrong=True, strict=False)


def run():
    folder = Path(tempfile.mkdtemp(prefix='Seraphine-ranking-cache-'))
    clock = Clock()
    cache = RankingCache(folder, max_entries=32, max_age=200, fresh_for=20, clock=clock)
    key = cache.make_key('ranked', 'kr', 'emerald_plus', 'MID')
    assert cache.get(key) is None
    payload = native()
    assert cache.put(key, payload)
    assert cache.get(key) == dict(payload=payload, storedAt=10000, age=0, stale=False)
    assert RankingCache(folder, max_age=200, fresh_for=20, clock=clock).get(key)['payload'] == payload
    payload['data']['MID'][0]['name'] = 'changed-after-put'
    fetched = cache.get(key)
    fetched['payload']['data']['MID'][0]['name'] = 'changed-after-get'
    assert cache.get(key)['payload']['data']['MID'][0]['name'] == '无极剑圣'
    # Every individual filter has its own identity; there is no nearest-key fallback.
    variants = [
        cache.make_key('aram_mayhem', 'kr', 'emerald_plus', 'MID'),
        cache.make_key('ranked', 'global', 'emerald_plus', 'MID'),
        cache.make_key('ranked', 'cn', 'emerald_plus', 'MID'),
        cache.make_key('ranked', 'consensus', 'emerald_plus', 'MID'),
        cache.make_key('ranked', 'kr', 'all', 'MID'),
        cache.make_key('ranked', 'kr', 'emerald_plus', 'TOP'),
        cache.make_key('ranked', 'kr', 'emerald_plus', 'MID', fraction=.2),
        cache.make_key('ranked', 'kr', 'emerald_plus', 'MID', only_strong=False),
        cache.make_key('ranked', 'kr', 'emerald_plus', 'MID', strict=True),
    ]
    assert all(cache.get(other) is None for other in variants)
    mayhem_key = cache.make_key('aram_mayhem', 'cn', 'all', 'none')
    mayhem = dict(version='16.21', statDate='20261030', source='tencent-101', data=[hero(mayhem=True)])
    assert cache.put(mayhem_key, mayhem) and cache.get(mayhem_key)['payload'] == mayhem
    assert not cache.put(mayhem_key, native())
    assert not cache.put(key, mayhem)
    cn = cache.make_key('ranked', 'cn', 'all', 'MID')
    assert cache.put(cn, analysis())
    assert not cache.put(cn, analysis(region='kr'))
    assert not cache.put(cache.make_key('ranked', 'cn', 'all', 'TOP'), analysis())
    assert not cache.put(cache.make_key('ranked', 'cn', 'all', 'MID', strict=True), analysis())
    specific = analysis()
    specific['enemyId'] = 64
    assert not cache.put(cn, specific)
    for bad in ({'records': []}, dict(version='16.21', data=[]), dict(version='16.21', data=[dict(championId=True)])):
        assert not cache.put(key, bad)
    for private_key in ('puuid', 'accessToken', 'Authorization', 'account_id', 'cookie'):
        bad = native()
        bad['data']['MID'][0][private_key] = 'must-not-save'
        assert not cache.put(key, bad)
    for rate in (float('nan'), float('inf'), '0.5', True, 1.01):
        bad = native()
        bad['data']['MID'][0]['winRate'] = rate
        assert not cache.put(key, bad)
    assert cache.get(key)['payload'] == native(), 'Invalid saves must preserve the previous good snapshot'
    # Clock and retention metadata are explicit: stale can be displayed, expired cannot.
    clock.now += 21
    assert cache.get(key)['stale'] and cache.get(key)['age'] == 21
    clock.now += 180
    assert cache.get(key) is None
    assert not cache.put(key, native(), fetched_at=10000)
    assert not cache.put(key, native(), fetched_at=float('nan'))
    assert not cache.put(key, native(), fetched_at=True)
    assert not cache.put(key, native(), fetched_at=clock.now + 61)

    # Recognized malformed snapshots are ignored, without ever showing a different query.
    assert cache.put(key, native())
    path = cache._path(key)
    good = path.read_bytes()
    for malformed in (b'{', b'[]', b'{"schema":2}', json.dumps(dict(schema=1, key=list(variants[1]),
                      storedAt=clock.now, payload=native())).encode()):
        path.write_bytes(malformed)
        assert cache.get(key) is None
    path.write_bytes(good)
    assert cache.get(key)
    record = json.loads(good)
    record['storedAt'] = clock.now + 100
    path.write_text(json.dumps(record))
    assert cache.get(key) is None
    path.write_bytes(good)
    limited = RankingCache(folder, max_entry_bytes=1024, clock=clock)
    huge = native()
    huge['notice'] = 'x' * 2048
    assert not limited.put(key, huge)
    assert cache.get(key)['payload'] == native()
    path.write_bytes(b' ' * (cache.max_entry_bytes + 1))
    assert cache.get(key) is None
    path.write_bytes(good)

    # Failed atomic replacement does not lose the prior entry or leave a temp file.
    with patch('app.lol.ranking_cache.os.replace', side_effect=OSError('disk unavailable')):
        assert not cache.put(key, dict(native(), version='new'))
    assert cache.get(key)['payload'] == native()
    assert not list(folder.glob('*.tmp'))
    blocked = folder / 'not-a-folder'
    blocked.write_text('preserve')
    assert not RankingCache(blocked, clock=clock).put(key, native())
    assert blocked.read_text() == 'preserve'

    bounded_folder = folder / 'bounded'
    bounded = RankingCache(bounded_folder, max_entries=3, max_age=200, clock=clock)
    bounded_folder.mkdir()
    unrelated = bounded_folder / 'my-notes.json'
    unrelated.write_text('preserve')
    keys = []
    for tier in ('all', 'gold_plus', 'platinum_plus', 'emerald_plus', 'diamond_plus'):
        clock.now += 1
        item = bounded.make_key('ranked', 'kr', tier, 'MID')
        keys.append(item)
        assert bounded.put(item, native())
    assert bounded.get(keys[0]) is None and bounded.get(keys[-1])
    assert len([file for file in bounded_folder.glob('*.json') if file.name != 'my-notes.json']) == 3
    assert unrelated.read_text() == 'preserve'
    clock.now += 201
    assert bounded.put(keys[-1], native())
    assert len([file for file in bounded_folder.glob('*.json') if file.name != 'my-notes.json']) == 1
    for invalid in (None, (), ('ranked',), ['ranked', 'kr', '../tier', 'MID', .3, True, False]):
        assert bounded.get(invalid) is None and not bounded.put(invalid, native())
    print('PASS: exact mode/source/tier/lane/rule isolation, JSON restart/deep-copy snapshots, source metadata,')
    print('Rift/Mayhem separation, public-only validation, stale/expiry/future clocks, corrupt/oversized files,')
    print('atomic-write failure safety, entry/retention bounds and unrelated-file preservation.')


if __name__ == '__main__':
    run()
