import os
import asyncio
import hashlib
import json

import aiohttp
from async_lru import alru_cache

from app.lol.connector import connector, gameIconCachePath
from app.common.config import cfg, LOCAL_PATH
from app.lol.tencent_hextech import TencentHextech, AUGMENTS
from app.lol.rift_analysis import (TencentRift, SOURCES, SOURCE_NAMES, analyze_rows,
    parse_overseas_rank, parse_overseas_counters)

TAG = "opgg"

CLASSIC_RUNE_ICON_FOLDER = "app/resource/game/classic rune icons"
LEGACY_MASTERY_ICON_FOLDER = "app/resource/game/legacy mastery icons"


class Opgg:

    def __init__(self):
        self.session = None
        # Separate session (no base_url) for fetching assets off OP.GG's CDN
        # host, which differs from the API host self.session is pinned to.
        self.assetSession = None
        self.proxy = None
        self._mayhemIconTasks = set()
        self._mayhemIconLocks = {}
        self._mayhemDefinitionTask = None

        if cfg.get(cfg.enableOpggProxy):
            self.proxy = f"http://{cfg.get(cfg.opggProxyAddr)}"

    async def start(self):
        self.session = aiohttp.ClientSession("https://lol-api-champion.op.gg")
        self.assetSession = aiohttp.ClientSession()
        self.hextech = TencentHextech(self.assetSession)
        self.rift = TencentRift(self.hextech)

    async def close(self):
        for task in self._mayhemIconTasks:
            task.cancel()
        if self._mayhemIconTasks:
            await asyncio.gather(*list(self._mayhemIconTasks), return_exceptions=True)
        if self.session:
            await self.session.close()
        if self.assetSession:
            await self.assetSession.close()

    async def __get(self, url, params=None):
        res = await self.session.get(url, params=params, ssl=False, proxy=self.proxy)
        return await res.json()

    async def __downloadAndCacheAsset(self, url, folder, key):
        if not os.path.exists(folder):
            os.makedirs(folder)

        path = f"{folder}/{key}.png"

        if not os.path.exists(path):
            res = await self.assetSession.get(url, ssl=False, proxy=self.proxy)
            data = await res.read()

            with open(path, 'wb') as f:
                f.write(data)

        return path

    async def getBPChampionIcon(self, champion_id):
        """Load draft portraits on demand, preferring the local client."""
        try:
            return await asyncio.wait_for(connector.getChampionIcon(champion_id), 5)
        except (Exception, asyncio.TimeoutError):
            if not self.assetSession or self.assetSession.closed:
                raise ValueError('图标服务尚未连接')
        url = ('https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/'
               f'global/default/v1/champion-icons/{champion_id}.png')
        path = gameIconCachePath('champion', champion_id)
        async with self.assetSession.get(url, proxy=self.proxy,
                                        timeout=aiohttp.ClientTimeout(total=10)) as response:
            response.raise_for_status()
            content = await response.read()
        if not content.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ValueError('英雄图标格式无效')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = path + '.bp.tmp'
        with open(temporary, 'wb') as file:
            file.write(content)
        os.replace(temporary, path)
        return path

    async def getSelectedMatchup(self, region, tier, position, ally_id, enemy_id):
        """The user's pick is never subject to winning/strength/availability filters."""
        sources = SOURCES if region == 'consensus' else (region,)
        version = None
        overseas = [s for s in sources if s != 'cn']
        if overseas:
            ranks = await asyncio.gather(*(self.__fetchTierList(s, 'ranked', tier) for s in overseas))
            versions = {raw['meta']['version'] for raw in ranks}
            if len(versions) != 1:
                raise ValueError('全球与韩服版本不一致，无法比较当前对位。')
            version = versions.pop()
        if 'cn' in sources:
            national = await self.rift.rank(tier, version)
            version = national['version']
        async def query(source):
            if source == 'cn':
                rows, date, sample_tier = await self.rift.counters(tier, version, enemy_id, position)
                return dict(rows.get(ally_id) or {}, date=date, sampleTier=sample_tier)
            raw = await self.__fetchChampionBuild(source, 'ranked', enemy_id, position.lower(), tier)
            rows = parse_overseas_counters(raw, enemy_id, position, version)
            return dict(rows.get(ally_id) or {}, date=raw['meta'].get('cached_at', ''), sampleTier=tier)
        results = await asyncio.gather(*(query(s) for s in sources), return_exceptions=True)
        stats, unavailable = {}, {}
        for source, result in zip(sources, results):
            if isinstance(result, Exception):
                unavailable[source] = str(result)
            elif result.get('winRate') is None:
                unavailable[source] = '该分路没有此英雄组合的对位数据'
            else:
                stats[source] = result
        return dict(allyId=ally_id, enemyId=enemy_id, position=position,
                    version=version, stats=stats, unavailable=unavailable)

    async def getClassicRuneIcon(self, url, sourceToken):
        """
        League Classic's old-style rune icons (Mark/Seal/Glyph/Quintessence)
        are hosted on OP.GG's own CDN, not exposed by the LCU -- cache them
        locally the same way champion/item/rune icons are cached elsewhere.
        """
        return await self.__downloadAndCacheAsset(url, CLASSIC_RUNE_ICON_FOLDER, sourceToken)

    async def getLegacyMasteryIcon(self, url, masteryId):
        """
        Same as getClassicRuneIcon, but for the old Offense/Defense/Utility
        mastery tree icons -- also CDN-hosted by OP.GG, also not in the LCU.
        """
        return await self.__downloadAndCacheAsset(url, LEGACY_MASTERY_ICON_FOLDER, masteryId)

    def _prefetchMayhemIcons(self, augments, version):
        folder = os.path.join(LOCAL_PATH, 'cache', 'tencent-hextech-icons')
        os.makedirs(folder, exist_ok=True)
        for augment in augments:
            url = augment.get('largeIcon') or augment.get('smallIcon')
            definition = getattr(connector.manager, 'cherryAugments', {}).get(augment['id'])
            def basename(value):
                return os.path.basename(value or '').lower().replace('_small', '').replace('_large', '')
            matched = bool(definition and definition.get('augmentSmallIconPath') and
                (augment.get('name') == definition.get('nameTRA') or
                 (url and basename(url) == basename(definition['augmentSmallIconPath']))))
            shared = gameIconCachePath('augment', augment['id']) if matched else ''
            key = hashlib.sha256(url.encode()).hexdigest()[:20] if url else ''
            cdn = os.path.join(folder, key+'.png') if url else ''
            # Existing high-resolution CDN images are also local reusable assets.
            if shared and os.path.isfile(shared) and os.path.getsize(shared):
                augment['icon'] = shared
            elif cdn and os.path.isfile(cdn) and os.path.getsize(cdn):
                augment['icon'] = cdn
            else:
                augment['icon'] = shared or cdn
            augment['_clientIcon'] = matched
            augment['_iconUrl'] = url
        async def loadAll():
            semaphore = asyncio.Semaphore(8)
            async def load(augment):
                path = augment['icon']
                if not path:
                    return
                async with semaphore:
                    lock = self._mayhemIconLocks.setdefault(path, asyncio.Lock())
                    async with lock:
                        if os.path.isfile(path) and os.path.getsize(path):
                            return
                        if augment['_clientIcon']:
                            try:
                                await asyncio.wait_for(connector.getAugmentIcon(augment['id']), timeout=5)
                                return
                            except Exception:
                                pass  # An older client can lack an individual current asset.
                        if not augment['_iconUrl']:
                            augment['iconError'] = True
                            return
                        try:
                            async with self.assetSession.get(augment['_iconUrl'],
                                    timeout=aiohttp.ClientTimeout(total=20)) as response:
                                response.raise_for_status()
                                content = await response.read()
                            if not content.startswith(b'\x89PNG\r\n\x1a\n'):
                                raise ValueError('Invalid icon image')
                            with open(path+'.tmp', 'wb') as file:
                                file.write(content)
                            os.replace(path+'.tmp', path)
                        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
                            augment['iconError'] = True
            # Load the top recommendations before the remaining rows.
            ordered = sorted(augments, key=lambda a: (a['tier'] if a['tier'] is not None else 99, a['rank']))
            await asyncio.gather(*(load(a) for a in ordered[:12]))
            await asyncio.gather(*(load(a) for a in ordered[12:]))
        task = asyncio.create_task(loadAll())
        self._mayhemIconTasks.add(task)
        task.add_done_callback(self._mayhemIconTasks.discard)
        return task

    async def _getMayhemTierList(self, region, tier):
        if self._mayhemDefinitionTask is None:
            async def warm_definitions():
                try:
                    await self.hextech.get_json(AUGMENTS)
                except Exception:
                    pass  # Opening a hero retries and reports any actual failure.
            task = asyncio.create_task(warm_definitions())
            self._mayhemDefinitionTask = task
            self._mayhemIconTasks.add(task)
            task.add_done_callback(self._mayhemIconTasks.discard)
        champions, date = await self.hextech.rank()
        icons = await asyncio.gather(*(connector.getChampionIcon(c['championId']) for c in champions))
        rows = [dict(champion,
                     name=connector.manager.getChampionNameById(champion['championId']),
                     icon=icon, banRate=None, position=None, counters=[],
                     mayhem=True, source='tencent-101')
                for champion, icon in zip(champions, icons)]
        return {'data': rows, 'version': date, 'statDate': date,
                'source': 'tencent-101', 'fallback': False}

    async def _getMayhemBuild(self, region, tier, championId):
        parsed = await self.hextech.build(championId)
        augments = parsed['augments']
        date = parsed['statDate']
        iconTask = self._prefetchMayhemIcons(augments, date)
        recommendations = parsed['recommendations']
        itemIds = sorted({id for group in ('starter_items', 'core_items', 'boots', 'other_items')
                          for ids in recommendations[group] for id in ids})
        itemPaths, championIcon = await asyncio.gather(
            asyncio.gather(*(connector.getItemIcon(id) for id in itemIds)),
            connector.getChampionIcon(championId))
        data = dict(parsed, mayhem=True, championId=championId,
                    name=connector.manager.getChampionNameById(championId),
                    icon=championIcon, _iconTask=iconTask,
                    itemIcons=dict(zip(itemIds, itemPaths)), spellIcons={},
                    source='tencent-101',
                    url='https://101.qq.com/#/hero-detail?heroId='+str(championId)+'&mode=hextech')
        return {'data': data, 'version': date, 'statDate': date,
                'mode': 'aram_mayhem', 'source': 'tencent-101', 'fallback': False}

    @alru_cache(maxsize=512, ttl=900)
    async def __fetchTierList(self, region, mode, tier):
        url = f"/api/{region}/champions/{mode}"
        params = {"tier": tier}

        res = await self.__get(url, params)

        return res

    @alru_cache(maxsize=512, ttl=900)
    async def __fetchChampionBuild(self, region, mode, championId, position, tier):
        if mode != 'arena':
            url = f"/api/{region}/champions/{mode}/{championId}/{position}"
        else:
            url = f"/api/{region}/champions/{mode}/{championId}"

        params = {"tier": tier}

        res = await self.__get(url, params)

        return res

    @alru_cache(maxsize=512, ttl=900)
    async def getChampionBuild(self, region, mode, championId, position, tier):
        if mode == 'aram_mayhem':
            return await self._getMayhemBuild(region, tier, championId)
        if mode == 'ranked' and region in ('cn', 'consensus'):
            version = None
            if region == 'consensus':
                raw = await self.__fetchTierList('global', 'ranked', tier)
                version = raw['meta']['version']
            result = await self.rift.build(tier, championId, position, version)
            data = result['data']
            itemIds = sorted({id for section in data['sections'] for row in section['rows'] for id in row['ids']})
            spellIds = sorted({id for row in data['spells'] for id in row['ids']})
            runeIds = sorted({id for row in data['perks'] for id in row['perks']})
            items, spells, runes, hero = await asyncio.gather(
                asyncio.gather(*(connector.getItemIcon(id) for id in itemIds)),
                asyncio.gather(*(connector.getSummonerSpellIcon(id) for id in spellIds)),
                asyncio.gather(*(connector.getRuneIcon(id) for id in runeIds)),
                connector.getChampionIcon(championId))
            data.update(name=connector.manager.getChampionNameById(championId), icon=hero,
                        itemIcons=dict(zip(itemIds, items)), spellIcons=dict(zip(spellIds, spells)),
                        runeIcons=dict(zip(runeIds, runes)))
            def native_row(row, icon_map):
                return dict(row, icons=[icon_map[id] for id in row['ids']],
                            winRate=row['winRate']/100, pickRate=row['pickRate']/100, play=None)
            groups = [[native_row(row, data['itemIcons']) for row in section['rows']]
                      for section in data['sections']]
            last_ids = list(dict.fromkeys(id for section in data['sections'][3:]
                                         for row in section['rows'] for id in row['ids']))
            native_perks = [dict(row, icons=[data['runeIcons'][id] for id in row['perks']],
                                 winRate=row['winRate']/100, pickRate=row['pickRate']/100)
                            for row in data['perks']]
            data['nativeBuild'] = {'summary': dict(data['summary'], name=data['name'], icon=hero,
                championId=championId), 'summonerSpells': [native_row(row, data['spellIcons']) for row in data['spells'][:2]],
                'perks': native_perks, 'championSkills': data['skillStats'],
                'items': {'startItems': groups[0], 'boots': groups[1], 'coreItems': groups[2],
                          'lastItems': [data['itemIcons'][id] for id in last_ids]},
                'source': 'tencent-rift', 'statDate': data['statDate']}
            return result
        positions = await self.getChampionPositions(region, championId, tier, mode)
        if position not in positions and positions and mode in ('ranked', 'classic'):
            position = positions[0]

        raw = await self.__fetchChampionBuild(region, mode, championId, position, tier)

        if mode != 'arena':
            res = await OpggDataParser.parseOtherChampionBuild(raw, position)
        else:
            res = await OpggDataParser.parseArenaChampionBuild(raw)

        return {
            'data': res,
            'version': raw['meta']['version'],
            'mode': mode,
            'fallback': bool(raw.get('_fallback')),
        }

    async def getRiftAnalysis(self, region, tier, position, fraction=.30, only_strong=True,
                              enemy_id=0, strict=False, excluded=()):
        sources = SOURCES if region == 'consensus' else (region,)
        source_lists = {}
        dates = {}
        overseas = [source for source in sources if source != 'cn']
        raw_lists = await asyncio.gather(*(self.__fetchTierList(source, 'ranked', tier) for source in overseas))
        versions = {raw['meta']['version'] for raw in raw_lists}
        if len(versions) > 1:
            raise ValueError('全球与韩服版本不一致，暂不能比较共同强势英雄。')
        version = next(iter(versions), None)
        for source, raw in zip(overseas, raw_lists):
            source_lists[source] = parse_overseas_rank(raw, source)
            dates[source] = raw['meta'].get('analyzed_at') or raw['meta'].get('cached_at') or ''
        if 'cn' in sources:
            national = await self.rift.rank(tier, version)
            version = national['version']
            source_lists['cn'] = national['data']
            dates['cn'] = national['statDate']
        matchups = None
        unavailable = {}
        matchup_dates = {}
        notes = []
        if enemy_id:
            async def query(source):
                if source == 'cn':
                    rows, date, sample_tier = await self.rift.counters(tier, version, enemy_id, position)
                    if sample_tier != tier:
                        notes.append('腾讯对位样本将宗师/王者合并为大师+。')
                        if strict:
                            raise ValueError('国服对位段位分组不同，无法严格确认三服一致克制。')
                    return rows, date
                raw = await self.__fetchChampionBuild(source, 'ranked', enemy_id, position.lower(), tier)
                return parse_overseas_counters(raw, enemy_id, position, version), raw['meta'].get('cached_at', '')
            results = await asyncio.gather(*(query(source) for source in sources), return_exceptions=True)
            matchups = {}
            for source, result in zip(sources, results):
                if isinstance(result, Exception):
                    unavailable[source] = str(result)
                else:
                    matchups[source], matchup_dates[source] = result
        rows = analyze_rows(source_lists, position, fraction,
                            only_strong=only_strong and region == 'consensus',
                            matchups=matchups, unavailable=unavailable,
                            require_all_matchups=strict, excluded=excluded)
        icons = await asyncio.gather(*(connector.getChampionIcon(row['championId']) for row in rows))
        for row, icon in zip(rows, icons):
            row.update(name=connector.manager.getChampionNameById(row['championId']), icon=icon)
        legacy_rows = []
        if region == 'cn':
            counter_ids = {id for row in rows for id in row['evidence']['cn'].get('counterIds', [])[:3]}
            counter_icons = await asyncio.gather(*(connector.getChampionIcon(id) for id in sorted(counter_ids)))
            counter_icons = dict(zip(sorted(counter_ids), counter_icons))
            for row in rows:
                evidence = row['evidence']['cn']
                rate = row['matchups'].get('cn', {}).get('winRate') if enemy_id else evidence['winRate']
                legacy_rows.append(dict(evidence, name=row['name'], icon=row['icon'], winRate=rate,
                    counters=[{'championId': id, 'icon': counter_icons[id]}
                              for id in evidence.get('counterIds', [])[:3]], source='tencent-rift'))
        return {'data': rows, 'source': 'rift-analysis', 'region': region, 'version': version,
                'legacyRows': legacy_rows,
                'dates': dates, 'sources': list(sources), 'enemyId': enemy_id, 'position': position,
                'fraction': fraction, 'onlyStrong': only_strong, 'strict': strict,
                'unavailable': unavailable, 'matchupDates': matchup_dates, 'notes': notes,
                'enemyName': connector.manager.getChampionNameById(enemy_id) if enemy_id else '',
                'fallback': False}

    @alru_cache(maxsize=512, ttl=900)
    async def getTierList(self, region, mode, tier):
        if mode == 'aram_mayhem':
            return await self._getMayhemTierList(region, tier)
        raw = await self.__fetchTierList(region, mode, tier)

        version = raw['meta']['version']

        if mode == 'ranked':
            res = await OpggDataParser.parseRankedTierList(raw)
        else:
            res = await OpggDataParser.parseOtherTierList(raw)

        return {
            'data': res,
            'version': version,
            'fallback': bool(raw.get('_fallback')),
        }

    @alru_cache(maxsize=512)
    async def getChampionPositions(self, region, championId, tier, mode="ranked"):
        # This call is quite fast due to caching
        data = await self.__fetchTierList(region, mode, tier)

        for item in data['data']:
            if item['id'] == championId:
                return [p['name'] for p in item['positions'] or []]

        return []


