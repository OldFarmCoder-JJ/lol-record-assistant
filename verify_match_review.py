"""History depth, factual KDA, old-cache compatibility and compact review UI."""
import asyncio
import copy
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
os.environ['APPDATA'] = tempfile.mkdtemp(prefix='Seraphine-match-review-')
sys.path.insert(0, str(ROOT / 'Seraphine'))
os.chdir(ROOT / 'Seraphine')
from app.common.qt_runtime import configure_platform_plugins
configure_platform_plugins()
from PyQt5.QtCore import Qt, QTranslator
from PyQt5.QtGui import QFontMetrics
from PyQt5.QtWidgets import QApplication, QStyleOptionViewItem, QStyle
from qasync import QEventLoop
from app.common.qfluentwidgets import Theme, setTheme
from app.lol.connector import connector
from app.lol.personal_history import PersonalHistory, normalize_game, select_records, aggregate, merge_record
from app.view.personal_champions_widget import combat_text, duration_text
from app.view.opgg_window import OpggWindow


def game(index, queue=420, stats=None):
    return dict(gameId=index, platformId='TEST', queueId=queue,
                gameCreation=1793300000000 + index * 3600000, gameDuration=1867,
                participants=[dict(puuid='me', championId=11 if index % 2 else 64,
                    teamPosition='JUNGLE', **(stats or dict(win=index % 3 == 0, kills=8, deaths=0, assists=12)))])


