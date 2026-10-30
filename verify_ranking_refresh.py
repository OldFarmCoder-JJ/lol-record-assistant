"""Real Qt selector/events with deliberately out-of-order network responses."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
os.environ['APPDATA'] = tempfile.mkdtemp(prefix='Seraphine-refresh-test-')
sys.path.insert(0, str(ROOT / 'Seraphine'))
os.chdir(ROOT / 'Seraphine')
from app.common.qt_runtime import configure_platform_plugins
configure_platform_plugins()
from PyQt5.QtWidgets import QApplication
from qasync import QEventLoop
from app.lol.connector import connector
from app.lol.opgg import opgg
from app.view.opgg_window import OpggWindow


async def verify():
    window = OpggWindow()
    window.setMicaEffectEnabled(False)
    window.hextechLive.timer.stop()
    window.setAutoRefreshEnabled(False)
    window.regionComboBox.setCurrentIndex(window.regionComboBox.findData('cn'))
    window.positionComboBox.setCurrentIndex(window.positionComboBox.findData('TOP'))
    connector.lcuSess = None
    calls, gates = [], {}
    lane_ids = dict(TOP=86, JUNGLE=64, MID=1, ADC=22, SUPPORT=40)

    def row(lane, marker, mayhem=False):
        return dict(championId=11 if mayhem else lane_ids[lane], name=marker,
                    rank=1, tier=1, winRate=.54, pickRate=.1, banRate=.04,
                    counters=[], icon='app/resource/images/champion-0.png', mayhem=mayhem)

    async def national(region, tier, lane, **kwargs):
        calls.append(('cn', lane, tier))
        marker = f'CN-{lane}-{len(calls)}'
        data = dict(data=[row(lane, marker)], legacyRows=[row(lane, marker)], region='cn',
                    enemyId=0, version=marker, source='rift-analysis', dates={'cn': '20261030'})
        gate = gates.pop(('cn', lane), None)
        if gate:
            await gate
        return data

    async def tiers(region, mode, tier):
        calls.append((mode, region, tier))
        gate = gates.pop((mode,), None)
        if gate:
            await gate
        marker = f'{mode}-{len(calls)}'
        return dict(version=marker, statDate='20261030', source='tencent-101',
                    data=[row('MID', marker, True)] if mode == 'aram_mayhem' else
                    {lane: [row(lane, marker)] for lane in lane_ids})

    async def no_build(*args, **kwargs):
        raise AssertionError('Switching game modes must load ranking, not the previous champion build')
    opgg.getRiftAnalysis, opgg.getTierList, opgg.getChampionBuild = national, tiers, no_build

    async def until(predicate):
        for _ in range(120):
            await asyncio.sleep(.01)
            if predicate():
                return
        raise AssertionError('GUI did not reach expected state')

    def shown(hero):
        items = window.tierInterface.tierList.items
        return (window.stackedWidget.currentWidget() is window.tierInterface
                and items and items[0].championId == hero)

    def mode(value):
        window.modeComboBox.setCurrentIndex(window.modeComboBox.findData(value))

    def lane(value):
        window.positionComboBox.setCurrentIndex(window.positionComboBox.findData(value))

    def draft(value):
        window.onChampSelect(dict(eventType='Update', data=dict(localPlayerCellId=0,
            myTeam=[dict(cellId=0, championId=0, assignedPosition=value)], theirTeam=[])),
            flow=dict(phase='ChampSelect', gameData=dict(queue=dict(id=420))))

    # First initialization starts from the home page, then real UI signals drive all updates.
    await window.initWindow()
    window.show()
    assert shown(86)
    for _ in range(2):
        mode('aram_mayhem')
        await until(lambda: shown(11))
        mode('ranked')
        await until(lambda: shown(86))
    window.buildInterface.setCurrentChampionId(86)
    window.setCurrentInterface(window.buildInterface)
    mode('aram_mayhem')
    await until(lambda: shown(11))
    mode('ranked')
    await until(lambda: shown(86))

    # A slow old Mayhem response, or an old error, must not overwrite the new Rift view.
    for fail in (False, True):
        gate = asyncio.Future()
        gates[('aram_mayhem',)] = gate
        mode('aram_mayhem')
        await until(lambda: ('aram_mayhem',) not in gates)
        assert window.modeComboBox.isEnabled()
        mode('ranked')
        await until(lambda: shown(86))
        version = window.versionLabel.text()
        gate.set_exception(ValueError('stale network error')) if fail else gate.set_result(None)
        await asyncio.sleep(.08)
        assert shown(86) and window.versionLabel.text() == version

    lane('JUNGLE')
    await until(lambda: shown(64))
    gate = asyncio.Future()
    gates[('cn', 'TOP')] = gate
    lane('TOP')
    await until(lambda: ('cn', 'TOP') not in gates)
    # A real new draft resets prior manual browsing before the assigned lane arrives.
    window.draftFollow.on_phase('Lobby')
    window.draftFollow.on_phase('ChampSelect')
    draft('middle')
    await until(lambda: shown(1))
    assert window.positionComboBox.currentData() == 'MID'
    assert window.personalStats.laneCombo.currentData() == 'MID'
    gate.set_result(None)
    await asyncio.sleep(.08)
    assert shown(1), 'Old TOP response replaced automatically detected MID'
    count = len(calls)
    draft('middle')
    draft('middle')
    await asyncio.sleep(.05)
    assert len(calls) == count, 'Unchanged draft events must not reload the ranking'
    draft('jungle')
    await until(lambda: shown(64))

    # Returning to exactly the same filters still discards an older request (A -> B -> A).
    gate = asyncio.Future()
    gates[('cn', 'TOP')] = gate
    lane('TOP')
    await until(lambda: ('cn', 'TOP') not in gates)
    lane('MID')
    await until(lambda: shown(1))
    lane('TOP')
    await until(lambda: shown(86))
    version = window.versionLabel.text()
    gate.set_result(None)
    await asyncio.sleep(.08)
    assert shown(86) and window.versionLabel.text() == version

    # Source switching also rejects an old overseas response before it populates the cache.
    gate = asyncio.Future()
    gates[('ranked',)] = gate
    window.regionComboBox.setCurrentIndex(window.regionComboBox.findData('kr'))
    await until(lambda: ('ranked',) not in gates)
    window.regionComboBox.setCurrentIndex(window.regionComboBox.findData('cn'))
    await until(lambda: shown(86))
    version = window.versionLabel.text()
    gate.set_result(None)
    await asyncio.sleep(.08)
    assert shown(86) and window.versionLabel.text() == version

    window.setCurrentInterface(window.personalStats)
    mode('aram_mayhem')
    await until(lambda: shown(11))
    draft('middle')
    await asyncio.sleep(.05)
    assert shown(11), 'Reading a lane in Mayhem must not replace its ranking'
    mode('ranked')
    await until(lambda: shown(86))
    window.followCheck.setChecked(False)
    window.followCheck.setChecked(True)
    await until(lambda: shown(1))
    assert window.positionComboBox.currentData() == 'MID'

    # A legitimate champion-build lane fallback must finish, rather than stay on the spinner.
    window.buildInterface.updateInterface = lambda data: None
    async def fallback(*args):
        return dict(data={'summary': {'position': 'TOP'}}, version='BUILD', mode='ranked')
    opgg.getChampionBuild = fallback
    window.buildInterface.setCurrentChampionId(86)
    await window.updateAndSwitchTo(window.tierInterface, window.buildInterface)
    assert window.stackedWidget.currentWidget() is window.buildInterface
    assert window.positionComboBox.currentData() == 'TOP'
    window.hextechLive.stop()
    window.hide()
    print('PASS: actual mode switches from ranking/build/personal, home initialization, slow old replies/errors,')
    print('automatic MID/JUNGLE refresh, repeated-event deduplication, A-B-A responses and return from Mayhem.')


app = QApplication([])
loop = QEventLoop(app)
asyncio.set_event_loop(loop)
with loop:
    loop.run_until_complete(verify())
