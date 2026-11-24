import sys
import win32api
import traceback
import asyncio
import time
import copy
from datetime import datetime
from pathlib import Path

from qasync import asyncSlot, asyncClose
from PyQt5.QtGui import QColor, QPainter, QIcon, QShowEvent
from PyQt5.QtCore import Qt, pyqtSignal, QSize, QRect, QEvent, QSignalBlocker
from PyQt5.QtWidgets import (QHBoxLayout, QStackedWidget, QWidget, QLabel,
                             QFrame, QVBoxLayout, QSpacerItem, QSizePolicy,
                             QApplication, QListWidget, QListWidgetItem)


from app.common.icons import Icon
from app.lol.connector import connector, gameIconCachePath
from app.lol.opgg import opgg
from app.lol.champions import ChampionAlias
from app.lol.rift_analysis import bp_snapshot
from app.lol.draft_follow import DraftFollow, draft_target
from app.lol.draft_master import locked_teams, recommend, identify_opponent
from app.lol.ranking_cache import RankingCache
from app.lol.draft_availability import DraftAvailability
from app.common.logger import logger
from app.common.config import qconfig, cfg, LOCAL_PATH
from app.common.style_sheet import StyleSheet
from app.common.signals import signalBus
from app.common.util import getLolClientWindowPos
from app.common.qfluentwidgets import (FramelessWindow, isDarkTheme, BackgroundAnimationWidget,
                                       FluentTitleBar,  ComboBox, BodyLabel, ToolTipFilter,
                                       ToolTipPosition, IndeterminateProgressRing, setTheme,
                                       Theme, PushButton, SearchLineEdit, ToolButton,
                                       FlyoutViewBase, Flyout, FlyoutAnimationType, CheckBox, CaptionLabel)
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
        self.draftEpoch = 0
        self._jungleOptions = None
        self.bpLoading = False
        self.clientLane = None
        self.loadRevision = 0
        self._loadTasks = set()
        self._stopping = False
        self.requestedInterface = None
        self.requestedMode = 'ranked'
        self.draftFollow = DraftFollow(cfg.get(cfg.autoFollowDraft))
        self.currentDraftFlow = None
        self.currentDraftSession = None
        self._draftAvailabilityKey = None
        self._draftAvailabilityRevision = 0
        self._pickableTask = None
        self.rankingCache = RankingCache(LOCAL_PATH + '/cache/rankings')
        self.personalStats = PersonalChampionsWidget(
            lambda id: connector.manager.getChampionNameById(id) if connector.manager else str(id),
            opgg.getBPChampionIcon)
        self.personalButton = PushButton('战绩统计')
        self.personalButton.clicked.connect(self.__togglePersonalStats)
        self.personalContext = CaptionLabel('本人战绩 · 仅已读取范围')
        self.personalContext.hide()

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
        self.enemyJungle = ComboBox()
        self.enemyJungle.setAccessibleName('指定敌方对位英雄')
        self.enemyJungle.setToolTip('对面有摇摆英雄时，可从已锁英雄中指定对位')
        self.enemyJungle.hide()
        self.masterList = QListWidget()
        self.masterList.setAccessibleName('BP大师英雄推荐')
        self.masterList.setIconSize(QSize(36, 36))
        self.masterList.setWordWrap(True)
        self.masterList.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.masterList.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.masterList.hide()
        self._shownPicks = ()
        self._duelKey = self._duelTask = None
        self._duelRows = {}
        self._duelSource = ''
        self._iconTasks = {}
        self.masterLabel = BodyLabel()
        self.masterLabel.setWordWrap(True)
        self.masterLabel.setTextFormat(Qt.PlainText)
        self.masterLabel.setContentsMargins(8, 4, 8, 4)
        self.masterLabel.hide()
        self.statusBar = QWidget()
        status_layout = QHBoxLayout(self.statusBar)
        status_layout.setContentsMargins(4, 0, 4, 0)
        status_layout.setSpacing(8)
        self.rankStatus = CaptionLabel('选人时同步模式和分路')
        self.rankStatus.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.followCheck = CheckBox('自动跟随')
        self.followCheck.setChecked(self.draftFollow.enabled)
        self.followCheck.toggled.connect(self.__onFollowToggled)
        self.retryRankingButton = PushButton('刷新')
        self.retryRankingButton.setFixedWidth(55)
        self.retryRankingButton.clicked.connect(self.__retryRanking)
        status_layout.addWidget(self.rankStatus, 1)
        status_layout.addWidget(self.followCheck)
        status_layout.addWidget(self.retryRankingButton)
        self.statusBar.setFixedHeight(30)

        self.displayedBuildKey = None
        self._buildVersionData = None
        self.displayedRankingKey = None
        self.rankingLoadedAt = 0
        self._displayedTierPayload = None
        self._rankingStatus = None

        self.filterLock = False
        self._pendingFilterToken = None

        self.__initWindow()
        self.__initLayout()
        self.hextechLive = HextechDraftAssistant(self)
        self.vBoxLayout.insertWidget(1, self.hextechLive.bar)
        QApplication.instance().aboutToQuit.connect(self.stop)
        self.personalStats.setLane(self.positionComboBox.currentData())
        self.personalStats.laneChanged.connect(self.__onPersonalLaneChanged)
        self.positionComboBox.currentIndexChanged.connect(
            lambda _: self.personalStats.setLane(self.positionComboBox.currentData()))
        self.personalStats.championChosen.connect(lambda hero, lane: self.__toChampionBuildInterface(
            hero, 'aram_mayhem' if self.personalStats.queueCombo.currentData() == 'mayhem' else 'ranked',
            'none' if self.personalStats.queueCombo.currentData() == 'mayhem' else lane))
        self.personalStats.historyChanged.connect(self.__onPersonalHistory)
        signalBus.currentSummonerProfileChanged.connect(self.__onAccount)

        # self.debugButton.click()
        self.debugButton.setVisible(False)
        self.setHomeInterfaceEnabled(True)
        self.tierInterface.analysis.ruleChanged.connect(self.__analysisRuleChanged)
        self.tierInterface.analysis.championChosen.connect(
            lambda id: self.__toChampionBuildInterface(id, 'ranked', self.positionComboBox.currentData()))
        signalBus.champSelectChanged.connect(self.onChampSelect)
        signalBus.gameStatusChanged.connect(self.onGameStatus)
        signalBus.lolClientEnded.connect(self.clearOpponents)
        signalBus.lolClientChanged.connect(self.clearOpponents)
        self.enemyJungle.currentIndexChanged.connect(self.refreshDraftMaster)
        self.tierInterface.tierList.favoriteChanged.connect(self.refreshDraftMaster)
        for combo in (self.modeComboBox, self.regionComboBox, self.positionComboBox, self.tierComboBox):
            combo.currentIndexChanged.connect(self.refreshDraftMaster)
        self.__updateFollowStatus()

    def refreshDraftMaster(self, *args):
        session = self.currentDraftSession or {}
        target = draft_target(self.currentDraftFlow, session)
        if (not target or target['mode'] != 'ranked' or self.modeComboBox.currentData() != 'ranked'
                or self._stopping or not session):
            self.masterList.hide()
            self.masterLabel.hide()
            self.enemyJungle.hide()
            return
        enemies, allies, assigned, locked = locked_teams(session)
        if locked:
            self.masterList.hide()
            self.masterLabel.hide()
            self.enemyJungle.hide()
            return
        lane = assigned or self.positionComboBox.currentData()
        self.enemyJungle.setVisible(bool(enemies))
        lane_text = self.positionComboBox.itemText(self.positionComboBox.findData(lane))
        header = f'BP大师 · {lane_text} · 敌方已锁 {len(enemies)} 人'
        if not assigned:
            header += ' · 手选分路'
        tier_list = self.tierInterface.tierList
        rows = []
        payload = self._displayedTierPayload or {}
        if payload and self.displayedRankingKey == self.__rankingKey():
            if payload.get('source') == 'rift-analysis':
                if payload.get('position') == lane:
                    rows = payload.get('legacyRows') or [
                        dict(championId=r['championId'], name=r['name'],
                             rank=i, winRate=r['evidence'].get('cn', {}).get('winRate'),
                             icon=r.get('icon'), counters=[{'championId': h} for h in
                                 r['evidence'].get('cn', {}).get('counterIds', [])])
                        for i, r in enumerate(payload['data'], 1)]
            elif isinstance(payload.get('data'), dict):
                rows = payload['data'].get(lane, [])
        lines = []
        picks = []
        if enemies:
            def name(hero):
                return connector.manager.getChampionNameById(hero) if connector.manager else str(hero)
            options = (lane, tuple((h, name(h)) for h in sorted(enemies)))
            role = '打野' if lane == 'JUNGLE' else '对位'
            if options != self._jungleOptions:
                previous = self.enemyJungle.currentData() if self._jungleOptions and self._jungleOptions[0] == lane else 0
                with QSignalBlocker(self.enemyJungle):
                    self.enemyJungle.clear()
                    self.enemyJungle.addItem('敌方' + role + ' · 自动识别', userData=0)
                    for hero, label in options[1]:
                        self.enemyJungle.addItem('指定' + role + ' · ' + label, userData=hero)
                    self.enemyJungle.setCurrentIndex(max(0, self.enemyJungle.findData(previous)))
                self._jungleOptions = options
            opponent, basis = identify_opponent(session, enemies, self.enemyJungle.currentData(), rows, lane)
            header += f' · {name(opponent)}（{basis}）' if opponent else f' · 对方{role}待确认'
            key = (self.regionComboBox.currentData(), self.tierComboBox.currentData(), lane,
                   opponent, payload.get('version')) if opponent and rows and payload.get('version') else None
            if key != self._duelKey:
                if self._duelTask:
                    self._duelTask.cancel()
                self._duelKey, self._duelRows, self._duelSource = key, {}, ''
                if key and opgg.session and not opgg.session.closed:
                    self._duelSource = '查询对位中'
                    self._duelTask = asyncio.create_task(self.loadDuel(key))
            picks = recommend(lane, enemies, allies, tier_list.draftAvailability,
                              tier_list.preferences, rows, name, opponent, self._duelRows if key else {})
            header += f' · {len(picks)} 个候选'
            if rows:
                source_name = {'global':'全球', 'kr':'韩服', 'cn':'国服', 'consensus':'国服'}.get(
                    self.regionComboBox.currentData(), '')
                header += f' · 结合分路榜（{source_name}）'
            if key and self._duelSource:
                lines.append(self._duelSource)
            if tier_list.draftAvailability.pickable is None:
                lines.append('可选待确认')
            if not picks:
                lines.append('当前候选里暂无合适推荐')
        else:
            lines.append('等待对面锁定英雄')
        self.showMasterPicks(picks)
        self.masterLabel.setText(' · '.join([header, *lines]))
        self.masterLabel.show()

    async def loadDuel(self, key):
        try:
            rows, source = await asyncio.wait_for(opgg.draftLaneMatchups(*key), 12)
            status = source + '同路对局' if rows else '无对位样本'
        except asyncio.CancelledError:
            return
        except Exception:
            rows, status = {}, '对位统计暂缺'
        if self._duelKey == key and not self._stopping:
            self._duelRows, self._duelSource = rows, status
            self.refreshDraftMaster()

    def showMasterPicks(self, picks):
        self.masterList.setVisible(bool(picks))
        if tuple(picks) == self._shownPicks:
            return
        self._shownPicks = tuple(picks)
        scroll = self.masterList.verticalScrollBar().value()
        self.masterList.clear()
        for index, pick in enumerate(picks, 1):
            path = Path(pick.icon) if pick.icon else Path(gameIconCachePath('champion', pick.hero))
            if not path.is_file():
                path = Path(gameIconCachePath('champion', pick.hero))
            item = QListWidgetItem(f'{index}. {pick.name}　{pick.stats}\n{pick.reason}')
            item.setData(Qt.UserRole, pick.hero)
            item.setToolTip(item.text())
            item.setIcon(QIcon(str(path) if path.is_file() else 'app/resource/images/champion-0.png'))
            self.masterList.addItem(item)
            if not path.is_file() and pick.hero not in self._iconTasks and connector.lcuSess and opgg.assetSession:
                self._iconTasks[pick.hero] = asyncio.create_task(self.loadMasterIcon(pick.hero))
        self.masterList.setFixedHeight(min(180, 8 + sum(self.masterList.sizeHintForRow(i)
                                                        for i in range(min(4, len(picks))))))
        self.masterList.verticalScrollBar().setValue(scroll)

    async def loadMasterIcon(self, hero):
        try:
            icon = QIcon(await opgg.getBPChampionIcon(hero))
            for i in range(self.masterList.count()):
                item = self.masterList.item(i)
                if item.data(Qt.UserRole) == hero:
                    item.setIcon(icon)
        except Exception:
            pass
        finally:
            self._iconTasks.pop(hero, None)

    def __onAccount(self, profile):
        previous = self.personalStats.puuid
        self.tierInterface.tierList.setAccount(profile)
        self.personalStats.setAccount(profile)
        self.refreshDraftMaster()
        if (self.personalStats.puuid and self.personalStats.puuid != previous
                and connector.lcuSess is not None
                and time.time() - self.personalStats.data.get('updated', 0) > 300):
            self.personalStats.requestRefresh()

    def __onPersonalHistory(self, records):
        self.tierInterface.tierList.setAccount(self.personalStats.puuid)
        self.tierInterface.tierList.setRecent(records)
        self.refreshDraftMaster()

    def __updateFollowStatus(self):
        paused = self.draftFollow.enabled and self.draftFollow.suspended
        self.followCheck.setText('跟随已暂停' if paused else '自动跟随')
        self.followCheck.setToolTip('本局已手动改选，自动跟随暂停；取消后重新勾选可恢复，下次选人也会恢复。' if paused else
            '选人时跟随已识别的游戏模式和本人分路；手动改选后本局暂停，不改变游戏里的选择。')

    def __manualDraftSelection(self):
        self.draftFollow.user_override()
        self.__updateFollowStatus()

    def __onModeChanged(self, index):
        if not self.filterLock:
            self.__manualDraftSelection()
            self.__onFilterTextChanged(index)

    def __onPositionChanged(self, index):
        if not self.filterLock:
            self.__manualDraftSelection()
            self.__onFilterTextChanged(index)

    def __onOtherFilterChanged(self, index):
        if not self.filterLock:
            self.__onFilterTextChanged(index)

    def __onFollowToggled(self, enabled):
        cfg.set(cfg.autoFollowDraft, enabled)
        self.draftFollow.set_enabled(enabled)
        self.__updateFollowStatus()
        if enabled:
            if self.currentDraftFlow and self.currentDraftSession:
                self.__applyDraftFollow(self.currentDraftFlow, self.currentDraftSession)
            else:
                self.__loadCurrentBP()

    def __applyDraftFollow(self, flow, session, revision=None):
        target = self.draftFollow.observe(flow, session, revision=revision)
        self.__updateFollowStatus()
        if target is None:
            return
        blockers = [QSignalBlocker(self.modeComboBox), QSignalBlocker(self.positionComboBox),
                    QSignalBlocker(self.personalStats)]
        self.__setComboBoxCurrentData(self.modeComboBox, target['mode'])
        if target['lane']:
            self.__setComboBoxCurrentData(self.positionComboBox, target['lane'])
            self.personalStats.setLane(target['lane'])
        del blockers
        self.requestedMode = target['mode']
        self.__configureMode(target['mode'], self.regionComboBox.currentData())
        self.hextechLive.refreshBar()
        if self.stackedWidget.currentWidget() is not self.personalStats:
            self.__refreshRanking()

    def clearOpponents(self, *args):
        self.bpRevision += 1
        self.loadRevision += 1
        self.clientLane = None
        self.currentDraftFlow = self.currentDraftSession = None
        self.draftEpoch += 1
        self._jungleOptions = self._duelKey = None
        self.draftFollow.reset()
        self.personalStats.disconnect()
        self.tierInterface.tierList.setAccount('')
        self.__updateFollowStatus()
        self.tierInterface.tierList.setOpponents(())
        self.clearDraftAvailability()

    def clearDraftAvailability(self):
        self._draftAvailabilityRevision += 1
        self._draftAvailabilityKey = None
        if self._pickableTask:
            self._pickableTask.cancel()
            self._pickableTask = None
        self.tierInterface.tierList.setDraftAvailability(DraftAvailability())
        self.masterLabel.hide()
        self.masterList.hide()
        self.enemyJungle.hide()

    def updateDraftAvailability(self, session):
        state = DraftAvailability.from_session(session)
        key = (session.get('gameId'), session.get('localPlayerCellId'), state.banned, state.picked)
        if key == self._draftAvailabilityKey:
            return  # Ignore timer/hover chatter instead of polling the client repeatedly.
        self.clearDraftAvailability()
        self._draftAvailabilityKey = key
        self.tierInterface.tierList.setDraftAvailability(state)
        if connector.lcuSess is not None and not session.get('benchEnabled') and not self._stopping:
            revision = self._draftAvailabilityRevision
            self._pickableTask = asyncio.create_task(self.loadDraftAvailability(session, revision))

    async def loadDraftAvailability(self, session, revision):
        try:
            await asyncio.sleep(.15)  # Coalesce closely spaced ban/pick events.
            pickable = await asyncio.wait_for(connector.getPickableChampionIds(), 3)
            if revision == self._draftAvailabilityRevision and not self._stopping:
                self.tierInterface.tierList.setDraftAvailability(
                    DraftAvailability.from_session(session, pickable))
                self.refreshDraftMaster()
        except (Exception, asyncio.CancelledError):
            pass  # Optional endpoint: bans/picks stay visible, ownership stays unknown.

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
            self.__onModeChanged)
        self.regionComboBox.currentIndexChanged.connect(
            self.__onOtherFilterChanged)
        self.tierComboBox.currentIndexChanged.connect(
            self.__onOtherFilterChanged)
        self.positionComboBox.currentIndexChanged.connect(
            self.__onPositionChanged)

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
        if index >= 0:
            comboBox.setCurrentIndex(index)
        return index

    def __initLayout(self):
        self.filterLayout.addWidget(self.toggleButton)
        self.filterLayout.addWidget(self.searchButton)
        self.filterLayout.addWidget(self.modeComboBox)
        self.filterLayout.addWidget(self.regionComboBox)
        self.filterLayout.addWidget(self.tierComboBox)
        self.filterLayout.addWidget(self.positionComboBox)
        self.filterLayout.addWidget(self.personalButton)
        self.filterLayout.addWidget(self.personalContext)
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
        self.vBoxLayout.addWidget(self.statusBar)
        self.vBoxLayout.addWidget(self.enemyJungle, 0, Qt.AlignLeft)
        self.vBoxLayout.addWidget(self.masterLabel)
        self.vBoxLayout.addWidget(self.masterList)
        self.vBoxLayout.addWidget(self.stackedWidget,1)

    @asyncSlot()
    async def __togglePersonalStats(self):
        if self.stackedWidget.currentWidget() is self.personalStats:
            await self.showRanking()
            return
        self.loadRevision += 1
        self.personalStats.setMode(self.modeComboBox.currentData())
        self.setCurrentInterface(self.personalStats)
        if (connector.lcuSess is not None and self.personalStats.needsRefresh()
                and (self.personalStats.task is None or self.personalStats.task.done())):
            self.personalStats.requestRefresh()

    def __onPersonalLaneChanged(self,lane):
        if lane == 'ALL' or self.positionComboBox.currentData()==lane:
            return
        self.__manualDraftSelection()
        blocker=QSignalBlocker(self.positionComboBox)
        self.positionComboBox.setCurrentIndex(self.positionComboBox.findData(lane))
        del blocker

    @asyncSlot(int)
    async def __onToggleButtonClicked(self, index):
        target = self.tierInterface if index == 0 else self.buildInterface
        if target is self.tierInterface:
            await self.showRanking()
        else:
            await self.updateAndSwitchTo(self.stackedWidget.currentWidget(), target)

    def __onStackedWidgetCurrentChanged(self):
        widget = self.stackedWidget.currentWidget()
        self.personalButton.setText('返回排行' if widget is self.personalStats else '战绩统计')
        self.personalButton.setVisible(True)
        self.retryRankingButton.setVisible(widget not in (self.personalStats, self.homeInterface))
        self.rankStatus.setVisible(widget is not self.personalStats)
        self.statusBar.setVisible(widget is not self.personalStats)
        self.__configureFilterVisibility()
        self.setComboBoxesEnabled(True)

        if widget is self.homeInterface:
            self.setComboBoxesEnabled(False)
        elif widget is self.waitingInterface:
            self.toggleButton.setEnabled(False)
            self.searchButton.setEnabled(False)
            self.personalButton.setEnabled(False)
        elif widget in [self.buildInterface, self.errorInterface, self.personalStats]:
            self.searchButton.setEnabled(False)

        if widget is self.tierInterface \
                and not self.buildInterface.getCurrentChampionId():
            self.toggleButton.setEnabled(False)

        if (index := self.stackedWidget.currentIndex()) in [0, 1]:
            self.toggleButton.setCurrentIcon(index)

    def __configureFilterVisibility(self):
        personal = self.stackedWidget.currentWidget() is self.personalStats
        ranked = self.modeComboBox.currentData() == 'ranked'
        for widget in (self.toggleButton, self.searchButton, self.modeComboBox, self.versionLabel):
            widget.setVisible(not personal)
        for widget in (self.regionComboBox, self.tierComboBox, self.positionComboBox):
            widget.setVisible(ranked and not personal)
        self.personalContext.setVisible(personal)
        self.setWindowTitle('战绩统计' if personal else
            '英雄排行与攻略' if ranked else '腾讯 101 · 海克斯大乱斗')

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

    def onChampSelect(self, session, flow=None, follow_revision=None):
        # Connector emits the LCU WebSocket event envelope, not just its data.
        if isinstance(session, dict) and session.get('eventType') == 'Delete':
            self.onGameStatus('None')
            return
        if isinstance(session, dict) and isinstance(session.get('data'), dict):
            session = session['data']
        if not isinstance(session, dict) or 'myTeam' not in session:
            return
        self.bpRevision += 1
        opponents, _ = bp_snapshot(session)
        self.tierInterface.tierList.setOpponents(opponents)
        local = next((p for p in session.get('myTeam') or [] if p.get('cellId')==session.get('localPlayerCellId')), {})
        lane = ALIASES.get((local.get('assignedPosition') or '').lower())
        self.clientLane = lane
        old_game = (self.currentDraftSession or {}).get('gameId')
        if old_game and session.get('gameId') and old_game != session['gameId']:
            self.currentDraftFlow = None
            self._jungleOptions = None
            self.draftEpoch += 1
        self.currentDraftSession = session
        self.updateDraftAvailability(session)
        if flow is not None:
            self.currentDraftFlow = flow
            # A fresh GET also recovers a missed WebSocket phase transition.
            # Reads started before a phase/manual change cannot reset that state.
            if (follow_revision is not None and follow_revision == self.draftFollow.revision
                    and flow.get('phase') == 'ChampSelect'
                    and self.draftFollow.phase not in (None, 'ChampSelect')):
                follow_revision = self.draftFollow.on_phase('ChampSelect')
        if self.currentDraftFlow:
            self.__applyDraftFollow(self.currentDraftFlow, session, follow_revision)
        elif connector.lcuSess is not None:
            self.__loadCurrentBP()
        self.refreshDraftMaster()

    @asyncSlot()
    async def __refreshRanking(self):
        await self.showRanking()

    def onGameStatus(self, phase):
        if (phase == 'EndOfGame' and self.stackedWidget.currentWidget() is self.personalStats
                and connector.lcuSess is not None):
            self.personalStats.requestRefresh()
        self.bpRevision += 1
        if phase != self.draftFollow.phase:
            self.draftEpoch += 1
            self._jungleOptions = self._duelKey = None
        self.draftFollow.on_phase(phase)
        self.__updateFollowStatus()
        if phase == 'ChampSelect':
            self.__loadCurrentBP()
        else:
            self.clientLane = None
            self.currentDraftFlow = self.currentDraftSession = None
            self.tierInterface.tierList.setOpponents(())
            self.clearDraftAvailability()

    @asyncSlot()
    async def __loadCurrentBP(self):
        if self.bpLoading or connector.lcuSess is None:
            return
        self.bpLoading = True
        revision = self.bpRevision
        epoch = self.draftEpoch
        follow_revision = self.draftFollow.revision
        try:
            flow = await asyncio.wait_for(connector.getGameflowSession(), 3)
            if flow.get('phase') != 'ChampSelect':
                if revision == self.bpRevision:
                    self.currentDraftFlow = self.currentDraftSession = None
                    self.tierInterface.tierList.setOpponents(())
                    self.clearDraftAvailability()
                return
            session = await asyncio.wait_for(connector.getChampSelectSession(), 3)
            if epoch != self.draftEpoch:
                return
            if revision != self.bpRevision:
                # Picks/timer updates can arrive during both GETs. Keep the newer
                # WebSocket session and attach the gameflow of this same draft.
                session = self.currentDraftSession
                if not draft_target(flow, session):
                    return
            self.onChampSelect(session, flow=flow, follow_revision=follow_revision)
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
        if not enabled:
            self._pendingFilterToken = None

    def __onFilterTextChanged(self, _):
        token = object()
        self._pendingFilterToken = token
        self.__loadFilters(token)

    @asyncSlot(object)
    async def __loadFilters(self, token):
        # Coalesce selector callbacks queued in the same event-loop turn. An
        # explicit awaited load consumes them instead of being superseded by one.
        if self.filterLock or token is not self._pendingFilterToken:
            return
        self._pendingFilterToken = None

        mode = self.modeComboBox.currentData()
        mode_changed = mode != self.requestedMode
        self.requestedMode = mode

        current = self.stackedWidget.currentWidget()
        if mode_changed:
            current = self.tierInterface
        elif current is self.personalStats:
            if mode == 'ranked':
                return
            current = self.tierInterface
        elif current not in (self.tierInterface, self.buildInterface):
            current = self.requestedInterface or self.tierInterface

        await self.updateAndSwitchTo(current, current)

    @asyncSlot(int, str, str)
    async def __toChampionBuildInterface(self, championId, mode, pos):
        self.setAutoRefreshEnabled(False)
        self.buildInterface.setCurrentChampionId(championId)
        if mode:
            self.__setComboBoxCurrentData(self.modeComboBox, mode)
        if pos:
            self.__setComboBoxCurrentData(self.positionComboBox, pos)
        target_mode = self.modeComboBox.currentData()
        key = (target_mode, self.regionComboBox.currentData(),
               'all' if target_mode == 'aram_mayhem' else self.tierComboBox.currentData(), championId,
               'none' if target_mode == 'aram_mayhem' else self.positionComboBox.currentData())
        if key == self.displayedBuildKey:
            self._pendingFilterToken = None
            self.loadRevision += 1
            self.requestedInterface = self.buildInterface
            self.requestedMode = self.modeComboBox.currentData()
            self.setAutoRefreshEnabled(True)
            self.setCurrentInterface(self.buildInterface)
            if self._buildVersionData:
                self.__setVersionLabel(self._buildVersionData)
            return

        current = self.stackedWidget.currentWidget()
        await self.updateAndSwitchTo(current, self.buildInterface)

    async def showRanking(self):
        """Navigation reuses an exact, recently loaded view, including its scroll position."""
        if self._stopping:
            return
        if (self.displayedRankingKey == self.__rankingKey()
                and time.time() - self.rankingLoadedAt < self.rankingCache.fresh_for):
            self._pendingFilterToken = None
            self.loadRevision += 1
            self.requestedInterface = self.tierInterface
            self.requestedMode = self.modeComboBox.currentData()
            self.setAutoRefreshEnabled(True)
            self.setCurrentInterface(self.tierInterface)
            if self._rankingStatus:
                self.rankStatus.setText(self._rankingStatus[0])
                self.rankStatus.setToolTip(self._rankingStatus[1])
                if '正在更新' in self.rankStatus.text():
                    self.__setRankingStatus(self.rankStatus.text().replace('正在更新', '点击刷新可更新'),
                                            '已返回当前筛选条件的缓存；点击刷新可重新请求。')
            # The build page may have replaced the shared version label.
            self.__setVersionLabel(self._displayedTierPayload)
            return
        await self.updateAndSwitchTo(self.tierInterface, self.tierInterface)

    def __setRankingStatus(self, text, tooltip):
        self.rankStatus.setText(text)
        self.rankStatus.setToolTip(tooltip)
        self._rankingStatus = (text, tooltip)

    async def updateAndSwitchTo(self, current, to):
        if self._stopping:
            return
        task = asyncio.current_task()
        self._loadTasks.add(task)
        try:
            return await self.__loadAndSwitchTo(current, to)
        except asyncio.CancelledError:
            # qasync's older slot wrapper reports CancelledError as an unhandled
            # callback exception. Quiet only our own shutdown cancellations.
            if not self._stopping:
                raise
        finally:
            self._loadTasks.discard(task)

    def stop(self):
        self._stopping = True
        if self._duelTask:
            self._duelTask.cancel()
        for task in tuple(self._iconTasks.values()):
            task.cancel()
        self.clearDraftAvailability()
        self.loadRevision += 1
        self._pendingFilterToken = None
        self.hextechLive.stop()
        self.personalStats.stop()
        for task in tuple(self._loadTasks):
            task.cancel()

    async def __loadAndSwitchTo(self, current, to):
        """加载最新选择；等待期间允许改选，较早请求的结果和错误均丢弃。"""
        self._pendingFilterToken = None

        # 如果是在出错的界面请求的更新，则需要知道是因为刷新了啥才进入到的出错界面
        if current is self.errorInterface:
            # 将目标界面置为进入错误界面之前的界面
            to = self.errorInterface.getFromInterface()
        if to not in (self.tierInterface, self.buildInterface):
            to = self.tierInterface
        self.loadRevision += 1
        revision = self.loadRevision
        selection = self.__filterKey()
        self.requestedInterface = to
        self.requestedMode = self.modeComboBox.currentData()
        # filterLock only batches synchronous selector changes, never network waits.
        self.setAutoRefreshEnabled(True)
        cache_key = self.__rankingKey() if to is self.tierInterface else None
        if to is not self.tierInterface or self.displayedRankingKey != cache_key:
            self.setCurrentInterface(self.waitingInterface)
            self.rankStatus.setText('正在读取排行…' if to is self.tierInterface else '正在读取攻略…')
        cached = await asyncio.to_thread(self.rankingCache.get, cache_key) if cache_key else None
        if not self.__requestCurrent(revision, selection):
            return
        if cached:
            try:
                if not await self.__applyTierPayload(cached['payload'], revision, selection):
                    return
            except (KeyError, TypeError, ValueError, AttributeError):
                cached = None
        if cached:
            self.setCurrentInterface(self.tierInterface)
            stamp = datetime.fromtimestamp(cached['storedAt']).strftime('%m-%d %H:%M')
            self.rankingLoadedAt = cached['storedAt']
            self.__setRankingStatus('缓存 ' + stamp + ' · 正在更新',
                '当前筛选条件的本地缓存；统计日期和数据版本见右上角。')
        else:
            self.setCurrentInterface(self.waitingInterface)
            self.rankStatus.setText('正在读取排行…' if to is self.tierInterface else '正在读取攻略…')

        try:
            # 尝试刷新当前的界面
            loaded_selection = await self.__updateInterface(to, revision)

            # 让转圈消失，显示界面
            if self.__requestCurrent(revision, loaded_selection or selection):
                self.setCurrentInterface(to)
                self.rankStatus.setText('已读取 · ' + datetime.now().strftime('%H:%M'))
                self.rankStatus.setToolTip('统计日期和数据版本见右上角；刷新可重新请求数据。')
                if to is self.tierInterface:
                    self.rankingLoadedAt = time.time()
                    self._rankingStatus = (self.rankStatus.text(), self.rankStatus.toolTip())
        except Exception as e:
            if not self.__requestCurrent(revision, selection):
                return
            if cached:
                if not await self.__applyTierPayload(cached['payload'], revision, selection, force=True):
                    return
                self.setCurrentInterface(self.tierInterface)
                stamp = datetime.fromtimestamp(cached['storedAt']).strftime('%m-%d %H:%M')
                self.rankingLoadedAt = cached['storedAt']
                self.__setRankingStatus('暂用缓存 ' + stamp + ' · 更新失败，可重试',
                    '仅保留当前筛选条件的缓存。' + str(e)[:160])
                return
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
            self.rankStatus.setText('读取失败 · 点击刷新重试')
        finally:
            if revision == self.loadRevision:
                self.setAutoRefreshEnabled(True)

    def __filterKey(self):
        return tuple(box.currentData() for box in (self.modeComboBox, self.regionComboBox,
                                                   self.tierComboBox, self.positionComboBox))

    def __rankingKey(self):
        mode = self.modeComboBox.currentData()
        analysis = self.tierInterface.analysis
        if mode == 'aram_mayhem':
            return self.rankingCache.make_key(mode, 'cn', 'all', 'none')
        return self.rankingCache.make_key(mode, self.regionComboBox.currentData(),
            self.tierComboBox.currentData(), self.positionComboBox.currentData(),
            fraction=analysis.fraction.currentData(), only_strong=analysis.strongOnly.isChecked(),
            strict=analysis.strict.isChecked() and self.regionComboBox.currentData() == 'consensus')

    async def __applyTierPayload(self, payload, revision, selection, *, force=False):
        if not self.__requestCurrent(revision, selection):
            return False
        key = self.__rankingKey()
        if not force and self.displayedRankingKey == key and self._displayedTierPayload == payload:
            self.__setVersionLabel(payload)
            return True
        data = copy.deepcopy(payload)
        icon_paths = {}
        def usable_icons(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == 'icon' and isinstance(item, str):
                        if item not in icon_paths:
                            icon_paths[item] = item if Path(item).is_file() else 'app/resource/images/champion-0.png'
                        value[key] = icon_paths[item]
                    else:
                        usable_icons(item)
            elif isinstance(value, list):
                for item in value:
                    usable_icons(item)
        usable_icons(data)
        mode = self.modeComboBox.currentData()
        native = data.get('source') != 'rift-analysis' or data.get('region') == 'cn'
        staged = {}
        if data.get('source') == 'rift-analysis' and native:
            ids = {row['championId'] for row in data['data']}
            rows = [row for row in data['legacyRows'] if row['championId'] in ids]
        elif native:
            rows = data['data'][self.positionComboBox.currentData()] if mode == 'ranked' else data['data']
        if native:
            staged = await self.tierInterface.tierList.prepareRows(rows,
                lambda: self.__requestCurrent(revision, selection))
            if staged is None:
                return False
        try:
            self.__configureMode(mode, self.regionComboBox.currentData())
            if data.get('source') == 'rift-analysis':
                self.tierInterface.showAnalysis(data, context=key, staged=staged)
            else:
                self.tierInterface.showAnalysis(None)
                self.tierInterface.tierList.updateList(rows, context=key, staged=staged)
            self.__setVersionLabel(data)
            self.displayedRankingKey = key
            self._displayedTierPayload = copy.deepcopy(payload)
            self.refreshDraftMaster()
            return True
        finally:
            self.tierInterface.tierList.discardRows(staged)

    @asyncSlot()
    async def __retryRanking(self):
        mode = self.modeComboBox.currentData()
        region = self.regionComboBox.currentData()
        tier = 'all' if mode == 'aram_mayhem' else self.tierComboBox.currentData()
        invalidate = getattr(opgg, 'invalidateRanking', None)
        if invalidate:
            invalidate(region, mode, tier)
        await self.updateAndSwitchTo(self.tierInterface, self.tierInterface)

    def __requestCurrent(self, revision, selection):
        return (revision is None or revision == self.loadRevision) and selection == self.__filterKey()

    async def __updateInterface(self, interface: QWidget, revision=None):
        map = {
            self.tierInterface: self.__updateTierInterface,
            self.buildInterface: self.__updateBuildInterface
        }

        return await map[interface](revision)

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
        self.personalButton.setVisible(True)
        desktop = QApplication.desktop().availableGeometry()
        width = 640
        self.setFixedSize(min(width, desktop.width()), min(826, desktop.height()))
        self.positionComboBox.setToolTip('选择分路查看英雄排行与攻略')
        self.__configureFilterVisibility()

    async def __updateTierInterface(self, revision=None):
        selection = self.__filterKey()
        cache_key = self.__rankingKey()
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

        if mode == 'ranked' and region in ('cn', 'consensus'):
            analysis = self.tierInterface.analysis
            data = await opgg.getRiftAnalysis(region, tier, position,
                fraction=analysis.fraction.currentData(), only_strong=analysis.strongOnly.isChecked(),
                strict=analysis.strict.isChecked() and region == 'consensus')
        else:
            data = await opgg.getTierList(region, mode, 'all' if mode == 'aram_mayhem' else tier)

        fetched_at = time.time()
        if not self.__requestCurrent(revision, selection):
            return
        if not await self.__applyTierPayload(data, revision, selection):
            return
        await asyncio.to_thread(self.rankingCache.put, cache_key, data, fetched_at=fetched_at)

    async def __updateBuildInterface(self, revision=None):
        selection = self.__filterKey()
        mode = self.modeComboBox.currentData()
        region = self.regionComboBox.currentData()
        tier = self.tierComboBox.currentData()
        position = self.positionComboBox.currentData()
        championId = self.buildInterface.getCurrentChampionId()
        self.__configureMode(mode, region)

        if mode == 'aram_mayhem':
            position = 'none'
            tier = 'all'

        logger.info(
            f"Get champion build, {mode}, {region}, {tier}, {position}, {championId}", TAG)

        data = await opgg.getChampionBuild(region, mode, championId, position, tier)

        # A slower response for the previous selection must not overwrite a newer hero.
        if (not self.__requestCurrent(revision, selection)
                or championId != self.buildInterface.getCurrentChampionId() or mode != self.modeComboBox.currentData()
                or region != self.regionComboBox.currentData()
                or (mode == 'ranked' and position != self.positionComboBox.currentData())):
            return
        if mode == 'aram_mayhem' and data['data'].get('championId') != championId:
            raise ValueError('强化推荐所属英雄与当前选择不一致。')

        self.buildInterface.updateInterface(data['data'])

        # 若英雄没有在特定位置下的数据，则根据得到的数据重新设置一下位置的 combo box
        if (pos := data['data'].get('summary', {}).get('position', 'none')) != position \
                and mode == 'ranked':

            # 在设置之前需要锁住 combo box changed 的槽函数，防止它自动刷新
            self.setAutoRefreshEnabled(False)
            self.__setComboBoxCurrentData(self.positionComboBox, pos)
            self.setAutoRefreshEnabled(True)

        self.__setVersionLabel(data)
        self._buildVersionData = {key: data[key] for key in ('version', 'source', 'region', 'dates', 'statDate') if key in data}
        self.displayedBuildKey = (mode, region, tier, championId,
                                 self.positionComboBox.currentData() if mode == 'ranked' else 'none')

        return self.__filterKey()

    @asyncSlot(bool)
    async def __onDebugButtonClicked(self, _):
        await opgg.start()
        await connector.autoStart()
        await ChampionAlias.checkAndUpdate()

        await self.initWindow()

    async def initWindow(self):
        await self.updateAndSwitchTo(self.tierInterface, self.tierInterface)

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
