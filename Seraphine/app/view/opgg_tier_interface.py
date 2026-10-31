"""Compact rankings with reusable rows and portrait-safe preference controls."""
from collections import OrderedDict
import asyncio
import time
from PyQt5.QtWidgets import (QHBoxLayout, QWidget, QFrame, QVBoxLayout,
                             QSizePolicy, QLabel, QToolButton, QMenu)
from PyQt5.QtCore import Qt, pyqtSignal, QRectF
from PyQt5.QtGui import QPainter, QColor, QPen

from app.components.animation_frame import ColorAnimationFrame
from app.components.transparent_button import TransparentButton
from app.components.champion_icon_widget import RoundIcon, RoundedLabel
from app.common.signals import signalBus
from app.common.style_sheet import StyleSheet
from app.common.qfluentwidgets import BodyLabel, SmoothScrollArea
from app.view.rift_analysis_widget import RiftAnalysisWidget, tier_pixmap
from app.lol.hextech_live import champion_order
from app.lol.champion_preferences import ChampionPreferences, preference_text
from app.common.config import LOCAL_PATH


class TierInterface(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.vBoxLayout = QVBoxLayout(self)
        self.tierList = TierListWidget()
        self.analysis = RiftAnalysisWidget()
        self.analysis.hide()

        self.__initLayout()

        StyleSheet.OPGG_TIER_INTERFACE.apply(self)

    def __initLayout(self):
        self.vBoxLayout.setContentsMargins(0, 0, 0, 0)
        self.vBoxLayout.addWidget(self.tierList)
        self.vBoxLayout.addWidget(self.analysis)

    def showAnalysis(self, data=None, context=None, staged=None):
        if data is None:
            self.analysis.data = None
        national = data is not None and data['region'] == 'cn'
        self.analysis.setVisible(data is not None and not national)
        self.tierList.setVisible(data is None or national)
        self.tierList.titleBar.winRateLabel.setText('对位胜率' if national and data['enemyId'] else self.tierList.titleBar.tr('Win Rate'))
        if national:
            ids = {row['championId'] for row in data['data']}
            self.tierList.updateList([row for row in data['legacyRows'] if row['championId'] in ids], context=context, staged=staged)
            self.analysis.data = data
            self.tierList.setToolTip('腾讯 101 国服数据；梯队、胜率、登场率、禁用率和劣势对抗来自国服。'
                                    + ('胜率列为当前对手的我方对位胜率。' if data['enemyId'] else ''))
            return
        self.tierList.setToolTip('')
        if data is not None:
            self.analysis.updateData(data)

    def clearRecommendations(self, message, clear_native=True):
        self.analysis.data = None
        self.analysis.table.setRowCount(0)
        self.analysis.count.setText(message)
        if clear_native:
            self.tierList.updateList([])


class TierListWidget(QFrame):
    favoriteChanged = pyqtSignal(int, bool)
    def __init__(self, parent=None):
        super().__init__(parent)

        self.vBoxLayout = QVBoxLayout(self)

        self.titleBar = ListTitleBar()

        self.scrollArea = SmoothScrollArea()
        self.scrollWidget = QFrame()
        # This is a single-column list. A wrapping layout repeatedly measures
        # every child during show/hide/reparent and adds no useful behavior.
        self.scrollLayout = QVBoxLayout(self.scrollWidget)

        self.allChampionShowing = True

        self.items: list[ListItem] = []
        self.available = {}
        self.opponents = frozenset()
        self.sortKey = 'rank'
        self._rowsData = None
        self._rowContext = None
        self._rowPool = OrderedDict()
        self.maxCachedRows = 360
        self._layoutOrder = ()
        self.preferences = ChampionPreferences(LOCAL_PATH + '/cache/champion-preferences')

        self.__initWidget()
        self.__initLayout()

    def __initWidget(self):
        self.scrollArea.setObjectName("scrollArea")
        self.scrollWidget.setObjectName("scrollWidget")

        self.titleBar.sortRequested.connect(self.sortBy)

    def __initLayout(self):
        self.scrollLayout.setContentsMargins(0, 0, 0, 0)
        self.scrollLayout.setAlignment(Qt.AlignTop)
        self.scrollLayout.setSpacing(3)
        self.scrollArea.setWidget(self.scrollWidget)
        self.scrollArea.setWidgetResizable(True)
        self.scrollArea.setViewportMargins(6, 6, 17, 6)

        self.vBoxLayout.setSpacing(0)
        self.vBoxLayout.setContentsMargins(0, 0, 0, 0)
        self.vBoxLayout.setAlignment(Qt.AlignTop)
        self.vBoxLayout.addWidget(self.titleBar)
        self.vBoxLayout.addWidget(self.scrollArea)

    async def prepareRows(self, data, is_current):
        """Build a cold view in short GUI-thread slices; never block input for the full list."""
        self.validateRows(data)
        staged = {}
        started = time.perf_counter()
        try:
            for info in data:
                if not is_current():
                    self.discardRows(staged)
                    return None
                key = (bool(info.get('mayhem')), info['championId'])
                item = self._rowPool.get(key)
                if item is None or item.info != info:
                    item = ListItem(0, info, self.scrollWidget)
                    item.setParent(self.scrollWidget)
                    item.hide()
                    staged[key] = item
                if time.perf_counter() - started >= .012:
                    await asyncio.sleep(.001)
                    started = time.perf_counter()
            if not is_current():
                self.discardRows(staged)
                return None
            return staged
        except BaseException:
            self.discardRows(staged)
            raise

    @staticmethod
    def discardRows(staged):
        for item in staged.values():
            item.hide()
            item.deleteLater()

    def updateList(self, data: list, context=None, staged=None):
        self.validateRows(data)
        if data == self._rowsData and context == self._rowContext:
            return
        # Reuse the previous mode's rows. Source/lane changes replace only rows
        # whose actual contents changed; the pool has a fixed upper bound.
        prepared = []
        for info in data:
            key = (bool(info.get('mayhem')), info['championId'])
            item = self._rowPool.get(key)
            if item is None or item.info != info:
                replacement = staged.pop(key) if staged and key in staged else ListItem(0, info, self.scrollWidget)
                replacement.favoriteRequested.connect(self.toggleFavorite)
                if item is not None:
                    self.scrollLayout.removeWidget(item)
                    item.hide()
                    item.deleteLater()
                item = replacement
                self._rowPool[key] = item
            self._rowPool.move_to_end(key)
            prepared.append(item)
        self.setUpdatesEnabled(False)
        self.scrollLayout.setEnabled(False)
        try:
            while self.scrollLayout.count():
                self.scrollLayout.takeAt(0).widget().hide()
            self._layoutOrder = ()
            self.items = prepared
            self._rowsData = data
            self._rowContext = context
            self.allChampionShowing = True
            active = set(prepared)
            for key, item in list(self._rowPool.items()):
                if len(self._rowPool) <= self.maxCachedRows:
                    break
                if item not in active:
                    del self._rowPool[key]
                    item.deleteLater()
            self._configureColumns(data)
            self.sortKey = 'rank'
            self.titleBar.resetSelected()
            self.setAvailable(self.available, force=True)
            self.refreshPreferences()
            self.scrollArea.delegate.vScrollBar.resetValue(0)
            self.scrollArea.verticalScrollBar().setValue(0)
        finally:
            self.scrollLayout.setEnabled(True)
            self.setUpdatesEnabled(True)

    @staticmethod
    def validateRows(data):
        if not isinstance(data, list) or len(data) > 300:
            raise ValueError('排行列表格式异常')
        seen = set()
        for row in data:
            for field in ('championId', 'name', 'rank', 'tier', 'winRate', 'pickRate', 'banRate'):
                if field not in row:
                    raise ValueError('排行缺少字段：' + field)
            hero = row['championId']
            if type(hero) is not int or hero <= 0 or hero in seen or not isinstance(row['name'], str):
                raise ValueError('排行英雄标识异常')
            seen.add(hero)
            if type(row['tier']) is not int or not 0 <= row['tier'] <= 5:
                raise ValueError('排行梯队异常')
            if type(row['rank']) is not int or row['rank'] < 1:
                raise ValueError('排行名次异常')
            for field in ('winRate', 'pickRate', 'banRate'):
                value = row[field]
                if value is not None and (type(value) not in (int, float) or not 0 <= value <= 1):
                    raise ValueError('排行数值异常：' + field)
            counters = row.get('counters') or []
            if not isinstance(counters, list) or any(type(c.get('championId')) is not int for c in counters):
                raise ValueError('克制英雄格式异常')

    def _configureColumns(self, data):
        mayhem = bool(data and data[0].get('mayhem'))
        self.titleBar.rankLabel.setFixedWidth(32)
        for widget, key in ((self.titleBar.winRateLabel, 'winRate'),
                            (self.titleBar.pickRateLabel, 'pickRate'),
                            (self.titleBar.banRateLabel, 'banRate')):
            widget.setFixedWidth(64 if mayhem else 62 if key == 'winRate' else 54)
            widget.setVisible(not mayhem or any(row.get(key) is not None for row in data))
        self.titleBar.countersLabel.setVisible(bool(data) and not mayhem)

    def setAccount(self, profile):
        if self.preferences.set_account(profile):
            self.refreshPreferences()

    def setRecent(self, records):
        if self.preferences.set_recent(records):
            self.refreshPreferences()

    def toggleFavorite(self, hero):
        if not self.preferences.account:
            return
        try:
            favorite = self.preferences.toggle(hero)
        except OSError:
            for item in self.items:
                if item.championId == hero:
                    item.favoriteButton.setToolTip('收藏保存失败，请稍后重试。')
            return
        self.refreshPreferences()
        self.favoriteChanged.emit(hero, favorite)

    def refreshPreferences(self):
        # Hidden pooled rows refresh when they are displayed again, so account
        # changes never leak markers and do not repaint every cached mode.
        for item in self.items:
            item.setPreferences(self.preferences)

    def setAvailable(self, choices, force=False):
        if self.available == choices and not force:
            return
        self.available = dict(choices)
        mayhem = bool(self.items and self.items[0].info.get('mayhem'))
        active = self.available if mayhem else {}
        self.items.sort(key=(lambda x: champion_order(x.info, active)) if active
                        else self._rankOrder)
        available_rank = 0
        for item in self.items:
            hero = item.info['championId']
            selected = hero in active
            if selected:
                available_rank += 1
            # Missing rates never receive a high-win-rate badge.
            item.availableRank = available_rank if selected and item.info.get('winRate') is not None else 0
            item.opponent = not mayhem and hero in self.opponents
            suffix = active[hero] if selected else ''
            if 0 < item.availableRank <= 3:
                suffix += ' · TOP ' + str(item.availableRank)
            item.availabilityText = suffix
            item.refreshDetail()
            item.nameLabel.setToolTip(item.name + (' · ' + suffix if suffix else '') + (' · 对面已锁定' if item.opponent else ''))
            item.opponentLabel.setVisible(item.opponent)
            item.update()
        self.__update()
        if active:
            self.titleBar.selectSort('winRate')

    def _rankOrder(self, item):
        value = item.info.get(self.sortKey)
        pinned = not item.info.get('mayhem') and item.championId in self.opponents
        return (not pinned, value is None,
                (value if self.sortKey == 'rank' else -value) if value is not None else 0,
                item.info.get('rank', 9999))

    def setOpponents(self, champion_ids):
        opponents = frozenset(champion_ids)
        if opponents == self.opponents:
            return
        self.opponents = opponents
        if not self.items or self.items[0].info.get('mayhem'):
            return
        self.setAvailable(self.available, force=True)
        if any(item.opponent for item in self.items):
            self.scrollArea.delegate.vScrollBar.resetValue(0)
            self.scrollArea.verticalScrollBar().setValue(0)

    def __update(self):
        order = tuple(self.items)
        if order != self._layoutOrder:
            self.scrollLayout.setEnabled(False)
            while self.scrollLayout.count():
                self.scrollLayout.takeAt(0)
            for item in self.items:
                self.scrollLayout.addWidget(item)
                item.show()
            self._layoutOrder = order
            self.scrollLayout.setEnabled(True)
        for i, x in enumerate(self.items, start=1):
            x.setCounter(i if x.info.get('mayhem') else x.info.get('rank', i))

    def sortBy(self, key: str):
        if not self.items or not any(item.info.get(key) is not None for item in self.items):
            return

        if self.available and self.items[0].info.get('mayhem'):
            self.setAvailable(self.available, force=True)
            return

        self.sortKey = key
        self.items.sort(key=self._rankOrder)
        self.__update()

    def filterChampions(self, type: str, x):
        self.allChampionShowing = False

        '''
        通过两种方式来筛选英雄：
        - `type` 为 `championId` 时，通过判断英雄 id 在不在列表 `x` 中筛选
        - `type` 为 `name` 时，通过判断字符串 `x` 在不在英雄名中筛选
        '''

        if type == 'championId':
            for item in self.items:
                item.setVisible(item.info['championId'] in x)

        else:
            for item in self.items:
                item.setVisible(x in item.info['name'])

    def showAllChampions(self):
        if self.allChampionShowing:
            return

        self.allChampionShowing = True

        for item in self.items:
            item.setVisible(True)


class ListTitleBar(QFrame):
    sortRequested = pyqtSignal(str)

    def __init__(self, parent: QWidget = None):
        super().__init__(parent)
        self.hBoxLayout = QHBoxLayout(self)

        # self.setStyleSheet("border: 1px solid black;")

        self.counterLabel = QLabel("#")
        self.favoriteLabel = QLabel('☆')
        self.favoriteLabel.setFixedWidth(24)
        self.favoriteLabel.setAlignment(Qt.AlignCenter)
        self.favoriteLabel.setToolTip('收藏在独立一列；点击星标或右键英雄即可收藏。')
        self.championLabel = QLabel(self.tr("Champion"))
        self.rankLabel = TransparentButton(self.tr("Tier"))
        self.winRateLabel = TransparentButton(self.tr("Win Rate"))
        self.pickRateLabel = TransparentButton(self.tr("Pick Rate"))
        self.banRateLabel = TransparentButton(self.tr("Ban Rate"))
        self.countersLabel = QLabel('选谁打他')
        self.countersLabel.setToolTip('头像为该行英雄较难应对的英雄；胜率列仍为该行英雄的整体胜率。')

        self.selected: TransparentButton = self.rankLabel

        self.__initWidget()
        self.__initLayout()

    def __initLayout(self):
        self.hBoxLayout.setContentsMargins(14, 4, 31, 4)
        self.hBoxLayout.setSpacing(3)
        self.hBoxLayout.addWidget(self.favoriteLabel)
        self.hBoxLayout.addWidget(self.counterLabel, alignment=Qt.AlignCenter)
        self.hBoxLayout.addSpacing(28 + 3)
        self.hBoxLayout.addWidget(self.championLabel, 1)
        self.hBoxLayout.addWidget(self.rankLabel, alignment=Qt.AlignCenter)
        self.hBoxLayout.addWidget(self.winRateLabel, alignment=Qt.AlignCenter)
        self.hBoxLayout.addWidget(self.pickRateLabel, alignment=Qt.AlignCenter)
        self.hBoxLayout.addWidget(self.banRateLabel, alignment=Qt.AlignCenter)
        self.hBoxLayout.addWidget(self.countersLabel, alignment=Qt.AlignCenter)

    def __initWidget(self):
        self.counterLabel.setFixedWidth(26)
        self.counterLabel.setObjectName("counterLabel")
        self.counterLabel.setAlignment(Qt.AlignCenter)
        self.countersLabel.setAlignment(Qt.AlignCenter)
        self.countersLabel.setObjectName("countersLabel")
        self.championLabel.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self.championLabel.setObjectName("championLabel")

        self.rankLabel.setFixedWidth(32)
        self.winRateLabel.setFixedWidth(62)
        self.pickRateLabel.setFixedWidth(54)
        self.banRateLabel.setFixedWidth(54)
        self.countersLabel.setFixedWidth(74)

        self.rankLabel.clicked.connect(
            lambda: self.__onSortButtonClicked("rank"))
        self.winRateLabel.clicked.connect(
            lambda: self.__onSortButtonClicked("winRate"))
        self.pickRateLabel.clicked.connect(
            lambda: self.__onSortButtonClicked("pickRate"))
        self.banRateLabel.clicked.connect(
            lambda: self.__onSortButtonClicked("banRate"))

        self.resetSelected()

    def __onSortButtonClicked(self, type):
        widget: TransparentButton = getattr(self, f"{type}Label")
        self.__setSelected(widget)

        self.sortRequested.emit(type)

    def __setSelected(self, widget: TransparentButton):
        if self.selected is widget and widget.property('selected'):
            return
        self.selected.setProperty("selected", False)
        self.selected.style().polish(self.selected)

        widget.setProperty("selected", True)
        widget.style().polish(widget)

        self.selected = widget

    def resetSelected(self):
        self.__setSelected(self.rankLabel)

    def selectSort(self, key):
        self.__setSelected(getattr(self, key + 'Label'))


class PreferenceIcon(RoundIcon):
    favoriteRequested = pyqtSignal(int)

    def __init__(self, info, diameter, base_tooltip, parent=None, draw_favorite=True):
        path = info.get('icon')
        super().__init__(path if isinstance(path, str) else 'app/resource/images/champion-0.png',
                         diameter, 2, 2, parent=parent)
        self.hero = info['championId']
        self.baseTooltip = base_tooltip
        self.favorite = False
        self.recent = False
        self.accountReady = False
        self.drawFavorite = draw_favorite
        self._preferenceState = None

    def setPreferences(self, preferences, mayhem=False):
        summary = preferences.summary(self.hero, mayhem)
        state = (self.hero in preferences.favorites, bool(preferences.account), tuple(summary.items()), mayhem)
        if state == self._preferenceState:
            return
        self._preferenceState = state
        self.favorite = state[0]
        self.recent = bool(summary)
        self.accountReady = bool(preferences.account)
        self.setToolTip(self.baseTooltip + '\n' + preference_text(self.favorite, summary, mayhem))
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.drawFavorite or not (self.recent or self.favorite):
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        # Rings leave the entire portrait visible, including small Counter icons.
        painter.setBrush(Qt.NoBrush)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self.recent:
            painter.setPen(QPen(QColor('#16827b'), 1.5))
            painter.drawEllipse(rect)
        if self.favorite:
            painter.setPen(QPen(QColor('#398fd1'), 1.5))
            painter.drawEllipse(rect.adjusted(2, 2, -2, -2) if self.recent else rect)
        painter.end()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        action = menu.addAction('取消收藏' if self.favorite else '收藏英雄')
        action.setEnabled(self.accountReady)
        if menu.exec_(event.globalPos()) is action:
            self.favoriteRequested.emit(self.hero)


class ElidedLabel(BodyLabel):
    """Keep full text for tooltips/accessibility without widening the columns."""
    def __init__(self, text='', parent=None):
        super().__init__(parent=parent)
        self.setText(text)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.setFont(self.font())
        painter.drawText(self.contentsRect(), Qt.AlignVCenter | Qt.AlignLeft,
                         self.fontMetrics().elidedText(self.text(), Qt.ElideRight, self.contentsRect().width()))


class ListItem(ColorAnimationFrame):
    favoriteRequested = pyqtSignal(int)
    availableRank = 0
    opponent = False

    def paintEvent(self, event):
        super().paintEvent(event)
        if 0 < self.availableRank <= 3 or self.opponent:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(QPen(QColor('#d04a50') if self.opponent else QColor('#d39a28'), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(self.rect().adjusted(2, 2, -2, -2), 4, 4)
            painter.end()

    def __init__(self, number, info, parent: QWidget = None):
        super().__init__(type=f"tier{info['tier']}", parent=parent)
        self.setFixedWidth(589)

        self.championId = info['championId']
        self.name = info['name']

        self.info = info

        tierIcon = f"app/resource/images/icon-tier-{info['tier']}.svg"

        self.hBoxLayout = QHBoxLayout(self)

        self.numberLabel = BodyLabel(str(number))
        self.nameLabel = ElidedLabel(info.get("name"))
        self.nameWidget = QWidget(self)
        self.nameLayout = QVBoxLayout(self.nameWidget)
        self.nameLayout.setContentsMargins(0, 0, 0, 0)
        self.nameLayout.setSpacing(0)
        self.nameLayout.addWidget(self.nameLabel)
        self.detailLabel = ElidedLabel()
        self.detailLabel.setObjectName('rankingDetail')
        self.nameLayout.addWidget(self.detailLabel)
        self.detailLabel.hide()
        self.availabilityText = self.recentText = ''
        self.opponentLabel = QLabel('对面')
        self.opponentLabel.setStyleSheet('color: #d04a50; font-size: 11px; font-weight: 600;')
        self.opponentLabel.setToolTip('对面已锁定 · 当前分路榜中存在该英雄，不代表已确认对方分路')
        self.opponentLabel.hide()
        self.championIcon = PreferenceIcon(info, 28, self.name, draw_favorite=False)
        self.championIcon.favoriteRequested.connect(self.favoriteRequested)
        self.favorite = False
        self._preferenceState = None
        self.favoriteButton = QToolButton(self)
        self.favoriteButton.setObjectName('favoriteButton')
        self.favoriteButton.setFixedSize(24, 28)
        self.favoriteButton.setText('☆')
        self.favoriteButton.setFocusPolicy(Qt.StrongFocus)
        self.favoriteButton.clicked.connect(lambda _: self.favoriteRequested.emit(self.championId))
        if info.get('source') == 'tencent-101':
            self.tierLabel = BodyLabel('T' + str(info['tier']))
            self.tierLabel.setFixedWidth(28)
            self.tierLabel.setAlignment(Qt.AlignCenter)
            colors = {0: '#c291ff', 1: '#59afff', 2: '#40cbb5', 3: '#daa45f', 4: '#94a0b2'}
            self.tierLabel.setStyleSheet('font-weight: 700; color: ' + colors.get(info['tier'], '#94a0b2') + ';')
        else:
            self.tierLabel = RoundedLabel(tierIcon, borderWidth=0, radius=0)
            if self.tierLabel.pixmap().isNull():
                self.tierLabel.setPixmap(tier_pixmap(info['tier']))

        if info['winRate'] is not None:
            self.winRateLabel = BodyLabel(f"{info['winRate']*100:.2f}%")
        else:
            self.winRateLabel = BodyLabel("--")

        if info['pickRate'] is not None:
            self.pickRateLabel = BodyLabel(f"{info['pickRate']*100:.2f}%")
        else:
            self.pickRateLabel = BodyLabel("--")

        if info['banRate'] is not None:
            self.banRateLabel = BodyLabel(f"{info['banRate']*100:.2f}%")
        else:
            self.banRateLabel = BodyLabel("--")

        self.countersLabel = QWidget()
        self.countersLayout = QHBoxLayout(self.countersLabel)

        self.countersLayout.setAlignment(Qt.AlignCenter)

        counters = info.get("counters")
        self.counterIcons = []
        if counters:
            for c in counters[:3]:
                icon = PreferenceIcon(c, 22, f"{c.get('name', str(c['championId']))} · 可用于对抗{self.name}")
                icon.favoriteRequested.connect(self.favoriteRequested)
                self.counterIcons.append(icon)
                self.countersLayout.addWidget(icon)
        else:
            icon = BodyLabel("--")
            self.countersLayout.addWidget(icon)

        self.__initWidget()
        self.__initLayout()

        self.countersLabel.setToolTip('选谁打' + self.name + '：' +
            '、'.join(c.get('name', str(c['championId'])) for c in (counters or [])[:3])
            if counters else '暂无克制数据')
        if info.get('mayhem'):
            self.winRateLabel.setVisible(True)
            self.pickRateLabel.setVisible(info.get('pickRate') is not None)
            self.banRateLabel.hide()
            self.countersLabel.hide()

    def __initWidget(self):
        self.setFixedHeight(42)
        self.numberLabel.setFixedWidth(26)
        self.numberLabel.setAlignment(Qt.AlignCenter)
        self.winRateLabel.setAlignment(Qt.AlignCenter)
        self.pickRateLabel.setAlignment(Qt.AlignCenter)
        self.banRateLabel.setAlignment(Qt.AlignCenter)

        mayhem = bool(self.info.get('mayhem'))
        self.tierLabel.setFixedWidth(32)
        self.winRateLabel.setFixedWidth(64 if mayhem else 62)
        self.pickRateLabel.setFixedWidth(64 if mayhem else 54)
        self.banRateLabel.setFixedWidth(64 if mayhem else 54)
        self.countersLabel.setFixedWidth(74)

        self.clicked.connect(self.__onClicked)

    def __initLayout(self):
        self.hBoxLayout.setContentsMargins(8, 4, 8, 4)
        self.hBoxLayout.setSpacing(3)
        self.countersLayout.setContentsMargins(0, 0, 0, 0)
        self.countersLayout.setSpacing(2)

        self.hBoxLayout.addWidget(self.favoriteButton, alignment=Qt.AlignCenter)
        self.hBoxLayout.addWidget(self.numberLabel, alignment=Qt.AlignCenter)
        self.hBoxLayout.addWidget(self.championIcon, alignment=Qt.AlignCenter)
        self.hBoxLayout.addWidget(self.nameWidget, 1)
        self.hBoxLayout.addWidget(self.opponentLabel, alignment=Qt.AlignCenter)
        self.hBoxLayout.addWidget(self.tierLabel, alignment=Qt.AlignCenter)
        self.hBoxLayout.addWidget(self.winRateLabel, alignment=Qt.AlignCenter)
        self.hBoxLayout.addWidget(self.pickRateLabel, alignment=Qt.AlignCenter)
        self.hBoxLayout.addWidget(self.banRateLabel, alignment=Qt.AlignCenter)
        self.hBoxLayout.addWidget(self.countersLabel, alignment=Qt.AlignCenter)

    def setCounter(self, x):
        self.numberLabel.setText(str(x))

    def setPreferences(self, preferences):
        mayhem = bool(self.info.get('mayhem'))
        summary = preferences.summary(self.championId, mayhem)
        state = (self.championId in preferences.favorites, bool(preferences.account), tuple(summary.items()))
        if state != self._preferenceState:
            self._preferenceState = state
            self.favorite = state[0]
            self.favoriteButton.setEnabled(state[1])
            self.favoriteButton.setText('★' if self.favorite else '☆')
            self.favoriteButton.setProperty('favorite', self.favorite)
            self.favoriteButton.style().polish(self.favoriteButton)
            self.favoriteButton.setAccessibleName(('取消收藏' if self.favorite else '收藏') + self.name)
            self.favoriteButton.setToolTip(('取消收藏 ' if self.favorite else '收藏 ') + self.name
                if preferences.account else '连接客户端后可收藏；收藏按账号分别保存。')
            self.recentText = (f"近 {summary['games']} 场 · {summary['wins']}胜 {summary['losses']}负"
                               if summary else '')
            self.refreshDetail()
        self.championIcon.setPreferences(preferences, mayhem)
        for icon in self.counterIcons:
            icon.setPreferences(preferences, mayhem)

    def refreshDetail(self):
        text = ' · '.join(part for part in (self.availabilityText, self.recentText) if part)
        self.detailLabel.setText(text)
        self.detailLabel.setToolTip(text)
        self.detailLabel.setVisible(bool(text))

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        action = menu.addAction('取消收藏' if self.favorite else '收藏英雄')
        action.setEnabled(self.favoriteButton.isEnabled())
        if menu.exec_(event.globalPos()) is action:
            self.favoriteRequested.emit(self.championId)

    def __onClicked(self):
        signalBus.toOpggBuildInterface.emit(self.championId, "", "")
