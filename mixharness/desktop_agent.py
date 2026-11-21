from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QSpinBox, QVBoxLayout,
)

from .agent_settings import skill_catalog
from .desktop_widgets import Document, button, label


class AgentDialog(QDialog):
    def __init__(self, parent, engine, sid, pid):
        super().__init__(parent)
        self.engine, self.sid, self.pid = engine, sid, pid
        self.folders = engine.skill_folders(sid, pid)
        settings = engine.settings_for(self.folders)
        self.enabled = set(settings.skills)
        self.setWindowTitle("技能与上下文")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(550, 510)
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(label("上下文窗口", "section"))
        top.addStretch()
        self.window_size = QSpinBox()
        self.window_size.setAccessibleName("上下文窗口")
        self.window_size.setToolTip("设为 0 自动分配；手动设置不少于 4096 token")
        self.window_size.setRange(0, 2000000)
        self.window_size.setSingleStep(4096)
        self.window_size.setSpecialValueText("自动")
        self.window_size.setSuffix(" token")
        self.window_size.setValue(settings.context_window)
        top.addWidget(self.window_size)
        layout.addLayout(top)
        layout.addWidget(label("启用技能", "section"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索技能")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.filter_skills)
        layout.addWidget(self.search)
        self.skills = QListWidget()
        self.skills.setAccessibleName("启用技能")
        self.skills.itemChanged.connect(self.changed_skill)
        layout.addWidget(self.skills, 2)
        dirs = QHBoxLayout()
        dirs.addWidget(label("技能目录", "section"))
        dirs.addStretch()
        dirs.addWidget(button("添加…", self.add_dir))
        self.remove = button("移除", self.remove_dir)
        dirs.addWidget(self.remove)
        layout.addLayout(dirs)
        self.directories = QListWidget()
        self.directories.setAccessibleName("技能目录")
        self.directories.addItems(settings.skill_dirs)
        self.directories.setMaximumHeight(75)
        self.directories.currentRowChanged.connect(lambda row: self.remove.setEnabled(row >= 0))
        self.remove.setEnabled(False)
        layout.addWidget(self.directories)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button("取消", self.reject))
        actions.addWidget(button("保存", self.save, name="primary"))
        layout.addLayout(actions)
        self.refresh_skills()

    def dirs(self):
        return [self.directories.item(i).text() for i in range(self.directories.count())]

    def add_dir(self):
        dialog = QFileDialog(self, "选择技能目录")
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        def selected(path):
            if path not in self.dirs():
                self.directories.addItem(path)
                self.refresh_skills()
        dialog.fileSelected.connect(selected)
        dialog.open()

    def remove_dir(self):
        self.directories.takeItem(self.directories.currentRow())
        self.refresh_skills()

    def refresh_skills(self):
        settings = self.engine.settings.model_copy(update={"skill_dirs": self.dirs()})
        catalog = skill_catalog(settings, self.folders)
        self.skills.clear()
        for name in sorted(catalog.keys() | self.enabled, key=str.casefold):
            item = QListWidgetItem(name)
            item.setToolTip(catalog[name]["description"] + "\n" + catalog[name]["path"] if name in catalog else "技能文件已移除")
            item.setCheckState(Qt.CheckState.Checked if name in self.enabled else Qt.CheckState.Unchecked)
            self.skills.addItem(item)
        self.filter_skills(self.search.text())

    def filter_skills(self, query):
        for i in range(self.skills.count()):
            item = self.skills.item(i)
            item.setHidden(query.casefold() not in item.text().casefold())

    def changed_skill(self, item):
        if item.checkState() == Qt.CheckState.Checked:
            self.enabled.add(item.text())
        else:
            self.enabled.discard(item.text())

    def save(self):
        try:
            self.engine.save_settings({"context_window": self.window_size.value(), "skill_dirs": self.dirs(),
                                       "skills": sorted(self.enabled)}, self.sid, self.pid)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "无法保存设置", str(exc))
            return
        self.accept()


class ApprovalDialog(QDialog):
    def __init__(self, parent, engine, sid, request):
        super().__init__(parent)
        self.setWindowTitle("操作确认")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(560, 370)
        layout = QVBoxLayout(self)
        layout.addWidget(label(request["source"] + " · " + request["title"], "section"))
        detail = Document()
        detail.setPlainText(request["detail"])
        layout.addWidget(detail, 1)
        def decide(allow):
            try:
                engine.decide(sid, request["id"], allow)
            except ValueError as exc:
                QMessageBox.information(self, "审批状态", str(exc))
            self.accept()
        actions = QHBoxLayout()
        actions.addWidget(button("稍后", self.reject))
        actions.addStretch()
        actions.addWidget(button("拒绝", lambda: decide(False)))
        actions.addWidget(button("允许", lambda: decide(True), name="primary"))
        layout.addLayout(actions)
