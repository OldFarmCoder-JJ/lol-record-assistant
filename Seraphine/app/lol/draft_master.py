"""Explainable draft suggestions. Trait matches are rules, never win probabilities."""
from math import log
from typing import NamedTuple

from app.lol.draft_follow import LANES

# Common picks used only while the matching lane ranking is unavailable.
LANE_PICKS = {
    'TOP': (54, 24, 78, 516, 114, 875, 17, 85, 36, 887, 6, 98),
    'JUNGLE': (33, 78, 113, 32, 254, 121, 203, 48, 427, 59, 57, 5, 14, 54, 28, 107, 141),
    'MID': (127, 3, 163, 69, 90, 45, 61, 101, 115, 103, 34, 268, 38),
    'ADC': (222, 67, 96, 145, 81, 15, 110, 51, 22, 498, 18, 29),
    'SUPPORT': (78, 201, 40, 117, 12, 111, 89, 25, 267, 43, 143, 161, 63),
}
# Nonexclusive, deliberately conservative tags. Missing tags confer no evidence.
AUTO = {11, 23, 24, 17, 51, 67, 96, 222, 22, 29, 15, 18, 119, 236, 523,
        145, 203, 21, 104, 157, 777, 895, 498, 429, 221, 41, 200, 19, 48}
DIVE = {5, 11, 24, 39, 56, 59, 64, 84, 91, 105, 120, 121, 131, 141, 157,
        164, 238, 245, 254, 266, 421, 497, 517, 555, 777, 875, 897}
FRONT = {12, 14, 31, 32, 33, 36, 44, 48, 54, 57, 72, 78, 79, 89, 98, 111,
         113, 154, 201, 223, 516, 526, 875, 897}
POKE = {51, 76, 81, 99, 101, 110, 115, 126, 142, 161, 235, 202}
MAGIC_BURST = {1, 7, 34, 45, 55, 60, 61, 63, 84, 99, 103, 105, 112, 127, 134, 142, 245, 517, 518, 711}
CARRY = {22, 29, 67, 96, 203, 222, 498, 523}
PEEL = {12, 25, 34, 40, 43, 44, 78, 98, 117, 127, 163, 201, 223, 267, 427}
ENGAGE = {12, 32, 54, 57, 59, 79, 85, 89, 111, 113, 127, 154, 254, 497, 516, 526}
# Positive evidence only. An untagged champion is never evidence of low tank damage.
FRAGILE = {1, 7, 15, 16, 18, 21, 22, 29, 30, 37, 40, 45, 51, 63, 67, 81, 96,
           99, 101, 110, 115, 117, 119, 134, 142, 161, 202, 222, 267, 498, 523}
ASSASSINS = {7, 28, 38, 55, 56, 64, 76, 84, 91, 105, 107, 121, 141, 238, 245, 246}
RANGED_PHYSICAL = {15, 18, 21, 22, 29, 51, 81, 104, 119, 126, 133, 202, 203, 222, 236, 498, 523}
LOW_TANK_DAMAGE = {1, 7, 12, 16, 25, 37, 40, 53, 89, 99, 111, 117, 127, 134, 142, 201, 202, 267, 497, 555}
TANK_BUSTERS = {11, 48, 67, 69, 96, 110, 114, 145, 203, 268, 887}
# Specific, stoppable movements. Blinks and displacement-immune ults are excluded.
POPPY_W_TARGETS = {104: '男枪E', 7: '妖姬W去程', 67: '薇恩Q', 136: '龙王W',
                   39: '刀妹Q', 24: '贾克斯Q', 157: '亚索E', 236: '卢锡安E',
                   64: '盲僧二段Q', 254: '蔚Q', 18: '小炮W', 58: '鳄鱼E'}
RULES = (
    (AUTO, {17, 24, 33, 54, 98}, '对面有{}名普攻手，可限制普攻输出'),
    (DIVE, PEEL, '对面有{}名突进英雄，可保护后排'),
    (DIVE, {15, 18, 81, 498}, '对面有{}名突进英雄，有自保手段'),
    (FRONT, {48, 63, 67, 69, 96, 110, 114, 145, 203, 268, 887}, '对面有{}名前排，可持续打前排'),
    (MAGIC_BURST, {3, 38, 25}, '对面有{}名法术爆发英雄，可用魔法护盾应对'),
    (POKE, ENGAGE, '对面有{}名远程消耗英雄，可主动开团'),
)


def completed_picks(session):
    return {a.get('actorCellId'): a.get('championId') for group in session.get('actions') or []
             for a in group if a.get('type') == 'pick' and a.get('completed') is True and (a.get('championId') or 0) > 0}
def own_locked_pick(session):
    local = session.get('localPlayerCellId')
    locks = completed_picks(session)
    if local is None or local not in locks:
        return 0
    player = next((p for p in session.get('myTeam') or [] if p.get('cellId') == local), {})
    return player.get('championId') or locks[local]


def locked_teams(session):
    """Use completed actions to confirm slots, current portraits to follow trades."""
    locks = completed_picks(session)
    local = session.get('localPlayerCellId')
    def team(side):
        return {p.get('championId') or locks[p['cellId']] for p in session.get(side) or []
                if p.get('cellId') in locks and p.get('cellId') != local} - {0, None}
    player = next((p for p in session.get('myTeam') or [] if p.get('cellId') == local), {})
    lane = LANES.get(str(player.get('assignedPosition') or '').lower())
    return team('theirTeam'), team('myTeam'), lane, local in locks


