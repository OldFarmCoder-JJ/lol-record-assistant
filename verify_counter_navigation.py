"""Counter-portrait activation, conservative draft evidence and stale optional reads."""
import asyncio
import copy
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
os.environ['APPDATA'] = tempfile.mkdtemp(prefix='Seraphine-counter-navigation-')
sys.path.insert(0, str(ROOT / 'Seraphine'))
os.chdir(ROOT / 'Seraphine')
from app.common.qt_runtime import configure_platform_plugins
configure_platform_plugins()
from PyQt5.QtCore import Qt, QTranslator, QPoint
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
from qasync import QEventLoop
from app.common.qfluentwidgets import Theme, setTheme
from app.common.signals import signalBus
from app.lol.connector import connector
from app.lol.draft_availability import DraftAvailability
from app.view.opgg_window import OpggWindow


def draft():
    return dict(gameId=123, localPlayerCellId=1,
                myTeam=[dict(cellId=1, championId=64, assignedPosition='jungle')],
                theirTeam=[dict(cellId=6, championId=11)],
                bans={'myTeamBans': [19], 'theirTeamBans': [0, -1]},
                actions=[[dict(type='pick', championId=11, actorCellId=6, completed=True),
                          dict(type='pick', championId=64, actorCellId=1, completed=False)]])


