
from PyQt5.QtWidgets import (QHBoxLayout, QWidget, QFrame, QVBoxLayout, QSpacerItem,
                             QSizePolicy, QLabel, QHBoxLayout, QWidget, QLabel, QFrame,
                             QVBoxLayout, QSpacerItem, QSizePolicy)
from PyQt5.QtCore import Qt, pyqtSignal, QEasingCurve
from PyQt5.QtGui import QPixmap, QPainter, QColor, QPen
from qasync import asyncSlot

from app.components.animation_frame import ColorAnimationFrame
from app.components.transparent_button import TransparentButton
from app.components.champion_icon_widget import RoundIcon, RoundedLabel
from app.common.signals import signalBus
from app.common.style_sheet import StyleSheet
from app.common.qfluentwidgets import BodyLabel, SmoothScrollArea, FlowLayout, IconWidget
from app.view.rift_analysis_widget import RiftAnalysisWidget, tier_pixmap
from app.lol.hextech_live import champion_order


class TierInterface(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.vBoxLayout = QVBoxLayout(self)
        self.tierList = TierListWidget()
        self.analysis = RiftAnalysisWidget()
        self.analysis.hide()

        self.__initWidget()
        self.__initLayout()

        StyleSheet.OPGG_TIER_INTERFACE.apply(self)

    def __initWidget(self):
        pass

    def __initLayout(self):
        self.vBoxLayout.setContentsMargins(0, 0, 0, 0)
        self.vBoxLayout.addWidget(self.tierList)
        self.vBoxLayout.addWidget(self.analysis)

    def showAnalysis(self, data=None):
        if data is None:
            self.analysis.data = None
        national = data is not None and data['region'] == 'cn'
        self.analysis.setVisible(data is not None and not national)
        self.tierList.setVisible(data is None or national)
        self.tierList.titleBar.winRateLabel.setText('对位胜率' if national and data['enemyId'] else self.tierList.titleBar.tr('Win Rate'))
        if national:
            self.analysis.data = data
            ids = {row['championId'] for row in data['data']}
            self.tierList.updateList([row for row in data['legacyRows'] if row['championId'] in ids])
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
    def __init__(self, parent=None):
        '''
        英雄梯队列表组件

        观前警告：
            为了实现该死的上下对齐，该类实现中含有大量魔法数字
            请自备降压药
        '''

        super().__init__(parent)

        self.vBoxLayout = QVBoxLayout(self)

        self.titleBar = ListTitleBar()

        self.scrollArea = SmoothScrollArea()
        self.scrollWidget = QFrame()
        self.scrollLayout = FlowLayout(needAni=False, isTight=True)

        self.allChampionShowing = True

        self.items: list[ListItem] = []
        self.available = {}

        self.__initWidget()
        self.__initLayout()

    def __initWidget(self):
        self.scrollArea.setObjectName("scrollArea")
        self.scrollWidget.setObjectName("scrollWidget")

        self.titleBar.sortRequested.connect(self.__onSortRequested)

    def __initLayout(self):
        self.scrollLayout.setContentsMargins(0, 0, 0, 0)
        self.scrollLayout.setAlignment(Qt.AlignTop)
        self.scrollLayout.setVerticalSpacing(3)
        self.scrollLayout.setAnimation(450, QEasingCurve.Type.OutQuart)
        self.scrollWidget.setLayout(self.scrollLayout)
        self.scrollArea.setWidget(self.scrollWidget)
        self.scrollArea.setWidgetResizable(True)
        self.scrollArea.setViewportMargins(6, 6, 17, 6)

        self.vBoxLayout.setSpacing(0)
        self.vBoxLayout.setContentsMargins(0, 0, 0, 0)
        self.vBoxLayout.setAlignment(Qt.AlignTop)
        self.vBoxLayout.addWidget(self.titleBar)
        self.vBoxLayout.addWidget(self.scrollArea)

    def updateList(self, data: list):
        mayhem = bool(data and data[0].get('mayhem'))
        for widget, key in ((self.titleBar.winRateLabel, 'winRate'),
                            (self.titleBar.pickRateLabel, 'pickRate'),
                            (self.titleBar.banRateLabel, 'banRate')):
            widget.setVisible(not mayhem or any(row.get(key) is not None for row in data))
        self.titleBar.countersLabel.hide()
        if len(self.items) != 0:
            self.scrollLayout.takeAllWidgets()

        self.scrollArea.delegate.vScrollBar.resetValue(0)
        self.scrollArea.verticalScrollBar().setSliderPosition(0)

        self.items = [ListItem(0, x) for x in data]
        self.titleBar.resetSelected()
        self.setAvailable(self.available, force=True)

    def setAvailable(self, choices, force=False):
        if self.available == choices and not force:
            return
        self.available = dict(choices)
        mayhem = bool(self.items and self.items[0].info.get('mayhem'))
        active = self.available if mayhem else {}
        self.items.sort(key=(lambda x: champion_order(x.info, active)) if active
                        else (lambda x: x.info.get('rank', 9999)))
        available_rank = 0
        for item in self.items:
            hero = item.info['championId']
            selected = hero in active
            if selected:
                available_rank += 1
            # Missing rates never receive a high-win-rate badge.
            item.availableRank = available_rank if selected and item.info.get('winRate') is not None else 0
            suffix = (' · ' + active[hero]) if selected else ''
            if 0 < item.availableRank <= 3:
                suffix += ' · TOP ' + str(item.availableRank)
            item.nameLabel.setText(item.name + suffix)
            item.nameLabel.setToolTip(item.name + suffix)
            item.update()
        self.__update()
        if active:
            self.titleBar.selectSort('winRate')

    def __update(self):
        self.scrollLayout.removeAllWidgets()

        for i, x in enumerate(self.items, start=1):
            x.setCounter(i)
            self.scrollLayout.addWidget(x)

    @asyncSlot(str)
    async def __onSortRequested(self, key: str):
        if not self.items or self.items[0].info.get(key) == None:
            return

        if self.available and self.items[0].info.get('mayhem'):
            self.setAvailable(self.available, force=True)
            return

        if key == 'rank':
            def fun(x): return x.info.get(key)
        else:
            def fun(x): return -x.info.get(key)

        self.items.sort(key=fun)
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
        self.championLabel = QLabel(self.tr("Champion"))
        self.rankLabel = TransparentButton(self.tr("Tier"))
        self.winRateLabel = TransparentButton(self.tr("Win Rate"))
        self.pickRateLabel = TransparentButton(self.tr("Pick Rate"))
        self.banRateLabel = TransparentButton(self.tr("Ban Rate"))
        self.countersLabel = QLabel(self.tr("Counters"))

        self.selected: TransparentButton = self.rankLabel

        self.__initWidget()
        self.__initLayout()

    def __initLayout(self):
        self.hBoxLayout.setContentsMargins(17, 6, 28, 6)

        self.hBoxLayout.addWidget(self.counterLabel, alignment=Qt.AlignCenter)
        # self.hBoxLayout.addSpacing()
        self.hBoxLayout.addWidget(self.championLabel, alignment=Qt.AlignCenter)
        self.hBoxLayout.addSpacerItem(QSpacerItem(
            0, 0, QSizePolicy.Expanding, QSizePolicy.Fixed))
        self.hBoxLayout.addWidget(self.rankLabel, alignment=Qt.AlignCenter)
        # self.hBoxLayout.addSpacing(1)
        self.hBoxLayout.addWidget(self.winRateLabel, alignment=Qt.AlignCenter)
        self.hBoxLayout.addSpacing(1)
        self.hBoxLayout.addWidget(self.pickRateLabel, alignment=Qt.AlignCenter)
        self.hBoxLayout.addWidget(self.banRateLabel, alignment=Qt.AlignCenter)
        self.hBoxLayout.addSpacing(8)
        self.hBoxLayout.addWidget(self.countersLabel, alignment=Qt.AlignCenter)

    def __initWidget(self):
        self.counterLabel.setFixedWidth(30)
        self.counterLabel.setObjectName("counterLabel")
        self.counterLabel.setAlignment(Qt.AlignCenter)
        self.countersLabel.setAlignment(Qt.AlignCenter)
        self.countersLabel.setObjectName("countersLabel")
        self.championLabel.setAlignment(Qt.AlignCenter)
        self.championLabel.setObjectName("championLabel")

        width = 70

        self.rankLabel.setFixedWidth(50)
        self.winRateLabel.setFixedWidth(width)
        self.pickRateLabel.setFixedWidth(width)
        self.banRateLabel.setFixedWidth(width)
        self.countersLabel.setFixedWidth(80)

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
        self.selected.setProperty("selected", False)
        self.selected.style().polish(self.selected)

        widget.setProperty("selected", True)
        widget.style().polish(widget)

        self.selected = widget

    def resetSelected(self):
        self.__setSelected(self.rankLabel)

    def selectSort(self, key):
        self.__setSelected(getattr(self, key + 'Label'))


class ListItem(ColorAnimationFrame):
    availableRank = 0

    def paintEvent(self, event):
        super().paintEvent(event)
        if 0 < self.availableRank <= 3:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(QPen(QColor('#d39a28'), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(self.rect().adjusted(2, 2, -2, -2), 4, 4)
            painter.end()

    def __init__(self, number, info, parent: QWidget = None):
        super().__init__(type=f"tier{info['tier']}", parent=parent)
        # super().__init__(type="default", parent=parent)

        # self.setStyleSheet("border: 1px solid black;")
        self.setFixedWidth(589)

        self.championId = info['championId']
        self.name = info['name']

        self.info = info

        tierIcon = f"app/resource/images/icon-tier-{info['tier']}.svg"

        self.hBoxLayout = QHBoxLayout(self)

        self.numberLabel = BodyLabel(str(number))
        self.nameLabel = BodyLabel(info.get("name"))
        self.championIcon = RoundIcon(info.get("icon"), 28, 2, 2)
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

        if info['winRate']:
            self.winRateLabel = BodyLabel(f"{info['winRate']*100:.2f}%")
        else:
            self.winRateLabel = BodyLabel("--")

        if info['pickRate']:
            self.pickRateLabel = BodyLabel(f"{info['pickRate']*100:.2f}%")
        else:
            self.pickRateLabel = BodyLabel("--")

        if info['banRate']:
            self.banRateLabel = BodyLabel(f"{info['banRate']*100:.2f}%")
        else:
            self.banRateLabel = BodyLabel("--")

        self.countersLabel = QWidget()
        self.countersLayout = QHBoxLayout(self.countersLabel)

        self.countersLayout.setAlignment(Qt.AlignCenter)

        counters = info.get("counters")
        if counters:
            for c in counters:
                icon = RoundIcon(c['icon'], 22, 2, 2)
                self.countersLayout.addWidget(icon)
        else:
            icon = BodyLabel("--")
            self.countersLayout.addWidget(icon)

        self.__initWidget()
        self.__initLayout()

        self.countersLabel.hide()
        if info.get('mayhem'):
            self.winRateLabel.setVisible(True)
            self.pickRateLabel.setVisible(info.get('pickRate') is not None)
            self.banRateLabel.hide()
            self.countersLabel.hide()

    def __initWidget(self):
        self.numberLabel.setFixedWidth(30)
        self.numberLabel.setAlignment(Qt.AlignCenter)
        self.winRateLabel.setAlignment(Qt.AlignCenter)
        self.pickRateLabel.setAlignment(Qt.AlignCenter)
        self.banRateLabel.setAlignment(Qt.AlignCenter)

        width = 70

        # self.tierLabel.setFixedWidth(50)
        self.winRateLabel.setFixedWidth(width)
        self.pickRateLabel.setFixedWidth(width)
        self.banRateLabel.setFixedWidth(width)
        self.countersLabel.setFixedWidth(80)

        self.nameLabel.setContentsMargins(0, 0, 0, 2)
        # self.tierLabel.setContentsMargins(0, 0, 0, 2)
        # self.winRateLabel.setContentsMargins(0, 0, 0, 2)
        # self.pickRateLabel.setContentsMargins(0, 0, 0, 2)
        # self.banRateLabel.setContentsMargins(0, 0, 0, 2)

        self.clicked.connect(self.__onClicked)

    def __initLayout(self):
        self.countersLayout.setContentsMargins(0, 0, 0, 0)
        self.countersLayout.setSpacing(2)

        self.hBoxLayout.addWidget(self.numberLabel, alignment=Qt.AlignCenter)
        self.hBoxLayout.addSpacing(6)
        self.hBoxLayout.addWidget(self.championIcon, alignment=Qt.AlignCenter)
        self.hBoxLayout.addSpacing(6)
        self.hBoxLayout.addWidget(self.nameLabel, alignment=Qt.AlignCenter)
        self.hBoxLayout.addSpacerItem(QSpacerItem(
            0, 0, QSizePolicy.Expanding, QSizePolicy.Fixed))
        self.hBoxLayout.addWidget(self.tierLabel, alignment=Qt.AlignCenter)
        self.hBoxLayout.addSpacing(13)
        self.hBoxLayout.addWidget(self.winRateLabel, alignment=Qt.AlignCenter)
        self.hBoxLayout.addWidget(self.pickRateLabel, alignment=Qt.AlignCenter)
        self.hBoxLayout.addWidget(self.banRateLabel, alignment=Qt.AlignCenter)
        self.hBoxLayout.addSpacing(8)
        self.hBoxLayout.addWidget(self.countersLabel, alignment=Qt.AlignCenter)

    def setCounter(self, x):
        self.numberLabel.setText(str(x))

    def __onClicked(self):
        signalBus.toOpggBuildInterface.emit(self.championId, "", "")
