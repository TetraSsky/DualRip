"""
Audio preview backend (zero Qt imports — decoupled, testable standalone).

Two feed modes:
  load() — complete int16 stereo buffer (SFX, cached replays).
  stream_begin/feed/finalize — growing buffer (streaming music render).
  live_begin/push/end — ring-buffer for live seekable preview.

Single persistent PortAudio OutputStream (sd.play crashes rapid previews on Windows). Callback never touches Qt. sounddevice is optional.
"""

import threading
import numpy as np

try:
    import sounddevice as _sd
except Exception: # missing package or no audio backend on this machine
    _sd = None

STOPPED = 'stopped'
PLAYING = 'playing'
PAUSED = 'paused'

BLOCKSIZE = 1024
STREAM_INITIAL_SECONDS = 8
RING_SECONDS = 2.0

class _Player:
    def __init__(self):
        self._lock = threading.Lock()
        self._stream = None
        self._stream_rate = None
        self._data = None # int16 stereo ndarray (capacity, not length)
        self._rate = 0
        self._pos = 0 # playhead, in frames
        self._filled = 0 # valid frames in _data (== capacity when whole)
        self._cap = 0 # allocated frames in _data
        self._final = True # False while a stream is still being fed
        self._armed = False # want to auto-start once primed
        self._prime = 0 # frames to buffer before auto-starting
        self._est_total = 0 # estimated total frames while streaming
        self._epoch = 0 # bumped on every (re)load; stale-writer guard
        self._state = STOPPED
        self._loop = False
        self._loop_start = 0 # loop region, in frames (end exclusive);
        self._loop_end = 0 # equals (0, filled) when the sound has no marks
        self._underruns = 0 # PortAudio output_underflow reports
        self._live = False
        self._ring = None # int16 (R, 2) circular audio
        self._cpos = None # int64 (R,) content sample index per ring frame
        self._R = 0 # ring capacity in frames
        self._w = 0 # stream write index (monotonic)
        self._rd = 0 # stream read index (monotonic); valid = [_rd, _w)
        self._cur_content = 0 # content position of the playhead (for the UI)
        self._content_total = 0 # exact total once known, else 0
        self._armed_live = False # auto-start once first block arrives
        self._ended = False # producer reached the natural end (no loop)
        self._seek_epoch = 0 # bumped on every seek request
        self._seek_target = None # pending seek (content frame) for the producer
        self._cond = threading.Condition(self._lock) # wakes producer on space/seek

    def _callback(self, outdata, frames, _time, status):
        try:
            with self._lock:
                if self._live:
                    if status and status.output_underflow:
                        self._underruns += 1
                    self._callback_live(outdata, frames)
                    return
                data = self._data
                if data is None or self._state != PLAYING:
                    outdata[:] = 0
                    return
                if status and status.output_underflow:
                    self._underruns += 1
                filled = self._filled
                loop = self._loop and self._final
                out = 0
                while out < frames:
                    end = self._loop_end if loop else filled
                    if self._pos >= end:
                        if loop:
                            self._pos = self._loop_start
                            continue
                        if self._final:
                            self._state = STOPPED
                            self._pos = 0
                            break
                        break
                    take = min(self._pos + frames - out, end)
                    chunk = data[self._pos:take]
                    m = len(chunk)
                    outdata[out:out + m] = chunk
                    out += m
                    self._pos += m
                outdata[out:] = 0
        except Exception:
            outdata[:] = 0

    # --- live (ring) mode: called under _lock from _callback ---
    def _callback_live(self, outdata, frames):
        if self._state != PLAYING or self._ring is None:
            outdata[:] = 0
            return
        R = self._R
        avail = self._w - self._rd
        if avail <= 0:
            outdata[:] = 0
            if self._ended:
                self._state = STOPPED
            return
        take = min(frames, avail)
        start = self._rd % R
        end = start + take
        if end <= R:
            outdata[:take] = self._ring[start:end]
            last = (end - 1) % R
        else:
            first = R - start
            outdata[:first] = self._ring[start:R]
            outdata[first:take] = self._ring[0:end - R]
            last = (end - R - 1) % R
        if take < frames:
            outdata[take:] = 0
        self._rd += take
        self._cur_content = int(self._cpos[last]) + 1
        self._cond.notify()

    def live_begin(self, rate, est_total=0, loop_marks=None):
        """Arm live (ring) playback. A background producer then feeds blocks via live_push() and re-seeds on request_seek(). Returns an epoch token (or None without audio)."""
        if _sd is None:
            return None
        with self._lock:
            self._data = None
            self._state = STOPPED
            self._live = False
        if not self._ensure_stream(rate):
            return None
        R = max(int(rate * RING_SECONDS), BLOCKSIZE * 4)
        ring = np.zeros((R, 2), dtype=np.int16)
        cpos = np.zeros(R, dtype=np.int64)
        with self._lock:
            self._live = True
            self._ring = ring
            self._cpos = cpos
            self._R = R
            self._rate = rate
            self._w = 0
            self._rd = 0
            self._cur_content = 0
            self._content_total = 0
            self._est_total = int(est_total)
            self._final = False
            self._ended = False
            self._armed_live = True
            self._state = STOPPED
            self._epoch += 1
            self._seek_epoch = 0
            self._seek_target = None
            if loop_marks:
                self._loop_start, self._loop_end = int(loop_marks[0]), int(loop_marks[1])
            else:
                self._loop_start = self._loop_end = 0
            self._underruns = 0
            return self._epoch

    def live_push(self, token, content_start, block):
        """Append up to the free ring space (producer thread). Returns the number of frames actually written (0 if full or the token is stale)."""
        block = np.ascontiguousarray(block, dtype=np.int16)
        m = len(block)
        if m == 0:
            return 0
        with self._lock:
            if token != self._epoch or not self._live:
                return 0
            free = self._R - (self._w - self._rd)
            if free <= 0:
                return 0
            take = min(m, free)
            R = self._R
            start = self._w % R
            end = start + take
            crange = np.arange(content_start, content_start + take, dtype=np.int64)
            if end <= R:
                self._ring[start:end] = block[:take]
                self._cpos[start:end] = crange
            else:
                first = R - start
                self._ring[start:R] = block[:first]
                self._cpos[start:R] = crange[:first]
                self._ring[0:end - R] = block[first:take]
                self._cpos[0:end - R] = crange[first:]
            self._w += take
            prime = min(self._R, max(BLOCKSIZE * 2, int(self._rate * 0.08)))
            if self._armed_live and (self._w - self._rd) >= prime:
                self._state = PLAYING
                self._armed_live = False
            return take

    def live_wait_space(self, token, timeout):
        """Producer: block until the ring has free space, a seek is pending, or the token goes stale."""
        with self._lock:
            if token != self._epoch:
                return False
            if self._seek_target is not None:
                return True
            if self._R - (self._w - self._rd) > 0:
                return True
            self._cond.wait(timeout)
            return token == self._epoch

    def request_seek(self, frame):
        """User seek (any thread): jump the playhead; the producer re-seeds the renderer to match. Drops buffered audio so no stale sound plays."""
        with self._lock:
            if not self._live:
                return
            t = max(0, int(frame))
            if self._content_total:
                t = min(t, self._content_total)
            self._seek_target = t
            self._seek_epoch += 1
            self._cur_content = t
            self._rd = self._w
            self._ended = False
            self._cond.notify()

    def consume_seek(self, token):
        """Producer: fetch and clear a pending seek target (None if none)."""
        with self._lock:
            if token != self._epoch:
                return None
            t = self._seek_target
            self._seek_target = None
            return t

    def live_seek_pending(self, token):
        with self._lock:
            return token == self._epoch and self._seek_target is not None

    def live_end(self, token):
        """Producer: no more audio will be pushed (natural end, no loop). The callback drains what remains, then stops."""
        with self._lock:
            if token != self._epoch:
                return
            self._ended = True
            self._final = True
            if not self._content_total:
                self._content_total = self._cur_content
            self._cond.notify()

    def live_set_total(self, token, frames, exact=False, loop_marks=None):
        """Report the track length: a fast estimate for the seek bar, later the exact total (and loop marks) once the racer has run the whole thing."""
        with self._lock:
            if token != self._epoch or not self._live:
                return
            if exact:
                self._content_total = int(frames)
                self._final = True
                if loop_marks:
                    self._loop_start = int(loop_marks[0])
                    self._loop_end = int(loop_marks[1])
            elif not self._content_total:
                self._est_total = int(frames)

    def loop_enabled(self):
        with self._lock:
            return self._loop

    def loop_region(self):
        with self._lock:
            if self._loop_end > self._loop_start:
                return (self._loop_start, self._loop_end)
            return None

    def is_live(self):
        with self._lock:
            return self._live

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
            stream = _sd.OutputStream(
                samplerate=rate, channels=2, dtype='int16',
                blocksize=BLOCKSIZE, callback=self._callback)
            stream.start()
        except Exception:
            return False
        self._stream = stream
        self._stream_rate = rate
        return True

    def load(self, audio, rate, loop_start=None, loop_end=None):
        """Load a complete sound (int16 stereo ndarray) without starting playback. loop_start/loop_end are frame indices of the sound's own loop region; looping falls back to the whole buffer when absent."""
        if _sd is None:
            return False
        audio = np.ascontiguousarray(audio, dtype=np.int16)
        if audio.ndim != 2 or audio.shape[1] != 2 or len(audio) == 0:
            return False
        with self._lock:
            self._data = None # silence while (re)arming
            self._state = STOPPED
            self._pos = 0
        if not self._ensure_stream(rate):
            return False
        n = len(audio)
        ls = 0 if loop_start is None else int(loop_start)
        le = n if loop_end is None else int(loop_end)
        if not 0 <= ls < le <= n:
            ls, le = 0, n
        with self._lock:
            self._live = False # a complete buffer leaves live (ring) mode
            self._data = audio
            self._rate = rate
            self._filled = n
            self._cap = n
            self._final = True
            self._armed = False
            self._est_total = 0
            self._epoch += 1
            self._loop_start = ls
            self._loop_end = le
            self._pos = 0
            self._underruns = 0
        return True

    def stream_begin(self, rate, prime_frames):
        """Arm a growing buffer; playback auto-starts once prime_frames are fed. Returns an epoch token for set_estimated_total (so an estimator of a superseded stream can never touch a newer one), or None if no audio output is available."""
        if _sd is None:
            return None
        if not self._ensure_stream(rate):
            return None
        cap = max(int(rate) * STREAM_INITIAL_SECONDS, int(prime_frames) + 1)
        buf = np.zeros((cap, 2), dtype=np.int16)
        with self._lock:
            self._live = False # SFX streaming uses the plain growing buffer
            self._data = buf
            self._rate = rate
            self._cap = cap
            self._filled = 0
            self._pos = 0
            self._final = False
            self._armed = True
            self._prime = int(prime_frames)
            self._est_total = 0
            self._epoch += 1
            self._state = STOPPED
            self._loop = self._loop # preserve the user's Loop toggle
            self._loop_start = 0
            self._loop_end = 0
            self._underruns = 0
            return self._epoch

    def set_estimated_total(self, token, frames):
        """Report the sequencer-derived total length (a fast lower bound) so the UI can size its seek bar before the render finishes."""
        with self._lock:
            if token == self._epoch and not self._final:
                self._est_total = int(frames)

    def stream_feed(self, chunk):
        """Append an int16 stereo chunk to the streaming buffer (producer thread only). Auto-starts playback once primed."""
        if self._data is None:
            return
        chunk = np.ascontiguousarray(chunk, dtype=np.int16)
        if chunk.ndim != 2 or chunk.shape[1] != 2 or len(chunk) == 0:
            return
        m = len(chunk)
        if self._filled + m > self._cap:
            new_cap = max(self._filled + m, self._cap * 2)
            new = np.zeros((new_cap, 2), dtype=np.int16)
            with self._lock:
                old = self._data
                filled = self._filled
            new[:filled] = old[:filled]
            with self._lock:
                self._data = new
                self._cap = new_cap
        with self._lock:
            self._data[self._filled:self._filled + m] = chunk
            self._filled += m
            if self._armed and self._filled >= self._prime:
                self._state = PLAYING
                self._armed = False

    def stream_finalize(self, loop_start=None, loop_end=None):
        """Mark the streaming render complete: total length and loop points are now known."""
        with self._lock:
            total = self._filled
            ls = 0 if loop_start is None else int(loop_start)
            le = total if loop_end is None else int(loop_end)
            if not 0 <= ls < le <= total:
                ls, le = 0, total
            self._loop_start = ls
            self._loop_end = le
            self._final = True
            self._est_total = 0
            if self._pos > total:
                self._pos = total
            if self._armed:
                self._state = PLAYING
                self._armed = False

    def play(self):
        """Start, or resume from the current playhead."""
        with self._lock:
            if self._live:
                self._state = PLAYING
                self._armed_live = False
                return
            if self._data is not None:
                self._state = PLAYING
                self._armed = False

    def pause(self):
        with self._lock:
            if self._state == PLAYING:
                self._state = PAUSED

    def stop(self):
        """Halt and rewind; the buffer stays loaded."""
        with self._lock:
            if self._live:
                self._state = STOPPED
                self._cur_content = 0
                self._seek_target = 0
                self._seek_epoch += 1
                self._rd = self._w
                self._ended = False
                self._cond.notify()
                return
            self._state = STOPPED
            self._armed = False
            self._pos = 0

    def _total_locked(self):
        if self._data is None:
            return 0
        if self._final:
            return self._filled
        return max(self._est_total, self._filled)

    def seek(self, frame):
        with self._lock:
            if self._data is not None:
                self._pos = max(0, min(int(frame), self._total_locked()))

    def set_loop(self, enabled):
        with self._lock:
            self._loop = bool(enabled)

    def unload(self):
        with self._lock:
            self._live = False
            self._ring = None
            self._cpos = None
            self._data = None
            self._state = STOPPED
            self._armed = False
            self._armed_live = False
            self._final = True
            self._est_total = 0
            self._content_total = 0
            self._epoch += 1
            self._pos = 0
            self._filled = 0
            self._w = 0
            self._rd = 0
            self._cur_content = 0
            self._ended = False
            self._seek_target = None
            self._cond.notify() # wake any producer so it sees the stale token

    def state(self):
        with self._lock:
            return self._state

    def position(self):
        with self._lock:
            if self._live:
                return self._cur_content
            return self._pos

    def duration(self):
        """Total length in frames: exact once final, otherwise the best known value (estimated total, or the buffered amount until the estimate lands)."""
        with self._lock:
            if self._live:
                return max(self._content_total, self._est_total)
            return self._total_locked()

    def buffered(self):
        """Frames actually rendered and playable so far. In live mode the whole track is seekable (checkpoints), so it reads as fully buffered."""
        with self._lock:
            if self._live:
                return max(self._content_total, self._est_total)
            return 0 if self._data is None else self._filled

    def is_final(self):
        with self._lock:
            return self._final

    def underruns(self):
        with self._lock:
            return self._underruns

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
    """Load a complete int16 stereo ndarray into the player. Returns success."""
    return _player.load(audio, rate, loop_start, loop_end)

