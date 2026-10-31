"""Account-isolated match history and factual play counts."""
import asyncio
import hashlib
import json
import time
from pathlib import Path

LANES = {'TOP':'TOP', 'JUNGLE':'JUNGLE', 'MIDDLE':'MID', 'MID':'MID',
         'BOTTOM':'ADC', 'ADC':'ADC', 'UTILITY':'SUPPORT', 'SUPPORT':'SUPPORT'}


def normalize_game(game, puuid):
    identities = game.get('participantIdentities') or []
    pid = next((p.get('participantId') for p in identities if (p.get('player') or {}).get('puuid') == puuid), None)
    player = next((p for p in game.get('participants') or [] if
                   p.get('puuid') == puuid or (pid is not None and p.get('participantId') == pid)), None)
    if not player or not game.get('gameId'):
        return None
    stats = player.get('stats') or player
    timeline = player.get('timeline') or {}
    lane = LANES.get(str(player.get('teamPosition') or player.get('individualPosition') or '').upper())
    if not lane:
        raw, role = str(timeline.get('lane') or '').upper(), str(timeline.get('role') or '').upper()
        if raw in ('BOTTOM', 'BOT'):
            lane = 'SUPPORT' if role in ('DUO_SUPPORT','SUPPORT') else 'ADC' if role in ('DUO_CARRY','CARRY') else None
        elif raw in ('TOP', 'MIDDLE', 'MID', 'JUNGLE'):
            lane = LANES.get(raw)
        elif role in ('SUPPORT','DUO_SUPPORT') and game.get('queueId')==420:
            lane = 'SUPPORT'  # Explicit solo/duo support role, even when lane is NONE.
    win = stats.get('win')
    win = bool(win) if isinstance(win, (bool,int)) and win in (0,1) else None
    return dict(key=str(game.get('platformId') or '')+':'+str(game['gameId']),
                gameId=game['gameId'], queue=game.get('queueId'), champion=player.get('championId',0),
                lane=lane or 'UNKNOWN', win=win, date=game.get('gameCreation',0),
                remake=bool(stats.get('gameEndedInEarlySurrender')) or
                       (0 < game.get('gameDuration',0) < 300))


def select_records(records, queues=(420, 440), lane=None, limit=None):
    """Newest unique games in the requested scope, including unknown outcomes."""
    result, seen = [], set()
    rows = sorted(records, key=lambda r: (r.get('date') or 0, r.get('gameId') or 0), reverse=True)
    for index, row in enumerate(rows):
        if row.get('queue') not in queues or (lane not in (None, 'ALL') and row.get('lane') != lane):
            continue
        key = row.get('key') or ((row.get('queue'), row['gameId']) if row.get('gameId') else ('index', index))
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
        if limit is not None and len(result) >= limit:
            break
    return result


def aggregate(records, lane=None, queues=(420, 440)):
    """Every played hero is visible; order by play count, then latest use."""
    counts = {}
    for row in select_records(records, queues, lane):
        if not row.get('champion'):
            continue
        entry = counts.setdefault(row['champion'], dict(championId=row['champion'], games=0,
            wins=0, losses=0, remakes=0, unknown=0, lastPlayed=row.get('date') or 0))
        entry['games'] += 1
        if row.get('remake'):
            entry['remakes'] += 1
        elif row.get('win') is True:
            entry['wins'] += 1
        elif row.get('win') is False:
            entry['losses'] += 1
        else:
            entry['unknown'] += 1
    return sorted(counts.values(), key=lambda r: (-r['games'], -r['lastPlayed'], r['championId']))


def recent_ranked(records, limit=20):
    """Newest ranked matches across all lanes; retain remakes/unknown outcomes explicitly."""
    return select_records(records, limit=limit)


