import os

from PySide6.QtCore import QLibraryInfo

from mixharness.workspace import clear_git_context


# 测试创建独立的 Git 仓库，直接运行测试时同样隔离宿主的仓库和临时配置。
clear_git_context(os.environ)
# 桌面测试和它启动的 Cocoa 子进程使用当前 Python 环境的 Qt 插件。
os.environ['QT_PLUGIN_PATH'] = QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath)