def identify_opponent(session, enemies, manual, rows, position):
    if manual in enemies:
        return manual, '手动指定'
    possible = set(enemies)
    confirmed = set()
    for player in session.get('theirTeam') or []:
        hero = player.get('championId')
        if hero not in enemies:
            continue
        lane = LANES.get(str(player.get('assignedPosition') or '').lower())
        smite = 11 in (player.get('spell1Id'), player.get('spell2Id'))
        if lane == position or (position == 'JUNGLE' and smite):
            confirmed.add(hero)
        elif lane or smite:
            possible.discard(hero)
    if len(confirmed) == 1:
        return confirmed.pop(), '客户端分路或惩戒' if position == 'JUNGLE' else '客户端分路'
    common = {r['championId'] for r in rows} or set(LANE_PICKS.get(position, ()))
    likely = possible & common
    if not confirmed and len(likely) == 1:
        return likely.pop(), '分路推测，可改'
    return 0, ''


class Pick(NamedTuple):
    hero: int
    name: str
    icon: str
    reason: str
    stats: str


def rating(rate):
    rate = min(.99, max(.01, rate))
    return log(rate / (1 - rate))


def recommend(lane, enemies, allies, availability, preferences, rows, name, opponent=0, matchups=None):
    by_id = {r['championId']: r for r in rows}
    # Include off-meta tank/assassin options even when a lane board omits them.
    candidates = dict.fromkeys([*by_id, *LANE_PICKS.get(lane, ())])
    direct = {c['championId'] for c in by_id.get(opponent, {}).get('counters', ()) or ()}
    enemy_base = by_id.get(opponent, {}).get('winRate') or .5
    blocked = [skill for enemy, skill in POPPY_W_TARGETS.items() if enemy in enemies]
    scored = []
    for index, hero in enumerate(candidates):
        if hero in enemies or hero in allies or availability.status(hero) in ('picked', 'banned', 'unavailable'):
            continue
        reasons = []
        for threats, answers, reason in RULES:
            count = len(threats & enemies)
            if hero == 78 and blocked and threats is DIVE:
                continue  # The specific W interaction replaces the generic peel reason.
            if hero in answers and count:
                reasons.append((count, reason.format(count)))
        if hero == 78 and blocked:
            reasons.append((2 + len(blocked), 'W圈内可拦' + '、'.join(blocked)))
        fragile = len(enemies & FRAGILE)
        if hero in ASSASSINS and fragile >= 2:
            reasons.append((fragile + 1, f'可切入对面{fragile}名脆皮'))
        ranged = len(enemies & RANGED_PHYSICAL)
        if hero in (54, 33) and ranged >= 2:
            reasons.append((ranged + 2, f'护甲收益高，可针对{ranged}名物理长手'))
        if allies & CARRY and hero in PEEL:
            reasons.append((2, '能保护己方持续输出位'))
        if len(allies) >= 2 and not allies & FRONT and hero in FRONT:
            reasons.append((3, '能给已选队友补前排'))
        if hero in FRONT and len(enemies & LOW_TANK_DAMAGE) >= 2 and not enemies & TANK_BUSTERS:
            reasons.append((3, '已选敌人以爆发或辅助为主，持续打坦能力有限'))
        if not opponent and any(hero in (r.get('counterIds') or []) or
                                any(c['championId'] == hero for c in r.get('counters', []))
                                for h, r in by_id.items() if h in enemies):
            reasons.append((2, '若同路可打分路榜中的已选对手'))
        row = by_id.get(hero, {})
        base = row.get('winRate')
        matchup = (matchups or {}).get(hero, {})
        observed = matchup.get('winRate')
        games = matchup.get('play')
        bonus = 0
        priority = 1
        stats = [f'分路胜率 {base:.1%}' if base is not None else '分路胜率暂无']
        if observed is not None:
            # Compare the pair with expected log odds from both standalone roles.
            # Shrink small samples towards neutral. This is ranking evidence.
            delta = rating(observed) - rating(base or .5) + rating(enemy_base)
            weight = games / (games + 200) if games else .5
            bonus = max(-4, min(4, delta * weight * 15))
            if games is None or games >= 200:
                if observed > .5 and delta > 0:
                    priority = 0
                elif observed < .5 and delta < 0:
                    priority = 2
            stats.append(f'对位胜率 {observed:.1%}' + (f'（{games}场）' if games else '（样本数未知）'))
        elif hero in direct:
            priority = 0
            stats.append(('打野克制榜' if lane == 'JUNGLE' else '同路克制榜') + '可打' + name(opponent))
        if not reasons and not row and priority == 1 and hero not in preferences.favorites:
            continue
        reasons.sort(key=lambda item: -item[0])
        # Use the strongest distinct reasons, limiting double counting of similar traits.
        fit = sum(points for points, _ in reasons[:2])
        strength = max(-4, min(4, rating(base or .5) * 16))
        familiar = hero in preferences.favorites or bool(preferences.summary(hero))
        details = [reason for _, reason in reasons[:2]]
        if familiar:
            details.append('已收藏' if hero in preferences.favorites else '近期排位玩过')
        if not details:
            details.append('按当前对位和分路表现入选' if opponent else '按当前分路表现入选')
        key = (priority, -(fit + strength + bonus + .5 * familiar), row.get('rank') or index + 1, hero)
        pick = Pick(hero, row.get('name') or name(hero), row.get('icon') or '', '，'.join(details), ' · '.join(stats))
        scored.append((key, pick))
    return [pick for _, pick in sorted(scored)[:12]]
