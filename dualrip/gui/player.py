"""
Playback bar widget (seek slider, loop toggle, Play/Pause/Stop).
"""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPainter
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

POLL_MS = 40 # playhead refresh period

class SeekSlider(QSlider):
    """Slider with click-to-seek + buffered-progress band underneath."""

    def __init__(self, *args):
        super().__init__(*args)
        self._buffered_frac = 1.0

    def set_buffered_fraction(self, frac):
        frac = 0.0 if frac < 0.0 else (1.0 if frac > 1.0 else frac)
        if abs(frac - self._buffered_frac) > 0.002 or (
            (frac >= 1.0) != (self._buffered_frac >= 1.0)
        ):
            self._buffered_frac = frac
            self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._buffered_frac >= 1.0 or self.maximum() <= 0:
            return
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(
            QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)
        color = self.palette().highlight().color()
        color.setAlpha(90)
        painter = QPainter(self)
        painter.fillRect(groove.x(), groove.bottom() + 2, int(groove.width() * self._buffered_frac), 2, color)
        painter.end()

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
    loaded_changed = Signal(object) # cache key of loaded track

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self._loaded_key = None
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
        bottom.addWidget(self.btn_pause)
        bottom.addWidget(self.btn_stop)
        bottom.addStretch(1)
        for w in (self.btn_play, self.btn_pause, self.btn_stop, self.chk_loop):
            w.setFocusPolicy(Qt.NoFocus)

        self.vol_slider = QSlider(Qt.Horizontal, self)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(int(audio.volume() * 100))
        self.vol_slider.setFixedWidth(100)
        self.vol_slider.valueChanged.connect(lambda v: (
            audio.set_volume(v / 100.0),
            self.vol_lbl.setText(f'{v}%'),
        ))
        self.vol_lbl = QLabel('100%', self)
        self.vol_lbl.setFixedWidth(36)
        self.vol_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        bottom.addWidget(self.vol_slider)
        bottom.addWidget(self.vol_lbl)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addLayout(mid)
        lay.addLayout(bottom)

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)

        if not audio.available():
            self.setToolTip('sounddevice is not installed; playback disabled')

    @property
    def loaded_key(self):
        return self._loaded_key

    def _set_loaded_key(self, key):
        if key != self._loaded_key:
            self._loaded_key = key
            self.loaded_changed.emit(key)

    def load_result(self, key, res, rate):
        marks = None
        if res.loop_start is not None:
            marks = (round(res.loop_start * rate), round(res.loop_end * rate))
        if not audio.load(res.audio, rate, marks[0] if marks else None, marks[1] if marks else None):
            return False
        self._set_loaded_key(key)
        self._rate = rate
        self.slider.setRange(0, max(audio.duration() - 1, 0))
        audio.set_loop(self.chk_loop.isChecked())
        audio.play()
        self._timer.start()
        self._poll()
        return True

    def begin_stream(self, key, rate):
        """Arm bar for streaming render. Full-track estimate arrives in ~300ms (sequencer-only), then exact total snaps at finalize."""
        self._set_loaded_key(key)
        self._rate = rate
        self.slider.setRange(0, 0)
        self.slider.set_buffered_fraction(0.0)
        self._timer.start()
        self._poll()

    def begin_live(self, key, rate):
        """Arm bar for live (ring) music render. Whole track seekable from start via checkpoints — no buffered band, drag jumps instantly."""
        self._set_loaded_key(key)
        self._rate = rate
        self.slider.setRange(0, 0)
        self.slider.set_buffered_fraction(1.0)
        self._timer.start()
        self._poll()

    def resume(self):
        audio.play()

    def clear(self):
        audio.unload()
        self._set_loaded_key(None)
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
        buffered = audio.buffered()
        pos = audio.position()
        final = audio.is_final()
        self.slider.setRange(0, max(total - 1, 0))
        self.slider.set_buffered_fraction(
            1.0 if final or not total else buffered / total)
        if not self._dragging:
            self.slider.setValue(pos)
            if not total:
                self.lbl_time.setText('-')
            else:
                text = f'{self._fmt(pos)} / {self._fmt(total)} s'
                if not final and pos >= buffered:
                    text += ' (rendering...)'
                self.lbl_time.setText(text)

    def _drag_started(self):
        self._dragging = True

    def _drag_moved(self, value):
        self.lbl_time.setText(
            f'{self._fmt(value)} / {self._fmt(audio.duration())} s')

    def _drag_finished(self):
        self._dragging = False
        if audio.is_live():
            audio.request_seek(self.slider.value())
        else:
            audio.seek(self.slider.value())
