# visualizer_window.py
# Audiophile FFT visualizer: log-frequency FFT, dB scale, A-weighting option,
# smoothing (EMA), peak-hold, octave / reference markers.
#
# Dependencies: PyQt6, numpy
#
# Usage: create VisualizerWindow(engine) or VisualizerWindow() and connect:
#   engine.pcm_chunk.connect(visualizer.push_chunk)
#
# The visualizer will try to read sample rate from engine.current_track.sample_rate
# if available; otherwise it falls back to 44100 Hz.

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QSlider
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QPainter, QColor, QPen, QFont
import numpy as np
import math
import time


def a_weighting(freq_hz: np.ndarray) -> np.ndarray:
    # A-weighting according to IEC 61672 (approx)
    f2 = freq_hz * freq_hz
    # avoid division by zero
    with np.errstate(divide='ignore', invalid='ignore'):
        ra = (12200.0**2 * f2**2) / \
             ((f2 + 20.6**2) * np.sqrt((f2 + 107.7**2) * (f2 + 737.9**2)) * (f2 + 12200.0**2))
        a = 20.0 * np.log10(ra) + 2.00
    a = np.nan_to_num(a, neginf=-200.0, posinf=200.0)
    return a


class VisualizerWindow(QWidget):
    def __init__(
        self,
        engine=None,
        fft_size=8192,
        bands=120,              # number of plotted bins (columns)
        min_freq=20.0,
        max_freq=20000.0,
        smoothing=0.7,         # EMA smoothing (0..1) where 1 keeps much of previous
        peak_hold_seconds=1.2,
        fps=30,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Audiophile FFT Visualizer")
        self.resize(1000, 400)

        # engine reference for sample rate access (optional)
        self.engine = engine

        # FFT / buffer parameters
        self.fft_size = int(fft_size)
        self.hop_size = max(256, int(self.fft_size // 8))
        self.buffer = np.zeros(self.fft_size, dtype=np.float32)
        self.buffer_fill = 0

        # display settings
        self.bands = bands
        self.min_freq = float(min_freq)
        self.max_freq = float(max_freq)
        self.smoothing = float(smoothing)
        self.peak_hold_seconds = float(peak_hold_seconds)
        self.peak_times = np.zeros(self.bands, dtype=np.float64)
        self.peak_values = np.full(self.bands, -200.0, dtype=np.float32)

        # data arrays
        self.smoothed = np.full(self.bands, -200.0, dtype=np.float32)  # dB values
        self.latest_db = np.full(self.bands, -200.0, dtype=np.float32)

        # A-weight toggle
        self.a_weight = False

        # UI controls
        layout = QVBoxLayout(self)
        ctrl_row = QHBoxLayout()
        self.aw_btn = QPushButton("A-weight: OFF")
        self.aw_btn.setCheckable(True)
        self.aw_btn.clicked.connect(self._toggle_a_weight)
        ctrl_row.addWidget(self.aw_btn)

        ctrl_row.addWidget(QLabel("Smoothing"))
        self.smooth_slider = QSlider(Qt.Orientation.Horizontal)
        self.smooth_slider.setRange(0, 100)
        self.smooth_slider.setValue(int(self.smoothing * 100))
        self.smooth_slider.valueChanged.connect(self._smooth_changed)
        ctrl_row.addWidget(self.smooth_slider)

        ctrl_row.addWidget(QLabel("Peak hold (s)"))
        self.peak_slider = QSlider(Qt.Orientation.Horizontal)
        self.peak_slider.setRange(0, 500)
        self.peak_slider.setValue(int(self.peak_hold_seconds * 100))
        self.peak_slider.valueChanged.connect(self._peak_changed)
        ctrl_row.addWidget(self.peak_slider)

        self.clear_btn = QPushButton("Clear Peaks")
        self.clear_btn.clicked.connect(self._clear_peaks)
        ctrl_row.addWidget(self.clear_btn)

        ctrl_row.addStretch()
        layout.addLayout(ctrl_row)

        # instructions label
        info = QLabel("Log-frequency FFT (20 Hz — 20 kHz). Designed for critical listening.")
        info.setStyleSheet("color: gray;")
        layout.addWidget(info)

        # repaint timer
        self.fps = fps
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update)
        self.timer.start(int(1000 / self.fps))

        # visual style
        self.bg = QColor(8, 8, 10)
        self.grid = QColor(32, 32, 36)
        self.bar_fg = QColor(65, 180, 255)
        self.peak_fg = QColor(255, 100, 100)
        self.text_color = QColor(200, 200, 200)
        self.font = QFont("Monospace", 9)

        # time tracking for peak hold decay
        self._now = time.time()

    # ----- control callbacks -----
    def _toggle_a_weight(self, checked):
        self.a_weight = checked
        self.aw_btn.setText("A-weight: ON" if checked else "A-weight: OFF")

    def _smooth_changed(self, v):
        self.smoothing = v / 100.0

    def _peak_changed(self, v):
        self.peak_hold_seconds = v / 100.0

    def _clear_peaks(self):
        self.peak_values.fill(-200.0)
        self.peak_times.fill(0.0)

    # ----- audio input -----
    def push_chunk(self, pcm):
        """
        Called from engine.pcm_chunk via Qt signal. Accepts array (frames,channels) or (frames,).
        We append left channel (or mono) to a rolling buffer and run FFT when full enough.
        """
        try:
            arr = np.array(pcm, copy=False)
            if arr.ndim > 1:
                arr = arr[:, 0]
            # normalize integer types
            if np.issubdtype(arr.dtype, np.integer):
                info = np.iinfo(arr.dtype)
                arr = arr.astype(np.float32) / float(info.max)
            else:
                arr = arr.astype(np.float32)

            # append to circular buffer (simple rolling)
            n = arr.size
            if n == 0:
                return
            if n >= self.fft_size:
                # if chunk bigger than fft, use last fft_size samples
                new = arr[-self.fft_size:]
                self.buffer[:] = new
                self.buffer_fill = self.fft_size
            else:
                # roll left and append
                shift = n
                self.buffer = np.roll(self.buffer, -shift)
                self.buffer[-n:] = arr
                self.buffer_fill = min(self.fft_size, self.buffer_fill + n)
        except Exception as e:
            # do not let exceptions cross Qt boundary
            print("Visualizer push_chunk error:", e)

        # If buffer has enough samples, compute FFT and update display arrays
        if self.buffer_fill >= self.fft_size:
            self._compute_fft()

    # ----- FFT and mapping -----
    def _get_samplerate(self):
        # try to read sample rate from engine.current_track.sample_rate if available
        try:
            if self.engine and getattr(self.engine, "current_track", None):
                sr = getattr(self.engine.current_track, "sample_rate", None)
                if sr:
                    return int(sr)
        except Exception:
            pass
        return 44100

    def _compute_fft(self):
        sr = self._get_samplerate()
        # windowed FFT
        win = np.hanning(self.fft_size).astype(np.float32)
        data = self.buffer * win
        spec = np.fft.rfft(data)
        mag = np.abs(spec) / (self.fft_size / 2.0)   # amplitude normalization
        # avoid log of zero
        mag = np.maximum(mag, 1e-12)
        freqs = np.fft.rfftfreq(self.fft_size, d=1.0 / sr)

        # Convert to dB
        db = 20.0 * np.log10(mag)

        # apply A-weighting if requested
        if self.a_weight:
            db = db + a_weighting(freqs)

        # Map to log-frequency bands
        band_db = self._map_to_log_bands(freqs, db, self.bands, self.min_freq, self.max_freq)

        # smoothing (EMA)
        alpha = 1.0 - self.smoothing  # we want smoothing near 1 to be slow; alpha is update rate
        self.smoothed = (1.0 - alpha) * self.smoothed + alpha * band_db
        self.latest_db = band_db

        # update peaks (peak hold)
        now = time.time()
        for i in range(self.bands):
            v = self.smoothed[i]
            if v > self.peak_values[i]:
                self.peak_values[i] = v
                self.peak_times[i] = now
            else:
                # decay after hold time
                if (now - self.peak_times[i]) > self.peak_hold_seconds:
                    # decay peak slowly towards smoothed value
                    self.peak_values[i] = max(self.smoothed[i], self.peak_values[i] - 0.6)

    def _map_to_log_bands(self, freqs, db, bands, fmin, fmax):
        """
        Map linear FFT bins to `bands` log-spaced frequency columns between fmin and fmax.
        We compute weighted average of FFT bins falling into each band (in linear power domain).
        """
        fmin = max(0.1, float(fmin))
        fmax = float(fmax)
        log_bins = np.logspace(math.log10(fmin), math.log10(fmax), num=bands + 1)
        band_vals = np.full(bands, -200.0, dtype=np.float32)

        power = 10 ** (db / 10.0)  # convert dB to linear power
        for i in range(bands):
            low = log_bins[i]
            high = log_bins[i + 1]
            # select FFT bins within this band
            idx = np.where((freqs >= low) & (freqs < high))[0]
            if idx.size == 0:
                # if no bin falls in band, pick nearest bin
                idx = np.array([np.argmin(np.abs(freqs - (low + high) / 2.0))], dtype=int)
            # average power then convert back to dB
            pw = np.mean(power[idx])
            band_vals[i] = 10.0 * np.log10(max(pw, 1e-12))
        return band_vals

    # ----- drawing -----
    def paintEvent(self, ev):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()

        # background
        painter.fillRect(rect, self.bg)
        w = rect.width()
        h = rect.height()
        margin_top = 36
        margin_bottom = 28
        plot_h = h - margin_top - margin_bottom
        plot_y = margin_top

        # draw horizontal dB grid lines (e.g., -90, -60, -30, 0 dB)
        db_lines = [-90, -60, -40, -30, -20, -10, 0, 6]
        pen = QPen(self.grid)
        pen.setWidth(1)
        painter.setPen(pen)
        for db_val in db_lines:
            y = self._db_to_y(db_val, plot_y, plot_h)
            painter.drawLine(0, y, w, y)
            # label
            painter.setFont(self.font)
            painter.setPen(self.text_color)
            painter.drawText(4, y - 2, f"{db_val} dB")
            painter.setPen(pen)

        # compute x positions for bands (log spaced visually)
        xs = np.logspace(math.log10(self.min_freq), math.log10(self.max_freq), num=self.bands)
        # map to x pixel positions (log axis)
        freqs_log = np.logspace(math.log10(self.min_freq), math.log10(self.max_freq), num=self.bands)
        x_positions = np.log10(freqs_log / self.min_freq) / math.log10(self.max_freq / self.min_freq)
        x_pixels = (x_positions * (w - 120)) + 60.0  # leave left/right margins

        # draw bars
        bar_width = max(1.0, (w - 120) / self.bands * 0.9)
        for i in range(self.bands):
            dbv = float(self.smoothed[i])
            y = self._db_to_y(dbv, plot_y, plot_h)
            x = x_pixels[i]
            # bar rect: from y to bottom of plot area
            painter.setPen(Qt.PenStyle.NoPen)
            # gradient color based on amplitude
            painter.setBrush(self.bar_fg)
            painter.drawRect(int(x), int(y), int(bar_width), int(plot_y + plot_h - y))

            # peak marker
            pv = float(self.peak_values[i])
            py = self._db_to_y(pv, plot_y, plot_h)
            painter.setPen(QPen(self.peak_fg))
            painter.drawLine(int(x), int(py), int(x + bar_width), int(py))

        # draw frequency labels for octave ticks and reference points
        painter.setPen(self.text_color)
        painter.setFont(self.font)
        ticks = [20, 40, 80, 160, 320, 640, 1250, 2500, 5000, 10000, 20000]
        for f in ticks:
            if f < self.min_freq or f > self.max_freq:
                continue
            pos = (math.log10(f / self.min_freq) / math.log10(self.max_freq / self.min_freq))
            x = pos * (w - 120) + 60
            painter.setPen(QPen(self.grid))
            painter.drawLine(int(x), plot_y + plot_h, int(x), plot_y + plot_h + 6)
            painter.setPen(self.text_color)
            painter.drawText(int(x) - 18, plot_y + plot_h + 20, f"{f:g}")

        # bottom labels: samplerate and mode
        sr = self._get_samplerate()
        mode = "A-weight ON" if self.a_weight else "Linear"
        painter.setPen(self.text_color)
        painter.drawText(8, h - 6, f"SR: {sr} Hz  |  Mode: {mode}  |  Smoothing: {self.smoothing:.2f}")

        painter.end()

    def _db_to_y(self, db_val, plot_y, plot_h):
        # map dB range [-100 .. +12] to pixel y (higher dB => smaller y)
        top_db = 12.0
        bottom_db = -100.0
        frac = (db_val - top_db) / (bottom_db - top_db)
        frac = max(0.0, min(1.0, frac))
        return int(plot_y + frac * plot_h)

    # expose a simple API to set min/max frequency
    def set_frequency_range(self, fmin, fmax):
        self.min_freq = float(max(1.0, fmin))
        self.max_freq = float(fmax)