def stream_begin(rate, prime_frames):
    """Arm a streaming buffer; returns an epoch token (or None w/o audio)."""
    return _player.stream_begin(rate, prime_frames)

def set_estimated_total(token, frames):
    _player.set_estimated_total(token, frames)

def stream_feed(chunk):
    _player.stream_feed(chunk)

def stream_finalize(loop_start=None, loop_end=None):
    _player.stream_finalize(loop_start, loop_end)

# --- live (ring) mode ------------------------------------------------
def live_begin(rate, est_total=0, loop_marks=None):
    """Arm live ring playback; returns an epoch token (or None w/o audio)."""
    return _player.live_begin(rate, est_total, loop_marks)

def live_push(token, content_start, block):
    return _player.live_push(token, content_start, block)

def live_wait_space(token, timeout):
    return _player.live_wait_space(token, timeout)

def request_seek(frame):
    _player.request_seek(frame)

def consume_seek(token):
    return _player.consume_seek(token)

def live_seek_pending(token):
    return _player.live_seek_pending(token)

def live_end(token):
    _player.live_end(token)

def live_set_total(token, frames, exact=False, loop_marks=None):
    _player.live_set_total(token, frames, exact, loop_marks)

def loop_enabled():
    return _player.loop_enabled()

def loop_region():
    return _player.loop_region()

def is_live():
    return _player.is_live()

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

def buffered():
    return _player.buffered()

def is_final():
    return _player.is_final()

def underruns():
    return _player.underruns()

def unload():
    _player.unload()

def shutdown():
    """Close the output stream (call on application exit)."""
    _player.shutdown()
