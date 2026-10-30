"""Small public-ranking snapshots, isolated by every query filter.

This stores only JSON ranking payloads, never client sessions or personal history.
Cache failures are intentionally non-fatal: live data must still render normally.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time


_LANES = {'TOP', 'JUNGLE', 'MID', 'ADC', 'SUPPORT', 'none'}
_PRIVATE_KEYS = {'puuid', 'summonerid', 'accountid', 'playerid', 'token', 'accesstoken',
                 'refreshtoken', 'sgptoken', 'password', 'secret', 'authorization', 'cookie'}
_CACHE_FILE = re.compile(r'[0-9a-f]{64}\.json\Z')


class RankingCache:
    def __init__(self, folder, *, max_entries=32, max_age=7 * 86400,
                 fresh_for=900, max_entry_bytes=2 * 1024 * 1024, clock=time.time):
        self.folder = Path(folder)
        self.max_entries = max(1, int(max_entries))
        self.max_age = max(1, float(max_age))
        self.fresh_for = max(0, min(float(fresh_for), self.max_age))
        self.max_entry_bytes = max(256, int(max_entry_bytes))
        self.clock = clock

    @staticmethod
    def make_key(mode, region, tier, lane, *, fraction=.30, only_strong=True, strict=False):
        """Use effective request filters; no automatic mode/source/lane fallback."""
        if mode not in ('ranked', 'aram_mayhem') or region not in ('cn', 'global', 'kr', 'consensus'):
            raise ValueError('Unsupported ranking mode or source')
        if not isinstance(tier, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,64}', tier) or lane not in _LANES:
            raise ValueError('Invalid ranking tier or lane')
        if not isinstance(fraction, (int, float)) or isinstance(fraction, bool) or not 0 < fraction <= 1:
            raise ValueError('Invalid ranking fraction')
        if type(only_strong) is not bool or type(strict) is not bool:
            raise ValueError('Invalid ranking rules')
        return mode, region, tier, lane, float(fraction), only_strong, strict

    def _key(self, key):
        if not isinstance(key, (list, tuple)) or len(key) != 7:
            raise ValueError('Invalid cache key')
        return self.make_key(*key[:4], fraction=key[4], only_strong=key[5], strict=key[6])

    def _path(self, key):
        encoded = json.dumps(self._key(key), separators=(',', ':'), ensure_ascii=True).encode()
        return self.folder / (hashlib.sha256(encoded).hexdigest() + '.json')

    @staticmethod
    def _public_json(value, depth=0):
        if depth > 20:
            return False
        if value is None or type(value) in (str, int, bool):
            return True
        if type(value) is float:
            return math.isfinite(value)
        if isinstance(value, list):
            return all(RankingCache._public_json(item, depth + 1) for item in value)
        if isinstance(value, dict):
            return all(isinstance(key, str)
                       and re.sub(r'[-_]', '', key).lower() not in _PRIVATE_KEYS
                       and RankingCache._public_json(item, depth + 1)
                       for key, item in value.items())
        return False

    @classmethod
    def _payload_valid(cls, key, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get('version'), str) or not payload['version']:
            return False
        if not cls._public_json(payload):
            return False
        data = payload.get('data')
        if isinstance(data, dict):
            rows = data.get(key[3])
        else:
            rows = data
        if not isinstance(rows, list) or not all(isinstance(row, dict) and
                type(row.get('championId')) is int and row['championId'] > 0 for row in rows):
            return False
        for row in rows:
            for field in ('winRate', 'pickRate', 'banRate'):
                value = row.get(field)
                if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1):
                    return False
        if key[0] == 'aram_mayhem':
            if not isinstance(data, list) or any(row.get('mayhem') is not True for row in rows):
                return False
        else:
            if any(row.get('mayhem') for row in rows):
                return False
            if not isinstance(data, dict) and payload.get('source') != 'rift-analysis':
                return False
        if payload.get('source') == 'rift-analysis':
            # This cache is for base rankings. An enemy-specific result needs its
            # own identity; never reuse it as the ordinary ranking.
            if key[0] != 'ranked' or payload.get('enemyId', 0):
                return False
            for field, expected in (('region', key[1]), ('position', key[3]),
                                    ('fraction', key[4]), ('onlyStrong', key[5]), ('strict', key[6])):
                if field in payload and payload[field] != expected:
                    return False
        return True

    def _decode(self, path, expected_key=None):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > self.max_entry_bytes:
            return None
        record = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(record, dict) or record.get('schema') != 1:
            return None
        key = self._key(record.get('key'))
        if expected_key is not None and key != expected_key:
            return None
        if path.name != self._path(key).name or not self._payload_valid(key, record.get('payload')):
            return None
        stored = record.get('storedAt')
        if type(stored) not in (int, float) or not math.isfinite(stored):
            return None
        now = self.clock()
        if stored < 0 or stored > now + 60 or now - stored > self.max_age:
            return None
        return record

    def get(self, key):
        """Return a detached payload plus local cache age, or None when unusable."""
        try:
            key = self._key(key)
            record = self._decode(self._path(key), key)
            if record is None:
                return None
            age = max(0, self.clock() - record['storedAt'])
            return dict(payload=record['payload'], storedAt=record['storedAt'],
                        age=age, stale=age > self.fresh_for)
        except (OSError, ValueError, TypeError, RecursionError):
            return None

    def put(self, key, payload, *, fetched_at=None):
        """Save a successful response atomically; False never blocks live rendering."""
        temporary = None
        try:
            key = self._key(key)
            if not self._payload_valid(key, payload):
                return False
            stored = self.clock() if fetched_at is None else fetched_at
            if (type(stored) not in (int, float) or not math.isfinite(stored) or stored < 0
                    or stored > self.clock() + 60 or self.clock() - stored > self.max_age):
                return False
            body = json.dumps(dict(schema=1, key=key, storedAt=stored, payload=payload),
                              ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
            if len(body) > self.max_entry_bytes:
                return False
            self.folder.mkdir(parents=True, exist_ok=True)
            path = self._path(key)
            if path.is_symlink():
                return False
            with tempfile.NamedTemporaryFile(dir=self.folder, prefix=path.stem + '-', suffix='.tmp', delete=False) as file:
                temporary = Path(file.name)
                file.write(body)
            os.replace(temporary, path)
            temporary = None
            self._prune()
            return True
        except (OSError, ValueError, TypeError, RecursionError):
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _prune(self):
        valid = []
        for path in self.folder.iterdir():
            if not _CACHE_FILE.fullmatch(path.name) or path.is_symlink():
                continue
            try:
                record = self._decode(path)
                if record is None:
                    path.unlink(missing_ok=True)
                else:
                    valid.append((record['storedAt'], path.stat().st_mtime_ns, path))
            except (OSError, ValueError, TypeError, RecursionError):
                # Corrupt recognized cache snapshots are safe to discard; never
                # touch unrelated filenames or in-flight temporary writes.
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        for _, _, path in sorted(valid, key=lambda item: (item[0], item[1], item[2].name), reverse=True)[self.max_entries:]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
