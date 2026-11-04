"""Personal win-rate display, with explicit sample sizes rather than mastery claims."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class ChampionPerformance:
    games: int
    win_rate: float | None
    label: str
    tone: str
    lower_bound: float


def champion_performance(wins, losses):
    """Counts are confirmed, non-remake outcomes from personal_history.aggregate."""
    games = wins + losses
    if not games:
        return ChampionPerformance(0, None, '待统计', 'neutral', 0.0)
    rate = wins / games
    # Wilson 95% lower bound is used only to sort within sample-size groups.
    z2 = 1.96 ** 2
    lower = (rate + z2 / (2 * games) - 1.96 * math.sqrt(
        rate * (1 - rate) / games + z2 / (4 * games * games))) / (1 + z2 / games)
    lower = max(0.0, min(1.0, lower))
    if games < 5:
        label, tone = '样本少', 'neutral'
    elif games < 10:
        label, tone = '观察中', 'neutral'
    elif wins * 100 >= games * 60:
        label, tone = '强势', 'strong'
    elif wins * 100 >= games * 55:
        label, tone = '不错', 'good'
    elif wins * 100 >= games * 45:
        label, tone = '平稳', 'steady'
    else:
        label, tone = '待提升', 'improve'
    return ChampionPerformance(games, rate * 100, label, tone, lower)


def sort_champions(rows, order='games'):
    """Do not hide small samples, and keep ties deterministic across refreshes."""
    def key(row):
        performance = champion_performance(row['wins'], row['losses'])
        fallback = (-row['games'], -row['lastPlayed'], row['championId'])
        if order == 'advantage':
            return (-(performance.games >= 10), -(performance.games > 0), -performance.lower_bound, *fallback)
        if order == 'winrate':
            return (-(performance.win_rate if performance.win_rate is not None else -1),
                    -performance.games, *fallback)
        if order == 'recent':
            return (-row['lastPlayed'], *fallback)
        return fallback
    return sorted(rows, key=key)
