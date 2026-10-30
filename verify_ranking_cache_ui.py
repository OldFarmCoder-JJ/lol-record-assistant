"""Offline ranking UI: exact disk cache, stale failures, retries and late responses."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
os.environ['APPDATA'] = tempfile.mkdtemp(prefix='Seraphine-cache-ui-')
sys.path.insert(0, str(ROOT / 'Seraphine'))
os.chdir(ROOT / 'Seraphine')
from app.common.qt_runtime import configure_platform_plugins
configure_platform_plugins()
from PyQt5.QtCore import Qt, QTranslator
from PyQt5.QtWidgets import QApplication
from qasync import QEventLoop
from app.lol.connector import connector
from app.lol.opgg import opgg
from app.view.opgg_window import OpggWindow


async def verify():
    connector.lcuSess = None
    icon = 'app/resource/images/champion-0.png'
    lane_ids = dict(TOP=86, JUNGLE=64, MID=1, ADC=22, SUPPORT=40)
    windows, calls, gates = [], [], {}
    online = True
    generation = 'first'
    malformed = False

    def row(lane, marker, mayhem=False):
        return dict(championId=11 if mayhem else lane_ids[lane], name=marker,
                    icon=icon, rank=1, tier=1, winRate=.54, pickRate=.1, banRate=.04,
                    counters=[], mayhem=mayhem)

    async def national(region, tier, lane, **rules):
        identity = ('ranked', region, tier, lane)
        calls.append(identity)
        marker = f'{region}-{lane}-{generation}'
        values = [row(lane, marker)]
        data = dict(data=values, legacyRows=values, region=region, position=lane,
                    source='rift-analysis', version=marker, dates={'cn': '20261030'},
                    enemyId=0, enemyName='', fraction=rules.get('fraction', .3),
                    onlyStrong=rules.get('only_strong', True), strict=rules.get('strict', False),
                    unavailable={}, notes=[], sources=['cn'], matchupDates={})
        if region == 'consensus':
            data.update(sources=['cn', 'global', 'kr'], dates={source: '20261030' for source in ('cn', 'global', 'kr')})
            for item in values:
                item.update(evidence={source: dict(rank=1, tier=1, tierText='T1', winRate=.54,
                            pickRate=.1, banRate=.04, play=1500) for source in ('cn', 'global', 'kr')},
                            commonStrong=True, matchups={}, supported=[])
        if malformed and region == 'cn':
            data['legacyRows'][0].pop('tier')
        gate = gates.pop(identity, None)
        if gate is not None:
            await gate
        elif not online:
            raise ValueError('offline fixture')
        return data

    async def tiers(region, mode, tier):
        identity = (mode, region, tier)
        calls.append(identity)
        marker = f'{mode}-{region}-{generation}'
        data = dict(version=marker, statDate='20261030', source='tencent-101',
                    data=[row('MID', marker, True)] if mode == 'aram_mayhem' else
                    {lane: [row(lane, marker)] for lane in lane_ids})
        gate = gates.pop(identity, None)
        if gate is not None:
            await gate
        elif not online:
            raise ValueError('offline fixture')
        return data

    opgg.getRiftAnalysis, opgg.getTierList = national, tiers

    def select(window, *, mode='ranked', region='cn', tier='all', lane='MID'):
        window.setAutoRefreshEnabled(False)
        for box, value in ((window.modeComboBox, mode), (window.regionComboBox, region),
                           (window.tierComboBox, tier), (window.positionComboBox, lane)):
            box.setCurrentIndex(box.findData(value))

    def create():
        window = OpggWindow()
        windows.append(window)
        window.setAttribute(Qt.WA_DontShowOnScreen, True)
        window.setMicaEffectEnabled(False)
        window.hextechLive.timer.stop()
        select(window)
        return window

    async def load(window):
        await window.updateAndSwitchTo(window.tierInterface, window.tierInterface)
        await asyncio.sleep(.02)

    async def until(predicate):
        for _ in range(150):
            await asyncio.sleep(.01)
            if predicate():
                return
        raise AssertionError('cache UI did not reach expected state')

    def shown(window, marker):
        items = window.tierInterface.tierList.items
        return (window.stackedWidget.currentWidget() is window.tierInterface and items
                and items[0].info['name'] == marker)

    first = create()
    await load(first)
    assert shown(first, 'cn-MID-first')
    original_version = first.versionLabel.text()
    snapshots = list(first.rankingCache.folder.glob('*.json'))
    assert snapshots, 'Successful live rankings were not persisted'
    first.show()
    await asyncio.sleep(.04)
    assert first.tierInterface.height() > 650, first.tierInterface.height()

    # Same filters show their saved ranking while a network request is still pending.
    gate = asyncio.Future()
    gates[('ranked', 'cn', 'all', 'MID')] = gate
    pending = asyncio.create_task(first.updateAndSwitchTo(first.tierInterface, first.tierInterface))
    await until(lambda: ('ranked', 'cn', 'all', 'MID') not in gates)
    assert not pending.done() and shown(first, 'cn-MID-first')
    assert '缓存' in first.rankStatus.text()
    gate.set_exception(ValueError('offline fixture'))
    await pending
    assert shown(first, 'cn-MID-first') and '暂用缓存' in first.rankStatus.text()
    assert first.versionLabel.text() == original_version

    # A bad live payload must neither replace the good disk snapshot nor leave
    # half-rendered new metadata behind under a cached-status message.
    online, generation, malformed = True, 'broken-payload', True
    await load(first)
    assert shown(first, 'cn-MID-first') and first.versionLabel.text() == original_version
    assert first.tierInterface.tierList.scrollLayout.count() > 0
    cache_key = first.rankingCache.make_key('ranked', 'cn', 'all', 'MID')
    assert first.rankingCache.get(cache_key)['payload']['version'] == 'cn-MID-first'
    malformed = False

    # A fresh window must load disk snapshots, not just retain old widgets in memory.
    online = False
    reopened = create()
    await load(reopened)
    assert shown(reopened, 'cn-MID-first') and '暂用缓存' in reopened.rankStatus.text()
    for options in (dict(lane='TOP'), dict(region='kr'), dict(tier='gold_plus'), dict(mode='aram_mayhem')):
        select(reopened, **options)
        await load(reopened)
        assert reopened.stackedWidget.currentWidget() is reopened.errorInterface, options
        assert '暂用缓存' not in reopened.rankStatus.text(), options
    select(reopened)
    await load(reopened)
    assert shown(reopened, 'cn-MID-first')

    # Retry is an actual button click and must replace the cached snapshot with live data.
    online, generation = True, 'recovered'
    reopened.retryRankingButton.click()
    await until(lambda: shown(reopened, 'cn-MID-recovered'))
    assert '暂用缓存' not in reopened.rankStatus.text()
    assert reopened.versionLabel.text() != original_version
    online = False
    third = create()
    await load(third)
    assert shown(third, 'cn-MID-recovered')

    # Seed a different mode, then prove its cache cannot overwrite a newer lane selection.
    online, generation = True, 'mayhem'
    select(third, mode='aram_mayhem')
    await load(third)
    assert shown(third, 'aram_mayhem-cn-mayhem')
    gate = asyncio.Future()
    gates[('aram_mayhem', 'cn', 'all')] = gate
    delayed = asyncio.create_task(third.updateAndSwitchTo(third.tierInterface, third.tierInterface))
    await until(lambda: ('aram_mayhem', 'cn', 'all') not in gates)
    assert shown(third, 'aram_mayhem-cn-mayhem')
    generation = 'new-mid'
    select(third)
    await load(third)
    assert shown(third, 'cn-MID-new-mid')
    version = third.versionLabel.text()
    gate.set_result(None)
    await delayed
    assert shown(third, 'cn-MID-new-mid') and third.versionLabel.text() == version

    # Consensus controls are part of the key, not a source-wide fallback.
    select(third, region='consensus')
    await load(third)
    assert third.stackedWidget.currentWidget() is third.tierInterface
    online = False
    third.setAutoRefreshEnabled(False)
    panel = third.tierInterface.analysis
    panel.fraction.setCurrentIndex(panel.fraction.findData(.2))
    await load(third)
    assert third.stackedWidget.currentWidget() is third.errorInterface
    assert '暂用缓存' not in third.rankStatus.text()

    # Cached payloads preserve the independent source's version/date and labels after retry.
    online = False
    select(first)
    await load(first)
    assert shown(first, 'cn-MID-new-mid')
    assert '20261030' in first.versionLabel.toolTip()
    first.grab().save(str(ROOT / 'ranking-cache-offline.png'))
    for window in windows:
        window.hextechLive.stop()
        window.hide()
    print('PASS: live-to-disk-to-new-window offline cache, immediate cached ranking during requests,')
    print('mode/source/lane/tier/consensus-rule separation, preserved version/date, failed/malformed refresh retention,')
    print('button retry recovery, late response rejection and >650px ranking area.')


app = QApplication([])
translator = QTranslator()
assert translator.load('app/resource/i18n/Seraphine.zh_CN.qm')
app.installTranslator(translator)
loop = QEventLoop(app)
asyncio.set_event_loop(loop)
with loop:
    loop.run_until_complete(verify())
