import asyncio
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QSignalBlocker, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QInputDialog, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox, QSizePolicy, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .desktop_projects import ArchiveDialog, ProjectDialog
from .desktop_agent import AgentDialog, ApprovalDialog
from .agent_settings import PERMISSIONS, SPEEDS, available_speeds
from .slash import execute_command
from .desktop_clients import ClientHistory
from .desktop_usage import UsageDialog
from .desktop_style import RESOURCES, THEMES, apply_theme
from .desktop_widgets import (
    ALIGN, ArtifactDialog, Document, Editor, ModelPicker, SettingsDialog, Submenu, Timeline, button, icon, label,
)
from .providers import MODELS
from .clients import CLIENTS
from .reasoning import LEVEL_NAMES, group_for, resolve_choice
from .store import LABELS, STATUSES, new_id


USER_ROLE = Qt.ItemDataRole.UserRole


class Window(QMainWindow):
    closed = Signal()

    def __init__(self, engine, settings):
        super().__init__()
        self.engine, self.settings = engine, settings
        self.sid = None
        self.pid = None
        self.catalog_tasks = {}
        self.backend_task = None
        self.backend_error = ""
        self.draft_permission, self.draft_speed = "ask", "standard"
        self.steering = set()
        self.versions = {}
        self.preview_text = ""
        self.history_key = None
        self.config_revision = engine.config_revision
        self.setWindowTitle("MixHarness")
        self.setWindowIcon(QIcon(str(RESOURCES / "app.png")))
        self.resize(1140, 740)
        self.setMinimumSize(700, 490)
        self.layout_row = QHBoxLayout()
        self.layout_row.setContentsMargins(0, 0, 0, 0)
        self.layout_row.setSpacing(0)
        central = QWidget()
        central.setObjectName("main")
        central.setLayout(self.layout_row)
        self.setCentralWidget(central)
        self.make_sidebar()
        self.make_chat()
        self.make_inspector()
        self.make_menus()
        self.set_theme(str(settings.value("theme", "light")))
        geometry = settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        sessions = self.session_list()
        previous = settings.value("session")
        if any(s["id"] == previous for s in sessions):
            self.select_session(previous)
        elif settings.contains("project") and not previous:
            saved = settings.value("project")
            pid = saved if saved in {p["id"] for p in engine.store.projects()} else None
            self.create_chat(pid)
        elif sessions:
            self.select_session(sessions[0]["id"])
        elif engine.workspace:
            self.open_project(engine.workspace)
        else:
            self.create_chat(None)
        self.timer = QTimer(self)
        self.timer.setInterval(180)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()

    def make_sidebar(self):
        self.sidebar = QFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(254)
        col = QVBoxLayout(self.sidebar)
        col.setContentsMargins(13, 14, 13, 13)
        col.setSpacing(3)
        brand = QHBoxLayout()
        brand.setContentsMargins(6, 0, 1, 14)
        mark = label("")
        mark.setPixmap(QIcon(str(RESOURCES / "app.png")).pixmap(QSize(24, 24), self.devicePixelRatioF()))
        brand.addWidget(mark)
        brand.addWidget(label("MixHarness", "brand"))
        brand.addStretch()
        brand.addWidget(button("收起侧栏", self.toggle_sidebar, "sidebar", icon_only=True))
        col.addLayout(brand)
        col.addWidget(button("新建会话", self.new_session, "new", "nav"))
        col.addWidget(button("搜索会话", self.search, "search", "nav"))
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("搜索项目或会话")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self.fill_history)
        col.addWidget(self.filter)
        self.filter.hide()
        col.addSpacing(20)
        project_actions = QHBoxLayout()
        caption = label("项目", "section")
        caption.setIndent(8)
        project_actions.addWidget(caption, 1)
        self.create_project_button = button("新建项目", self.project_dialog, "plus", "rowAction", icon_only=True)
        self.create_project_button.setFixedSize(26, 26)
        project_actions.addWidget(self.create_project_button)
        col.addLayout(project_actions)
        self.sources_box = QComboBox()
        self.sources_box.setAccessibleName("会话来源")
        for title, value in (("MixHarness", "mixharness"), ("全部来源", "all"),
                             ("Codex", "codex"), ("Antigravity", "antigravity")):
            self.sources_box.addItem(title, value)
        col.addWidget(self.sources_box)
        self.history = QTreeWidget()
        self.history.setAccessibleName("项目与会话")
        self.history.setColumnCount(2)
        self.history.header().setStretchLastSection(False)
        self.history.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.history.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.history.setColumnWidth(1, 48)
        self.history.setHeaderHidden(True)
        self.history.setIndentation(15)
        self.history.setExpandsOnDoubleClick(False)
        self.history.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.history.customContextMenuRequested.connect(self.history_menu)
        self.history.itemCollapsed.connect(lambda item: self.remember_expanded(item, False))
        self.history.itemExpanded.connect(lambda item: self.remember_expanded(item, True))
        self.history.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.history.currentItemChanged.connect(self.history_changed)
        col.addWidget(self.history, 1)
        self.client_history = ClientHistory(self)
        col.addWidget(self.client_history, 1)
        self.client_history.hide()
        self.sources_box.currentIndexChanged.connect(self.select_source)
        col.addWidget(button("归档", self.show_archive, "archive", "nav"))
        foot = QHBoxLayout()
        foot.addWidget(button("设置", self.connections, "settings", "nav"), 1)
        self.theme_button = button("切换主题", self.toggle_theme, "moon", icon_only=True)
        foot.addWidget(self.theme_button)
        col.addLayout(foot)
        self.layout_row.addWidget(self.sidebar)

    def make_chat(self):
        main = QWidget()
        main.setObjectName("content")
        body = QVBoxLayout(main)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        header = QHBoxLayout()
        header.setContentsMargins(17, 10, 16, 8)
        self.expand_side = button("展开侧栏", self.toggle_sidebar, "sidebar", icon_only=True)
        self.expand_side.hide()
        header.addWidget(self.expand_side)
        self.project_name = button("选择项目", self.project_picker, "folder", "quiet")
        self.project_name.setMaximumWidth(200)
        self.title = label("新会话", "title")
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        header.addWidget(self.project_name)
        header.addWidget(label("／", "context"))
        header.addWidget(self.title, 1)
        self.chat_more = button("会话操作", self.current_chat_menu, "more", icon_only=True)
        header.addWidget(self.chat_more)
        self.inspect_button = button("任务与成果", self.toggle_inspector, "inspector", icon_only=True)
        header.addWidget(self.inspect_button)
        body.addLayout(header)
        shell = QHBoxLayout()
        shell.setContentsMargins(22, 4, 22, 14)
        view = QWidget()
        view.setMaximumWidth(760)
        self.chat_layout = QVBoxLayout(view)
        col = self.chat_layout
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(7)
        col.addStretch(1)
        self.heading = label("想在这个项目里做什么？", "heading")
        self.heading.setAlignment(ALIGN.AlignCenter)
        col.addWidget(self.heading)
        col.addSpacing(12)
        self.timeline = Timeline()
        col.addWidget(self.timeline, 1)
        self.preview = Document()
        self.preview.setAccessibleName("正在生成的内容")
        self.preview.setMaximumHeight(110)
        col.addWidget(self.preview)
        self.preview.hide()
        self.work_status = button("", self.status_action, name="quiet")
        col.addWidget(self.work_status, 0, ALIGN.AlignLeft)
        self.work_status.hide()
        self.composer = QFrame()
        self.composer.setObjectName("composer")
        controls = QVBoxLayout(self.composer)
        controls.setContentsMargins(14, 11, 12, 10)
        controls.setSpacing(6)
        self.editor = Editor()
        self.editor.submitted.connect(self.send)
        self.editor.textChanged.connect(self.editor_changed)
        controls.addWidget(self.editor)
        tools = QHBoxLayout()
        tools.setSpacing(8)
        self.pickers = {}
        for provider, title in (("gemini", "Gemini"), ("openai", "OpenAI")):
            pick = ModelPicker(provider, title)
            pick.setMaximumWidth(205)
            pick.changed.connect(self.choose_model)
            pick.refresh_requested.connect(self.refresh_catalog)
            pick.usage_requested.connect(self.show_usage)
            self.pickers[provider] = pick
            tools.addWidget(pick, 1)
        tools.addStretch()
        self.speed_box = QComboBox()
        self.speed_box.setAccessibleName("Codex 速度")
        self.speed_box.setToolTip("Codex 速度")
        self.speed_box.currentIndexChanged.connect(self.change_speed)
        tools.addWidget(self.speed_box, 0, ALIGN.AlignBottom)
        self.backend_box = QComboBox()
        self.backend_box.setAccessibleName("会话执行方式")
        self.backend_box.setToolTip("执行方式")
        self.backend_box.setCursor(Qt.CursorShape.PointingHandCursor)
        self.backend_box.addItem("API", "api")
        self.backend_box.addItem("本机客户端", "clients")
        self.backend_box.currentIndexChanged.connect(self.change_backend)
        tools.addWidget(self.backend_box, 0, ALIGN.AlignBottom)
        self.send_button = button("发送消息", self.send, "send", "send", icon_only=True)
        self.send_button.setEnabled(False)
        tools.addWidget(self.send_button, 0, ALIGN.AlignBottom)
        controls.addLayout(tools)
        col.addWidget(self.composer)
        foot = QHBoxLayout()
        self.permission_box = QComboBox()
        self.permission_box.setAccessibleName("操作权限")
        self.permission_box.setToolTip("对应 Antigravity 的 Default / Auto / Turbo")
        for mode, title in PERMISSIONS.items():
            self.permission_box.addItem(title, mode)
        self.permission_box.currentIndexChanged.connect(self.change_permission)
        foot.addWidget(self.permission_box)
        self.connection_state = button("", self.connections, name="quiet")
        foot.addWidget(self.connection_state)
        foot.addStretch()
        col.addLayout(foot)
        col.addStretch(1)
        shell.addWidget(view)
        body.addLayout(shell, 1)
        self.layout_row.addWidget(main, 1)

    def make_inspector(self):
        self.inspector = QFrame()
        self.inspector.setObjectName("inspector")
        self.inspector.setFixedWidth(260)
        col = QVBoxLayout(self.inspector)
        col.setContentsMargins(12, 10, 12, 14)
        row = QHBoxLayout()
        row.addWidget(label("任务与成果", "panelTitle"), 1)
        row.addWidget(button("关闭面板", self.toggle_inspector, "close", icon_only=True))
        col.addLayout(row)
        self.tabs = QTabWidget()
        self.tasks = QListWidget()
        self.assets = QListWidget()
        self.brief = Document()
        for view, title in ((self.tasks, "任务"), (self.assets, "成果"), (self.brief, "需求")):
            view.setAccessibleName(title)
            self.tabs.addTab(view, title)
        self.tasks.itemActivated.connect(self.task_detail)
        self.assets.itemActivated.connect(self.artifact_detail)
        self.tabs.currentChanged.connect(self.inspector_action)
        self.tasks.currentItemChanged.connect(self.inspector_action)
        self.assets.currentItemChanged.connect(self.inspector_action)
        col.addWidget(self.tabs, 1)
        self.detail_button = button("查看详情", self.open_detail)
        col.addWidget(self.detail_button)
        self.layout_row.addWidget(self.inspector)
        self.inspector.hide()

    def make_menus(self):
        file_menu = self.menuBar().addMenu("文件")
        view_menu = self.menuBar().addMenu("视图")
        for menu, title, key, callback in (
            (file_menu, "新建会话", QKeySequence.StandardKey.New, self.new_session),
            (file_menu, "新建项目…", "", self.project_dialog),
            (file_menu, "打开项目…", QKeySequence.StandardKey.Open, self.choose_project),
            (file_menu, "添加文件夹…", "", self.choose_source),
            (file_menu, "新建独立会话", "Ctrl+Shift+N", self.new_standalone),
            (file_menu, "归档…", "", self.show_archive),
            (file_menu, "打开会话库…", "", self.choose_library),
            (file_menu, "设置…", QKeySequence.StandardKey.Preferences, self.connections),
            (file_menu, "技能与上下文…", "", self.agent_settings),
            (file_menu, "退出 MixHarness", QKeySequence.StandardKey.Quit, self.close),
            (view_menu, "搜索会话", QKeySequence.StandardKey.Find, self.search),
            (view_menu, "切换侧栏", "Ctrl+B", self.toggle_sidebar),
            (view_menu, "任务与成果", "Ctrl+Shift+I", self.toggle_inspector),
            (view_menu, "切换主题", "Ctrl+Shift+L", self.toggle_theme),
        ):
            action = QAction(title, self)
            action.setShortcut(QKeySequence(key))
            if callback == self.search:
                action.setShortcuts([QKeySequence(key), QKeySequence("Ctrl+K")])
            if callback == self.close:
                action.setMenuRole(QAction.MenuRole.QuitRole)
            elif callback == self.connections:
                action.setMenuRole(QAction.MenuRole.PreferencesRole)
            action.triggered.connect(callback)
            menu.addAction(action)

    def session_list(self, query=""):
        return self.engine.store.sessions(query=query)

    def remember_expanded(self, item, expanded):
        if item.parent() is None and not self.filter.text().strip():
            self.settings.setValue("collapsed/" + (item.data(0, USER_ROLE) or "local"), not expanded)

    def fill_history(self):
        query = self.filter.text().strip()
        groups = self.engine.store.projects()
        groups.append({"id": None, "name": "独立会话", "folders": [], "pinned": False})
        by_project = {}
        chats = self.session_list(query)
        for chat in chats:
            by_project.setdefault(chat["project_id"], []).append(chat)
        signature = (self.sid, self.pid, query, groups, chats,
                     [self.settings.value("collapsed/" + (p["id"] or "local"), False, type=bool) for p in groups])
        if signature == self.history_key:
            return
        self.history_key = signature
        scroll = self.history.verticalScrollBar().value()
        with QSignalBlocker(self.history):
            self.history.clear()
            for project in groups:
                pid, title = project["id"], project["name"]
                visible = by_project.get(pid, [])
                if query and not visible and query.casefold() not in title.casefold():
                    continue
                group = QTreeWidgetItem(self.history, [title])
                group.setIcon(0, icon("pin" if project["pinned"] else ("folder" if pid else "chat")))
                group.setToolTip(0, "\n".join(f["path"] for f in project["folders"]) or title)
                group.setData(0, USER_ROLE, pid)
                self.add_row_actions(group, pid, project=True)
                if self.sid is None and pid == self.pid:
                    self.history.setCurrentItem(group)
                for session in visible:
                    item = QTreeWidgetItem(group, [session["title"]])
                    item.setToolTip(0, session["title"])
                    item.setData(0, USER_ROLE, session["id"])
                    if session["pinned"]:
                        item.setIcon(0, icon("pin"))
                    self.add_row_actions(item, session["id"], project=False)
                    if session["id"] == self.sid:
                        self.history.setCurrentItem(item)
                collapsed = self.settings.value("collapsed/" + (pid or "local"), False, type=bool)
                group.setExpanded(bool(query) or not collapsed)
        self.history.verticalScrollBar().setValue(scroll)

    def add_row_actions(self, item, identifier, *, project):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addStretch()
        if project:
            compose = button("在此新建会话", lambda: self.create_chat(identifier), "new", "rowAction", True)
            compose.setFixedSize(23, 24)
            layout.addWidget(compose)
        if identifier:
            action = button("项目操作" if project else "会话操作", lambda: self.show_row_menu(
                identifier, project, action.mapToGlobal(action.rect().bottomLeft())), "more", "rowAction", True)
            action.setFixedSize(23, 24)
            action.setAccessibleName(item.text(0) + "，" + action.accessibleName())
            layout.addWidget(action)
        self.history.setItemWidget(item, 1, row)

    def history_changed(self, item, previous):
        if not item:
            return
        value = item.data(0, USER_ROLE)
        if item.parent() is None:
            if value != self.pid:
                self.activate_project(value)
        elif value != self.sid:
            self.select_session(value)

    def search(self):
        self.sidebar.show()
        self.expand_side.hide()
        self.sources_box.setCurrentIndex(0)
        self.filter.show()
        self.filter.setFocus()
        self.filter.selectAll()

    def new_session(self, checked=False, directory=None):
        if directory is not None:
            return self.open_project(directory)
        self.create_chat(self.pid)

    def new_standalone(self):
        self.create_chat(None)

    def create_chat(self, pid):
        self.sources_box.setCurrentIndex(0)
        self.filter.clear()
        self.settings.setValue("collapsed/" + (pid or "local"), False)
        self.open_view(pid, None)

    def select_source(self):
        source = self.sources_box.currentData()
        local = source == "mixharness"
        self.history.setVisible(local)
        self.client_history.setVisible(not local)
        if not local:
            self.filter.hide()
        self.client_history.select_source(None if local else source)

    def run_task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.steering.add(task)
        task.add_done_callback(self.steering.discard)
        return task

    def catalogs(self):
        return self.engine.client_catalogs if self.backend_box.currentData() == "clients" else self.engine.catalogs

    def model_key(self, provider, backend=None):
        return "client-models/" + provider if (backend or self.backend_box.currentData()) == "clients" else provider

    def preferred_models(self, backend, session):
        remembered = session.get("backend_models", {}).get(backend, {})
        models = {}
        for provider in self.pickers:
            pref = self.settings.value(self.model_key(provider, backend), "")
            if backend == "clients":
                available = self.engine.client_catalogs[provider].view()["models"]
                options = (remembered.get(provider), pref, session.get(provider))
                models[provider] = next((m for m in options if m in available), next(iter(available), ""))
            else:
                models[provider] = remembered.get(provider) or pref or MODELS[provider][0]
        return models

    def preferred_efforts(self, backend, session, models):
        remembered = session.get("backend_models", {}).get(backend, {})
        catalogs = self.engine.client_catalogs if backend == "clients" else self.engine.catalogs
        selected = {}
        for p, model in models.items():
            key = self.model_key(p, backend)
            effort = remembered.get("efforts", {}).get(p) if remembered.get(p) == model else None
            if effort is None and self.settings.value(key) == model:
                effort = self.settings.value(key + "-effort", "")
            group = group_for(catalogs[p].view()["groups"], model)
            if group:
                selected[p] = resolve_choice(group, model, effort if effort in group["choices"] else "")[1]
            else:
                selected[p] = ""
        return selected

    def cancel_backend_change(self):
        if self.backend_task:
            self.backend_task.cancel()
            self.backend_task = None
        self.backend_error = ""

    def change_backend(self):
        self.cancel_backend_change()
        backend = self.backend_box.currentData()
        if not self.sid:
            self.draft_speed = "standard"
            self.settings.setValue("backend", backend)
            self.refresh(force=True)
            self.refresh_catalogs()
            return
        sid = self.sid
        if backend == self.engine.store.session(sid)["backend"]:
            self.refresh(force=True)
            return
        if backend == "api":
            self.apply_backend(sid, backend)
            self.refresh_catalogs()
            return

        async def select_clients():
            await asyncio.gather(*(catalog.refresh() for catalog in self.engine.client_catalogs.values()))
            if self.backend_task is asyncio.current_task() and self.sid == sid:
                self.backend_task = None
                self.apply_backend(sid, "clients")

        self.backend_task = self.run_task(select_clients())
        self.refresh(force=True)

    def apply_backend(self, sid, backend):
        session = self.engine.store.session(sid)
        models = self.preferred_models(backend, session)
        try:
            self.engine.switch_backend(sid, backend, models, self.preferred_efforts(backend, session, models))
        except ValueError as exc:
            self.backend_error = str(exc)
        self.refresh(force=True)

    def project_dialog(self, checked=False, *, pid=None):
        store = self.engine.store
        dialog = ProjectDialog(self, store, store.project(pid) if pid else None)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        if pid:
            dialog.saved.connect(lambda _: self.refresh(force=True))
        else:
            dialog.saved.connect(self.activate_project)
        dialog.open()

    def project_picker(self):
        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        for project in self.engine.store.projects():
            action = menu.addAction(icon("folder"), project["name"],
                                    lambda checked=False, pid=project["id"]: self.choose_context(pid))
            action.setCheckable(True)
            action.setChecked(self.pid == project["id"])
        menu.addAction("独立会话", lambda: self.choose_context(None))
        menu.addSeparator()
        menu.addAction(icon("plus"), "新建项目…", self.project_dialog)
        menu.popup(self.project_name.mapToGlobal(self.project_name.rect().bottomLeft()))

    def choose_context(self, pid):
        if pid == self.pid:
            return
        if self.sid:
            self.activate_project(pid)
            return
        old_key, text = self.draft_key(), self.editor.toPlainText()
        self.create_chat(pid)
        if text and not self.editor.toPlainText():
            self.editor.setPlainText(text)
            self.save_draft()
            self.settings.remove(old_key)

    def choose_project(self):
        self.pick_folder("打开项目文件夹", self.open_project)

    def pick_folder(self, title, callback):
        dialog = QFileDialog(self, title)
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.fileSelected.connect(callback)
        dialog.open()

    def open_project(self, directory):
        try:
            project = self.engine.store.open_project(directory)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "无法打开项目", str(exc))
            return
        self.activate_project(project["id"])

    def activate_project(self, pid):
        self.filter.clear()
        self.settings.setValue("collapsed/" + (pid or "local"), False)
        chats = [s["id"] for s in self.session_list() if s["project_id"] == pid]
        if chats:
            recent = self.settings.value("recent/" + (pid or "local"))
            if recent != "":
                self.select_session(recent if recent in chats else chats[0])
                return
        self.create_chat(pid)

    def choose_source(self, checked=False, *, pid=None):
        pid = pid or self.pid
        if not pid:
            self.choose_project()
            return
        def selected(path):
            try:
                self.engine.store.attach_folder(pid, path)
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "无法添加文件夹", str(exc))
                return
            self.refresh(force=True)
        self.pick_folder("添加文件夹", selected)

    def history_menu(self, point):
        item = self.history.itemAt(point)
        if item:
            self.show_row_menu(item.data(0, USER_ROLE), item.parent() is None,
                               self.history.viewport().mapToGlobal(point))

    def current_chat_menu(self):
        if self.sid:
            self.show_row_menu(self.sid, False, self.chat_more.mapToGlobal(self.chat_more.rect().bottomLeft()))

    def show_row_menu(self, identifier, project_row, position):
        menu = QMenu(self.history)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        if not project_row:
            chat = self.engine.store.session(identifier)
            menu.addAction(icon("pin"), "取消置顶" if chat["pinned"] else "置顶会话",
                           lambda: self.pin_row(identifier, False, not chat["pinned"]))
            menu.addAction("重命名会话…", lambda: self.rename_chat(identifier))
            menu.addAction(icon("archive"), "归档会话", lambda: self.archive_chat(identifier))
            menu.addSeparator()
            menu.addAction(icon("trash"), "删除会话…", lambda: self.confirm_delete(identifier))
        else:
            menu.addAction(icon("new"), "在此新建会话", lambda: self.create_chat(identifier))
        if project_row and identifier:
            pid = identifier
            project = self.engine.store.project(pid)
            menu.addAction(icon("pin"), "取消置顶" if project["pinned"] else "置顶项目",
                           lambda: self.pin_row(pid, True, not project["pinned"]))
            menu.addAction("编辑项目…", lambda: self.project_dialog(pid=pid))
            folders = Submenu("在文件管理器中打开", menu)
            for folder in project["folders"]:
                path = folder["path"]
                folders.addAction(path, lambda checked=False, path=path: QDesktopServices.openUrl(QUrl.fromLocalFile(path)))
            menu.addSeparator()
            menu.addAction("移出侧栏", lambda: self.hide_project(pid))
        menu.popup(position)

    def pin_row(self, identifier, project, pinned):
        if project:
            self.engine.store.pin_project(identifier, pinned)
        else:
            self.engine.store.pin_session(identifier, pinned)
        self.fill_history()

    def rename_chat(self, sid):
        dialog = QInputDialog(self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle("重命名会话")
        dialog.setLabelText("会话名称")
        dialog.setTextValue(self.engine.store.session(sid)["title"])
        dialog.setOkButtonText("保存")
        dialog.setCancelButtonText("取消")
        def save(name):
            try:
                self.engine.store.rename_session(sid, name)
            except ValueError as exc:
                QMessageBox.information(self, "会话名称", str(exc))
                return
            self.refresh(force=True)
        dialog.textValueSelected.connect(save)
        dialog.open()

    def archive_chat(self, sid):
        self.engine.store.archive_session(sid)
        self.after_removal(sid)

    def confirm_delete(self, sid, archive=None):
        chat = self.engine.store.session(sid)
        dialog = QMessageBox(archive or self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle("删除会话")
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText("删除会话「" + chat["title"] + "」？")
        dialog.setInformativeText("聊天记录与生成内容将被移除，本地项目文件不受影响。")
        cancel = dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        delete = dialog.addButton("删除会话", QMessageBox.ButtonRole.DestructiveRole)
        dialog.setDefaultButton(cancel)
        dialog.setEscapeButton(cancel)
        def finished(_):
            if dialog.clickedButton() != delete:
                return
            self.engine.delete_session(sid)
            if sid == self.sid:
                self.editor.clear()
            self.settings.remove("drafts/" + sid)
            recent = "recent/" + (chat["project_id"] or "local")
            if self.settings.value(recent) == sid:
                self.settings.remove(recent)
            self.after_removal(sid)
            if archive:
                archive.reload()
        dialog.finished.connect(finished)
        dialog.open()

    def after_removal(self, sid):
        if sid == self.sid:
            self.activate_project(self.pid)
        else:
            self.fill_history()

    def show_archive(self):
        dialog = ArchiveDialog(self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.open()

    def hide_project(self, pid):
        self.engine.store.archive_project(pid)
        if pid != self.pid:
            self.fill_history()
            return
        chats = self.session_list()
        if chats:
            self.select_session(chats[0]["id"])
        else:
            self.new_standalone()

    def choose_library(self):
        dialog = QFileDialog(self, "选择包含 .mixharness 的目录")
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.fileSelected.connect(self.open_library)
        dialog.open()

    def open_library(self, path):
        project = Path(path).resolve()
        if project == self.engine.project:
            return
        if not (project / ".mixharness" / "sessions.sqlite3").is_file():
            QMessageBox.information(self, "未找到会话库", "所选目录中没有 .mixharness 会话库。")
            return
        args = [] if getattr(sys, "frozen", False) else ["-m", "mixharness"]
        started, _ = QProcess.startDetached(sys.executable, [*args, "--project", str(project)])
        if not started:
            QMessageBox.warning(self, "无法打开会话库", str(project))

    def select_session(self, sid):
        self.open_view(self.engine.store.session(sid)["project_id"], sid)

    def open_view(self, pid, sid):
        self.cancel_backend_change()
        if hasattr(self, "state"):
            self.save_draft()
        self.pid, self.sid = pid, sid
        self.draft_permission, self.draft_speed = "ask", "standard"
        self.settings.setValue("project", pid or "")
        self.settings.setValue("session", sid or "")
        self.settings.setValue("recent/" + (pid or "local"), sid or "")
        self.timeline.reset()
        self.editor.setPlainText(str(self.settings.value(self.draft_key(), "")))
        self.refresh(force=True)
        self.editor.setFocus()

    def draft_key(self):
        return "drafts/" + self.sid if self.sid else "composers/" + (self.pid or "local")

    def save_draft(self):
        key = self.draft_key()
        text = self.editor.toPlainText()
        if text:
            self.settings.setValue(key, text)
        else:
            self.settings.remove(key)

    def choose_model(self, provider, value, effort=""):
        if self.backend_task:
            return
        catalog = self.catalogs()[provider]
        if self.backend_box.currentData() == "clients" and value not in catalog.view()["models"]:
            return
        value, effort = self.engine.model_choice(self.backend_box.currentData(), provider, value, effort)
        key = self.model_key(provider)
        self.settings.setValue(key, value)
        self.settings.setValue(key + "-effort", effort)
        if not self.sid:
            self.pickers[provider].select(value, catalog.view(), effort)
            if provider == "openai":
                self.draft_speed = "standard"
                self.refresh_access()
            self.editor_changed()
            return
        session = self.engine.store.session(self.sid)
        if value == session[provider] and effort == session["efforts"].get(provider, ""):
            return
        self.engine.set_choices(self.sid, {provider: (value, effort)})
        self.refresh()

    def refresh_catalogs(self):
        self.update_status()
        if self.backend_task:
            return
        for provider in self.pickers:
            self.refresh_catalog(provider)

    def refresh_catalog(self, provider):
        catalog = self.catalogs()[provider]
        picker = self.pickers[provider]
        previous = self.catalog_tasks.get(provider)
        if previous:
            previous.cancel()
        picker.loading = catalog.view()["configured"]
        picker.select(picker.model, catalog.view(), picker.effort)
        if not picker.loading:
            return
        async def fetch():
            await catalog.refresh()
            if catalog is self.catalogs()[provider]:
                picker.loading = False
                data = catalog.view()
                model = picker.model
                if not self.sid and data["source"] == "client" and model not in data["models"]:
                    model = next(iter(data["models"]), "")
                    self.settings.setValue(self.model_key(provider), model)
                effort = (self.engine.store.session(self.sid)["efforts"].get(provider, "") if self.sid
                          else self.preferred_efforts(self.backend_box.currentData(), {}, {provider: model})[provider])
                picker.select(model, data, effort)
                if provider == "openai":
                    self.refresh_access()
                self.editor_changed()
                self.update_status()
        task = asyncio.create_task(fetch())
        self.catalog_tasks[provider] = task

    def editor_changed(self):
        ready = self.backend_box.currentData() != "clients" or all(
            p.model in p.catalog.get("models", []) for p in self.pickers.values())
        text = self.editor.toPlainText().strip()
        self.send_button.setEnabled(bool(text) and (ready or text.startswith("/")) and self.backend_task is None)

    def send(self):
        text = self.editor.toPlainText().strip()
        if not text or self.backend_task:
            return
        if text.startswith("/"):
            self.send_command(text)
            return
        if self.backend_box.currentData() == "clients" and any(
                p.model not in p.catalog.get("models", []) for p in self.pickers.values()):
            return
        if len(text) > 120000:
            QMessageBox.information(self, "消息过长", "单条消息上限为 120,000 字符。")
            return
        sid = self.sid
        if not sid:
            models = [self.pickers[provider].model for provider in ("gemini", "openai")]
            sid = self.engine.create_session(*models, project_id=self.pid, standalone=self.pid is None,
                                             backend=self.backend_box.currentData(),
                                             permission=self.draft_permission, speed=self.draft_speed,
                                             efforts={p: picker.effort for p, picker in self.pickers.items()})["id"]
        _, changed = self.engine.submit(sid, text, new_id())
        if changed and self.engine.steering:
            task = asyncio.create_task(self.engine.steer(sid, text))
            self.steering.add(task)
            task.add_done_callback(self.steering.discard)
        self.editor.clear()
        if self.sid:
            self.save_draft()
            self.refresh()
        else:
            self.select_session(sid)

    def refresh(self, force=False):
        if self.config_revision != self.engine.config_revision:
            self.config_revision = self.engine.config_revision
            self.refresh_catalogs()
            force = True
        if force or self.versions != self.engine.changes:
            self.fill_history()
        if force or self.versions.get(self.sid, 0) != self.engine.changes.get(self.sid, 0):
            if self.sid:
                self.state = self.engine.store.snapshot(self.sid)
            else:
                session = {"backend": self.settings.value("backend", "api"), "title": "新会话", "revision": 0, "brief": {}}
                models = self.preferred_models(session["backend"], session)
                session["efforts"] = self.preferred_efforts(session["backend"], session, models)
                session.update(models)
                self.state = dict(session=session, events=[], tasks=[], artifacts=[])
            session = self.state["session"]
            backend = "clients" if self.backend_task else session["backend"]
            changed_backend = self.backend_box.currentData() != backend
            with QSignalBlocker(self.backend_box):
                self.backend_box.setCurrentIndex(self.backend_box.findData(backend))
            project = self.engine.store.project(self.pid) if self.pid else None
            self.project_name.setText(project["name"] if project else ("独立会话" if self.sid else "选择项目"))
            self.project_name.setToolTip("\n".join(f["path"] for f in project["folders"]) if project else "选择项目")
            self.chat_more.setVisible(bool(self.sid))
            self.title.setText(session["title"])
            self.title.setToolTip(session["title"])
            models = self.preferred_models(backend, session) if self.backend_task else session
            efforts = self.preferred_efforts(backend, session, models) if self.backend_task else session["efforts"]
            for provider, picker in self.pickers.items():
                picker.setEnabled(self.backend_task is None)
                effort = efforts.get(provider, "")
                if force or changed_backend or picker.model != models[provider] or picker.effort != effort:
                    catalog = self.catalogs()[provider].view()
                    picker.loading = catalog["loading"]
                    picker.select(models[provider], catalog, effort)
            empty = not self.state["events"]
            self.heading.setText("想在这个项目里做什么？" if project else "今天想做什么？")
            self.heading.setVisible(empty)
            self.timeline.setVisible(not empty)
            self.chat_layout.setStretch(0, int(empty))
            self.chat_layout.setStretch(self.chat_layout.count() - 1, int(empty))
            self.timeline.update_events(self.state["events"], THEMES[self.theme])
            self.fill_inspector()
            self.refresh_access()
            self.update_status()
            self.editor_changed()
            if changed_backend and self.isVisible():
                self.refresh_catalogs()
        self.versions = self.engine.changes.copy()
        drafts = []
        for task in self.state["tasks"]:
            stream = self.engine.previews.get(task["id"])
            saved = task["draft"] if task["status"] in ("waiting", "running", "ready") else ""
            if stream or saved:
                drafts.append(task["title"] + "\n" + (stream or saved))
        text = "\n\n".join(drafts)
        if text != self.preview_text:
            self.preview_text = text
            self.preview.setPlainText(text)
            self.preview.verticalScrollBar().setValue(self.preview.verticalScrollBar().maximum())
            self.preview.setVisible(bool(text))

    def update_status(self):
        tasks = getattr(self, "state", {}).get("tasks", [])
        active = [j for j in tasks if j["status"] in ("needs_key", "running", "ready", "waiting")]
        pending = next((j for j in active if j["status"] == "needs_key"), None)
        running = next((j for j in active if j["status"] == "running"), None)
        self.work_status.setEnabled(self.backend_task is None)
        if self.backend_task:
            self.work_status.setText("正在读取客户端模型…")
        elif self.backend_error:
            self.work_status.setText(self.backend_error + " · 重试")
        elif self.state.get("approvals"):
            request = self.state["approvals"][0]
            self.work_status.setText("待批准：" + request["title"] + " · 查看")
        elif pending:
            provider = "Gemini" if pending["kind"] in ("plan", "report", "section") else "OpenAI"
            self.work_status.setText("等待 " + provider + " API Key · 去配置")
        elif running:
            self.work_status.setText(running["title"] + "…")
        elif active:
            self.work_status.setText("等待前置任务…")
        self.work_status.setVisible(bool(active or self.backend_task or self.backend_error))
        if getattr(self, "state", {}).get("session", {}).get("backend") == "clients":
            errors = [c.error for c in self.engine.client_catalogs.values() if c.error]
            self.connection_state.setText(errors[0] if errors else "")
            self.connection_state.setToolTip("客户端连接状态")
            self.connection_state.setVisible(bool(errors))
            return
        missing = [title + " 未配置" for key, title in (("gemini", "Gemini"), ("openai", "OpenAI")) if not self.engine.keys[key]]
        text = self.engine.config_error or " · ".join(missing)
        self.connection_state.setText(text)
        self.connection_state.setToolTip("打开设置\n" + str(self.engine.project / ".env"))
        self.connection_state.setVisible(bool(text))

    def status_action(self):
        if self.backend_error:
            self.backend_box.setCurrentIndex(self.backend_box.findData("clients"))
        elif self.state.get("approvals"):
            ApprovalDialog(self, self.engine, self.sid, self.state["approvals"][0]).open()
        elif any(j["status"] == "needs_key" for j in self.state["tasks"]):
            self.connections()
        else:
            self.inspector.show()
            self.tabs.setCurrentWidget(self.tasks)

    def fill_inspector(self):
        for widget, key in ((self.tasks, "tasks"), (self.assets, "artifacts")):
            current = widget.currentItem()
            selected = current.data(USER_ROLE) if current else None
            with QSignalBlocker(widget):
                widget.clear()
                for entry in reversed(self.state[key]):
                    if key == "tasks":
                        title = entry["title"] + "\n" + STATUSES[entry["status"]] + " · " + entry["model"]
                        if entry["effort"]:
                            title += " · " + LEVEL_NAMES.get(entry["effort"], entry["effort"])
                    else:
                        title = entry["name"]
                    item = QListWidgetItem(title)
                    item.setData(USER_ROLE, entry["id"])
                    item.setToolTip(title)
                    widget.addItem(item)
                    if entry["id"] == selected:
                        widget.setCurrentItem(item)
                if widget.count() == 0:
                    item = QListWidgetItem("暂无任务" if key == "tasks" else "暂无成果")
                    item.setFlags(Qt.ItemFlag.NoItemFlags)
                    widget.addItem(item)
                elif not widget.currentItem():
                    widget.setCurrentRow(0)
        brief = self.state["session"]["brief"]
        self.brief.setPlainText("\n\n".join(LABELS[k] + "\n" + v for k, v in brief.items() if v) or "暂无需求记录")
        self.inspector_action()

    def inspector_action(self):
        page = self.tabs.currentWidget()
        self.detail_button.setVisible(page in (self.tasks, self.assets))
        current = page.currentItem() if page in (self.tasks, self.assets) else None
        self.detail_button.setEnabled(bool(current and current.data(USER_ROLE)))

    def open_detail(self):
        if self.tabs.currentWidget() == self.tasks:
            self.task_detail(self.tasks.currentItem())
        else:
            self.artifact_detail(self.assets.currentItem())

    def task_detail(self, item):
        jid = item.data(USER_ROLE)
        sid = self.sid
        task = self.engine.store.job(jid)
        dialog = QDialog(self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle(task["title"])
        dialog.resize(520, 340)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label(STATUSES[task["status"]] + " · " + task["model"], "section"))
        detail = Document()
        detail.setPlainText("\n\n".join(str(v) for v in task["payload"].values()) + "\n\n" +
                            (task["error"] or self.engine.previews.get(jid) or task["draft"]))
        layout.addWidget(detail, 1)
        actions = QHBoxLayout()
        actions.addStretch()
        if task["status"] in ("failed", "needs_key"):
            def retry():
                try:
                    self.engine.retry(sid, jid)
                except ValueError as exc:
                    QMessageBox.information(dialog, "任务状态", str(exc))
                dialog.accept()
                self.refresh()
            actions.addWidget(button("重试", retry))
        actions.addWidget(button("关闭", dialog.reject))
        layout.addLayout(actions)
        dialog.open()

    def artifact_detail(self, item):
        asset = self.engine.store.one("SELECT * FROM artifacts WHERE id=? AND sid=?", item.data(USER_ROLE), self.sid)
        dialog = ArtifactDialog(self, asset)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.open()

    def connections(self):
        dialog = SettingsDialog(self, self.engine)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.saved.connect(self.refresh_catalogs)
        dialog.open()

    def agent_settings(self, checked=False):
        AgentDialog(self, self.engine, self.sid, self.pid).open()

    def refresh_access(self):
        session = self.engine.store.session(self.sid) if self.sid else {}
        permission = session.get("permission", self.draft_permission)
        with QSignalBlocker(self.permission_box):
            self.permission_box.setCurrentIndex(self.permission_box.findData(permission))
        catalog = self.engine.client_catalogs["openai"]
        model = self.pickers["openai"].model
        speeds = available_speeds(group_for(catalog.groups, model)) if self.backend_box.currentData() == "clients" else {}
        speed = session.get("speed", self.draft_speed)
        if self.backend_task is None and catalog.groups and not catalog.error and speed != "standard" and speed not in speeds:
            speed = self.draft_speed = "standard"
            if self.sid:
                self.engine.set_access(self.sid, speed=speed)
        with QSignalBlocker(self.speed_box):
            self.speed_box.clear()
            for key in ("standard", *speeds):
                self.speed_box.addItem(SPEEDS[key], key)
            self.speed_box.setCurrentIndex(self.speed_box.findData(speed))
        self.speed_box.setVisible(bool(speeds) and self.backend_task is None)

    def change_permission(self):
        mode = self.permission_box.currentData()
        if self.sid:
            self.engine.set_access(self.sid, permission=mode)
        else:
            self.draft_permission = mode

    def change_speed(self):
        speed = self.speed_box.currentData()
        if self.sid:
            self.engine.set_access(self.sid, speed=speed)
        else:
            self.draft_speed = speed

    def send_command(self, text):
        sid, pid = self.sid, self.pid
        self.editor.clear()
        self.save_draft()
        if text in ("/settings", "/config"):
            self.agent_settings()
            return
        options = dict(line=text, id=sid, project_id=pid, cwd=str(self.engine.skill_folders(sid, pid)[0]),
                       backend=self.backend_box.currentData(), permission=self.permission_box.currentData(),
                       speed=self.speed_box.currentData() or "standard", standalone=pid is None)
        for p, picker in self.pickers.items():
            options[p] = picker.model
            options[p + "_effort"] = picker.effort

        async def run():
            try:
                result = await execute_command(self.engine, options)
            except (ValueError, RuntimeError, OSError, KeyError) as exc:
                if (sid, pid) == (self.sid, self.pid) and not self.editor.toPlainText():
                    self.editor.setPlainText(text)
                self.command_detail("指令未完成", str(exc))
                return
            if (sid, pid) == (self.sid, self.pid) and result["id"] and result["id"] != self.sid:
                self.select_session(result["id"])
            if result.get("exit"):
                self.close()
                return
            self.refresh(force=True)
            if result["text"]:
                self.command_detail(text.split()[0], result["text"])
        self.run_task(run())

    def command_detail(self, title, text):
        dialog = QDialog(self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle(title)
        dialog.resize(540, 390)
        layout = QVBoxLayout(dialog)
        content = Document()
        content.setPlainText(text)
        layout.addWidget(content)
        layout.addWidget(button("关闭", dialog.reject), 0, ALIGN.AlignRight)
        dialog.open()

    def show_usage(self, provider):
        UsageDialog(self, CLIENTS[provider], self.pickers[provider].dialog).open()

    def toggle_sidebar(self):
        show = self.sidebar.isHidden()
        if show and self.width() < 930:
            self.inspector.hide()
        self.sidebar.setVisible(show)
        self.expand_side.setVisible(not show)

    def toggle_inspector(self):
        show = self.inspector.isHidden()
        if show and self.width() < 930:
            self.sidebar.hide()
            self.expand_side.show()
        self.inspector.setVisible(show)

    def set_theme(self, name):
        self.theme = name if name in THEMES else "light"
        apply_theme(QApplication.instance(), self.theme)
        self.theme_button.setIcon(icon("sun" if self.theme == "dark" else "moon"))
        self.theme_button.setToolTip("切换为浅色主题" if self.theme == "dark" else "切换为深色主题")
        if hasattr(self, "state"):
            self.timeline.reset()
            self.refresh(force=True)

    def toggle_theme(self):
        self.set_theme("dark" if self.theme == "light" else "light")
        self.settings.setValue("theme", self.theme)

    def closeEvent(self, event):
        self.timer.stop()
        self.cancel_backend_change()
        self.save_draft()
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.sync()
        super().closeEvent(event)
        self.closed.emit()
