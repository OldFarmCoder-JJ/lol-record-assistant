"""Exercise draft events against the actual ranking GUI; no live client mutations."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
os.environ['APPDATA'] = tempfile.mkdtemp(prefix='Seraphine-opponent-test-')
sys.path.insert(0, str(ROOT / 'Seraphine'))
os.chdir(ROOT / 'Seraphine')
from app.common.qt_runtime import configure_platform_plugins
configure_platform_plugins()
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QTranslator
from qasync import QEventLoop
from app.common.qfluentwidgets import setTheme, Theme
from app.common.signals import signalBus
from app.lol.connector import connector
from app.lol.opgg import opgg
from app.view.opgg_window import OpggWindow


class Manager:
    def getChampionNameById(self, hero):
        return {11: '无极剑圣', 64: '盲僧', 19: '祖安怒兽', 59: '德玛西亚皇子'}.get(hero, '测试英雄')


async def verify():
    connector.manager = Manager()
    window = OpggWindow()
    window.setMicaEffectEnabled(False)
    window.setAutoRefreshEnabled(False)
    window.hextechLive.timer.stop()
    ranking = window.tierInterface.tierList
    icon = 'app/resource/images/champion-0.png'
    heroes = [59, *range(100, 147), 11, 64, 19]
    rows = [dict(championId=hero, name=connector.manager.getChampionNameById(hero), rank=index + 1,
                 tier=1, winRate=.51 + index / 10000, pickRate=.1, banRate=.05,
                 counters=[dict(championId=19, name='祖安怒兽', icon=icon),
                           dict(championId=64, name='盲僧', icon=icon)], icon=icon)
            for index, hero in enumerate(heroes)]

    async def forbidden(*args, **kwargs):
        raise AssertionError('Retired matchup/overlay service must not be called')
    opgg.getSelectedMatchup = forbidden
    opgg.hextech = SimpleNamespace(build=forbidden)

    async def tier_data(region, mode, tier):
        data = ([dict(row, mayhem=True) for row in rows[-3:]] if mode == 'aram_mayhem' else
                {lane: [dict(row) for row in rows if lane == 'JUNGLE' or row['championId'] not in (11, 64)]
                 for lane in ('TOP', 'JUNGLE', 'MID', 'ADC', 'SUPPORT')})
        return dict(data=data, version='TEST', statDate='20261018', source='tencent-101')
    opgg.getTierList = tier_data

    async def national(region, tier, position, **kwargs):
        assert not kwargs.get('enemy_id')
        selected = (await tier_data(region, 'ranked', tier))['data'][position]
        return dict(data=selected, legacyRows=selected, region='cn', enemyId=0,
                    version='TEST', source='rift-analysis', dates={'cn': '20261018'})
    opgg.getRiftAnalysis = national

    async def load(region='kr', lane='JUNGLE', mode='ranked'):
        window.setAutoRefreshEnabled(False)
        for box, value in ((window.regionComboBox, region), (window.positionComboBox, lane),
                           (window.modeComboBox, mode)):
            box.setCurrentIndex(box.findData(value))
        await window.updateAndSwitchTo(window.tierInterface, window.tierInterface)
        await asyncio.sleep(.05)

    def draft(hero=11, completed=True):
        return dict(localPlayerCellId=0, myTeam=[dict(cellId=0, championId=0)],
                    theirTeam=[dict(cellId=5, championId=hero)],
                    actions=[[dict(type='pick', actorCellId=5, championId=hero, completed=completed)]])

    await load()
    window.show()
    window.currentDraftFlow = dict(phase='ChampSelect', gameData=dict(queue=dict(id=420)))
    await asyncio.sleep(.15)
    assert len(ranking.items) == 51
    assert ranking.items[0].championId == 59
    window.onChampSelect(dict(eventType='Update', data=draft(completed=False)))
    assert not any(item.opponent for item in ranking.items), 'Hover must not be treated as locked'
    window.onChampSelect(dict(eventType='Update', data=draft()))
    assert ranking.items[0].championId == 11 and ranking.items[0].opponent
    assert ranking.items[0].numberLabel.text() == '49', 'Keep true source rank'
    assert ranking.items[0].opponentLabel.isVisible()
    assert ranking.items[0].countersLayout.itemAt(0).widget().toolTip().startswith('祖安怒兽')
    assert not ranking.titleBar.countersLabel.isHidden()
    assert len(ranking.items) == 51 and ranking.height() > 650
    assert not hasattr(window, 'bpBoard') and not hasattr(window, 'bpBar')
    assigned = draft()
    assigned['myTeam'][0]['assignedPosition'] = 'top'
    window.onChampSelect(assigned)
    await asyncio.sleep(.1)
    assert window.positionComboBox.currentData() == 'TOP'
    assert not any(item.opponent for item in ranking.items)
    await load()

    # Repeated unrelated draft events preserve scrolling; a new lock brings its row into view.
    ranking.scrollArea.verticalScrollBar().setValue(200)
    window.onChampSelect(draft())
    assert ranking.scrollArea.verticalScrollBar().value() == 200
    window.onChampSelect(draft(64))
    assert ranking.items[0].championId == 64 and ranking.scrollArea.verticalScrollBar().value() == 0
    session = draft(11)
    session['theirTeam'].append(dict(cellId=6, championId=64))
    session['actions'][0].append(dict(type='pick', actorCellId=6, championId=64, completed=True))
    window.onChampSelect(session)
    await ranking._TierListWidget__onSortRequested.__wrapped__(ranking, 'winRate')
    assert [item.championId for item in ranking.items[:2]] == [64, 11]
    # New rank responses and lane changes retain only matching highlights, without changing the lane.
    await load('cn')
    assert [item.championId for item in ranking.items[:2]] == [11, 64]
    await load('cn', 'TOP')
    assert not any(item.opponent for item in ranking.items)
    assert window.positionComboBox.currentData() == 'TOP'
    await load('cn')
    for theme, name in ((Theme.LIGHT, 'opponent-ranking-light.png'), (Theme.DARK, 'opponent-ranking-dark.png')):
        setTheme(theme, save=False)
        await asyncio.sleep(.2)
        for item in ranking.items[:3]:
            assert item.nameLabel.width() >= item.nameLabel.fontMetrics().horizontalAdvance(item.name)
            assert item.countersLabel.geometry().right() < item.width()
            assert item.nameLabel.geometry().right() < item.tierLabel.geometry().left()
        window.grab().save(str(ROOT / name))

    # Source mode does not inherit Rift opponents or counters; bench win-rate behavior stays intact.
    await load(mode='aram_mayhem')
    assert [item.championId for item in ranking.items] == [11, 64, 19], 'No enemy pinning before bench is read'
    ranking.setAvailable({11: '当前', 64: '备选', 19: '备选'})
    assert [item.championId for item in ranking.items] == [19, 64, 11]
    assert all(item.availableRank > 0 and not item.opponent for item in ranking.items)
    assert ranking.titleBar.countersLabel.isHidden()
    assert all(item.countersLabel.isHidden() for item in ranking.items)
    await load()
    for phase in ('GameStart', 'InProgress', 'Reconnect', 'EndOfGame', 'Lobby', 'None'):
        window.onChampSelect(draft())
        window.onGameStatus(phase)
        assert not ranking.opponents and not any(item.opponent for item in ranking.items)
    window.onChampSelect(draft())
    signalBus.lolClientEnded.emit()
    assert not ranking.opponents
    window.onChampSelect(draft())
    window.onChampSelect(dict(eventType='Delete'))
    assert not ranking.opponents

    # Opening after an opponent has already locked fetches the present session.
    async def flow(): return dict(phase='ChampSelect')
    async def current(): return draft()
    connector.lcuSess = object()
    connector.getGameflowSession = flow
    connector.getChampSelectSession = current
    await window._OpggWindow__loadCurrentBP.__wrapped__(window)
    assert ranking.items[0].championId == 11 and ranking.items[0].opponent
    connector.lcuSess = None
    window.hextechLive.stop()
    window.hide()
    print('PASS: 51-row ranking, confirmed enemy pinning, true ranks, repeated-event scroll, source/lane changes,')
    print('counter names, light/dark layout, unchanged Mayhem sorting, lifecycle clearing and late-open snapshot.')


app = QApplication([])
translator = QTranslator()
assert translator.load('app/resource/i18n/Seraphine.zh_CN.qm')
app.installTranslator(translator)
loop = QEventLoop(app)
asyncio.set_event_loop(loop)
with loop:
    loop.run_until_complete(verify())
