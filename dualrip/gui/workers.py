"""
Background workers (plain Python threads, signals > GUI via Qt queued delivery).

CRITICAL: signal connections MUST be bound methods of a GUI-thread QObject (never lambdas). Lambdas can execute in the worker thread and corrupt Qt's heap.
Keep a Python ref until the terminal signal, then wait() before dropping.
"""

import threading
import time
import numpy as np
from PySide6.QtCore import QObject, Signal
from ..engine.render import LiveRenderer, render_entry_stream
from ..export import RenderResult, rip_archive, rip_sequences
from . import audio

# --- streaming preview pacing ---
PRIME_SECONDS = 0.3 # buffer before playback auto-starts
PACE_SLEEP = 0.03 # per-chunk GIL yield once playing (~5× realtime, no crackle)
# Skipped during priming or when playhead > buffered > render at full speed.


class _ThreadWorker(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def wait(self):
        """Join the worker thread. Called from the terminal-signal handler, where the thread is already past its last emit, so this returns almost immediately."""
        if self._thread.is_alive():
            self._thread.join()

    def _emit(self, sig, *args):
        try:
            sig.emit(*args)
        except RuntimeError:
            pass

    def _run(self):
        raise NotImplementedError

class StreamWorker(_ThreadWorker):
    """
    Incremental render > audio stream + accumulate for cache.

    done(key, RenderResult) on completion; failed(key, msg) on error.
    cancel() stops early (superseded) — no signal, audio left for caller.
    """

    done = Signal(object, object) # request key, RenderResult
    failed = Signal(object, str) # request key, error message

    def __init__(self, key, sdat, seqarc, entry, rate, resolver, parent=None):
        super().__init__(parent)
        self._key = key
        self._sdat = sdat
        self._seqarc = seqarc
        self._entry = entry
        self._rate = rate
        self._resolver = resolver
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def _pace(self):
        if audio.state() == audio.PLAYING and audio.position() < audio.buffered():
            time.sleep(PACE_SLEEP)

    def _start_racer(self, token, blob, offset, bank, waveArc, entry_volume, player_prio):
        """State-only LiveRenderer fast-forward → exact total before the streaming render produces much audio (~50-500× faster, no numpy synthesis)."""

        def run():
            try:
                r = LiveRenderer(blob, offset, bank, waveArc, entry_volume, self._rate, player_prio=player_prio, loop_passes=2)
                while not r.finished and not self._cancel.is_set():
                    r.step(produce=False)
                if not self._cancel.is_set():
                    audio.set_estimated_total(token, r.emitted)
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def _run(self):
        key, entry = self._key, self._entry
        if entry.offset is None:
            self._emit(self.done, key, RenderResult(entry.index, entry.name, 'null'))
            return
        try:
            rbid = self._resolver.resolve(entry)
            label = str(entry.bank_id) if rbid == entry.bank_id else f'{entry.bank_id}->{rbid}'
            bank, wave_arc = self._sdat.bank(rbid)
        except Exception as exc:
            self._emit(self.done, key, RenderResult(entry.index, entry.name, 'error', str(entry.bank_id), error=str(exc)))
            return

        token = audio.stream_begin(self._rate, int(self._rate * PRIME_SECONDS))
        if token is not None:
            self._start_racer(token, self._seqarc.blob, entry.offset, bank, wave_arc, entry.volume, entry.cpr or 0)
        chunks = []
        looped = False
        marks = None
        try:
            for item in render_entry_stream(
                self._seqarc.blob, entry.offset, bank, wave_arc, entry.volume,
                self._rate, player_prio=entry.cpr or 0, loop_passes=2,
            ):
                if self._cancel.is_set():
                    return
                if item[0] == 'data':
                    chunks.append(item[1])
                    audio.stream_feed(item[1])
                    self._pace()
                else:
                    looped, marks = item[1], item[2]
        except Exception as exc:
            self._emit(self.failed, key, str(exc))
            return
        if self._cancel.is_set():
            return

        audio.stream_finalize(marks[0] if marks else None, marks[1] if marks else None)

        full = np.concatenate(chunks) if chunks else np.zeros((0, 2), dtype=np.int16)
        peak = int(np.abs(full.astype(np.int32)).max()) if len(full) else 0
        if len(full) == 0 or peak == 0:
            res = RenderResult(entry.index, entry.name, 'empty', label)
        else:
            ls = marks[0] / self._rate if marks else None
            le = marks[1] / self._rate if marks else None
            res = RenderResult(entry.index, entry.name, 'loop' if looped else 'ok', label, len(full) / self._rate, ls, le, full)
        self._emit(self.done, key, res)

# --- live (music) preview pacing ---
CHECKPOINT_SECONDS = 4.0 # snapshot interval; seek > ffwd <= 4s (~15ms) > instant
RACE_STEP_FRAMES = 8820 # racer step size (~0.2s), yields GIL between
RACE_YIELD = 0.0005 # racer GIL yield per step
PRODUCE_CHUNK_FRAMES = 1024 # audio push size (small > frequent GIL handoff)
PRODUCE_YIELD = 0.001 # producer GIL yield per push
LIVE_PRIME_SECONDS = 0.08 # mirrors audio.live_push prime

class _Checkpoints:
    """Thread-safe LiveRenderer snapshots, sorted by emitted. nearest(target) returns the last snapshot <= target > minimal ffwd."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items = [] # (emitted, snapshot), sorted by emitted

    def add(self, snap):
        with self._lock:
            self._items.append((snap['emitted'], snap))

    def nearest(self, target):
        with self._lock:
            best = None
            for em, snap in self._items:
                if em <= target:
                    best = snap
                else:
                    break
            return best

class LiveWorker(_ThreadWorker):
    """
    Seamless music: on-demand synthesis > ring buffer, instant seek.

    Racer runs state-only (~150× realtime), snapshots every CHECKPOINT_SECONDS. Seek > nearest snapshot + ffwd (~15ms, bit-exact).
    No full-song buffer kept.

    meta(key, RenderResult with audio=None) on exact length/loops known.
    failed(key, msg) on error. cancel() + wait() to clean up.
    """

    meta = Signal(object, object) # request key, RenderResult (audio=None)
    failed = Signal(object, str) # request key, error message

    def __init__(self, key, sdat, seqarc, entry, rate, resolver, parent=None):
        super().__init__(parent)
        self._key = key
        self._sdat = sdat
        self._seqarc = seqarc
        self._entry = entry
        self._rate = rate
        self._resolver = resolver
        self._cancel = threading.Event()
        self._ckpts = _Checkpoints()
        self._args = None
        self._prio = 0
        self._label = ''

    def cancel(self):
        self._cancel.set()

    def _new_renderer(self):
        return LiveRenderer(*self._args, player_prio=self._prio, loop_passes=2)

    def _reseed(self, target):
        """Return a renderer positioned exactly at `target` content frames: the nearest checkpoint restored and silently fast-forwarded (or from 0 if the racer has not reached that region yet)."""
        snap = self._ckpts.nearest(target)
        r = LiveRenderer.from_snapshot(snap) if snap is not None else self._new_renderer()
        r.fast_forward_to(target)
        return r

    def _render_chunk(self, renderer, loop_end):
        """Produce up to PRODUCE_CHUNK_FRAMES of audio. Returns (block, content_start) or (None, None) when the render has finished with nothing left."""
        parts = []
        got = 0
        content_start = None
        while got < PRODUCE_CHUNK_FRAMES and not renderer.finished:
            before = renderer.emitted
            out = renderer.step(produce=True)
            if out is not None:
                if content_start is None:
                    content_start = before
                parts.append(out)
                got += len(out)
            if loop_end is not None and renderer.emitted >= loop_end:
                break
        if content_start is None:
            return None, None
        block = parts[0] if len(parts) == 1 else np.concatenate(parts)
        return block, content_start

    def _racer(self, token, yield_gil=True):
        """
        State-only run > checkpoints + exact length/loop marks.

        When yield_gil=False (called synchronously before the producer), runs at max speed (~150× realtime) — no loop-end undershoot.
        """
        try:
            r = self._new_renderer()
            self._ckpts.add(r.snapshot())
            target = RACE_STEP_FRAMES
            interval = int(CHECKPOINT_SECONDS * self._rate)
            next_ck = interval
            while not r.finished and not self._cancel.is_set():
                r.fast_forward_to(target)
                target += RACE_STEP_FRAMES
                if r.emitted >= next_ck and not r.finished:
                    self._ckpts.add(r.snapshot())
                    next_ck += interval
                if yield_gil:
                    time.sleep(RACE_YIELD)
            if self._cancel.is_set():
                return
            total = r.emitted
            marks = r.loop_marks
            looped = r.ply.loop_detected
            if token is not None:
                audio.live_set_total(token, total, exact=True, loop_marks=marks)
            ls = marks[0] / self._rate if marks else None
            le = marks[1] / self._rate if marks else None
            res = RenderResult(
                self._entry.index, self._entry.name,
                'loop' if looped else 'ok', self._label,
                total / self._rate, ls, le, None)
            self._emit(self.meta, self._key, res)
        except Exception:
            pass

    def _wait_for_seek(self, token):
        """After the (non-looping) natural end, stay alive so a later drag still re-seeds. Returns True when a seek arrived, False on cancel."""
        while not self._cancel.is_set():
            if audio.live_seek_pending(token):
                return True
            time.sleep(0.03)
        return False

    def _run(self):
        key, entry = self._key, self._entry
        if entry.offset is None:
            self._emit(self.meta, key, RenderResult(entry.index, entry.name, 'null'))
            return
        try:
            rbid = self._resolver.resolve(entry)
            self._label = str(entry.bank_id) if rbid == entry.bank_id else f'{entry.bank_id}->{rbid}'
            bank, wave_arc = self._sdat.bank(rbid)
        except Exception as exc:
            self._emit(self.meta, key, RenderResult(entry.index, entry.name, 'error', str(entry.bank_id), error=str(exc)))
            return

        self._prio = entry.cpr or 0
        self._args = (self._seqarc.blob, entry.offset, bank, wave_arc, entry.volume, self._rate)

        token = audio.live_begin(self._rate)

        self._racer(token, yield_gil=False)

        if token is None or self._cancel.is_set():
            return

        renderer = self._new_renderer()
        try:
            while not self._cancel.is_set():
                target = audio.consume_seek(token)
                if target is not None:
                    renderer = self._reseed(target)

                marks = audio.loop_region()
                looping = marks is not None and audio.loop_enabled()
                if looping and renderer.emitted >= marks[1]:
                    renderer = self._reseed(marks[0])

                loop_end = marks[1] if looping else None
                block, content_start = self._render_chunk(renderer, loop_end)
                if block is None:
                    # natural end of the raw render
                    if audio.loop_enabled():
                        region = audio.loop_region()
                        renderer = self._reseed(region[0] if region else 0)
                        continue
                    audio.live_end(token)
                    if not self._wait_for_seek(token):
                        break
                    continue

                off = 0
                m = len(block)
                while off < m and not self._cancel.is_set():
                    w = audio.live_push(token, content_start + off, block[off:])
                    if w == 0:
                        if audio.live_seek_pending(token):
                            break # abandon this block; the seek is handled next loop
                        audio.live_wait_space(token, 0.05)
                    else:
                        off += w
                time.sleep(PRODUCE_YIELD) # yield the GIL to the audio callback
        except Exception as exc:
            self._emit(self.failed, key, str(exc))

class BatchWorker(_ThreadWorker):
    """Rip a list of tagged jobs to disk, with per-entry progress, per-job
    summaries and cancellation.

    sdats: OrderedDict[sdat_key, (label, SdatFile)]
    Each job is (sdat_key, kind, ident, sel):
      ('arc', arc_id, only_set_or_None) -> one SSAR archive
      ('seq', None, seq_set_or_None) -> standalone SSEQ music (None = all)
    """

    batch_progress = Signal(int, int, object) # done, total, RenderResult
    archive_done = Signal(object) # per-job summary dict
    batch_done = Signal(object) # list of summaries
    failed = Signal(str)

    def __init__(self, sdats, jobs, out_root, rate, override_map=None, parent=None):
        super().__init__(parent)
        self._sdats = sdats
        self._jobs = jobs
        self._out_root = out_root
        self._rate = rate
        self._override = override_map
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def _sdat(self, sk):
        return self._sdats[sk][1]

    def _seq_ids(self, sk, sel):
        if sel is not None:
            return sorted(sel)
        sdat = self._sdat(sk)
        return [sid for sid, _n, _b in sdat.sequence_list]

    def _job_size(self, sk, kind, ident, sel):
        if kind == 'seq':
            return len(self._seq_ids(sk, sel))
        if sel is not None:
            return len(sel)
        return len(self._sdat(sk).seqarc(ident).entries)

    def _run(self):
        try:
            summaries = []
            grand_total = sum(self._job_size(j[0], j[1], j[2], j[3]) for j in self._jobs)
            base = 0
            for sk, kind, ident, sel in self._jobs:
                if self._cancel.is_set():
                    break
                sdat = self._sdat(sk)

                def progress(done, _total, res, _base=base):
                    self._emit(self.batch_progress, _base + done, grand_total, res)

                if kind == 'seq':
                    summary = rip_sequences(
                        sdat,
                        self._seq_ids(sk, sel),
                        self._out_root,
                        rate=self._rate,
                        override_map=self._override,
                        progress=progress,
                        should_cancel=self._cancel.is_set,
                    )
                else:
                    summary = rip_archive(
                        sdat,
                        ident,
                        self._out_root,
                        rate=self._rate,
                        override_map=self._override,
                        only=sel,
                        progress=progress,
                        should_cancel=self._cancel.is_set,
                    )
                summaries.append(summary)
                self._emit(self.archive_done, summary)
                base += self._job_size(sk, kind, ident, sel)
            self._emit(self.batch_done, summaries)
        except Exception as exc:
            self._emit(self.failed, str(exc))
