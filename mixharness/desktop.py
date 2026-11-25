import asyncio
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QProcess, QSignalBlocker, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QDesktopServices, QIcon, QKeySequence, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QBoxLayout, QComboBox, QDialog, QFileDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QHeaderView, QInputDialog,
    QFormLayout, QLineEdit, QListWidget, QListWidgetItem, QProgressBar,
    QMainWindow, QMenu, QMessageBox, QSizePolicy, QSplitter, QTabWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QWidgetAction,
)

from .desktop_projects import ArchiveDialog, ProjectDialog
from .desktop_chrome import TitleBar, hide_macos_title
from .desktop_agent import AgentDialog, ApprovalDialog
from .desktop_slash import SlashMenu
from .desktop_chat_actions import McpDialog, ShareDialog, SideChat, save_transcript
from .chat_snapshot import feedback_url, transcript
from .agent_settings import PERMISSIONS, SPEEDS, available_speeds
from .slash import PROVIDER_NAMES, execute_command, goal_text, split
from .slash_catalog import COMMANDS, command_name
from .desktop_clients import ClientHistory
from .desktop_inputs import ComposerInputs
from .desktop_usage import UsageDialog
from .usage import API_DASHBOARDS
from .desktop_style import RESOURCES, THEMES, apply_theme
from .desktop_widgets import (
    ALIGN, ArtifactDialog, Document, Editor, FitCombo, HistoryTree, menu_icon, MessageNav, ModelPicker, SettingsDialog, Submenu, Timeline, button, icon, label,
)
from .providers import MODELS
from .clients import CLIENTS, is_dot_model
from .reasoning import LEVEL_NAMES, group_for, model_available, resolve_choice, select_daybreak_model
from .store import ACTIVE_STATUSES, CHAT_JOBS, GOAL_STATES, LABELS, MODES, MODE_TITLES, PROVIDERS, STATUSES, accepts_guidance, new_id


USER_ROLE = Qt.ItemDataRole.UserRole
RECIPIENTS = {"": "双模型协作", "gemini": "Gemini", "openai": "GPT"}


