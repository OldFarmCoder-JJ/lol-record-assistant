"""Read-only draft assistance and a normal, topmost in-game reference window."""
import asyncio
import re
import os
from qasync import asyncSlot

from PyQt5.QtCore import QObject, Qt, QTimer, QSize
from PyQt5.QtGui import QColor, QIcon, QPixmap
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                             QCheckBox, QComboBox, QCompleter, QTableWidget,
                             QTableWidgetItem, QHeaderView, QAbstractItemView)

from app.common.signals import signalBus
from app.common.qfluentwidgets import BodyLabel, CheckBox, PushButton
from app.lol.connector import connector
from app.lol.opgg import opgg
from app.lol.hextech_live import is_hextech, draft_choices, augment_order, game_champion, reroll_advice

PLAYING = ('GameStart', 'InProgress', 'Reconnect')
TERMINAL = ('None', 'Lobby', 'Matchmaking', 'ReadyCheck', 'PreEndOfGame',
            'EndOfGame', 'WaitingForStats', 'TerminatedInError')


class HextechOverlay(QWidget):
    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setWindowTitle('海克斯推荐 · 腾讯 101')
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.resize(540, 620)
        self.setMinimumSize(440, 340)
        self.setStyleSheet('QWidget { background: #171e2c; color: #e8edf7; font-size: 13px; } '
                           'QComboBox, QPushButton { background: #28364b; border: 1px solid #425573; '
                           'border-radius: 5px; padding: 5px; } '
                           'QLineEdit { background: #28364b; } '
                           'QHeaderView::section { background: #28364b; padding: 5px; border: none; } '
                           'QTableWidget { gridline-color: #34435c; }')
        layout = QVBoxLayout(self)
        self.title = QLabel('等待当前英雄')
        self.title.setStyleSheet('font-size: 19px; font-weight: 700; color: #70e1cd;')
        self.note = QLabel('')
        self.note.setWordWrap(True)
        layout.addWidget(self.title)
        layout.addWidget(self.note)
        hint = QLabel('三个候选需手动输入或选择；留空时显示该英雄推荐榜。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.choices = []
        self.candidateIcons = []
        for index in range(3):
            candidate = QHBoxLayout()
            thumbnail = QLabel('—')
            thumbnail.setFixedSize(42, 42)
            thumbnail.setAlignment(Qt.AlignCenter)
            thumbnail.setStyleSheet('background: #253044; border: 1px solid #425573; border-radius: 5px;')
            self.candidateIcons.append(thumbnail)
            combo = QComboBox()
            combo.setIconSize(QSize(28, 28))
            combo.setEditable(True)
            combo.setInsertPolicy(QComboBox.NoInsert)
            combo.setMinimumContentsLength(14)
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.lineEdit().setPlaceholderText('候选 ' + str(index + 1) + '：输入海克斯名称')
            combo.editTextChanged.connect(self.render)
            self.choices.append(combo)
            candidate.addWidget(thumbnail)
            candidate.addWidget(combo, 1)
            layout.addLayout(candidate)
        controls = QHBoxLayout()
        self.rarity = QComboBox()
        for title, value in [('全部品质', None), ('白银', 1), ('黄金', 4), ('棱彩', 8)]:
            self.rarity.addItem(title, value)
        self.rarity.currentIndexChanged.connect(self.render)
        clear = QPushButton('清空候选')
        clear.clicked.connect(self.clearChoices)
        self.retry = QPushButton('重试数据')
        controls.addWidget(self.rarity)
        controls.addWidget(clear)
        controls.addWidget(self.retry)
        layout.addLayout(controls)
        self.summary = QLabel('')
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.rerollHint = QLabel('')
        self.rerollHint.setWordWrap(True)
        self.rerollHint.setToolTip('按当前英雄的官方评级判断：三个候选均为 C / D 级才建议刷新。'
                                   '未读取剩余刷新次数，也未结合装备、已选海克斯和阵容。')
        layout.addWidget(self.rerollHint)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['海克斯', '评级', '排名', '选取率'])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3):
            self.table.setColumnWidth(column, 65)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setIconSize(QSize(36, 36))
        self.table.verticalHeader().setDefaultSectionSize(46)
        layout.addWidget(self.table, 1)
        foot = QLabel('按官方评级、推荐排名排序；选取率不等于胜率。\n无边框 / 窗口化可置顶；关闭仅隐藏，不会替你选择。')
        foot.setStyleSheet('color: #a4b1c6; font-size: 12px;')
        foot.setWordWrap(True)
        layout.addWidget(foot)
        self.rows = []
        self.hero = 0
        self.dismissed = False
        self.positioned = False
        self.iconCache = {}
        self.iconTask = None
        self.iconTimer = QTimer(self)
        self.iconTimer.setInterval(300)
        self.iconTimer.timeout.connect(self.refreshIcons)

    def openPanel(self):
        if not self.positioned:
            area = self.screen().availableGeometry()
            self.move(area.right() - self.width() - 16, area.top() + 70)
            self.positioned = True
        self.show()

    def closeEvent(self, event):
        event.ignore()
        self.dismissed = True
        self.hide()

    def clearChoices(self):
        for combo in self.choices:
            combo.blockSignals(True)
            combo.setCurrentIndex(-1)
            combo.setEditText('')
            combo.blockSignals(False)
        self.render()

    def loading(self, hero, name):
        self.iconTimer.stop()
        self.iconTask = None
        changed = self.hero != hero
        self.hero = hero
        self.rows = []
        self.title.setText(name + ' · 海克斯推荐')
        self.note.setText('正在获取该英雄的国服海克斯数据…')
        for combo in self.choices:
            combo.blockSignals(True)
            combo.clear()
            combo.blockSignals(False)
        if changed:
            self.rarity.setCurrentIndex(0)
        self.render()

    def setData(self, data):
        self.iconTimer.stop()
        self.rows = data['augments']
        self.iconTask = data.get('_iconTask')
        date = data['statDate']
        self.note.setText('腾讯 101 国服 · 统计日期 ' + date[:4] + '-' + date[4:6] + '-' + date[6:8]
                          + ' · 当前英雄专属数据')
        for combo in self.choices:
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(sorted({row['name'] for row in self.rows}))
            combo.setCurrentIndex(-1)
            combo.completer().setFilterMode(Qt.MatchContains)
            combo.completer().setCompletionMode(QCompleter.PopupCompletion)
            combo.blockSignals(False)
        self.render()
        if self.iconTask is not None and not self.iconTask.done():
            self.iconTimer.start()

    def localIcon(self, row):
        path = row.get('icon') or ''
        if path in self.iconCache:
            return self.iconCache[path]
        if path and os.path.isfile(path):
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                self.iconCache[path] = QIcon(pixmap)
                return self.iconCache[path]
        return QIcon()

    def refreshIcons(self):
        # Update decorations only: keep typed names, comparison and scroll position.
        by_name = {row['name']: row for row in self.rows}
        icons = {name: self.localIcon(row) for name, row in by_name.items()}
        for combo, thumbnail in zip(self.choices, self.candidateIcons):
            combo.blockSignals(True)
            for index in range(combo.count()):
                icon = icons.get(combo.itemText(index), QIcon())
                if combo.itemIcon(index).cacheKey() != icon.cacheKey():
                    combo.setItemIcon(index, icon)
            combo.blockSignals(False)
            name = combo.currentText().strip()
            icon = icons.get(name, QIcon())
            thumbnail.clear()
            if not icon.isNull():
                thumbnail.setPixmap(icon.pixmap(36, 36))
                thumbnail.setToolTip(name + '\n' + re.sub('<[^>]+>', '', by_name[name].get('tooltip', '')))
            else:
                thumbnail.setText('待图' if name in by_name else '—')
                thumbnail.setToolTip('图标尚未缓存或无法读取' if name in by_name else '选择海克斯后显示图标')
        for index in range(self.table.rowCount()):
            item = self.table.item(index, 0)
            if item is not None:
                item.setIcon(icons.get(item.text(), QIcon()))
        if self.iconTask is None or self.iconTask.done():
            self.iconTimer.stop()

    def render(self, *args):
        if not hasattr(self, 'rows'):
            return
        names = {combo.currentText().strip() for combo in self.choices if combo.currentText().strip()}
        known = {row['name'] for row in self.rows}
        missing = names - known
        advice, message = reroll_advice(self.rows, names)
        self.rerollHint.setText(message)
        colors = ('#49391f', '#ffda86') if advice == 'reroll' else (
            ('#203e38', '#b8ffe2') if advice == 'keep' else ('#253044', '#bdc9de'))
        self.rerollHint.setStyleSheet('background: ' + colors[0] + '; color: ' + colors[1]
                                     + '; border-radius: 6px; padding: 8px;')
        rows = [row for row in self.rows if (not names or row['name'] in names)
                and (names or self.rarity.currentData() is None or row.get('rarity') == self.rarity.currentData())]
        rows.sort(key=augment_order)
        rows = rows[:15]
        if missing:
            self.summary.setText('未匹配：' + '、'.join(sorted(missing)) + '。请选完整名称；候选未齐，暂不标出首选。')
        elif advice == 'reroll':
            self.summary.setText('当前候选均为低评级 · 建议见下方')
        elif rows:
            self.summary.setText(('候选比较 · ' if names else '推荐前 15 · ')
                                 + ('优先参考：' + rows[0]['name'] if rows[0].get('tier') is not None else '暂无官方评级'))
        else:
            self.summary.setText('暂无匹配数据')
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            tier = row.get('tier')
            values = [row['name'], 'SABCD'[tier] if tier in range(5) else '—',
                      str(row['rank']), f"{row['popular']:.2f}%"]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(re.sub('<[^>]+>', '', row.get('tooltip', '')))
                if index == 0 and tier is not None and not missing and (not names or advice == 'keep'):
                    item.setBackground(QColor('#285444'))
                    item.setForeground(QColor('#b8ffe2'))
                self.table.setItem(index, column, item)
        self.refreshIcons()


