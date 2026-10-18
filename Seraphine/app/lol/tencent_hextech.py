"""Public Tencent 101 Hextech endpoints and their champion-specific formats."""
import asyncio
import hashlib
import json
import math
import os
import time
from datetime import datetime, timedelta, timezone

import aiohttp
from async_lru import alru_cache

from app.common.config import LOCAL_PATH

API = 'https://mlol.qt.qq.com/go/battle_info/odp_proxy/'
AUGMENTS = 'https://game.gtimg.cn/images/lol/act/img/js/kiwi/kiwi_augments.json'
HEROES = 'https://game.gtimg.cn/images/lol/act/img/js/heroList/hero_list.js'
RARITIES = {'kSilver': 1, 'kGold': 4, 'kPrismatic': 8}
GRADES = {'S': 0, 'A': 1, 'B': 2, 'C': 3, 'D': 4}


class HextechDataError(ValueError):
    pass


def payload(response):
    if not isinstance(response, dict) or response.get('code', 0) != 0:
        raise HextechDataError('腾讯 101 接口返回错误。')
    data = response.get('data')
    if isinstance(data, dict) and data.get('_fieldValues'):
        data = next(iter(data['_fieldValues'].values()))
    if isinstance(data, dict) and 'result' in data:
        data = data['result']
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError as error:
            raise HextechDataError('腾讯 101 返回的数据格式无法解析。') from error
    if not isinstance(data, dict):
        raise HextechDataError('腾讯 101 暂无数据。')
    return data


def tier_number(rank, total):
    # Same thresholds and positive-number rounding as the official website.
    thresholds = []
    for ratio in (.04, .15, .30, .40):
        thresholds.append(max(thresholds[-1] if thresholds else 1,
                              math.floor(total * ratio + .5)))
    return next((i for i, cutoff in enumerate(thresholds) if rank <= cutoff), 4)


def parse_rank(response, heroes):
    raw = payload(response)
    rows = []
    for encoded in raw.get('listcollect', '').split('#'):
        fields = encoded.split('_')
        if len(fields) < 5:
            continue
        try:
            rows.append({'championId': int(fields[0]), 'rank': int(fields[1]),
                         'winRate': float(fields[3]), 'pickRate': float(fields[4])})
        except ValueError:
            continue
    rows.sort(key=lambda row: row['rank'])
    metadata = {int(hero['heroId']): hero for hero in heroes}
    for row in rows:
        row['tier'] = 4
        row['roles'] = [r.lower() for r in metadata.get(row['championId'], {}).get('roles', [])]
    for role in ('tank', 'fighter', 'mage', 'assassin', 'marksman', 'support'):
        group = [row for row in rows if role in row['roles']]
        for rank, row in enumerate(group, 1):
            row['tier'] = min(row['tier'], tier_number(rank, len(group)))
    for row in rows:
        if not row['roles']:
            row['tier'] = tier_number(row['rank'], len(rows))
    return rows, str(raw.get('dtstatdate') or '')


