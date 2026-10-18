import sys
import win32api
import traceback
import asyncio
import time

from qasync import asyncSlot, asyncClose
from PyQt5.QtGui import QColor, QPainter, QIcon, QShowEvent
from PyQt5.QtCore import Qt, pyqtSignal, QSize, QRect, QEvent, QSignalBlocker
from PyQt5.QtWidgets import (QHBoxLayout, QStackedWidget, QWidget, QLabel,
                             QFrame, QVBoxLayout, QSpacerItem, QSizePolicy,
                             QApplication)


from app.common.icons import Icon
from app.lol.connector import connector
from app.lol.opgg import opgg
from app.lol.champions import ChampionAlias
from app.common.logger import logger
from app.common.config import qconfig, cfg
from app.common.style_sheet import StyleSheet
from app.common.signals import signalBus
from app.common.util import getLolClientWindowPos
from app.common.qfluentwidgets import (FramelessWindow, isDarkTheme, BackgroundAnimationWidget,
                                       FluentTitleBar,  ComboBox, BodyLabel, ToolTipFilter,
                                       ToolTipPosition, IndeterminateProgressRing, setTheme,
                                       Theme, PushButton, SearchLineEdit, ToolButton,
                                       FlyoutViewBase, Flyout, FlyoutAnimationType)
from app.components.transparent_button import TransparentToggleButton
from app.view.opgg_tier_interface import TierInterface
from app.view.opgg_build_interface import BuildInterface
from app.view.bp_assistant_widget import ALIASES
from app.view.personal_champions_widget import PersonalChampionsWidget
from app.view.hextech_draft_widget import HextechDraftAssistant

TAG = 'OpggWindow'


class OpggWindowBase(BackgroundAnimationWidget, FramelessWindow):
    def __init__(self, parent=None):
        self._isMicaEnabled = cfg.get(cfg.micaEnabled)
        self._lightBackgroundColor = QColor(243, 243, 243)
        self._darkBackgroundColor = QColor(32, 32, 32)

        super().__init__(parent=parent)

        self.setTitleBar(FluentTitleBar(self))
        self.setMicaEffectEnabled(self._isMicaEnabled)
        self.setContentsMargins(0, 36, 0, 0)

        self.titleBar.hBoxLayout.setContentsMargins(14, 0, 0, 0)
        self.titleBar.maxBtn.setVisible(False)

        qconfig.themeChangedFinished.connect(self._onThemeChangedFinished)

    def setCustomBackgroundColor(self, light, dark):
        self._lightBackgroundColor = QColor(light)
        self._darkBackgroundColor = QColor(dark)
        self._updateBackgroundColor()

    def _normalBackgroundColor(self):
        if not self.isMicaEffectEnabled():
            return self._darkBackgroundColor if isDarkTheme() else self._lightBackgroundColor

        return QColor(0, 0, 0, 0)

    def _onThemeChangedFinished(self):
        if self.isMicaEffectEnabled():
            self.windowEffect.setMicaEffect(self.winId(), isDarkTheme())

    def paintEvent(self, e):
        super().paintEvent(e)
        painter = QPainter(self)
        painter.setPen(Qt.NoPen)
        painter.setBrush(self.backgroundColor)
        painter.drawRect(self.rect())

    def setMicaEffectEnabled(self, isEnabled: bool):
        """ set whether the mica effect is enabled, only available on Win11 """
        if sys.platform != 'win32' or sys.getwindowsversion().build < 22000:
            return

        self._isMicaEnabled = isEnabled

        if isEnabled:
            self.windowEffect.setMicaEffect(self.winId(), isDarkTheme())
        else:
            self.windowEffect.removeBackgroundEffect(self.winId())

        self.setBackgroundColor(self._normalBackgroundColor())

    def isMicaEffectEnabled(self):
        return self._isMicaEnabled

    def setStaysOnTopEnabled(self, enable: bool):
        isMinimized = self.isMinimized()
        isVisiable = self.isVisible()

        if enable:
            flag = self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint
        else:
            flag = self.windowFlags() & ~Qt.WindowType.WindowStaysOnTopHint

        self.setWindowFlags(flag)

        self.windowEffect.enableBlurBehindWindow(self.winId())
        self.windowEffect.addWindowAnimation(self.winId())

        if sys.platform == 'win32' and sys.getwindowsversion().build >= 22000:
            self.windowEffect.addShadowEffect(self.winId())

        if not isMinimized and isVisiable:
            self.show()
        elif isVisiable and isMinimized:
            self.showMinimized()
            self.setWindowState(self.windowState() | Qt.WindowMinimized)

    def show(self):
        self.activateWindow()
        self.setWindowState(self.windowState() & ~
                            Qt.WindowMinimized | Qt.WindowActive)
        self.showNormal()