async def verify():
    session = draft()
    state = DraftAvailability.from_session(session, [59, 64, 5])
    assert state.status(19) == 'banned' and state.status(11) == 'picked'
    assert state.status(64) == 'available', 'Hovered hero is not a confirmed pick'
    assert state.status(22) == 'unavailable'
    assert DraftAvailability.from_session(session).status(22) == 'unknown'
    assert DraftAvailability.from_session(session, []).status(22) == 'unknown'
    assert DraftAvailability.from_session(session, [True, 64]).status(22) == 'unknown'
    assert DraftAvailability().status(19) == ''
    assert not DraftAvailability.from_session(None).active
    cancelled = copy.deepcopy(session)
    cancelled['actions'][0].append(dict(type='ban', championId=5, completed=False))
    assert DraftAvailability.from_session(cancelled).status(5) == 'unknown'

    connector.manager = SimpleNamespace(getChampionNameById=lambda hero: str(hero))
    connector.lcuSess = None
    window = OpggWindow()
    window.setMicaEffectEnabled(False)
    window.hextechLive.timer.stop()
    window.draftFollow.set_enabled(False)
    ranking = window.tierInterface.tierList
    icon = 'app/resource/images/champion-0.png'
    counters = [dict(championId=hero, name=name, icon=icon) for hero, name in
                ((19, '祖安怒兽'), (64, '盲僧'), (22, '寒冰射手'))]
    rows = [dict(championId=11, name='无极剑圣', rank=49, tier=1, winRate=.52,
                 pickRate=.1, banRate=.05, counters=counters, icon=icon)]
    ranking.updateList(rows)
    ranking.setAccount('test-account')
    ranking.setDraftAvailability(state)
    window.setCurrentInterface(window.tierInterface)
    window.show()
    await asyncio.sleep(.05)
    item = ranking.items[0]
    requests = []
    # This test observes the real global navigation signal, without triggering
    # online guide reads. Other tests cover mode/lane guide resolution.
    signalBus.toOpggBuildInterface.disconnect()
    signalBus.toOpggBuildInterface.connect(lambda hero, mode, lane: requests.append((hero, mode, lane)))
    try:
        assert [c.draftStatus for c in item.counterIcons] == ['banned', 'available', 'unavailable']
        for index, portrait in enumerate(item.counterIcons):
            QTest.mouseClick(portrait, Qt.LeftButton)
            assert requests[-1] == (counters[index]['championId'], '', '')
            assert len(requests) == index + 1, 'Portrait click must not also open the enemy row'
        portrait = item.counterIcons[0]
        QTest.keyClick(portrait, Qt.Key_Return)
        assert requests[-1][0] == 19 and len(requests) == 4
        QTest.keyClick(portrait, Qt.Key_Space)
        assert len(requests) == 5
        QTest.mousePress(portrait, Qt.LeftButton)
        QTest.mouseRelease(portrait, Qt.LeftButton, pos=QPoint(-4, -4))
        assert len(requests) == 5, 'Dragging outside the portrait must not activate'
        assert '已禁用' in portrait.toolTip() and '点击查看攻略' in portrait.accessibleName()
        # Preference refresh must keep draft evidence; clearing draft keeps favorites.
        ranking.toggleFavorite(19)
        assert portrait.favorite and '已收藏' in portrait.toolTip() and '已禁用' in portrait.toolTip()
        ranking.setDraftAvailability(DraftAvailability())
        assert portrait.favorite and '已禁用' not in portrait.toolTip()
        assert [r.championId for r in ranking.items] == [11] and item.numberLabel.text() == '49'
        ranking.setDraftAvailability(state)
        for theme, suffix in ((Theme.LIGHT, 'light'), (Theme.DARK, 'dark')):
            setTheme(theme, save=False)
            await asyncio.sleep(.08)
            assert portrait.width() == 22 and portrait.height() == 22
            assert item.favoriteButton.geometry().right() < item.championIcon.geometry().left()
            window.grab().save(str(ROOT / f'counter-navigation-{suffix}.png'))
        ranking.updateList([dict(rows[0], mayhem=True)])
        assert all(c.draftStatus == '' for c in ranking.items[0].counterIcons)
        ranking.updateList(rows)
        assert ranking.items[0].counterIcons[0].draftStatus == 'banned', 'Reused rows restore current status'

        # Optional endpoint reads are coalesced, keep explicit bans/picks on
        # failures, and cannot resurrect old status after leave/reconnect.
        connector.lcuSess = object()
        calls = []
        async def pickable():
            calls.append(1)
            return [59, 64]
        connector.getPickableChampionIds = pickable
        window.onChampSelect(session, flow={'phase': 'ChampSelect'})
        task = window._pickableTask
        for _ in range(10):
            window.onChampSelect(copy.deepcopy(session), flow={'phase': 'ChampSelect'})
        assert window._pickableTask is task
        await task
        assert len(calls) == 1 and ranking.draftAvailability.status(64) == 'available'
        gate = asyncio.Future()
        async def slow():
            try:
                return await asyncio.shield(gate)
            except asyncio.CancelledError:
                return await gate  # Simulate a client ignoring cancellation.
        connector.getPickableChampionIds = slow
        changed = copy.deepcopy(session)
        changed['bans']['myTeamBans'].append(59)
        window.onChampSelect(changed, flow={'phase': 'ChampSelect'})
        slow_task = window._pickableTask
        await asyncio.sleep(.18)
        window.onGameStatus('InProgress')
        assert not ranking.draftAvailability.active
        gate.set_result([64])
        await slow_task
        assert not ranking.draftAvailability.active, 'Late reads cannot revive a completed draft'
        async def unsupported(): raise ValueError('unsupported optional endpoint')
        connector.getPickableChampionIds = unsupported
        window.onChampSelect(session, flow={'phase': 'ChampSelect'})
        await window._pickableTask
        assert ranking.draftAvailability.status(19) == 'banned'
        assert ranking.draftAvailability.status(64) == 'unknown'
        window.clearOpponents()
        assert not ranking.draftAvailability.active
        print('PASS: exact counter portrait/keyboard navigation without parent activation, conservative')
        print('ban/pick/hover/unknown states, quiet endpoint failure, event coalescing, stale-read rejection,')
        print('pooled mode restoration, favorite coexistence and portrait-safe light/dark layout.')
    finally:
        connector.lcuSess = None
        window.stop()
        window.hide()


app = QApplication([])
translator = QTranslator()
assert translator.load('app/resource/i18n/Seraphine.zh_CN.qm')
app.installTranslator(translator)
loop = QEventLoop(app)
asyncio.set_event_loop(loop)
with loop:
    loop.run_until_complete(verify())
