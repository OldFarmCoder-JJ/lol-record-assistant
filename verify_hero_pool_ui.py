"""Integration of account/history signals, preference icons and the full ranking window."""
import asyncio
import copy
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
os.environ['APPDATA'] = tempfile.mkdtemp(prefix='Seraphine-hero-pool-ui-')
sys.path.insert(0, str(ROOT / 'Seraphine'))
os.chdir(ROOT / 'Seraphine')
from app.common.qt_runtime import configure_platform_plugins
configure_platform_plugins()
from PyQt5.QtCore import QTranslator, Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
from qasync import QEventLoop
from app.common.qfluentwidgets import setTheme, Theme
from app.common.signals import signalBus
from app.lol.champion_preferences import ChampionPreferences
from app.lol.connector import connector
from app.lol.opgg import opgg
from app.view.opgg_window import OpggWindow


async def verify():
    connector.lcuSess = None
    names = {11: '无极剑圣', 64: '盲僧', 19: '祖安怒兽', 59: '德玛西亚皇子'}
    connector.manager = SimpleNamespace(getChampionNameById=lambda hero: names.get(hero, '测试英雄'))
    icon = 'app/resource/images/champion-0.png'
    heroes = [59, *range(100, 147), 11, 64, 19]
    rows = [dict(championId=hero, name=names.get(hero, '测试英雄'), rank=index + 1,
                 tier=1, winRate=.51 + index / 10000, pickRate=.1, banRate=.04,
                 counters=[dict(championId=11, name=names[11], icon=icon),
                           dict(championId=64, name=names[64], icon=icon)], icon=icon)
            for index, hero in enumerate(heroes)]

    async def tiers(region, mode, tier):
        data = ([dict(row, mayhem=True) for row in rows[-3:]] if mode == 'aram_mayhem'
                else {lane: copy.deepcopy(rows) for lane in ('TOP', 'JUNGLE', 'MID', 'ADC', 'SUPPORT')})
        return dict(data=data, version='测试数据', source='fixture')
    opgg.getTierList = tiers

    first = [dict(key='TEST:1', gameId=1, champion=11, queue=420, lane='JUNGLE', win=True, date=1000),
             dict(key='TEST:2', gameId=2, champion=64, queue=2400, lane='UNKNOWN', win=False, date=2000)]
    second = [dict(key='TEST:3', gameId=3, champion=19, queue=440, lane='JUNGLE', win=False, date=3000)]
    third = [dict(key='TEST:4', gameId=4, champion=64, queue=420, lane='JUNGLE', win=True, date=4000),
             dict(key='TEST:5', gameId=5, champion=19, queue=2400, lane='UNKNOWN', win=True, date=5000)]
    class History:
        def cached(self, puuid):
            return dict(records=copy.deepcopy({'first': first, 'second': second, 'third': []}.get(puuid, [])), updated=time.time())
        async def refresh(self, puuid):
            return dict(records=copy.deepcopy(third if puuid == 'third' else first), updated=time.time())

    window = OpggWindow()
    window.setMicaEffectEnabled(False)
    window.hextechLive.timer.stop()
    window.setAutoRefreshEnabled(False)
    window.regionComboBox.setCurrentIndex(window.regionComboBox.findData('kr'))
    window.positionComboBox.setCurrentIndex(window.positionComboBox.findData('JUNGLE'))
    ranking, stats = window.tierInterface.tierList, window.personalStats
    folder = Path(tempfile.mkdtemp(prefix='Seraphine-pool-accounts-'))
    ranking.preferences = ChampionPreferences(folder)
    alternate = ChampionPreferences(folder)
    alternate.set_account('second')
    alternate.toggle(19)
    stats.service = History()
    async def load_icon(hero): return icon
    stats.icon_loader = load_icon
    await window.updateAndSwitchTo(window.tierInterface, window.tierInterface)
    window.show()
    await asyncio.sleep(.1)
    original_height = window.tierInterface.height()
    assert original_height > 650 and len(ranking.items) == 51

    # Normal profile signal supplies the account before its cached history is rendered.
    signalBus.currentSummonerProfileChanged.emit(dict(puuid='first'))
    assert ranking.preferences.account == stats.puuid == 'first'
    assert ranking.preferences.summary(11)['games'] == 1
    assert ranking.preferences.summary(64, True)['games'] == 1
    by_id = {item.championId: item for item in ranking.items}
    assert by_id[11].championIcon.recent and not by_id[64].championIcon.recent
    assert by_id[59].counterIcons[0].recent
    QTest.mouseClick(by_id[11].favoriteButton, Qt.LeftButton)
    assert by_id[11].favorite and by_id[59].counterIcons[0].favorite
    assert [item.championId for item in ranking.items] == heroes
    assert window.tierInterface.height() == original_height

    signalBus.currentSummonerProfileChanged.emit(dict(puuid='second'))
    assert ranking.preferences.account == stats.puuid == 'second'
    assert ranking.preferences.favorites == {19}
    assert ranking.preferences.summary(19)['games'] == 1 and not ranking.preferences.summary(11)
    assert not any(item.championIcon.recent for item in ranking.items if item.championId != 19)
    signalBus.currentSummonerProfileChanged.emit(dict(puuid='first'))
    assert ranking.preferences.favorites == {11} and ranking.preferences.summary(11)

    # Refresh can discover an account without a profile signal. Its historyChanged wiring must still switch safely.
    async def current(): return dict(puuid='third')
    connector.getCurrentSummoner = current
    stats.requestRefresh()
    await stats.task
    assert ranking.preferences.account == stats.puuid == 'third'
    assert not ranking.preferences.favorites
    assert ranking.preferences.summary(64)['games'] == 1
    assert not ranking.preferences.summary(11)
    assert ranking.preferences.summary(19, True)['wins'] == 1

    # Return to the first account for realistic full-window markers in both ranking modes.
    signalBus.currentSummonerProfileChanged.emit(dict(puuid='first'))
    ranking.setOpponents([11])
    assert ranking.items[0].championId == 11 and ranking.items[0].numberLabel.text() == '49'
    for mode in ('ranked', 'aram_mayhem'):
        window.setAutoRefreshEnabled(False)
        window.modeComboBox.setCurrentIndex(window.modeComboBox.findData(mode))
        await window.updateAndSwitchTo(window.tierInterface, window.tierInterface)
        if mode == 'aram_mayhem':
            ranking.setAvailable({11: '当前', 64: '备选席', 19: '备选席'})
            assert [item.championId for item in ranking.items] == [19, 64, 11]
            assert [item.availableRank for item in ranking.items] == [1, 2, 3]
        else:
            assert ranking.items[0].championId == 11 and ranking.items[0].opponent
        for theme, suffix in ((Theme.LIGHT, 'light'), (Theme.DARK, 'dark')):
            setTheme(theme, save=False)
            await asyncio.sleep(.15)
            assert window.width() == 640 and window.tierInterface.height() > 620
            for item in ranking.items[:3]:
                assert item.championIcon.geometry().right() < item.nameWidget.geometry().left()
                assert item.favoriteButton.parent() is item
                assert item.favoriteButton.geometry().right() < item.championIcon.geometry().left()
                assert item.nameLabel.width() >= item.nameLabel.fontMetrics().horizontalAdvance(item.name)
            window.grab().save(str(ROOT / f'hero-pool-{mode}-{suffix}.png'))

    signalBus.lolClientEnded.emit()
    assert stats.puuid == '' and ranking.preferences.account == ''
    assert not ranking.preferences.favorites and not ranking.preferences.summary(11)
    assert not any(item.championIcon.recent or item.favorite or item.favoriteButton.isEnabled() for item in ranking.items)
    window.hextechLive.stop()
    if stats.iconTasks:
        await asyncio.gather(*list(stats.iconTasks.values()))
    window.hide()
    print('PASS: full-window account/cache signal wiring, direct-refresh account discovery, account isolation,')
    print('disconnect clearing, separate favorite controls/counter rings, unchanged ranking/Mayhem order and full-window themes/layout.')


app = QApplication([])
translator = QTranslator()
assert translator.load('app/resource/i18n/Seraphine.zh_CN.qm')
app.installTranslator(translator)
loop = QEventLoop(app)
asyncio.set_event_loop(loop)
with loop:
    loop.run_until_complete(verify())
