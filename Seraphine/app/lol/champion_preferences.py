"""Local, account-isolated favorites and factual recent-play summaries."""
import hashlib
import json
from pathlib import Path
import tempfile


class ChampionPreferences:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.account = ''
        self.favorites = set()
        self.recent = {'ranked': {}, 'mayhem': {}}

    def path(self):
        return self.folder / (hashlib.sha256(self.account.encode()).hexdigest() + '.json')

    def set_account(self, account):
        account = account.get('puuid', '') if isinstance(account, dict) else account
        account = account if isinstance(account, str) else ''
        if account == self.account:
            return False
        self.account = account
        self.favorites = set()
        self.recent = {'ranked': {}, 'mayhem': {}}
        if not account:
            return True
        try:
            data = json.loads(self.path().read_text(encoding='utf-8'))
            if data.get('schema') == 1 and isinstance(data.get('favorites'), list):
                self.favorites = {hero for hero in data['favorites']
                                  if isinstance(hero, int) and not isinstance(hero, bool) and hero > 0}
        except (OSError, ValueError, AttributeError):
            pass
        return True

    def toggle(self, hero):
        if not self.account or not isinstance(hero, int) or isinstance(hero, bool) or hero <= 0:
            return False
        favorites = self.favorites ^ {hero}
        self.folder.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=self.folder,
                                             prefix='favorites-', suffix='.tmp', delete=False) as file:
                temporary = Path(file.name)
                json.dump({'schema': 1, 'favorites': sorted(favorites)}, file)
            temporary.replace(self.path())
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
        self.favorites = favorites
        return hero in favorites

    def set_recent(self, records):
        recent = {'ranked': {}, 'mayhem': {}}
        if not self.account:
            records = []
        seen, mode_counts = set(), {'ranked': 0, 'mayhem': 0}
        rows = [row for row in records if isinstance(row, dict)]
        rows.sort(key=lambda row: row.get('date') or 0, reverse=True)
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or row.get('remake'):
                continue
            hero = row.get('champion')
            if not isinstance(hero, int) or isinstance(hero, bool) or hero <= 0:
                continue
            queue = row.get('queue')
            mode = 'ranked' if queue in (420, 440) else 'mayhem' if queue == 2400 else None
            if mode is None:
                continue
            key = row.get('key') or ((queue, row['gameId']) if row.get('gameId') else ('index', index))
            if key in seen:
                continue
            seen.add(key)
            if mode_counts[mode] >= 20:
                continue
            mode_counts[mode] += 1
            summary = recent[mode].setdefault(hero, {'games': 0, 'wins': 0, 'losses': 0})
            summary['games'] += 1
            summary['wins'] += row.get('win') is True
            summary['losses'] += row.get('win') is False
        if recent == self.recent:
            return False
        self.recent = recent
        return True

    def summary(self, hero, mayhem=False):
        return self.recent['mayhem' if mayhem else 'ranked'].get(hero, {})


def preference_text(favorite, summary, mayhem=False):
    parts = ['已收藏' if favorite else '未收藏']
    if summary:
        mode = '海斗' if mayhem else '排位'
        unknown = summary['games'] - summary['wins'] - summary['losses']
        text = f"已读取记录内近期玩过：{mode} {summary['games']} 场，{summary['wins']} 胜 {summary['losses']} 负"
        if unknown:
            text += f'，{unknown} 场结果未知'
        parts.append(text)
        parts.append('基于已读取记录内最近20场有效同模式对局；近期玩过不代表熟练度。')
        parts.append('收藏不改变胜率排序。')
    return '\n'.join(parts)
