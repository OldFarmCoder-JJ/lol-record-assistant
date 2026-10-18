"""National Rift statistics and comparisons with explicit per-source evidence."""
import asyncio
import math

from app.lol.tencent_hextech import API, payload, HextechDataError

LANES = {'TOP': 'TOP', 'JUNGLE': 'JUNGLE', 'MID': 'MIDDLE', 'ADC': 'BOTTOM', 'SUPPORT': 'SUPPORT'}
REVERSE_LANES = {value: key for key, value in LANES.items()}
TIERS = {'all': 255, 'gold_plus': 24, 'platinum_plus': 25, 'emerald_plus': 26,
         'diamond_plus': 27, 'master': 8, 'master_plus': 28, 'grandmaster': 9, 'challenger': 10}
SOURCES = ('cn', 'global', 'kr')
SOURCE_NAMES = {'cn': '国服', 'global': '全球', 'kr': '韩服'}


def api_tier(tier):
    if tier not in TIERS:
        raise HextechDataError('国服接口不支持“黄金以下”组合，请选择全部或黄金以上等段位。')
    return TIERS[tier]


def parse_national_rank(response):
    raw = payload(response)
    result = {lane: [] for lane in LANES}
    for row in raw.get('datadetails', '').split('#'):
        fields = row.split('_')
        if len(fields) < 8 or fields[3] not in REVERSE_LANES:
            continue
        position = REVERSE_LANES[fields[3]]
        result[position].append({
            'championId': int(fields[1]), 'rank': int(fields[0]), 'tier': int(fields[2][1:]),
            'position': position, 'winRate': float(fields[4]) / 100,
            'pickRate': float(fields[5]) / 100, 'banRate': float(fields[6]) / 100,
            'counterIds': [int(id) for id in fields[7].split(',') if id.isdigit()],
            'play': None, 'source': 'cn', 'tierText': fields[2],
        })
    for rows in result.values():
        rows.sort(key=lambda row: row['rank'])
    return result, str(raw.get('dtstatdate') or '')


def parse_overseas_rank(response, source):
    result = {lane: [] for lane in LANES}
    for champion in response['data']:
        for position in champion.get('positions') or []:
            lane = position['name']
            stats = position.get('stats') or {}
            tier = stats.get('tier_data') or {}
            if lane not in result or tier.get('rank') is None or stats.get('win_rate') is None:
                continue
            result[lane].append({
                'championId': champion['id'], 'position': lane, 'rank': tier['rank'],
                'tier': tier.get('tier'), 'winRate': stats['win_rate'],
                'pickRate': stats.get('pick_rate'), 'banRate': stats.get('ban_rate'),
                'play': stats.get('play'), 'source': source,
                'tierText': 'OP' if tier.get('tier') == 0 else 'T' + str(tier.get('tier')),
            })
    for rows in result.values():
        rows.sort(key=lambda row: row['rank'])
    return result


def analyze_rows(source_lists, position, fraction=.30, only_strong=True, matchups=None,
                 unavailable=(), require_all_matchups=False, excluded=()):
    """Worst-source rank fraction orders results; no averaging overlapping servers."""
    maps = {source: {row['championId']: row for row in lanes[position]}
            for source, lanes in source_lists.items()}
    candidates = set.intersection(*(set(rows) for rows in maps.values())) if maps else set()
    rows = []
    for champion_id in candidates - set(excluded):
        evidence = {source: source_map[champion_id] for source, source_map in maps.items()}
        strong = all(row['rank'] <= max(1, math.ceil(len(maps[source]) * fraction))
                     and row['winRate'] >= .5
                     and (row['play'] is None or row['play'] >= 1000)
                     for source, row in evidence.items())
        if only_strong and not strong:
            continue
        matchup = {source: source_rows[champion_id] for source, source_rows in (matchups or {}).items()
                   if champion_id in source_rows}
        supported = {source: item for source, item in matchup.items()
                     if item['winRate'] > .5 and (item['play'] is None or item['play'] >= 200)
                     and (item.get('advantage') is None or item['advantage'] > 0)}
        if matchups is not None:
            if require_all_matchups and (unavailable or len(supported) != len(maps)):
                continue
            if not require_all_matchups and not supported:
                continue
        score = max(row['rank'] / len(maps[source]) for source, row in evidence.items())
        rows.append({'championId': champion_id, 'position': position, 'evidence': evidence,
                     'commonStrong': strong, 'worstRankFraction': score,
                     'matchups': matchup, 'supported': list(supported)})
    rows.sort(key=lambda row: (-len(row['supported']),
                              -min((row['matchups'][s]['winRate'] for s in row['supported']), default=0),
                              row['worstRankFraction'], row['championId']))
    return rows


