"""Recent ranked-game selection, win/loss UI, and account-switch isolation."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
os.environ['APPDATA'] = tempfile.mkdtemp(prefix='Seraphine-recent-test-')
sys.path.insert(0, str(ROOT / 'Seraphine'))
os.chdir(ROOT / 'Seraphine')
from app.common.qt_runtime import configure_platform_plugins
configure_platform_plugins()
from PyQt5.QtCore import Qt, QTranslator
from PyQt5.QtGui import QFontMetrics
from PyQt5.QtWidgets import QApplication, QStyleOptionViewItem, QStyle
from qasync import QEventLoop
from app.common.qfluentwidgets import setTheme, Theme
from app.lol.connector import connector
from app.lol.personal_history import recent_ranked, PersonalHistory
from app.view.opgg_window import OpggWindow


async def verify():
    records = [dict(key=f'TEST:{i}', gameId=i, queue=(420, 440, 430, 2400)[i % 4],
                    champion=86 if i % 4 == 0 else 1, lane='MID' if i % 2 == 0 else 'TOP',
                    date=1793300000000 + i * 3600000, win=i % 3 == 0, remake=False)
               for i in range(1, 81)]
    records[75]['remake'] = True  # Game 76 is a remake, retained as its own result.
    records[72]['win'] = None
    records[76]['lane'] = 'UNKNOWN'
    records.append(dict(records[79]))  # Duplicate page record must not count twice.
    selected = recent_ranked(list(reversed(records)))
    expected = [i for i in range(80, 0, -1) if i % 4 in (0, 1)][:20]
    assert [row['gameId'] for row in selected] == expected
    assert {row['queue'] for row in selected} == {420, 440}
    assert len(recent_ranked([records[0]])) == 1

    def raw_game(row, summary=False):
        player = dict(puuid='me', participantId=1, championId=row['champion'], teamPosition=row['lane'],
                      win=row['win'], gameEndedInEarlySurrender=row['remake'])
        return dict(gameId=row['gameId'], platformId='TEST', queueId=row['queue'], gameCreation=row['date'],
                    gameDuration=1800, participants=[player])

    class LimitedClient:
        inTencent = True
        def __init__(self): self.official_calls = 0
        async def getSummonerGamesByPuuid(self, puuid, start, end):
            return {'games': [raw_game(row) for row in sorted(records[:-1], key=lambda row: -row['date'])[:20]]}
        async def getSummonerGamesByPuuidViaSGP(self, puuid, start, end):
            self.official_calls += 1
            return {'games': [{'json': raw_game(row)} for row in sorted(records[:-1], key=lambda row: -row['date'])[start:end + 1]]}
    adapter = LimitedClient()
    history = PersonalHistory(adapter, tempfile.mkdtemp())
    supplemented = await history.refresh('me')
    assert adapter.official_calls == 1 and supplemented['supplemented']
    assert supplemented['read'] == len(supplemented['records']) == 50
    assert [row['gameId'] for row in recent_ranked(supplemented['records'])] == expected
    assert not any('重复返回' in warning for warning in supplemented['warnings'])
    assert not history.cached('other')['records']
    class OfflineHistory(LimitedClient):
        async def getSummonerGamesByPuuidViaSGP(self, *args): raise ValueError('unavailable')
    unavailable = await PersonalHistory(OfflineHistory(), tempfile.mkdtemp()).refresh('me')
    assert len(unavailable['records']) == 20 and unavailable['warnings']

    connector.manager = SimpleNamespace(getChampionNameById=lambda hero: {86: '德玛西亚之力', 1: '黑暗之女'}.get(hero, str(hero)))
    window = OpggWindow()
    window.setMicaEffectEnabled(False)
    window.hextechLive.timer.stop()
    window.setAutoRefreshEnabled(False)
    stats = window.personalStats
    async def icon(hero): return 'app/resource/images/champion-0.png'
    stats.icon_loader = icon
    stats.data = dict(records=records)
    stats.setLane('MID')
    stats.render()
    assert stats.viewCombo.currentData() == 'recent' and stats.laneCombo.isHidden()
    assert stats.table.rowCount() == 20 and stats.table.columnCount() == 7
    assert stats.table.item(0, 0).data(Qt.UserRole) == 86
    assert stats.table.item(0, 1).text() == '失败'
    results = [stats.table.item(index, 1).text() for index in range(20)]
    assert '重开' in results and '待确认' in results and '胜利' in results
    assert stats.table.item(1, 3).text() == '待确认'  # Unknown lane stays explicit.
    assert stats.table.item(1, 2).text() == '—', 'Old cached KDA must remain unknown'
    assert sum(result == '胜利' for result in results) == sum(row['win'] is True and not row['remake'] for row in selected)
    assert '20 场排位' in stats.scope.text()
    stats.setLane('TOP')
    assert stats.table.rowCount() == 20, 'Recent view must include every lane'
    choices = []
    stats.championChosen.disconnect()
    stats.championChosen.connect(lambda hero, lane: choices.append((hero, lane)))
    stats.chooseChampion(0, 0)
    assert choices[-1] == (86, 'MID'), 'Open the recorded lane rather than the current filter'
    stats.chooseChampion(1, 0)
    assert choices[-1] == (1, 'TOP'), 'Unknown lane falls back to the current lane'
    await asyncio.sleep(.05)

    stats.setLane('MID')
    stats.viewCombo.setCurrentIndex(stats.viewCombo.findData('champions'))
    assert stats.table.columnCount() == 6 and not stats.laneCombo.isHidden()
    assert stats.table.rowCount() > 0
    stats.viewCombo.setCurrentIndex(stats.viewCombo.findData('recent'))
    assert stats.table.rowCount() == 20 and stats.table.columnCount() == 7
    await asyncio.gather(*list(stats.iconTasks.values()))
    window.setCurrentInterface(stats)
    window.show()
    for theme, name in ((Theme.LIGHT, 'recent-ranked-light.png'), (Theme.DARK, 'recent-ranked-dark.png')):
        setTheme(theme, save=False)
        await asyncio.sleep(.2)
        assert stats.table.width() > 500 and stats.height() > 600
        assert not stats.table.item(0, 0).icon().isNull()
        assert stats.refreshButton.geometry().right() < stats.width()
        for col in range(1, 7):
            for row in range(4):
                index = stats.table.model().index(row, col)
                option = QStyleOptionViewItem()
                option.initFrom(stats.table)
                option.rect = stats.table.visualRect(index)
                stats.table.itemDelegate().initStyleOption(option, index)
                text_rect = stats.table.style().subElementRect(QStyle.SE_ItemViewItemText, option, stats.table)
                assert QFontMetrics(option.font).horizontalAdvance(index.data()) <= text_rect.width(), (col, index.data(), text_rect.width())
        window.grab().save(str(ROOT / name))

    # Cached matches from the previous account and a late request must never appear for the next one.
    stats.service = PersonalHistory(connector, tempfile.mkdtemp())
    stats.setAccount(dict(puuid='other'))
    assert stats.table.rowCount() == 0
    gate = asyncio.Future()
    class Delayed:
        def cached(self, puuid): return dict(records=[])
        async def refresh(self, puuid): return await gate
    stats.service = Delayed()
    async def profile(): return dict(puuid='first')
    connector.getCurrentSummoner = profile
    stats.requestRefresh()
    await asyncio.sleep(.05)
    stats.setAccount(dict(puuid='second'))
    gate.set_result(dict(records=records))
    await stats.task
    assert stats.puuid == 'second' and stats.table.rowCount() == 0
    stats.data = dict(records=[records[0]])
    stats.render()
    assert stats.table.rowCount() == 1 and '不足20场' in stats.status.text()
    stats.disconnect()
    assert stats.table.rowCount() == 0
    window.hextechLive.stop()
    window.hide()
    print('PASS: newest 20 ranked matches (solo/flex), mixed modes excluded, duplicates, single-game heroes,')
    print('official-history fallback after repeated LCU pages, deduplication, fallback failure/cache safety,')
    print('win/loss/remake/unknown outcomes, all lanes, recorded-lane drilldown, view switching, icons/themes,')
    print('short history, account switch and late account response isolation.')


app = QApplication([])
translator = QTranslator()
assert translator.load('app/resource/i18n/Seraphine.zh_CN.qm')
app.installTranslator(translator)
loop = QEventLoop(app)
asyncio.set_event_loop(loop)
with loop:
    loop.run_until_complete(verify())
