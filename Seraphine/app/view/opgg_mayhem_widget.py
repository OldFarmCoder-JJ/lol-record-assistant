"""Champion-specific Tencent Hextech recommendations."""
import html
import re
import os

from PyQt5.QtCore import Qt, QTimer, QRectF, QRect
from PyQt5.QtGui import (QPixmap, QDesktopServices, QIcon, QPainter, QColor,
                          QFont, QPainterPath, QLinearGradient, QBrush)
from PyQt5.QtCore import QUrl, QSize
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
                             QTableWidgetItem, QHeaderView, QAbstractItemView)
from app.common.qfluentwidgets import (SearchLineEdit, ComboBox, PushButton, TableWidget,
                                       SubtitleLabel, BodyLabel, CaptionLabel)
from app.components.animation_frame import CardWidget
from qfluentwidgets.components.widgets.table_view import TableItemDelegate


class _TierItem(QTableWidgetItem):
    def __init__(self, tier):
        super().__init__({0:'S',1:'A',2:'B',3:'C',4:'D'}.get(tier,'—'))
        self.setData(Qt.UserRole, tier)

    def __lt__(self, other):
        left, right = self.data(Qt.UserRole), other.data(Qt.UserRole)
        return (left if left is not None else 99) < (right if right is not None else 99)


class _MayhemDelegate(TableItemDelegate):
    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        rect = option.rect
        bg = '#3b3d51' if index.row() in self.selectedRows else '#373946' if self.hoverRow == index.row() else '#30313c'
        painter.fillRect(rect, QColor(bg))
        painter.setPen(QColor('#25262f'))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        if index.column() == 2:
            tier = index.data(Qt.UserRole)
            colors = {0:('#ce92ff','#7245df'),1:('#62baff','#237cc9'),
                      2:('#53d6c7','#1a9b8d'),3:('#ffbf72','#d38637'),4:('#b0b8c8','#687383')}
            top, bottom = colors.get(tier,('#a2aabb','#687383'))
            x, y = rect.center().x()-12, rect.center().y()-15
            path = QPainterPath()
            path.moveTo(x,y); path.lineTo(x+24,y); path.lineTo(x+24,y+24)
            path.lineTo(x+12,y+31); path.lineTo(x,y+24); path.closeSubpath()
            gradient=QLinearGradient(x,y,x+24,y+31)
            gradient.setColorAt(0,QColor(top)); gradient.setColorAt(1,QColor(bottom))
            painter.setPen(Qt.NoPen); painter.setBrush(QBrush(gradient)); painter.drawPath(path)
            painter.setPen(QColor('white')); painter.setFont(QFont('Segoe UI',11,QFont.Bold))
            painter.drawText(QRectF(x,y,24,26),Qt.AlignCenter,index.data(Qt.DisplayRole))
        else:
            painter.setPen(QColor('#eef0f7' if index.column() != 1 else '#aeb8cd'))
            painter.setFont(QFont('Microsoft YaHei',9,QFont.Bold if index.column()==0 else QFont.Normal))
            if index.column() == 0:
                icon=index.data(Qt.DecorationRole)
                if icon: icon.paint(painter,QRect(rect.x()+12,rect.center().y()-20,40,40))
                textRect=rect.adjusted(64,0,-8,0)
                text=painter.fontMetrics().elidedText(index.data(Qt.DisplayRole) or '',Qt.ElideRight,textRect.width())
                painter.drawText(textRect,Qt.AlignVCenter|Qt.AlignLeft,text)
            else:
                painter.drawText(rect,Qt.AlignCenter,index.data(Qt.DisplayRole) or '')
        painter.restore()


class _NumberItem(QTableWidgetItem):
    def __init__(self, value, percent=False):
        super().__init__('—' if value is None else f'{value:.2f}' + ('%' if percent else ''))
        self.setData(Qt.UserRole, float(value) if value is not None else -1)

    def __lt__(self, other):
        return self.data(Qt.UserRole) < other.data(Qt.UserRole)


class MayhemBuildWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(8)
        self.setObjectName('mayhemPanel')
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet('#mayhemPanel { background: #20212a; border-radius: 10px; }')
        self.layout.setContentsMargins(12,12,12,12)
        self.heroHeader = QWidget()
        self.heroHeader.setObjectName('mayhemHeroHeader')
        self.heroHeader.setStyleSheet('#mayhemHeroHeader { background: #292a35; border-radius: 10px; }')
        headerLayout = QHBoxLayout(self.heroHeader)
        headerLayout.setContentsMargins(14,14,14,14)
        self.heroIcon = QLabel()
        self.heroIcon.setFixedSize(64,64)
        headerLayout.addWidget(self.heroIcon)
        self.heroText = QWidget()
        heroTextLayout = QVBoxLayout(self.heroText)
        heroTextLayout.setContentsMargins(4,0,0,0)
        self.title = SubtitleLabel()
        self.title.setObjectName('titleLabel')
        self.note = CaptionLabel()
        self.note.setWordWrap(True)
        self.note.setObjectName('grayBodyLabel')
        self.title.setStyleSheet('color: #f2f3fa; font-size: 22px; font-weight: 700;')
        self.note.setStyleSheet('color: #a5b3cd; font-size: 12px;')
        heroTextLayout.addWidget(self.title)
        heroTextLayout.addWidget(self.note)
        headerLayout.addWidget(self.heroText,1)
        self.layout.addWidget(self.heroHeader)
        self.recommendationCard = QWidget()
        self.recommendationCard.setAttribute(Qt.WA_StyledBackground, True)
        self.recommendationCard.setObjectName('mayhemRecommendations')
        self.recommendationCard.setStyleSheet('#mayhemRecommendations { background: #292a35; border-radius: 8px; }')
        self.recommendationLayout = QVBoxLayout(self.recommendationCard)
        filters = QHBoxLayout()
        self.search = SearchLineEdit()
        self.search.setPlaceholderText('搜索强化符文名称或描述')
        self.search.setStyleSheet('QLineEdit { background: #1c1d24; color: #e9edf7; border: 1px solid #404353; border-radius: 6px; padding: 8px; }')
        self.rarity = ComboBox()
        for name, value in [('全部品质', None), ('白银', 1), ('黄金', 4), ('棱彩', 8)]:
            self.rarity.addItem(name, userData=value)
        filters.addWidget(self.search, 1)
        filters.addWidget(self.rarity)
        self.layout.addLayout(filters)
        self.rarity.setStyleSheet('QPushButton { background: #343644; color: #e9edf7; border: 1px solid #484b60; border-radius: 6px; padding: 6px; }')
        self.countLabel = CaptionLabel()
        self.layout.addWidget(self.countLabel)
        self.countLabel.setStyleSheet('color: #a5b3cd;')
        self.table = TableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(['强化符文', '品质', '评级', '推荐排名', '选取率'])
        self.table.setItemDelegate(_MayhemDelegate(self.table))
        self.table.setStyleSheet('QTableWidget { background: #30313c; border: none; color: #eef0f7; border-radius: 8px; } QHeaderView::section { background: #292a35; color: #adb7cc; border: none; border-bottom: 1px solid #414455; padding: 8px 2px; }')
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setIconSize(QSize(40, 40))
        self.table.verticalHeader().setDefaultSectionSize(54)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for column, width in [(1, 56), (2, 54), (3, 78), (4, 74)]:
            self.table.setColumnWidth(column, width)
        self.table.setMinimumHeight(380)
        self.table.setAccessibleName('海克斯强化符文推荐，可点击列名排序')
        self.layout.addWidget(self.table)
        self.layout.addWidget(self.recommendationCard)
        self.openButton = PushButton('在腾讯 101 查看国服完整攻略')
        self.openButton.setStyleSheet('QPushButton { background: #506fe3; color: white; border: none; border-radius: 6px; padding: 8px; }')
        self.openButton.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.url)))
        self.layout.addWidget(self.openButton)
        self.search.textChanged.connect(self.filterRows)
        self.rarity.currentIndexChanged.connect(self.filterRows)
        self.url = ''
        self._iconCache = {}
        self._placeholderCache = {}
        self._resolvedIcons = set()
        self._iconData = {}
        self._downloadTask = None
        self.iconTimer = QTimer(self)
        self.iconTimer.setInterval(250)
        self.iconTimer.timeout.connect(self.refreshIcons)

    def iconTile(self, path, rarity):
        if path in self._iconCache:
            return self._iconCache[path]
        image = QPixmap(path) if path and os.path.isfile(path) else QPixmap()
        if image.isNull() and rarity in self._placeholderCache:
            return self._placeholderCache[rarity]
        tile = QPixmap(128, 128)
        tile.fill(Qt.transparent)
        painter = QPainter(tile)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        colors = {1: '#91a5b5', 4: '#d6ae5c', 8: '#b896f6'}
        painter.setPen(QColor(colors.get(rarity, '#8994a5')))
        painter.setBrush(QColor('#202636'))
        painter.drawRoundedRect(QRectF(2, 2, 124, 124), 18, 18)
        if not image.isNull():
            painter.drawPixmap(QRect(12, 12, 104, 104), image, image.rect())
        else:
            painter.setPen(QColor('#b8c2d3'))
            painter.drawText(tile.rect(), Qt.AlignCenter, '…')
        painter.end()
        icon = QIcon(tile)
        if not image.isNull():
            self._iconCache[path] = icon
        else:
            self._placeholderCache[rarity] = icon
        return icon

    def refreshIcons(self):
        updated = 0
        pending = False
        sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is None:
                continue
            augment = self._iconData.get(item.data(Qt.UserRole+1))
            if augment and augment['id'] not in self._resolvedIcons and os.path.isfile(augment['icon']):
                if updated >= 16:
                    pending = True
                    break
                item.setIcon(self.iconTile(augment['icon'], augment.get('rarity')))
                self._resolvedIcons.add(augment['id'])
                updated += 1
        self.table.setSortingEnabled(sorting)
        if not pending and (self._downloadTask is None or self._downloadTask.done()):
            self.iconTimer.stop()

    def updateWidget(self, data):
        self.iconTimer.stop()
        self._resolvedIcons.clear()
        self._iconData = {a['id']: a for a in data['augments']}
        self._downloadTask = data.get('_iconTask')
        self.url = data['url']
        self.title.setText(data['name'] + ' · 海克斯大乱斗')
        self.heroIcon.setPixmap(QPixmap(data['icon']).scaled(64,64,Qt.KeepAspectRatio,Qt.SmoothTransformation))
        date = data['statDate']
        self.note.setText(data['name'] + '的强化推荐 · 腾讯 101 国服\n'
                          '官方评级与选取率 · 统计日期 ' + date[:4] + '-' + date[4:6] + '-' + date[6:8])
        while self.recommendationLayout.count():
            widget = self.recommendationLayout.takeAt(0).widget()
            if widget:
                widget.deleteLater()
        rec = data['recommendations']
        for key, title, icons in [
                ('spells', '召唤师技能', data['spellIcons']),
                ('starter_items', '出门装', data['itemIcons']),
                ('boots', '鞋子', data['itemIcons']),
                ('core_items', '核心出装', data['itemIcons']),
                ('other_items', '其他成装', data['itemIcons'])]:
            if not rec.get(key):
                continue
            label = BodyLabel(title)
            label.setStyleSheet('color: #e6eaf6; font-weight: 600;')
            label.setObjectName('subtitleLabel')
            self.recommendationLayout.addWidget(label)
            for index, ids in enumerate(rec[key]):
                row = QWidget()
                layout = QHBoxLayout(row)
                layout.setContentsMargins(0, 0, 0, 0)
                for itemId in ids:
                    icon = QLabel()
                    icon.setPixmap(QPixmap(icons[itemId]).scaled(32, 32, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                    icon.setToolTip(str(itemId))
                    layout.addWidget(icon)
                layout.addStretch()
                stats = data.get('recommendationStats', {}).get(key, [])
                if index < len(stats):
                    stat = stats[index]
                    rates = CaptionLabel(f"胜率 {stat['winRate']:.2f}% · 登场 {stat['pickRate']:.2f}%")
                    rates.setStyleSheet('color: #a5b3cd;')
                    layout.addWidget(rates)
                self.recommendationLayout.addWidget(row)
        if rec['skillOrder']:
            label = BodyLabel('技能加点：' + ' → '.join(rec['skillOrder']))
            label.setStyleSheet('color: #a5b3cd;')
            label.setWordWrap(True)
            self.recommendationLayout.addWidget(label)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(data['augments']))
        ordered = sorted(data['augments'], key=lambda a: (
            a.get('tier') if a.get('tier') is not None else 99, a['rank']))
        for row, augment in enumerate(ordered):
            name = augment.get('name') or f"强化符文 #{augment['id']}"
            description = re.sub('<[^>]*>', '', augment.get('tooltip') or augment.get('desc') or '')
            item = QTableWidgetItem(name)
            item.setIcon(self._iconCache.get(augment['icon']) or self.iconTile('', augment.get('rarity')))
            item.setData(Qt.UserRole+1, augment['id'])
            item.setData(Qt.UserRole, (name+' '+description).casefold())
            item.setToolTip(html.escape(description))
            self.table.setItem(row, 0, item)
            rarity = augment.get('rarity')
            item = QTableWidgetItem({1: '白银', 4: '黄金', 8: '棱彩'}.get(rarity, '未知'))
            item.setData(Qt.UserRole, rarity)
            self.table.setItem(row, 1, item)
            tier = augment.get('tier')
            self.table.setItem(row, 2, _TierItem(tier))
            rankItem = _NumberItem(augment.get('rank'))
            rankItem.setText(str(augment['rank']))
            self.table.setItem(row, 3, rankItem)
            self.table.setItem(row, 4, _NumberItem(augment.get('popular'), True))
        self.table.setSortingEnabled(True)
        # Preserve the website's ordered ranking within each official grade.
        self.table.sortItems(2, Qt.AscendingOrder)
        self.table.horizontalHeader().setSortIndicatorShown(False)
        self.search.clear()
        self.filterRows()
        self.iconTimer.start()
        QTimer.singleShot(0, self.refreshIcons)

    def filterRows(self, *args):
        text = self.search.text().strip().casefold()
        rarity = self.rarity.currentData()
        count = 0
        header = self.table.horizontalHeader()
        column, order = header.sortIndicatorSection(), header.sortIndicatorOrder()
        self.table.setSortingEnabled(False)
        for row in range(self.table.rowCount()):
            augment = self._iconData.get(self.table.item(row, 0).data(Qt.UserRole+1))
            stats = augment.get('rarityStats', {}).get(rarity, augment)
            self.table.setItem(row, 2, _TierItem(stats.get('tier')))
            rankItem = _NumberItem(stats.get('rank'))
            rankItem.setText(str(stats['rank']))
            self.table.setItem(row, 3, rankItem)
            self.table.setItem(row, 4, _NumberItem(stats.get('popular'), True))
            matches = text in self.table.item(row, 0).data(Qt.UserRole)
            matches = matches and (rarity is None or self.table.item(row, 1).data(Qt.UserRole) == rarity)
            self.table.setRowHidden(row, not matches)
            count += int(matches)
        self.table.setSortingEnabled(True)
        if column >= 0:
            self.table.sortItems(column, order)
        header.setSortIndicatorShown(False)
        self.countLabel.setText(f'强化符文 {count} / {self.table.rowCount()} 条' if count else '没有匹配的强化符文，请调整搜索或品质。')
