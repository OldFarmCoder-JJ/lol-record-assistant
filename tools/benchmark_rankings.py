"""Identical offline UI workload; emits bounded timings and live widget counts."""
import asyncio
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
os.environ['APPDATA'] = tempfile.mkdtemp(prefix='Seraphine-perf-')
sys.path.insert(0, str(ROOT / 'Seraphine'))
os.chdir(ROOT / 'Seraphine')
from app.common.qt_runtime import configure_platform_plugins
configure_platform_plugins()
from PyQt5.QtCore import QCoreApplication, QEvent, Qt, QTimer
from PyQt5.QtWidgets import QApplication
from qasync import QEventLoop
from app.lol.connector import connector
from app.lol.opgg import opgg
from app.view.opgg_window import OpggWindow
from app.view.opgg_tier_interface import ListItem

async def verify():
    connector.lcuSess = None
    icon = 'app/resource/images/champion-0.png'
    def row(hero, mayhem=False):
        return dict(championId=hero, name='测试英雄'+str(hero), rank=hero, tier=hero % 5,
                    winRate=.5 + hero / 10000, pickRate=.1, banRate=.03, icon=icon,
                    counters=[] if mayhem else [dict(championId=i, name='克制英雄', icon=icon) for i in (11, 64, 19)],
                    mayhem=mayhem)
    data = {lane: [row(i) for i in range(1, 56)] for lane in ('TOP','JUNGLE','MID','ADC','SUPPORT')}
    mayhem = [row(i, True) for i in range(1, 171)]
    calls = []
    async def tiers(region, mode, tier):
        calls.append(mode)
        return dict(data=mayhem if mode == 'aram_mayhem' else data, version='fixture')
    opgg.getTierList = tiers
    window = OpggWindow()
    window.setAttribute(Qt.WA_DontShowOnScreen, True)
    window.setMicaEffectEnabled(False)
    window.hextechLive.stop()
    window.setAutoRefreshEnabled(False)
    window.regionComboBox.setCurrentIndex(window.regionComboBox.findData('kr'))
    window.show()
    ticks, last = [], [time.perf_counter()]
    timer = QTimer()
    timer.setInterval(8)
    def tick():
        now = time.perf_counter()
        ticks.append((now - last[0]) * 1000)
        last[0] = now
    timer.timeout.connect(tick)
    timer.start()
    timings, counts = [], []
    for cycle in range(6):
        for mode in ('ranked', 'aram_mayhem'):
            window.setAutoRefreshEnabled(False)
            window.modeComboBox.setCurrentIndex(window.modeComboBox.findData(mode))
            start = time.perf_counter()
            await window.updateAndSwitchTo(window.tierInterface, window.tierInterface)
            await asyncio.sleep(.001)
            ranking = window.tierInterface.tierList
            assert len(ranking.items) == (170 if mode == 'aram_mayhem' else 55), (len(ranking.items), window.rankStatus.text())
            assert ranking.scrollLayout.count() == len(ranking.items)
            assert all(item.parent() is ranking.scrollWidget for item in ranking.items)
            timings.append(dict(cycle=cycle, mode=mode, ms=round((time.perf_counter()-start)*1000,2)))
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        counts.append(len(window.findChildren(ListItem)))
    start=time.perf_counter()
    for _ in range(5):
        window.setCurrentInterface(window.buildInterface)
        await window.showRanking()
    back_ms=(time.perf_counter()-start)*200
    result=dict(timings=timings, live_rows_per_cycle=counts, service_calls=len(calls),
                return_ms=round(back_ms,2), max_event_loop_gap_ms=round(max(ticks, default=0),2),
                median_warm_switch_ms=round(statistics.median(t['ms'] for t in timings[2:]),2))
    timer.stop()
    print(json.dumps(result))
    if len(sys.argv)>1:
        Path(sys.argv[1]).write_text(json.dumps(result,indent=2),encoding='utf-8')
    window.hide()
    window.deleteLater()
    await asyncio.sleep(.02)

app=QApplication([])
loop=QEventLoop(app)
asyncio.set_event_loop(loop)
with loop:
    loop.run_until_complete(verify())
