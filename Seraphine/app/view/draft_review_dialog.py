from datetime import datetime

from PyQt5.QtCore import Qt, QSignalBlocker, pyqtSignal
from PyQt5.QtWidgets import (QDialog, QLabel, QVBoxLayout, QTableWidget, QTableWidgetItem,
                             QAbstractItemView, QHeaderView, QPushButton, QComboBox, QHBoxLayout)
from app.lol.draft_review import group_summary
from app.view.bp_assistant_widget import LANES


class DraftReviewDialog(QDialog):
    marked = pyqtSignal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('BP 推荐复盘')
        self.resize(780, 470)
        layout = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.note = QLabel('按最终出场英雄匹配锁定前的推荐。重开、待结算、无推荐快照的场次不计胜率。')
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(['选人时间', '分路', '锁定', '出场', '推荐名次', '结果', '版本'])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        layout.addWidget(self.table)
        self.snapshot = QLabel()
        self.snapshot.setWordWrap(True)
        self.snapshot.setTextFormat(Qt.PlainText)
        layout.addWidget(self.snapshot)
        controls = QHBoxLayout()
        controls.addWidget(QLabel('本人是否参考了建议'))
        self.followed = QComboBox()
        for label, value in (('未标记', None), ('参考了', 1), ('未参考', 0)):
            self.followed.addItem(label, value)
        controls.addWidget(self.followed)
        controls.addStretch()
        self.followed.activated.connect(self.mark_selected)
        layout.addLayout(controls)
        self.refresh = QPushButton('补查待结算对局')
        layout.addWidget(self.refresh)
        self.rows = []
        self.table.itemSelectionChanged.connect(self.show_snapshot)

    def render(self, rows, name):
        current = self.table.currentRow()
        game = self.rows[current]['game'] if 0 <= current < len(self.rows) else None
        self.rows = rows
        self.summary.setText(group_summary(rows) if rows else '从本版本开始记录。打完一局后，到这里看推荐命中和胜负。')
        self.table.setRowCount(len(rows))
        selected = 0
        for i, r in enumerate(rows):
            if r['game'] == game:
                selected = i
            rank = r['rank']
            fields = (datetime.fromtimestamp(r['stamp']/1000).strftime('%m-%d %H:%M'),
                      dict(LANES).get(r['lane'], '未知'), name(r['locked']) if r['locked'] else '—',
                      name(r['played']) if r['played'] else '—',
                      '—' if rank is None else f'第{rank}名' if rank else '榜外',
                      r['result'] or '待匹配', r['version'])
            for col, field in enumerate(fields):
                self.table.setItem(i, col, QTableWidgetItem(str(field)))
        if rows:
            self.table.selectRow(selected)
        self.show_snapshot()

    def mark_selected(self, index):
        i = self.table.currentRow()
        if 0 <= i < len(self.rows):
            self.marked.emit(self.rows[i]['game'], self.followed.itemData(index))

    def show_snapshot(self):
        i = self.table.currentRow()
        row = self.rows[i] if 0 <= i < len(self.rows) else {}
        self.followed.setEnabled(bool(row))
        with QSignalBlocker(self.followed):
            self.followed.setCurrentIndex({None:0, 1:1, 0:2}[row.get('followed')])
        picks = row.get('picks', [])
        locked_rank = next((n for n,p in enumerate(picks,1) if p[0] == row.get('locked')), None)
        lock_note = ''
        if row.get('locked') and row.get('played') and row['played'] != row['locked'] and picks:
            lock_note = ('最初锁定第%d名' % locked_rank if locked_rank else '最初锁定榜外英雄') + '，出场英雄已更换。\n'
        self.snapshot.setText(lock_note + '选前推荐　' + '　'.join(f'{n}. {p[1]}' for n,p in enumerate(picks,1))
                              if picks else '暂无选前推荐快照')

