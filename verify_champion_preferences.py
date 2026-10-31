"""Account persistence, factual recent flags and unchanged live ranking order."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
os.environ['APPDATA'] = tempfile.mkdtemp(prefix='Seraphine-preferences-test-')
sys.path.insert(0, str(ROOT / 'Seraphine'))
os.chdir(ROOT / 'Seraphine')
from app.common.qt_runtime import configure_platform_plugins
configure_platform_plugins()
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt, QTranslator
from PyQt5.QtTest import QTest
from qasync import QEventLoop
from app.common.qfluentwidgets import setTheme, Theme
from app.common.signals import signalBus
from app.lol.champion_preferences import ChampionPreferences
from app.view.opgg_tier_interface import TierInterface


async def verify():
    folder = Path(tempfile.mkdtemp(prefix='Seraphine-favorites-store-'))
    store = ChampionPreferences(folder)
    assert not store.toggle(11)
    assert not list(folder.glob('*.json'))
    store.set_account('first/account')
    assert store.toggle(11)
    assert store.path().parent == folder and 'first' not in store.path().name
    assert len(list(folder.glob('*.json'))) == 1
    restarted = ChampionPreferences(folder)
    restarted.set_account('first/account')
    assert restarted.favorites == {11}
    with patch.object(Path, 'replace', side_effect=OSError('read-only storage')):
        try:
            restarted.toggle(19)
        except OSError:
            pass
        else:
            raise AssertionError('A failed write must be observable')
    assert restarted.favorites == {11} and not list(folder.glob('*.tmp'))
    restarted.set_account('second-account')
    assert not restarted.favorites
    restarted.toggle(64)
    restarted.set_account('first/account')
    assert restarted.favorites == {11}

    records = [dict(key='cn:1', gameId=1, champion=11, queue=420, win=True),
               dict(key='cn:2', gameId=2, champion=11, queue=440, win=False),
               dict(key='cn:3', gameId=3, champion=64, queue=2400, win=True),
               dict(key='cn:4', gameId=4, champion=19, queue=430, win=True),
               dict(key='cn:5', gameId=5, champion=19, queue=420, win=False, remake=True),
               dict(key='cn:6', gameId=6, champion=64, queue=420, win=None)]
    records.append(dict(records[0]))
    restarted.set_recent(records)
    assert restarted.summary(11) == dict(games=2, wins=1, losses=1)
    assert restarted.summary(64, True) == dict(games=1, wins=1, losses=0)
    assert restarted.summary(64) == dict(games=1, wins=0, losses=0)
    assert not restarted.summary(11, True) and not restarted.summary(19)
    ancient = dict(key='cn:old', gameId=7, champion=19, queue=420, win=True, date=1)
    newer = [dict(key=f'cn:new-{i}', gameId=100+i, champion=11, queue=420, win=True, date=100+i)
             for i in range(20)]
    restarted.set_recent([ancient, *newer])
    assert not restarted.summary(19) and restarted.summary(11)['games'] == 20
    restarted.set_account('')
    assert not restarted.favorites and not restarted.summary(11)
    restarted.set_recent(records)
    assert not restarted.summary(11)

    view = TierInterface()
    view.resize(615, 700)
    ranking = view.tierList
    ranking.preferences = ChampionPreferences(folder)
    ranking.setAccount(dict(puuid='first/account'))
    ranking.setRecent(records)
    names = {11: '无极剑圣', 64: '盲僧', 19: '祖安怒兽'}
    icon = 'app/resource/images/champion-0.png'
    rows = [dict(championId=hero, name=names[hero], rank=index + 1, tier=1, winRate=rate,
                 pickRate=.1, banRate=.05, icon=icon,
                 counters=[dict(championId=11, name=names[11], icon=icon),
                           dict(championId=64, name=names[64], icon=icon)])
            for index, (hero, rate) in enumerate(((19, .56), (64, .54), (11, .51)))]
    ranking.updateList(rows)
    view.show()
    await asyncio.sleep(.08)
    initial_order = [item.championId for item in ranking.items]
    enemy = ranking.items[0]
    assert enemy.counterIcons[0].favorite and enemy.counterIcons[0].recent
    assert enemy.counterIcons[1].recent and not enemy.counterIcons[1].favorite
    assert '排位 2 场' in enemy.counterIcons[0].toolTip()
    assert '不代表熟练度' in enemy.counterIcons[0].toolTip()
    assert '1 场结果未知' in enemy.counterIcons[1].toolTip()
    selected = next(item for item in ranking.items if item.championId == 11)
    assert selected.favoriteButton.text() == '★' and selected.favoriteButton.focusPolicy() == Qt.StrongFocus
    builds = []
    signalBus.toOpggBuildInterface.connect(lambda *args: builds.append(args))
    QTest.mouseClick(selected.favoriteButton, Qt.LeftButton)
    assert not selected.favorite and selected.favoriteButton.text() == '☆'
    assert not enemy.counterIcons[0].favorite and not builds, 'Star click must not open the row guide'
    selected.favoriteButton.setFocus()
    QTest.keyClick(selected.favoriteButton, Qt.Key_Space)
    assert selected.favorite
    assert [item.championId for item in ranking.items] == initial_order
    reloaded = ChampionPreferences(folder)
    reloaded.set_account('first/account')
    assert reloaded.favorites == {11}

    ranking.setOpponents([11])
    assert ranking.items[0].championId == 11 and ranking.items[0].opponent
    ranking.toggleFavorite(19)
    assert ranking.items[0].championId == 11
    assert ranking.items[0].numberLabel.text() == '3'
    for theme, suffix in ((Theme.LIGHT, 'light'), (Theme.DARK, 'dark')):
        setTheme(theme, save=False)
        await asyncio.sleep(.15)
        assert all(item.width() == 589 for item in ranking.items)
        assert all(item.favoriteButton.parent() is item and
                   item.favoriteButton.geometry().right() < item.championIcon.geometry().left()
                   for item in ranking.items), 'Favorite controls must not overlap portraits'
        view.grab().save(str(ROOT / f'champion-preferences-{suffix}.png'))

    ranking.updateList([dict(row, mayhem=True) for row in rows])
    ranking.setAvailable({11: '当前', 64: '备选席', 19: '备选席'})
    assert [item.championId for item in ranking.items] == [19, 64, 11]
    assert [item.availableRank for item in ranking.items] == [1, 2, 3]
    assert not any(item.opponent for item in ranking.items)
    by_id = {item.championId: item for item in ranking.items}
    assert not by_id[11].championIcon.recent, 'Ranked history must not appear as Mayhem performance'
    assert by_id[64].championIcon.recent and '海斗 1 场' in by_id[64].championIcon.toolTip()
    ranking.toggleFavorite(64)
    assert [item.championId for item in ranking.items] == [19, 64, 11]
    assert [item.availableRank for item in ranking.items] == [1, 2, 3]
    ranking.setAccount(dict(puuid='second-account'))
    assert {item.championId for item in ranking.items if item.favorite} == {64}
    assert not any(item.championIcon.recent for item in ranking.items)
    ranking.setAccount('')
    assert not any(item.favorite or item.favoriteButton.isEnabled() or item.championIcon.recent for item in ranking.items)
    view.hide()
    print('PASS: local favorite restart/account isolation, mouse and keyboard toggles without guide navigation,')
    print('ranked/Mayhem recent summaries, duplicate/remake/unknown-result handling, counter markers,')
    print('unchanged ranking/opponent true ranks and Mayhem win-rate/gold-top3 order, fixed-width themes.')


app = QApplication([])
translator = QTranslator()
assert translator.load('app/resource/i18n/Seraphine.zh_CN.qm')
app.installTranslator(translator)
loop = QEventLoop(app)
asyncio.set_event_loop(loop)
with loop:
    loop.run_until_complete(verify())
