"""Actual Qt controls and delayed read-only client replies for draft following."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
os.environ['APPDATA'] = tempfile.mkdtemp(prefix='Seraphine-follow-ui-')
sys.path.insert(0, str(ROOT / 'Seraphine'))
os.chdir(ROOT / 'Seraphine')
from app.common.qt_runtime import configure_platform_plugins
configure_platform_plugins()
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication
from qasync import QEventLoop
from app.common.config import cfg
from app.common.signals import signalBus
from app.lol.connector import connector
from app.lol.opgg import opgg
from app.view.opgg_window import OpggWindow


async def verify():
    connector.lcuSess = None
    connector.manager = SimpleNamespace(getChampionNameById=lambda hero: str(hero))
    calls = []
    heroes = dict(TOP=86, JUNGLE=64, MID=1, ADC=22, SUPPORT=40)

    def row(hero, mode=False):
        return dict(championId=hero, name=str(hero), rank=1, tier=1, winRate=.53,
                    pickRate=.1, banRate=.03, counters=[], icon='app/resource/images/champion-0.png', mayhem=mode)

    async def national(region, tier, lane, **kwargs):
        calls.append(('ranked', region, tier, lane))
        rows = [row(heroes[lane])]
        return dict(data=rows, legacyRows=rows, region='cn', enemyId=0, source='rift-analysis',
                    dates={'cn': '20261030'}, version='TEST-' + lane)

    async def tiers(region, mode, tier):
        calls.append((mode, region, tier, None))
        return dict(data=[row(11, True)] if mode == 'aram_mayhem' else
                    {lane: [row(hero)] for lane, hero in heroes.items()},
                    source='tencent-101', statDate='20261030', version='TEST-MAYHEM')

    opgg.getRiftAnalysis, opgg.getTierList = national, tiers
    window = OpggWindow()
    window.setAttribute(Qt.WA_DontShowOnScreen, True)
    window.setMicaEffectEnabled(False)
    window.hextechLive.timer.stop()
    window.setAutoRefreshEnabled(False)
    window.regionComboBox.setCurrentIndex(window.regionComboBox.findData('cn'))
    window.positionComboBox.setCurrentIndex(window.positionComboBox.findData('TOP'))
    await window.initWindow()
    window.show()
    await asyncio.sleep(.15)

    async def until(predicate):
        for _ in range(150):
            await asyncio.sleep(.01)
            if predicate():
                return
        raise AssertionError('UI did not reach the required following state')

    def shown(hero):
        items = window.tierInterface.tierList.items
        return window.stackedWidget.currentWidget() is window.tierInterface and items and items[0].championId == hero

    def flow(game, mode='ranked', queue=None):
        return dict(phase='ChampSelect', gameData=dict(gameId=game,
            map=dict(id=12 if mode == 'aram_mayhem' else 11),
            queue=dict(id=queue if queue is not None else 2400 if mode == 'aram_mayhem' else 420,
                       gameMode='KIWI' if mode == 'aram_mayhem' else 'CLASSIC')))

    def draft(game, lane='middle'):
        return dict(gameId=game, localPlayerCellId=0,
                    myTeam=[dict(cellId=0, championId=0, assignedPosition=lane)], theirTeam=[])

    def observe(game, mode='ranked', lane='middle', queue=None):
        window.onChampSelect(draft(game, lane), flow=flow(game, mode, queue),
                             follow_revision=window.draftFollow.revision)

    def manual_mode(mode):
        window.modeComboBox.setCurrentIndex(window.modeComboBox.findData(mode))

    def manual_lane(lane):
        window.positionComboBox.setCurrentIndex(window.positionComboBox.findData(lane))

    def resume():
        window.followCheck.setChecked(False)
        assert not window.draftFollow.enabled and not cfg.get(cfg.autoFollowDraft)
        window.followCheck.setChecked(True)
        assert window.draftFollow.enabled and cfg.get(cfg.autoFollowDraft)

    assert window.followCheck.isChecked() and window.draftFollow.enabled
    assert shown(86)
    # No confirmed queue, or an unsupported queue, can force the user's mode or lane.
    count = len(calls)
    window.onChampSelect(draft(1, 'middle'))
    await asyncio.sleep(.05)
    assert shown(86) and window.positionComboBox.currentData() == 'TOP' and len(calls) == count, (shown(86), window.positionComboBox.currentData(), calls, count, window.stackedWidget.currentWidget().__class__.__name__)
    observe(1, lane='middle', queue=1090)
    await asyncio.sleep(.05)
    assert shown(86) and window.positionComboBox.currentData() == 'TOP' and len(calls) == count

    window.onGameStatus('ChampSelect')
    observe(10, mode='aram_mayhem')
    await until(lambda: shown(11))
    assert window.modeComboBox.currentData() == 'aram_mayhem'
    before_manual = window.draftFollow.revision
    manual_mode('ranked')
    assert window.draftFollow.suspended, 'Manual control must suspend before async refresh begins'
    await until(lambda: shown(86))
    for _ in range(3):
        observe(10, mode='aram_mayhem')
    window.onChampSelect(draft(10), flow=flow(10, 'aram_mayhem'), follow_revision=before_manual)
    await asyncio.sleep(.08)
    assert shown(86) and window.modeComboBox.currentData() == 'ranked'
    assert window.followCheck.isChecked() and '暂停' in window.followCheck.text()
    resume()
    await until(lambda: shown(11))
    assert not window.draftFollow.suspended

    # The enabled preference persists independently from per-draft manual suspension.
    window.followCheck.setChecked(False)
    other = OpggWindow()
    other.hextechLive.timer.stop()
    assert not other.followCheck.isChecked() and not other.draftFollow.enabled
    other.hextechLive.stop()
    other.hide()
    window.followCheck.setChecked(True)
    await until(lambda: shown(11))

    manual_mode('ranked')
    await until(lambda: shown(86))
    assert window.draftFollow.suspended
    window.onGameStatus('Lobby')
    window.onGameStatus('ChampSelect')
    observe(20, lane='jungle')
    await until(lambda: shown(64))
    assert not window.draftFollow.suspended and window.positionComboBox.currentData() == 'JUNGLE'
    observe(20, lane='middle')
    await until(lambda: shown(1))
    assert window.personalStats.laneCombo.currentData() == 'MID'
    count = len(calls)
    observe(20, lane='middle')
    observe(20, lane='middle')
    await asyncio.sleep(.05)
    assert len(calls) == count
    manual_lane('TOP')
    assert window.draftFollow.suspended
    await until(lambda: shown(86))
    observe(20, lane='middle')
    await asyncio.sleep(.05)
    assert shown(86) and window.positionComboBox.currentData() == 'TOP'
    resume()
    await until(lambda: shown(1))

    # Following can update selectors while the full personal page stays open.
    window.setCurrentInterface(window.personalStats)
    window.onGameStatus('Lobby')
    window.onGameStatus('ChampSelect')
    observe(30, mode='aram_mayhem')
    await asyncio.sleep(.05)
    assert window.stackedWidget.currentWidget() is window.personalStats
    assert window.modeComboBox.currentData() == 'aram_mayhem'
    assert window.personalButton.isVisible(), 'Auto-follow hid the personal page return control'
    await window._OpggWindow__togglePersonalStats.__wrapped__(window)
    await until(lambda: shown(11))

    # A confirmed snapshot recovers a missed ChampSelect WebSocket transition.
    window.onGameStatus('Lobby')
    async def polled_flow(): return flow(35)
    async def polled_session(): return draft(35, 'middle')
    connector.getGameflowSession, connector.getChampSelectSession = polled_flow, polled_session
    connector.lcuSess = object()
    await window.hextechLive.poll()
    await until(lambda: shown(1))
    assert window.draftFollow.phase == 'ChampSelect'
    connector.lcuSess = None
    window.onGameStatus('Lobby')
    async def late_open_flow(): return flow(36, 'aram_mayhem')
    async def late_open_session(): return draft(36)
    connector.getGameflowSession, connector.getChampSelectSession = late_open_flow, late_open_session
    connector.lcuSess = object()
    await window._OpggWindow__loadCurrentBP.__wrapped__(window)
    await until(lambda: shown(11))
    assert window.draftFollow.phase == 'ChampSelect'
    connector.lcuSess = None

    # A manual choice made while the initial async snapshot is pending survives the reply.
    window.onGameStatus('Lobby')
    window.onGameStatus('ChampSelect')
    gate = asyncio.Future()
    async def delayed_flow(): return await gate
    async def current_session(): return draft(40, 'middle')
    connector.getGameflowSession, connector.getChampSelectSession = delayed_flow, current_session
    connector.lcuSess = object()
    task = asyncio.create_task(window._OpggWindow__loadCurrentBP.__wrapped__(window))
    await until(lambda: window.bpLoading)
    manual_mode('ranked')
    manual_lane('TOP')
    assert window.draftFollow.suspended
    gate.set_result(flow(40))
    await task
    await until(lambda: shown(86))
    assert window.positionComboBox.currentData() == 'TOP'
    resume()
    await until(lambda: shown(1))

    # Disconnect invalidates a pending client read and clears personal and follow state.
    gate = asyncio.Future()
    task = asyncio.create_task(window._OpggWindow__loadCurrentBP.__wrapped__(window))
    await until(lambda: window.bpLoading)
    window.personalStats.puuid = 'test-account'
    signalBus.lolClientEnded.emit()
    assert window.currentDraftFlow is None and window.currentDraftSession is None
    assert not window.personalStats.puuid and window.draftFollow.phase is None
    assert not window.draftFollow.suspended
    gate.set_result(flow(40))
    await task
    assert window.currentDraftFlow is None and window.currentDraftSession is None
    connector.lcuSess = None

    # Batched source/rank changes must be suppressed synchronously, not queued
    # until the explicit load re-enables refresh while awaiting the network.
    initial_count = len(calls)
    original_tiers = opgg.getTierList
    async def delayed_tiers(*args):
        await asyncio.sleep(.05)
        return await original_tiers(*args)
    opgg.getTierList = delayed_tiers
    window.setAutoRefreshEnabled(False)
    window.regionComboBox.setCurrentIndex(window.regionComboBox.findData('kr'))
    window.tierComboBox.setCurrentIndex(window.tierComboBox.findData('all'))
    await window.updateAndSwitchTo(window.tierInterface, window.tierInterface)
    assert window.stackedWidget.currentWidget() is window.tierInterface, 'Awaited batch load was invalidated by queued filter callbacks'
    assert len(calls) == initial_count + 1, 'Batched source/rank change caused duplicate requests'
    await asyncio.sleep(.12)
    assert window.stackedWidget.currentWidget() is window.tierInterface
    assert len(calls) == initial_count + 1, 'A delayed filter callback escaped the batch guard'
    opgg.getTierList = original_tiers
    window.hextechLive.stop()
    window.hide()
    print('PASS: default/persisted follow toggle, confirmed mode and own lane, unsupported-flow isolation,')
    print('synchronous manual pause, repeated/late snapshots, toggle resume, next-draft reset, personal-page return,')
    print('GET-only missed-phase/late-open recovery, pending initial read override, repeated-event dedupe, disconnect invalidation,')
    print('and exactly one awaited request after batched source/rank changes.')


app = QApplication([])
loop = QEventLoop(app)
asyncio.set_event_loop(loop)
with loop:
    loop.run_until_complete(verify())
