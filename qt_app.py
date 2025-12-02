#!/usr/bin/env python3
import sys
import os
import traceback
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
    QSlider,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QPixmap, QImage, QAction, QFont

# Import engine from app package
from app.audio_engine import AudioEngine, PlaybackState

# Visualizer window
from visualizer_window import VisualizerWindow


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


class SeekSlider(QSlider):
    clicked = pyqtSignal(int)

    def mousePressEvent(self, ev):
        if ev.button():
            x = ev.position().x() if hasattr(ev, "position") else ev.x()
            w = self.width()
            if w > 0:
                ratio = max(0.0, min(1.0, x / float(w)))
                val = int(round(self.minimum() + ratio * (self.maximum() - self.minimum())))
                self.setValue(val)
                self.clicked.emit(val)
                return
        super().mousePressEvent(ev)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Bit-Perfect Player (PyQt6)")
        self.setMinimumSize(1000, 600)

        # Engine
        self.engine = AudioEngine()

        # Seeking state
        self._seeking = False
        self._seek_preview_seconds: Optional[float] = None

        # Visualizer attribute (created on demand)
        self.visualizer: Optional[VisualizerWindow] = None

        # Widgets
        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Name", "Length"])
        self.tree.setColumnWidth(0, 450)

        self.now_playing_label = QLabel("Now Playing: –")
        self.state_label = QLabel("State: Idle")

        self.art_label = QLabel()
        self.art_label.setFixedSize(200, 200)
        self.art_label.setStyleSheet("background: #111; border: 1px solid #333;")
        self.art_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.meta_title = QLabel("Title: –")
        self.meta_artist = QLabel("Artist: –")
        self.meta_album = QLabel("Album: –")
        self.meta_info = QLabel("Format: –")

        self.seek_slider = SeekSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 1000)
        self.seek_slider.setSingleStep(1)
        self.time_label_current = QLabel("0:00")
        self.time_label_total = QLabel("0:00")
        self.time_label_current.setFixedWidth(60)
        self.time_label_total.setFixedWidth(60)

        self.device_combo = QComboBox()
        self.prev_button = QPushButton("Prev")
        self.play_button = QPushButton("Play")
        self.pause_button = QPushButton("Pause")
        self.next_button = QPushButton("Next")
        self.stop_button = QPushButton("Stop")

        # NEW: Visualizer button
        self.visualizer_button = QPushButton("Visualizer")

        self._create_menu_bar()
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

        self._setup_ui()
        self._connect_signals()

        self._load_tracks_tree()
        self._load_devices()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_status)
        self.timer.start(500)

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

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        main_layout = QHBoxLayout()
        central.setLayout(main_layout)

        left_layout = QVBoxLayout()
        left_layout.addWidget(QLabel("Library"))
        left_layout.addWidget(self.tree)

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

        seek_row = QHBoxLayout()
        seek_row.addWidget(self.time_label_current)
        seek_row.addWidget(self.seek_slider, 1)
        seek_row.addWidget(self.time_label_total)
        right_layout.addLayout(seek_row)

        # device row with refresh button
        dev_row = QHBoxLayout()
        dev_row.addWidget(QLabel("Output device:"))
        dev_row.addWidget(self.device_combo)
        self.device_refresh_button = QPushButton("Refresh")
        dev_row.addWidget(self.device_refresh_button)
        right_layout.addLayout(dev_row)

        ctrl_row = QHBoxLayout()
        ctrl_row.addWidget(self.prev_button)
        ctrl_row.addWidget(self.play_button)
        ctrl_row.addWidget(self.pause_button)
        ctrl_row.addWidget(self.next_button)
        ctrl_row.addWidget(self.stop_button)

        # Add visualizer button to controls row
        ctrl_row.addWidget(self.visualizer_button)

        right_layout.addLayout(ctrl_row)

        main_layout.addLayout(left_layout, 2)
        main_layout.addLayout(right_layout, 1)

    def _connect_signals(self):
        self.tree.itemDoubleClicked.connect(self.on_tree_double_click)
        self.play_button.clicked.connect(self.on_play_clicked)
        self.pause_button.clicked.connect(self.on_pause_clicked)
        self.stop_button.clicked.connect(self.on_stop_clicked)
        self.device_combo.currentIndexChanged.connect(self.on_device_changed)
        self.device_refresh_button.clicked.connect(self._load_devices)

        self.prev_button.clicked.connect(self.on_prev_clicked)
        self.next_button.clicked.connect(self.on_next_clicked)

        self.seek_slider.sliderPressed.connect(self.on_seek_pressed)
        self.seek_slider.sliderReleased.connect(self.on_seek_released)
        self.seek_slider.sliderMoved.connect(self.on_seek_moved)
        self.seek_slider.clicked.connect(self.on_slider_clicked)

        # Visualizer button handler
        self.visualizer_button.clicked.connect(self.on_visualizer_clicked)

    # styling helper
    def style_tree_item(self, item: QTreeWidgetItem, level: int):
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

    def _load_tracks_tree(self):
        self.tree.clear()
        tracks = self.engine.list_tracks()
        if not tracks:
            return

        root_map = {}
        base = os.path.abspath(self.engine.music_dir)

        for t in tracks:
            try:
                rel = os.path.relpath(t.path, base)
            except Exception:
                rel = t.path
            parts = rel.split(os.sep)
            parent = None
            path_acc = base
            for i, part in enumerate(parts[:-1]):
                path_acc = os.path.join(path_acc, part)
                if path_acc not in root_map:
                    node = QTreeWidgetItem([part, ""])
                    node.setData(0, Qt.ItemDataRole.UserRole, {"type": "dir", "path": path_acc})
                    root_map[path_acc] = node
                    if parent is None:
                        self.tree.addTopLevelItem(node)
                        self.style_tree_item(node, level=0)
                    else:
                        parent.addChild(node)
                        depth = len(parts[:i+1]) - 1
                        self.style_tree_item(node, level=depth if depth >= 0 else 1)
                    parent = node
                else:
                    parent = root_map[path_acc]

            name = parts[-1]
            length = format_seconds(t.duration)
            file_item = QTreeWidgetItem([name, length])
            file_item.setData(0, Qt.ItemDataRole.UserRole, {"type": "file", "track_id": t.id, "path": t.path})
            self.style_tree_item(file_item, level=2)
            if parent is None:
                self.tree.addTopLevelItem(file_item)
            else:
                parent.addChild(file_item)

        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setExpanded(True)

    def _load_devices(self):
        self.device_combo.clear()
        try:
            devs = self.engine.list_alsa_devices()
        except Exception as e:
            devs = []
            print("Error listing ALSA devices:", e)

        if not devs:
            self.device_combo.addItem("No ALSA devices found", userData=None)
            self.device_combo.setEnabled(False)
            return

        preferred_index = -1
        for i, d in enumerate(devs):
            label = d.get("name", d["id"])
            self.device_combo.addItem(label, userData=d["id"])
            if d["id"] == getattr(self.engine, "alsa_card", None):
                preferred_index = i

        if preferred_index >= 0:
            self.device_combo.setCurrentIndex(preferred_index)
        else:
            self.device_combo.setCurrentIndex(0)
            sel = self.device_combo.currentData()
            if sel:
                self.engine.set_output_device(sel)

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
        try:
            self.engine._scan_library()
        except Exception:
            pass
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
            st = self.engine.status()
            if st.get("current_track_id") is not None and st.get("state") == PlaybackState.PAUSED:
                self.engine.resume()
                return
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
            self._seeking = False
            self._seek_preview_seconds = None
            self.engine.play(track_id)
        except Exception as e:
            QMessageBox.critical(self, "Playback error", str(e))

    def _current_track_index(self) -> Optional[int]:
        st = self.engine.status()
        cur_id = st.get("current_track_id")
        if cur_id is None:
            return None
        tracks = self.engine.list_tracks()
        for i, t in enumerate(tracks):
            if t.id == cur_id:
                return i
        return None

    def on_prev_clicked(self):
        tracks = self.engine.list_tracks()
        if not tracks:
            return
        cur_idx = self._current_track_index()
        if cur_idx is None:
            sel = self.tree.currentItem()
            if sel:
                data = sel.data(0, Qt.ItemDataRole.UserRole)
                if data and data.get("type") == "file":
                    self.play_track(data.get("track_id"))
                    return
            self.play_track(tracks[0].id)
            return

        prev_idx = max(0, cur_idx - 1)
        if prev_idx != cur_idx:
            self.play_track(tracks[prev_idx].id)
        else:
            self.play_track(tracks[prev_idx].id)

    def on_next_clicked(self):
        tracks = self.engine.list_tracks()
        if not tracks:
            return
        cur_idx = self._current_track_index()
        if cur_idx is None:
            sel = self.tree.currentItem()
            if sel:
                data = sel.data(0, Qt.ItemDataRole.UserRole)
                if data and data.get("type") == "file":
                    self.play_track(data.get("track_id"))
                    return
            self.play_track(tracks[0].id)
            return

        next_idx = min(len(tracks) - 1, cur_idx + 1)
        if next_idx != cur_idx:
            self.play_track(tracks[next_idx].id)

    def on_seek_pressed(self):
        self._seeking = True

    def on_seek_moved(self, value: int):
        s = self.engine.get_duration()
        if s and s > 0:
            frac = value / 1000.0
            preview = frac * s
            self._seek_preview_seconds = preview
            self.time_label_current.setText(format_seconds(preview))
        else:
            self.time_label_current.setText("–")

    def on_seek_released(self):
        value = self.seek_slider.value()
        s = self.engine.get_duration()
        if s and s > 0:
            frac = value / 1000.0
            target = frac * s
            self.engine.seek(target)
            self.time_label_current.setText(format_seconds(target))
        self._seeking = False
        self._seek_preview_seconds = None

    def on_slider_clicked(self, slider_value: int):
        s = self.engine.get_duration()
        if s and s > 0:
            frac = slider_value / 1000.0
            target = frac * s
            self.engine.seek(target)
            self.time_label_current.setText(format_seconds(target))
            self.seek_slider.setValue(slider_value)

    def on_visualizer_clicked(self):
        # create on demand and connect engine signal -> visualizer push_chunk
        try:
            if self.visualizer is None:
                # pass engine so the visualizer can read current sample rate
                self.visualizer = VisualizerWindow(engine=self.engine)
                self.engine.pcm_chunk.connect(self.visualizer.push_chunk)
            self.visualizer.show()
            self.visualizer.raise_()
        except Exception as e:
            QMessageBox.warning(self, "Visualizer error", f"Could not open visualizer: {e}")


    def _clear_metadata(self):
        self.art_label.setPixmap(QPixmap())
        self.meta_title.setText("Title: –")
        self.meta_artist.setText("Artist: –")
        self.meta_album.setText("Album: –")
        self.meta_info.setText("Format: –")
        self.time_label_current.setText("0:00")
        self.time_label_total.setText("0:00")
        self.seek_slider.setValue(0)

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

        pos = s.get("position", 0.0)
        dur = s.get("duration", 0.0)
        if dur is None:
            dur = 0.0

        self.time_label_total.setText(format_seconds(dur))

        if self._seeking:
            return

        try:
            if dur > 0:
                frac = max(0.0, min(1.0, pos / dur))
            else:
                frac = 0.0
            slider_val = int(round(frac * 1000.0))
            self.seek_slider.blockSignals(True)
            self.seek_slider.setValue(slider_val)
            self.seek_slider.blockSignals(False)
            self.time_label_current.setText(format_seconds(pos))
        except Exception:
            pass

    def closeEvent(self, event):
        try:
            self.engine.stop()
        except Exception:
            pass
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    app.exec()


if __name__ == "__main__":
    main()
