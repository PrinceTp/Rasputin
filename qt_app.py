#!/usr/bin/env python3
import sys
import os
from typing import Optional

from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QPushButton,
    QLabel,
    QVBoxLayout,
    QHBoxLayout,
    QMessageBox,
    QComboBox,
    QFileDialog,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QPixmap, QImage, QAction, QFont

from app.audio_engine import AudioEngine, PlaybackState


def format_seconds(sec: Optional[float]) -> str:
    if sec is None:
        return "–"
    try:
        s = int(round(sec))
        m = s // 60
        s = s % 60
        return f"{m}:{s:02d}"
    except Exception:
        return "–"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Bit-Perfect Player (PyQt6)")
        self.setMinimumSize(1000, 600)

        # Engine
        self.engine = AudioEngine()

        # Widgets
        self.tree = QTreeWidget()
        # two columns: name, length
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Name", "Length"])
        self.tree.setColumnWidth(0, 450)

        self.now_playing_label = QLabel("Now Playing: –")
        self.state_label = QLabel("State: Idle")

        # Album art + metadata
        self.art_label = QLabel()
        self.art_label.setFixedSize(200, 200)
        self.art_label.setStyleSheet("background: #111; border: 1px solid #333;")
        self.art_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.meta_title = QLabel("Title: –")
        self.meta_artist = QLabel("Artist: –")
        self.meta_album = QLabel("Album: –")
        self.meta_info = QLabel("Format: –")

        # Device combo + controls
        self.device_combo = QComboBox()
        self.play_button = QPushButton("Play")
        self.pause_button = QPushButton("Pause")
        self.stop_button = QPushButton("Stop")

        # Menu
        self._create_menu_bar()

        # Make menu bar bigger / more prominent using stylesheet
        self.menuBar().setStyleSheet("""
            QMenuBar {
                font-size: 16px;
                padding: 6px 12px;
                spacing: 20px;
                background: #0b1220;
                color: #e6eef6;
            }
            QMenuBar::item {
                padding: 8px 16px;
                background: transparent;
            }
            QMenuBar::item:selected {
                background: #1f2a44;
            }
            QMenu {
                font-size: 14px;
            }
        """)

        # Layout
        self._setup_ui()
        self._connect_signals()

        # Load data
        self._load_tracks_tree()
        self._load_devices()

        # Status timer
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_status)
        self.timer.start(500)

    # ---------------- Menu ----------------
    def _create_menu_bar(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu("&File")

        import_action = QAction("Import folder", self)
        import_action.setShortcut("Ctrl+O")
        import_action.triggered.connect(self.on_import_folder)
        file_menu.addAction(import_action)

        rescan_action = QAction("Rescan library", self)
        rescan_action.setShortcut("Ctrl+R")
        rescan_action.triggered.connect(self.on_rescan)
        file_menu.addAction(rescan_action)

        file_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

    # ---------------- UI ----------------
    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        main_layout = QHBoxLayout()
        central.setLayout(main_layout)

        # left: tree
        left_layout = QVBoxLayout()
        left_layout.addWidget(QLabel("Library (folder structure)"))
        left_layout.addWidget(self.tree)

        # right: now playing + art + metadata + device + controls
        right_layout = QVBoxLayout()
        right_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.now_playing_label.setStyleSheet("font-weight: bold;")
        right_layout.addWidget(self.now_playing_label)

        self.state_label.setStyleSheet("color: gray;")
        right_layout.addWidget(self.state_label)

        right_layout.addWidget(self.art_label)
        right_layout.addWidget(self.meta_title)
        right_layout.addWidget(self.meta_artist)
        right_layout.addWidget(self.meta_album)
        right_layout.addWidget(self.meta_info)

        dev_row = QHBoxLayout()
        dev_row.addWidget(QLabel("Output device:"))
        dev_row.addWidget(self.device_combo)
        right_layout.addLayout(dev_row)

        ctrl_row = QHBoxLayout()
        ctrl_row.addWidget(self.play_button)
        ctrl_row.addWidget(self.pause_button)
        ctrl_row.addWidget(self.stop_button)
        right_layout.addLayout(ctrl_row)

        main_layout.addLayout(left_layout, 2)
        main_layout.addLayout(right_layout, 1)

    # ---------------- Signals ----------------
    def _connect_signals(self):
        self.tree.itemDoubleClicked.connect(self.on_tree_double_click)
        self.play_button.clicked.connect(self.on_play_clicked)
        self.pause_button.clicked.connect(self.on_pause_clicked)
        self.stop_button.clicked.connect(self.on_stop_clicked)
        self.device_combo.currentIndexChanged.connect(self.on_device_changed)

    # ---------------- Tree styling helper ----------------
    def style_tree_item(self, item: QTreeWidgetItem, level: int):
        """
        level = 0 → top-level folder
        level = 1 → subfolder
        level >=2 → files / deeper levels
        """
        f = QFont()
        if level == 0:
            f.setPointSize(14)
            f.setBold(True)
        elif level == 1:
            f.setPointSize(12)
            f.setBold(True)
        else:
            f.setPointSize(10)
            f.setBold(False)

        item.setFont(0, f)
        item.setFont(1, f)

    # ---------------- Load / build tree ----------------
    def _load_tracks_tree(self):
        """
        Build a QTreeWidget mirroring the folder structure under music_dir.
        Folders become top/inner nodes; files are leaf nodes with length column.
        """
        self.tree.clear()
        tracks = self.engine.list_tracks()
        if not tracks:
            return

        root_map = {}  # maps folder path -> QTreeWidgetItem

        base = os.path.abspath(self.engine.music_dir)

        for t in tracks:
            # relative path from music_dir
            try:
                rel = os.path.relpath(t.path, base)
            except Exception:
                rel = t.path
            parts = rel.split(os.sep)
            # build or find parent node
            parent = None
            path_acc = base
            for i, part in enumerate(parts[:-1]):  # all folder parts
                path_acc = os.path.join(path_acc, part)
                if path_acc not in root_map:
                    node = QTreeWidgetItem([part, ""])
                    node.setData(0, Qt.ItemDataRole.UserRole, {"type": "dir", "path": path_acc})
                    root_map[path_acc] = node
                    # attach to parent or top-level
                    if parent is None:
                        self.tree.addTopLevelItem(node)
                        self.style_tree_item(node, level=0)
                    else:
                        parent.addChild(node)
                        # level = depth of this folder relative to base
                        depth = len(parts[:i+1]) - 1
                        self.style_tree_item(node, level=depth if depth >= 0 else 1)
                    parent = node
                else:
                    parent = root_map[path_acc]

            # now add the file node under parent (or top-level if no parent)
            name = parts[-1]
            length = format_seconds(t.duration)
            file_item = QTreeWidgetItem([name, length])
            file_item.setData(0, Qt.ItemDataRole.UserRole, {"type": "file", "track_id": t.id, "path": t.path})
            # style file item as deeper level
            self.style_tree_item(file_item, level=2)
            if parent is None:
                self.tree.addTopLevelItem(file_item)
            else:
                parent.addChild(file_item)

        # expand top level folders for convenience
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setExpanded(True)

    # ---------------- Devices ----------------
    def _load_devices(self):
        self.device_combo.clear()
        devs = self.engine.list_alsa_devices()
        if not devs:
            self.device_combo.addItem("No ALSA hw devices", userData=None)
            self.device_combo.setEnabled(False)
            return
        for d in devs:
            label = f"{d['name']} ({d['id']})"
            self.device_combo.addItem(label, userData=d["id"])
        # select current if present
        cur = self.engine.status().get("alsa_card")
        if cur:
            idx = self.device_combo.findData(cur)
            if idx >= 0:
                self.device_combo.setCurrentIndex(idx)

    # ---------------- Actions ----------------
    def on_import_folder(self):
        initial = self.engine.status().get("music_dir") or os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(self, "Select primary music folder", initial)
        if not folder:
            return
        self.engine.set_music_dir(folder)
        self._load_tracks_tree()
        self._load_devices()
        self.now_playing_label.setText("Now Playing: –")
        self._clear_metadata()
        self.state_label.setText(f"State: Idle (library: {folder})")
        self.state_label.setStyleSheet("color: gray;")

    def on_rescan(self):
        self.engine._scan_library()
        self._load_tracks_tree()
        QMessageBox.information(self, "Rescan", "Library rescan complete.")

    def on_tree_double_click(self, item: QTreeWidgetItem, col: int):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        if data.get("type") == "file":
            track_id = data.get("track_id")
            if track_id is not None:
                self.play_track(track_id)

    def on_play_clicked(self):
        sel = self.tree.currentItem()
        if not sel:
            QMessageBox.information(self, "No selection", "Select a track to play.")
            return
        data = sel.data(0, Qt.ItemDataRole.UserRole)
        if not data or data.get("type") != "file":
            QMessageBox.information(self, "Select a file", "Please select a song (not a folder).")
            return
        tid = data.get("track_id")
        self.play_track(tid)

    def on_pause_clicked(self):
        self.engine.pause()

    def on_stop_clicked(self):
        self.engine.stop()

    def on_device_changed(self, idx: int):
        dev_id = self.device_combo.currentData()
        if dev_id:
            self.engine.set_output_device(dev_id)
            self.state_label.setText(f"State: Idle (output: {dev_id})")
            self.state_label.setStyleSheet("color: gray;")

    def play_track(self, track_id: int):
        try:
            self.engine.play(track_id)
        except Exception as e:
            QMessageBox.critical(self, "Playback error", str(e))

    # ---------------- Metadata / status ----------------
    def _clear_metadata(self):
        self.art_label.setPixmap(QPixmap())
        self.meta_title.setText("Title: –")
        self.meta_artist.setText("Artist: –")
        self.meta_album.setText("Album: –")
        self.meta_info.setText("Format: –")

    def update_status(self):
        s = self.engine.status()
        state = s.get("state")
        cur_name = s.get("current_track")
        if cur_name:
            self.now_playing_label.setText(f"Now Playing: {cur_name}")
        else:
            self.now_playing_label.setText("Now Playing: –")

        if state == PlaybackState.PLAYING:
            self.state_label.setText("State: Playing")
            self.state_label.setStyleSheet("color: #22c55e;")
        elif state == PlaybackState.PAUSED:
            self.state_label.setText("State: Paused")
            self.state_label.setStyleSheet("color: #facc15;")
        elif state == PlaybackState.STOPPED:
            self.state_label.setText("State: Stopped")
            self.state_label.setStyleSheet("color: gray;")
        else:
            if "output:" not in self.state_label.text():
                self.state_label.setText("State: Idle")
            self.state_label.setStyleSheet("color: gray;")

        tid = s.get("current_track_id")
        if tid is None:
            self._clear_metadata()
            return

        track = self.engine.get_track_by_id(tid)
        if not track:
            return

        self.meta_title.setText(f"Title: {track.title or '–'}")
        self.meta_artist.setText(f"Artist: {track.artist or '–'}")
        self.meta_album.setText(f"Album: {track.album or '–'}")

        if track.sample_rate and track.bit_depth and track.channels:
            fmt = f"{track.sample_rate/1000:.1f} kHz | {track.bit_depth}-bit | {track.channels} ch"
        else:
            fmt = "–"
        self.meta_info.setText(f"Format: {fmt}")

        if track.album_art:
            try:
                img = QImage.fromData(track.album_art)
                pix = QPixmap.fromImage(img).scaled(
                    200, 200, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
                )
                self.art_label.setPixmap(pix)
            except Exception:
                self.art_label.setPixmap(QPixmap())
        else:
            self.art_label.setPixmap(QPixmap())


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    app.exec()


if __name__ == "__main__":
    main()