class OpggDataParser:

    @staticmethod
    async def parseRankedTierList(data):
        '''
        Original tier data for Summoner's Rift mode, returns all champions and positions together

        In this function, separate them by lane position
        '''

        data = data['data']
        res = {p: []
               for p in ['TOP', 'JUNGLE', 'MID', 'ADC', 'SUPPORT']}

        for item in data:
            championId = item['id']
            name = connector.manager.getChampionNameById(championId)
            icon = await connector.getChampionIcon(championId)

            for p in item['positions']:
                position = p['name']

                stats = p['stats']
                tier = stats['tier_data']

                counters = [{
                    'championId': c['champion_id'],
                    'icon': await connector.getChampionIcon(c['champion_id'])
                } for c in p['counters']]

                res[position].append({
                    'championId': championId,
                    'name': name,
                    'icon': icon,
                    'winRate': stats.get('win_rate'),
                    'pickRate': stats.get('pick_rate'),
                    'banRate': stats.get('ban_rate'),
                    'kda': stats.get('kda'),
                    'tier': tier.get('tier'),
                    'rank': tier.get('rank'),
                    'position': position,
                    'counters': counters,
                })

        # Ranking/tier is unordered, so sort it
        for tier in res.values():
            tier.sort(key=lambda x: x['rank'])

        return res

    @staticmethod
    async def parseOtherTierList(data):
        '''
        Process original tier data for other modes
        '''

        data = data['data']
        res = []

        for item in data:
            stats = item['average_stats']

            if stats == None:
                continue

            if stats.get('rank') == None:
                continue

            championId = item['id']
            name = connector.manager.getChampionNameById(championId)
            icon = await connector.getChampionIcon(championId)

            res.append({
                'championId': championId,
                'name': name,
                'icon': icon,
                'winRate': stats.get('win_rate'),
                'pickRate': stats.get('pick_rate'),
                'banRate': stats.get('ban_rate'),
                'kda': stats.get('kda'),
                'tier': stats.get('tier'),
                'rank': stats.get('rank'),
                "position": None,
                'counters': [],
            })

        return sorted(res, key=lambda x: x['rank'])

    @staticmethod
    async def parseOtherChampionBuild(data, position):
        data = data['data']

        summary = data['summary']
        championId = summary['id']
        icon = await connector.getChampionIcon(championId)
        name = connector.manager.getChampionNameById(championId)

        if position != 'none':
            stats = {}
            # 数据量少的英雄 positions 可能为 None
            for p in summary['positions'] or []:
                if p['name'] == position:
                    stats: dict = p['stats'] or {}
                    break

            winRate = stats.get('win_rate')
            pickRate = stats.get('pick_rate')
            banRate = stats.get('ban_rate')
            kda = stats.get('kda')

            tierData: dict = stats.get('tier_data') or {}
            tier = tierData.get("tier")
            rank = tierData.get("rank")

        else:
            # 数据量少的英雄 average_stats 可能为 None
            stats = summary['average_stats'] or {}
            winRate = stats.get('win_rate')
            pickRate = stats.get('pick_rate')
            banRate = stats.get('ban_rate')
            kda = stats.get('kda')
            tier = stats.get("tier")
            rank = stats.get("rank")

        summonerSpells = []
        for s in data['summoner_spells']:
            if 'play' not in s:
                continue

            icons = [await connector.getSummonerSpellIcon(id)
                     for id in s['ids']]

            summonerSpells.append({
                'ids': s['ids'],
                'icons': icons,
                'win': s['win'],
                'play': s['play'],
                'pickRate': s['pick_rate']
            })

        # League Classic mixes static "recommendation" entries (no play/win/
        # pick_rate, just a source_type tag) in among the real stat-backed
        # ones whenever a champion/position doesn't have enough games --
        # filter those out everywhere rather than crash on a missing key.
        def withStats(items):
            return [i for i in (items or []) if 'play' in i]

        # Low-sample-size modes/positions (e.g. League Classic) may not have
        # enough games recorded to have any skill-order data at all.
        skillMasteries = withStats(data['skill_masteries'])
        skillOrders = withStats(data['skills'])
        if skillMasteries and skillOrders:
            skills = {
                "masteries": skillMasteries[0]['ids'],
                "order": skillOrders[0]['order'],
                'play': skillOrders[0]['play'],
                'win': skillOrders[0]['win'],
                'pickRate': skillOrders[0]['pick_rate']
            }
        else:
            skills = None

        boots = []
        for i in withStats(data['boots'])[:3]:
            icons = [await connector.getItemIcon(id) for id in i['ids']]
            boots.append({
                "icons": icons,
                "play": i['play'],
                "win": i['win'],
                'pickRate': i['pick_rate']
            })

        startItems = []
        for i in withStats(data['starter_items'])[:3]:
            icons = [await connector.getItemIcon(id) for id in i['ids']]
            startItems.append({
                "icons": icons,
                "play": i['play'],
                "win": i['win'],
                'pickRate': i['pick_rate']
            })

        coreItems = []
        for i in withStats(data['core_items'])[:5]:
            icons = [await connector.getItemIcon(id) for id in i['ids']]
            coreItems.append({
                "icons": icons,
                "play": i['play'],
                "win": i['win'],
                'pickRate': i['pick_rate']
            })

        lastItems = []
        for i in withStats(data['last_items'])[:16]:
            lastItems.append(await connector.getItemIcon(i['ids'][0]))

        strongAgainst = []
        weakAgainst = []

        for c in data['counters']:
            winRate = c['win'] / c['play']
            arr = strongAgainst if winRate >= 0.5 else weakAgainst

            arr.append({
                'championId': (id := c['champion_id']),
                'name': connector.manager.getChampionNameById(id),
                'icon': await connector.getChampionIcon(id),
                'play': c['play'],
                'win': c['win'],
                'winRate': winRate
            })

        strongAgainst.sort(key=lambda x: -x['winRate'])
        weakAgainst.sort(key=lambda x: x['winRate'])

        perks = [{
            'primaryId': (mainId := perk['primary_page_id']),
            "primaryIcon": await connector.getRuneIcon(mainId),
            'secondaryId': (subId := perk['secondary_page_id']),
            "secondaryIcon": await connector.getRuneIcon(subId),
            'perks': (perkIds := perk['primary_rune_ids']+perk['secondary_rune_ids']+perk['stat_mod_ids']),
            "icons": [await connector.getRuneIcon(id) for id in perkIds],
            'play': perk['play'],
            'win': perk['win'],
            'pickRate': perk['pick_rate'],
        } for perk in data['runes']
        ]

        # League Classic (and its ARAM: Mayhem Classic-ish variant) use the
        # old pre-2017 Mark/Seal/Glyph/Quintessence rune system instead of
        # the modern rune trees -- OP.GG exposes this separately as
        # "classic_runes" since it has no relation to the modern "runes"
        # field (which is always empty for these modes).
        # Only the first page is ever shown (see ClassicRunesWidget), and
        # alternate "recommendation" pages (source_type set instead of
        # play/win/pick_rate) don't carry a source_token per rune -- so only
        # parse that first page rather than crashing on the others.
        classicRunePages = data.get('classic_runes') or []
        classicRunes = [{
            'runes': [{
                'icon': await opgg.getClassicRuneIcon(rune['icon_url'], rune.get('source_token', rune['id'])),
                'name': rune['name'],
                'tooltip': rune['tooltip'],
                'count': rune['count'],
            } for rune in classicRunePages[0]['runes']],
            'play': classicRunePages[0].get('play'),
            'win': classicRunePages[0].get('win'),
            'pickRate': classicRunePages[0].get('pick_rate'),
        }] if classicRunePages else []

        # ARAM: Mayhem Classic-ish has no runes at all (see classicRunes
        # above), but -- like Mayhem and Arena -- does have an augment pick,
        # exposed the same way Arena's "augment_group" is. Most modes don't
        # have this field at all, so keep augments as None (not []) for
        # them -- ChampionAugmentsWidget treats None as "hide", but an empty
        # list would render as a blank card.
        augmentGroup = data.get('augment_group')
        hasAugmentData = augmentGroup and any(g['augments'] for g in augmentGroup)

        # Not a nested comprehension on purpose: a comprehension-inside-a-
        # comprehension containing `await` fails to compile on Python 3.8
        # ("asynchronous comprehension outside of an asynchronous function"),
        # which is what the CI packaging step still targets.
        augments = None
        if hasAugmentData:
            augments = []
            for group in augmentGroup:
                arr = []
                for aug in group['augments']:
                    augId = aug['id']
                    arr.append({
                        "id": augId,
                        "icon": await connector.getAugmentIcon(augId),
                        "name": connector.manager.getAugmentsName(augId),
                        "win": aug['win'],
                        'play': aug['play'],
                        'pickRate': aug['pick_rate']
                    })
                augments.append(arr)

        # League Classic's old point-allocation mastery trees
        # (Offense/Defense/Utility), separate from classicRunes above.
        # OP.GG returns a couple of alternate "recommendation" pages with no
        # play/win/pickRate to rank them by -- just show the first one.
        legacyMasteryPages = data.get('legacy_masteries') or []
        legacyMasteries = [{
            'id': (mid := mastery['id']),
            'icon': await opgg.getLegacyMasteryIcon(mastery['icon_url'], mid),
            'name': mastery['name'],
            'description': mastery['description'],
            'tree': mastery['tree'],
            'rank': mastery['rank'],
            'maxRank': mastery['max_rank'],
        } for mastery in (legacyMasteryPages[0]['masteries'] if legacyMasteryPages else [])]

        return {
            "summary": {
                'name': name,
                'championId': championId,
                'icon': icon,
                'position': position,
                'winRate': winRate,
                'pickRate': pickRate,
                'banRate': banRate,
                'kda': kda,
                'tier': tier,
                'rank': rank
            },
            "summonerSpells": summonerSpells,
            "championSkills": skills,
            "items": {
                "boots": boots,
                "startItems": startItems,
                "coreItems": coreItems,
                "lastItems": lastItems,
            },
            "counters": {
                "strongAgainst": strongAgainst,
                "weakAgainst": weakAgainst,
            },
            "perks": perks,
            "classicRunes": classicRunes,
            "legacyMasteries": legacyMasteries,
            "augments": augments,
        }

    @staticmethod
    async def parseArenaChampionBuild(data):
        data = data['data']

        summary = data['summary']
        championId = summary['id']
        name = connector.manager.getChampionNameById(championId)
        icon = await connector.getChampionIcon(championId)

        stats = summary['average_stats']
        play = stats['play']
        winRate = stats['win'] / play
        firstRate = stats['first_place'] / play
        averagePlace = stats['total_place'] / play
        pickRate = stats['pick_rate']
        banRate = stats['ban_rate']
        tier = stats['tier']

        skills = {
            "masteries": data['skill_masteries'][0]['ids'],
            "order": data['skills'][0]['order'],
            'play': data['skills'][0]['play'],
            'win': data['skills'][0]['win'],
            'pickRate': data['skills'][0]['pick_rate']
        }

        async def parseItemGroup(items, limit):
            res = []
            for i in (items or [])[:limit]:
                icons = [await connector.getItemIcon(id) for id in i['ids']]
                res.append({
                    "icons": icons,
                    "play": i['play'],
                    "win": i['win'],
                    'pickRate': i['pick_rate'],
                    "averatePlace": i['total_place'] / i['play'],
                    "firstRate": i['first_place'] / i['play']
                })

            return res

        boots = await parseItemGroup(data.get('boots'), 3)
        startItems = await parseItemGroup(data.get('starter_items'), 3)
        prismItems = await parseItemGroup(data.get('prism_items'), 3)
        coreItems = await parseItemGroup(data.get('core_items'), 5)

        lastItems = []
        for i in (data.get('last_items') or [])[:16]:
            lastItems.append(await connector.getItemIcon(i['ids'][0]))

        augments = []
        for item in data['augment_group']:
            arr = [{
                "id": (augId := aug['id']),
                "icon": await connector.getAugmentIcon(augId),
                "name": connector.manager.getAugmentsName(augId),
                "win": aug['win'],
                'play': aug['play'],
                "totalPlace": aug['total_place'],
                "firstPlace": aug['first_place'],
                'pickRate': aug['pick_rate']
            }for aug in item['augments']]

            augments.append(arr)

        synergies = [{
            "championId": (chId := syn['champion_id']),
            'icon': await connector.getChampionIcon(chId),
            "name": connector.manager.getChampionNameById(chId),
            "win": syn['win'],
            'play': syn['play'],
            "totalPlace": syn['total_place'],
            "firstPlace": syn['first_place'],
            'pickRate': syn['pick_rate']
        }for syn in data['synergies']]

        return {
            "summary": {
                "name": name,
                "icon": icon,
                "championId": championId,
                "play": play,
                "winRate": winRate,
                "firstRate": firstRate,
                "averagePlace": averagePlace,
                "pickRate": pickRate,
                "banRate": banRate,
                "tier": tier,
                "position": "none"
            },
            "championSkills": skills,
            "items": {
                "boots": boots,
                "startItems": startItems,
                "prismItems": prismItems,
                "coreItems": coreItems,
                "lastItems": lastItems,
            },
            "augments": augments,
            "synergies": synergies,
        }


opgg = Opgg()
