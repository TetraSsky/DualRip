"""
Playback bar widget (seek slider, loop toggle, Play/Pause/Stop).
"""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QStyle,
    QStyleOptionSlider,
    QVBoxLayout,
)
from . import audio

POLL_MS = 40  # playhead refresh period

class SeekSlider(QSlider):
    """Slider that seeks to clicked position (default QSlider pages)."""

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            groove = self.style().subControlRect(
                QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)
            handle = self.style().subControlRect(
                QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)
            span = groove.width() - handle.width()
            x = event.position().x() - groove.x() - handle.width() / 2
            if span > 0:
                value = QStyle.sliderValueFromPosition(
                    self.minimum(), self.maximum(), round(x), span)
                self.setValue(value)
        super().mousePressEvent(event)

class PlayerBar(QFrame):
    play_clicked = Signal() # the main window decides what to (re)render

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.loaded_key = None
        self._rate = 0
        self._dragging = False

        top = QHBoxLayout()
        self.slider = SeekSlider(Qt.Horizontal, self)
        self.slider.setRange(0, 0)
        self.slider.sliderPressed.connect(self._drag_started)
        self.slider.sliderReleased.connect(self._drag_finished)
        self.slider.sliderMoved.connect(self._drag_moved)
        top.addWidget(self.slider, 1)
        self.chk_loop = QCheckBox('Loop', self)
        self.chk_loop.setToolTip("Loop playback; uses the sound's own loop points when it has some, otherwise the whole sound.")
        self.chk_loop.toggled.connect(audio.set_loop)
        top.addWidget(self.chk_loop)

        mid = QHBoxLayout()
        self.lbl_time = QLabel('-', self)
        mid.addStretch(1)
        mid.addWidget(self.lbl_time)
        mid.addStretch(1)

        bottom = QHBoxLayout()
        self.btn_play = QPushButton('Play', self)
        self.btn_play.clicked.connect(self.play_clicked)
        self.btn_pause = QPushButton('Pause', self)
        self.btn_pause.clicked.connect(audio.pause)
        self.btn_stop = QPushButton('Stop', self)
        self.btn_stop.clicked.connect(audio.stop)
        bottom.addWidget(self.btn_play)
        bottom.addStretch(1)
        bottom.addWidget(self.btn_pause)
        bottom.addStretch(1)
        bottom.addWidget(self.btn_stop)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addLayout(mid)
        lay.addLayout(bottom)

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)

        if not audio.available():
            self.setToolTip('sounddevice is not installed; playback disabled')

    def load_result(self, key, res, rate):
        marks = None
        if res.loop_start is not None:
            marks = (round(res.loop_start * rate), round(res.loop_end * rate))
        if not audio.load(res.audio, rate,
                          marks[0] if marks else None,
                          marks[1] if marks else None):
            return False
        self.loaded_key = key
        self._rate = rate
        self.slider.setRange(0, max(audio.duration() - 1, 0))
        audio.set_loop(self.chk_loop.isChecked())
        audio.play()
        self._timer.start()
        self._poll()
        return True

    def resume(self):
        audio.play()

    def clear(self):
        audio.unload()
        self.loaded_key = None
        self._timer.stop()
        self.slider.setRange(0, 0)
        self.lbl_time.setText('-')

    def setEnabled(self, enabled): # noqa: N802 (Qt override)
        super().setEnabled(enabled and audio.available())

    def _fmt(self, frames):
        if self._rate <= 0:
            return '0.000'
        return f'{frames / self._rate:.3f}'

    def _poll(self):
        total = audio.duration()
        pos = audio.position()
        if not self._dragging:
            self.slider.setValue(pos)
            self.lbl_time.setText(
                f'{self._fmt(pos)} / {self._fmt(total)} s'
                if total else '-')

    def _drag_started(self):
        self._dragging = True

    def _drag_moved(self, value):
        self.lbl_time.setText(
            f'{self._fmt(value)} / {self._fmt(audio.duration())} s')

    def _drag_finished(self):
        self._dragging = False
        audio.seek(self.slider.value())
