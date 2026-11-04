"""Regression scenarios for fast navigation, interrupted rendering and factual history."""
import asyncio
import copy
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
os.environ['APPDATA'] = tempfile.mkdtemp(prefix='Seraphine-redesign-')
sys.path.insert(0, str(ROOT / 'Seraphine'))
os.chdir(ROOT / 'Seraphine')
from app.common.qt_runtime import configure_platform_plugins
configure_platform_plugins()
from PyQt5.QtCore import Qt, QCoreApplication, QEvent, QTranslator
from PyQt5.QtWidgets import QApplication
from qasync import QEventLoop
from app.common.qfluentwidgets import setTheme, Theme
from app.lol.connector import connector
from app.lol.opgg import opgg
from app.lol.personal_history import aggregate
from app.view.opgg_window import OpggWindow
from app.view.opgg_tier_interface import ListItem

LANES = ('TOP', 'JUNGLE', 'MID', 'ADC', 'SUPPORT')
ICON = 'app/resource/images/champion-0.png'


async def until(predicate):
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(.01)
    raise AssertionError('UI did not reach the expected state')


def select(window, mode='ranked', lane='MID'):
    window.setAutoRefreshEnabled(False)
    for combo, value in ((window.modeComboBox, mode), (window.positionComboBox, lane)):
        combo.setCurrentIndex(combo.findData(value))


