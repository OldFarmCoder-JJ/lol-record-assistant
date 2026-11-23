"""Exercise the BP大师 panel via the normal champion-select event entry point."""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

root = Path(__file__).resolve().parent
os.environ['APPDATA'] = tempfile.mkdtemp(prefix='seraphine-draft-master-')
os.chdir(root / 'Seraphine')
sys.path.insert(0, os.getcwd())
from app.common.qt_runtime import configure_platform_plugins
configure_platform_plugins()
from PyQt5.QtWidgets import QApplication
from app.lol.connector import connector
from app.lol.draft_availability import DraftAvailability
from app.view.opgg_window import OpggWindow

app = QApplication([])
window = OpggWindow()
window.hextechLive.timer.stop()
window.followCheck.setChecked(False)
window.setAutoRefreshEnabled(False)
window.modeComboBox.setCurrentIndex(window.modeComboBox.findData('ranked'))
window.positionComboBox.setCurrentIndex(window.positionComboBox.findData('TOP'))
names = {54: '墨菲特', 24: '贾克斯', 17: '提莫', 114: '菲奥娜', 98: '慎'}
connector.manager = SimpleNamespace(getChampionNameById=lambda hero: names.get(hero, str(hero)))
flow = dict(phase='ChampSelect', gameData=dict(gameId=1, queue=dict(id=420, gameMode='CLASSIC')))
local = dict(cellId=0, assignedPosition='top', championId=0)
session = dict(gameId=1, localPlayerCellId=0, myTeam=[local], theirTeam=[], actions=[[]])

def update(enemies, allies=(), hovered=(), banned=()):
    session['theirTeam'] = [dict(cellId=5+i, championId=h) for i, h in enumerate([*enemies, *hovered])]
    session['myTeam'] = [local] + [dict(cellId=i+1, championId=h) for i, h in enumerate(allies)]
    session['actions'] = [[dict(type='pick', actorCellId=p['cellId'], championId=p['championId'],
                                completed=p['championId'] not in hovered)
                           for p in session['theirTeam'] + session['myTeam'] if p['championId']]]
    session['bans'] = dict(myTeamBans=list(banned))
    window.onChampSelect(dict(data=session, eventType='Update'), flow=flow)
    return window.masterLabel.text()

assert '等待' in update([], hovered=[11])
text = update([23, 11, 222], [101, 51])
assert '3名普攻手' in text and '墨菲特' in text and '补前排' in text, text
assert '墨菲特' not in update([23, 11, 222], banned=[54])
tier = window.tierInterface.tierList
tier.setDraftAvailability(DraftAvailability.from_session(session, [24]))
window.refreshDraftMaster()
assert window.masterLabel.text().splitlines()[1:] == ['1. 贾克斯　对面有3名普攻手，可限制普攻输出']
text = update([14, 31, 223])
assert '菲奥娜' in text and '3名前排' in text, text
tier.preferences.favorites.add(114)
window.refreshDraftMaster()
assert '1. 菲奥娜' in window.masterLabel.text() and '已收藏' in window.masterLabel.text()
# Feed the same ranking shape used by the native regional ranking page.
window._displayedTierPayload = {'data': {'TOP': [
    dict(championId=14, name='赛恩', rank=1, counters=[dict(championId=114)]),
    dict(championId=114, name='菲奥娜', rank=2)]}}
window.displayedRankingKey = window._OpggWindow__rankingKey()
text = update([14])
assert '结合分路榜' in text and '若同路可打赛恩' in text and '已收藏' in text, text
local['championId'] = 114
update([14])
assert window.masterLabel.isHidden()
local['championId'] = 0
update([14])
assert not window.masterLabel.isHidden()
window.onChampSelect({'eventType': 'Delete'})
assert window.masterLabel.isHidden()
flow['gameData']['queue']['id'] = 2400
update([14])
assert window.masterLabel.isHidden()
window.stop()
print('PASS draft master: locked teams, rankings, bans, pickable, familiarity, lifecycle and mode')