async def verify():
    raw = game(1)
    normalized = normalize_game(raw, 'me')
    assert combat_text(normalized) == '8 / 0 / 12' and duration_text(normalized['duration']) == '31:07'
    nested = copy.deepcopy(raw)
    nested['participants'][0] = dict(puuid='me', championId=11, stats=dict(win=True, kills=4, deaths=3, assists=7))
    assert combat_text(normalize_game(nested, 'me')) == '4 / 3 / 7'
    assert normalize_game(nested, 'other') is None
    for invalid in (None, -1, '9', True, float('nan'), float('inf')):
        malformed = game(2)
        malformed['participants'][0]['kills'] = invalid
        malformed['gameDuration'] = invalid
        row = normalize_game(malformed, 'me')
        assert row['kills'] is None and row['duration'] is None and combat_text(row) == '—'
    assert duration_text(0) == '—' and duration_text(65) == '01:05'
    assert not select_records([normalized], limit=0)
    restored = merge_record(normalized, dict(normalized, kills=None, deaths=None, assists=None, lane='UNKNOWN', win=None))
    assert restored['kills'] == 8 and restored['lane'] == 'JUNGLE' and restored['win'] == normalized['win']

    raw_games = [game(i, (420, 440, 2400)[i % 3]) for i in range(150, 0, -1)]
    class Adapter:
        inTencent = True
        def __init__(self): self.calls = []
        async def getSummonerGamesByPuuid(self, puuid, start, end):
            return dict(games=copy.deepcopy(raw_games[:20]))  # Repeating-page client.
        async def getSummonerGamesByPuuidViaSGP(self, puuid, start, end):
            self.calls.append((start, end))
            return dict(games=[{'json': copy.deepcopy(g)} for g in raw_games[start:end + 1]])
    adapter = Adapter()
    service = PersonalHistory(adapter, tempfile.mkdtemp())
    data = await service.refresh('me', target=100)
    assert len(adapter.calls) == 3 and len(data['records']) == 150
    assert len(select_records(data['records'], limit=100)) == 100
    assert len(select_records(data['records'], (2400,), limit=100)) == 50
    assert not service.cached('other')['records']
    for count in (20, 50, 100):
        assert len(select_records(data['records'], limit=count)) == count
    mayhem = Adapter()
    mayhem_data = await PersonalHistory(mayhem, tempfile.mkdtemp()).refresh('me', queues=(2400,), target=50)
    assert len(mayhem.calls) == 3 and len(select_records(mayhem_data['records'], (2400,))) == 50
    assert len(select_records(mayhem_data['records'], (2400,), limit=100)) == 50
    # Fewer entries and an offline endpoint retain the full existing account cache.
    class Offline:
        async def getSummonerGamesByPuuid(self, *args): raise ValueError('offline')
    service.connector = Offline()
    offline = await service.refresh('me', target=100)
    assert len(offline['records']) == 150 and offline['warnings']

    records = [dict(normalized, key='one', gameId=1, champion=11),
               dict(normalized, key='remake', gameId=2, champion=11, remake=True, kills=999),
               dict(normalized, key='missing', gameId=3, champion=11, kills=None),
               dict(normalized, key='zero', gameId=4, champion=11, kills=0, deaths=0, assists=0)]
    hero = aggregate(records)[0]
    assert hero['games'] == 4 and hero['combatGames'] == 2
    assert (hero['kills'], hero['deaths'], hero['assists']) == (8, 0, 12)
    assert 'winRate' not in hero

    names = {11: '无极剑圣', 64: '盲僧'}
    connector.lcuSess = None
    connector.manager = SimpleNamespace(getChampionNameById=lambda hero: names.get(hero, str(hero)))
    window = OpggWindow()
    window.setMicaEffectEnabled(False)
    if '--compact' in sys.argv:
        window.setFixedHeight(640)
    window.hextechLive.timer.stop()
    stats = window.personalStats
    async def icon(hero): return 'app/resource/images/champion-0.png'
    stats.icon_loader = icon
    stats.setLane('ALL')
    stats.data = data
    stats.render()
    window.setCurrentInterface(stats)
    window.show()
    try:
        for count in (20, 50, 100):
            stats.recentLimitCombo.setCurrentIndex(stats.recentLimitCombo.findData(count))
            assert stats.table.rowCount() == count and stats.table.columnCount() == 7
            assert stats.gamesCard.value.text() == str(count)
        assert stats.table.item(0, 2).text() == '8 / 0 / 12'
        assert stats.table.item(0, 5).text() == '31:07'
        stats.queueCombo.setCurrentIndex(stats.queueCombo.findData('mayhem'))
        assert stats.table.rowCount() == 50 and '不足100场' in stats.status.text()
        stats.queueCombo.setCurrentIndex(stats.queueCombo.findData('ranked'))
        stats.viewCombo.setCurrentIndex(stats.viewCombo.findData('champions'))
        assert stats.recentLimitCombo.isHidden() and stats.table.columnCount() == 6
        assert stats.table.item(0, 4).text() == '8.0 / 0.0 / 12.0'
        assert '完整KDA' in stats.table.item(0, 4).toolTip()
        for theme, suffix in ((Theme.LIGHT, 'light'), (Theme.DARK, 'dark')):
            setTheme(theme, save=False)
            for view in ('recent', 'champions'):
                stats.viewCombo.setCurrentIndex(stats.viewCombo.findData(view))
                await asyncio.sleep(.1)
                assert stats.refreshButton.geometry().right() < stats.width()
                assert stats.queueCombo.geometry().right() < stats.width()
                assert stats.table.horizontalScrollBar().maximum() == 0, 'Review must fit the compact window'
                for column in range(1, stats.table.columnCount()):
                    for row in range(min(3, stats.table.rowCount())):
                        index = stats.table.model().index(row, column)
                        option = QStyleOptionViewItem()
                        option.initFrom(stats.table)
                        option.rect = stats.table.visualRect(index)
                        stats.table.itemDelegate().initStyleOption(option, index)
                        rect = stats.table.style().subElementRect(QStyle.SE_ItemViewItemText, option, stats.table)
                        assert QFontMetrics(option.font).horizontalAdvance(index.data()) <= rect.width(), (column, index.data(), rect.width())
                window.grab().save(str(ROOT / f'match-review-{view}-{suffix}.png'))
        stats.data = {'records': records}
        stats.render()
        assert stats.table.item(0, 4).text() == '4.0 / 0.0 / 6.0'
        stats.data = {'records': [dict(champion=11, queue=420, win=True, date=1000)]}
        stats.render()
        assert stats.table.item(0, 4).text() == '—'
        # Requests carry the queue/depth selected at request start; switching
        # accounts while a slow read finishes cannot publish the previous user.
        stats.viewCombo.setCurrentIndex(stats.viewCombo.findData('recent'))
        stats.recentLimitCombo.setCurrentIndex(stats.recentLimitCombo.findData(100))
        gate = asyncio.Future()
        requested = []
        class Delayed:
            def cached(self, puuid): return dict(records=[])
            async def refresh(self, puuid, **kwargs):
                requested.append((puuid, kwargs))
                return await gate
        stats.service = Delayed()
        async def profile(): return dict(puuid='first')
        connector.getCurrentSummoner = profile
        stats.requestRefresh()
        await asyncio.sleep(.03)
        assert requested == [('first', {'queues': (420, 440), 'target': 100})]
        stats.setAccount(dict(puuid='second'))
        gate.set_result(data)
        await stats.task
        assert stats.puuid == 'second' and not stats.table.rowCount()
        print('PASS: 20/50/100 scopes, matching-queue official pagination, old/missing/zero KDA, duration,')
        print('deduplication, remake exclusion, honest averages, offline/cache/account safety and compact light/dark UI.')
    finally:
        window.stop()
        await asyncio.gather(*list(stats.iconTasks.values()), return_exceptions=True)
        window.hide()


app = QApplication([])
translator = QTranslator()
assert translator.load('app/resource/i18n/Seraphine.zh_CN.qm')
app.installTranslator(translator)
loop = QEventLoop(app)
asyncio.set_event_loop(loop)
with loop:
    loop.run_until_complete(verify())
