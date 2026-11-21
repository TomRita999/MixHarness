from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialog, QFileDialog, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout

from .desktop_widgets import button, label


ROLE = Qt.ItemDataRole.UserRole


class ProjectDialog(QDialog):
    saved = Signal(str)

    def __init__(self, parent, store, project=None):
        super().__init__(parent)
        self.store = store
        self.pid = project["id"] if project else None
        self.setWindowTitle("编辑项目" if project else "新建项目")
        self.setMinimumWidth(450)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(9)
        layout.addWidget(label("项目名称", "panelTitle"))
        self.name = QLineEdit(project["name"] if project else "")
        self.name.setAccessibleName("项目名称")
        self.name.setPlaceholderText("输入项目名称")
        self.name.setMaxLength(100)
        layout.addWidget(self.name)
        layout.addSpacing(5)
        row = QHBoxLayout()
        row.addWidget(label("项目文件夹", "panelTitle"), 1)
        row.addWidget(button("添加…", self.choose_folder, "plus", "quiet"))
        layout.addLayout(row)
        self.folders = QListWidget()
        self.folders.setAccessibleName("项目文件夹")
        self.folders.setMinimumHeight(85)
        self.folders.setMaximumHeight(158)
        layout.addWidget(self.folders)
        self.hint = label("", "caption")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        actions = QHBoxLayout()
        self.primary = button("设为主目录", self.make_primary, name="quiet")
        self.remove = button("移除", self.remove_folder, name="quiet")
        actions.addWidget(self.primary)
        actions.addWidget(self.remove)
        actions.addStretch()
        layout.addLayout(actions)
        self.error = label("", "caption")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        footer = QHBoxLayout()
        footer.addStretch()
        footer.addWidget(button("取消", self.reject))
        self.save = button("保存" if project else "创建", self.commit, name="primary")
        self.save.setDefault(True)
        footer.addWidget(self.save)
        layout.addLayout(footer)
        self.name.textChanged.connect(self.update_controls)
        self.folders.currentRowChanged.connect(self.update_controls)
        for folder in project["folders"] if project else []:
            self.add_folder(folder["path"])
        self.update_controls()
        self.name.setFocus()

    def choose_folder(self):
        picker = QFileDialog(self, "选择项目文件夹")
        picker.setFileMode(QFileDialog.FileMode.Directory)
        picker.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        picker.fileSelected.connect(self.add_folder)
        picker.open()

    def add_folder(self, directory):
        path = Path(directory)
        for row in range(self.folders.count()):
            if self.folders.item(row).data(ROLE) == str(path):
                self.folders.setCurrentRow(row)
                return
        item = QListWidgetItem(str(path))
        item.setData(ROLE, str(path))
        item.setToolTip(str(path))
        self.folders.addItem(item)
        self.folders.setCurrentItem(item)
        if not self.name.text().strip():
            self.name.setText(path.name or str(path))

    def make_primary(self):
        item = self.folders.takeItem(self.folders.currentRow())
        self.folders.insertItem(0, item)
        self.folders.setCurrentItem(item)
        self.update_controls()

    def remove_folder(self):
        self.folders.takeItem(self.folders.currentRow())
        self.update_controls()

    def update_controls(self):
        count = self.folders.count()
        self.save.setEnabled(bool(self.name.text().strip()))
        self.primary.setEnabled(self.folders.currentRow() > 0)
        self.remove.setEnabled(self.folders.currentRow() >= 0)
        hint = "留空将自动创建工作目录" if count == 0 else ("首项为主工作目录" if count > 1 else "")
        self.hint.setText(hint)
        self.hint.setVisible(bool(hint))

    def commit(self):
        folders = [self.folders.item(i).data(ROLE) for i in range(self.folders.count())]
        try:
            project = self.store.save_project(self.name.text(), folders, self.pid)
        except (OSError, ValueError) as exc:
            self.error.setText(str(exc))
            self.error.show()
            return
        self.saved.emit(project["id"])
        self.accept()


class ArchiveDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.store = window.engine.store
        self.setWindowTitle("归档")
        self.resize(465, 335)
        layout = QVBoxLayout(self)
        self.entries = QListWidget()
        self.entries.setAccessibleName("归档列表")
        layout.addWidget(self.entries, 1)
        self.empty = label("暂无归档内容", "caption")
        layout.addWidget(self.empty)
        row = QHBoxLayout()
        self.delete = button("删除会话…", self.confirm_delete, "trash", "quiet")
        row.addWidget(self.delete)
        row.addStretch()
        row.addWidget(button("关闭", self.reject))
        self.restore = button("恢复", self.restore_entry, name="primary")
        row.addWidget(self.restore)
        layout.addLayout(row)
        self.entries.currentRowChanged.connect(self.update_controls)
        self.reload()

    def reload(self):
        self.entries.clear()
        for project in self.store.projects(archived=True):
            item = QListWidgetItem("项目　" + project["name"])
            item.setData(ROLE, ("project", project["id"]))
            self.entries.addItem(item)
        for chat in self.store.sessions(archived=True):
            item = QListWidgetItem(chat["title"] + "\n" + (chat["project_name"] or "独立会话"))
            item.setData(ROLE, ("session", chat["id"]))
            self.entries.addItem(item)
        if self.entries.count():
            self.entries.setCurrentRow(0)
        self.empty.setVisible(self.entries.count() == 0)
        self.update_controls()

    def update_controls(self):
        item = self.entries.currentItem()
        self.restore.setEnabled(item is not None)
        self.delete.setVisible(bool(item and item.data(ROLE)[0] == "session"))

    def restore_entry(self):
        kind, identifier = self.entries.currentItem().data(ROLE)
        if kind == "project":
            self.store.archive_project(identifier, False)
        else:
            self.store.archive_session(identifier, False)
        self.window.refresh(force=True)
        self.reload()

    def confirm_delete(self):
        _, sid = self.entries.currentItem().data(ROLE)
        self.window.confirm_delete(sid, self)
