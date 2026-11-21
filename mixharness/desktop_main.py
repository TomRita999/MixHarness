import argparse
import asyncio
import signal
import sys
from pathlib import Path


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["--check-worker"]:
        from .check_runner import main as run_check
        run_check(args[1:])
        return 0
    if args[:1] == ["--standard-check"]:
        from .check_runner import standard_check
        standard_check(args[1:])
        return 0
    parser = argparse.ArgumentParser(description="启动 MixHarness 桌面应用")
    parser.add_argument("--project", type=Path, help="包含配置文件和会话记录的目录")
    parser.add_argument("--data-dir", type=Path, help="会话与验证记录目录")
    options = parser.parse_args(args)
    from filelock import Timeout
    from PySide6.QtCore import QEvent, QObject, QSettings, QStandardPaths
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QMessageBox
    from qasync import QEventLoop

    from .desktop import Window
    from .commands import control_server
    from .desktop_style import RESOURCES, load_icons
    from .engine import open_engine

    app = QApplication([sys.argv[0]])
    app.setApplicationName("MixHarness")
    app.setOrganizationDomain("mixharness.local")
    app.setOrganizationName("MixHarness")
    app.setDesktopFileName("MixHarness")
    app.setWindowIcon(QIcon(str(RESOURCES / "app.png")))
    app.setStyle("Fusion")
    app.setProperty("iconFamily", load_icons())
    app.setQuitOnLastWindowClosed(False)
    preferences = QSettings()
    default = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    previous = Path(str(preferences.value("project", default)))
    project = (options.project or (previous if previous.is_dir() else Path(default))).resolve()
    home = (options.data_dir or project / ".mixharness").resolve()
    project.mkdir(parents=True, exist_ok=True)
    settings = QSettings(str(home / "desktop.ini"), QSettings.Format.IniFormat)
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    class QuitFilter(QObject):
        def eventFilter(self, obj, event):
            if event.type() == QEvent.Type.Quit and window.isVisible():
                window.close()
                return True
            return False

    async def run(window, engine):
        closed = asyncio.Event()
        window.closed.connect(closed.set)
        worker = asyncio.create_task(engine.serve())
        closing = asyncio.create_task(closed.wait())
        try:
            window.show()
            window.raise_()
            window.activateWindow()
            window.refresh_catalogs()
            async with control_server(engine):
                await asyncio.wait((worker, closing), return_when=asyncio.FIRST_COMPLETED)
                if worker.done():
                    worker.result()
        finally:
            pending = (worker, closing, *window.steering, *window.catalog_tasks.values())
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    try:
        with loop, open_engine(project, home) as engine:
            window = Window(engine, settings)
            if not options.data_dir:
                preferences.setValue("project", str(project))
            quit_filter = QuitFilter()
            app.installEventFilter(quit_filter)
            signal.signal(signal.SIGTERM, lambda *_: window.close())
            signal.signal(signal.SIGINT, lambda *_: window.close())
            loop.run_until_complete(run(window, engine))
    except Timeout:
        QMessageBox.information(None, "MixHarness 正在运行", "此会话目录已被打开。\n" + str(home))
        return 1
    except (OSError, ValueError) as exc:
        QMessageBox.critical(None, "无法启动 MixHarness", str(exc))
        return 1
    return 0
