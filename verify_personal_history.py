import asyncio
import os
from pathlib import Path
import sys
import tempfile
ROOT=Path(__file__).resolve().parent
os.environ['APPDATA']=tempfile.mkdtemp(prefix='Seraphine-personal-test-')
sys.path.insert(0,str(ROOT/'Seraphine'))
os.chdir(ROOT/'Seraphine')
from app.common.qt_runtime import configure_platform_plugins
configure_platform_plugins()
from PyQt5.QtWidgets import QApplication
from qasync import QEventLoop
from app.lol.personal_history import normalize_game,aggregate,PersonalHistory
from app.view.opgg_window import OpggWindow
from app.lol.connector import connector
from app.common.qfluentwidgets import setTheme,Theme

def game(id,hero=86,win=True,lane='TOP',role='SOLO',queue=420,remake=False,puuid='me'):
    return dict(gameId=id,platformId='TEST',queueId=queue,gameCreation=1791500000000+id*1000,gameDuration=1800,
                participantIdentities=[dict(participantId=2,player=dict(puuid=puuid)),dict(participantId=1,player=dict(puuid='other'))],
                participants=[dict(participantId=1,championId=999,stats=dict(win=False)),
                              dict(participantId=2,championId=hero,timeline=dict(lane=lane,role=role),
                                   stats=dict(win=win,gameEndedInEarlySurrender=remake))])

async def verify():
    games=[game(i,86,i<=3) for i in range(1,6)]+[game(i,10,True) for i in range(6,10)]
    games += [game(i,75, i<12) for i in range(10,15)]
    games += [game(15,86,True,queue=430),game(16,86,True,remake=True),game(17,86,True,lane='NONE')]
    records=[normalize_game(g,'me') for g in games]
    rows=aggregate(records,'TOP')
    assert len(rows)==2 and rows[0]['championId']==86 and rows[0]['games']==5 and rows[0]['wins']==3
    assert all(r['championId']!=10 for r in rows)  # Four perfect games must not pass.
    assert normalize_game(game(18,lane='BOTTOM',role='DUO_SUPPORT'),'me')['lane']=='SUPPORT'
    assert normalize_game(game(19,lane='BOTTOM',role='DUO_CARRY'),'me')['lane']=='ADC'
    assert normalize_game(game(20,lane='BOTTOM',role='DUO'),'me')['lane']=='UNKNOWN'
    assert normalize_game(game(21),'wrong-account') is None
    class Adapter:
        def __init__(self):self.calls=0
        async def getSummonerGamesByPuuid(self,puuid,start,end):
            self.calls+=1
            return {'games':games+[game(100+i,queue=430) for i in range(3)]}  # API repeats the same full page.
        async def getGameDetailByGameId(self,id):return game(id,lane='MIDDLE')
    adapter=Adapter()
    service=PersonalHistory(adapter,tempfile.mkdtemp())
    result=await service.refresh('me')
    assert adapter.calls==2 and len(result['records'])==20 and result['read']==20
    result2=await service.refresh('me')
    assert len(result2['records'])==20 and len(service.cached('other')['records'])==0
    assert next(r for r in result['records'] if r['gameId']==17)['lane']=='MID'
    class Broken:
        async def getSummonerGamesByPuuid(self,*args):raise ValueError('offline')
    service.connector=Broken()
    partial=await service.refresh('me')
    assert len(partial['records'])==20 and partial['warnings']
    class Manager:
        def getChampionNameById(self,hero):return {86:'盖伦',75:'内瑟斯',10:'凯尔'}.get(hero,str(hero))
    connector.manager=Manager()
    window=OpggWindow()
    window.setMicaEffectEnabled(False)
    window.setAutoRefreshEnabled(False)
    window.modeComboBox.blockSignals(True)
    window.regionComboBox.blockSignals(True)
    window.personalStats.data={'records':records}
    window.personalStats.setLane('TOP')
    window.personalStats.render()
    assert window.personalStats.table.rowCount()==2
    assert window.personalStats.table.item(0,3).text()=='60.00%'
    window.personalStats.service=service
    window.personalStats.setAccount({'puuid':'other'})
    assert window.personalStats.table.rowCount()==0
    window.personalStats.data={'records':records}
    window.personalStats.render()
    window.setCurrentInterface(window.personalStats)
    window.show()
    for theme,name in [(Theme.LIGHT,'personal-history-test.png'),(Theme.DARK,'personal-history-test-dark.png')]:
        setTheme(theme,save=False)
        await asyncio.sleep(.2)
        window.grab().save(str(ROOT/name))
        assert window.personalStats.table.width()>500 and window.personalStats.height()>600
    window.personalStats.disconnect()
    assert window.personalStats.table.rowCount()==0
    for task in window.personalStats.iconTasks.values():task.cancel()
    window.hide()
    await asyncio.sleep(.02)
    print('PASS: threshold 4/5, self identity, wins/losses, solo queue/remakes, support/ADC/unknown, repeated pages, merge, partial failure, account isolation, light/dark layout')

app=QApplication([])
loop=QEventLoop(app)
asyncio.set_event_loop(loop)
with loop:loop.run_until_complete(verify())
