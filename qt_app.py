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

from app.audio_engine import AudioEngine, PlaybackState


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Bit-Perfect Player (PyQt6)")
        self.setMinimumSize(900, 550)

        # Core audio engine
        self.engine = AudioEngine()

        # UI elements
        self.track_list = QListWidget()
        self.now_playing_label = QLabel("Now Playing: –")
        self.state_label = QLabel("State: Idle")

        self.play_button = QPushButton("Play")
        self.pause_button = QPushButton("Pause")
        self.stop_button = QPushButton("Stop")

        self._selected_track_id = None

        self._setup_ui()
        self._connect_signals()
        self._load_tracks()

        # Periodic status refresh
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.update_status)
        self.status_timer.start(500)  # every 500 ms

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        main_layout = QHBoxLayout()
        central.setLayout(main_layout)

        # Left: track list
        left_layout = QVBoxLayout()
        left_layout.addWidget(QLabel("Library"))
        left_layout.addWidget(self.track_list)

        # Right: now playing + controls
        right_layout = QVBoxLayout()
        right_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.now_playing_label.setStyleSheet("font-weight: bold;")
        right_layout.addWidget(self.now_playing_label)

        self.state_label.setStyleSheet("color: gray;")
        right_layout.addWidget(self.state_label)

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

    def _load_tracks(self):
        self.track_list.clear()
        tracks = self.engine.list_tracks()
        if not tracks:
            QMessageBox.warning(self, "No tracks", "No FLAC/WAV files found in your music directory.")
            return

        for t in tracks:
            item = QListWidgetItem(f"[{t.id}] {t.name}")
            # store track id on the item
            item.setData(Qt.ItemDataRole.UserRole, t.id)
            self.track_list.addItem(item)

    # ---------- UI event handlers ----------

    def on_track_double_clicked(self, item: QListWidgetItem):
        track_id = item.data(Qt.ItemDataRole.UserRole)
        self._selected_track_id = track_id
        self.play_track(track_id)

    def on_play_clicked(self):
        item = self.track_list.currentItem()
        if item is None:
            QMessageBox.information(self, "No track selected", "Please select a track from the list.")
            return
        track_id = item.data(Qt.ItemDataRole.UserRole)
        self._selected_track_id = track_id
        self.play_track(track_id)

    def on_pause_clicked(self):
        self.engine.pause()

    def on_stop_clicked(self):
        self.engine.stop()

    def play_track(self, track_id: int):
        try:
            self.engine.play(track_id)
        except ValueError as e:
            QMessageBox.critical(self, "Playback error", str(e))

    def update_status(self):
        status = self.engine.status()
        state = status["state"]
        current_name = status["current_track"]

        # Update labels
        if current_name:
            self.now_playing_label.setText(f"Now Playing: {current_name}")
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
            self.state_label.setText("State: Idle")
            self.state_label.setStyleSheet("color: gray;")


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
