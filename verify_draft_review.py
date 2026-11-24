"""End-to-end draft capture, postgame reconciliation and local-only review controls."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

root = Path(__file__).resolve().parent
os.environ['APPDATA'] = tempfile.mkdtemp(prefix='seraphine-bp-review-')
os.chdir(root / 'Seraphine')
sys.path.insert(0, os.getcwd())
from app.common.qt_runtime import configure_platform_plugins
configure_platform_plugins()
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication
from qasync import QEventLoop
from app.lol.connector import connector
from app.view.opgg_window import OpggWindow
from app.lol.draft_review import DraftReview

async def verify():
    w = OpggWindow()
    w.setAttribute(Qt.WA_DontShowOnScreen)
    w.hextechLive.timer.stop()
    w.followCheck.setChecked(False)
    w.setAutoRefreshEnabled(False)
    connector.manager = SimpleNamespace(getChampionNameById=lambda h: str(h))
    w.modeComboBox.setCurrentIndex(w.modeComboBox.findData('ranked'))
    w.onAccount({'puuid':'A'})
    w.showNormal()
    await asyncio.sleep(.03)
    def draft(game, mine, enemy=23):
        flow = dict(phase='ChampSelect',gameData=dict(gameId=game,queue=dict(id=420)))
        ss = dict(gameId=game, localPlayerCellId=0, myTeam=[dict(cellId=0, championId=mine,
                  assignedPosition='top', puuid='A')], theirTeam=[dict(cellId=5, championId=enemy)],
                  actions=[[dict(type='pick', completed=True, actorCellId=5, championId=enemy)]])
        if mine:
            ss['actions'][0].append(dict(type='pick', completed=True, actorCellId=0, championId=mine))
        w.onChampSelect(ss,flow=flow)
    draft(100,0)
    draft(100,0,14)
    ids = [p.hero for p in w._shownPicks]
    assert ids and [p[0] for p in w.review.rows()[0]['picks']] == ids
    draft(100,ids[0],14)
    draft(100,ids[-1],14)
    draft(100,0,23)
    assert w.review.rows()[0]['locked']==ids[0]
    assert [p[0] for p in w.review.rows()[0]['picks']] == ids
    w.onGameStatus('GameStart')
    async def detail(game):
        return dict(gameId=game, gameDuration=1700, queueId=420, participants=[
            dict(puuid='A', championId=ids[-1], stats={'win':True})])
    connector.getGameDetailByGameId=detail
    connector.lcuSess=object()
    w.onGameStatus('EndOfGame')
    await asyncio.wait_for(w.reviewTask,7)
    connector.lcuSess=None
    assert w.review.rows()[0]['rank']==len(ids) and w.review.rows()[0]['result']=='胜'
    draft(101,0)
    draft(101,999)
    draft(102,999)  # Opened after lock: result exists, no reconstructed suggestions.
    draft(103,0)
    draft(103,78)
    w.review.reconcile([dict(gameId=g,queue=420,champion=999,win=False,remake=(g==103)) for g in (101,102,103)])
    w.personalStats.reviewButton.click()
    dlg=w.reviewDialog
    assert dlg.isVisible() and '榜外 0胜 1负' in dlg.summary.text() and '推荐内 1胜 0负' in dlg.summary.text()
    i=next(i for i,row in enumerate(dlg.rows) if row['game']==100)
    dlg.table.selectRow(i)
    dlg.followed.activated.emit(1)
    assert '本人标记参考 1胜 0负' in dlg.summary.text()
    other=DraftReview(w.review.folder)
    other.set_account('B')
    assert not other.rows()
    other.set_account('A')
    assert len(other.rows())==4 and any(r['followed']==1 for r in other.rows())
    w.clearOpponents()
    assert not dlg.rows
    w.stop()
    w.hide()
    print('PASS displayed order, immutable lock, trade, end-of-game sync, manual feedback, missing and remake exclusion, restart and account isolation')

app=QApplication([])
loop=QEventLoop(app)
asyncio.set_event_loop(loop)
with loop:
    loop.run_until_complete(verify())
