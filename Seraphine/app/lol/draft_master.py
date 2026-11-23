"""Explainable draft suggestions. Trait matches are rules, never win probabilities."""
from app.lol.draft_follow import LANES

# Common picks used only while the matching lane ranking is unavailable.
LANE_PICKS = {
    'TOP': (54, 24, 78, 516, 114, 875, 17, 85, 36, 887, 6, 98),
    'JUNGLE': (33, 78, 113, 32, 254, 121, 203, 48, 427, 59, 57, 5),
    'MID': (127, 3, 163, 69, 90, 45, 61, 101, 115, 103, 34, 268, 38),
    'ADC': (222, 67, 96, 145, 81, 15, 110, 51, 22, 498, 18, 29),
    'SUPPORT': (201, 40, 117, 12, 111, 89, 25, 267, 43, 143, 161, 63),
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
RULES = (
    (AUTO, {17, 24, 33, 54, 98}, '对面有{}名普攻手，可限制普攻输出'),
    (DIVE, PEEL, '对面有{}名突进英雄，可保护后排'),
    (DIVE, {15, 18, 81, 498}, '对面有{}名突进英雄，有自保手段'),
    (FRONT, {48, 63, 67, 69, 96, 110, 114, 145, 203, 268, 887}, '对面有{}名前排，可持续打前排'),
    (MAGIC_BURST, {3, 38, 25}, '对面有{}名法术爆发英雄，可用魔法护盾应对'),
    (POKE, ENGAGE, '对面有{}名远程消耗英雄，可主动开团'),
)


def locked_teams(session):
    """Use completed actions to confirm slots, current portraits to follow trades."""
    locks = {a.get('actorCellId'): a.get('championId') for group in session.get('actions') or []
             for a in group if a.get('type') == 'pick' and a.get('completed') is True}
    local = session.get('localPlayerCellId')
    def team(side):
        return {p.get('championId') or locks[p['cellId']] for p in session.get(side) or []
                if p.get('cellId') in locks and p.get('cellId') != local} - {0, None}
    player = next((p for p in session.get('myTeam') or [] if p.get('cellId') == local), {})
    lane = LANES.get(str(player.get('assignedPosition') or '').lower())
    return team('theirTeam'), team('myTeam'), lane, local in locks


def recommend(lane, enemies, allies, availability, preferences, rows, name):
    by_id = {r['championId']: r for r in rows}
    candidates = by_id or LANE_PICKS.get(lane, ())
    counters = {}
    for enemy in sorted(enemies & by_id.keys()):
        for entry in by_id[enemy].get('counters') or ():
            counters.setdefault(entry['championId'], []).append(by_id[enemy]['name'])
    scored = []
    for index, hero in enumerate(candidates):
        if hero in enemies or hero in allies or availability.status(hero) in ('picked', 'banned', 'unavailable'):
            continue
        reasons = []
        score = 0
        for threats, answers, reason in RULES:
            count = len(threats & enemies)
            if hero in answers and count:
                reasons.append((count, reason.format(count)))
                score += count
        if hero in counters:
            reasons.append((2, '若同路可打' + '、'.join(counters[hero])))
            score += 2
        if allies & CARRY and hero in PEEL:
            reasons.append((2, '能保护己方持续输出位'))
            score += 2
        if len(allies) >= 2 and not allies & FRONT and hero in FRONT:
            reasons.append((1, '能给已选队友补前排'))
            score += 1
        row = by_id.get(hero) or {}
        favorite = hero in preferences.favorites
        recent = bool(preferences.summary(hero))
        if not reasons and not row and not (favorite or recent):
            continue
        reasons.sort(key=lambda pair: -pair[0])
        details = [s for _, s in reasons[:2]]
        if not details and row:
            details.append(f"当前分路排行第{row['rank']}")
        if favorite or recent:
            details.append('已收藏' if favorite else '近期排位玩过')
        # Prefer familiar picks at similar fit, while preserving strong draft evidence.
        key = (-(score + int(favorite or recent)), -score, row.get('rank') or index + 1, hero)
        scored.append((key, row.get('name') or name(hero), '，'.join(details)))
    return [(label, reason) for _, label, reason in sorted(scored)[:3]]
