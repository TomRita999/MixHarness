import tempfile
import time
import unittest
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox, QPushButton

from mixharness.attachments import Attachment
from mixharness.catalog import MODELS
from mixharness.desktop_widgets import ArtifactDialog, Document, MessageRow
from mixharness.engine import open_engine
from mixharness.store import new_id
from mixharness.workspace import artifact
from tests.qt_app import application
from tests.test_video_media import ROOT, recording


class DesktopVideoTests(unittest.TestCase):
    def setUp(self):
        self.app = application()
        scratch = ROOT / '.work' / 'tests'
        scratch.mkdir(parents=True, exist_ok=True)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=scratch, prefix='desktop-video-')))
        self.project = self.root / 'project'
        self.project.mkdir()
        self.engine = self.enterContext(open_engine(self.root, self.root / 'data'))
        self.store = self.engine.store
        models = {provider: choices[0] for provider, choices in MODELS.items()}
        self.sid = self.engine.create_session(**models, directory=self.project)['id']

    def message(self, seq):
        event = next(e for e in self.store.snapshot(self.sid)['events'] if e['seq'] == seq)
        row = MessageRow(event, self.store.inputs, workspace=self.project)
        self.addCleanup(row.close)
        row.show()
        return row

    def report(self, name, content):
        job = self.store.one('SELECT * FROM jobs WHERE sid=?', self.sid)
        with self.store.db:
            aid = artifact(self.store, job, name, content, {})
        asset = self.store.one('SELECT * FROM artifacts WHERE sid=? AND id=?', self.sid, aid)
        dialog = ArtifactDialog(None, asset, self.project)
        self.addCleanup(dialog.close)
        dialog.show()
        return dialog

    def assert_playback(self, host, activate):
        from PySide6.QtMultimedia import QMediaPlayer
        from mixharness.desktop_video import VideoPreview

        errors = []
        def report_dialog():
            box = host.findChild(QMessageBox)
            if box is not None and box.isVisible():
                errors.append(box.text())
                box.reject()

        # 记录同步错误对话框，避免文本预览回归时一直等待人工关闭。
        timer = QTimer(host)
        timer.timeout.connect(report_dialog)
        timer.start(30)
        try:
            activate()
        finally:
            timer.stop()
        self.assertEqual(errors, [])
        preview = host.findChild(VideoPreview)
        self.assertIsNotNone(preview, '点击后没有打开视频播放器')
        try:
            player = preview.player
            preview.audio.setMuted(True)
            preview.play_button.click()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                self.assertEqual(player.error(), QMediaPlayer.Error.NoError, player.errorString())
                frame = player.videoSink().videoFrame()
                if frame.isValid() and player.position() > 0:
                    break
                QTest.qWait(10)
            else:
                self.fail('播放器未输出画面或播放进度')
            self.assertEqual((frame.width(), frame.height()), (64, 64))
            preview.play_button.click()
            self.assertEqual(player.playbackState(), QMediaPlayer.PlaybackState.PausedState)
            self.assertTrue(preview.position.isEnabled())
            preview.position.setValue(preview.position.maximum() // 2)
            deadline = time.monotonic() + 3
            while abs(player.position() - player.duration() // 2) > 130 and time.monotonic() < deadline:
                QTest.qWait(10)
            self.assertAlmostEqual(player.position(), player.duration() // 2, delta=130)
        finally:
            preview.close()
            QTest.qWait(50)

    def open_link(self, host, document, path):
        link = document.document().find('查看录像')
        self.assertFalse(link.isNull())
        url = QUrl(link.charFormat().anchorHref())
        self.assertEqual(url, QUrl.fromLocalFile(str(path)))
        self.assert_playback(host, lambda: document.anchorClicked.emit(url))

    def test_saved_webm_plays_after_original_is_removed(self):
        path = self.project / '操作录制.WebM'
        path.write_bytes(recording('.webm'))
        item = Attachment.from_file(path)
        seq, _ = self.engine.submit(self.sid, '', new_id(), attachments=[item])
        path.unlink()
        row = self.message(seq)
        play, = [b for b in row.findChildren(QPushButton) if b.accessibleName() == '视频 · ' + path.name]
        self.assert_playback(row, play.click)

    def test_message_and_markdown_artifact_open_local_mp4(self):
        path = self.project / '项目 演示.MP4'
        path.write_bytes(recording('.mp4'))
        self.engine.submit(self.sid, '查看本地录像。', new_id())
        with self.store.db:
            seq = self.store.event(self.sid, 'message', 'openai', f'[查看录像](<{path.name}>)')
        row = self.message(seq)
        self.open_link(row, row.body, path)
        dialog = self.report('播放说明.md', f'[查看录像]({path.as_uri()})')
        self.open_link(dialog, dialog.findChild(Document), path)

    def test_named_mov_and_plain_text_artifact(self):
        path = self.project / '工程录制.mov'
        data = recording('.mov')
        path.write_bytes(data)
        self.engine.submit(self.sid, '保存录像。', new_id())
        for name, content in ((path.name, ''), ('录像清单.txt', f'[查看录像]({path.as_uri()})')):
            with self.subTest(name=name):
                dialog = self.report(name, content)
                play, = [b for b in dialog.findChildren(QPushButton) if b.text() == '播放 ' + path.name]
                self.assert_playback(dialog, play.click)
                output = self.root / name
                dialog.export(str(output))
                self.assertEqual(output.read_bytes(), data if name == path.name else content.encode())
                dialog.close()