def parse_build(response, champion_id, definitions):
    raw = payload(response)
    by_id = {int(a['augmentID']): a for a in definitions}
    augments = {}
    # Prefer the explicit official grade list. Do not fabricate grades by rank.
    encoded = raw.get('augment_json_irank') or raw.get('augment_json') or ''
    for group in encoded.split('&'):
        group_key = group.partition(':')[0] if ':' in group else '255'
        body = group.partition(':')[2] if ':' in group else group
        for row in body.split('#'):
            fields = row.split('|')
            if len(fields) < 5:
                continue
            try:
                rank, hero_id, augment_id = map(int, fields[:3])
                pick_rate = float(fields[4])
            except ValueError:
                continue
            if hero_id != champion_id:
                raise HextechDataError('腾讯返回的强化所属英雄与当前选择不一致。')
            definition = by_id.get(augment_id)
            if not definition:
                continue
            grade = fields[5] if len(fields) > 5 else ''
            entry = {
                'id': augment_id, 'championId': hero_id, 'rank': rank,
                'name': definition.get('name_cn') or definition.get('name_en'),
                'rarity': RARITIES.get(definition.get('level')),
                'tier': GRADES.get(grade), 'popular': pick_rate * 100,
                'tooltip': definition.get('tooltip') or definition.get('desc') or '',
                'largeIcon': definition.get('large_Icon') or definition.get('small_Icon'),
            }
            previous = augments.get(augment_id)
            if previous is None:
                augments[augment_id] = entry
            elif group_key == '255':
                entry['rarityStats'] = previous.get('rarityStats', {})
                augments[augment_id] = entry
            if group_key in RARITIES:
                # Statistics may lag a static-definition rarity change by a patch.
                # Use the rarity of the actual dated champion statistics.
                augments[augment_id]['rarity'] = RARITIES[group_key]
                augments[augment_id].setdefault('rarityStats', {})[RARITIES[group_key]] = {
                    'tier': entry['tier'], 'rank': rank, 'popular': entry['popular']}
    # Valid empty hero responses should stay empty rather than use a global list.
    if not raw.get('dtstatdate') or not any(k in raw for k in ('augment_json_irank', 'augment_json', 'itemout')):
        raise HextechDataError('腾讯 101 暂无当前英雄的海克斯攻略。')

    def item_rows(key):
        result = []
        for row in raw.get(key, '').split('#'):
            fields = row.split('$')
            if len(fields) < 3:
                continue
            ids = [int(id) for id in fields[0].split(',') if id.isdigit()]
            if ids:
                result.append({'ids': ids, 'pickRate': float(fields[1]) * 100,
                               'winRate': float(fields[2]) * 100})
        return result

    def json_rows(key, id_key):
        value = raw.get(key) or {}
        if isinstance(value, str):
            value = json.loads(value)
        rows = value.values() if isinstance(value, dict) else value
        result = []
        for row in rows:
            ids = [int(id) for id in str(row.get(id_key, '')).replace(',', '&').split('&') if id.isdigit()]
            if ids:
                result.append({'ids': ids, 'pickRate': float(row.get('showrate', 0)) / 100,
                               'winRate': float(row.get('winrate', 0)) / 100})
        return sorted(result, key=lambda row: -row['winRate'])

    sections = {'starter_items': item_rows('itemout')[:3], 'boots': item_rows('itemshoes')[:3],
                'core_items': json_rows('itemcore_json', 'itemcore')[:3],
                'other_items': json_rows('itemone_json', 'itemone')[:6]}
    skills = raw.get('skill_json') or {}
    if isinstance(skills, str):
        skills = json.loads(skills)
    skill = next(iter(skills.values()), {}) if isinstance(skills, dict) else {}
    plans = skill.get('sks') or {}
    plan = next(iter(plans.values()), {}) if isinstance(plans, dict) else {}
    order = [{'1': 'Q', '2': 'W', '3': 'E', '4': 'R'}.get(id, id)
             for id in plan.get('sk', '').split('&') if id]
    recommendations = {key: [row['ids'] for row in rows] for key, rows in sections.items()}
    recommendations.update({'spells': [], 'skillOrder': order})
    return {'augments': list(augments.values()), 'recommendations': recommendations,
            'recommendationStats': sections, 'statDate': str(raw['dtstatdate'])}


class TencentHextech:
    def __init__(self, session):
        self.session = session

    @alru_cache(maxsize=256, ttl=900)
    async def get_json(self, url, params_key=()):
        key = hashlib.sha256(json.dumps([url, params_key]).encode()).hexdigest()
        folder = os.path.join(LOCAL_PATH, 'cache', 'tencent-hextech')
        path = os.path.join(folder, key + '.json')
        ttl = 86400 if url in (HEROES, AUGMENTS) else 900
        def read_cache():
            try:
                with open(path, encoding='utf-8') as file:
                    cached = json.load(file)
                if 0 <= time.time() - cached['saved'] < ttl:
                    return cached['data']
            except (OSError, ValueError, KeyError, TypeError):
                pass
        loop = asyncio.get_running_loop()
        cached = await loop.run_in_executor(None, read_cache)
        if cached is not None:
            return cached
        async with self.session.get(url, params=dict(params_key),
                timeout=aiohttp.ClientTimeout(total=20)) as response:
            response.raise_for_status()
            content = await response.read()
        data = await loop.run_in_executor(None, json.loads, content)
        if isinstance(data, dict) and ('code' in data) and (data['code'] != 0 or data.get('data') is None):
            raise HextechDataError(data.get('message') or '腾讯 101 暂无数据。')
        def write_cache():
            try:
                os.makedirs(folder, exist_ok=True)
                with open(path + '.tmp', 'w', encoding='utf-8') as file:
                    json.dump({'saved': time.time(), 'data': data}, file, ensure_ascii=False)
                os.replace(path + '.tmp', path)
            except OSError:
                pass
        await loop.run_in_executor(None, write_cache)
        return data

    async def rank(self):
        heroes = await self.get_json(HEROES)
        today = datetime.now(timezone(timedelta(hours=8))).date()
        for days in range(1, 4):
            date = (today - timedelta(days=days)).strftime('%Y%m%d')
            try:
                raw = await self.get_json(API + 'fuwen_aram_hero_rank_v2', (('dtstatdate', date),))
                rows, stat_date = parse_rank(raw, heroes['hero'])
                if rows:
                    return rows, stat_date or date
            except HextechDataError:
                continue
        raise HextechDataError('腾讯 101 最近三天暂无海克斯英雄排行，请稍后重试。')

    async def build(self, champion_id):
        raw, definitions = await asyncio.gather(
            self.get_json(API + 'fuwen_hero_rank', (('championid', champion_id),)),
            self.get_json(AUGMENTS))
        return parse_build(raw, champion_id, definitions)
