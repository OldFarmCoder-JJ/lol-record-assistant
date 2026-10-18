"""Account-isolated, local-only aggregation of LCU solo/duo history."""
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


def aggregate(records, lane, minimum=5):
    counts = {}
    for row in records:
        if row.get('queue') != 420 or row.get('remake') or row.get('win') is None or row.get('lane') != lane or not row.get('champion'):
            continue
        entry = counts.setdefault(row['champion'], dict(championId=row['champion'],games=0,wins=0))
        entry['games'] += 1
        entry['wins'] += int(row['win'])
    rows = [dict(r,losses=r['games']-r['wins'],winRate=r['wins']/r['games']) for r in counts.values() if r['games'] >= minimum]
    return sorted(rows,key=lambda r:(-r['winRate'],-r['games'],r['championId']))


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
                    warnings.append(f'客户端重复返回近期 {len(games)} 场，已停止分页；后续刷新可累计新战绩。')
                break
            for game in fresh:
                key = str(game.get('platformId',''))+':'+str(game.get('gameId'))
                seen.add(key)
                read += 1
                row = normalize_game(game,puuid)
                if row:
                    old = records.get(row['key'])
                    if old and row['lane']=='UNKNOWN' and old['lane']!='UNKNOWN':
                        row['lane'] = old['lane']
                    records[row['key']] = row
                    if row['queue']==420 and row['lane']=='UNKNOWN' and len(pending)<20:
                        pending.append(row)
                else:
                    warnings.append('部分场次无法确认本人，已跳过。')
            if len(games)<20:
                break
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
        await asyncio.gather(*(enrich(row) for row in pending))
        result = dict(schema=1,records=sorted(records.values(),key=lambda r:r.get('date',0),reverse=True)[:2000],
                      updated=time.time(),read=read,warnings=list(dict.fromkeys(warnings)))
        self.folder.mkdir(parents=True,exist_ok=True)
        temporary = self.path(puuid).with_suffix('.tmp')
        temporary.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
        temporary.replace(self.path(puuid))
        return result
