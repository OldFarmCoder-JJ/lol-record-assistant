"""Read-only Hextech draft state and recommendation ordering."""


def is_hextech(flow):
    queue = (flow.get('gameData') or {}).get('queue') or {}
    return queue.get('id') == 2400 or queue.get('gameMode') == 'KIWI'


def draft_choices(session):
    """Bench heroes plus our own hero; teammates are not directly selectable."""
    local = next((p for p in session.get('myTeam') or []
                  if p.get('cellId') == session.get('localPlayerCellId')), {})
    current = int(local.get('championId') or 0)
    occupied = {p.get('championId') for p in session.get('myTeam') or []}
    choices = {}
    if session.get('benchEnabled'):
        for row in session.get('benchChampions') or []:
            hero = int(row.get('championId') or 0)
            if hero > 0 and hero not in occupied:
                choices[hero] = '备选席'
    if current:
        choices[current] = '当前'
    return current, choices


def champion_order(row, choices):
    rate = row.get('winRate')
    return (row['championId'] not in choices, rate is None,
            -rate if rate is not None else 0, row.get('rank', 9999))


def augment_order(row):
    return (row.get('tier') is None,
            row['tier'] if row.get('tier') is not None else 99,
            row.get('rank', 9999), -row.get('popular', 0))


def reroll_advice(rows, names):
    """Conservative, champion-specific grade heuristic, not live synergy analysis."""
    names = {name.strip() for name in names if name.strip()}
    if not names:
        return 'empty', '填齐三个候选后，会提示是否值得刷新。'
    if len(names) != 3:
        return 'incomplete', '请填齐三个不同候选，再判断是否刷新。'
    by_name = {row['name']: row for row in rows}
    if not names.issubset(by_name):
        return 'unknown', '有候选未匹配，暂不判断是否刷新。'
    candidates = [by_name[name] for name in names]
    if any(row.get('tier') not in range(5) for row in candidates):
        return 'unknown', '有候选缺少官方评级，暂不判断是否刷新。'
    candidates.sort(key=augment_order)
    best = candidates[0]
    title = best['name'] + '（' + 'SABCD'[best['tier']] + '级）'
    if best['tier'] >= 3:
        return 'reroll', ('建议刷新一下（若还有次数）：三个候选均为 C / D 级。'
                          '\n若没有刷新次数，可优先参考：' + title + '。')
    if best['tier'] <= 1:
        return 'keep', '已有较高评级候选，建议保留刷新次数。\n优先参考：' + title + '。'
    return 'keep', '有 B 级候选可用，优先保留刷新次数。\n可参考：' + title + '。'


def game_champion(flow, account):
    data = flow.get('gameData') or {}
    players = ((data.get('playerChampionSelections') or [])
               + (data.get('teamOne') or []) + (data.get('teamTwo') or []))
    for row in players:
        if ((account.get('puuid') and row.get('puuid') == account['puuid'])
                or (account.get('summonerId') and row.get('summonerId') == account['summonerId'])):
            hero = int(row.get('championId') or 0)
            if hero > 0:
                return hero
    return 0