class OpggWindow(OpggWindowBase):
    def __init__(self, parent=None):
        super().__init__()

        # setTheme(Theme.LIGHT)
        self.vBoxLayout = QVBoxLayout(self)

        self.filterLayout = QHBoxLayout()
        self.searchButton = ToolButton(Icon.SEARCH)
        self.toggleButton = TransparentToggleButton(Icon.APPLIST, Icon.PERSON)
        self.modeComboBox = ComboBox()
        self.regionComboBox = ComboBox()
        self.tierComboBox = ComboBox()
        self.positionComboBox = ComboBox()
        self.bpRevision = 0
        self.bpLoading = False
        self.personalStats = PersonalChampionsWidget(
            lambda id: connector.manager.getChampionNameById(id) if connector.manager else str(id),
            opgg.getBPChampionIcon)
        self.personalButton = PushButton('我的英雄')
        self.personalButton.clicked.connect(self.__togglePersonalStats)
        signalBus.currentSummonerProfileChanged.connect(self.personalStats.setAccount)
        signalBus.lolClientEnded.connect(self.personalStats.disconnect)

        self.debugButton = PushButton()
        self.debugButton.setFixedSize(33, 33)
        self.debugButton.clicked.connect(self.__onDebugButtonClicked)

        self.versionLabel = BodyLabel()

        self.stackedWidget = QStackedWidget()
        self.tierInterface = TierInterface()
        self.buildInterface = BuildInterface()
        self.waitingInterface = WaitingInterface()
        self.errorInterface = ErrorInterface()
        self.homeInterface = HomeInterface()

        # 缓存一个召唤师峡谷的梯队数据，切换位置的时候不重新调 opgg 了
        self.cachedTier = None
        self.cachedRegion = None
        self.cachedRankedTierList = None
        self.displayedBuildKey = None

        self.filterLock = False

        self.__initWindow()
        self.__initLayout()
        self.hextechLive = HextechDraftAssistant(self)
        self.vBoxLayout.insertWidget(1, self.hextechLive.bar)
        QApplication.instance().aboutToQuit.connect(self.hextechLive.stop)
        self.personalStats.setLane(self.positionComboBox.currentData())
        self.personalStats.laneChanged.connect(self.__onPersonalLaneChanged)
        self.positionComboBox.currentIndexChanged.connect(
            lambda _: self.personalStats.setLane(self.positionComboBox.currentData()))
        self.personalStats.championChosen.connect(lambda hero,lane: self.__toChampionBuildInterface(hero,'ranked',lane))

        # self.debugButton.click()
        self.debugButton.setVisible(False)
        self.setHomeInterfaceEnabled(True)
        self.tierInterface.analysis.ruleChanged.connect(self.__analysisRuleChanged)
        self.tierInterface.analysis.championChosen.connect(
            lambda id: self.__toChampionBuildInterface(id, 'ranked', self.positionComboBox.currentData()))
        signalBus.champSelectChanged.connect(self.onChampSelect)
        signalBus.gameStatusChanged.connect(self.onGameStatus)
    def __initWindow(self):
        desktop = QApplication.desktop().availableGeometry()
        # Don't let the fixed size exceed the actual screen (e.g. 1366x768
        # laptops have ~728px of usable height) -- same issue fixed for the
        # main window.
        self.setFixedSize(min(640, desktop.width()),
                          min(826, desktop.height()))
        self.setWindowIcon(QIcon("app/resource/images/opgg.svg"))
        self.setWindowTitle("英雄排行与攻略")

        self.toggleButton.setToolTip(self.tr("Show Tier / Build"))
        self.toggleButton.installEventFilter(ToolTipFilter(
            self.toggleButton, 500, ToolTipPosition.TOP))

        self.modeComboBox.addItem(
            self.tr("Ranked"), icon="app/resource/images/sr-victory.png", userData='ranked')
        # 海克斯大乱斗（ARAM: Mayhem，queue 2400）
        # 官网海克斯数据由独立适配器读取，不替代成普通大乱斗数据。
        self.modeComboBox.addItem(
            self.tr("ARAM: Mayhem"), icon="app/resource/images/ha-victory.png", userData='aram_mayhem')

        self.regionComboBox.addItem(
            self.tr("All regions"), icon="app/resource/images/global.svg", userData="global")
        self.regionComboBox.addItem(
            self.tr("Korea"), icon="app/resource/images/kr.svg", userData="kr")
        self.regionComboBox.addItem('国服', userData='cn')
        self.regionComboBox.addItem('三服共强', userData='consensus')
        self.regionComboBox.setToolTip('国服：腾讯 101；全球/韩服：OP.GG；三服共强：同版本同分路比较')

        self.tierComboBox.addItem(
            self.tr("All"), icon="app/resource/images/UNRANKED.svg", userData="all")
        self.tierComboBox.addItem(
            self.tr("Gold -"), icon="app/resource/images/GOLD.svg", userData="ibsg")
        self.tierComboBox.addItem(
            self.tr("Gold +"), icon="app/resource/images/GOLD.svg", userData="gold_plus")
        self.tierComboBox.addItem(
            self.tr("Platinum +"), icon="app/resource/images/PLATINUM.svg", userData="platinum_plus")
        self.tierComboBox.addItem(
            self.tr("Emerald +"), icon="app/resource/images/EMERALD.svg", userData="emerald_plus")
        self.tierComboBox.addItem(
            self.tr("Diamond +"), icon="app/resource/images/DIAMOND.svg", userData="diamond_plus")
        self.tierComboBox.addItem(
            self.tr("Master"), icon="app/resource/images/MASTER.svg", userData="master")
        self.tierComboBox.addItem(self.tr(
            "Master +"), icon="app/resource/images/MASTER.svg", userData="master_plus")
        self.tierComboBox.addItem(
            self.tr("Grandmaster"), icon="app/resource/images/GRANDMASTER.svg", userData="grandmaster")
        self.tierComboBox.addItem(self.tr(
            "Challenger"), icon="app/resource/images/CHALLENGER.svg", userData="challenger")

        self.positionComboBox.addItem(
            self.tr("Top"), "app/resource/images/icon-position-top.svg", "TOP")
        self.positionComboBox.addItem(
            self.tr("Jungle"), "app/resource/images/icon-position-jng.svg", "JUNGLE")
        self.positionComboBox.addItem(
            self.tr("Mid"), "app/resource/images/icon-position-mid.svg", "MID")
        self.positionComboBox.addItem(
            self.tr("Bottom"), "app/resource/images/icon-position-bot.svg", "ADC")
        self.positionComboBox.addItem(
            self.tr("Support"), "app/resource/images/icon-position-sup.svg", "SUPPORT")

        self.__setComboBoxCurrentData(
            self.tierComboBox, cfg.get(cfg.opggTier))
        self.__setComboBoxCurrentData(
            self.regionComboBox, cfg.get(cfg.opggRegion))
        self.__setComboBoxCurrentData(
            self.positionComboBox, cfg.get(cfg.opggPosition))

        self.stackedWidget.currentChanged.connect(
            self.__onStackedWidgetCurrentChanged)
        self.modeComboBox.currentIndexChanged.connect(
            self.__onFilterTextChanged)
        self.regionComboBox.currentIndexChanged.connect(
            self.__onFilterTextChanged)
        self.tierComboBox.currentIndexChanged.connect(
            self.__onFilterTextChanged)
        self.positionComboBox.currentIndexChanged.connect(
            self.__onFilterTextChanged)

        self.toggleButton.changed.connect(self.__onToggleButtonClicked)
        self.searchButton.clicked.connect(self.__onSearchButtonClicked)

        signalBus.toOpggBuildInterface.connect(
            self.__toChampionBuildInterface)

    def __setComboBoxCurrentData(self, comboBox: ComboBox, data) -> int:
        """
        这 `ComboBox` 居然没提供通过 `userData` 设置当前项的函数，我帮它实现一个

        虽然这函数是 $O(n)$ 的，但 `ComboBox` 提供的 `setCurrentText()` 也是 $O(n)$ 的 ^^
        """

        index = comboBox.findData(data)
        comboBox.setCurrentIndex(index)

    def __initLayout(self):
        self.filterLayout.addWidget(self.toggleButton)
        self.filterLayout.addWidget(self.searchButton)
        self.filterLayout.addWidget(self.modeComboBox)
        self.filterLayout.addWidget(self.regionComboBox)
        self.filterLayout.addWidget(self.tierComboBox)
        self.filterLayout.addWidget(self.positionComboBox)
        self.filterLayout.addWidget(self.personalButton)
        self.filterLayout.addWidget(self.debugButton)
        self.filterLayout.addSpacerItem(QSpacerItem(
            0, 0, QSizePolicy.Expanding,  QSizePolicy.Fixed))
        self.filterLayout.addWidget(self.versionLabel)
        self.filterLayout.addSpacing(4)

        self.stackedWidget.addWidget(self.tierInterface)
        self.stackedWidget.addWidget(self.buildInterface)
        self.stackedWidget.addWidget(self.waitingInterface)
        self.stackedWidget.addWidget(self.errorInterface)
        self.stackedWidget.addWidget(self.homeInterface)
        self.stackedWidget.addWidget(self.personalStats)

        self.vBoxLayout.setAlignment(Qt.AlignTop)
        self.vBoxLayout.addLayout(self.filterLayout)
        self.vBoxLayout.addWidget(self.stackedWidget,1)

    @asyncSlot()
    async def __togglePersonalStats(self):
        if self.stackedWidget.currentWidget() is self.personalStats:
            await self.updateAndSwitchTo(self.tierInterface, self.tierInterface)
            return
        self.setCurrentInterface(self.personalStats)
        if connector.lcuSess is not None and time.time() - self.personalStats.data.get('updated', 0) > 300:
            self.personalStats.requestRefresh()

    def __onPersonalLaneChanged(self,lane):
        if self.positionComboBox.currentData()==lane:
            return
        blocker=QSignalBlocker(self.positionComboBox)
        self.positionComboBox.setCurrentIndex(self.positionComboBox.findData(lane))
        del blocker

    def __onToggleButtonClicked(self, index):
        self.stackedWidget.setCurrentIndex(index)

    def __onStackedWidgetCurrentChanged(self):
        widget = self.stackedWidget.currentWidget()
        self.personalButton.setText('返回排行' if widget is self.personalStats else '我的英雄')
        self.setComboBoxesEnabled(True)

        if widget in [self.waitingInterface, self.homeInterface]:
            self.setComboBoxesEnabled(False)
        elif widget in [self.buildInterface, self.errorInterface, self.personalStats]:
            self.searchButton.setEnabled(False)

        if widget is self.tierInterface \
                and not self.buildInterface.getCurrentChampionId():
            self.toggleButton.setEnabled(False)

        if (index := self.stackedWidget.currentIndex()) in [0, 1]:
            self.toggleButton.setCurrentIcon(index)

    def __onSearchButtonClicked(self):
        # 点击之后弹出的搜索框是空白的，让下方的所有英雄重新显示出来比较符合直觉
        self.tierInterface.tierList.showAllChampions()
        self.tierInterface.analysis.search.clear()

        view = SearchLineEditFlyout()
        Flyout.make(view, self.searchButton, self, isDeleteOnClose=True)
        view.textChanged.connect(self.__onSearchLineTextChanged)

        # 点一下搜索按钮之后，自动让弹出的搜索框获得焦点，可以少点一次鼠标
        view.searchLineEdit.setFocus()

    def __onSearchLineTextChanged(self, text):
        if not self.tierInterface.analysis.isHidden():
            self.tierInterface.analysis.search.setText(text)
            return
        if text == '':
            self.tierInterface.tierList.showAllChampions()
            return

        if ChampionAlias.isAvailable():
            ids = ChampionAlias.getChampionIdsByAliasFuzzily(text)
            self.tierInterface.tierList.filterChampions('championId', ids)
        else:
            self.tierInterface.tierList.filterChampions('name', text)

    def setComboBoxesEnabled(self, enabled):
        self.toggleButton.setEnabled(enabled)
        self.searchButton.setEnabled(enabled)
        self.modeComboBox.setEnabled(enabled)
        self.regionComboBox.setEnabled(enabled)
        self.tierComboBox.setEnabled(enabled)
        self.positionComboBox.setEnabled(enabled)
        self.personalButton.setEnabled(enabled)

    def onChampSelect(self, session):
        # Connector emits the LCU WebSocket event envelope, not just its data.
        if isinstance(session, dict) and session.get('eventType') == 'Delete':
            self.onGameStatus('None')
            return
        if isinstance(session, dict) and isinstance(session.get('data'), dict):
            session = session['data']
        if not isinstance(session, dict) or 'myTeam' not in session:
            return
        self.bpRevision += 1
        local = next((p for p in session.get('myTeam') or [] if p.get('cellId')==session.get('localPlayerCellId')), {})
        lane = ALIASES.get((local.get('assignedPosition') or '').lower())
        if lane:
            self.personalStats.setLane(lane)

    def onGameStatus(self, phase):
        if (phase == 'EndOfGame' and self.stackedWidget.currentWidget() is self.personalStats
                and connector.lcuSess is not None):
            self.personalStats.requestRefresh()
        self.bpRevision += 1
        if phase == 'ChampSelect':
            self.__loadCurrentBP()

    @asyncSlot()
    async def __loadCurrentBP(self):
        if self.bpLoading or connector.lcuSess is None:
            return
        self.bpLoading = True
        revision = self.bpRevision
        try:
            flow = await asyncio.wait_for(connector.getGameflowSession(), 3)
            if flow.get('phase') != 'ChampSelect':
                return
            session = await asyncio.wait_for(connector.getChampSelectSession(), 3)
            if revision == self.bpRevision:
                self.onChampSelect(session)
        except (Exception, asyncio.CancelledError):
            pass  # WebSocket updates still provide the next snapshot.
        finally:
            self.bpLoading = False

    @asyncSlot()
    async def __analysisRuleChanged(self):
        if not self.filterLock and self.stackedWidget.currentWidget() is self.tierInterface:
            await self.updateAndSwitchTo(self.tierInterface, self.tierInterface)

    def setCurrentInterface(self, widget: QWidget):
        self.stackedWidget.setCurrentWidget(widget)

    def setAutoRefreshEnabled(self, enabled):
        """
        设置界面是否随着 Combo Box 的改变而自动刷新

        用于想要一次性设置多个 Combo Box 的值之后再刷新的场景
        """

        self.filterLock = not enabled

    @asyncSlot(int)
    async def __onFilterTextChanged(self, _):
        # 给函数加个互斥锁，防止在该函数内修改了 combo box 的值，导致无限递归
        if self.filterLock:
            return

        # 上方 Combo box 改变的时候，相当于从自己跳转到自己
        current = self.stackedWidget.currentWidget()
        if current is self.personalStats:
            if self.modeComboBox.currentData() == 'ranked':
                return
            current = self.tierInterface

        self.setAutoRefreshEnabled(False)
        await self.updateAndSwitchTo(current, current)

    @asyncSlot(int, str, str)
    async def __toChampionBuildInterface(self, championId, mode, pos):
        key = (mode or self.modeComboBox.currentData(), self.regionComboBox.currentData(),
               self.tierComboBox.currentData(), championId, pos or self.positionComboBox.currentData())
        if key == self.displayedBuildKey:
            self.setCurrentInterface(self.buildInterface)
            return

        self.setAutoRefreshEnabled(False)
        self.buildInterface.setCurrentChampionId(championId)

        if mode:
            self.__setComboBoxCurrentData(self.modeComboBox, mode)

        if pos:
            self.__setComboBoxCurrentData(self.positionComboBox, pos)

        current = self.stackedWidget.currentWidget()
        await self.updateAndSwitchTo(current, self.buildInterface)

    async def updateAndSwitchTo(self, current, to):
        """
        这个函数做三件事情：

        1. 显示转圈界面，并锁住上方的 combo box
        2. 尝试刷新 `to` 界面
        3. 解锁上方的 combo box
        4. - 若更新成功，则转到 `to` 界面
           - 若更新失败，则转到错误界面
        """

        # 显示转圈圈界面，并且锁住上方的 combo box
        self.setCurrentInterface(self.waitingInterface)

        # 如果是在出错的界面请求的更新，则需要知道是因为刷新了啥才进入到的出错界面
        if current is self.errorInterface:
            # 将目标界面置为进入错误界面之前的界面
            to = self.errorInterface.getFromInterface()

        try:
            # 尝试刷新当前的界面
            await self.__updateInterface(to)

            # 让转圈消失，显示界面
            self.setCurrentInterface(to)
        except Exception as e:
            stack = traceback.format_exc()
            logger.error(f"Get OPGG data failed, {stack}\n{e}", TAG)

            # 记录一下是想要进入到哪个界面时加载出错了
            self.errorInterface.setFromInterface(to)
            if self.modeComboBox.currentData() == 'aram_mayhem':
                self.errorInterface.content.setText(
                    '海克斯大乱斗数据加载失败，请检查网络或稍后重试。\n'
                    '未使用普通大乱斗数据替代。\n' + str(e)[:180])
            elif self.regionComboBox.currentData() in ('cn', 'consensus'):
                self.errorInterface.content.setText(
                    '峡谷数据加载失败，请检查网络、段位或版本是否可用。\n' + str(e)[:180])
            else:
                self.errorInterface.content.setText(self.tr('Please check your network connection'))

            # 显示出错的界面
            self.setCurrentInterface(self.errorInterface)
        finally:
            self.setAutoRefreshEnabled(True)

    async def __updateInterface(self, interface: QWidget):
        map = {
            self.tierInterface: self.__updateTierInterface,
            self.buildInterface: self.__updateBuildInterface
        }

        await map[interface]()

    def __setVersionLabel(self, data):
        text = self.tr("Version: ") + data['version']

        if data.get('source') == 'rift-analysis':
            text = data['version'] + (' · 三服' if data['region']=='consensus' else ' · 国服' if data['region']=='cn' else '')
            self.versionLabel.setToolTip(' / '.join(source+': '+date for source,date in data['dates'].items()))
        elif data.get('source') == 'tencent-rift':
            text = data['version'] + ' · 国服'
            self.versionLabel.setToolTip('腾讯 101 统计日期 '+data['statDate'])
        elif data.get('source') == 'tencent-101':
            date = data['statDate']
            text = '国服 · ' + date[4:6] + '-' + date[6:8]
            self.versionLabel.setToolTip('腾讯 101 国服海克斯数据 · 统计日期 ' + date[:4] + '-' + date[4:6] + '-' + date[6:8])
        else:
            self.versionLabel.setToolTip('OP.GG 召唤师峡谷数据')

        self.versionLabel.setText(text)

    def __configureMode(self, mode, region):
        ranked = mode == 'ranked'
        self.personalButton.setVisible(ranked)
        desktop = QApplication.desktop().availableGeometry()
        width = 640
        self.setFixedSize(min(width, desktop.width()), min(826, desktop.height()))
        self.setWindowTitle('腾讯 101 · 海克斯大乱斗' if not ranked else
                            '英雄排行与攻略')
        self.positionComboBox.setToolTip('选择分路查看英雄排行与攻略')

    async def __updateTierInterface(self):
        mode = self.modeComboBox.currentData()
        region = self.regionComboBox.currentData()
        tier = self.tierComboBox.currentData()
        position = self.positionComboBox.currentData()
        self.__configureMode(mode, region)

        cfg.set(cfg.opggRegion, region)
        cfg.set(cfg.opggTier, tier)
        cfg.set(cfg.opggPosition, position)

        logger.info(
            f"Get tier list: {mode}, {region}, {tier}, {position}", TAG)

        # 只有在排位模式下，可以选择对应的分路
        if mode != 'ranked':
            position = 'none'
            self.positionComboBox.setVisible(False)
        else:
            self.positionComboBox.setVisible(True)

        # Tencent Hextech data is national and has no rank filter.
        self.regionComboBox.setVisible(mode != 'aram_mayhem')
        if mode in ('arena', 'aram_mayhem'):
            tier = 'all'
            self.tierComboBox.setVisible(False)
        else:
            self.tierComboBox.setVisible(True)

        if mode == 'ranked':
            if region in ('cn', 'consensus'):
                analysis = self.tierInterface.analysis
                data = await opgg.getRiftAnalysis(region, tier, position,
                    fraction=analysis.fraction.currentData(), only_strong=analysis.strongOnly.isChecked(),
                    strict=analysis.strict.isChecked() and region=='consensus')
                if (mode != self.modeComboBox.currentData() or region != self.regionComboBox.currentData()
                        or position != self.positionComboBox.currentData()):
                    return
                self.__setVersionLabel(data)
                self.tierInterface.showAnalysis(data)
                return
            # rank 模式下，如果是切换了位置选项，会命中 cache，不用重新请求了
            if tier == self.cachedTier and \
                    region == self.cachedRegion and \
                    self.cachedRankedTierList != None:
                res = self.cachedRankedTierList['data'][position]
                data = self.cachedRankedTierList

            # 否则是第一次请求 rank 模式数据，记录一下 cache
            else:
                data = await opgg.getTierList(region, mode, tier)
                self.cachedTier = tier
                self.cachedRegion = region
                self.cachedRankedTierList = data

                res = data['data'][position]

        # 除了 rank 以外的其他模式，该咋整咋整吧
        else:
            data = await opgg.getTierList(region, mode, tier)
            res = data['data']

        self.__setVersionLabel(data)
        self.tierInterface.showAnalysis(None)
        self.tierInterface.tierList.updateList(res)

    async def __updateBuildInterface(self):
        mode = self.modeComboBox.currentData()
        region = self.regionComboBox.currentData()
        tier = self.tierComboBox.currentData()
        position = self.positionComboBox.currentData()
        championId = self.buildInterface.getCurrentChampionId()
        self.__configureMode(mode, region)

        # 只有在排位模式（包括 League Classic，其 build 接口也按分路返回数据）下，可以选择对应的分路
        if mode not in ('ranked', 'classic'):
            position = 'none'
            self.positionComboBox.setVisible(False)
        else:
            self.positionComboBox.setVisible(True)

        # Tencent Hextech data is national and has no rank filter.
        self.regionComboBox.setVisible(mode != 'aram_mayhem')
        if mode in ('arena', 'aram_mayhem'):
            tier = 'all'
            self.tierComboBox.setVisible(False)
        else:
            self.tierComboBox.setVisible(True)

        logger.info(
            f"Get champion build, {mode}, {region}, {tier}, {position}, {championId}", TAG)

        data = await opgg.getChampionBuild(region, mode, championId, position, tier)

        # A slower response for the previous selection must not overwrite a newer hero.
        if (championId != self.buildInterface.getCurrentChampionId() or mode != self.modeComboBox.currentData()
                or region != self.regionComboBox.currentData()
                or (mode == 'ranked' and position != self.positionComboBox.currentData())):
            return
        if mode == 'aram_mayhem' and data['data'].get('championId') != championId:
            raise ValueError('强化推荐所属英雄与当前选择不一致。')

        self.buildInterface.updateInterface(data['data'])
        self.displayedBuildKey = (mode, region, tier, championId, position)

        # 若英雄没有在特定位置下的数据，则根据得到的数据重新设置一下位置的 combo box
        if (pos := data['data'].get('summary', {}).get('position', 'none')) != position \
                and mode in ('ranked', 'classic'):

            # 在设置之前需要锁住 combo box changed 的槽函数，防止它自动刷新
            self.setAutoRefreshEnabled(False)
            self.__setComboBoxCurrentData(self.positionComboBox, pos)
            self.setAutoRefreshEnabled(True)

        self.__setVersionLabel(data)

    @asyncSlot(bool)
    async def __onDebugButtonClicked(self, _):
        await opgg.start()
        await connector.autoStart()
        await ChampionAlias.checkAndUpdate()

        await self.initWindow()

    async def initWindow(self):
        self.__onFilterTextChanged(1)

    def showEvent(self, a0: QShowEvent) -> None:
        """在显示的时候，自动显示在客户端正右侧"""
        self.__loadCurrentBP()

        size: QSize = self.size()
        pos = getLolClientWindowPos()

        if not pos:
            self.__moveRightCenter()
            return super().showEvent(a0)

        # 别问为什么要这么算，我也不知道，反正它能跑
        dpi = self.devicePixelRatioF()
        x = pos.right()
        y = pos.center().y() - size.height() * dpi / 2
        rect = QRect(int(x / dpi), int(y / dpi), size.width(), size.height())

        # 如果超出右边界，则直接 return 了
        screenWidth = win32api.GetSystemMetrics(0)
        if (rect.left() + size.width()) * dpi > screenWidth:
            self.__moveRightCenter()
            return super().showEvent(a0)

        self.setGeometry(rect)
        return super().showEvent(a0)

    def __moveRightCenter(self):
        """
        将窗口移动到屏幕最右侧的中心
        """
        desktop = QApplication.desktop().availableGeometry()
        w, h = desktop.width(), desktop.height()
        self.move(w - self.width(), h // 2 - self.height() // 2)

    def setHomeInterfaceEnabled(self, enabeld):
        interface = self.homeInterface if enabeld else self.tierInterface
        self.stackedWidget.setCurrentWidget(interface)

    def eventFilter(self, obj, e: QEvent):
        # Fix #553
        if e.type() == QEvent.Type.MouseButtonRelease:
            self.adjustSize()

        return super().eventFilter(obj, e)

    def closeEvent(self, e):
        # Fix #555
        e.ignore()
        self.hide()


class WaitingInterface(QFrame):
    def __init__(self, parent: QWidget = None):
        super().__init__(parent)

        self.vBoxLayout = QVBoxLayout(self)
        self.processRing = IndeterminateProgressRing()

        self.__initWidget()
        self.__initLayout()

        StyleSheet.OPGG_WAITING_INTERFACE.apply(self)

    def __initWidget(self):
        pass

    def __initLayout(self):
        self.vBoxLayout.setAlignment(Qt.AlignCenter)
        self.vBoxLayout.addWidget(self.processRing, alignment=Qt.AlignCenter)


class ErrorInterface(QFrame):
    def __init__(self, parent: QWidget = None):
        super().__init__(parent)

        self.vBoxLayout = QVBoxLayout(self)
        self.title = QLabel(self.tr("Fetch data failed 😭"))
        self.content = QLabel(self.tr("Please wait and try again"))

        self.fromInterface: QWidget = None

        self.__initWidget()
        self.__initLayout()

        StyleSheet.OPGG_ERROR_INTERFACE.apply(self)

    def setFromInterface(self, interface: QWidget):
        self.fromInterface = interface

    def getFromInterface(self):
        return self.fromInterface

    def __initWidget(self):
        self.title.setObjectName("titleLabel")
        self.content.setObjectName("contentLabel")

    def __initLayout(self):
        self.vBoxLayout.setAlignment(Qt.AlignCenter)
        self.vBoxLayout.addWidget(self.title, alignment=Qt.AlignCenter)
        self.vBoxLayout.addWidget(self.content, alignment=Qt.AlignCenter)


class HomeInterface(QFrame):
    def __init__(self, parent: QWidget = None):
        super().__init__(parent)

        self.vBoxLayout = QVBoxLayout(self)
        self.title = QLabel(self.tr("Waiting for LOL Client"))

        self.__initWidget()
        self.__initLayout()

        StyleSheet.OPGG_HOME_INTERFACE.apply(self)

    def setFromInterface(self, interface: QWidget):
        self.fromInterface = interface

    def getFromInterface(self):
        return self.fromInterface

    def __initWidget(self):
        self.title.setObjectName("titleLabel")

    def __initLayout(self):
        self.vBoxLayout.setAlignment(Qt.AlignCenter)
        self.vBoxLayout.addWidget(self.title, alignment=Qt.AlignCenter)


class SearchLineEditFlyout(FlyoutViewBase):
    textChanged = pyqtSignal(str)

    def __init__(self, parent: QWidget = None):
        super().__init__(parent)

        self.vBoxLayout = QVBoxLayout(self)
        self.searchLineEdit = SearchLineEdit()
        self.vBoxLayout.addWidget(self.searchLineEdit)

        self.searchLineEdit.textChanged.connect(self.textChanged)
        self.searchLineEdit.setPlaceholderText(self.tr("Search champions"))
        self.searchLineEdit.setMinimumWidth(200)
