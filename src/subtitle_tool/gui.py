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
from dataclasses import dataclass, replace
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
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QIcon, QKeySequence, QPalette
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
    QSizePolicy,
    QSlider,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import default_backend, find_local_subs, guess_title, read_text, write_translation_outputs

VIDEO_FILTER = "Video or audio (*.mkv *.mp4 *.m4v *.avi *.mov *.webm *.mp3 *.m4a *.wav *.flac *.ogg);;All files (*)"
SUB_FILTER = "Subtitles (*.srt);;All files (*)"
COL_START, COL_END, COL_TOP, COL_BOTTOM = range(4)
KEY_LABELS = {
    "DEEPL_API_KEY": "DeepL API key",
    "OPENSUBTITLES_API_KEY": "OpenSubtitles API key",
    "OPENSUBTITLES_USERNAME": "OpenSubtitles username",
    "OPENSUBTITLES_PASSWORD": "OpenSubtitles password",
    "HF_TOKEN": "Hugging Face token (optional)",
}
KEY_SETTINGS = tuple(KEY_LABELS)


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

    active: QPlainTextEdit | None = None

    def createEditor(self, parent, option, index):
        editor = QPlainTextEdit(parent)
        editor.setTabChangesFocus(True)
        self.active = editor
        editor.destroyed.connect(lambda *_: setattr(self, "active", None))
        return editor

    def commit_active(self) -> None:
        if self.active is not None:
            editor, self.active = self.active, None
            self.commitData.emit(editor)
            self.closeEditor.emit(editor)

    def setEditorData(self, editor, index):
        # Only fill once: playback highlighting touches the row while you type,
        # and Qt would otherwise reset the editor to the old text.
        if not editor.property("filled"):
            editor.setProperty("filled", True)
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
    progress = Signal(object)  # a status string, or partial results for on_partial


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


