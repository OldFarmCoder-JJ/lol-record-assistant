"""Pure draft following: confirmed modes/own lanes, manual override and stale snapshots."""
from copy import deepcopy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'Seraphine'))
from app.lol.draft_follow import DraftFollow, draft_target


def flow(queue=420, game=10, mode='CLASSIC', map_id=11, phase='ChampSelect'):
    return dict(phase=phase, gameData=dict(gameId=game, map=dict(id=map_id), queue=dict(id=queue, gameMode=mode)))


def session(game=10, lane='middle', queue=None):
    result = dict(gameId=game, localPlayerCellId=3,
                  myTeam=[dict(cellId=0, assignedPosition='top'), dict(cellId=3, assignedPosition=lane)],
                  theirTeam=[dict(cellId=8, assignedPosition='jungle', championId=64)])
    if queue is not None:
        result['queueId'] = queue
    return result


controller = DraftFollow()
assert controller.enabled and not controller.suspended
revision = controller.on_phase('ChampSelect')
assert controller.observe(flow(), session(), revision) == dict(mode='ranked', lane='MID')
assert controller.observe(flow(), session()) is None
assert controller.observe(flow(), session(lane='jungle')) == dict(mode='ranked', lane='JUNGLE')
controller.user_override()
assert controller.suspended
assert controller.observe(flow(), session(lane='top')) is None
controller.on_phase('ChampSelect')  # Repeated events cannot clear the manual choice.
assert controller.suspended and controller.observe(flow(), session()) is None
controller.set_enabled(False)
assert controller.observe(flow(), session()) is None
controller.set_enabled(True)
assert controller.observe(flow(), session()) == dict(mode='ranked', lane='MID')

controller.on_phase('Lobby')
assert controller.observe(flow(), session()) is None
controller.on_phase('ChampSelect')
assert controller.observe(flow(), session()) is None, 'Completed draft id must not revive'
assert controller.observe(flow(game=11), session(game=11)) == dict(mode='ranked', lane='MID')
controller.user_override()
assert controller.observe(flow(game=12), session(game=12)) == dict(mode='ranked', lane='MID')
assert not controller.suspended
assert controller.observe(flow(game=11), session(game=11)) is None, 'Older identity cannot overwrite a newer draft'

# Late initial reads cannot override manual browsing, even before an id is discovered.
controller.reset()
pending = controller.revision
controller.user_override()
assert controller.observe(flow(), session(), pending) is None
assert controller.observe(flow(), session()) is None and controller.suspended
controller.on_phase('ChampSelect')
assert controller.observe(flow(), session()) is None and controller.suspended
controller.on_phase('Lobby')
controller.on_phase('ChampSelect')
assert controller.observe(flow(game=20), session(game=20)) == dict(mode='ranked', lane='MID')
controller.user_override()
assert controller.observe(flow(game=0), session(game=0)) is None
assert controller.observe(flow(game=20), session(game=20)) is None

# Becoming aware of the game id must not be mistaken for the next draft.
controller.reset()
controller.on_phase('ChampSelect')
assert controller.observe(flow(game=0), session(game=0)) == dict(mode='ranked', lane='MID')
controller.user_override()
assert controller.observe(flow(game=21), session(game=21)) is None and controller.suspended
old_revision = controller.revision
controller.on_phase('InProgress')
controller.on_phase('ChampSelect')
assert controller.observe(flow(game=30), session(game=30), old_revision) is None
assert controller.observe(flow(game=30), session(game=30)) == dict(mode='ranked', lane='MID')

for queue in (400, 420, 430, 440, 490):
    assert draft_target(flow(queue=queue), session())['mode'] == 'ranked'
for queue, mode, map_id in ((450, 'ARAM', 12), (1700, 'CHERRY', 30), (3140, 'PRACTICETOOL', 11),
                            (1090, 'TFT', 22), (9999, 'CLASSIC', 11), (420, 'CLASSIC', 12)):
    assert draft_target(flow(queue=queue, mode=mode, map_id=map_id), session()) is None
assert draft_target(flow(queue=2400, mode='KIWI', map_id=12), session())['mode'] == 'aram_mayhem'
assert draft_target(flow(queue=9999, mode='KIWI', map_id=12), session())['mode'] == 'aram_mayhem'
assert draft_target(flow(game=10), session(game=11)) is None
assert draft_target(flow(queue=420), session(queue=440)) is None
for phase in ('Lobby', 'InProgress', 'EndOfGame', 'None'):
    assert draft_target(flow(phase=phase), session()) is None
for assigned, lane in (('top', 'TOP'), (' JUNGLE ', 'JUNGLE'), ('middle', 'MID'), ('bottom', 'ADC'),
                       ('utility', 'SUPPORT'), ('', None), ('unknown', None)):
    assert draft_target(flow(), session(lane=assigned))['lane'] == lane
missing_local = session()
missing_local['localPlayerCellId'] = 100
assert draft_target(flow(), missing_local)['lane'] is None
assert draft_target(flow(queue=2400, mode='KIWI'), session(lane='middle'))['lane'] is None
assert draft_target(None, session()) is None
assert draft_target(flow(), None) is None

print('PASS: supported modes/own lanes, dedupe, manual suspension before first reply, toggle resume,')
print('new drafts, unknown-id enrichment, completed identities, mismatches, stale epochs and reconnect reset.')
