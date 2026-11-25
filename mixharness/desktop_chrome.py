import ctypes

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget


def hide_macos_title(window):
    # Qt 扩展客户区后，AppKit 仍会在同一行绘制窗口标题。保留标题供窗口菜单和辅助功能读取。
    runtime = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
    selector = runtime.sel_registerName
    selector.argtypes = [ctypes.c_char_p]
    selector.restype = ctypes.c_void_p
    pointer = ctypes.c_void_p
    get_window = ctypes.CFUNCTYPE(pointer, pointer, pointer)(("objc_msgSend", runtime))
    set_visibility = ctypes.CFUNCTYPE(None, pointer, pointer, ctypes.c_long)(("objc_msgSend", runtime))
    nswindow = get_window(int(window.winId()), selector(b"window"))
    if nswindow:
        set_visibility(nswindow, selector(b"setTitleVisibility:"), 1)  # NSWindowTitleHidden


class TitleBar(QWidget):
    """整行空白和标题可用于拖动，按钮仍由 Qt 单独处理。"""

    def __init__(self):
        super().__init__()
        self.drag_offset = None
        self.setFixedHeight(46)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self.window().isFullScreen():
            return super().mousePressEvent(event)
        window = self.window()
        if QApplication.platformName() == "cocoa" or not window.windowHandle().startSystemMove():
            self.drag_offset = event.globalPosition().toPoint() - window.frameGeometry().topLeft()
        event.accept()

    def mouseMoveEvent(self, event):
        if self.drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self.drag_offset)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self.drag_offset = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self.window().isFullScreen():
            window = self.window()
            window.showNormal() if window.isMaximized() else window.showMaximized()
            self.drag_offset = None
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)
