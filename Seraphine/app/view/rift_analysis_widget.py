"""Rift comparison table with separate country and matchup statistics."""
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from PyQt5.QtCore import Qt, QSize, pyqtSignal, QUrl, QRect, QRectF
from PyQt5.QtGui import QIcon, QPixmap, QDesktopServices, QColor, QFont, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QTableWidgetItem, QHeaderView, QAbstractItemView, QStyle)
from PyQt5.QtSvg import QSvgRenderer
from qasync import asyncSlot

from app.common.qfluentwidgets import (TableWidget, BodyLabel, CaptionLabel,
    SubtitleLabel, SearchLineEdit, ComboBox, CheckBox, PushButton,
    isDarkTheme, qconfig, setCustomStyleSheet)
from app.lol.rift_analysis import SOURCE_NAMES
from app.lol.connector import connector
from app.lol.tools import createAndSetRunePage
from app.common.style_sheet import colorManager
from qfluentwidgets.components.widgets.table_view import TableItemDelegate

LANE_NAMES = {'TOP':'上路', 'JUNGLE':'打野', 'MID':'中路', 'ADC':'下路', 'SUPPORT':'辅助'}


def _font(size, bold=False):
    font = QFont('Microsoft YaHei')
    font.setPixelSize(size)
    font.setBold(bold)
    return font


@lru_cache(maxsize=6)
def tier_pixmap(tier):
    pixmap = QPixmap(48, 48)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer('app/resource/images/icon-tier-'+str(tier)+'.svg').render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return pixmap


def _palette():
    if isDarkTheme():
        return dict(surface='#292929', text='#f3f3f3', muted='#c0c0c0', border='#454545',
                    row='#2d343c', alternate='#293039', hover='#354554', selected='#234f70', header='#303030')
    return dict(surface='#faf9f7', text='#202020', muted='#505a66', border='#d8dce1',
                row='#eaf4fc', alternate='#f5f9fc', hover='#dceefd', selected='#c9e5fb', header='#f4f3f1')


def _date_text(value):
    try:
        if len(value) == 8 and value.isdigit():
            return datetime.strptime(value, '%Y%m%d').strftime('%m-%d')
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if date.tzinfo:
            date = date.astimezone(timezone(timedelta(hours=8)))
        return date.strftime('%m-%d %H:%M')
    except (ValueError, TypeError):
        return value


