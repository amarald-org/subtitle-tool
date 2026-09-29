"""Desktop editor: video on the left, editable dual-language cue list on the right.

Run with `subtitle-tool gui [movie]`. Playback uses Qt Multimedia, which ships its
own FFmpeg inside the PySide6 wheels, so no separate player install is needed.
"""

from __future__ import annotations

import bisect
import html
import os
import re
import sys
import tempfile
import traceback
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import srt
from PySide6.QtCore import (
    QEvent,
    QObject,
    QRectF,
    QRunnable,
    QSettings,
    Qt,
    QThreadPool,
    QUrl,
    Signal,
)
from PySide6.QtGui import QAction, QBrush, QColor, QKeySequence, QPalette
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from . import default_backend, find_local_subs, guess_title, read_text, write_translation_outputs

VIDEO_FILTER = "Video (*.mkv *.mp4 *.m4v *.avi *.mov *.webm);;All files (*)"
SUB_FILTER = "Subtitles (*.srt);;All files (*)"
COL_START, COL_END, COL_TOP, COL_BOTTOM = range(4)
KEY_SETTINGS = ("OPENSUBTITLES_API_KEY", "OPENSUBTITLES_USERNAME", "OPENSUBTITLES_PASSWORD", "DEEPL_API_KEY")


@dataclass
class Cue:
    start: int  # ms
    end: int  # ms
    top: str  # source language (English)
    bottom: str = ""  # translation


def fmt_ms(ms: int) -> str:
    return srt.timedelta_to_srt_timestamp(timedelta(milliseconds=max(ms, 0)))


def parse_ms(text: str) -> int:
    return int(srt.srt_timestamp_to_timedelta(text.strip()).total_seconds() * 1000)


def parse_srt(text: str) -> list[Cue]:
    return [
        Cue(int(s.start.total_seconds() * 1000), int(s.end.total_seconds() * 1000), s.content.strip())
        for s in srt.sort_and_reindex(srt.parse(text, ignore_errors=True))
    ]


def cues_to_srt(cues: list[Cue], bottom: bool = False) -> list[srt.Subtitle]:
    return [
        srt.Subtitle(
            i + 1,
            timedelta(milliseconds=c.start),
            timedelta(milliseconds=c.end),
            c.bottom if bottom else c.top,
        )
        for i, c in enumerate(cues)
    ]


TAG_RE = re.compile(r"</?[ibu]>|</?font[^>]*>|\{\\[^}]*\}")


def plain(text: str) -> str:
    """Text as shown in the list: SRT formatting tags hidden."""
    return TAG_RE.sub("", text)


class MultilineDelegate(QStyledItemDelegate):
    """Edit cue text in a multi-line box. Enter saves, Shift+Enter adds a line."""

    def createEditor(self, parent, option, index):
        editor = QPlainTextEdit(parent)
        editor.setTabChangesFocus(True)
        return editor

    def setEditorData(self, editor, index):
        editor.setPlainText(index.data() or "")

    def setModelData(self, editor, model, index):
        model.setData(index, editor.toPlainText().strip())

    def updateEditorGeometry(self, editor, option, index):
        rect = option.rect
        rect.setHeight(max(rect.height(), 70))
        editor.setGeometry(rect)

    def eventFilter(self, editor, event):
        if (
            event.type() == QEvent.KeyPress
            and event.key() in (Qt.Key_Return, Qt.Key_Enter)
            and not event.modifiers() & Qt.ShiftModifier
        ):
            self.commitData.emit(editor)
            self.closeEditor.emit(editor)
            return True
        return super().eventFilter(editor, event)


# --- background jobs --------------------------------------------------------


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(str)


class Job(QRunnable):
    """Run fn(progress) on the thread pool; results come back on the GUI thread."""

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = _Signals()

    def run(self):
        try:
            self.signals.done.emit(self.fn(self.signals.progress.emit))
        except Exception as e:  # shown to the user in a dialog
            traceback.print_exc()
            self.signals.failed.emit(f"{type(e).__name__}: {e}")