def parse_national_counters(response):
    raw = payload(response)
    result = {}
    for field in ('low_op_details', 'high_op_details'):
        for row in raw.get(field, '').split('#'):
            parts = row.split('_')
            if len(parts) < 3 or not parts[1].isdigit():
                continue
            # Query hero is the ENEMY: invert its win rate and adjusted advantage.
            result[int(parts[1])] = {'winRate': 1 - float(parts[2]) / 100, 'play': None,
                                     'advantage': -float(parts[3]) if len(parts) > 3 else None}
    return result, str(raw.get('dtstatdate') or '')


def parse_overseas_counters(response, enemy_id, position, expected_version):
    if response.get('meta', {}).get('version') != expected_version:
        raise HextechDataError('海外对位版本与排行不一致，请稍后重试。')
    data = response.get('data') or {}
    summary = data.get('summary') or {}
    if summary.get('id') != enemy_id or not any(p['name'] == position for p in summary.get('positions') or []):
        raise HextechDataError('对方英雄没有该分路的有效对位数据。')
    return {row['champion_id']: {'winRate': 1 - row['win'] / row['play'],
                                 'play': row['play'], 'advantage': None}
            for row in data.get('counters') or [] if row.get('play', 0) > 0}


def bp_snapshot(session):
    """Never infer enemy lanes from cell order; expose only API-confirmed picks."""
    team = session.get('theirTeam') or []
    cells = {player['cellId'] for player in team if 'cellId' in player}
    actions = [action for group in session.get('actions') or [] for action in group]
    completed = {a.get('championId') for a in actions if a.get('type') == 'pick'
                 and a.get('completed') and a.get('actorCellId') in cells}
    opponents = sorted({p['championId'] for p in team if p.get('championId', 0) > 0
                        and (not actions or p['championId'] in completed)})
    excluded = {p['championId'] for p in (session.get('myTeam') or []) + team if p.get('championId', 0) > 0}
    for group in (session.get('bans') or {}).values():
        if isinstance(group, list):
            excluded.update(id for id in group if isinstance(id, int) and id > 0)
    excluded.update(a['championId'] for a in actions if a.get('type') == 'ban'
                    and a.get('completed') and a.get('championId', 0) > 0)
    return opponents, excluded


