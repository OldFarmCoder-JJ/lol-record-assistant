"""Pure champion-select following state; no client writes, Qt, or persisted settings."""
from collections import deque


RIFT_QUEUES = frozenset((400, 420, 430, 440, 490))
LANES = {'top': 'TOP', 'jungle': 'JUNGLE', 'middle': 'MID',
         'bottom': 'ADC', 'utility': 'SUPPORT'}


def _positive_int(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    try:
        value = int(value)
        return value if value > 0 else None
    except ValueError:
        return None


def draft_target(flow, session):
    """Return confirmed supported mode/lane and identity, otherwise None.

    Enemy champions, team cell order and champion stereotypes never determine lanes.
    """
    if not isinstance(flow, dict) or flow.get('phase') != 'ChampSelect' or not isinstance(session, dict):
        return None
    data = flow.get('gameData')
    if not isinstance(data, dict):
        return None
    queue = data.get('queue')
    if not isinstance(queue, dict):
        return None
    queue_id = _positive_int(queue.get('id'))
    session_queue = _positive_int(session.get('queueId'))
    if queue_id and session_queue and queue_id != session_queue:
        return None
    game_id = _positive_int(data.get('gameId')) or _positive_int(flow.get('gameId'))
    session_game = _positive_int(session.get('gameId'))
    if game_id and session_game and game_id != session_game:
        return None
    game_id = game_id or session_game
    map_data = data.get('map')
    map_id = _positive_int(map_data.get('id')) if isinstance(map_data, dict) else None
    map_id = map_id or _positive_int(queue.get('mapId'))
    mode = queue.get('gameMode') or data.get('gameMode')
    if queue_id == 2400 or mode == 'KIWI':
        target_mode = 'aram_mayhem'
    elif queue_id in RIFT_QUEUES and (map_id is None or map_id == 11) and mode in (None, '', 'CLASSIC'):
        target_mode = 'ranked'
    else:
        return None
    players = session.get('myTeam')
    if not isinstance(players, list):
        return None
    cell_id = session.get('localPlayerCellId')
    local = next((player for player in players if isinstance(player, dict)
                  and cell_id is not None and player.get('cellId') == cell_id), {})
    lane = LANES.get(str(local.get('assignedPosition') or '').strip().lower()) if target_mode == 'ranked' else None
    return {'mode': target_mode, 'lane': lane, 'queueId': queue_id or session_queue, 'gameId': game_id}


class DraftFollow:
    """Choose changes once per supported draft while respecting manual browsing.

    Call ``on_phase`` on gameflow events and capture ``revision`` before async client
    reads. Pass that revision to ``observe`` so a late reply cannot revive a draft.
    User overrides before the first reply are retained until a real draft boundary.
    """

    def __init__(self, enabled=True):
        self.enabled = bool(enabled)
        self.suspended = False
        self.revision = 0
        self.phase = None
        self._identity = None
        self._last_target = None
        self._retired = deque(maxlen=32)

    def reset(self):
        """Reset for a client/account reconnect; enable preference stays unchanged."""
        self.revision += 1
        self.phase = None
        self.suspended = False
        self._identity = None
        self._last_target = None
        self._retired.clear()
        return self.revision

    def _retire(self):
        if self._identity and self._identity[2]:
            self._retired.append(self._identity)

    def on_phase(self, phase):
        if phase == self.phase:
            return self.revision
        initial_override = self.phase is None and phase == 'ChampSelect' and self.suspended
        self.revision += 1
        if phase != 'ChampSelect':
            self._retire()
        self.phase = phase
        self._identity = None
        self._last_target = None
        self.suspended = initial_override
        return self.revision

    def user_override(self):
        """Suspend the current draft, including its pending first client reply."""
        self.revision += 1
        self.suspended = True
        self._last_target = None

    def set_enabled(self, enabled):
        enabled = bool(enabled)
        if enabled == self.enabled:
            return
        self.enabled = enabled
        self.revision += 1
        self.suspended = False
        self._last_target = None

    def observe(self, flow, session, revision=None):
        if revision is not None and revision != self.revision:
            return None
        if self.phase not in (None, 'ChampSelect'):
            return None
        target = draft_target(flow, session)
        if target is None:
            return None
        identity = (target['mode'], target['queueId'], target['gameId'])
        if target['gameId'] and any(old[0] == identity[0] and old[2] == identity[2]
                                    for old in self._retired):
            return None
        previous = self._identity
        if previous is not None:
            # A missing id becoming known is the same draft, not a reason to
            # override a user's manual browsing choice.
            changed = (previous[0] != identity[0]
                       or (previous[1] and identity[1] and previous[1] != identity[1])
                       or (previous[2] and identity[2] and previous[2] != identity[2]))
            if changed:
                self._retire()
                self.revision += 1
                self.suspended = False
                self._last_target = None
            else:
                identity = (identity[0], identity[1] or previous[1], identity[2] or previous[2])
        self.phase = 'ChampSelect'
        self._identity = identity
        selection = (target['mode'], target['lane'])
        if not self.enabled or self.suspended or selection == self._last_target:
            return None
        self._last_target = selection
        return {'mode': selection[0], 'lane': selection[1]}
