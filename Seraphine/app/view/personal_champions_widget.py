"""Recent games and hero usage, without sample gates or personal win-rate ranks."""
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
import time

from PyQt5.QtCore import Qt, pyqtSignal, QEvent
from PyQt5.QtGui import QIcon, QColor
from PyQt5.QtWidgets import (QWidget, QFrame, QLabel, QVBoxLayout, QHBoxLayout,
    QTableWidgetItem, QHeaderView, QAbstractItemView)
from app.common.qfluentwidgets import (BodyLabel, CaptionLabel, SubtitleLabel,
    ComboBox, PushButton, TableWidget, setCustomStyleSheet, isDarkTheme, qconfig)
from app.common.config import LOCAL_PATH
from app.lol.connector import connector, gameIconCachePath
from app.lol.personal_history import PersonalHistory, aggregate, select_records
from app.view.bp_assistant_widget import LANES
from qfluentwidgets.common.style_sheet import CustomStyleSheet

QUEUE_SCOPES = {'ranked': (420, 440), 'solo': (420,), 'flex': (440,), 'mayhem': (2400,)}
QUEUE_NAMES = {420: '单／双排', 440: '灵活组排', 2400: '海斗'}
BEIJING = timezone(timedelta(hours=8))


def date_text(timestamp, pattern='%m-%d %H:%M'):
    return datetime.fromtimestamp(timestamp / 1000, BEIJING).strftime(pattern) if timestamp else '—'


class StatCard(QFrame):
    def __init__(self, caption, parent=None):
        super().__init__(parent)
        self.setObjectName('historyStatCard')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(2)
        layout.addWidget(CaptionLabel(caption))
        self.value = QLabel('—')
        self.value.setObjectName('historyStatValue')
        layout.addWidget(self.value)
        self.setFixedHeight(67)