# --- video with subtitle overlay ----------------------------------------------


class VideoView(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(Qt.black)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setMinimumSize(320, 180)
        self.video = QGraphicsVideoItem()
        self.scene().addItem(self.video)
        self.box = QGraphicsRectItem()
        self.box.setBrush(QColor(0, 0, 0, 150))
        self.box.setPen(Qt.NoPen)
        self.scene().addItem(self.box)
        self.text = QGraphicsTextItem()
        self.scene().addItem(self.text)
        self.video.nativeSizeChanged.connect(lambda _: self._layout())
        self._top = self._bottom = ""

    def set_subtitle(self, top: str, bottom: str) -> None:
        if (top, bottom) == (self._top, self._bottom):
            return
        self._top, self._bottom = top, bottom
        self._layout()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout()

    def _layout(self) -> None:
        w, h = self.viewport().width(), self.viewport().height()
        self.scene().setSceneRect(0, 0, w, h)
        self.video.setSize(self.viewport().size())
        px = max(12, int(h / 20))
        parts = []
        if self._top:
            parts.append(f'<div style="color:#ffffff">{_html(self._top)}</div>')
        if self._bottom:
            parts.append(f'<div style="color:#ffe066; font-style:italic">{_html(self._bottom)}</div>')
        self.text.setHtml(
            f'<div align="center" style="font-size:{px}px; font-family:Helvetica, Arial">'
            + "".join(parts)
            + "</div>"
        )
        self.text.setTextWidth(w * 0.9)
        br = self.text.boundingRect()
        self.text.setPos((w - br.width()) / 2, h - br.height() - h * 0.04)
        visible = bool(parts)
        self.text.setVisible(visible)
        self.box.setVisible(visible)
        if visible:
            self.box.setRect(QRectF(self.text.pos(), br.size()).adjusted(-8, 0, 8, 0))


def _html(text: str) -> str:
    # Keep <i>/<b> from SRT, escape everything else.
    out = html.escape(text)
    for tag in ("i", "b", "u"):
        out = out.replace(f"&lt;{tag}&gt;", f"<{tag}>").replace(f"&lt;/{tag}&gt;", f"</{tag}>")
    return out.replace("\n", "<br>")


# --- dialogs -------------------------------------------------------------------


class KeysDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("API keys")
        form = QFormLayout(self)
        self.fields = {}
        for key in KEY_SETTINGS:
            edit = QLineEdit(os.environ.get(key, ""))
            if "PASSWORD" in key or "KEY" in key:
                edit.setEchoMode(QLineEdit.PasswordEchoOnEdit)
            self.fields[key] = edit
            form.addRow(key.replace("_", " ").title(), edit)
        form.addRow(QLabel(
            'Free keys: <a href="https://www.opensubtitles.com/consumers">OpenSubtitles</a>, '
            '<a href="https://www.deepl.com/pro-api">DeepL</a>',
            openExternalLinks=True,
        ))
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def save(self) -> None:
        settings = QSettings()
        for key, edit in self.fields.items():
            value = edit.text().strip()
            settings.setValue(key, value)
            if value:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)


def load_saved_keys() -> None:
    """Keys from the environment win; otherwise use the ones saved in the app."""
    settings = QSettings()
    for key in KEY_SETTINGS:
        value = settings.value(key, "")
        if value and not os.environ.get(key):
            os.environ[key] = value


