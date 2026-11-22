import asyncio
from datetime import datetime

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QComboBox, QDialog, QFrame, QHBoxLayout, QProgressBar, QScrollArea, QVBoxLayout, QWidget

from .desktop_widgets import button, label
from .usage import API_DASHBOARDS, CODEX_BILLING, plan_usage


def local_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")


class UsageDialog(QDialog):
    def __init__(self, host, source, parent=None):
        super().__init__(parent or host)
        self.setWindowTitle("套餐用量")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(440, 360)
        self.host = host
        self.task = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        header = QHBoxLayout()
        self.source = QComboBox()
        self.source.setAccessibleName("套餐用量来源")
        for key, title in (("antigravity", "Antigravity"), ("codex", "Codex")):
            self.source.addItem(title, key)
        self.source.setCurrentIndex(self.source.findData(source))
        header.addWidget(self.source)
        header.addStretch()
        self.plan_billing = button("套餐账单 ↗", lambda: QDesktopServices.openUrl(QUrl(CODEX_BILLING)))
        header.addWidget(self.plan_billing)
        self.billing = button("API 用量 ↗", self.open_billing)
        header.addWidget(self.billing)
        self.refresh_button = button("刷新", self.refresh)
        header.addWidget(self.refresh_button)
        layout.addLayout(header)
        self.summary = label("", "section")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        layout.addWidget(self.scroll, 1)
        self.stamp = label("", "caption")
        footer = QHBoxLayout()
        footer.addWidget(self.stamp, 1)
        footer.addWidget(button("关闭", self.reject))
        layout.addLayout(footer)
        self.source.currentIndexChanged.connect(self.refresh)
        self.finished.connect(self.stop)
        self.refresh()

    def stop(self):
        if self.task:
            self.task.cancel()
            self.task = None

    def open_billing(self):
        provider = "gemini" if self.source.currentData() == "antigravity" else "openai"
        QDesktopServices.openUrl(QUrl(API_DASHBOARDS[provider]))

    def refresh(self):
        self.stop()
        source = self.source.currentData()
        self.plan_billing.setVisible(source == "codex")
        self.summary.setText("正在读取…")
        self.stamp.clear()
        self.refresh_button.setEnabled(False)
        content = QWidget()
        rows = QVBoxLayout(content)
        rows.setContentsMargins(0, 4, 4, 4)
        rows.setSpacing(7)
        self.scroll.setWidget(content)

        async def read():
            try:
                data = await plan_usage(source, self.host.engine.home)
            except (OSError, ValueError, RuntimeError) as exc:
                self.summary.setText(str(exc))
            else:
                self.summary.setText(self.source.currentText() + " · " + (data["plan"] or "套餐额度"))
                for entry in data["buckets"]:
                    text = entry["name"]
                    if entry.get("disabled"):
                        text += "　已停用"
                    if "remaining_percent" in entry:
                        text += f"　剩余 {entry['remaining_percent']:g}%"
                    if "remaining_amount" in entry:
                        text += f"　剩余额度 {entry['remaining_amount']}"
                    rows.addWidget(label(text))
                    if "remaining_percent" in entry:
                        bar = QProgressBar()
                        bar.setRange(0, 1000)
                        bar.setValue(round(entry["remaining_percent"] * 10))
                        bar.setAccessibleName(text)
                        bar.setTextVisible(False)
                        bar.setFixedHeight(5)
                        rows.addWidget(bar)
                    if entry.get("reset"):
                        rows.addWidget(label("重置于 " + local_time(entry["reset"]), "caption"))
                credits = data.get("credits") or {}
                has_balance = credits.get("unlimited") or credits.get("balance") is not None
                if has_balance:
                    rows.addWidget(label("点数　" + ("不限额" if credits.get("unlimited") else str(credits["balance"]))))
                if not data["buckets"] and not has_balance:
                    rows.addWidget(label("暂无额度明细", "caption"))
                rows.addStretch()
                self.stamp.setText("更新于 " + local_time(data["updated"]))
            finally:
                if self.task is asyncio.current_task():
                    self.refresh_button.setEnabled(True)
                    self.task = None

        self.task = self.host.run_task(read())