class HextechLiveAssistant(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.bar = QWidget()
        layout = QVBoxLayout(self.bar)
        layout.setContentsMargins(4, 2, 4, 2)
        self.label = BodyLabel('等待读取备选席；仅海克斯大乱斗自动跟随')
        self.label.setWordWrap(True)
        layout.addWidget(self.label)
        actions = QHBoxLayout()
        self.auto = CheckBox('局内自动显示推荐')
        self.auto.setChecked(True)
        self.openButton = PushButton('局内海克斯推荐')
        self.openButton.clicked.connect(self.openPanel)
        self.openButton.setEnabled(False)
        self.rankButton = PushButton('可选英雄排行')
        self.rankButton.clicked.connect(self.showRanking)
        actions.addWidget(self.auto)
        actions.addStretch()
        actions.addWidget(self.rankButton)
        actions.addWidget(self.openButton)
        layout.addLayout(actions)
        self.overlay = HextechOverlay()
        self.overlay.retry.clicked.connect(lambda: self.requestBuild(force=True))
        self.phase = ''
        self.active = False
        self.hero = 0
        self.choices = {}
        self.revision = 0
        self.pollTask = None
        self.buildTask = None
        self.gameKey = None
        self.gameHeroConfirmed = False
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.schedulePoll)
        self.timer.start()
        signalBus.champSelectChanged.connect(self.onDraft)
        signalBus.gameStatusChanged.connect(self.onPhase)
        signalBus.lolClientEnded.connect(self.reset)
        signalBus.lolClientChanged.connect(lambda _: self.reset())
        window.modeComboBox.currentIndexChanged.connect(self.refreshBar)
        self.refreshBar()

    def refreshBar(self, *args):
        visible = self.window.modeComboBox.currentData() == 'aram_mayhem'
        self.bar.setVisible(visible)
        self.window.tierInterface.tierList.setAvailable(self.choices if self.active and visible else {})

    def reset(self):
        self.revision += 1
        self.active = False
        self.hero = 0
        self.phase = ''
        self.gameKey = None
        self.gameHeroConfirmed = False
        self.choices = {}
        if self.buildTask and not self.buildTask.done():
            self.buildTask.cancel()
        self.overlay.hide()
        self.overlay.dismissed = False
        self.overlay.loading(0, '等待当前英雄')
        self.overlay.note.setText('进入海克斯选人或对局后自动获取推荐。')
        self.openButton.setEnabled(False)
        self.label.setText('等待读取备选席；仅海克斯大乱斗自动跟随')
        self.refreshBar()

    def onPhase(self, phase):
        self.revision += 1
        if phase in TERMINAL:
            self.reset()
        self.phase = phase
        if phase != 'ChampSelect':
            self.choices = {}
            self.refreshBar()
        self.schedulePoll()

    def onDraft(self, event):
        self.revision += 1
        if event.get('eventType') == 'Delete':
            # LCU deletes the draft on game start. Preserve the final hero.
            self.choices = {}
            self.refreshBar()
            self.schedulePoll()
            return
        session = event.get('data', event)
        if self.active and self.phase == 'ChampSelect' and isinstance(session, dict):
            self.applyDraft(session)
        else:
            self.schedulePoll()

    def applyDraft(self, session):
        self.gameHeroConfirmed = False
        hero, self.choices = draft_choices(session)
        self.label.setText('备选席 + 当前英雄按胜率置顶 · 前三名金框 · 队友英雄不计入\n'
                           + f'已读取 {len(self.choices)} 位；换英雄以客户端实际可操作状态为准。')
        self.setHero(hero)
        self.refreshBar()

    def setHero(self, hero):
        if hero == self.hero:
            return
        self.hero = hero
        self.openButton.setEnabled(bool(hero))
        if self.buildTask and not self.buildTask.done():
            self.buildTask.cancel()
        name = connector.manager.getChampionNameById(hero) if hero and connector.manager else str(hero)
        self.overlay.loading(hero, name or str(hero))
        if hero:
            self.requestBuild()

    def requestBuild(self, force=False):
        if not self.hero or not self.active or not getattr(opgg, 'hextech', None):
            return
        if self.buildTask and not self.buildTask.done():
            if not force:
                return
            self.buildTask.cancel()
        self.buildTask = asyncio.create_task(self.loadBuild(self.hero))

    async def loadBuild(self, hero):
        try:
            data = await opgg.hextech.build(hero)
            if self.active and self.hero == hero:
                if opgg.assetSession is not None and not opgg.assetSession.closed:
                    data['_iconTask'] = opgg._prefetchMayhemIcons(data['augments'], data['statDate'])
                self.overlay.setData(data)
        except asyncio.CancelledError:
            return
        except Exception:
            if self.active and self.hero == hero:
                self.overlay.note.setText('该英雄数据获取失败，请点击“重试数据”。')

    def openPanel(self):
        if self.hero:
            self.overlay.dismissed = False
            self.overlay.openPanel()
            if not self.overlay.rows:
                self.requestBuild()

    @asyncSlot()
    async def showRanking(self):
        if self.window.stackedWidget.currentWidget() is self.window.waitingInterface:
            return
        self.rankButton.setEnabled(False)
        try:
            await self.window.updateAndSwitchTo(self.window.tierInterface,
                                               self.window.tierInterface)
            self.refreshBar()
        finally:
            self.rankButton.setEnabled(True)

    def schedulePoll(self):
        # Connector initialization precedes the data session; wait for both.
        ready = opgg.assetSession is not None and not opgg.assetSession.closed
        if ready and connector.lcuSess is not None and (self.pollTask is None or self.pollTask.done()):
            self.pollTask = asyncio.create_task(self.poll())

    async def poll(self):
        revision = self.revision
        try:
            flow = await asyncio.wait_for(connector.getGameflowSession(), 3)
            if revision != self.revision:
                return
            phase = flow.get('phase', '')
            if not is_hextech(flow) or phase not in ('ChampSelect',) + PLAYING:
                if self.active:
                    self.reset()
                return
            self.active = True
            self.phase = phase
            game_key = (flow.get('gameData') or {}).get('gameId')
            if game_key and game_key != self.gameKey:
                self.gameKey = game_key
                self.gameHeroConfirmed = False
                self.overlay.dismissed = False
            if phase == 'ChampSelect':
                session = await asyncio.wait_for(connector.getChampSelectSession(), 3)
                if revision != self.revision:
                    return
                if 'myTeam' in session:
                    self.applyDraft(session)
            else:
                self.choices = {}
                self.refreshBar()
                if not self.hero or not self.gameHeroConfirmed:
                    account = await asyncio.wait_for(connector.getCurrentSummoner(), 3)
                    if revision != self.revision:
                        return
                    hero = game_champion(flow, account)
                    if hero:
                        self.setHero(hero)
                        self.gameHeroConfirmed = True
                self.label.setText('对局中 · 推荐跟随本局英雄；可打开置顶小窗查询海克斯。')
                if self.hero and self.auto.isChecked() and not self.overlay.dismissed and not self.overlay.isVisible():
                    self.overlay.openPanel()
        except asyncio.CancelledError:
            return
        except Exception:
            if revision == self.revision:
                self.choices = {}
                self.refreshBar()
                self.label.setText('客户端状态暂不可读，已取消可选标记；等待重连。')

    def stop(self):
        self.timer.stop()
        self.overlay.iconTimer.stop()
        for task in (self.pollTask, self.buildTask):
            if task and not task.done():
                task.cancel()
        self.overlay.hide()