class PersonalHistory:
    def __init__(self, connector, folder):
        self.connector, self.folder = connector, Path(folder)

    def path(self, puuid):
        return self.folder/(hashlib.sha256(puuid.encode()).hexdigest()+'.json')

    def cached(self, puuid):
        try:
            data = json.loads(self.path(puuid).read_text(encoding='utf-8'))
            return data if data.get('schema') == 1 and isinstance(data.get('records'),list) else {'records':[]}
        except (OSError,ValueError,AttributeError):
            return {'records':[]}

    async def refresh(self, puuid, limit=200):
        previous = self.cached(puuid)
        records = {r['key']:r for r in previous['records'] if isinstance(r,dict) and 'key' in r}
        read, warnings, seen, pending = 0, [], set(), []
        repeated = False
        def store(game):
            nonlocal read
            key = str(game.get('platformId',''))+':'+str(game.get('gameId'))
            if key not in seen:
                seen.add(key)
                read += 1
            row = normalize_game(game, puuid)
            if not row:
                warnings.append('部分场次无法确认本人，已跳过。')
                return None
            old = records.get(row['key'])
            if old and row['lane'] == 'UNKNOWN' and old['lane'] != 'UNKNOWN':
                row['lane'] = old['lane']
            records[row['key']] = row
            if row['queue'] in (420, 440) and row['lane'] == 'UNKNOWN' and len(pending) < 20:
                pending.append(row)
            return row

        for start in range(0,limit,20):
            try:
                page = await asyncio.wait_for(self.connector.getSummonerGamesByPuuid(puuid,start,min(start+19,limit-1)),12)
                games = page.get('games',[]) if isinstance(page,dict) else page
                if not isinstance(games,list):
                    raise ValueError('战绩列表格式异常')
            except Exception:
                warnings.append('读取部分战绩失败，保留已有数据；可重新刷新。')
                break
            fresh = [g for g in games if str(g.get('platformId',''))+':'+str(g.get('gameId')) not in seen]
            if not fresh:
                if games:
                    repeated = True
                    warnings.append(f'客户端重复返回近期 {len(games)} 场，已停止分页；后续刷新可累计新战绩。')
                break
            for game in fresh:
                store(game)
            if len(games)<20:
                break

        # Some clients ignore page indexes and repeat their last 20 games. Reuse
        # the authenticated official history adapter already provided by Connector.
        official = getattr(self.connector, 'getSummonerGamesByPuuidViaSGP', None)
        supplemented = False
        if callable(official) and getattr(self.connector, 'inTencent', True) and (repeated or len(recent_ranked(list(records.values()))) < 20):
            official_seen, official_ranked = set(), set()
            for start in range(0, limit, 50):
                try:
                    page = await asyncio.wait_for(official(puuid, start, min(start + 49, limit - 1)), 12)
                    games = page.get('games', []) if isinstance(page, dict) else page
                    if not isinstance(games, list):
                        raise ValueError('官方历史列表格式异常')
                    added = 0
                    for entry in games:
                        game = entry.get('json', entry)
                        if not isinstance(game, dict) or not game.get('gameId'):
                            continue
                        key = str(game.get('platformId', '')) + ':' + str(game['gameId'])
                        if key in official_seen:
                            continue
                        official_seen.add(key)
                        added += 1
                        row = store(game)
                        if row:
                            supplemented = True
                            if row['queue'] in (420, 440):
                                official_ranked.add(key)
                    if len(official_ranked) >= 20 or not added or len(games) < min(50, limit - start):
                        break
                except Exception:
                    warnings.append('较早的排位暂未读到，已保留客户端战绩和缓存。')
                    break
            if supplemented:
                warnings = [warning for warning in warnings if not warning.startswith('客户端重复返回近期')]

        semaphore = asyncio.Semaphore(4)
        async def enrich(row):
            try:
                async with semaphore:
                    game = await asyncio.wait_for(self.connector.getGameDetailByGameId(row['gameId']),5)
                detail = normalize_game(game,puuid)
                if detail and detail['lane']!='UNKNOWN':
                    records[row['key']] = dict(detail,key=row['key'])
            except Exception:
                pass  # Unknown lanes remain explicit; never guess from the hero.
        await asyncio.gather(*(enrich(row) for row in pending if records[row['key']]['lane'] == 'UNKNOWN'))
        result = dict(schema=1,records=sorted(records.values(),key=lambda r:r.get('date',0),reverse=True)[:2000],
                      updated=time.time(),read=read,supplemented=supplemented,warnings=list(dict.fromkeys(warnings)))
        self.folder.mkdir(parents=True,exist_ok=True)
        temporary = self.path(puuid).with_suffix('.tmp')
        temporary.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
        temporary.replace(self.path(puuid))
        return result
