"""Read-only draft board. Enemy lane assignments belong to this app only."""
import uuid
import os
import asyncio
import time

from PyQt5.QtCore import Qt, QMimeData, pyqtSignal, QSize
from PyQt5.QtGui import QDrag, QIcon, QPixmap
from PyQt5.QtWidgets import QWidget, QGridLayout, QListWidget, QListWidgetItem, QHBoxLayout, QSizePolicy, QLabel
from app.common.qfluentwidgets import BodyLabel, CaptionLabel, ComboBox, PushButton, isDarkTheme
from app.common.config import qconfig
from app.lol.rift_analysis import bp_snapshot
from app.lol.connector import gameIconCachePath

LANES = [('TOP', '上单'), ('JUNGLE', '打野'), ('MID', '中单'),
         ('ADC', '射手'), ('SUPPORT', '辅助'), ('UNKNOWN', '待确认')]
ALIASES = {'top': 'TOP', 'jungle': 'JUNGLE', 'middle': 'MID',
           'bottom': 'ADC', 'utility': 'SUPPORT'}
MIME = 'application/x-seraphine-bp-champion'


class AllyLane(QWidget):
    def __init__(self):
        super().__init__()
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(4)
        self.caption = CaptionLabel('—')
        self.caption.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.layout.addWidget(self.caption, 1)
        self.icons = []

    def updateHeroes(self, entries, icon_path):
        for label in self.icons:
            self.layout.removeWidget(label)
            label.deleteLater()
        self.icons = []
        for hero, text in entries:
            if hero:
                label = QLabel()
                label.setFixedSize(24, 24)
                label.setPixmap(QPixmap(icon_path(hero)).scaled(24, 24, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                label.setToolTip(text)
                self.layout.insertWidget(len(self.icons), label)
                self.icons.append(label)
        text = ' / '.join(t for _, t in entries) or '—'
        self.caption.setText(text)
        self.setToolTip(text)

    def text(self):
        return self.caption.text()


class DraftAssignments:
    def __init__(self):
        self.enemies = []
        self.manual = {}
        self.session = {}

    def update(self, session):
        self.session = session
        self.enemies, _ = bp_snapshot(session)
        self.manual = {hero: lane for hero, lane in self.manual.items() if hero in self.enemies}

    def move(self, hero, lane):
        if hero not in self.enemies or lane not in dict(LANES):
            return False
        old = self.manual.get(hero, 'UNKNOWN')
        occupant = next((h for h, l in self.manual.items() if l == lane and h != hero), None)
        if lane != 'UNKNOWN' and occupant is not None:
            self.manual[occupant] = old  # Swap; never lose an opponent.
        self.manual[hero] = lane
        return True


class EnemySlot(QListWidget):
    def __init__(self, board, lane):
        super().__init__(board)
        self.board, self.lane = board, lane
        self.setFixedHeight(30)
        self.setFlow(QListWidget.LeftToRight)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        if lane == 'UNKNOWN':
            self.setFixedHeight(46)
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setDragEnabled(True)
        self.setIconSize(QSize(22, 22))
        self.setAcceptDrops(True)
        self.setAccessibleName(dict(LANES)[lane] + '敌方英雄')
        self.applyStyle()
        qconfig.themeChangedFinished.connect(self.applyStyle)
        self.itemClicked.connect(board.selectItem)
        self.itemActivated.connect(board.selectItem)

    def applyStyle(self):
        bg, text, border, selected = ('#2b2b2b', '#f3f3f3', '#555555', '#164f70') if isDarkTheme() else ('#ffffff', '#202020', '#d6d6d6', '#d8edff')
        self.setStyleSheet('QListWidget { background: %s; color: %s; border: 1px solid %s; border-radius: 4px; }'
                           'QListWidget::item { padding: 2px 4px; } QListWidget::item:selected { background: %s; color: %s; }'
                           % (bg, text, border, selected, text))

    def startDrag(self, actions):
        item = self.currentItem()
        if item is None:
            return
        mime = QMimeData()
        mime.setData(MIME, f'{self.board.token}:{item.data(Qt.UserRole)}'.encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec_(Qt.MoveAction)

    def heroFromEvent(self, event):
        try:
            token, hero = bytes(event.mimeData().data(MIME)).decode().split(':')
            hero = int(hero)
            return hero if token == self.board.token and hero in self.board.model.enemies else None
        except (ValueError, UnicodeDecodeError):
            return None

    def dragEnterEvent(self, event):
        if self.heroFromEvent(event) is not None:
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        hero = self.heroFromEvent(event)
        if hero is not None:
            self.board.moveHero(hero, self.lane)
            event.acceptProposedAction()


class BPAssistantWidget(QWidget):
    matchupChosen = pyqtSignal(int, str)
    assignmentChanged = pyqtSignal(int, str)
    contextChanged = pyqtSignal()
    matchupRefreshRequested = pyqtSignal()

    def __init__(self, name_lookup, parent=None, icon_loader=None):
        super().__init__(parent)
        self.name_lookup = name_lookup
        self.model = DraftAssignments()
        self.token = uuid.uuid4().hex
        self.selected = 0
        self.icon_loader = icon_loader
        self.iconPaths = {}
        self.iconTasks = {}
        self.iconRetryAt = {}
        self.iconSemaphore = asyncio.Semaphore(4)
        self.context = None
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setVerticalSpacing(2)
        grid.addWidget(CaptionLabel('分路'), 0, 0)
        grid.addWidget(CaptionLabel('我方 · 预选 / 锁定'), 0, 1)
        grid.addWidget(CaptionLabel('敌方 · 拖动调整分路'), 0, 2)
        self.allies, self.slots = {}, {}
        for row, (lane, title) in enumerate(LANES, 1):
            grid.addWidget(BodyLabel(title), row, 0)
            self.allies[lane] = AllyLane()
            self.allies[lane].setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            grid.addWidget(self.allies[lane], row, 1)
            self.slots[lane] = EnemySlot(self, lane)
            grid.addWidget(self.slots[lane], row, 2)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 2)
        controls = QHBoxLayout()
        self.selection = CaptionLabel('点击敌方英雄')
        self.laneCombo = ComboBox()
        for lane, title in LANES:
            self.laneCombo.addItem(title, userData=lane)
        self.moveButton = PushButton('移到此路')
        self.queryButton = PushButton('查看 Counter')
        self.resetButton = PushButton('重置分路')
        self.moveButton.clicked.connect(lambda: self.moveHero(self.selected, self.laneCombo.currentData()))
        self.queryButton.clicked.connect(self.query)
        self.resetButton.clicked.connect(self.resetLanes)
        for w in (self.selection, self.laneCombo, self.moveButton, self.queryButton, self.resetButton):
            controls.addWidget(w)
        grid.addLayout(controls, 7, 0, 1, 3)
        self.hint = CaptionLabel('等待选角；敌方分路需确认，拖动或点击后移到指定分路。')
        self.hint.setWordWrap(True)
        grid.addWidget(self.hint, 8, 0, 1, 3)
        self.matchupLabel = BodyLabel('你的对位：等待你选择英雄和确认敌方分路')
        self.matchupLabel.setWordWrap(True)
        self.matchupLabel.setMinimumHeight(38)
        matchupLayout = QHBoxLayout()
        matchupLayout.addWidget(self.matchupLabel, 1)
        self.refreshMatchupButton = PushButton('刷新对位')
        self.refreshMatchupButton.clicked.connect(self.matchupRefreshRequested)
        matchupLayout.addWidget(self.refreshMatchupButton)
        grid.addLayout(matchupLayout, 9, 0, 1, 3)
        self.render()

    def updateSession(self, session):
        self.model.update(session)
        if self.selected not in self.model.enemies:
            self.selected = 0
        self.render()

    def clear(self):
        for task in list(self.iconTasks.values()):
            task.cancel()
        self.model = DraftAssignments()
        self.selected = 0
        self.render()

    def selectItem(self, item):
        self.selected = item.data(Qt.UserRole)
        self.syncControls()
        self.updateContext()

    def iconPath(self, hero):
        path = self.iconPaths.get(hero) or gameIconCachePath('champion', hero)
        return path if os.path.isfile(path) else 'app/resource/images/champion-0.png'

    def loadIcons(self, heroes):
        if not self.icon_loader:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for hero in heroes:
            if hero in self.iconPaths or hero in self.iconTasks or self.iconRetryAt.get(hero, 0) > time.monotonic():
                continue
            self.iconTasks[hero] = loop.create_task(self.loadIcon(hero))

    async def loadIcon(self, hero):
        try:
            async with self.iconSemaphore:
                path = await self.icon_loader(hero)
            if QPixmap(path).isNull():
                raise ValueError('图标图片无法加载')
            self.iconPaths[hero] = path
            self.render()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.iconRetryAt[hero] = time.monotonic() + 30
        finally:
            self.iconTasks.pop(hero, None)

    def updateContext(self):
        session = self.model.session
        player = next((p for p in session.get('myTeam') or []
                       if p.get('cellId') == session.get('localPlayerCellId')), {})
        hero = player.get('championId') or player.get('championPickIntent') or 0
        lane = ALIASES.get((player.get('assignedPosition') or '').lower(), 'UNKNOWN')
        if lane == 'UNKNOWN':
            lane = self.model.manual.get(self.selected, 'UNKNOWN')
        enemy = next((h for h in self.model.enemies if self.model.manual.get(h) == lane), 0)
        context = (hero, enemy, lane) if hero and enemy and lane != 'UNKNOWN' else None
        if context != self.context:
            self.context = context
            self.matchupLabel.setText('你的对位：正在查询…' if context else '你的对位：等待你选择英雄和确认敌方分路')
            self.matchupLabel.setToolTip('')
            self.contextChanged.emit()
        self.refreshMatchupButton.setEnabled(bool(context))

    def syncControls(self):
        lane = self.model.manual.get(self.selected, 'UNKNOWN')
        self.selection.setText(self.name_lookup(self.selected) if self.selected else '点击敌方英雄')
        self.laneCombo.setCurrentIndex(self.laneCombo.findData(lane))
        self.moveButton.setEnabled(bool(self.selected))
        self.queryButton.setEnabled(bool(self.selected) and lane != 'UNKNOWN')

    def moveHero(self, hero, lane):
        if self.model.move(hero, lane):
            self.selected = hero
            self.render()
            self.assignmentChanged.emit(hero, lane)

    def resetLanes(self):
        self.model.manual.clear()
        self.render()
        self.assignmentChanged.emit(0, 'UNKNOWN')

    def query(self):
        lane = self.model.manual.get(self.selected, 'UNKNOWN')
        if self.selected in self.model.enemies and lane != 'UNKNOWN':
            self.matchupChosen.emit(self.selected, lane)

    def render(self):
        allies = {lane: [] for lane, _ in LANES}
        session = self.model.session
        completed = {a.get('actorCellId') for group in session.get('actions') or [] for a in group
                     if a.get('type') == 'pick' and a.get('completed')}
        for player in session.get('myTeam') or []:
            hero = player.get('championId') or player.get('championPickIntent') or 0
            lane = ALIASES.get((player.get('assignedPosition') or '').lower(), 'UNKNOWN')
            name = self.name_lookup(hero) if hero else '未选'
            state = '锁定' if hero and player.get('cellId') in completed else '预选' if hero else ''
            me = '你 · ' if player.get('cellId') == session.get('localPlayerCellId') else ''
            allies[lane].append((hero, me + name + (' · ' + state if state else '')))
        for lane, _ in LANES:
            self.allies[lane].updateHeroes(allies[lane], self.iconPath)
            self.slots[lane].clear()
        for hero in self.model.enemies:
            lane = self.model.manual.get(hero, 'UNKNOWN')
            item = QListWidgetItem(self.name_lookup(hero))
            item.setIcon(QIcon(self.iconPath(hero)))
            item.setData(Qt.UserRole, hero)
            item.setToolTip('敌方已锁定；拖动到分路，或点击后使用“移到此路”')
            self.slots[lane].addItem(item)
        self.hint.setText('敌方已锁定 %d 名；手动分路保留至本轮 BP 结束。对位胜率是整局统计。' % len(self.model.enemies)
                          if session else '等待选角；敌方分路需确认，拖动或点击后移到指定分路。')
        self.syncControls()
        self.loadIcons(set(self.model.enemies) | {h for entries in allies.values() for h, _ in entries if h})
        self.updateContext()
