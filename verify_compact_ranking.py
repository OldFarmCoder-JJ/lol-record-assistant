"""Checks compact page layout and that game phases cannot spawn the removed overlay."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
os.environ['APPDATA'] = tempfile.mkdtemp(prefix='Seraphine-compact-test-')
sys.path.insert(0, str(ROOT / 'Seraphine'))
os.chdir(ROOT / 'Seraphine')
from app.common.qt_runtime import configure_platform_plugins
configure_platform_plugins()
from PyQt5.QtWidgets import QApplication
from qasync import QEventLoop
from app.common.qfluentwidgets import setTheme, Theme
from app.lol.connector import connector
from app.lol.opgg import opgg
from app.view.opgg_window import OpggWindow


class Manager:
    def getChampionNameById(self, hero):
        return {1: '黑暗之女', 86: '德玛西亚之力', 54: '熔岩巨兽'}.get(hero, str(hero))


async def verify():
    connector.manager = Manager()
    window = OpggWindow()
    window.setMicaEffectEnabled(False)
    window.setAutoRefreshEnabled(False)
    live = window.hextechLive
    live.timer.stop()
    assert not hasattr(window, 'bpBoard') and not hasattr(window, 'draftTabs')
    assert not hasattr(window, 'bpBar') and not hasattr(live, 'overlay')
    assert not hasattr(live, 'buildTask')
    async def forbidden(*args, **kwargs):
        raise AssertionError('Counter/overlay data must not be requested')
    opgg.hextech = SimpleNamespace(build=forbidden)
    opgg.getSelectedMatchup = forbidden
    rows = [dict(championId=hero, name=connector.manager.getChampionNameById(hero), rank=index + 1,
                 tier=index, winRate=rate, pickRate=.1, banRate=None, counters=[],
                 icon='app/resource/images/champion-0.png', mayhem=True, source='tencent-101')
            for index, (hero, rate) in enumerate([(1, .51), (86, .55), (54, .53)])]
    async def tier_data(region, mode, tier):
        data = rows if mode == 'aram_mayhem' else {lane: [dict(row, mayhem=False) for row in rows]
                                                   for lane in ('TOP', 'JUNGLE', 'MID', 'ADC', 'SUPPORT')}
        return dict(data=data, version='UI TEST', statDate='20261016', source='tencent-101')
    opgg.getTierList = tier_data
    window.regionComboBox.setCurrentIndex(window.regionComboBox.findData('kr'))
    window.modeComboBox.setCurrentIndex(window.modeComboBox.findData('ranked'))
    await window.updateAndSwitchTo(window.tierInterface, window.tierInterface)
    window.show()
    for theme, name in [(Theme.LIGHT, 'compact-ranking-light.png'), (Theme.DARK, 'compact-ranking-dark.png')]:
        setTheme(theme, save=False)
        await asyncio.sleep(.3)
        assert window.tierInterface.height() > 650, window.tierInterface.height()
        assert window.personalStats.isHidden()
        window.grab().save(str(ROOT / name))
    # The personal page replaces, rather than shares, the ranking area.
    await window._OpggWindow__togglePersonalStats.__wrapped__(window)
    assert window.stackedWidget.currentWidget() is window.personalStats
    assert window.personalStats.height() > 650
    await window._OpggWindow__togglePersonalStats.__wrapped__(window)
    assert window.stackedWidget.currentWidget() is window.tierInterface
    window.modeComboBox.setCurrentIndex(window.modeComboBox.findData('aram_mayhem'))
    await window.updateAndSwitchTo(window.tierInterface, window.tierInterface)
    flow = dict(phase='ChampSelect', gameData=dict(queue=dict(id=2400)))
    session = dict(localPlayerCellId=0, myTeam=[dict(cellId=0, championId=1)], benchEnabled=True,
                   benchChampions=[dict(championId=86), dict(championId=54)])
    async def gameflow(): return flow
    async def draft(): return session
    connector.getGameflowSession = gameflow
    connector.getChampSelectSession = draft
    await live.poll()
    assert [item.championId for item in window.tierInterface.tierList.items] == [86, 54, 1]
    live.onDraft(dict(eventType='Update', data=dict(session, benchChampions=[])))
    assert live.choices == {1: '当前'}
    assert window.tierInterface.height() > 620
    assert live.bar.height() < 45
    await live.poll()
    for phase in ('GameStart', 'InProgress', 'Reconnect', 'EndOfGame', 'Lobby'):
        live.onPhase(phase)
        window.onGameStatus(phase)
        await asyncio.sleep(.01)
        assert not live.active and not live.choices
        assert not any(widget.__class__.__name__ == 'HextechOverlay' for widget in QApplication.topLevelWidgets())
    flow['phase'] = 'ChampSelect'
    await live.poll()
    live.label.setToolTip('模拟选人界面验证；以下是测试数据。')
    await asyncio.sleep(.2)
    window.grab().save(str(ROOT / 'compact-hextech-ranking.png'))
    live.stop()
    window.hide()
    print('PASS: no Counter widgets, >650px ranking, full-page personal stats, retained bench sorting,')
    print('compact draft bar, repeated game phases create no overlay and request no in-game recommendations.')


app = QApplication([])
loop = QEventLoop(app)
asyncio.set_event_loop(loop)
with loop:
    loop.run_until_complete(verify())
