#!/usr/bin/env python3
import sys

from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QLabel,
    QVBoxLayout,
    QHBoxLayout,
    QMessageBox,
    QComboBox,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QPixmap, QImage
from app.audio_engine import AudioEngine, PlaybackState 


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Bit-Perfect Player (PyQt6)")
        self.setMinimumSize(900, 550)

        # -------- Core audio engine --------
        self.engine = AudioEngine()

        # -------- UI widgets --------
        self.track_list = QListWidget()
        self.now_playing_label = QLabel("Now Playing: –")
        self.state_label = QLabel("State: Idle")

        # NEW: album art + metadata widgets
        self.art_label = QLabel()
        self.art_label.setFixedSize(200, 200)
        self.art_label.setStyleSheet("background: #111; border: 1px solid #333;")
        self.art_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.meta_title = QLabel("Title: –")
        self.meta_artist = QLabel("Artist: –")
        self.meta_album = QLabel("Album: –")
        self.meta_info = QLabel("Format: –")

        # ALSA device selector
        self.device_label = QLabel("Output device:")
        self.device_combo = QComboBox()

        # Playback controls
        self.play_button = QPushButton("Play")
        self.pause_button = QPushButton("Pause")
        self.stop_button = QPushButton("Stop")

        self._selected_track_id = None

        # Build UI & connect signals
        self._setup_ui()
        self._connect_signals()
        self._load_tracks()
        self._load_devices()

        # Periodic status refresh
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.update_status)
        self.status_timer.start(500)  # every 500 ms

    # ---------- UI setup ----------

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        main_layout = QHBoxLayout()
        central.setLayout(main_layout)

        # Left: track list
        left_layout = QVBoxLayout()
        left_layout.addWidget(QLabel("Library"))
        left_layout.addWidget(self.track_list)

        # Right: now playing + art + metadata + device + controls
        right_layout = QVBoxLayout()
        right_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.now_playing_label.setStyleSheet("font-weight: bold;")
        right_layout.addWidget(self.now_playing_label)

        self.state_label.setStyleSheet("color: gray;")
        right_layout.addWidget(self.state_label)

        # NEW: album art + metadata block
        right_layout.addWidget(self.art_label)
        right_layout.addWidget(self.meta_title)
        right_layout.addWidget(self.meta_artist)
        right_layout.addWidget(self.meta_album)
        right_layout.addWidget(self.meta_info)

        # Device selector row
        device_row = QHBoxLayout()
        device_row.addWidget(self.device_label)
        device_row.addWidget(self.device_combo)
        right_layout.addLayout(device_row)

        controls_layout = QHBoxLayout()
        controls_layout.addWidget(self.play_button)
        controls_layout.addWidget(self.pause_button)
        controls_layout.addWidget(self.stop_button)
        right_layout.addLayout(controls_layout)

        main_layout.addLayout(left_layout, 2)
        main_layout.addLayout(right_layout, 1)


    def _connect_signals(self):
        self.track_list.itemDoubleClicked.connect(self.on_track_double_clicked)
        self.play_button.clicked.connect(self.on_play_clicked)
        self.pause_button.clicked.connect(self.on_pause_clicked)
        self.stop_button.clicked.connect(self.on_stop_clicked)
        self.device_combo.currentIndexChanged.connect(self.on_device_changed)

    # ---------- Data loading ----------

    def _load_tracks(self):
        self.track_list.clear()
        tracks = self.engine.list_tracks()
        if not tracks:
            QMessageBox.warning(
                self,
                "No tracks",
                "No FLAC/WAV files found in your music directory.",
            )
            return

        for t in tracks:
            display_name = f"{t.title} — {t.artist}" if t.title else t.name
            item = QListWidgetItem(display_name)
            item.setData(Qt.ItemDataRole.UserRole, t.id)
            self.track_list.addItem(item)

    def _load_devices(self):
        self.device_combo.clear()
        devices = self.engine.list_alsa_devices()
        if not devices:
            self.device_combo.addItem("No ALSA hw devices found", userData=None)
            self.device_combo.setEnabled(False)
            return

        current_card = self.engine.status().get("alsa_card")

        for dev in devices:
            display = f'{dev["name"]} ({dev["id"]})'
            self.device_combo.addItem(display, userData=dev["id"])

        # Try to set selection to current alsa_card
        if current_card:
            index = self.device_combo.findData(current_card)
            if index >= 0:
                self.device_combo.setCurrentIndex(index)

    # ---------- Event handlers ----------

    def on_track_double_clicked(self, item: QListWidgetItem):
        track_id = item.data(Qt.ItemDataRole.UserRole)
        self._selected_track_id = track_id
        self.play_track(track_id)

    def on_play_clicked(self):
        item = self.track_list.currentItem()
        if item is None:
            QMessageBox.information(
                self,
                "No track selected",
                "Please select a track from the list.",
            )
            return
        track_id = item.data(Qt.ItemDataRole.UserRole)
        self._selected_track_id = track_id
        self.play_track(track_id)

    def on_pause_clicked(self):
        self.engine.pause()

    def on_stop_clicked(self):
        self.engine.stop()

    def on_device_changed(self, index: int):
        device_id = self.device_combo.currentData()
        if not device_id:
            return
        # Change ALSA output device in the engine
        self.engine.set_output_device(device_id)
        # Optional: update status label
        self.state_label.setText(f"State: Idle (output: {device_id})")
        self.state_label.setStyleSheet("color: gray;")

    def play_track(self, track_id: int):
        try:
            self.engine.play(track_id)
        except ValueError as e:
            QMessageBox.critical(self, "Playback error", str(e))

    # ---------- Status updates ----------

    def update_status(self):
        status = self.engine.status()
        state = status["state"]
        current_name = status["current_track"]

        # ---- Now Playing label ----
        if current_name:
            self.now_playing_label.setText(f"Now Playing: {current_name}")
        else:
            self.now_playing_label.setText("Now Playing: –")

        # ---- State label ----
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
            # idle
            if "output:" not in self.state_label.text():
                self.state_label.setText("State: Idle")
            self.state_label.setStyleSheet("color: gray;")

        # ---- Album art + metadata ----
        track_id = status.get("current_track_id")
        if track_id is None:
            # reset everything when not playing
            self.art_label.setPixmap(QPixmap())
            self.meta_title.setText("Title: –")
            self.meta_artist.setText("Artist: –")
            self.meta_album.setText("Album: –")
            self.meta_info.setText("Format: –")
            return

        # Fetch TrackInfo object
        track = self.engine.get_track_by_id(track_id)
        if not track:
            return  # nothing to update

        # ---- Metadata ----
        self.meta_title.setText(f"Title: {track.title or '–'}")
        self.meta_artist.setText(f"Artist: {track.artist or '–'}")
        self.meta_album.setText(f"Album: {track.album or '–'}")

        # Audio info: sample rate, bit depth, channels
        if track.sample_rate and track.bit_depth and track.channels:
            fmt = f"{track.sample_rate/1000:.1f} kHz | {track.bit_depth}-bit | {track.channels} ch"
        else:
            fmt = "Format: –"

        self.meta_info.setText(f"Format: {fmt}")

        # ---- Album Art ----
        if track.album_art:
            qimg = QImage.fromData(track.album_art)
            pix = QPixmap.fromImage(qimg).scaled(
                200,
                200,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.art_label.setPixmap(pix)
        else:
            # clear art box
            self.art_label.setPixmap(QPixmap())



def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
