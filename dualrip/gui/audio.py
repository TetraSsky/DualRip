"""
Audio preview backend with a small transport: load / play / pause / stop /
seek / loop, plus position polling for a playhead.

A single persistent OutputStream is kept open and fed by callback. Opening
and closing one PortAudio/WASAPI stream per preview (what sounddevice's
sd.play() does) is what crashed rapid successive previews on Windows.

The GUI polls position()/state() from a timer; the callback never touches Qt.

sounddevice is optional: without it the GUI still works, only playback is
disabled.
"""

import threading
import numpy as np

try:
    import sounddevice as _sd
except Exception:  # missing package or no audio backend on this machine
    _sd = None

STOPPED = 'stopped'
PLAYING = 'playing'
PAUSED = 'paused'

class _Player:
    def __init__(self):
        self._lock = threading.Lock()
        self._stream = None
        self._stream_rate = None
        self._data = None       # int16 stereo ndarray, kept loaded across stops
        self._rate = 0
        self._pos = 0           # playhead, in frames
        self._state = STOPPED
        self._loop = False
        self._loop_start = 0    # loop region, in frames (end exclusive);
        self._loop_end = 0      # equals (0, len) when the sound has no marks

    # PortAudio callback: copy the next slice of the current buffer, wrapping
    # inside the loop region when looping; silence when idle. Never raises.
    def _callback(self, outdata, frames, _time, _status):
        try:
            with self._lock:
                data = self._data
                if data is None or self._state != PLAYING:
                    outdata[:] = 0
                    return
                out = 0
                while out < frames:
                    end = self._loop_end if self._loop else len(data)
                    chunk = data[self._pos:min(self._pos + frames - out, end)]
                    n = len(chunk)
                    outdata[out:out + n] = chunk
                    out += n
                    self._pos += n
                    if self._pos >= end:
                        if self._loop:
                            self._pos = self._loop_start
                        else:
                            self._state = STOPPED
                            self._pos = 0
                            break
                outdata[out:] = 0
        except Exception:
            outdata[:] = 0

    def _ensure_stream(self, rate):
        if self._stream is not None and self._stream_rate == rate:
            return True
        old, self._stream = self._stream, None
        if old is not None:
            try:
                old.stop()
                old.close()
            except Exception:
                pass
        try:
            stream = _sd.OutputStream(samplerate=rate, channels=2, dtype='int16', callback=self._callback)
            stream.start()
        except Exception:
            return False
        self._stream = stream
        self._stream_rate = rate
        return True

    def load(self, audio, rate, loop_start=None, loop_end=None):
        """Load a sound (int16 stereo ndarray) without starting playback.
        loop_start/loop_end are frame indices of the sound's own loop region;
        looping falls back to the whole buffer when absent or degenerate."""
        if _sd is None:
            return False
        audio = np.ascontiguousarray(audio, dtype=np.int16)
        if audio.ndim != 2 or audio.shape[1] != 2 or len(audio) == 0:
            return False
        with self._lock:
            self._data = None       # silence while (re)arming
            self._state = STOPPED
            self._pos = 0
        if not self._ensure_stream(rate):
            return False
        ls = 0 if loop_start is None else int(loop_start)
        le = len(audio) if loop_end is None else int(loop_end)
        if not 0 <= ls < le <= len(audio):
            ls, le = 0, len(audio)
        with self._lock:
            self._data = audio
            self._rate = rate
            self._loop_start = ls
            self._loop_end = le
        return True

    def play(self):
        """Start, or resume from the current playhead."""
        with self._lock:
            if self._data is not None:
                self._state = PLAYING

    def pause(self):
        with self._lock:
            if self._state == PLAYING:
                self._state = PAUSED

    def stop(self):
        """Halt and rewind; the buffer stays loaded."""
        with self._lock:
            self._state = STOPPED
            self._pos = 0

    def seek(self, frame):
        with self._lock:
            if self._data is not None:
                self._pos = max(0, min(int(frame), len(self._data)))

    def set_loop(self, enabled):
        with self._lock:
            self._loop = bool(enabled)

    def unload(self):
        with self._lock:
            self._data = None
            self._state = STOPPED
            self._pos = 0

    def state(self):
        with self._lock:
            return self._state

    def position(self):
        with self._lock:
            return self._pos

    def duration(self):
        with self._lock:
            return 0 if self._data is None else len(self._data)

    def rate(self):
        with self._lock:
            return self._rate

    def shutdown(self):
        self.unload()
        old, self._stream = self._stream, None
        if old is not None:
            try:
                old.stop()
                old.close()
            except Exception:
                pass

_player = _Player()

def available():
    return _sd is not None

def load(audio, rate, loop_start=None, loop_end=None):
    """Load an int16 stereo ndarray into the player. Returns success."""
    return _player.load(audio, rate, loop_start, loop_end)

def play():
    _player.play()

def pause():
    _player.pause()

def stop():
    _player.stop()

def seek(frame):
    _player.seek(frame)

def set_loop(enabled):
    _player.set_loop(enabled)

def state():
    return _player.state()

def position():
    return _player.position()

def duration():
    return _player.duration()

def unload():
    _player.unload()

def shutdown():
    """Close the output stream (call on application exit)."""
    _player.shutdown()