def msg_box(icon, parent, title: str, text: str, buttons=None):
    """QMessageBox drawn by Qt, not macOS: the native alert crashes on macOS 27
    while rendering its icon (CoreUI vector glyph exception)."""
    box = QMessageBox(icon, title, text, parent=parent)
    box.setOption(QMessageBox.Option.DontUseNativeDialog, True)
    if buttons is None:
        buttons = QMessageBox.Yes | QMessageBox.No if icon == QMessageBox.Question else QMessageBox.Ok
    box.setStandardButtons(buttons)
    box.exec()
    return box.standardButton(box.clickedButton())


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
            form.addRow(KEY_LABELS[key], edit)
        form.addRow(QLabel(
            'Free keys: <a href="https://www.deepl.com/pro-api">DeepL</a> (translation), '
            '<a href="https://www.opensubtitles.com/consumers">OpenSubtitles</a> (search), '
            '<a href="https://huggingface.co/settings/tokens">Hugging Face</a> '
            "(only makes Whisper downloads faster)",
            openExternalLinks=True, wordWrap=True,
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

LANGUAGES = [
    ("fi", "Finnish"), ("en", "English"), ("sv", "Swedish"), ("et", "Estonian"),
    ("de", "German"), ("fr", "French"), ("es", "Spanish"), ("it", "Italian"),
    ("pt", "Portuguese"), ("nl", "Dutch"), ("da", "Danish"), ("nb", "Norwegian"),
    ("pl", "Polish"), ("ru", "Russian"), ("uk", "Ukrainian"), ("tr", "Turkish"),
    ("el", "Greek"), ("cs", "Czech"), ("hu", "Hungarian"), ("ja", "Japanese"),
    ("ko", "Korean"), ("zh", "Chinese"), ("ar", "Arabic"), ("id", "Indonesian"),
]
MAC = sys.platform == "darwin"

TOOLBAR_STYLE = """
QToolBar { spacing: 3px; padding: 3px; }
QToolButton { padding: 4px 9px; border: 1px solid transparent; border-radius: 6px; }
QToolButton:hover { background: rgba(128, 128, 128, 0.22); border-color: rgba(128, 128, 128, 0.35); }
QToolButton:pressed { background: rgba(70, 130, 230, 0.45); border-color: rgba(70, 130, 230, 0.8); }
QToolButton:disabled { color: rgba(128, 128, 128, 0.6); }
"""


def shortcuts(*keys) -> list[QKeySequence]:
    """Standard keys plus the literal Ctrl combos on macOS (where Qt's Ctrl means Cmd)."""
    seqs, seen = [], set()
    for k in keys:
        if k is None:
            continue
        seq = QKeySequence(k)
        name = seq.toString()
        if name and name not in seen:
            seen.add(name)
            seqs.append(seq)
    return seqs


def lang_combo(default: str, auto_label: str | None = None) -> QComboBox:
    box = QComboBox()
    box.setEditable(True)
    if auto_label:
        box.addItem(auto_label)
    for code, name in LANGUAGES:
        box.addItem(f"{code} – {name}")
    if default:
        box.setCurrentIndex(box.findText(default, Qt.MatchStartsWith))
    box.setToolTip("Pick a language or type any language code")
    return box


def combo_code(box: QComboBox) -> str | None:
    text = box.currentText().strip()
    if not text or text.lower().startswith("auto"):
        return None
    return text.split()[0].lower()


def deepl_missing(backend: str) -> bool:
    return backend == "deepl" and not os.environ.get("DEEPL_API_KEY")


class ModelsDialog(QDialog):
    """Downloaded Whisper models, with a way to free the disk space."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Downloaded Whisper models")
        self.resize(460, 260)
        self.lay = QVBoxLayout(self)
        self.list = QListWidget()
        self.lay.addWidget(self.list)
        row = QHBoxLayout()
        delete = QPushButton("Delete selected")
        delete.clicked.connect(self.delete)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(delete)
        row.addStretch(1)
        row.addWidget(close)
        self.lay.addLayout(row)
        self.reload()

    def reload(self) -> None:
        from .transcribe import downloaded_models

        self.list.clear()
        self.models = downloaded_models()
        for name, _repo, size in self.models:
            self.list.addItem(f"{name}  –  {size / 1e9:.2f} GB")
        if not self.models:
            self.list.addItem("Nothing downloaded yet.")

    def delete(self) -> None:
        from .transcribe import delete_model

        row = self.list.currentRow()
        if 0 <= row < len(self.models):
            delete_model(self.models[row][1])
            self.reload()


class AutoDialog(QDialog):
    def __init__(self, parent, target: str, backend: str):
        super().__init__(parent)
        self.setWindowTitle("Automatic subtitles")
        self.backend = backend
        form = QFormLayout(self)
        form.addRow(QLabel(
            "Listens to the audio with Whisper (runs on this computer, nothing is uploaded)\n"
            "and writes subtitles with timestamps. The list fills in as it goes."
        ))
        self.model = QComboBox()
        form.addRow("Accuracy", self.model)
        manage = QPushButton("Manage downloads…")
        manage.clicked.connect(self.manage)
        form.addRow("", manage)
        self._fill_models(QSettings().value("auto/model_index", 2, type=int))
        self.lang = lang_combo("", auto_label="Auto-detect")
        form.addRow("Spoken language", self.lang)
        self.music = QCheckBox("Song / music video")
        self.music.setToolTip(
            "Normally Whisper skips parts without speech, which can also skip singing over music.\n"
            "Tick this for songs so sung parts are kept."
        )
        form.addRow(self.music)
        form.addRow(QLabel("   Tick for songs: keeps the sung parts instead of skipping them as music."))
        self.translate = QCheckBox(f"Also translate to '{target}' with {backend}")
        self.translate.setChecked(True)
        form.addRow(self.translate)
        self.key_row = QWidget()
        kr = QHBoxLayout(self.key_row)
        kr.setContentsMargins(0, 0, 0, 0)
        kr.addWidget(QLabel("⚠️ DeepL needs a free API key to translate."))
        add_key = QPushButton("Add key…")
        add_key.clicked.connect(self.add_key)
        kr.addWidget(add_key)
        kr.addStretch(1)
        form.addRow(self.key_row)
        self.key_row.setVisible(deepl_missing(backend))
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Start")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _fill_models(self, index: int) -> None:
        try:
            from .transcribe import downloaded_models

            have = {m[0] for m in downloaded_models()}
        except Exception:
            have = set()
        self.model.clear()
        for name, note in [
            ("tiny", "fastest, rough, 75 MB"), ("base", "fast, 145 MB"), ("small", "good, 480 MB"),
            ("medium", "better, 1.5 GB"), ("large-v3-turbo", "best, 1.6 GB"),
        ]:
            mark = "  ✓ downloaded" if name in have else ""
            self.model.addItem(f"{name} – {note}{mark}", name)
        self.model.setCurrentIndex(index)

    def manage(self) -> None:
        ModelsDialog(self).exec()
        self._fill_models(self.model.currentIndex())

    def add_key(self) -> None:
        dlg = KeysDialog(self)
        if dlg.exec() == QDialog.Accepted:
            dlg.save()
        self.key_row.setVisible(deepl_missing(self.backend))

    def accept(self):
        QSettings().setValue("auto/model_index", self.model.currentIndex())
        super().accept()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.resize(1400, 820)
        self.pool = QThreadPool.globalInstance()
        self.video_path: Path | None = None
        self.cues: list[Cue] = []
        self.starts: list[int] = []
        self.source_lang = "en"
        self.current = -1
        self.dirty = False
        self.open_cue: Cue | None = None  # cue started with "Set start", waiting for its end
        self.undo_stack: list[tuple[list[Cue], str]] = []
        self.redo_stack: list[tuple[list[Cue], str]] = []
        self._filling = False
        self._busy_jobs: list[Job] = []

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)

        self.view = VideoView()
        self.player.setVideoOutput(self.view.video)

        self.play_btn = QPushButton()
        self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.play_btn.setToolTip("Play / pause (Space)")
        self.play_btn.clicked.connect(self.toggle_play)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.sliderMoved.connect(self.player.setPosition)
        self.time_label = QLabel("00:00:00.000 / 00:00:00")
        self.time_label.setFont(QFont("Menlo" if sys.platform == "darwin" else "Consolas", 11))
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setMaximumWidth(90)
        self.volume.valueChanged.connect(lambda v: self.audio.setVolume(v / 100))
        self.audio.setVolume(0.8)

        controls = QHBoxLayout()
        mod = "⌘" if MAC else "Ctrl+"
        seek_buttons = []
        for label, ms, keys, tip in [
            ("−5s", -5000, ("Ctrl+Shift+J", "Ctrl+Shift+Left"), f"{mod}⇧J"),
            ("−1s", -1000, ("Ctrl+J", "Ctrl+Left"), f"{mod}J"),
            ("−0.2s", -200, ("Ctrl+Alt+J", "Alt+Left"), f"{mod}⌥J"),
            ("+0.2s", 200, ("Ctrl+Alt+L", "Alt+Right"), f"{mod}⌥L"),
            ("+1s", 1000, ("Ctrl+L", "Ctrl+Right"), f"{mod}L"),
            ("+5s", 5000, ("Ctrl+Shift+L", "Ctrl+Shift+Right"), f"{mod}⇧L"),
        ]:
            a = self._action(label, lambda _=False, d=ms: self.seek_by(d), shortcuts(*keys))
            btn = QToolButton()
            btn.setDefaultAction(a)
            btn.setToolTip(f"{'Back' if ms < 0 else 'Forward'} {abs(ms) / 1000:g} s ({tip})")
            btn.setStyleSheet(TOOLBAR_STYLE)
            seek_buttons.append(btn)
        controls.addWidget(self.play_btn)
        controls.addWidget(self.slider, 1)
        controls.addWidget(self.time_label)
        controls.addWidget(QLabel("🔊"))
        controls.addWidget(self.volume)

        # Second row: fine seeking, and retiming the selected line to "now".
        seek_row = QHBoxLayout()
        for btn in seek_buttons[:3]:
            seek_row.addWidget(btn)
        seek_row.addSpacing(12)
        for btn in seek_buttons[3:]:
            seek_row.addWidget(btn)
        seek_row.addStretch(1)
        for label, slot, key, tip in [
            ("⇤ Start = now", self.retime_start, "Ctrl+Shift+B", "Set the selected line's start to now"),
            ("End = now ⇥", self.set_end, "Ctrl+Shift+E", "Set the selected line's end to now"),
        ]:
            btn = QToolButton()
            btn.setDefaultAction(self._action(label, slot, shortcuts(key)))
            btn.setToolTip(f"{tip} ({mod}{key.split('+', 1)[1].replace('Shift+', '⇧')})")
            btn.setStyleSheet(TOOLBAR_STYLE)
            seek_row.addWidget(btn)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(2)
        lv.addWidget(self.view, 1)
        lv.addLayout(controls)
        lv.addLayout(seek_row)

        self.table = QTableWidget(0, 4)
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
        self.text_delegates = [MultilineDelegate(self.table), MultilineDelegate(self.table)]
        self.table.setItemDelegateForColumn(COL_TOP, self.text_delegates[0])
        self.table.setItemDelegateForColumn(COL_BOTTOM, self.text_delegates[1])
        self.table.cellClicked.connect(self.on_row_clicked)
        self.table.itemChanged.connect(self.on_item_changed)

        # Manual timing row under the list.
        self.timing_bar = QToolBar()
        self.timing_bar.setStyleSheet(TOOLBAR_STYLE)
        mod = "⌘" if MAC else "Ctrl+"
        self.start_act = self._action(
            "⏺ Set start", self.set_start, shortcuts("Ctrl+B"),
            f"New subtitle starting now, then type the text ({mod}B)", self.timing_bar,
        )
        self.end_act = self._action(
            "⏹ Set end", self.set_end, shortcuts("Ctrl+E"),
            f"End the subtitle here ({mod}E)", self.timing_bar,
        )
        del_act = self._action(
            "Delete row", self.delete_rows, shortcuts(QKeySequence.Delete, "Backspace"),
            "Delete the selected subtitles (Delete)", self.timing_bar,
        )
        del_act.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self.table.addAction(del_act)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.timing_bar.addWidget(spacer)
        self.follow = QCheckBox("Follow playback")
        self.follow.setChecked(True)
        self.timing_bar.addWidget(self.follow)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(self.table, 1)
        rv.addWidget(self.timing_bar)

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
        self._build_menu()
        self._update_headers()
        self.player.positionChanged.connect(self.on_position)
        self.player.durationChanged.connect(lambda d: self.slider.setRange(0, d))
        self.player.playbackStateChanged.connect(self._update_play_icon)
        self.player.errorOccurred.connect(lambda _e, msg: self.statusBar().showMessage(f"Player: {msg}"))
        QApplication.instance().installEventFilter(self)
        self._update_undo_actions()
        self._update_title()

    # toolbar & menus

    def _action(self, text, slot, keys=None, tip=None, bar=None) -> QAction:
        a = QAction(text, self)
        a.triggered.connect(slot)
        if keys:
            a.setShortcuts(keys)
        if tip:
            a.setToolTip(tip)
        if bar is not None:
            bar.addAction(a)
        self.addAction(a)
        return a

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setStyleSheet(TOOLBAR_STYLE)
        self.addToolBar(tb)
        mod = "⌘" if MAC else "Ctrl+"

        self._action("Open movie", self.open_video_dialog, shortcuts(QKeySequence.Open), f"Open a video ({mod}O)", tb)
        tb.addSeparator()
        self._action("Search", self.search, None, "Find subtitles on OpenSubtitles", tb)
        self._action("✨ Auto", self.auto, None, "Make subtitles from the audio with Whisper", tb)
        self._action("Sync", self.sync, None, "Align subtitle timing to the audio (ffsubsync)", tb)
        self._action("Shift…", self.shift, None, "Move all subtitles earlier or later", tb)
        tb.addSeparator()
        self.undo_act = self._action(
            "◀ Undo", self.undo,
            shortcuts(QKeySequence.Undo, "Ctrl+Z", "Meta+Z" if MAC else None), f"Undo ({mod}Z)", tb,
        )
        self.redo_act = self._action(
            "Redo ▶", self.redo,
            shortcuts(QKeySequence.Redo, "Ctrl+Y", "Meta+Y" if MAC else None), f"Redo ({mod}Y)", tb,
        )
        tb.addSeparator()
        self.backend = QComboBox()
        self.backend.addItems(["deepl", "google", "argos"])
        self.backend.setCurrentText(default_backend())
        self.backend.setToolTip("Translation service")
        tb.addWidget(self.backend)
        self.target = lang_combo("fi")
        self.target.setMinimumContentsLength(12)
        self.target.currentTextChanged.connect(lambda _t: self._update_headers())
        tb.addWidget(self.target)
        self._action("Translate", self.translate, None, "Translate rows whose translation is empty", tb)
        tb.addSeparator()
        self._action(
            "Save", self.save, shortcuts(QKeySequence.Save, "Ctrl+S", "Meta+S" if MAC else None),
            f"Save subtitle files next to the movie ({mod}S)", tb,
        )
        self._action("Keys…", self.edit_keys, None, "OpenSubtitles and DeepL API keys", tb)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        for text, slot in [
            ("Open movie…", self.open_video_dialog),
            ("Open subtitles…", self.open_subs_dialog),
            ("Open translation…", self.open_translation_dialog),
            ("Save", self.save),
        ]:
            a = QAction(text, self)
            a.triggered.connect(slot)
            file_menu.addAction(a)
        help_menu = self.menuBar().addMenu("Help")
        about = QAction("About Subtitle Tool", self)
        about.setMenuRole(QAction.AboutRole)
        about.triggered.connect(self.about)
        help_menu.addAction(about)
        edit_menu = self.menuBar().addMenu("Edit")
        for a in (self.undo_act, self.redo_act, self.start_act, self.end_act):
            edit_menu.addAction(a)

    def about(self) -> None:
        from importlib.metadata import PackageNotFoundError, version

        try:
            ver = version("subtitle-tool")
        except PackageNotFoundError:
            ver = "dev"
        box = QMessageBox(self)
        box.setOption(QMessageBox.Option.DontUseNativeDialog, True)
        box.setWindowTitle("About Subtitle Tool")
        box.setIconPixmap(QIcon(str(ICON_PATH)).pixmap(96, 96))
        box.setTextFormat(Qt.RichText)
        box.setText(
            f"<b>Subtitle Tool</b> {ver}<br>Find, sync, transcribe and translate subtitles.<br><br>"
            '<a href="https://github.com/aaro-cmd/subtitle-tool">github.com/aaro-cmd/subtitle-tool</a>'
        )
        box.exec()

    def target_code(self) -> str:
        return combo_code(self.target) or "fi"

    def _update_headers(self) -> None:
        self.table.setHorizontalHeaderLabels(
            ["Start", "End", f"Original ({self.source_lang})", f"Translation ({self.target_code()})"]
        )

    # undo / redo

    def checkpoint(self, label: str = "") -> None:
        """Remember the current cues before a change."""
        self.undo_stack.append(([replace(c) for c in self.cues], label))
        del self.undo_stack[:-200]
        self.redo_stack.clear()
        self._update_undo_actions()

    def undo(self) -> None:
        self._restore(self.undo_stack, self.redo_stack, "Undid")

    def redo(self) -> None:
        self._restore(self.redo_stack, self.undo_stack, "Redid")

    def _restore(self, src, dst, verb) -> None:
        if not src:
            return
        cues, label = src.pop()
        dst.append(([replace(c) for c in self.cues], label))
        self.open_cue = None
        self.set_cues(cues)
        self.mark_dirty()
        self._update_undo_actions()
        self.statusBar().showMessage(f"{verb} {label}".strip())

    def _update_undo_actions(self) -> None:
        self.undo_act.setEnabled(bool(self.undo_stack))
        self.redo_act.setEnabled(bool(self.redo_stack))

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
        self.source_lang = "en"
        self.open_cue = None
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._update_undo_actions()
        self.set_cues([])
        top = find_local_subs(path, "en")
        if top:
            self.set_cues(parse_srt(read_text(top)))
            self.statusBar().showMessage(f"Loaded {top.name}")
            bottom = path.with_name(f"{path.stem}.{self.target_code()}.srt")
            if bottom.exists():
                self.merge_translation(parse_srt(read_text(bottom)))
        else:
            self.statusBar().showMessage(
                "No subtitles next to the video. Use Auto, Search, File › Open subtitles, or Set start."
            )
        self._update_headers()
        self.dirty = False
        self._update_title()

    def open_subs_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open subtitles", self._dir(), SUB_FILTER)
        if path:
            self.checkpoint("open subtitles")
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
            self.checkpoint("open translation")
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
        self.cues.sort(key=lambda c: c.start)
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
            if ms == (cue.start if col == COL_START else cue.end):
                return
            self.checkpoint("time edit")
            if col == COL_START:
                cue.start = ms
            else:
                cue.end = ms
            self._set_cell(row, col, fmt_ms(ms))
            if col == COL_START:
                self.refresh_table()  # keep rows in time order
        elif col == COL_TOP:
            if text == plain(cue.top):  # untouched rows keep their italics
                return
            self.checkpoint("text edit")
            cue.top = text
        else:
            if text == plain(cue.bottom):
                return
            self.checkpoint("translation edit")
            cue.bottom = text
        self.table.resizeRowToContents(row)
        self.mark_dirty()
        self.current = -1
        self.on_position(self.player.position())

    def _set_cell(self, row: int, col: int, text: str) -> None:
        self._filling = True
        self.table.item(row, col).setText(text)
        self._filling = False

    def on_row_clicked(self, row: int, col: int) -> None:
        if 0 <= row < len(self.cues):
            cue = self.cues[row]
            self.player.setPosition(cue.end if col == COL_END else cue.start)

    # manual timing

    def _commit_editor(self) -> None:
        """Save whatever is being typed in a cell before acting on the cues."""
        if self.table.state() != QAbstractItemView.EditingState:
            return
        for delegate in self.text_delegates:
            delegate.commit_active()
        if self.table.state() == QAbstractItemView.EditingState:
            self.table.setFocus()  # a time cell: focus-out saves it

    def set_start(self) -> None:
        if not self.video_path:
            return
        self._commit_editor()
        pos = self.player.position()
        if self.open_cue in self.cues and self.open_cue.end <= self.open_cue.start + 1:
            self.open_cue.end = max(self.open_cue.start + 500, pos)  # close the previous one
        self.checkpoint("set start")
        cue = Cue(pos, pos + 3000, "")
        self.cues.append(cue)
        self.open_cue = cue
        self.refresh_table()
        row = self.cues.index(cue)
        self.table.selectRow(row)
        self.table.scrollToItem(self.table.item(row, COL_TOP))
        self.table.editItem(self.table.item(row, COL_TOP))
        self.mark_dirty()
        self.statusBar().showMessage(
            "Type the line (Tab moves on, Enter saves). Press Set end when it should disappear."
        )

    def set_end(self) -> None:
        self._commit_editor()
        pos = self.player.position()
        cue = self.open_cue if self.open_cue in self.cues else self._target_cue()
        if cue is None:
            return
        if pos <= cue.start:
            self.statusBar().showMessage("The end has to be after the start.")
            return
        self.checkpoint("set end")
        cue.end = pos
        self.open_cue = None
        self.refresh_table()
        self.mark_dirty()
        self.statusBar().showMessage(f"Subtitle ends at {fmt_ms(pos)}")

    def _target_cue(self) -> Cue | None:
        """The selected line, or else the one on screen now."""
        rows = {i.row() for i in self.table.selectedIndexes()}
        if len(rows) == 1 and 0 <= next(iter(rows)) < len(self.cues):
            return self.cues[next(iter(rows))]
        return self.cues[self.current] if 0 <= self.current < len(self.cues) else None

    def retime_start(self) -> None:
        self._commit_editor()
        cue = self._target_cue()
        if cue is None:
            return
        pos = self.player.position()
        if pos >= cue.end:
            self.statusBar().showMessage("The start has to be before the end.")
            return
        self.checkpoint("set start")
        cue.start = pos
        self.refresh_table()
        self.table.selectRow(self.cues.index(cue))
        self.mark_dirty()
        self.statusBar().showMessage(f"Subtitle starts at {fmt_ms(pos)}")

    def delete_rows(self) -> None:
        if self.table.state() == QAbstractItemView.EditingState:
            return
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        self.checkpoint(f"delete {len(rows)} row(s)")
        for r in rows:
            del self.cues[r]
        self.refresh_table()
        self.mark_dirty()

    # playback

    def seek_by(self, delta_ms: int) -> None:
        pos = max(0, min(self.player.position() + delta_ms, self.player.duration() or 0))
        self.player.setPosition(pos)

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
        self.time_label.setText(f"{fmt_ms(pos).replace(',', '.')} / {fmt_ms(self.player.duration())[:8]}")
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

    # background actions

    def run_job(self, label: str, fn, on_done, on_partial=None) -> None:
        self.statusBar().showMessage(label)
        QApplication.setOverrideCursor(Qt.BusyCursor)
        job = Job(fn)
        self._busy_jobs.append(job)

        def finish():
            QApplication.restoreOverrideCursor()
            self._busy_jobs.remove(job)
            self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)

        def done(result):
            finish()
            on_done(result)

        def failed(msg):
            finish()
            self.statusBar().showMessage("Failed")
            msg_box(QMessageBox.Warning, self, "Something went wrong", msg)

        def progress(obj):
            if isinstance(obj, str):
                self.statusBar().showMessage(obj)
            elif on_partial:
                on_partial(obj)

        job.signals.done.connect(done)
        job.signals.failed.connect(failed)
        job.signals.progress.connect(progress)
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
                msg_box(QMessageBox.Information, self, "Search", "No subtitles found. Try another name.")
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
        self.checkpoint("download")
        self.source_lang = "en"
        self._update_headers()
        self.set_cues(parse_srt(text))
        self.mark_dirty()
        self.statusBar().showMessage(f"Loaded {label}. Press Sync to match it to the audio.")

    def auto(self) -> None:
        if not self.video_path:
            msg_box(QMessageBox.Information, self, "Auto", "Open a video first.")
            return
        if self.cues and msg_box(QMessageBox.Question, 
            self, "Auto", "Replace the current subtitles with automatic ones? (Undo brings them back.)"
        ) != QMessageBox.Yes:
            return
        backend, target = self.backend.currentText(), self.target_code()
        dlg = AutoDialog(self, target, backend)
        if dlg.exec() != QDialog.Accepted:
            return
        video, model = self.video_path, dlg.model.currentData()
        language, music, also_translate = combo_code(dlg.lang), dlg.music.isChecked(), dlg.translate.isChecked()

        translate_on = also_translate and not deepl_missing(backend)

        def work(progress):
            from .transcribe import transcribe
            from .translate import translate_lines

            state = {"error": None, "buffer": []}

            def send(lang, final=False):
                batch = state["buffer"]
                if not batch or (len(batch) < 5 and not final):
                    return
                state["buffer"] = []
                if translate_on and lang != target and not state["error"]:
                    try:
                        out = translate_lines([c.top for c in batch], target=target, backend=backend, source=lang)
                        for c, t in zip(batch, out):
                            c.bottom = t
                    except Exception as e:  # keep transcribing even if translation fails
                        state["error"] = f"{type(e).__name__}: {e}"
                progress((lang, batch))

            def on_lines(lang, lines):
                state["buffer"] += [Cue(l.start, l.end, l.text) for l in lines]
                send(lang)

            lang, _lines = transcribe(
                video, model=model, language=language, music=music, progress=progress, on_lines=on_lines
            )
            send(lang, final=True)
            return lang, state["error"]

        started = {"yes": False}

        def partial(result):
            lang, batch = result
            if not started["yes"]:
                started["yes"] = True
                self.checkpoint("auto subtitles")
                self.cues = []
                self.source_lang = lang
                self._update_headers()
            self.cues += batch
            self.refresh_table()
            self.mark_dirty()

        def done(result):
            lang, error = result
            self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
            msg = f"Made {len(self.cues)} subtitles from the audio (language: {lang})."
            self.statusBar().showMessage(msg)
            if also_translate and not translate_on:
                msg_box(QMessageBox.Information, self, "Not translated",
                        f"{msg}\n\nAdd a DeepL key under Keys… (or pick google) and press Translate.")
            elif error:
                msg_box(QMessageBox.Warning, self, "Translation failed", f"{msg}\n\nTranslation failed: {error}")

        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)  # read-only while it fills
        self.run_job("Starting automatic subtitles…", work, done, on_partial=partial)
        return


    def sync(self) -> None:
        if not (self.video_path and self.cues):
            msg_box(QMessageBox.Information, self, "Sync", "Open a movie and subtitles first.")
            return
        video, cues = self.video_path, [replace(c) for c in self.cues]

        def work(progress):
            from .sync import sync_to_video

            with tempfile.TemporaryDirectory() as tmp:
                src, out = Path(tmp) / "in.srt", Path(tmp) / "out.srt"
                src.write_text(srt.compose(cues_to_srt(cues)), encoding="utf-8")
                sync_to_video(video, src, out, progress)
                return parse_srt(out.read_text(encoding="utf-8"))

        def done(synced: list[Cue]):
            self.checkpoint("sync")
            if len(synced) == len(self.cues):
                for c, s in zip(self.cues, synced):
                    c.start, c.end = s.start, s.end
                self.refresh_table()
            else:
                self.set_cues(synced)
            self.mark_dirty()
            self.statusBar().showMessage("Synced to the audio.")

        self.run_job("Syncing to the audio…", work, done)

    def shift(self) -> None:
        secs, ok = QInputDialog.getDouble(
            self, "Shift subtitles", "Seconds (negative = earlier):", 0.0, -3600, 3600, 2
        )
        if ok and secs:
            self.checkpoint("shift")
            delta = int(secs * 1000)
            for c in self.cues:
                c.start, c.end = max(0, c.start + delta), max(0, c.end + delta)
            self.refresh_table()
            self.mark_dirty()

    def translate(self) -> None:
        rows = [i for i, c in enumerate(self.cues) if c.top.strip() and not c.bottom.strip()]
        if not rows:
            msg_box(QMessageBox.Information, 
                self, "Translate", "Every row already has a translation. Clear a cell to re-translate it."
            )
            return
        if deepl_missing(self.backend.currentText()):
            dlg = KeysDialog(self)
            if dlg.exec() != QDialog.Accepted:
                return
            dlg.save()
            if deepl_missing(self.backend.currentText()):
                return
        texts = [self.cues[i].top for i in rows]
        targets = [self.cues[i] for i in rows]
        backend, target, source = self.backend.currentText(), self.target_code(), self.source_lang

        def work(progress):
            from .translate import translate_lines

            return translate_lines(
                texts, target=target, backend=backend, source=source,
                progress=lambda d, t: progress(f"Translating… {d}/{t}"),
            )

        def done(result: list[str]):
            self.checkpoint("translate")
            for cue, t in zip(targets, result):
                cue.bottom = t
            self.refresh_table()
            self.mark_dirty()
            self.statusBar().showMessage(f"Translated {len(result)} lines.")

        self.run_job(f"Translating {len(rows)} lines with {backend}…", work, done)

    def save(self) -> bool:
        self._commit_editor()
        if not self.cues:
            return True
        if self.video_path:
            base = self.video_path.with_suffix("")
        else:
            path, _ = QFileDialog.getSaveFileName(self, "Save subtitles", self._dir(), SUB_FILTER)
            if not path:
                return False
            base = Path(path).with_suffix("")
        top_path = Path(f"{base}.{self.source_lang}.srt")
        top_path.write_text(srt.compose(cues_to_srt(self.cues)), encoding="utf-8")
        written = [top_path]
        if any(c.bottom for c in self.cues):
            written += write_translation_outputs(
                base, self.source_lang, self.target_code(), cues_to_srt(self.cues), [c.bottom for c in self.cues]
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
        name = self.video_path.name if self.video_path else "No video"
        self.setWindowTitle(f"{'• ' if self.dirty else ''}{name}")

    def confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        answer = msg_box(QMessageBox.Question, 
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


ICON_PATH = Path(__file__).with_name("assets") / "icon.svg"
APP_NAME = "Subtitle Tool"


def _set_os_app_name() -> None:
    """Show 'Subtitle Tool' instead of 'Python' in the Dock / taskbar."""
    if sys.platform == "darwin":
        try:
            from Foundation import NSBundle

            info = NSBundle.mainBundle().localizedInfoDictionary() or NSBundle.mainBundle().infoDictionary()
            if info is not None:
                info["CFBundleName"] = APP_NAME
        except Exception:
            pass
    elif sys.platform == "win32":
        try:
            import ctypes

            # Own taskbar group and icon instead of python.exe's.
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("aaro.subtitle-tool")
        except Exception:
            pass


def _set_dock_name() -> None:
    """macOS names unbundled apps after the executable ("python3.14") in the Dock.
    Rename this process in LaunchServices, like Chromium does for its helpers."""
    if sys.platform != "darwin":
        return
    try:
        import ctypes

        ls = ctypes.CDLL(
            "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/LaunchServices"
        )
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        ls._LSGetCurrentApplicationASN.restype = ctypes.c_void_p
        ls._LSSetApplicationInformationItem.restype = ctypes.c_int
        ls._LSSetApplicationInformationItem.argtypes = [ctypes.c_int] + [ctypes.c_void_p] * 4
        key = ctypes.c_void_p.in_dll(ls, "_kLSDisplayNameKey").value
        name = cf.CFStringCreateWithCString(None, APP_NAME.encode(), 0x08000100)  # UTF-8
        ls._LSSetApplicationInformationItem(-2, ls._LSGetCurrentApplicationASN(), key, name, None)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    _set_os_app_name()
    app = QApplication.instance() or QApplication([APP_NAME])
    app.setOrganizationName("subtitle-tool")
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setDesktopFileName("subtitle-tool")
    app.setWindowIcon(QIcon(str(ICON_PATH)))
    _set_dock_name()
    load_saved_keys()
    win = MainWindow()
    win.show()
    if argv:
        win.open_video(Path(argv[0]).expanduser())
    app.exec()


if __name__ == "__main__":
    main()
