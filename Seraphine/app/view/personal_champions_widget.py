"""Small, independently scrollable personal lane leaderboard."""
import asyncio
from datetime import datetime, timezone, timedelta
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QTableWidgetItem, QHeaderView, QAbstractItemView
from app.common.qfluentwidgets import BodyLabel, CaptionLabel, ComboBox, PushButton, TableWidget
from app.common.config import LOCAL_PATH
from app.lol.connector import connector, gameIconCachePath
from app.lol.personal_history import PersonalHistory, aggregate
from app.view.bp_assistant_widget import LANES


class PersonalChampionsWidget(QWidget):
    championChosen = pyqtSignal(int,str)
    laneChanged = pyqtSignal(str)

    def __init__(self, name_lookup, icon_loader, parent=None):
        super().__init__(parent)
        self.name_lookup, self.icon_loader = name_lookup, icon_loader
        self.service = PersonalHistory(connector, LOCAL_PATH+'/cache/personal-history')
        self.puuid, self.data, self.serial, self.task = '', {'records':[]}, 0, None
        self.icons, self.iconTasks = {}, {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4,4,4,4)
        header = QHBoxLayout()
        header.addWidget(BodyLabel('我的分路英雄榜'))
        self.laneCombo = ComboBox()
        for lane,title in LANES[:-1]:
            self.laneCombo.addItem(title,userData=lane)
        header.addWidget(self.laneCombo)
        header.addStretch()
        self.refreshButton = PushButton('读取 / 刷新战绩')
        header.addWidget(self.refreshButton)
        layout.addLayout(header)
        self.scope = CaptionLabel('单／双排 · 每英雄至少 5 场 · 胜率相同优先场次多')
        self.scope.setWordWrap(True)
        layout.addWidget(self.scope)
        self.table = TableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(['英雄','场次','胜 / 负','胜率'])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0,QHeaderView.Stretch)
        for col,width in ((1,65),(2,85),(3,95)):
            self.table.setColumnWidth(col,width)
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.setMinimumHeight(140)
        self.table.setToolTip('双击英雄查看当前分路攻略；不会自动选人。')
        layout.addWidget(self.table,1)
        self.status = CaptionLabel('连接客户端后，点击“读取 / 刷新战绩”。')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.refreshButton.clicked.connect(self.requestRefresh)
        self.laneCombo.currentIndexChanged.connect(self.render)
        self.laneCombo.currentIndexChanged.connect(lambda _: self.laneChanged.emit(self.laneCombo.currentData()))
        self.table.cellDoubleClicked.connect(self.chooseChampion)

    def setLane(self,lane):
        index = self.laneCombo.findData(lane)
        if index>=0:
            self.laneCombo.setCurrentIndex(index)

    def setAccount(self,profile):
        puuid = profile.get('puuid') if isinstance(profile,dict) else ''
        if puuid and puuid != self.puuid:
            self.puuid = puuid
            self.data = self.service.cached(puuid)
            self.render()

    def disconnect(self):
        self.serial += 1
        if self.task:
            self.task.cancel()
        self.puuid, self.data = '', {'records':[]}
        self.refreshButton.setEnabled(True)
        self.render()
        self.status.setText('客户端已断开；重新连接后读取对应账号战绩。')

    def requestRefresh(self,*args):
        self.serial += 1
        serial = self.serial
        if self.task:
            self.task.cancel()
        self.task = asyncio.create_task(self.refresh(serial))

    async def refresh(self,serial):
        self.refreshButton.setEnabled(False)
        self.status.setText('正在读取近期最多 200 场，按账号合并本地缓存…')
        try:
            profile = await asyncio.wait_for(connector.getCurrentSummoner(),6)
            if serial != self.serial:
                return
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

    def render(self,*args):
        rows = aggregate(self.data['records'],self.laneCombo.currentData())
        self.table.setRowCount(len(rows))
        for index,row in enumerate(rows):
            hero = row['championId']
            item = QTableWidgetItem(self.name_lookup(hero))
            item.setData(Qt.UserRole,hero)
            item.setIcon(QIcon(self.icons.get(hero) or gameIconCachePath('champion',hero)))
            self.table.setItem(index,0,item)
            for col,text in ((1,str(row['games'])),(2,f"{row['wins']} / {row['losses']}"),(3,f"{row['winRate']*100:.2f}%")):
                cell=QTableWidgetItem(text)
                cell.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(index,col,cell)
            if hero not in self.icons and hero not in self.iconTasks:
                try:
                    self.iconTasks[hero]=asyncio.get_running_loop().create_task(self.loadIcon(hero))
                except RuntimeError:
                    pass
        records = self.data['records']
        eligible=[r for r in records if r.get('queue')==420 and not r.get('remake') and r.get('win') is not None]
        unknown=sum(r.get('lane')=='UNKNOWN' for r in eligible)
        lane_count=sum(r.get('lane')==self.laneCombo.currentData() for r in eligible)
        dates=[r['date'] for r in records if r.get('date')]
        def date(value):
            return datetime.fromtimestamp(value/1000,timezone(timedelta(hours=8))).strftime('%Y-%m-%d')
        span=f"{date(min(dates))} 至 {date(max(dates))}" if dates else '暂无可用日期'
        self.scope.setText(f'本人单／双排 · 每英雄 ≥5 场 · 本地累计 {len(records)} 场，{span}')
        message=f'本分路有效 {lane_count} 场，{len(rows)} 名英雄达标；分路未知 {unknown} 场已排除。'
        if not rows:
            message+=' 暂无至少 5 场的英雄。'
        if self.data.get('updated'):
            updated=datetime.fromtimestamp(self.data['updated'],timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')
            message+=f" 最近读取 {self.data.get('read',0)} 场；更新 {updated}。"
        message+=' '.join(self.data.get('warnings',[]))
        self.status.setText(message)
        self.status.setToolTip('每次读取近期最多 200 场，本地最多保留 2000 场；仅代表已读取范围，并非生涯全部战绩。重开或少于 5 分钟的场次不计入。本人历史不按上方数据源或段位筛选；胜率相同优先场次多。')

    async def loadIcon(self,hero):
        try:
            self.icons[hero]=await self.icon_loader(hero)
            for row in range(self.table.rowCount()):
                item=self.table.item(row,0)
                if item and item.data(Qt.UserRole)==hero:
                    item.setIcon(QIcon(self.icons[hero]))
        except Exception:
            pass
        finally:
            self.iconTasks.pop(hero,None)

    def chooseChampion(self,row,col):
        item=self.table.item(row,0)
        if item:
            self.championChosen.emit(item.data(Qt.UserRole),self.laneCombo.currentData())