async def verify():
    connector.lcuSess = None
    calls, version, gates = [], ['ranking-v1'], {}
    def row(hero, mayhem=False):
        return dict(championId=hero, name='德玛西亚皇子' if hero % 4 == 0 else '测试英雄',
                    rank=hero % 1000 + 1, tier=hero % 5, icon=ICON,
                    winRate=.5 + hero % 100 / 1000, pickRate=.1, banRate=.03,
                    counters=[] if mayhem else [dict(championId=11, name='剑圣', icon=ICON)], mayhem=mayhem)
    rift = {lane: [row(i + 1000 * (index + 1)) for i in range(1, 56)] for index, lane in enumerate(LANES)}
    mayhem = [row(i, True) for i in range(1, 171)]
    async def tiers(region, mode, tier):
        calls.append(mode)
        gate = gates.pop(mode, None)
        if gate is not None:
            await gate
        return dict(data=mayhem if mode == 'aram_mayhem' else rift, version=version[0])
    opgg.getTierList = tiers
    window = OpggWindow()
    window.setAttribute(Qt.WA_DontShowOnScreen, True)
    window.setMicaEffectEnabled(False)
    window.hextechLive.stop()
    window.setAutoRefreshEnabled(False)
    window.regionComboBox.setCurrentIndex(window.regionComboBox.findData('kr'))
    ranking, stats = window.tierInterface.tierList, window.personalStats
    # Icons are intentionally local. No user account/cache/network is used here.
    async def icon(hero): return ICON
    stats.icon_loader = icon
    window.show()
    try:
        await window.updateAndSwitchTo(window.tierInterface, window.tierInterface)
        original_items = tuple(ranking.items)
        ranking.scrollArea.verticalScrollBar().setValue(200)
        scroll = ranking.scrollArea.verticalScrollBar().value()
        original_get = window.rankingCache.get
        def unexpected(*args, **kwargs):
            raise AssertionError('Returning to a fresh view must not read disk or call the ranking service')
        window.rankingCache.get = unexpected
        opgg.getTierList = unexpected
        for _ in range(25):
            window.setCurrentInterface(window.buildInterface)
            window.versionLabel.setText('build-version')
            await window.showRanking()
            assert tuple(ranking.items) == original_items
            assert ranking.scrollArea.verticalScrollBar().value() == scroll
            assert 'ranking-v1' in window.versionLabel.text()
        window.rankingCache.get, opgg.getTierList = original_get, tiers

        # A slow build response cannot steal the screen after a fast return.
        build_gate = asyncio.Future()
        async def build(*args):
            await build_gate
            return dict(data={'summary': {'position': 'MID'}}, version='build-v1')
        opgg.getChampionBuild = build
        window.buildInterface.updateInterface = lambda data: None
        window.buildInterface.setCurrentChampionId(3001)
        pending = asyncio.create_task(window.updateAndSwitchTo(window.tierInterface, window.buildInterface))
        await asyncio.sleep(.02)
        await window.showRanking()
        build_gate.set_result(None)
        await pending
        assert window.stackedWidget.currentWidget() is window.tierInterface

        # Navigation after expiry must refresh. Identical rows still reuse widgets.
        count = len(calls)
        window.rankingLoadedAt = 0
        version[0] = 'ranking-v2'
        await window.showRanking()
        assert len(calls) == count + 1 and 'ranking-v2' in window.versionLabel.text()
        assert tuple(ranking.items) == original_items

        # Interrupt creation of a cold 170-row view. Staged old rows must be destroyed.
        select(window, 'aram_mayhem')
        cold = asyncio.create_task(window.updateAndSwitchTo(window.tierInterface, window.tierInterface))
        await until(lambda: len(window.findChildren(ListItem)) > 60)
        assert not cold.done(), 'Cold rendering must yield to new input before all rows are built'
        assert window.stackedWidget.currentWidget() is window.waitingInterface, 'Old-mode rows must not masquerade as the new mode while preparing'
        select(window, lane='TOP')
        await window.updateAndSwitchTo(window.tierInterface, window.tierInterface)
        await cold
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        assert all(not item.info['mayhem'] for item in ranking.items)
        assert len(window.findChildren(ListItem)) == len(ranking._rowPool)

        # Cross enough datasets to force eviction; no retained rows grow without bound.
        for _ in range(2):
            for lane in LANES:
                select(window, lane=lane)
                await window.updateAndSwitchTo(window.tierInterface, window.tierInterface)
            select(window, 'aram_mayhem')
            await window.updateAndSwitchTo(window.tierInterface, window.tierInterface)
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            assert len(ranking._rowPool) <= ranking.maxCachedRows
            assert len(window.findChildren(ListItem)) == len(ranking._rowPool)
        ranking.setAvailable({1: '当前', 2: '备选席', 3: '备选席'})
        assert [item.championId for item in ranking.items[:3]] == [3, 2, 1]
        assert [item.availableRank for item in ranking.items[:3]] == [1, 2, 3]
        ranking.setAccount('test-account')
        ranking.toggleFavorite(2)
        for theme in (Theme.LIGHT, Theme.DARK):
            setTheme(theme, save=False)
            await asyncio.sleep(.05)
            for item in ranking.items[:4]:
                assert not item.favoriteButton.geometry().intersects(item.championIcon.geometry())
                assert item.favoriteButton.parent() is item
                assert item.nameWidget.geometry().right() < item.tierLabel.geometry().left()
                assert item.countersLabel.geometry().right() < item.width()

        # Reopening the same Mayhem guide from a different mode must restore its
        # mode and metadata while reusing the already rendered guide.
        build_calls = []
        async def ready_build(region, mode, hero, position, tier):
            build_calls.append((mode, hero, position, tier))
            return dict(data={'championId': hero, 'summary': {'position': position}}, version='guide-version')
        opgg.getChampionBuild = ready_build
        await window._OpggWindow__toChampionBuildInterface.__wrapped__(window, 2, 'aram_mayhem', 'none')
        assert build_calls[-1] == ('aram_mayhem', 2, 'none', 'all')
        select(window)
        await window.updateAndSwitchTo(window.tierInterface, window.tierInterface)
        build_count = len(build_calls)
        await window._OpggWindow__toChampionBuildInterface.__wrapped__(window, 2, 'aram_mayhem', 'none')
        assert len(build_calls) == build_count
        assert window.modeComboBox.currentData() == 'aram_mayhem' and 'guide-version' in window.versionLabel.text()
        assert window.positionComboBox.currentData() == 'MID', 'Mayhem must preserve the last valid Rift lane'

        # Factual stats: one-game heroes, both ranked queues, remakes and unknowns.
        records = [dict(key=str(i), gameId=i, date=1793400000000 + i * 3600000,
                        queue=queue, champion=hero, win=win, remake=remake, lane=lane)
                   for i, (hero, queue, win, remake, lane) in enumerate((
                       (11, 420, True, False, 'MID'), (64, 440, False, False, 'MID'),
                       (86, 420, None, False, 'UNKNOWN'), (40, 420, True, True, 'MID'),
                       (11, 420, False, False, 'TOP'), (19, 2400, True, False, 'UNKNOWN'),
                       (19, 2400, False, False, 'UNKNOWN'), (1, 2400, True, True, 'UNKNOWN'),
                       (22, 430, True, False, 'ADC')), 1)]
        records.append(copy.deepcopy(records[0]))
        rows = aggregate(records)
        assert len(rows) == 4 and rows[0]['championId'] == 11 and rows[0]['games'] == 2
        assert all('winRate' not in entry for entry in rows)
        assert next(r for r in rows if r['championId'] == 40)['remakes'] == 1
        assert next(r for r in rows if r['championId'] == 86)['unknown'] == 1
        notifications = []
        stats.historyChanged.connect(lambda values: notifications.append(values))
        stats.data = dict(records=records, updated=time.time())
        stats.puuid = 'test-account'
        stats.render()
        count = len(notifications)
        stats.viewCombo.setCurrentIndex(stats.viewCombo.findData('champions'))
        stats.laneCombo.setCurrentIndex(stats.laneCombo.findData('ALL'))
        assert stats.table.rowCount() == 4
        assert stats.gamesCard.value.text() == '5' and stats.resultsCard.value.text() == '1 / 2'
        assert '1 重开' in stats.scope.text() and '1 场结果待确认' in stats.scope.text()
        assert len(notifications) == count, 'Changing a stats filter must not republish global history'
        assert window.positionComboBox.currentData() in LANES
        assert stats.table.item(0, 3).text() == '50.0% · 样本少'
        assert stats.winRateCard.value.text() == '33.3%'
        stats.setLane('MID')
        assert stats.table.rowCount() == 3
        stats.queueCombo.setCurrentIndex(stats.queueCombo.findData('mayhem'))
        assert stats.laneCombo.isHidden() and stats.table.rowCount() == 2
        assert stats.gamesCard.value.text() == '3' and stats.resultsCard.value.text() == '1 / 1'
        stats.viewCombo.setCurrentIndex(stats.viewCombo.findData('recent'))
        assert stats.table.rowCount() == 3 and '海斗' in stats.scope.text()
        assert '重开' in [stats.table.item(r, 1).text() for r in range(3)]

        # Late current-summoner reads cannot revert a freshly selected account.
        class History:
            def cached(self, puuid): return dict(records=[], updated=time.time())
            async def refresh(self, puuid):
                raise AssertionError('Old/missing account profiles must not start history queries')
        stats.service = History()
        profile_gate = asyncio.Future()
        async def delayed_profile(): return await profile_gate
        connector.getCurrentSummoner = delayed_profile
        stats.requestRefresh()
        await asyncio.sleep(.02)
        stats.setAccount(dict(puuid='new-account'))
        profile_gate.set_result(dict(puuid='old-account'))
        await stats.task
        assert stats.puuid == 'new-account' and stats.table.rowCount() == 0
        async def missing_profile(): return {}
        connector.getCurrentSummoner = missing_profile
        stats.requestRefresh()
        await stats.task
        assert stats.puuid == 'new-account' and '读取失败' in stats.status.text()
        assert stats.emptyLabel.isVisible() is (window.stackedWidget.currentWidget() is stats)
        print('PASS: 25 navigation returns without disk/network/row rebuilding; retained scroll/version,')
        print('late build rejection, expiry refresh, interrupted cold rendering, bounded row pool/eviction,')
        print('portrait-safe favorites, Mayhem top3, factual one-game hero stats, ranked/Mayhem/lane scopes,')
        print('deduplication, remakes/unknowns, history notification dedupe and late/missing account protection.')
    finally:
        window.stop()
        tasks = list(window._loadTasks) + list(stats.iconTasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        window.hide()
        window.deleteLater()
        await asyncio.sleep(.02)


app = QApplication([])
translator = QTranslator()
assert translator.load('app/resource/i18n/Seraphine.zh_CN.qm')
app.installTranslator(translator)
loop = QEventLoop(app)
asyncio.set_event_loop(loop)
with loop:
    loop.run_until_complete(verify())
