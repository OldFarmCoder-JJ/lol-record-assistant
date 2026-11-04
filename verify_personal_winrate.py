"""Factual personal win rates, conservative marks, scope isolation and compact UI."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
SOURCE_DATA = Path(os.environ['APPDATA']) / 'Seraphine'
os.environ['APPDATA'] = tempfile.mkdtemp(prefix='Seraphine-personal-winrate-')
sys.path.insert(0, str(ROOT / 'Seraphine'))
os.chdir(ROOT / 'Seraphine')
from app.common.qt_runtime import configure_platform_plugins
configure_platform_plugins()
from PyQt5.QtCore import Qt, QTranslator
from PyQt5.QtGui import QFontMetrics
from PyQt5.QtWidgets import QApplication, QStyleOptionViewItem, QStyle
from qasync import QEventLoop
from app.common.qfluentwidgets import Theme, setTheme
from app.lol.champion_performance import champion_performance, sort_champions
from app.lol.personal_history import aggregate
from app.lol.connector import connector
from app.view.opgg_window import OpggWindow
from app.view.personal_champions_widget import PERFORMANCE_ROLE, performance_color


def verify_math():
    for wins, losses, label in ((0, 0, '待统计'), (1, 0, '样本少'), (0, 1, '样本少'),
            (4, 0, '样本少'), (5, 0, '观察中'), (9, 0, '观察中'), (10, 0, '强势'),
            (6, 4, '强势'), (11, 9, '不错'), (9, 11, '平稳'), (0, 10, '待提升'),
            (59, 41, '不错'), (54, 46, '平稳'), (44, 56, '待提升')):
        value = champion_performance(wins, losses)
        assert value.label == label and value.games == wins + losses
        assert value.win_rate == (wins / (wins + losses) * 100 if wins + losses else None)
        assert 0 <= value.lower_bound <= 1
    assert champion_performance(1, 0).lower_bound < champion_performance(13, 7).lower_bound


async def verify():
    verify_math()
    names = {64: '盲僧', 11: '无极剑圣', 19: '祖安怒兽', 76: '狂野女猎手',
             876: '含羞蓓蕾', 200: '虚空女皇', 54: '熔岩巨兽', 75: '沙漠死神', 31: '虚空恐惧'}
    connector.manager = SimpleNamespace(getChampionNameById=lambda hero: names.get(hero, str(hero)))
    connector.lcuSess = None
    records = []
    def add(hero, wins, losses, remakes=0, unknown=0, lane='JUNGLE', queue=420):
        for outcome, remake in ([(True, False)] * wins + [(False, False)] * losses +
                                [(True, True)] * remakes + [(None, False)] * unknown):
            count = len(records) + 1
            records.append(dict(key=f'DEMO:{count}', gameId=count, champion=hero, queue=queue,
                lane=lane, win=outcome, remake=remake, date=1793772000000 - count * 60000,
                kills=6, deaths=4, assists=10, duration=1800))
    add(64, 13, 7)
    add(11, 6, 4)
    add(19, 11, 9)
    add(76, 5, 5)
    add(876, 4, 7, remakes=1)
    add(200, 3, 2)
    add(54, 1, 0)
    add(75, 0, 0, remakes=1, unknown=1)
    add(31, 0, 10)
    add(64, 0, 5, lane='MID')
    add(11, 0, 5, queue=440)
    add(54, 10, 0, lane='UNKNOWN', queue=2400)
    rows = aggregate(records, 'JUNGLE', queues=(420,))
    lillia = next(row for row in rows if row['championId'] == 876)
    assert lillia['games'] == 12 and champion_performance(lillia['wins'], lillia['losses']).games == 11
    assert abs(champion_performance(4, 7).win_rate - 36.36363636) < 1e-6
    assert sort_champions(rows, 'advantage')[0]['championId'] == 64
    assert sort_champions(rows, 'winrate')[0]['championId'] == 54
    assert sort_champions(rows, 'winrate')[-1]['championId'] == 75
    assert sort_champions(rows) == rows and len(sort_champions(rows, 'advantage')) == len(rows)
    assert sort_champions(rows[::-1], 'advantage') == sort_champions(rows, 'advantage')
    assert sort_champions(rows, 'recent')[0]['championId'] == 64

    window = OpggWindow()
    window.setMicaEffectEnabled(False)
    if '--compact' in sys.argv:
        window.setFixedHeight(640)
    window.hextechLive.timer.stop()
    window.setAutoRefreshEnabled(False)
    stats = window.personalStats
    async def icon(hero):
        path = SOURCE_DATA / 'cache/game-icons/champion' / f'{hero}.png'
        return str(path) if path.is_file() else 'app/resource/images/champion-0.png'
    stats.icon_loader = icon
    events = []
    stats.historyChanged.connect(lambda values: events.append(values))
    stats.data = dict(records=records, warnings=['界面演示数据，非本人战绩。'])
    stats.puuid = 'demo-account'
    stats.viewCombo.setCurrentIndex(stats.viewCombo.findData('champions'))
    stats.queueCombo.setCurrentIndex(stats.queueCombo.findData('solo'))
    stats.setLane('JUNGLE')
    stats.render()
    window.setCurrentInterface(stats)
    window.personalContext.setText('功能预览 · 模拟战绩')
    window.show()
    try:
        count = len(events)
        for order in ('games', 'winrate', 'advantage', 'recent'):
            stats.sortCombo.setCurrentIndex(stats.sortCombo.findData(order))
            assert stats.table.rowCount() == 9 and len(events) == count
            expected = sort_champions(rows, order)
            assert [stats.table.item(i, 0).data(Qt.UserRole) for i in range(9)] == [r['championId'] for r in expected]
        stats.sortCombo.setCurrentIndex(stats.sortCombo.findData('advantage'))
        assert stats.table.item(0, 3).text() == '65.0% · 强势'
        assert stats.table.item(0, 3).data(Qt.AccessibleTextRole) == '65.0% · 强势'
        assert stats.table.item(8, 3).text() == '— · 待统计'
        assert stats.table.item(0, 4).text() == '6.0 / 4.0 / 10.0'
        assert '重开' in stats.table.item(0, 3).toolTip()
        assert stats.winRateCard.value.text() == f'{43 / 87 * 100:.1f}%'
        for theme, suffix in ((Theme.LIGHT, 'light'), (Theme.DARK, 'dark')):
            setTheme(theme, save=False)
            if stats.iconTasks:
                await asyncio.gather(*list(stats.iconTasks.values()))
            await asyncio.sleep(.15)
            assert stats.table.horizontalScrollBar().maximum() == 0
            assert stats.sortCombo.geometry().right() < stats.width()
            for card in (stats.gamesCard, stats.resultsCard, stats.winRateCard, stats.heroesCard):
                assert card.geometry().right() < stats.width()
                assert QFontMetrics(card.value.font()).horizontalAdvance(card.value.text()) <= card.value.width()
            for column in range(1, stats.table.columnCount()):
                for row in range(stats.table.rowCount()):
                    index = stats.table.model().index(row, column)
                    option = QStyleOptionViewItem()
                    option.initFrom(stats.table)
                    option.rect = stats.table.visualRect(index)
                    stats.table.itemDelegate().initStyleOption(option, index)
                    rect = stats.table.style().subElementRect(QStyle.SE_ItemViewItemText, option, stats.table)
                    assert QFontMetrics(option.font).horizontalAdvance(index.data()) <= rect.width(), (column, index.data(), rect.width())
                    if column == 3:
                        performance = index.data(PERFORMANCE_ROLE)
                        assert index.data(Qt.ForegroundRole).color() == performance_color(performance.tone)
            window.grab().save(str(ROOT / f'personal-winrate-{suffix}.png'))
        # Queue and lane changes recompute rates rather than reusing the old scope.
        stats.setLane('MID')
        assert stats.table.rowCount() == 1 and stats.table.item(0, 3).text() == '0.0% · 观察中'
        stats.setLane('JUNGLE')
        stats.queueCombo.setCurrentIndex(stats.queueCombo.findData('flex'))
        assert stats.table.rowCount() == 1 and stats.table.item(0, 3).text() == '0.0% · 观察中'
        stats.queueCombo.setCurrentIndex(stats.queueCombo.findData('mayhem'))
        assert stats.table.rowCount() == 1 and stats.table.item(0, 3).text() == '100.0% · 强势'
        stats.viewCombo.setCurrentIndex(stats.viewCombo.findData('recent'))
        assert stats.sortCombo.isHidden() and stats.table.columnCount() == 7
        assert stats.table.item(0, 3).data(PERFORMANCE_ROLE) is None
        assert stats.winRateCard.value.text() == '100.0%'
        stats.data = dict(records=[])
        stats.viewCombo.setCurrentIndex(stats.viewCombo.findData('champions'))
        stats.render()
        assert stats.table.rowCount() == 0 and stats.winRateCard.value.text() == '—'
        print('PASS: win-rate arithmetic, thresholds, remakes/unknowns, small samples, conservative sorting,')
        print('all rows retained, queue/lane scopes, accessible text, theme updates, compact layout and empty state.')
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