class Window(QMainWindow):
    closed = Signal()

    def __init__(self, engine, settings):
        super().__init__()
        self.engine, self.settings = engine, settings
        self.sid = None
        self.pid = None
        self.recipient = ""
        self.locations = []
        self.location_index = -1
        self.catalog_tasks = {}
        self.backend_task = None
        self.backend_error = ""
        self.draft_permission, self.draft_speed = "ask", "standard"
        self.draft_daybreak = False
        self.steering = set()
        self.versions = {}
        self.preview_text = ""
        self.history_key = None
        self.inspector_rows = {}
        self.config_revision = engine.config_revision
        self.setWindowTitle("MixHarness")
        self.setWindowIcon(QIcon(str(RESOURCES / "app.png")))
        self.unified_titlebar = QApplication.platformName() == "cocoa" and hasattr(Qt.WindowType, "ExpandedClientAreaHint")
        if self.unified_titlebar:
            self.setWindowFlag(Qt.WindowType.ExpandedClientAreaHint)
            self.setWindowFlag(Qt.WindowType.NoTitleBarBackgroundHint)
            self.setAttribute(Qt.WidgetAttribute.WA_ContentsMarginsRespectsSafeArea, False)
        self.resize(1140, 740)
        self.setMinimumSize(700, 490)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setObjectName("panes")
        if self.unified_titlebar:
            self.splitter.setAttribute(Qt.WidgetAttribute.WA_ContentsMarginsRespectsSafeArea, False)
        self.splitter.setHandleWidth(1)
        self.splitter.setChildrenCollapsible(False)
        self.setCentralWidget(self.splitter)
        self.make_sidebar()
        self.make_chat()
        self.make_inspector()
        self.splitter.setStretchFactor(1, 1)
        # 旧版保存的是单列侧栏宽度，新版还需要容纳左侧图标栏。
        old_width = int(settings.value("sidebar_width", 290))
        side_width = int(settings.value("navigation_width", max(290, old_width)))
        self.splitter.setSizes([side_width, self.width() - side_width - 1, int(settings.value("inspector_width", 280))])
        self.make_menus()
        system_dark = QApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
        self.set_theme(str(settings.value("theme", "dark" if system_dark else "light")))
        self.saved_geometry = settings.value("geometry")
        if self.saved_geometry:
            self.restoreGeometry(self.saved_geometry)
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
        self.sidebar.setMinimumWidth(258)
        self.sidebar.setMaximumWidth(520)
        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(0)
        brand = QHBoxLayout()
        brand.setContentsMargins(89 if self.unified_titlebar else 10, 0, 7, 0)
        brand.setSpacing(4)
        brand.addWidget(button("收起侧栏", self.toggle_sidebar, "sidebar", icon_only=True))
        brand.addStretch()
        self.go_back = button("后退", lambda: self.navigate(-1), "chevron-left", icon_only=True)
        self.go_forward = button("前进", lambda: self.navigate(1), "chevron-right", icon_only=True)
        brand.addWidget(self.go_back)
        brand.addWidget(self.go_forward)
        titlebar = TitleBar()
        titlebar.setLayout(brand)
        sidebar_layout.addWidget(titlebar)
        panes = QHBoxLayout()
        panes.setContentsMargins(0, 4, 0, 0)
        panes.setSpacing(0)
        sidebar_layout.addLayout(panes, 1)
        self.rail = QFrame()
        self.rail.setObjectName("rail")
        self.rail.setFixedWidth(52)
        rail = QVBoxLayout(self.rail)
        rail.setContentsMargins(6, 1, 6, 9)
        rail.setSpacing(8)
        def rail_button(title, glyph, callback, name="railAction"):
            control = button(title, callback, glyph, name, True)
            control.setFixedSize(36, 36)
            control.setIconSize(QSize(18, 18))
            rail.addWidget(control, 0, ALIGN.AlignHCenter)
            return control
        rail_button("工作台", "home", lambda: self.sources_box.setCurrentIndex(0), "railHome")
        rail_button("打开项目", "files", self.choose_project)
        rail_button("任务与成果", "clipboard", self.toggle_inspector)
        rail_button("已归档的会话", "history", self.show_archive)
        rail_button("更多操作", "more", self.rail_menu)
        divider = QFrame()
        divider.setObjectName("railRule")
        divider.setFixedSize(24, 1)
        rail.addWidget(divider, 0, ALIGN.AlignHCenter)
        self.create_project_button = rail_button("新建项目", "fork", self.project_dialog)
        rail_button("技能与上下文", "grid", self.agent_settings)
        rail.addStretch(1)
        self.theme_button = rail_button("切换主题", "moon", self.toggle_theme)
        rail_button("连接设置", "account", self.connections, "account")
        panes.addWidget(self.rail)
        col = QVBoxLayout()
        col.setContentsMargins(8, 1, 8, 8)
        col.setSpacing(1)
        panes.addLayout(col, 1)
        source_row = QHBoxLayout()
        source_row.setContentsMargins(3, 1, 0, 8)
        source_row.setSpacing(2)
        self.sources_box = QComboBox()
        self.sources_box.setObjectName("workspaceName")
        self.sources_box.setAccessibleName("会话来源")
        self.sources_box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        for title, value in (("MixHarness", "mixharness"), ("全部来源", "all"),
                             ("Codex", "codex"), ("Antigravity", "antigravity")):
            self.sources_box.addItem(title, value)
        self.sources_box.setToolTip("选择会话来源")
        source_row.addWidget(self.sources_box)
        source_row.addStretch()
        self.filter_button = button("筛选项目与会话", self.history_filter_menu, "filter", icon_only=True)
        source_row.addWidget(self.filter_button)
        source_row.addWidget(button("搜索聊天", self.search, "search", icon_only=True))
        col.addLayout(source_row)
        col.addWidget(button("新聊天", self.new_session, "new", "nav"))
        self.dot_chat = button("GPT-dot", self.open_dot_chat, "openai", "nav")
        self.dot_chat.setToolTip("在当前聊天中直接给 GPT-dot 发消息")
        col.addWidget(self.dot_chat)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("搜索项目或会话")
        self.filter.setClearButtonEnabled(True)
        self.filter_timer = QTimer(self)
        self.filter_timer.setSingleShot(True)
        self.filter_timer.setInterval(220)
        self.filter_timer.timeout.connect(self.select_source)
        self.filter.textChanged.connect(self.search_changed)
        col.addWidget(self.filter)
        self.filter.hide()
        col.addSpacing(14)
        self.history_filter = "all"
        self.history = HistoryTree()
        self.history.setAccessibleName("项目与会话")
        self.history.setColumnCount(2)
        # 第二列仅承载操作按钮。辅助功能选择和键盘焦点都跟随标题单元格。
        self.history.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.history.header().setStretchLastSection(False)
        self.history.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.history.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.history.setColumnWidth(1, 24)
        self.history.setHeaderHidden(True)
        self.history.setIndentation(13)
        self.history.setRootIsDecorated(False)
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
        self.connection_state = button("", self.connections, name="quiet")
        col.addWidget(self.connection_state)
        self.splitter.addWidget(self.sidebar)

    def rail_menu(self):
        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.addAction(menu_icon("usage"), "查看套餐用量", lambda: self.show_usage("openai"))
        menu.addAction(menu_icon("archive"), "归档管理", self.show_archive)
        menu.addAction(menu_icon("settings"), "连接设置", self.connections)
        control = self.sender()
        menu.popup(control.mapToGlobal(control.rect().topRight()))

    def history_filter_menu(self):
        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        for key, title in (("all", "全部"), ("pinned", "仅置顶"), ("working", "任务进行中")):
            action = menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(self.history_filter == key)
            action.triggered.connect(lambda _, value=key: self.set_history_filter(value))
        menu.popup(self.filter_button.mapToGlobal(self.filter_button.rect().bottomLeft()))

    def set_history_filter(self, value):
        self.history_filter = value
        self.sources_box.setCurrentIndex(0)
        self.fill_history()

    def make_chat(self):
        main = QWidget()
        main.setObjectName("content")
        body = QVBoxLayout(main)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        header = QHBoxLayout()
        header.setContentsMargins(8, 0, 8, 0)
        header.setSpacing(4)
        self.expand_side = button("展开侧栏", self.toggle_sidebar, "sidebar", icon_only=True)
        self.expand_side.hide()
        header.addWidget(self.expand_side)
        self.project_name = button("选择项目", self.project_picker, "folder", icon_only=True)
        self.title = label("新会话", "title")
        self.title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.title.installEventFilter(self)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        header.addWidget(self.project_name)
        header.addWidget(self.title, 1)
        self.chat_more = button("会话操作", self.current_chat_menu, "more", icon_only=True)
        header.addWidget(self.chat_more)
        self.share_button = button("分享聊天", lambda: ShareDialog(self, self.sid).open(), "share", icon_only=True)
        header.addWidget(self.share_button)
        self.options_button = button("会话设置", self.show_options, "sliders", icon_only=True)
        header.addWidget(self.options_button)
        self.inspect_button = button("任务与成果", self.toggle_inspector, "inspector", icon_only=True)
        header.addWidget(self.inspect_button)
        self.header = TitleBar()
        self.header.setLayout(header)
        body.addWidget(self.header)
        shell = QHBoxLayout()
        shell.setContentsMargins(20, 0, 20, 16)
        shell.setSpacing(0)
        view = QWidget()
        view.setMaximumWidth(736)
        view.setMinimumWidth(0)
        view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.chat_layout = QVBoxLayout(view)
        col = self.chat_layout
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(6)
        col.addStretch(1)
        self.heading = label("", "heading")
        self.heading.setAlignment(ALIGN.AlignCenter)
        col.addWidget(self.heading)
        self.heading_gap = QWidget()
        self.heading_gap.setFixedHeight(14)
        col.addWidget(self.heading_gap)
        self.timeline = Timeline(self.engine.store.inputs, self.retry_from_timeline)
        col.addWidget(self.timeline, 1)
        self.preview = Document()
        self.preview.setAccessibleName("正在生成的内容")
        self.preview.setMaximumHeight(110)
        col.addWidget(self.preview)
        self.preview.hide()
        self.activity_row = QWidget()
        activity = QHBoxLayout(self.activity_row)
        activity.setContentsMargins(7, 0, 4, 0)
        activity.setSpacing(6)
        self.work_progress = QProgressBar()
        self.work_progress.setRange(0, 0)
        self.work_progress.setTextVisible(False)
        self.work_progress.setFixedSize(28, 5)
        self.work_progress.setAccessibleName("任务进行中")
        activity.addWidget(self.work_progress)
        self.work_status = button("", self.status_action, name="workStatus")
        self.work_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        activity.addWidget(self.work_status, 1)
        self.stop_button = button("停止", self.stop_work, name="stopWork")
        self.stop_button.setAccessibleName("停止当前会话任务")
        self.stop_button.setToolTip("停止当前会话任务（输入框内按 Esc）")
        self.continue_button = button("继续", self.continue_work, name="stopWork")
        self.continue_button.setAccessibleName("继续上次停止的任务")
        self.continue_button.setToolTip("从上次停止处继续")
        activity.addWidget(self.continue_button)
        col.addWidget(self.activity_row)
        self.composer = QFrame()
        self.composer.setObjectName("composer")
        self.composer_shadow = QGraphicsDropShadowEffect(self.composer)
        self.composer_shadow.setBlurRadius(12)
        self.composer_shadow.setOffset(0, 2)
        self.composer.setGraphicsEffect(self.composer_shadow)
        controls = QVBoxLayout(self.composer)
        controls.setContentsMargins(12, 12, 9, 8)
        controls.setSpacing(6)
        self.editor = Editor()
        self.editor.submitted.connect(self.send)
        self.editor.interrupted.connect(self.stop_work)
        self.editor.textChanged.connect(self.editor_changed)
        self.editor.focus_changed.connect(self.composer_focus)
        self.inputs = ComposerInputs(self)
        self.inputs.changed.connect(self.editor_changed)
        self.editor.image_files.connect(self.inputs.add_files)
        self.editor.image_bytes.connect(self.inputs.paste_image)
        controls.addWidget(self.inputs)
        controls.addWidget(self.editor)
        self.actions_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.actions_layout.setSpacing(2)
        left = QHBoxLayout()
        left.setSpacing(2)
        self.add_input = button("添加附件或引用", self.inputs.menu, "plus", "round", icon_only=True)
        self.add_input.setFixedSize(28, 28)
        left.addWidget(self.add_input)
        self.permission_box = FitCombo()
        self.permission_box.setObjectName("permission")
        self.permission_box.setAccessibleName("操作权限")
        self.permission_box.setToolTip("操作权限")
        for mode, title in PERMISSIONS.items():
            self.permission_box.addItem(title, mode)
        self.permission_box.currentIndexChanged.connect(self.change_permission)
        left.addWidget(self.permission_box)
        self.recipient_button = button("双模型协作", self.recipient_menu, name="recipient")
        left.addWidget(self.recipient_button)
        left.addStretch()
        self.actions_layout.addLayout(left)
        tools = QHBoxLayout()
        tools.setSpacing(2)
        tools.addStretch()
        self.actions_layout.addLayout(tools)
        self.daybreak_action = QAction(self)
        self.daybreak_action.setCheckable(True)
        self.daybreak_action.triggered.connect(self.change_daybreak)
        self.speed_icons = (icon("zap", role=QPalette.ColorRole.PlaceholderText), icon("zap", role=QPalette.ColorRole.Link))
        self.speed_box = button("GPT Fast 速度", self.toggle_fast, name="speed", icon_only=True)
        self.speed_box.setCheckable(True)
        self.speed_box.setFixedSize(26, 26)
        self.speed_box.setIconSize(QSize(16, 16))
        self.pickers = {}
        for provider, title in (("gemini", "Gemini"), ("openai", "OpenAI")):
            pick = ModelPicker(provider, title, self.daybreak_action if provider == "openai" else None)
            if provider == "openai":
                tools.addWidget(self.speed_box)
            pick.changed.connect(self.choose_model)
            pick.refresh_requested.connect(self.refresh_catalog)
            pick.usage_requested.connect(self.show_usage)
            self.pickers[provider] = pick
            tools.addWidget(pick)
        self.compact_models = button("模型", self.compact_model_menu, name="recipient")
        self.compact_models.setAccessibleName("选择模型")
        tools.addWidget(self.compact_models)
        self.compact_efforts = button("强度", lambda: self.compact_model_menu(effort=True), name="recipient")
        self.compact_efforts.setAccessibleName("调整思考强度")
        tools.addWidget(self.compact_efforts)
        self.backend_box = FitCombo()
        self.backend_box.setObjectName("flat")
        self.backend_box.setAccessibleName("会话执行方式")
        self.backend_box.setToolTip("执行方式")
        self.backend_box.setCursor(Qt.CursorShape.PointingHandCursor)
        self.backend_box.addItem("API", "api")
        self.backend_box.addItem("本机客户端", "clients")
        self.backend_box.currentIndexChanged.connect(self.change_backend)
        tools.addSpacing(4)
        self.stop_button.setObjectName("stopRound")
        self.stop_button.setText("")
        self.stop_button.setIcon(icon("stop", inverse=True))
        self.stop_button.setIconSize(QSize(14, 14))
        self.stop_button.setFixedSize(28, 28)
        tools.addWidget(self.stop_button)
        self.send_button = button("发送消息", self.send, "send", "send", icon_only=True)
        self.send_button.setFixedSize(28, 28)
        self.send_button.setEnabled(False)
        tools.addWidget(self.send_button)
        controls.addLayout(self.actions_layout)
        col.addWidget(self.composer)
        self.options_menu = QMenu(self)
        self.options_menu.setAccessibleName("会话设置")
        panel = QWidget()
        form = QFormLayout(panel)
        form.setContentsMargins(12, 10, 12, 10)
        form.setSpacing(8)
        self.mode_button = button("会话模式", self.mode_menu, name="quiet")
        self.mode_caption = label("会话模式", "caption")
        form.addRow(self.mode_caption, self.mode_button)
        form.addRow(label("执行方式", "caption"), self.backend_box)
        panel_action = QWidgetAction(self.options_menu)
        panel_action.setDefaultWidget(panel)
        self.options_menu.addAction(panel_action)
        col.addStretch(1)
        shell.addWidget(view)
        body.addLayout(shell, 1)
        self.content = main
        self.message_nav = MessageNav(self.timeline, main)
        self.slash_menu = SlashMenu(self)
        self.slash_menu.chosen.connect(lambda name: self.send_command("/" + name))
        main.installEventFilter(self)
        self.composer.installEventFilter(self)
        self.splitter.addWidget(main)

    def show_options(self):
        anchor = self.options_button
        self.options_menu.popup(anchor.mapToGlobal(anchor.rect().bottomRight()) - QPoint(self.options_menu.sizeHint().width(), 0))

    def show_backend(self):
        self.show_options()
        QTimer.singleShot(0, self.backend_box, self.backend_box.showPopup)

    def make_inspector(self):
        self.inspector = QFrame()
        self.inspector.setObjectName("inspector")
        self.inspector.setMinimumWidth(240)
        self.inspector.setMaximumWidth(520)
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
        self.splitter.addWidget(self.inspector)
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
        if item.data(0, USER_ROLE + 1) == "project" and not self.filter.text().strip():
            self.settings.setValue("collapsed/" + (item.data(0, USER_ROLE) or "local"), not expanded)
            self.select_history_row()

    def fill_history(self):
        query = self.filter.text().strip()
        groups = self.engine.store.projects()
        chats = self.session_list(query)
        signature = (query, self.history_filter, groups, [(s["id"], s["title"], s["project_id"], s["pinned"], s["working"], s["updated"]) for s in chats])
        if signature == self.history_key:
            self.select_history_row()
            return
        self.history_key = signature
        scroll = self.history.verticalScrollBar().value()
        with QSignalBlocker(self.history):
            self.history.selectionModel().clear()
            self.history.clear_actions()
            self.history_rows = {}
            self.history.clear()
            def section(title):
                item = QTreeWidgetItem(self.history)
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                item.setFirstColumnSpanned(True)
                item.setSizeHint(0, QSize(0, 37))
                heading = label(title, "historySection")
                self.history.setItemWidget(item, 0, heading)

            def chat_row(parent, session):
                item = QTreeWidgetItem(parent, [session["title"]])
                item.setToolTip(0, session["title"] + (" · 任务进行中" if session["working"] else ""))
                item.setData(0, USER_ROLE, session["id"])
                item.setData(0, USER_ROLE + 1, "chat")
                self.history_rows[("chat", session["id"])] = item
                self.add_row_actions(item, session["id"], project=False, working=session["working"])

            def project_row(project, members):
                pid, title = project["id"], project["name"]
                group = QTreeWidgetItem(self.history, [title])
                group.setIcon(0, icon("folder-open"))
                group.setToolTip(0, "\n".join(f["path"] for f in project["folders"]) or title)
                group.setData(0, USER_ROLE, pid)
                group.setData(0, USER_ROLE + 1, "project")
                self.history_rows[("project", pid)] = group
                self.add_row_actions(group, pid, project=True)
                for session in members:
                    chat_row(group, session)
                collapsed = self.settings.value("collapsed/" + (pid or "local"), False, type=bool)
                group.setExpanded(bool(query) or not collapsed)

            pinned_projects = {p["id"] for p in groups if p["pinned"]}
            members = {}
            pins = []
            for chat in chats:
                if self.history_filter == "working" and not chat["working"]:
                    continue
                if chat["pinned"] and chat["project_id"] not in pinned_projects:
                    pins.append(chat)
                else:
                    members.setdefault(chat["project_id"], []).append(chat)
            projects = [p for p in groups if members.get(p["id"]) or
                        (self.history_filter != "working" and query.casefold() in p["name"].casefold())]
            for pinned, title in ((True, "置顶"), (False, "项目")):
                matching = [p for p in projects if bool(p["pinned"]) == pinned]
                if not pinned and self.history_filter == "pinned":
                    break
                if matching or (pinned and pins):
                    section(title)
                for project in matching:
                    project_row(project, members.get(project["id"], []))
                if pinned:
                    for chat in pins:
                        chat_row(self.history, chat)
            if self.history_filter != "pinned" and members.get(None):
                section("最近")
                for chat in members[None]:
                    chat_row(self.history, chat)
            self.history.doItemsLayout()
            if self.history.viewport().underMouse():
                hovered = self.history.itemAt(self.history.viewport().mapFromGlobal(QCursor.pos()))
                if hovered:
                    self.history.hover_item(hovered, 0)
            self.select_history_row()
        self.history.verticalScrollBar().setValue(scroll)

    def select_history_row(self):
        key = ("chat", self.sid) if self.sid else ("project", self.pid)
        item = self.history_rows.get(key)
        if item is not None and item.parent() is not None and not item.parent().isExpanded():
            item = item.parent()
        if item != self.history.currentItem():
            with QSignalBlocker(self.history):
                self.history.setCurrentItem(item, 0)
        self.history.update_actions()

    def add_row_actions(self, item, identifier, *, project, working=False):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addStretch()
        if working:
            progress = QProgressBar()
            progress.setRange(0, 0)
            progress.setFixedSize(16, 4)
            progress.setTextVisible(False)
            progress.setAccessibleName("会话任务进行中")
            progress.setToolTip("会话任务进行中")
            layout.addWidget(progress)
        action = button("项目操作" if project else "会话操作", lambda: self.show_row_menu(
            identifier, project, action.mapToGlobal(action.rect().bottomLeft())), "more", "rowAction", True)
        action.setFixedSize(22, 24)
        action.setAccessibleName(item.text(0) + "，" + action.accessibleName())
        layout.addWidget(action)
        self.history.add_trailing_action(item, progress if working else None, action)
        self.history.setItemWidget(item, 1, row)

    def history_changed(self, item, previous):
        if not item or not item.data(0, USER_ROLE + 1):
            return
        value = item.data(0, USER_ROLE)
        if item.data(0, USER_ROLE + 1) == "project":
            if value != self.pid:
                self.activate_project(value)
        elif value != self.sid:
            self.select_session(value)

    def search(self):
        self.sidebar.show()
        self.expand_side.hide()
        self.filter.show()
        self.filter.setFocus()
        self.filter.selectAll()

    def search_changed(self):
        if self.sources_box.currentData() == "mixharness":
            self.fill_history()
        else:
            self.filter_timer.start()

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
        self.filter_timer.stop()
        source = self.sources_box.currentData()
        local = source == "mixharness"
        self.history.setVisible(local)
        self.client_history.setVisible(not local)
        self.client_history.query = self.filter.text().strip()
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
                catalog = self.engine.client_catalogs[provider].view()
                available = catalog["models"]
                if provider == "openai":
                    enabled = session.get("daybreak", self.draft_daybreak) if session.get("backend", backend) == backend else False
                    available = [g["id"] for g in catalog["groups"] if model_available(g, enabled)]
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
            if session.get("backend") == backend and session.get(p) == model:
                effort = session.get("efforts", {}).get(p)
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
            self.draft_daybreak = False
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
            self.engine.switch_backend(sid, backend, models, self.preferred_efforts(backend, session, models), recipient=self.recipient)
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
            action = menu.addAction(project["name"], lambda checked=False, pid=project["id"]: self.choose_context(pid))
            action.setCheckable(True)
            action.setChecked(self.pid == project["id"])
        alone = menu.addAction("独立会话", lambda: self.choose_context(None))
        alone.setCheckable(True)
        alone.setChecked(self.pid is None)
        menu.addSeparator()
        menu.addAction("新建项目…", self.project_dialog)
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
        if item and item.data(0, USER_ROLE + 1):
            self.show_row_menu(item.data(0, USER_ROLE), item.data(0, USER_ROLE + 1) == "project",
                               self.history.viewport().mapToGlobal(point))

    def current_chat_menu(self):
        if self.sid:
            self.show_row_menu(self.sid, False, self.chat_more.mapToGlobal(self.chat_more.rect().bottomLeft()))

    def show_row_menu(self, identifier, project_row, position):
        menu = QMenu(self.history)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        if not project_row:
            chat = self.engine.store.session(identifier)
            menu.addAction(menu_icon("pin"), "取消置顶" if chat["pinned"] else "置顶会话",
                           lambda: self.pin_row(identifier, False, not chat["pinned"]))
            menu.addAction(menu_icon("pencil"), "重命名会话…", lambda: self.rename_chat(identifier))
            menu.addAction(menu_icon("archive"), "归档会话", lambda: self.archive_chat(identifier))
            menu.addSeparator()
            menu.addAction(menu_icon("trash"), "删除会话…", lambda: self.confirm_delete(identifier))
        else:
            menu.addAction(menu_icon("new"), "在此新建会话", lambda: self.create_chat(identifier))
        if project_row and identifier:
            pid = identifier
            project = self.engine.store.project(pid)
            menu.addAction(menu_icon("pin"), "取消置顶" if project["pinned"] else "置顶项目",
                           lambda: self.pin_row(pid, True, not project["pinned"]))
            menu.addAction(menu_icon("settings"), "编辑项目…", lambda: self.project_dialog(pid=pid))
            folders = Submenu("在文件管理器中打开", menu)
            folders.setIcon(menu_icon("folder"))
            for folder in project["folders"]:
                path = folder["path"]
                folders.addAction(path, lambda checked=False, path=path: QDesktopServices.openUrl(QUrl.fromLocalFile(path)))
            menu.addSeparator()
            menu.addAction(menu_icon("close"), "移出侧栏", lambda: self.hide_project(pid))
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
        self.engine.notify(sid)
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
        location = pid, sid
        if self.location_index < 0 or self.locations[self.location_index] != location:
            self.locations[self.location_index + 1:] = [location]
            self.location_index += 1
        self.go_back.setEnabled(self.location_index > 0)
        self.go_forward.setEnabled(self.location_index < len(self.locations) - 1)
        self.recipient = self.settings.value("recipient/" + self.draft_key(), "")
        if self.recipient not in RECIPIENTS:
            self.recipient = ""
        self.draft_permission, self.draft_speed = "ask", "standard"
        self.draft_daybreak = False
        self.draft_mode = self.settings.value("mode/" + self.draft_key(), "default")
        self.settings.setValue("project", pid or "")
        self.settings.setValue("session", sid or "")
        self.settings.setValue("recent/" + (pid or "local"), sid or "")
        self.timeline.reset()
        self.editor.setPlainText(str(self.settings.value(self.draft_key(), "")))
        self.inputs.restore()
        self.refresh(force=True)
        self.editor.setFocus()

    def navigate(self, step):
        sessions = {s["id"] for s in self.session_list()}
        projects = {p["id"] for p in self.engine.store.projects()} | {None}
        index = self.location_index + step
        while 0 <= index < len(self.locations):
            pid, sid = self.locations[index]
            if pid in projects and (sid is None or sid in sessions):
                self.location_index = index
                self.open_view(pid, sid)
                return
            index += step
        (self.go_back if step < 0 else self.go_forward).setEnabled(False)

    def recipient_menu(self):
        menu = QMenu(self)
        menu.setAccessibleName("消息接收方")
        for key, title in RECIPIENTS.items():
            action = menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(key == self.recipient)
            action.triggered.connect(lambda _, value=key: self.select_recipient(value))
        self.popup_choices(self.recipient_button, menu)

    def select_recipient(self, value):
        self.recipient = value
        self.settings.setValue("recipient/" + self.draft_key(), value)
        self.refresh(force=True)
        self.editor.setFocus()

    def recipient_name(self):
        if (self.recipient == "openai" and self.backend_box.currentData() == "clients"
                and is_dot_model(self.pickers["openai"].model)):
            return "GPT-dot"
        return RECIPIENTS[self.recipient]

    def open_dot_chat(self):
        self.cancel_backend_change()
        sid = self.sid
        session = self.engine.store.session(sid) if sid else {}
        switching = session.get("backend", self.settings.value("backend", "api")) != "clients"
        codex = self.engine.client_catalogs["openai"]
        gemini = self.engine.client_catalogs["gemini"]

        async def activate():
            try:
                catalogs = [] if any(is_dot_model(m) for m in codex.labels) else [codex]
                if switching and sid and not gemini.groups and any(
                    j["kind"] != CHAT_JOBS["openai"] for j in self.engine.store.active_jobs(sid)
                ):
                    catalogs.append(gemini)
                await asyncio.gather(*(catalog.refresh() for catalog in catalogs))
                if self.backend_task is not asyncio.current_task():
                    return
                daybreak = not switching and session.get("daybreak", self.draft_daybreak)
                choices = {g["id"]: g["name"] for g in codex.groups
                           if is_dot_model(g["id"]) and model_available(g, daybreak)}
                if not choices:
                    hint = ("当前 Daybreak 模式没有可用的 GPT-dot 模型。可在 GPT 模型列表顶部关闭 Daybreak 后重试。"
                            if daybreak else "本机 Codex 账户没有提供 GPT-dot 模型。")
                    raise ValueError(codex.error or hint)
                models = self.preferred_models("clients", session)
                current = session["openai"] if sid and not switching else models["openai"]
                default = next((m for m, name in choices.items() if m == "gpt-dot" or name.casefold() == "gpt-dot"),
                               next(iter(choices)))
                model = current if current in choices else default
                models["openai"] = model
                efforts = self.preferred_efforts("clients", session, models)
                if sid and switching:
                    self.engine.switch_backend(sid, "clients", models, efforts, recipient="openai")
                elif not sid:
                    self.settings.setValue("backend", "clients")
            except ValueError as exc:
                self.command_detail("无法打开 GPT-dot", str(exc))
                return
            finally:
                if self.backend_task is asyncio.current_task():
                    self.backend_task = None
                    self.refresh(force=True)
            self.choose_model("openai", model, efforts["openai"])
            self.sources_box.setCurrentIndex(0)
            self.select_recipient("openai")
            if switching and not gemini.groups and gemini not in catalogs:
                self.refresh_catalog("gemini")

        self.backend_task = self.run_task(activate())
        self.refresh(force=True)

    def layout_models(self):
        if not hasattr(self, "compact_efforts"):
            return
        active = [p for key, p in self.pickers.items() if not self.recipient or key == self.recipient]
        width = self.composer.width()
        stacked = width < 450
        self.actions_layout.setDirection(QBoxLayout.Direction.TopToBottom if stacked else QBoxLayout.Direction.LeftToRight)
        gpt = self.recipient != "gemini"
        self.speed_box.setVisible(gpt)
        reserved = 70 + 28 * int(gpt) + (30 if self.stop_button.isVisible() else 0)
        if not stacked:
            reserved += 38 + self.permission_box.sizeHint().width() + self.recipient_button.sizeHint().width()
        compact = width < reserved + sum(p.sizeHint().width() for p in active)
        for picker in self.pickers.values():
            picker.setVisible(not compact and picker in active)
        self.compact_models.setVisible(compact)
        self.compact_efforts.setVisible(compact and any(not p.effort_control.isHidden() for p in active))
        self.compact_efforts.setEnabled(any(p.effort_control.isEnabled() and not p.effort_control.isHidden() for p in active))

    def compact_model_menu(self, checked=False, *, effort=False):
        anchor = self.compact_efforts if effort else self.compact_models
        pickers = [p for key, p in self.pickers.items() if (not self.recipient or self.recipient == key)
                   and (not effort or p.effort_control.isEnabled() and not p.effort_control.isHidden())]
        def open_picker(picker):
            (picker.open_effort if effort else picker.open_models)(anchor=anchor)
        if len(pickers) == 1:
            open_picker(pickers[0])
            return
        menu = QMenu(self)
        menu.setAccessibleName(anchor.accessibleName())
        for picker in pickers:
            menu.addAction(RECIPIENTS[picker.provider], lambda _, p=picker: open_picker(p))
        self.popup_choices(anchor, menu)

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
        if provider == "openai" and not self.pickers[provider].accepts(group_for(catalog.view()["groups"], value)):
            return
        value, effort = self.engine.model_choice(self.backend_box.currentData(), provider, value, effort)
        key = self.model_key(provider)
        self.settings.setValue(key, value)
        self.settings.setValue(key + "-effort", effort)
        if not self.sid:
            previous = self.pickers[provider].model
            self.pickers[provider].select(value, catalog.view(), effort)
            if provider == "openai" and previous != value:
                self.draft_speed = "standard"
                self.refresh_access()
            self.refresh_mode()
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
                if self.sid and not model and data["source"] == "client" and data["models"]:
                    self.refresh(force=True)
                    model = picker.model
                if not self.sid and data["source"] == "client" and (
                    model not in data["models"] or not picker.accepts(group_for(data["groups"], model))
                ):
                    preferred = self.settings.value(self.model_key(provider), "")
                    choices = [m for m in data["models"] if picker.accepts(group_for(data["groups"], m))]
                    model = preferred if preferred in choices else next(iter(choices), "")
                    if model:
                        self.settings.setValue(self.model_key(provider), model)
                effort = (self.engine.store.session(self.sid)["efforts"].get(provider, "") if self.sid
                          else self.preferred_efforts(self.backend_box.currentData(), {}, {provider: model})[provider])
                picker.select(model, data, effort)
                if provider == "openai":
                    self.refresh_access()
                    self.refresh_mode()
                    if data["source"] == "client":
                        picker.refresh_open_models()
                self.editor_changed()
                self.update_status()
        task = asyncio.create_task(fetch())
        self.catalog_tasks[provider] = task

    def editor_changed(self):
        ready = self.models_ready() or bool(self.sid) and not self.recipient and all(p.model for p in self.pickers.values())
        text = self.editor.toPlainText().strip()
        has_input = bool(text) or bool(self.inputs.items) and not self.needs_goal_text()
        self.send_button.setEnabled(has_input and (ready or (text.startswith(("/", "／")) and not self.inputs.items))
                                    and self.backend_task is None)

    def needs_goal_text(self):
        if self.recipient:
            return False
        if self.sid:
            session = self.engine.store.session(self.sid)
            return session["mode"] == "goal" and not session["objective"]
        return self.draft_mode == "goal"

    def models_ready(self):
        return self.backend_box.currentData() != "clients" or all(
            p.model in p.catalog.get("models", []) and p.accepts(group_for(p.catalog["groups"], p.model))
            for provider, p in self.pickers.items() if not self.recipient or provider == self.recipient)

    def send(self):
        text = self.editor.toPlainText().strip()
        if self.backend_task or not text and (not self.inputs.items or self.needs_goal_text()):
            return
        if text.startswith(("/", "／")) and not self.inputs.items:
            self.send_command("/" + text[1:])
            return
        if not self.send_button.isEnabled():
            return
        if len(text) > 120000:
            QMessageBox.information(self, "消息过长", "单条消息上限为 120,000 字符。")
            return
        sid = self.sid
        if not sid:
            models = [self.pickers[provider].model for provider in ("gemini", "openai")]
            sid = self.engine.create_session(*models, project_id=self.pid, standalone=self.pid is None,
                                             backend=self.backend_box.currentData(),
                                             permission=self.draft_permission, speed=self.draft_speed, mode=self.draft_mode,
                                             daybreak=self.draft_daybreak, recipient=self.recipient,
                                             efforts={p: picker.effort for p, picker in self.pickers.items()})["id"]
        self.engine.submit(sid, text, new_id(), draft=self.draft_key(), recipient=self.recipient)
        self.editor.clear()
        self.inputs.restore()
        if self.sid:
            self.save_draft()
            self.refresh()
        else:
            self.settings.remove("mode/" + self.draft_key())
            self.settings.remove("recipient/" + self.draft_key())
            self.settings.setValue("recipient/drafts/" + sid, self.recipient)
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
                if self.state["session"]["archived"]:
                    self.after_removal(self.sid)
                    return
            else:
                session = {"backend": self.settings.value("backend", "api"), "title": "新会话", "revision": 0, "brief": {}}
                models = self.preferred_models(session["backend"], session)
                session["efforts"] = self.preferred_efforts(session["backend"], session, models)
                session.update(models)
                self.state = dict(session=session, events=[], tasks=[], artifacts=[])
            session = self.state["session"]
            backend = "clients" if self.backend_task else session["backend"]
            if self.sid and not self.backend_task and backend == "clients" and any(not session[p] for p in self.pickers):
                available = self.preferred_models(backend, session)
                missing = {p: m for p, m in available.items() if not session[p] and m}
                if missing:
                    efforts = self.preferred_efforts(backend, session, missing)
                    self.engine.set_choices(self.sid, {p: (m, efforts[p]) for p, m in missing.items()})
                    self.state = self.engine.store.snapshot(self.sid)
                    session = self.state["session"]
            changed_backend = self.backend_box.currentData() != backend
            with QSignalBlocker(self.backend_box):
                self.backend_box.setCurrentIndex(self.backend_box.findData(backend))
            if changed_backend:
                self.backend_box.updateGeometry()
            project = self.engine.store.project(self.pid) if self.pid else None
            self.project_name.setToolTip((project["name"] + "\n" + "\n".join(f["path"] for f in project["folders"])) if project else "选择项目")
            self.chat_more.setVisible(bool(self.sid))
            self.share_button.setVisible(bool(self.sid))
            self.title.setToolTip(session["title"] if self.sid else project["name"] if project else "新聊天")
            self.fit_title()
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
            self.heading.setText("我们要构建什么？" if not project else f"你想让我们在 {project['name']} 中构建什么？")
            self.heading.setWordWrap(True)
            self.heading.setVisible(empty)
            self.heading_gap.setVisible(empty)
            self.timeline.setVisible(not empty)
            self.chat_layout.setStretch(0, int(empty))
            self.chat_layout.setStretch(self.chat_layout.count() - 1, int(empty))
            self.timeline.workspace = self.preview.workspace = session.get("workspace")
            self.timeline.update_events(self.state["events"], THEMES[self.theme])
            self.fill_inspector()
            self.refresh_access()
            self.refresh_mode()
            self.update_status()
            self.editor_changed()
            if changed_backend and self.isVisible():
                self.refresh_catalogs()
        self.versions = self.engine.changes.copy()
        drafts = []
        intermediate = {}
        for task in self.state["tasks"]:
            if task["status"] not in ("waiting", "running", "ready"):
                continue
            jid = task["id"]
            body = self.engine.previews.get(jid) or task["draft"]
            if not body:
                continue
            if task.get("intermediate") or task["kind"] in CHAT_JOBS.values():
                intermediate[jid] = (task, body)
            else:
                author = "GPT" if PROVIDERS[task["kind"]] == "openai" else "Gemini"
                drafts.append(f"**{author} · {LABELS[task['kind']]}草稿**\n\n" + body)
        self.timeline.update_drafts(intermediate)
        text = "\n\n".join(drafts)
        if text != self.preview_text:
            self.preview_text = text
            self.preview.show_markdown(text)
            self.preview.verticalScrollBar().setValue(self.preview.verticalScrollBar().maximum())
            self.preview.setVisible(bool(text))

    def update_status(self):
        tasks = self.state["tasks"]
        active = [j for j in tasks if j["status"] in ACTIVE_STATUSES]
        pending = next((j for j in active if j["status"] == "needs_key"), None)
        running = [j for j in active if j["status"] == "running"]
        approvals = self.state.get("approvals")
        stopping = self.sid in self.engine.stopping and not active
        paused = bool(self.state["session"].get("paused_work"))
        phases = [self.engine.activity[j["id"]] for j in running if j["id"] in self.engine.activity]
        labels = [PROVIDER_NAMES[PROVIDERS[j["kind"]]] + " · " + LABELS[j["kind"]] for j in running]
        running_text = "、".join(dict.fromkeys(labels))
        if stopping:
            text = "正在停止任务…"
        elif self.backend_task:
            text = "正在读取客户端模型…"
        elif self.backend_error:
            text = self.backend_error + " · 重试"
        elif approvals:
            text = "等待批准 · " + approvals[0]["title"] + " · 查看"
        elif pending:
            provider = "Gemini" if PROVIDERS[pending["kind"]] == "gemini" else "OpenAI"
            text = "等待 " + provider + " API Key · 去配置"
            if running:
                text = running_text + " 进行中　" + text
        elif running:
            text = "进行中　" + running_text
        elif active:
            text = "任务已排队" if any(j["status"] == "ready" for j in active) else "正在等待前置任务"
        elif paused or tasks and tasks[-1]["status"] == "stopped":
            text = "任务已停止"
        else:
            text = ""
        self.work_status.setText(text)
        self.work_status.setToolTip("\n".join([text, *dict.fromkeys(phases)]))
        self.work_status.setAccessibleName(self.work_status.toolTip())
        self.work_status.setEnabled(self.backend_task is None and not stopping)
        self.work_progress.setVisible(bool(stopping or self.backend_task or running and not approvals))
        self.stop_button.setVisible(bool(active or stopping))
        self.stop_button.setEnabled(bool(active))
        self.continue_button.setVisible(paused and not stopping)
        guiding = not self.recipient and any(accepts_guidance(j) for j in active)
        action = "发送给 " + self.recipient_name() if self.recipient else (
            "发送引导" if guiding else "带着补充要求继续" if paused else "发送消息")
        self.send_button.setAccessibleName(action)
        self.send_button.setToolTip(action + "（Enter）")
        if not self.recipient and (guiding or paused):
            self.editor.setPlaceholderText("输入补充要求，按 Enter 引导当前任务…" if guiding else "输入补充要求后继续，或点击「继续」…")
        self.editor.can_interrupt = bool(active)
        self.activity_row.setVisible(bool(text))
        if getattr(self, "state", {}).get("session", {}).get("backend") == "clients":
            errors = [c.error for p, c in self.engine.client_catalogs.items() if c.error and (not self.recipient or p == self.recipient)]
            self.connection_state.setText(errors[0] if errors else "")
            self.connection_state.setToolTip("客户端连接状态")
            self.connection_state.setVisible(bool(errors))
            return
        missing = [title + " 未配置" for key, title in (("gemini", "Gemini"), ("openai", "OpenAI"))
                   if not self.engine.keys[key] and (not self.recipient or key == self.recipient)]
        text = self.engine.config_error or " · ".join(missing)
        self.connection_state.setText(text)
        self.connection_state.setToolTip("打开设置\n" + str(self.engine.project / ".env"))
        self.connection_state.setVisible(bool(text))

    def stop_work(self):
        if self.sid and self.stop_button.isEnabled():
            self.stop_button.setEnabled(False)
            self.editor.can_interrupt = False
            self.work_status.setText("正在停止任务…")
            self.run_task(self.engine.stop_session(self.sid))

    def continue_work(self):
        if self.sid:
            self.engine.continue_session(self.sid)
            self.refresh()

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
            rows = [(r["id"], r["title"], r["status"], r["model"], r["effort"]) if key == "tasks"
                    else (r["id"], r["name"]) for r in self.state[key]]
            if rows == self.inspector_rows.get(key):
                continue
            self.inspector_rows[key] = rows
            current = widget.currentItem()
            selected = current.data(USER_ROLE) if current else None
            entries = list(reversed(self.state[key]))
            same_items = [widget.item(i).data(USER_ROLE) for i in range(widget.count())] == [r["id"] for r in entries]
            with QSignalBlocker(widget):
                if not same_items:
                    widget.selectionModel().clear()
                    widget.clear()
                for index, entry in enumerate(entries):
                    if key == "tasks":
                        title = entry["title"] + "\n" + STATUSES[entry["status"]] + " · " + entry["model"]
                        if entry["effort"]:
                            title += " · " + LEVEL_NAMES.get(entry["effort"], entry["effort"])
                    else:
                        title = entry["name"]
                    item = widget.item(index) if same_items else QListWidgetItem(widget)
                    item.setText(title)
                    item.setToolTip(title)
                    if not same_items:
                        item.setData(USER_ROLE, entry["id"])
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

    def retry_from_timeline(self, jid, read_only):
        try:
            self.engine.retry(self.sid, jid, read_only=read_only)
        except (KeyError, ValueError) as exc:
            QMessageBox.information(self, "任务状态", str(exc))
        self.refresh(force=True)

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
        dialog = ArtifactDialog(self, asset, self.state["session"]["workspace"])
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
        native = self.backend_box.currentData() == "clients"
        speeds = available_speeds(group_for(catalog.groups, model)) if native else {}
        enabled = bool(session.get("daybreak", self.draft_daybreak)) and native
        picker = self.pickers["openai"]
        mode_changed = enabled != self.daybreak_action.isChecked()
        self.daybreak_action.setChecked(enabled)
        group = group_for(picker.catalog.get("groups", []), model)
        compatible = picker.accepts(group)
        if native:
            picker.effort_control.setEnabled(bool(compatible and group and len(group["choices"]) > 1 and not self.backend_task))
        eligible = any(model_available(g, True) for g in catalog.groups)
        show_daybreak = native and (eligible or enabled)
        self.daybreak_action.setVisible(show_daybreak)
        self.daybreak_action.setEnabled(self.backend_task is None and (enabled or eligible))
        self.daybreak_action.setText("Daybreak 模式    " + ("已开启" if enabled else "已关闭"))
        self.daybreak_action.setToolTip("关闭 Daybreak，使用常规模型" if enabled else "开启 Daybreak，使用当前账户授权的模型")
        if native and mode_changed:
            picker.refresh_open_models()
        if self.slash_menu.isVisible():
            self.slash_menu.refresh_matches()
        speed = session.get("speed", self.draft_speed)
        if self.backend_task is None and catalog.groups and not catalog.error and speed != "standard" and speed not in speeds:
            speed = self.draft_speed = "standard"
            if self.sid:
                self.engine.set_access(self.sid, speed=speed)
        speed_label = "Fast 已关闭" if speed == "standard" else SPEEDS[speed] + " 已开启"
        self.speed_box.setChecked(speed != "standard")
        self.speed_box.setIcon(self.speed_icons[speed != "standard"])
        self.speed_box.setAccessibleName("GPT 加速，" + speed_label)
        self.speed_box.setEnabled(self.backend_task is None and (not native or speed != "standard" or "fast" in speeds))
        if not native:
            reason = "点击切换到本机客户端并开启 Fast"
        elif self.backend_task or self.pickers["openai"].loading:
            reason = "正在读取 Codex 模型速度"
        else:
            reason = "点击关闭加速" if speed != "standard" else (
                "点击开启 Fast" if "fast" in speeds else catalog.error or "当前账户或模型未提供 Fast")
        self.speed_box.setToolTip(speed_label + " · " + reason)

    def change_daybreak(self, enabled):
        try:
            if self.sid:
                self.engine.set_daybreak(self.sid, enabled)
            else:
                picker = self.pickers["openai"]
                model = select_daybreak_model(picker.catalog["groups"], picker.model, enabled)
                self.draft_daybreak = enabled
                self.daybreak_action.setChecked(enabled)
                if model != picker.model:
                    self.choose_model("openai", model)
        except ValueError as exc:
            self.statusBar().showMessage(str(exc), 6000)
        self.refresh(force=True)

    def popup_choices(self, control, menu):
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.popup(control.mapToGlobal(QPoint(0, -menu.sizeHint().height() - 3)))

    def toggle_fast(self, enabled):
        if self.backend_box.currentData() == "api":
            self.enable_native_fast()
        else:
            self.change_speed("fast" if enabled else "standard")

    def speed_menu(self):
        menu = QMenu(self)
        menu.setAccessibleName("Codex 速度")
        native = self.backend_box.currentData() == "clients"
        if self.backend_task:
            menu.addAction("正在读取本机客户端…").setEnabled(False)
        elif not native:
            menu.addAction("切换到本机客户端并开启 Fast", self.enable_native_fast)
        else:
            catalog = self.engine.client_catalogs["openai"]
            speeds = available_speeds(group_for(catalog.groups, self.pickers["openai"].model))
            current = self.engine.store.session(self.sid)["speed"] if self.sid else self.draft_speed
            for key in ("standard", *speeds):
                action = menu.addAction("关闭 Fast" if key == "standard" else "开启 " + SPEEDS[key])
                action.setCheckable(True)
                action.setChecked(key == current)
                action.triggered.connect(lambda _, tier=key: self.change_speed(tier))
            if not speeds:
                menu.addSeparator()
                menu.addAction(catalog.error or ("当前模型未提供加速档位" if catalog.groups else "正在读取模型目录")).setEnabled(False)
                menu.addAction("选择 GPT 模型…", self.pickers["openai"].open_models)
                menu.addAction("刷新 Codex 模型", lambda: self.refresh_catalog("openai"))
        self.popup_choices(self.speed_box, menu)

    def enable_native_fast(self):
        sid = self.sid
        async def enable():
            try:
                await asyncio.gather(*(c.refresh() for c in self.engine.client_catalogs.values()))
                session = self.engine.store.session(sid) if sid else {}
                models = self.preferred_models("clients", session)
                for p, catalog in self.engine.client_catalogs.items():
                    if not models[p]:
                        raise ValueError(catalog.error or "客户端模型目录为空")
                self.engine.validate_access("clients", models["openai"], self.permission_box.currentData(), "fast")
                if sid:
                    self.engine.switch_backend(sid, "clients", models, self.preferred_efforts("clients", session, models))
                    self.engine.set_access(sid, speed="fast")
                else:
                    self.settings.setValue("backend", "clients")
                    self.draft_speed = "fast"
            except (OSError, ValueError, RuntimeError) as exc:
                self.command_detail("无法开启 Fast", str(exc))
            finally:
                if self.backend_task is asyncio.current_task():
                    self.backend_task = None
                    self.refresh(force=True)
        self.backend_task = self.run_task(enable())
        self.refresh(force=True)

    def change_permission(self):
        mode = self.permission_box.currentData()
        if self.sid:
            self.engine.set_access(self.sid, permission=mode)
        else:
            self.draft_permission = mode

    def change_speed(self, speed):
        if self.sid:
            self.engine.set_access(self.sid, speed=speed)
        else:
            self.draft_speed = speed
        self.refresh_access()

    def refresh_mode(self):
        session = self.state["session"]
        mode = session["mode"] if self.sid else self.draft_mode
        text = MODE_TITLES[mode]
        if mode == "goal" and session.get("goal_status"):
            text = "目标 · " + GOAL_STATES[session["goal_status"]]
        self.mode_button.setText(text + " ▾")
        self.mode_button.setToolTip((session.get("objective") if mode == "goal" else "") or text)
        recipient = self.recipient_name()
        self.dot_chat.setEnabled(self.backend_task is None)
        self.recipient_button.setEnabled(self.backend_task is None)
        self.recipient_button.setText(recipient + "  ▾")
        self.recipient_button.setAccessibleName("消息接收方，" + recipient)
        self.recipient_button.setToolTip("选择双模型协作、Gemini 或 GPT 直接对话")
        self.mode_button.setVisible(not self.recipient)
        self.mode_caption.setVisible(not self.recipient)
        self.layout_models()
        if self.recipient:
            self.editor.setPlaceholderText("给 " + recipient + " 发消息…")
            return
        self.editor.setPlaceholderText({"side": "继续向 GPT 提问…", "plan": "描述想解决的问题，或补充计划要求…",
                                        "goal": "输入要达成的目标，或补充验收条件…",
                                        "default": "描述更改或提出问题"}[mode])

    def mode_menu(self):
        session = self.state["session"]
        mode = session["mode"] if self.sid else self.draft_mode
        menu = QMenu(self)
        menu.setAccessibleName("会话模式")
        for key, title in MODES.items():
            action = menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(key == mode)
            action.triggered.connect(lambda _, value=key: self.select_mode(value))
        if self.sid and mode == "plan":
            menu.addSeparator()
            action = menu.addAction("执行当前计划", lambda: self.mode_action("implement"))
            action.setEnabled(bool(self.engine.store.current_proposal(self.sid)))
        if self.sid and session["objective"]:
            menu.addSeparator()
            menu.addAction("查看目标", lambda: self.command_detail("当前目标", goal_text(session)))
            status = session["goal_status"]
            if status in ("active", "paused", "blocked"):
                verb = "pause" if status == "active" else "resume"
                menu.addAction("暂停目标" if verb == "pause" else "继续目标", lambda: self.mode_action(verb))
            menu.addAction("清除目标", lambda: self.mode_action("clear"))
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.popup(self.options_button.mapToGlobal(self.options_button.rect().bottomLeft()))

    def select_mode(self, mode):
        if self.sid:
            self.engine.store.change_mode(self.sid, mode)
            self.engine.notify(self.sid)
            self.refresh()
        else:
            self.draft_mode = mode
            self.settings.setValue("mode/" + self.draft_key(), mode)
            self.refresh_mode()
            self.editor_changed()
        self.editor.setFocus()

    def mode_action(self, action):
        try:
            if action == "implement":
                self.engine.store.implement_plan(self.sid)
            else:
                self.engine.store.goal_action(self.sid, action)
        except ValueError as exc:
            self.command_detail("会话模式", str(exc))
        self.engine.notify(self.sid)
        self.refresh()

    def send_command(self, text):
        sid, pid = self.sid, self.pid
        self.editor.clear()
        self.save_draft()
        word, argument = split(text)
        name = command_name(word[1:])
        if name == "feedback":
            url = feedback_url(argument)
            if not QDesktopServices.openUrl(QUrl(url)):
                self.command_detail("反馈", url)
            return
        if name == "btw" and sid:
            self.open_side_chat(sid, argument)
            return
        if name == "history" or (name == "resume" and not argument):
            if name == "resume":
                self.sources_box.setCurrentIndex(0)
            self.search()
            self.filter.setText(argument)
            return
        if name == "usage" and argument in ("", "codex", "antigravity"):
            UsageDialog(self, argument or "codex").open()
            return
        if name == "usage" and argument in API_DASHBOARDS:
            if not QDesktopServices.openUrl(QUrl(API_DASHBOARDS[argument])):
                self.command_detail("API 用量", API_DASHBOARDS[argument])
            return
        if name == "mcp" and argument in ("", "codex", "antigravity"):
            McpDialog(self, argument or "codex").open()
            return
        if name in ("model", "effort") and argument in ("gemini", "antigravity", "openai", "codex", "gpt"):
            picker = self.pickers["gemini" if argument in ("gemini", "antigravity") else "openai"]
            (picker.open_models if name == "model" else picker.open_effort)()
            return
        if not argument:
            actions = {"new": self.new_session, "fast": self.speed_menu, "settings": self.agent_settings,
                       "skills": self.agent_settings, "mode": self.mode_menu, "plan": lambda: self.select_mode("plan"),
                       "goal": self.mode_menu if sid and self.state["session"]["objective"] else lambda: self.select_mode("goal"),
                       "model": self.pickers["openai"].open_models, "effort": self.pickers["openai"].open_effort,
                       "permissions": self.permission_box.showPopup, "backend": self.show_backend,
                       "project": self.project_picker, "projects": self.project_picker, "add-dir": self.choose_source}
            if sid:
                actions.update(rename=lambda: self.rename_chat(sid), archive=lambda: self.archive_chat(sid),
                               share=lambda: ShareDialog(self, sid).open(), fork=lambda: self.fork_menu(sid, pid),
                               export=lambda: save_transcript(self, transcript(self.engine.store, sid),
                                                              lambda message: self.statusBar().showMessage(message, 6000)))
            if name in actions:
                actions[name]()
                return
        self.run_command(text, sid, pid)

    def run_command(self, text, sid, pid):
        options = dict(line=text, id=sid, project_id=pid, cwd=str(self.engine.skill_folders(sid, pid)[0]),
                       backend=self.backend_box.currentData(), permission=self.permission_box.currentData(),
                       speed=self.engine.store.session(sid)["speed"] if sid else self.draft_speed,
                       daybreak=self.state["session"].get("daybreak", self.draft_daybreak),
                       mode=self.engine.store.session(sid)["mode"] if sid else self.draft_mode, standalone=pid is None)
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
                command = COMMANDS[command_name(text.split()[0][1:])]
                self.command_detail(command.title, result["text"])
        self.run_task(run())

    def open_side_chat(self, sid, question):
        dock = next((w for w in self.findChildren(SideChat) if w.origin == sid), None)
        if dock is None:
            dock = SideChat(self, sid)
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
            if self.width() < 930:
                dock.setFloating(True)
                dock.resize(330, 420)
        dock.show()
        dock.raise_()
        dock.editor.setFocus()
        if question:
            dock.editor.setPlainText(question)
            dock.send()

    def fork_menu(self, sid, pid):
        menu = QMenu(self)
        menu.setAccessibleName("创建聊天分支")
        menu.addAction("在当前工作区继续", lambda: self.run_command("/fork", sid, pid))
        root = Path(self.engine.store.session(sid)["workspace"])
        repository = next((path for path in (root, *root.parents) if (path / ".git").exists()), None)
        def choose_path():
            dialog = QInputDialog(self)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dialog.setWindowTitle("在新 Git 工作树中继续")
            dialog.setLabelText("新目录（以当前提交 HEAD 为起点）")
            if repository and repository.name:
                dialog.setTextValue(str(repository.with_name(repository.name + "-worktree")))
            dialog.textValueSelected.connect(lambda path: self.run_command("/fork --worktree " + path, sid, pid))
            dialog.open()
        worktree = menu.addAction("在新 Git 工作树中继续…", choose_path)
        worktree.setEnabled(len(self.engine.skill_folders(sid, pid)) == 1 and repository is not None)
        self.popup_choices(self.composer, menu)

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
        if self.unified_titlebar:
            self.header.layout().setContentsMargins(12 if show else 80, 0, 12, 0)

    def toggle_inspector(self):
        show = self.inspector.isHidden()
        if show and self.width() < 930:
            self.sidebar.hide()
            self.expand_side.show()
            if self.unified_titlebar:
                self.header.layout().setContentsMargins(80, 0, 12, 0)
        self.inspector.setVisible(show)

    def set_theme(self, name):
        self.theme = name if name in THEMES else "light"
        apply_theme(QApplication.instance(), self.theme)
        self.composer_shadow.setColor(QColor(THEMES[self.theme]["shadow"]))
        self.theme_button.setIcon(icon("sun" if self.theme == "dark" else "moon"))
        self.theme_button.setToolTip("切换为浅色主题" if self.theme == "dark" else "切换为深色主题")
        if hasattr(self, "state"):
            self.timeline.reset()
            self.preview_text = None
            self.refresh(force=True)

    def toggle_theme(self):
        self.set_theme("dark" if self.theme == "light" else "light")
        self.settings.setValue("theme", self.theme)

    def fit_title(self):
        self.title.setText(self.title.fontMetrics().elidedText(
            self.title.toolTip(), Qt.TextElideMode.ElideRight, self.title.contentsRect().width()))

    def eventFilter(self, watched, event):
        if watched is getattr(self, "title", None) and event.type() == QEvent.Type.Resize:
            self.fit_title()
        if watched is getattr(self, "composer", None) and event.type() == QEvent.Type.Resize:
            self.layout_models()
        if watched is getattr(self, "content", None) and event.type() == QEvent.Type.Resize:
            self.message_nav.reposition()
        return super().eventFilter(watched, event)

    def composer_focus(self, focused):
        self.composer.setProperty("focused", focused)
        self.composer.style().unpolish(self.composer)
        self.composer.style().polish(self.composer)

    def showEvent(self, event):
        super().showEvent(event)
        if self.unified_titlebar:
            hide_macos_title(self)
            if self.saved_geometry:
                # AppKit 创建扩展标题栏时会调整窗口框架，等原生窗口显示后恢复用户保存的尺寸。
                geometry, self.saved_geometry = self.saved_geometry, None
                QTimer.singleShot(0, self, lambda: self.restoreGeometry(geometry))

    def closeEvent(self, event):
        self.timer.stop()
        self.cancel_backend_change()
        self.save_draft()
        self.settings.setValue("geometry", self.saveGeometry())
        for pane, key in ((self.sidebar, "navigation_width"), (self.inspector, "inspector_width")):
            if pane.isVisible():
                self.settings.setValue(key, pane.width())
        self.settings.sync()
        super().closeEvent(event)
        self.closed.emit()