class _RiftDelegate(TableItemDelegate):
    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        rect = option.rect
        colors = _palette()
        tier = index.data(Qt.UserRole+1)
        normal, hover, pressed, border = colorManager.getColor('tier'+str(tier if tier is not None else 4))
        painter.setClipRect(rect)
        painter.setPen(QPen(border, 1))
        painter.setBrush(pressed if index.row() in self.selectedRows else hover if index.row()==self.hoverRow else normal)
        painter.drawRoundedRect(QRectF(4, rect.y()+2, self.parent().viewport().width()-13, rect.height()-4), 6, 6)
        painter.setPen(QColor(colors['text']))
        painter.setFont(_font(14))
        if index.column()==0:
            painter.drawText(QRect(rect.x()+12, rect.y(), 30, rect.height()), Qt.AlignCenter, str(index.row()+1))
            icon=index.data(Qt.DecorationRole)
            if icon:
                painter.save()
                icon_rect = QRect(rect.x()+48, rect.center().y()-14, 28, 28)
                path = QPainterPath()
                path.addEllipse(QRectF(icon_rect))
                painter.setClipPath(path)
                icon.paint(painter, icon_rect)
                painter.restore()
            name_rect=rect.adjusted(84,0,-8,0)
            text=painter.fontMetrics().elidedText(index.data(Qt.DisplayRole) or '',Qt.ElideRight,name_rect.width())
            painter.drawText(name_rect,Qt.AlignVCenter|Qt.AlignLeft,text)
        elif index.column() in (1, 2, 3) and index.data(Qt.UserRole):
            stat = index.data(Qt.UserRole)
            top = rect.center().y()-20
            painter.setPen(QColor(colors['muted']))
            painter.setFont(_font(14))
            painter.drawText(QRect(rect.x()+2, top, rect.width()-34, 22), Qt.AlignCenter, '#'+str(stat['rank']))
            painter.drawPixmap(QRect(rect.right()-32, top, 24, 24), tier_pixmap(stat['tier']))
            painter.setPen(QColor(colors['text']))
            painter.setFont(_font(14))
            matchup = index.data(Qt.UserRole+2)
            value = (f"{matchup['winRate']*100:.2f}%" if matchup.get('winRate') is not None else '—') if matchup else f"{stat['winRate']*100:.2f}%"
            if matchup and matchup.get('supported'):
                value += ' ✓'
            painter.drawText(QRect(rect.x()+2, top+22, rect.width()-4, 22), Qt.AlignCenter, value)
        else:
            painter.setFont(_font(14, index.column()==4))
            painter.drawText(rect,Qt.AlignCenter,index.data(Qt.DisplayRole) or '')
        if option.state & QStyle.State_HasFocus:
            painter.setPen(QPen(QColor('#0078d4'), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect.adjusted(2,2,-2,-2), 4, 4)
        painter.restore()


class RiftAnalysisWidget(QWidget):
    ruleChanged = pyqtSignal()
    championChosen = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground)
        self.setObjectName('riftAnalysis')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(3)
        self.title = SubtitleLabel('三服共同强势', self)
        self.title.hide()
        self.note = CaptionLabel(self)
        self.note.setWordWrap(True)
        self.note.hide()
        self.details = CaptionLabel()
        self.details.setWordWrap(True)
        self.details.hide()
        self.detailsButton = PushButton('规则')
        self.detailsButton.clicked.connect(lambda: self.details.setVisible(self.details.isHidden()))
        self.rules = QWidget()
        rules = QHBoxLayout(self.rules)
        rules.setContentsMargins(0, 0, 0, 0)
        self.fraction = ComboBox()
        for text, value in [('各服前 30%', .30), ('各服前 20%', .20), ('各服前 50%', .50)]:
            self.fraction.addItem(text, userData=value)
        self.strongOnly = CheckBox('共同强势')
        self.strongOnly.setChecked(True)
        self.strict = CheckBox('三服都克制')
        for checkbox in (self.strongOnly, self.strict):
            # Preserve Fluent indicator sizing and text geometry.
            setCustomStyleSheet(checkbox, "CheckBox { font: 14px 'Microsoft YaHei'; color: #202020; }",
                                "CheckBox { font: 14px 'Microsoft YaHei'; color: #f3f3f3; }")
        rules.addWidget(self.fraction)
        rules.addWidget(self.strongOnly)
        rules.addWidget(self.strict)
        rules.addStretch()
        self.search = SearchLineEdit(self)
        self.search.setPlaceholderText('搜索推荐英雄')
        self.search.hide()  # Use the same search flyout in the window toolbar.
        self.count = CaptionLabel()
        rules.addWidget(self.count)
        rules.addWidget(self.detailsButton)
        self.table = TableWidget()
        self.table.setItemDelegate(_RiftDelegate(self.table))
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(['#     英雄', '国服', '全球', '韩服', '我方对位胜率'])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setWordWrap(True)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(50)
        self.table.setIconSize(QSize(36, 36))
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for column, width in ((1, 104), (2, 104), (3, 104), (4, 150)):
            self.table.setColumnWidth(column, width)
        self.table.horizontalHeader().setFixedHeight(44)
        for widget in (self.rules, self.details):
            layout.addWidget(widget)
        layout.addWidget(self.table, 1)
        self.fraction.currentIndexChanged.connect(lambda _: self.ruleChanged.emit())
        self.strongOnly.stateChanged.connect(lambda _: self.ruleChanged.emit())
        self.strict.stateChanged.connect(lambda _: self.ruleChanged.emit())
        self.search.textChanged.connect(self.filterRows)
        self.table.cellDoubleClicked.connect(self.openHero)
        self.data = None
        qconfig.themeChangedFinished.connect(self.applyTheme)
        self.applyTheme()

    def applyTheme(self):
        colors = _palette()
        self.setStyleSheet('#riftAnalysis { background: '+colors['surface']+'; border: 1px solid '+colors['border']+'; border-radius: 6px; }')
        self.title.setStyleSheet('color: '+colors['text']+'; font: 600 20px "Microsoft YaHei";')
        for label in (self.note, self.count, self.details):
            label.setStyleSheet('color: '+colors['muted']+'; font: 14px "Microsoft YaHei";')
        table_style = ('QTableWidget { background: '+colors['surface']+'; color: '+colors['text']+'; border: none; } '
                       'QHeaderView::section { background: '+colors['header']+'; color: '+colors['text']+'; '
                       'font: 600 14px "Microsoft YaHei"; border: none; padding: 8px; }')
        setCustomStyleSheet(self.table, table_style, table_style)
        self.table.viewport().update()

    def openHero(self, row, column):
        self.championChosen.emit(self.table.item(row, 0).data(Qt.UserRole))

    def updateData(self, data):
        self.data = data
        common = data['region'] == 'consensus'
        enemy = data['enemyId']
        self.fraction.setVisible(common)
        self.strongOnly.setVisible(common)
        self.strict.setVisible(common and bool(enemy))
        self.title.setText(('三服共同强势' if common and data['onlyStrong'] else '三服数据对比' if common else SOURCE_NAMES[data['region']]+'英雄榜')
                           + ' · ' + LANE_NAMES[data['position']]
                           + (' · 克制'+data['enemyName'] if enemy else ''))
        lines = ['同版本 '+data['version']+'；各服独立展示排名、胜率和梯队。双击英雄查看攻略。']
        if common:
            prefix = '共同强势：' if data['onlyStrong'] else '共同强势筛选已关闭；规则：'
            lines.append(prefix+f"每服分路前 {data['fraction']*100:.0f}%、胜率 ≥ 50%；海外样本 ≥ 1000 场。按最弱一服排名排序。")
        if enemy:
            lines.append('对位胜率均为我方视角；海外对位样本 ≥ 200 场。— 表示未知，不算已确认克制。')
            lines.append('国服仅公开部分对位、未公开场次；默认允许有来源支持，勾选“仅三服都克制”要求三服均有优势数据。' if common else '国服仅公开部分对位、未公开场次。' if data['region']=='cn' else '')
        lines.append('数据时间（北京时间） '+ ' / '.join(SOURCE_NAMES[s]+': '+_date_text(date) for s,date in data['dates'].items()))
        for source, error in data['unavailable'].items():
            lines.append(SOURCE_NAMES[source]+'对位不可用：'+error[:100])
        lines.extend(data['notes'])
        if not data['data']:
            lines.append('没有符合当前条件的英雄；可取消共同强势或严格克制筛选。')
        self.note.setText('各服分别展示：排名 / 梯队 / 胜率。双击英雄查看攻略。'
                          + (' 对位胜率为我方视角。' if enemy else ''))
        self.details.setText('\n'.join(line for line in lines if line))
        self.detailsButton.setToolTip('\n'.join(line for line in lines if line))
        self.table.verticalHeader().setDefaultSectionSize(50)
        self.table.setRowCount(len(data['data']))
        for column, source in ((1, 'cn'), (2, 'global'), (3, 'kr')):
            self.table.setColumnHidden(column, source not in data['sources'])
        self.table.setColumnHidden(4, True)
        self.table.setHorizontalHeaderLabels(['#     英雄'] + [SOURCE_NAMES[s]+(' · 对位' if enemy else '') for s in ('cn','global','kr')] + ['我方对位胜率'])
        for index, row in enumerate(data['data']):
            row_tier = max((stat['tier'] for stat in row['evidence'].values()), default=4)
            name = QTableWidgetItem(row['name'])
            name.setIcon(QIcon(row['icon']))
            name.setData(Qt.UserRole, row['championId'])
            name.setData(Qt.UserRole+1, row_tier)
            name.setToolTip('三服均符合强势条件' if row['commonStrong'] and common else '双击查看攻略')
            self.table.setItem(index, 0, name)
            for column, source in ((1, 'cn'), (2, 'global'), (3, 'kr')):
                evidence = row['evidence'].get(source)
                text = f"#{evidence['rank']} · {evidence['tierText']}\n胜率 {evidence['winRate']*100:.2f}%" if evidence else '—'
                item = QTableWidgetItem(text)
                item.setData(Qt.UserRole+1, row_tier)
                item.setTextAlignment(Qt.AlignCenter)
                if evidence:
                    item.setData(Qt.UserRole, evidence)
                    games = str(evidence['play'])+' 场' if evidence['play'] is not None else '腾讯未公开场次'
                    item.setToolTip(f"{SOURCE_NAMES[source]} · 第 {evidence['rank']} 名 · {evidence['tierText']}\n"
                                    f"胜率 {evidence['winRate']*100:.2f}% · 登场 {evidence['pickRate']*100:.2f}% · {games}")
                    if enemy:
                        stat = row['matchups'].get(source)
                        item.setData(Qt.UserRole+2, {'winRate': stat['winRate'] if stat else None,
                                                     'supported': source in row['supported']})
                        item.setToolTip(item.toolTip()+'\n我方对位胜率：'+
                                        (f"{stat['winRate']*100:.2f}%" if stat else '未知')+
                                        (' · 样本 '+str(stat['play'])+' 场' if stat and stat['play'] is not None else ' · 未公开场次'))
                self.table.setItem(index, column, item)
            matchup = []
            for source in data['sources']:
                stat = row['matchups'].get(source)
                rate = f"{stat['winRate']*100:.2f}%" if stat else '—'
                if stat and stat['play'] is not None and stat['play'] < 200:
                    rate += '（样本少）'
                elif source in row['supported']:
                    rate += ' ✓'
                matchup.append(SOURCE_NAMES[source]+' '+rate)
            counter = QTableWidgetItem('\n'.join(matchup))
            counter.setTextAlignment(Qt.AlignCenter)
            counter.setToolTip('\n'.join(SOURCE_NAMES[s]+': '+
                (str(stat['play'])+' 场' if stat['play'] is not None else '未公开场次')
                for s, stat in row['matchups'].items()))
            self.table.setItem(index, 4, counter)
        self.filterRows()

    def filterRows(self, *args):
        text = self.search.text().strip().casefold()
        visible = 0
        for row in range(self.table.rowCount()):
            matches = text in self.table.item(row, 0).text().casefold()
            self.table.setRowHidden(row, not matches)
            visible += matches
        self.count.setText(f'{visible} / {self.table.rowCount()} 个英雄')


class NationalRiftBuildWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout = QVBoxLayout(self)
        self.setAttribute(Qt.WA_StyledBackground)
        self.setObjectName('nationalRift')
        self.data = None
        qconfig.themeChangedFinished.connect(self.applyTheme)
        self.applyTheme()

    def applyTheme(self):
        colors = _palette()
        self.setStyleSheet('#nationalRift { background: '+colors['surface']+'; border: 1px solid '+colors['border']+'; border-radius: 6px; }')
        for label in self.findChildren(QLabel):
            role = label.property('riftTextRole')
            if role:
                label.setStyleSheet('color: '+colors['text']+'; font: '+
                                    ('600 20px' if role=='title' else '14px')+' "Microsoft YaHei";')

    def label(self, text, large=False):
        label = BodyLabel(text)
        label.setWordWrap(True)
        label.setProperty('riftTextRole', 'title' if large else 'body')
        self.layout.addWidget(label)
        self.applyTheme()
        return label

    def iconRow(self, ids, icons, stats):
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(4, 4, 4, 4)
        for id in ids:
            icon = QLabel()
            icon.setPixmap(QPixmap(icons[id]).scaled(36, 36, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            icon.setToolTip(str(id))
            layout.addWidget(icon)
        layout.addStretch()
        text = CaptionLabel(f"胜率 {stats['winRate']:.2f}% · 登场 {stats['pickRate']:.2f}%")
        text.setProperty('riftTextRole', 'body')
        layout.addWidget(text)
        self.layout.addWidget(widget)
        self.applyTheme()

    def updateData(self, data):
        self.data = data
        while self.layout.count():
            widget = self.layout.takeAt(0).widget()
            if widget:
                widget.hide()
                widget.deleteLater()
        summary = data['summary']
        self.label(data['name']+' · '+LANE_NAMES[summary['position']]+' · 国服攻略', True)
        self.label(f"腾讯 101 · 统计日期 {data['statDate']} · {summary['tierText']}\n"
                   f"胜率 {summary['winRate']*100:.2f}% · 登场 {summary['pickRate']*100:.2f}%")
        for section in data['sections']:
            if section['rows']:
                self.label(section['title'], True)
                for row in section['rows']:
                    self.iconRow(row['ids'], data['itemIcons'], row)
        if data['spells']:
            self.label('召唤师技能', True)
            for row in data['spells']:
                self.iconRow(row['ids'], data['spellIcons'], row)
        if data['skillOrder']:
            self.label('技能加点：'+' → '.join(data['skillOrder']))
        if data['perks']:
            self.label('国服符文方案', True)
            for index, row in enumerate(data['perks']):
                self.iconRow(row['perks'], data['runeIcons'], row)
                button = PushButton('应用方案 '+str(index+1)+f"（{row['play']} 场）")
                button.clicked.connect(lambda checked, index=index: self.setRunes(index))
                self.layout.addWidget(button)
        self.status = self.label('')
        button = PushButton('在腾讯 101 查看完整攻略')
        button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.data['url'])))
        self.layout.addWidget(button)

    @asyncSlot(int)
    async def setRunes(self, index):
        row = self.data['perks'][index]
        try:
            pages = await connector.getRunePages()
            current = next((page['id'] for page in pages if page.get('current') and page.get('isEditable')), None)
            name = 'Seraphine 国服：'+self.data['name']
            if current:
                await connector.putRunePage(current, name, row['primaryId'], row['secondaryId'], row['perks'])
            else:
                await createAndSetRunePage(name, row['primaryId'], row['secondaryId'], row['perks'])
            self.status.setText('符文已应用。')
        except Exception as error:
            self.status.setText('符文应用失败：'+str(error)[:100])