class PersonalChampionsWidget(QWidget):
    championChosen = pyqtSignal(int, str)
    laneChanged = pyqtSignal(str)
    historyChanged = pyqtSignal(list)

    def __init__(self, name_lookup, icon_loader, parent=None):
        super().__init__(parent)
        self.name_lookup, self.icon_loader = name_lookup, icon_loader
        self.service = PersonalHistory(connector, LOCAL_PATH + '/cache/personal-history')
        self.puuid, self.data, self.serial, self.task = '', {'records': []}, 0, None
        self.icons, self.iconTasks = {}, {}
        self._iconSemaphore = asyncio.Semaphore(4)
        self.accountRevision = 0
        self._publishedAccount, self._publishedRecords = None, None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 8, 4, 4)
        layout.setSpacing(10)
        header = QHBoxLayout()
        header.addWidget(SubtitleLabel('战绩统计'))
        header.addStretch()
        self.refreshButton = PushButton('刷新战绩')
        header.addWidget(self.refreshButton)
        layout.addLayout(header)

        filters = QHBoxLayout()
        self.viewCombo = ComboBox()
        self.viewCombo.addItem('最近20场排位', userData='recent')
        self.viewCombo.addItem('英雄使用统计', userData='champions')
        self.viewCombo.setMinimumWidth(145)
        filters.addWidget(self.viewCombo)
        self.queueCombo = ComboBox()
        for title, value in (('全部排位', 'ranked'), ('单／双排', 'solo'), ('灵活组排', 'flex'), ('海克斯大乱斗', 'mayhem')):
            self.queueCombo.addItem(title, userData=value)
        filters.addWidget(self.queueCombo)
        self.laneCombo = ComboBox()
        self.laneCombo.addItem('全部分路', userData='ALL')
        for lane, title in LANES[:-1]:
            self.laneCombo.addItem(title, userData=lane)
        filters.addWidget(self.laneCombo)
        filters.addStretch()
        layout.addLayout(filters)

        cards = QHBoxLayout()
        cards.setSpacing(8)
        self.gamesCard = StatCard('当前范围 · 对局')
        self.resultsCard = StatCard('胜利 / 失败')
        self.heroesCard = StatCard('使用过的英雄')
        for card in (self.gamesCard, self.resultsCard, self.heroesCard):
            cards.addWidget(card, 1)
        layout.addLayout(cards)
        self.scope = CaptionLabel()
        self.scope.setWordWrap(True)
        layout.addWidget(self.scope)

        self.table = TableWidget()
        compact = 'QTableView::item { padding-left: 6px; padding-right: 6px; }'
        setCustomStyleSheet(self.table, compact, compact)
        self.table.setColumnCount(5)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.setMinimumHeight(140)
        self.emptyLabel = BodyLabel('连接客户端后，点击右上角“刷新战绩”。', self.table.viewport())
        self.emptyLabel.setAlignment(Qt.AlignCenter)
        self.emptyLabel.setWordWrap(True)
        self.emptyLabel.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.table.viewport().installEventFilter(self)
        layout.addWidget(self.table, 1)
        self.status = CaptionLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        light = ('#historyStatCard { background: #edf4fa; border: 1px solid #d6e3ef; border-radius: 8px; }'
                 '#historyStatValue { color: #174d78; font: 22px "Microsoft YaHei"; }')
        dark = ('#historyStatCard { background: #27323d; border: 1px solid #3b4a59; border-radius: 8px; }'
                '#historyStatValue { color: #c6e5ff; font: 22px "Microsoft YaHei"; }')
        setCustomStyleSheet(self, light, dark)
        CustomStyleSheet(self).apply(self)
        self.refreshButton.clicked.connect(self.requestRefresh)
        self.laneCombo.currentIndexChanged.connect(self.render)
        self.laneCombo.currentIndexChanged.connect(lambda _: self.laneChanged.emit(self.laneCombo.currentData()))
        self.table.cellDoubleClicked.connect(self.chooseChampion)
        self.viewCombo.currentIndexChanged.connect(self.render)
        self.queueCombo.currentIndexChanged.connect(self.render)
        qconfig.themeChangedFinished.connect(self.updateResultColors)
        self.render()

    def eventFilter(self, obj, event):
        if obj is self.table.viewport() and event.type() == QEvent.Resize:
            self.emptyLabel.setGeometry(self.table.viewport().rect().adjusted(20, 20, -20, -20))
        return super().eventFilter(obj, event)

    def setLane(self, lane):
        index = self.laneCombo.findData(lane)
        if index >= 0:
            self.laneCombo.setCurrentIndex(index)

    def setMode(self, mode):
        mayhem = mode == 'aram_mayhem'
        if mayhem != (self.queueCombo.currentData() == 'mayhem'):
            self.queueCombo.setCurrentIndex(self.queueCombo.findData('mayhem' if mayhem else 'ranked'))

    def setAccount(self, profile):
        puuid = profile.get('puuid') if isinstance(profile, dict) else ''
        if puuid and puuid != self.puuid:
            self.accountRevision += 1
            self.puuid = puuid
            self.data = self.service.cached(puuid)
            self.render()

    def needsRefresh(self):
        return not self.puuid or time.time() - self.data.get('updated', 0) > 300

    def stop(self):
        self.serial += 1
        if self.task:
            self.task.cancel()
        for task in self.iconTasks.values():
            task.cancel()

    def disconnect(self):
        self.stop()
        self.puuid, self.data = '', {'records': []}
        self.refreshButton.setEnabled(True)
        self.render()
        self.status.setText('客户端已断开；重新连接后读取对应账号战绩。')

    def requestRefresh(self, *args):
        self.serial += 1
        if self.task:
            self.task.cancel()
        self.task = asyncio.create_task(self.refresh(self.serial))

    async def refresh(self, serial):
        self.refreshButton.setEnabled(False)
        self.status.setText('正在读取近期对局，整理战绩统计…')
        account_revision = self.accountRevision
        try:
            profile = await asyncio.wait_for(connector.getCurrentSummoner(), 6)
            if serial != self.serial or account_revision != self.accountRevision:
                return
            if not isinstance(profile, dict) or not profile.get('puuid'):
                raise ValueError('未获取到当前账号')
            self.setAccount(profile)
            puuid = self.puuid
            if not puuid:
                raise ValueError('未获取到当前账号')
            data = await self.service.refresh(puuid)
            if serial == self.serial and puuid == self.puuid:
                self.data = data
                self.render()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if serial == self.serial:
                self.status.setText('读取失败，已保留当前账号缓存。请确认客户端连接后重试。')
                self.status.setToolTip(str(error))
        finally:
            if serial == self.serial:
                self.refreshButton.setEnabled(True)

    def render(self, *args):
        records = self.data['records']
        # Selector changes affect only this page, not global recent-play markers.
        if self._publishedAccount != self.puuid or records is not self._publishedRecords:
            self._publishedAccount, self._publishedRecords = self.puuid, records
            self.historyChanged.emit(records)
        recent = self.viewCombo.currentData() == 'recent'
        mayhem = self.queueCombo.currentData() == 'mayhem'
        self.viewCombo.setItemText(0, '最近20场海斗' if mayhem else '最近20场排位')
        self.laneCombo.setVisible(not recent and not mayhem)
        lane = None if recent or mayhem else self.laneCombo.currentData()
        queues = QUEUE_SCOPES[self.queueCombo.currentData()]
        selected = select_records(records, queues, lane, 20 if recent else None)
        self.table.setUpdatesEnabled(False)
        try:
            self.table.clearContents()
            if recent:
                self.renderRecent(selected)
            else:
                self.renderChampions(aggregate(selected, queues=queues))
        finally:
            self.table.setUpdatesEnabled(True)
        wins = sum(not row.get('remake') and row.get('win') is True for row in selected)
        losses = sum(not row.get('remake') and row.get('win') is False for row in selected)
        remakes = sum(bool(row.get('remake')) for row in selected)
        unknown = len(selected) - wins - losses - remakes
        self.gamesCard.value.setText(str(len(selected)))
        self.resultsCard.value.setText(f'{wins} / {losses}')
        self.heroesCard.value.setText(str(len({row['champion'] for row in selected if row.get('champion')})))
        scope = f'最近 {len(selected)} 场' + ('海斗' if mayhem else '排位') if recent else f'已读取范围 · {len(selected)} 场'
        scope += f' · {wins} 胜 {losses} 负'
        if remakes:
            scope += f' · {remakes} 重开'
        if unknown:
            scope += f' · {unknown} 场结果待确认'
        if recent:
            scope += ' · 全部分路'
        self.scope.setText(scope)
        self.emptyLabel.setVisible(not selected)
        self.emptyLabel.setText('该范围暂无战绩\n切换队列或分路，或点击“刷新战绩”。' if records else
                                '暂无战绩\n连接客户端后，点击右上角“刷新战绩”。')
        message = ('不足20场时显示当前已读取的场次。' if recent and len(selected) < 20 else
                   '按时间从新到旧。' if recent else '按使用场次排序；玩过1场也会显示。')
        if self.data.get('updated'):
            message += ' 更新 ' + date_text(self.data['updated'] * 1000) + '。'
        if self.data.get('supplemented'):
            message += ' 已补读更早战绩。'
        message += ' '.join(self.data.get('warnings', []))
        self.status.setText(message)
        dates = [r['date'] for r in records if r.get('date')]
        span = (date_text(min(dates), '%Y-%m-%d') + ' 至 ' + date_text(max(dates), '%Y-%m-%d')) if dates else '暂无日期'
        self.status.setToolTip(f'当前账号已读取 {len(records)} 场，{span}。\n'
            '每次最多读取近期200场，本地最多保留2000场；仅代表已读取范围。\n'
            '重开单独计数，不计入胜负；未知结果不算作失败。分路未知的对局仅在全部分路中显示。\n'
            '战绩不受排行数据源或段位影响；不以个人胜率评价英雄熟练度。')

    def _heroItem(self, hero, lane=None):
        item = QTableWidgetItem(self.name_lookup(hero) if hero else '未知英雄')
        item.setData(Qt.UserRole, hero)
        item.setData(Qt.UserRole + 1, lane)
        item.setIcon(QIcon(self.icons.get(hero) or gameIconCachePath('champion', hero)))
        self.requestIcon(hero)
        return item

    def _cell(self, row, column, text, color=None, tooltip=''):
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignCenter)
        if color:
            item.setForeground(QColor(color))
        item.setToolTip(tooltip)
        self.table.setItem(row, column, item)

    def renderChampions(self, rows):
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(['英雄', '场次', '胜 / 负', '最近使用'])
        for col, width in ((1, 65), (2, 95), (3, 112)):
            self.table.setColumnWidth(col, width)
        self.table.setToolTip('每个英雄都保留；双击查看当前分路攻略。海斗打开海斗攻略。')
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            self.table.setItem(index, 0, self._heroItem(row['championId']))
            details = f"{row['wins']} 胜 / {row['losses']} 负 / {row['remakes']} 重开 / {row['unknown']} 待确认"
            self._cell(index, 1, str(row['games']), tooltip=details)
            self._cell(index, 2, f"{row['wins']} / {row['losses']}", tooltip=details)
            self._cell(index, 3, date_text(row['lastPlayed']))

    def renderRecent(self, rows):
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(['英雄', '结果', '分路', '队列', '时间'])
        for col, width in ((1, 64), (2, 60), (3, 80), (4, 108)):
            self.table.setColumnWidth(col, width)
        self.table.setToolTip('按时间从新到旧；双击查看该场英雄攻略。')
        self.table.setRowCount(len(rows))
        lanes = dict(LANES)
        for index, row in enumerate(rows):
            item = self._heroItem(row.get('champion', 0), row.get('lane'))
            item.setData(Qt.UserRole + 2, row.get('queue'))
            self.table.setItem(index, 0, item)
            outcome = ('重开' if row.get('remake') else '胜利' if row.get('win') is True
                       else '失败' if row.get('win') is False else '待确认')
            color = self.resultColor(outcome)
            self._cell(index, 1, outcome, color)
            self._cell(index, 2, lanes.get(row.get('lane'), '未知'))
            self._cell(index, 3, QUEUE_NAMES[row['queue']])
            self._cell(index, 4, date_text(row.get('date')))

    @staticmethod
    def resultColor(outcome):
        return (('#6ccec0' if isDarkTheme() else '#13796d') if outcome == '胜利' else
                ('#f08c96' if isDarkTheme() else '#bd3546') if outcome == '失败' else None)

    def updateResultColors(self):
        if self.viewCombo.currentData() == 'recent':
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 1)
                color = self.resultColor(item.text()) if item else None
                if color:
                    item.setForeground(QColor(color))

    def requestIcon(self, hero):
        if not hero or hero in self.icons or hero in self.iconTasks:
            return
        cached = gameIconCachePath('champion', hero)
        if Path(cached).is_file():
            self.icons[hero] = cached
            return
        try:
            self.iconTasks[hero] = asyncio.get_running_loop().create_task(self.loadIcon(hero))
        except RuntimeError:
            pass

    async def loadIcon(self, hero):
        try:
            async with self._iconSemaphore:
                path = await self.icon_loader(hero)
            if not path:
                return
            self.icons[hero] = path
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 0)
                if item and item.data(Qt.UserRole) == hero:
                    item.setIcon(QIcon(path))
        except Exception:
            pass
        finally:
            self.iconTasks.pop(hero, None)

    def chooseChampion(self, row, col):
        item = self.table.item(row, 0)
        if item and item.data(Qt.UserRole):
            lane = item.data(Qt.UserRole + 1)
            fallback = self.laneCombo.currentData()
            self.championChosen.emit(item.data(Qt.UserRole), lane if lane in dict(LANES[:-1]) else
                                     fallback if fallback in dict(LANES[:-1]) else '')
