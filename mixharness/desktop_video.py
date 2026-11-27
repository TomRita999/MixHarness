from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, QSignalBlocker, Qt, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout


class VideoPreview(QDialog):
    def __init__(self, parent, filename, *, data=None):
        super().__init__(parent)
        path = Path(filename)
        self.setWindowTitle(path.name)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(800, 500)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        screen = QVideoWidget(self)
        screen.setAccessibleName('视频画面')
        self.player.setVideoOutput(screen)

        self.play_button = QPushButton('播放', self)
        self.play_button.clicked.connect(self.toggle_playback)
        self.position = QSlider(Qt.Orientation.Horizontal, self)
        self.position.setRange(0, 1000)
        self.position.setEnabled(False)
        self.position.setAccessibleName('视频播放进度')
        self.position.valueChanged.connect(self.seek)
        controls = QHBoxLayout()
        controls.addWidget(self.play_button)
        controls.addWidget(self.position, 1)
        self.error_message = QLabel(self)
        self.error_message.setTextFormat(Qt.TextFormat.PlainText)
        self.error_message.setWordWrap(True)
        self.error_message.hide()
        layout = QVBoxLayout(self)
        layout.addWidget(screen, 1)
        layout.addWidget(self.error_message)
        layout.addLayout(controls)

        self.player.playbackStateChanged.connect(self.update_play_button)
        self.player.positionChanged.connect(self.update_progress)
        self.player.durationChanged.connect(self.update_progress)
        self.player.seekableChanged.connect(self.update_progress)
        self.player.errorOccurred.connect(self.show_error)
        if data is None:
            self.player.setSource(QUrl.fromLocalFile(str(path.resolve(strict=True))))
        else:
            # QMediaPlayer 异步读取设备，缓冲区与对话框具有相同的生命周期。
            self.buffer = QBuffer(self)
            self.buffer.setData(data)
            if not self.buffer.open(QIODevice.OpenModeFlag.ReadOnly):
                raise OSError(self.buffer.errorString())
            name_hint = QUrl()
            name_hint.setPath(path.name)
            self.player.setSourceDevice(self.buffer, name_hint)

    def toggle_playback(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            if self.player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia:
                self.player.setPosition(0)
            self.player.play()

    def update_play_button(self, state):
        self.play_button.setText('暂停' if state == QMediaPlayer.PlaybackState.PlayingState else '播放')

    def seek(self, value):
        if self.player.isSeekable():
            self.player.setPosition(self.player.duration() * value // self.position.maximum())

    def update_progress(self, *_):
        duration = self.player.duration()
        self.position.setEnabled(duration > 0 and self.player.isSeekable())
        if duration > 0 and not self.position.isSliderDown():
            with QSignalBlocker(self.position):
                self.position.setValue(self.player.position() * self.position.maximum() // duration)

    def show_error(self, error, message):
        if error != QMediaPlayer.Error.NoError:
            self.error_message.setText(message or '当前媒体后端无法播放此视频。')
            self.error_message.show()
            self.play_button.setEnabled(False)

    def release_media(self):
        self.player.stop()
        self.player.setSource(QUrl())
        self.player.setSourceDevice(None)

    def done(self, result):
        self.release_media()
        super().done(result)

    def closeEvent(self, event):
        self.release_media()
        super().closeEvent(event)