class TencentRift:
    def __init__(self, public):
        self.public = public

    async def request(self, endpoint, tier, version, position='ALL', champion_id=None):
        params = {'itier': api_tier(tier), 'version_id': version, 'lane': LANES.get(position, position)}
        if champion_id is not None:
            params['championid'] = champion_id
        if endpoint == 'lol_101strategy':
            params.update(sort_metric=1, sort_order=2)
        return await self.public.get_json(API + endpoint, tuple(sorted(params.items())))

    async def rank(self, tier, version=None):
        versions = [version] if version else [v['name'] for v in
            (await self.public.get_json('https://mlol.qt.qq.com/go/database/versionlist',
                                       (('from', 'h5'), ('zone', 'lol'))))['data'][:2]]
        for patch in versions:
            try:
                rows, date = parse_national_rank(await self.request('lol_101strategy', tier, patch))
            except HextechDataError:
                # New patches may be listed before Tencent publishes their statistics.
                # An explicit comparison version must never silently change.
                if version:
                    raise
                continue
            if any(rows.values()):
                return {'data': rows, 'version': patch, 'statDate': date, 'source': 'tencent-rift'}
        raise HextechDataError('腾讯暂无所选版本/段位的峡谷排行，无法进行同版本比较。')

    async def counters(self, tier, version, enemy_id, position):
        # Tencent officially groups Challenger/Grandmaster matchup samples as Master+.
        query_tier = 'master_plus' if tier in ('challenger', 'grandmaster') else tier
        response = await self.request('lol_101strategy_confront', query_tier, version, position, enemy_id)
        rows, date = parse_national_counters(response)
        return rows, date, query_tier

    async def build(self, tier, champion_id, position, version=None):
        ranks = await self.rank(tier, version)
        row = next((row for row in ranks['data'][position] if row['championId'] == champion_id), None)
        if row is None:
            raise HextechDataError('腾讯暂无该英雄在当前分路的攻略，请选择它的实际分路。')
        endpoints = ('build', 'skill_point', 'skill', 'runeinfo')
        responses = await asyncio.gather(*(self.request('lol_101strategy_' + endpoint,
            tier, ranks['version'], position, champion_id) for endpoint in endpoints))
        raw_items, raw_skills, raw_spells, raw_runes = map(payload, responses)
        sections = []
        for field, title in (('starting', '出门装'), ('shoes', '鞋子'), ('core', '核心三件套'),
                             ('forth', '第四件'), ('fifth', '第五件'), ('sixth', '第六件')):
            items = []
            for encoded in raw_items.get(field + '_details', '').split('#'):
                parts = encoded.split('_')
                if len(parts) >= 4:
                    items.append({'ids': [int(id) for id in parts[0].split(',') if id.isdigit()],
                                  'pickRate': float(parts[2]), 'winRate': float(parts[3])})
            sections.append({'title': title, 'rows': items[:3]})
        spells = []
        for encoded in raw_spells.get('data_details', '').split('#'):
            parts = encoded.split('_')
            if len(parts) >= 4:
                spells.append({'ids': [int(parts[0]), int(parts[1])],
                               'winRate': float(parts[2]), 'pickRate': float(parts[3])})
        spells.sort(key=lambda row: -row['pickRate'])
        skill_parts = raw_skills.get('detaildetails', '').split('$')[0].split('@')
        skill_order = skill_parts[1].split('_')[0] if len(skill_parts) > 1 else ''
        skill_order = [{'1': 'Q', '2': 'W', '3': 'E', '4': 'R'}.get(id, id) for id in skill_order.split(',') if id]
        skill_stats = None
        if len(skill_parts) > 1:
            priority = skill_parts[0].split(':')[0].split(',')
            detail = skill_parts[1].split('_')
            if len(detail) >= 3:
                skill_stats = {'masteries': [{'1':'Q','2':'W','3':'E'}.get(id,id) for id in priority],
                               'order': skill_order, 'winRate': float(detail[2])/100,
                               'pickRate': float(detail[1])/100, 'play': None}
        primary = {8005:8000, 8008:8000, 8021:8000, 8010:8000, 8112:8100, 8124:8100,
                   8128:8100, 9923:8100, 8214:8200, 8229:8200, 8230:8200,
                   8437:8400, 8439:8400, 8465:8400, 8351:8300, 8360:8300, 8369:8300}
        secondary = {'jm':8000, 'zj':8100, 'zz':8100, 'ws':8200, 'jj':8400, 'qd':8300}
        perks = []
        for encoded in raw_runes.get('rune_top_details', '').split('#'):
            parts = encoded.split('_')
            if len(parts) < 7 or not parts[1].isdigit():
                continue
            ids = [int(id) for id in parts[3].split(',') if id.isdigit()]
            main, sub = primary.get(int(parts[1])), secondary.get(parts[2])
            if not main or not sub or len(ids) != 9:
                continue
            perks.append({'rank': int(parts[0]), 'primaryId': main, 'secondaryId': sub,
                          'perks': ids, 'pickRate': float(parts[4]), 'winRate': float(parts[5]),
                          'play': int(parts[6])})
        perks.sort(key=lambda row: row['rank'])
        return {'data': {'nationalRift': True, 'championId': champion_id, 'summary': row,
                         'sections': sections, 'spells': spells[:3], 'skillOrder': skill_order,
                         'skillStats': skill_stats,
                         'perks': perks[:3], 'statDate': raw_items.get('dtstatdate') or ranks['statDate'],
                         'url': 'https://101.qq.com/#/hero-detail?heroId='+str(champion_id)+
                                '&mode=rift&lane='+LANES[position].lower()+'&tier='+str(api_tier(tier))+
                                '&version='+ranks['version']},
                'version': ranks['version'], 'statDate': ranks['statDate'], 'source': 'tencent-rift',
                'mode': 'ranked', 'fallback': False}
