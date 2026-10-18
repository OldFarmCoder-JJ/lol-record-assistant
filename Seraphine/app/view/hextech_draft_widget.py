"""Draft-only bench highlighting. Does not create any in-game window."""
import asyncio

from PyQt5.QtCore import QObject, QTimer
from PyQt5.QtWidgets import QWidget, QHBoxLayout
from qasync import asyncSlot

from app.common.qfluentwidgets import BodyLabel, PushButton
from app.common.signals import signalBus
from app.lol.connector import connector
from app.lol.opgg import opgg
from app.lol.hextech_live import is_hextech, draft_choices


class HextechDraftAssistant(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.bar = QWidget()
        layout = QHBoxLayout(self.bar)
        layout.setContentsMargins(4, 0, 4, 0)
        self.label = BodyLabel('选人时标记备选席 · 按胜率置顶 · 前三名金框')
        layout.addWidget(self.label, 1)
        self.rankButton = PushButton('返回排行')
        self.rankButton.clicked.connect(self.showRanking)
        layout.addWidget(self.rankButton)
        self.active = False
        self.phase = ''
        self.choices = {}
        self.revision = 0
        self.pollTask = None
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.schedulePoll)
        self.timer.start()
        signalBus.champSelectChanged.connect(self.onDraft)
        signalBus.gameStatusChanged.connect(self.onPhase)
        signalBus.lolClientEnded.connect(self.reset)
        signalBus.lolClientChanged.connect(lambda _: self.reset())
        window.modeComboBox.currentIndexChanged.connect(self.refreshBar)
        window.stackedWidget.currentChanged.connect(self.refreshBar)
        self.refreshBar()

    def refreshBar(self, *args):
        visible = self.window.modeComboBox.currentData() == 'aram_mayhem'
        self.bar.setVisible(visible)
        self.rankButton.setVisible(self.window.stackedWidget.currentWidget() is not self.window.tierInterface)
        self.window.tierInterface.tierList.setAvailable(self.choices if self.active and visible else {})

    def reset(self):
        self.revision += 1
        self.active = False
        self.phase = ''
        self.choices = {}
        self.label.setText('选人时标记备选席 · 按胜率置顶 · 前三名金框')
        self.refreshBar()

    def onPhase(self, phase):
        self.revision += 1
        if phase != 'ChampSelect':
            self.reset()
        self.phase = phase
        self.schedulePoll()

    def onDraft(self, event):
        self.revision += 1
        if event.get('eventType') == 'Delete':
            self.onPhase('None')
            return
        session = event.get('data', event)
        if not isinstance(session, dict) or 'myTeam' not in session:
            return
        self.phase = 'ChampSelect'
        if self.active:
            self.applyDraft(session)
        else:
            self.schedulePoll()

    def applyDraft(self, session):
        _, self.choices = draft_choices(session)
        self.label.setText(f'备选席 + 当前英雄 {len(self.choices)} 位 · 胜率置顶 · 前三名金框')
        self.label.setToolTip('队友英雄不计入直接可选；换人以客户端实际可操作状态为准。')
        self.refreshBar()

    @asyncSlot()
    async def showRanking(self):
        if self.window.stackedWidget.currentWidget() is self.window.waitingInterface:
            return
        self.rankButton.setEnabled(False)
        try:
            await self.window.updateAndSwitchTo(self.window.tierInterface, self.window.tierInterface)
        finally:
            self.rankButton.setEnabled(True)

    def schedulePoll(self):
        ready = opgg.assetSession is not None and not opgg.assetSession.closed
        if (self.phase in ('', 'ChampSelect') and ready and connector.lcuSess is not None
                and (self.pollTask is None or self.pollTask.done())):
            self.pollTask = asyncio.create_task(self.poll())

    async def poll(self):
        revision = self.revision
        try:
            flow = await asyncio.wait_for(connector.getGameflowSession(), 3)
            if revision != self.revision:
                return
            phase = flow.get('phase', '')
            if phase != 'ChampSelect' or not is_hextech(flow):
                if self.active:
                    self.reset()
                self.phase = phase
                return
            session = await asyncio.wait_for(connector.getChampSelectSession(), 3)
            if revision != self.revision or 'myTeam' not in session:
                return
            self.phase = phase
            self.active = True
            self.applyDraft(session)
        except asyncio.CancelledError:
            return
        except Exception:
            if revision == self.revision:
                self.choices = {}
                self.refreshBar()
                self.label.setText('暂未读到备选席，等待客户端更新')

    def stop(self):
        self.timer.stop()
        if self.pollTask and not self.pollTask.done():
            self.pollTask.cancel()