# --- main window -----------------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.resize(1400, 800)
        self.pool = QThreadPool.globalInstance()
        self.video_path: Path | None = None
        self.cues: list[Cue] = []
        self.starts: list[int] = []
        self.current = -1
        self.dirty = False
        self._filling = False
        self._busy_jobs: list[Job] = []

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)

        self.view = VideoView()
        self.player.setVideoOutput(self.view.video)

        self.play_btn = QPushButton()
        self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.play_btn.clicked.connect(self.toggle_play)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.sliderMoved.connect(self.player.setPosition)
        self.time_label = QLabel("00:00:00 / 00:00:00")
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setMaximumWidth(90)
        self.volume.valueChanged.connect(lambda v: self.audio.setVolume(v / 100))
        self.audio.setVolume(0.8)

        controls = QHBoxLayout()
        controls.addWidget(self.play_btn)
        controls.addWidget(self.slider, 1)
        controls.addWidget(self.time_label)
        controls.addWidget(QLabel("🔊"))
        controls.addWidget(self.volume)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(self.view, 1)
        lv.addLayout(controls)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Start", "End", "English", "Translation"])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(COL_START, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(COL_END, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(COL_TOP, QHeaderView.Stretch)
        hdr.setSectionResizeMode(COL_BOTTOM, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(True)
        self.table.setTextElideMode(Qt.ElideNone)
        hdr.sectionResized.connect(lambda *_: self.table.resizeRowsToContents())
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.table.setItemDelegateForColumn(COL_TOP, MultilineDelegate(self.table))
        self.table.setItemDelegateForColumn(COL_BOTTOM, MultilineDelegate(self.table))
        self.table.cellClicked.connect(self.on_row_clicked)
        self.table.itemChanged.connect(self.on_item_changed)
        self.follow = QCheckBox("Follow playback")
        self.follow.setChecked(True)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(self.table, 1)
        rv.addWidget(self.follow)

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([800, 600])
        self.setCentralWidget(split)

        dark = self.palette().color(QPalette.Base).lightness() < 128
        self.bottom_color = QColor("#ffd84d" if dark else "#9a6b00")
        self.current_bg = QColor(80, 120, 200, 90)

        self._build_toolbar()
        self.player.positionChanged.connect(self.on_position)
        self.player.durationChanged.connect(lambda d: self.slider.setRange(0, d))
        self.player.playbackStateChanged.connect(self._update_play_icon)
        self.player.errorOccurred.connect(lambda _e, msg: self.statusBar().showMessage(f"Player: {msg}"))
        QApplication.instance().installEventFilter(self)
        self._update_title()

    # toolbar & menus

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)

        def act(text, slot, shortcut=None, tip=None):
            a = QAction(text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            if tip:
                a.setToolTip(tip)
            tb.addAction(a)
            return a

        act("Open movie", self.open_video_dialog, QKeySequence.Open)
        act("Open subtitles", self.open_subs_dialog, tip="Load an English .srt")
        act("Open translation", self.open_translation_dialog, tip="Load a translated .srt into the right column")
        tb.addSeparator()
        act("Search", self.search, tip="Find subtitles on OpenSubtitles")
        act("Sync", self.sync, tip="Align subtitle timing to the movie's audio (ffsubsync)")
        act("Shift…", self.shift, tip="Move all subtitles earlier or later")
        tb.addSeparator()
        self.backend = QComboBox()
        self.backend.addItems(["deepl", "google", "argos"])
        self.backend.setCurrentText(default_backend())
        self.backend.setToolTip("Translation service")
        tb.addWidget(self.backend)
        self.target = QLineEdit("fi")
        self.target.setMaximumWidth(40)
        self.target.setToolTip("Target language code (fi, sv, de, …)")
        tb.addWidget(self.target)
        act("Translate", self.translate, tip="Translate rows whose translation is empty")
        tb.addSeparator()
        act("Save", self.save, QKeySequence.Save)
        act("Keys…", self.edit_keys, tip="OpenSubtitles and DeepL API keys")

    # file loading

    def open_video_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open movie", str(Path.home() / "Movies"), VIDEO_FILTER)
        if path:
            self.open_video(Path(path))

    def open_video(self, path: Path) -> None:
        if not self.confirm_discard():
            return
        self.video_path = path
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.set_cues([])
        target = self.target.text().strip() or "fi"
        top = find_local_subs(path, "en")
        if top:
            self.set_cues(parse_srt(read_text(top)))
            self.statusBar().showMessage(f"Loaded {top.name}")
            bottom = path.with_name(f"{path.stem}.{target}.srt")
            if bottom.exists():
                self.merge_translation(parse_srt(read_text(bottom)))
        else:
            self.statusBar().showMessage("No subtitles next to the movie. Use Search or Open subtitles.")
        self.dirty = False
        self._update_title()

    def open_subs_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open subtitles", self._dir(), SUB_FILTER)
        if path:
            old = self.cues
            self.set_cues(parse_srt(read_text(Path(path))))
            if any(c.bottom for c in old) and len(old) == len(self.cues):
                for c, o in zip(self.cues, old):
                    c.bottom = o.bottom
                self.refresh_table()
            self.mark_dirty()

    def open_translation_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open translation", self._dir(), SUB_FILTER)
        if path:
            self.merge_translation(parse_srt(read_text(Path(path))))
            self.mark_dirty()

    def merge_translation(self, other: list[Cue]) -> None:
        """Pair each cue with the translated cue that overlaps it the most."""
        if len(other) == len(self.cues):
            for c, o in zip(self.cues, other):
                c.bottom = o.top
        else:
            j = 0
            for c in self.cues:
                while j < len(other) and other[j].end <= c.start:
                    j += 1
                best, overlap = "", 0
                k = j
                while k < len(other) and other[k].start < c.end:
                    ov = min(c.end, other[k].end) - max(c.start, other[k].start)
                    if ov > overlap:
                        best, overlap = other[k].top, ov
                    k += 1
                c.bottom = best
        self.refresh_table()

    def _dir(self) -> str:
        return str(self.video_path.parent) if self.video_path else str(Path.home())

    # table

    def set_cues(self, cues: list[Cue]) -> None:
        self.cues = cues
        self.current = -1
        self.refresh_table()

    def refresh_table(self) -> None:
        self._filling = True
        self.table.setRowCount(len(self.cues))
        for row, c in enumerate(self.cues):
            for col, text in enumerate((fmt_ms(c.start), fmt_ms(c.end), plain(c.top), plain(c.bottom))):
                item = QTableWidgetItem(text)
                if col == COL_BOTTOM:
                    item.setForeground(QBrush(self.bottom_color))
                self.table.setItem(row, col, item)
        self.table.resizeRowsToContents()
        self._filling = False
        self.starts = [c.start for c in self.cues]
        self.current = -1
        self.on_position(self.player.position())

    def on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._filling:
            return
        row, col = item.row(), item.column()
        cue = self.cues[row]
        text = item.text()
        if col in (COL_START, COL_END):
            try:
                ms = parse_ms(text)
            except Exception:
                self._set_cell(row, col, fmt_ms(cue.start if col == COL_START else cue.end))
                self.statusBar().showMessage("Use the format 00:01:23,456")
                return
            if col == COL_START:
                cue.start = ms
            else:
                cue.end = ms
            self._set_cell(row, col, fmt_ms(ms))
            self.starts = [c.start for c in self.cues]
        elif col == COL_TOP:
            if text != plain(cue.top):  # untouched rows keep their italics
                cue.top = text
        else:
            if text != plain(cue.bottom):
                cue.bottom = text
        self.table.resizeRowToContents(row)
        self.mark_dirty()
        self.current = -1
        self.on_position(self.player.position())

    def _set_cell(self, row: int, col: int, text: str) -> None:
        self._filling = True
        self.table.item(row, col).setText(text)
        self._filling = False

    def on_row_clicked(self, row: int, _col: int) -> None:
        if 0 <= row < len(self.cues):
            self.player.setPosition(self.cues[row].start)

    # playback

    def toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _update_play_icon(self) -> None:
        playing = self.player.playbackState() == QMediaPlayer.PlayingState
        self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPause if playing else QStyle.SP_MediaPlay))

    def on_position(self, pos: int) -> None:
        if not self.slider.isSliderDown():
            self.slider.setValue(pos)
        self.time_label.setText(f"{fmt_ms(pos)[:8]} / {fmt_ms(self.player.duration())[:8]}")
        i = bisect.bisect_right(self.starts, pos) - 1
        active = i if 0 <= i < len(self.cues) and pos < self.cues[i].end else -1
        if active >= 0:
            self.view.set_subtitle(self.cues[active].top, self.cues[active].bottom)
        else:
            self.view.set_subtitle("", "")
        if active != self.current:
            self._highlight(self.current, False)
            self._highlight(active, True)
            self.current = active
            if active >= 0 and self.follow.isChecked() and self.table.state() != QAbstractItemView.EditingState:
                self.table.scrollToItem(self.table.item(active, COL_TOP), QAbstractItemView.PositionAtCenter)

    def _highlight(self, row: int, on: bool) -> None:
        if not (0 <= row < self.table.rowCount()):
            return
        self._filling = True
        for col in range(4):
            item = self.table.item(row, col)
            if item:
                item.setBackground(QBrush(self.current_bg) if on else QBrush())
        self._filling = False

    def eventFilter(self, obj, event):
        # Space toggles playback unless the user is typing somewhere.
        if event.type() == QEvent.KeyPress and event.key() == Qt.Key_Space and self.isActiveWindow():
            focus = QApplication.focusWidget()
            if not isinstance(focus, (QLineEdit, QTextEdit, QPlainTextEdit)) and \
                    self.table.state() != QAbstractItemView.EditingState:
                self.toggle_play()
                return True
        return super().eventFilter(obj, event)

    # actions

    def run_job(self, label: str, fn, on_done) -> None:
        self.statusBar().showMessage(label)
        QApplication.setOverrideCursor(Qt.BusyCursor)
        job = Job(fn)
        self._busy_jobs.append(job)

        def finish():
            QApplication.restoreOverrideCursor()
            self._busy_jobs.remove(job)

        def done(result):
            finish()
            on_done(result)

        def failed(msg):
            finish()
            self.statusBar().showMessage("Failed")
            QMessageBox.warning(self, "Something went wrong", msg)

        job.signals.done.connect(done)
        job.signals.failed.connect(failed)
        job.signals.progress.connect(self.statusBar().showMessage)
        self.pool.start(job)

    def search(self) -> None:
        title, ok = QInputDialog.getText(
            self, "Search OpenSubtitles", "Movie name:",
            text=guess_title(self.video_path) if self.video_path else "",
        )
        if not ok or not title.strip():
            return
        video = self.video_path

        def work(_progress):
            from .opensubtitles import Client

            client = Client()
            return client, client.search(title.strip(), language="en", video=video)

        def done(result):
            client, hits = result
            if not hits:
                QMessageBox.information(self, "Search", "No subtitles found. Try another name.")
                return
            dlg = QDialog(self)
            dlg.setWindowTitle("Pick subtitles")
            dlg.resize(700, 400)
            lay = QVBoxLayout(dlg)
            lst = QListWidget()
            lst.addItems([h.label() for h in hits[:30]])
            lst.setCurrentRow(0)
            lst.itemDoubleClicked.connect(dlg.accept)
            lay.addWidget(lst)
            buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            buttons.accepted.connect(dlg.accept)
            buttons.rejected.connect(dlg.reject)
            lay.addWidget(buttons)
            if dlg.exec() != QDialog.Accepted or lst.currentRow() < 0:
                return
            hit = hits[lst.currentRow()]
            self.run_job(
                "Downloading…",
                lambda _p: client.download(hit.file_id),
                lambda text: self._loaded_download(text, hit.label()),
            )

        self.run_job("Searching…", work, done)

    def _loaded_download(self, text: str, label: str) -> None:
        self.set_cues(parse_srt(text))
        self.mark_dirty()
        self.statusBar().showMessage(f"Loaded {label}. Press Sync to match it to the audio.")

    def sync(self) -> None:
        if not (self.video_path and self.cues):
            QMessageBox.information(self, "Sync", "Open a movie and subtitles first.")
            return
        video, cues = self.video_path, self.cues

        def work(_progress):
            from .sync import sync_to_video

            with tempfile.TemporaryDirectory() as tmp:
                src, out = Path(tmp) / "in.srt", Path(tmp) / "out.srt"
                src.write_text(srt.compose(cues_to_srt(cues)), encoding="utf-8")
                sync_to_video(video, src, out)
                return parse_srt(out.read_text(encoding="utf-8"))

        def done(synced: list[Cue]):
            if len(synced) == len(self.cues):
                for c, s in zip(self.cues, synced):
                    c.start, c.end = s.start, s.end
                self.refresh_table()
            else:
                self.set_cues(synced)
            self.mark_dirty()
            self.statusBar().showMessage("Synced to the audio.")

        self.run_job("Syncing to the audio… (about half a minute)", work, done)

    def shift(self) -> None:
        secs, ok = QInputDialog.getDouble(
            self, "Shift subtitles", "Seconds (negative = earlier):", 0.0, -3600, 3600, 2
        )
        if ok and secs:
            delta = int(secs * 1000)
            for c in self.cues:
                c.start, c.end = max(0, c.start + delta), max(0, c.end + delta)
            self.refresh_table()
            self.mark_dirty()

    def translate(self) -> None:
        rows = [i for i, c in enumerate(self.cues) if not c.bottom.strip()]
        if not rows:
            QMessageBox.information(
                self, "Translate", "Every row already has a translation. Clear a cell to re-translate it."
            )
            return
        texts = [self.cues[i].top for i in rows]
        backend, target = self.backend.currentText(), self.target.text().strip() or "fi"

        def work(progress):
            from .translate import translate_lines

            return translate_lines(
                texts, target=target, backend=backend,
                progress=lambda d, t: progress(f"Translating… {d}/{t}"),
            )

        def done(result: list[str]):
            for i, t in zip(rows, result):
                self.cues[i].bottom = t
            self.refresh_table()
            self.mark_dirty()
            self.statusBar().showMessage(f"Translated {len(rows)} lines.")

        self.run_job(f"Translating {len(rows)} lines with {backend}…", work, done)

    def save(self) -> bool:
        if not self.cues:
            return True
        if self.video_path:
            base = self.video_path.with_suffix("")
        else:
            path, _ = QFileDialog.getSaveFileName(self, "Save subtitles", self._dir(), SUB_FILTER)
            if not path:
                return False
            base = Path(path).with_suffix("")
        top_path = Path(f"{base}.en.srt")
        top_path.write_text(srt.compose(cues_to_srt(self.cues)), encoding="utf-8")
        written = [top_path]
        if any(c.bottom for c in self.cues):
            target = self.target.text().strip() or "fi"
            written += write_translation_outputs(
                base, "en", target, cues_to_srt(self.cues), [c.bottom for c in self.cues]
            )
        self.dirty = False
        self._update_title()
        self.statusBar().showMessage("Saved " + ", ".join(p.name for p in written))
        return True

    def edit_keys(self) -> None:
        dlg = KeysDialog(self)
        if dlg.exec() == QDialog.Accepted:
            dlg.save()
            self.backend.setCurrentText(default_backend())

    # window state

    def mark_dirty(self) -> None:
        self.dirty = True
        self._update_title()

    def _update_title(self) -> None:
        name = self.video_path.name if self.video_path else "No movie"
        self.setWindowTitle(f"{'• ' if self.dirty else ''}{name} — Subtitle Tool")

    def confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        answer = QMessageBox.question(
            self, "Unsaved changes", "Save your subtitle changes first?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
        )
        if answer == QMessageBox.Save:
            return self.save()
        return answer == QMessageBox.Discard

    def closeEvent(self, event):
        if self.confirm_discard():
            self.player.stop()
            event.accept()
        else:
            event.ignore()


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setOrganizationName("subtitle-tool")
    app.setApplicationName("Subtitle Tool")
    load_saved_keys()
    win = MainWindow()
    win.show()
    if argv:
        win.open_video(Path(argv[0]).expanduser())
    app.exec()


if __name__ == "__main__":
    main()
