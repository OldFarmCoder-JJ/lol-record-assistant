"""Conservative draft evidence: confirmed bans/picks and optional client pickable list."""
from dataclasses import dataclass, field


def champion_ids(values):
    return frozenset(value for value in values if type(value) is int and value > 0)


@dataclass(frozen=True)
class DraftAvailability:
    active: bool = False
    banned: frozenset = field(default_factory=frozenset)
    picked: frozenset = field(default_factory=frozenset)
    pickable: object = None  # None means unknown; absence never establishes ownership.

    @classmethod
    def from_session(cls, session, pickable=None):
        if not isinstance(session, dict) or not isinstance(session.get('myTeam'), list):
            return cls()
        actions = [action for group in session.get('actions') or [] if isinstance(group, list)
                   for action in group if isinstance(action, dict)]
        banned = set()
        bans = session.get('bans') or {}
        for values in (bans.values() if isinstance(bans, dict) else ()):
            if isinstance(values, list):
                banned.update(champion_ids(values))
        banned.update(champion_ids(action.get('championId') for action in actions
                                  if action.get('type') == 'ban' and action.get('completed') is True))
        picked = champion_ids(action.get('championId') for action in actions
                              if action.get('type') == 'pick' and action.get('completed') is True)
        # Without action evidence, a team portrait may still be a hover. Do not
        # call it locked, and do not infer ownership from the champion's role.
        usable = (champion_ids(pickable) if isinstance(pickable, (list, set, frozenset))
                  and all(type(hero) is int and hero > 0 for hero in pickable) else None)
        # Empty lists also occur during phase transitions. Keep those unknown.
        return cls(True, frozenset(banned), picked, usable or None)

    def status(self, hero):
        if not self.active:
            return ''
        if hero in self.banned:
            return 'banned'
        if hero in self.picked:
            return 'picked'
        if self.pickable is not None:
            return 'available' if hero in self.pickable else 'unavailable'
        return 'unknown'


STATUS_TEXT = {
    '': '',
    'banned': '已禁用',
    'picked': '已锁定选择',
    'available': '当前可选 · 客户端确认',
    'unavailable': '暂不可选 · 客户端可选列表未包含，原因未确认',
    'unknown': '可选状态未确认',
}
