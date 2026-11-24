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
names = {54: '墨菲特', 24: '贾克斯', 17: '提莫', 114: '菲奥娜', 98: '慎', 14: '赛恩', 11: '剑圣', 121: '卡兹克'}
connector.manager = SimpleNamespace(getChampionNameById=lambda hero: names.get(hero, str(hero)))
flow = dict(phase='ChampSelect', gameData=dict(gameId=1, queue=dict(id=420, gameMode='CLASSIC')))
local = dict(cellId=0, assignedPosition='top', championId=0)
session = dict(gameId=1, localPlayerCellId=0, myTeam=[local], theirTeam=[], actions=[[]])

def panel_text():
    return '\n'.join([window.masterLabel.text(), *(
        window.masterList.item(i).text() for i in range(window.masterList.count()))])

def update(enemies, allies=(), hovered=(), banned=()):
    session['theirTeam'] = [dict(cellId=5+i, championId=h) for i, h in enumerate([*enemies, *hovered])]
    session['myTeam'] = [local] + [dict(cellId=i+1, championId=h) for i, h in enumerate(allies)]
    session['actions'] = [[dict(type='pick', actorCellId=p['cellId'], championId=p['championId'],
                                completed=p['championId'] not in hovered)
                           for p in session['theirTeam'] + session['myTeam'] if p['championId']]]
    session['bans'] = dict(myTeamBans=list(banned))
    window.onChampSelect(dict(data=session, eventType='Update'), flow=flow)
    return panel_text()

assert '等待' in update([], hovered=[11])
text = update([23, 11, 222], [101, 51])
assert '3名普攻手' in text and '墨菲特' in text and '补前排' in text, text
assert '墨菲特' not in update([23, 11, 222], banned=[54])
tier = window.tierInterface.tierList
tier.setDraftAvailability(DraftAvailability.from_session(session, [24]))
window.refreshDraftMaster()
assert window.masterList.count() == 1 and '1. 贾克斯' in panel_text() and '3名普攻手' in panel_text()
text = update([14, 31, 223])
assert '菲奥娜' in text and '3名前排' in text, text
tier.preferences.favorites.add(114)
window.refreshDraftMaster()
assert '1. 菲奥娜' in panel_text() and '已收藏' in panel_text()
# Feed the same ranking shape used by the native regional ranking page.
window._displayedTierPayload = {'data': {'TOP': [
    dict(championId=14, name='赛恩', rank=1, counters=[dict(championId=114)]),
    dict(championId=114, name='菲奥娜', rank=2)]}}
window.displayedRankingKey = window._OpggWindow__rankingKey()
text = update([14])
assert '结合分路榜' in text and '同路克制榜可打赛恩' in text and '已收藏' in text, text
local['championId'] = 114
update([14])
assert window.masterLabel.isHidden()
local['championId'] = 0
update([14])
assert not window.masterLabel.isHidden()
window.onChampSelect({'eventType': 'Delete'})
assert window.masterLabel.isHidden()
# A second draft, same window. An enemy jungler can be inferred, corrected,
# or confirmed by the client's assigned role. Direct counters beat comp score.
session['gameId'] = flow['gameData']['gameId'] = 2
local['assignedPosition'] = 'jungle'
window._displayedTierPayload = {'data': {'JUNGLE': [
    dict(championId=11, name='剑圣', rank=1, counters=[dict(championId=254)]),
    dict(championId=254, name='蔚', rank=20),
    dict(championId=33, name='龙龟', rank=2)]}}
text = update([11, 23, 222])
assert not window.masterLabel.isHidden() and '分路推测' in text and '1. 蔚' in text and '打野克制榜可打剑圣' in text, text
text = update([11, 33, 23])
assert '打野待确认' in text
window.enemyJungle.setCurrentIndex(window.enemyJungle.findData(11))
assert '手动指定' in panel_text() and '1. 蔚' in panel_text()
window.enemyJungle.setCurrentIndex(0)
session['theirTeam'][0]['spell1Id'] = 11
window.refreshDraftMaster()
assert '客户端分路或惩戒' in panel_text()
window.onGameStatus('Lobby')
assert window.enemyJungle.isHidden()
flow['gameData']['queue']['id'] = 2400
update([14])
assert window.masterLabel.isHidden()
# Role and matchup evidence are independent of composition heuristics.
from app.lol.draft_master import recommend
from app.lol.champion_preferences import ChampionPreferences
prefs = ChampionPreferences(root)
rows = [dict(championId=h, rank=i, name=names.get(h,str(h)), winRate=wr)
        for i, (h, wr) in enumerate([(14,.526), (33,.502), (121,.510), (54,.508),
                                    (102,.503), (107,.477), (254,.508), (11,.516)], 1)]
def ranked(enemies, allies, pair=None):
    return recommend('JUNGLE', set(enemies), set(allies), DraftAvailability(),
                     prefs, rows, str, 11 if pair else 0, pair)
sion = next(p for p in ranked([99,142,202], [22,101,103]) if p.hero == 14)
assert '补前排' in sion.reason and '打坦' in sion.reason and '52.6%' in sion.stats, sion
assassin = next(p for p in ranked([99,101,22], []) if p.hero == 121)
assert '脆皮' in assassin.reason
malphite = next(p for p in ranked([51,222,119], []) if p.hero == 54)
assert '物理长手' in malphite.reason
with_duel = ranked([11,99,142], [22,101], {254:dict(winRate=.55,play=1200),102:dict(winRate=.99,play=1)})
assert with_duel[0].hero == 254 and '55.0%' in with_duel[0].stats and len(with_duel)>3
local['assignedPosition']='jungle'
flow['gameData']['queue']['id'] = 420
window._displayedTierPayload={'data':{'JUNGLE':rows}}
text = update([99,142,202], [22,101,103])
assert window.masterList.count() > 3 and '52.6%' in text
assert all(not window.masterList.item(i).icon().isNull() for i in range(window.masterList.count()))
# The user's Poppy support game: actual interruptible abilities, not generic mobility.
enemies = {36, 104, 7, 67, 136}
poppy = next(p for p in recommend('SUPPORT', enemies, {233, 11, 127, 51},
                                  DraftAvailability(), prefs, [], str) if p.hero == 78)
assert all(t in poppy.reason for t in ('男枪E', '妖姬W去程', '薇恩Q', '龙王W')), poppy
assert '蒙多' not in poppy.reason
# No W-specific bonus for an enemy team of blinks/walkers.
assert all('W圈内' not in p.reason for p in recommend('SUPPORT', {38,81,36}, {22,11},
                                                       DraftAvailability(), prefs, [], str))
local['championId'] = 78
local['assignedPosition'] = 'utility'
update(list(enemies))
assert not window.ownGuide.isHidden() and '我选' in window.ownGuide.text()
opened = []
window._OpggWindow__toChampionBuildInterface = lambda *args: opened.append(args)
window.ownGuide.click()
assert opened == [(78, 'ranked', 'SUPPORT')]
# A post-lock trade changes the local portrait while the completed pick stays old.
local['championId'] = 89
window.onChampSelect(session, flow=flow)
window.ownGuide.click()
assert opened[-1] == (89, 'ranked', 'SUPPORT')
window.onGameStatus('Lobby')
assert window.ownGuide.isHidden()
window.stop()
print('PASS draft master: locked teams, rankings, bans, pickable, familiarity, lifecycle and mode')
