"""Background worker threads that emit Qt signals to the GUI."""

import threading
import time
import numpy as np
from PySide6.QtCore import QObject, Signal
from ..engine.ctr import LiveRenderer as CtrLiveRenderer
from ..engine.dse.synth import LiveRenderer as DseLiveRenderer
from ..engine.sdat.render import LiveRenderer
from ..formats.ctr import KIND_LABEL
from ..formats.dse import KIND_LABEL as DSE_KIND_LABEL
from ..export import (
    RenderResult,
    render_ctr_one,
    render_dse_one,
    render_one,
    render_strm_one,
    rip_archive,
    rip_ctr_folder,
    rip_dse_folder,
    rip_sequences,
    rip_streams,
)
from . import audio

DSE_RATE = 32728  # DSE native mixing rate

class _ThreadWorker(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def wait(self):
        """Join the worker thread."""
        if self._thread.is_alive():
            self._thread.join()

    def _emit(self, sig, *args):
        try:
            sig.emit(*args)
        except RuntimeError:
            pass

    def _run(self):
        raise NotImplementedError

CHECKPOINT_SECONDS = 4.0 # snapshot interval, so a seek is at most a 4s ffwd
RACE_STEP_FRAMES = 8820 # racer step size (~0.2s), yields GIL between
RACE_YIELD = 0.0005 # racer GIL yield per step
PRODUCE_CHUNK_FRAMES = 1024 # audio push size (small > frequent GIL handoff)
PRODUCE_YIELD = 0.001 # producer GIL yield per push
LIVE_PRIME_SECONDS = 0.08 # mirrors audio.live_push prime

class _Checkpoints:
    """Thread-safe LiveRenderer snapshots sorted by emitted frame."""

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
    """Seamless music: on-demand synthesis into a ring buffer with instant seek."""

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
        """Renderer positioned at target content frames."""
        snap = self._ckpts.nearest(target)
        r = LiveRenderer.from_snapshot(snap) if snap is not None else self._new_renderer()
        r.fast_forward_to(target)
        return r

    def _render_chunk(self, renderer, loop_end):
        """Produce one audio block, returns (block, content_start) or (None, None)."""
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
        """State-only run building checkpoints and the exact length and loop marks."""
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
            if total == 0:
                # nothing to play: report the one-shot status + cause
                self._emit(self.meta, self._key, render_one(
                    self._sdat, self._seqarc, self._entry, self._rate, self._resolver))
                return
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
        """Stay alive after the natural end so a later drag can re-seed."""
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
                            break # abandon this block, the seek is handled next loop
                        audio.live_wait_space(token, 0.05)
                    else:
                        off += w
                time.sleep(PRODUCE_YIELD) # yield the GIL to the audio callback
        except Exception as exc:
            self._emit(self.failed, key, str(exc))

class CtrPreviewWorker(_ThreadWorker):
    """Full in-memory render of one 3DS CSAR sound for preview."""

    done = Signal(object, object)
    failed = Signal(object, str)

    def __init__(self, key, archive, sound, rate, layer=None, parent=None):
        super().__init__(parent)
        self._key = key
        self._archive = archive
        self._sound = sound
        self._rate = rate
        self._layer = layer
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def _run(self):
        try:
            res, _chans, _native_rate, _loop = render_ctr_one(
                self._archive, self._sound, self._rate, layer=self._layer)
        except Exception as exc:
            self._emit(self.failed, self._key, str(exc))
            return
        if self._cancel.is_set():
            return
        self._emit(self.done, self._key, res)

class CtrLiveWorker(_ThreadWorker):
    """Seamless 3DS sequence: on-demand synthesis into a ring buffer with instant seek."""

    meta = Signal(object, object) # request key, RenderResult (audio=None)
    failed = Signal(object, str) # request key, error message

    def __init__(self, key, archive, sound, rate, parent=None):
        super().__init__(parent)
        self._key = key
        self._archive = archive
        self._sound = sound
        self._rate = rate
        self._cancel = threading.Event()
        self._ckpts = _Checkpoints()
        self._args = None

    def cancel(self):
        self._cancel.set()

    def _new_renderer(self):
        return CtrLiveRenderer(*self._args, loop_passes=2, base_vol=self._sound.volume)

    def _reseed(self, target):
        """Renderer positioned at target content frames."""
        snap = self._ckpts.nearest(target)
        r = CtrLiveRenderer.from_snapshot(snap) if snap is not None else self._new_renderer()
        r.fast_forward_to(target)
        return r

    def _render_chunk(self, renderer, loop_end):
        """Produce one audio block, returns (block, content_start) or (None, None)."""
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
        """State-only run building checkpoints and the exact length and loop marks."""
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
            if token is not None:
                audio.live_set_total(token, total, exact=True, loop_marks=marks)
            if total == 0:
                # nothing to play: report the one-shot status + cause
                res, _chans, _native_rate, _loop = render_ctr_one(self._archive, self._sound, self._rate, keep_audio=False)
                self._emit(self.meta, self._key, res)
                return
            ls = marks[0] / self._rate if marks else None
            le = marks[1] / self._rate if marks else None
            res = RenderResult(
                self._sound.index, self._sound.name,
                'loop' if marks else 'ok', KIND_LABEL[self._sound.kind],
                total / self._rate, ls, le, None)
            self._emit(self.meta, self._key, res)
        except Exception:
            pass

    def _wait_for_seek(self, token):
        """Stay alive after the natural end so a later drag can re-seed."""
        while not self._cancel.is_set():
            if audio.live_seek_pending(token):
                return True
            time.sleep(0.03)
        return False

    def _run(self):
        key, sound = self._key, self._sound
        try:
            blob = self._archive.sequence_blob(sound)
        except Exception as exc:
            self._emit(self.failed, key, str(exc))
            return
        self._args = (blob, sound.start_offset, self._archive.sequence_lookup(sound), self._rate, sound.channel_prio or 64)

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
                            break # abandon
                        audio.live_wait_space(token, 0.05)
                    else:
                        off += w
                time.sleep(PRODUCE_YIELD) # yield the GIL to the audio callback
        except Exception as exc:
            self._emit(self.failed, key, str(exc))

class DseLiveWorker(_ThreadWorker):
    """Seamless DSE sequence: on-demand synthesis into a ring buffer with instant seek."""

    meta = Signal(object, object) # request key, RenderResult (audio=None)
    failed = Signal(object, str) # request key, error message

    def __init__(self, key, archive, entry, parent=None):
        super().__init__(parent)
        self._key = key
        self._archive = archive
        self._entry = entry
        self._rate = DSE_RATE
        self._cancel = threading.Event()
        self._ckpts = _Checkpoints()
        self._args = (entry.events, entry.tpqn, entry.ntrk, entry.preset, entry.bank)

    def cancel(self):
        self._cancel.set()

    def _new_renderer(self):
        return DseLiveRenderer(*self._args, out_rate=self._rate, drain_held=self._entry.kind == 'sfx')

    def _reseed(self, target):
        """Renderer positioned at target content frames."""
        snap = self._ckpts.nearest(target)
        r = DseLiveRenderer.from_snapshot(snap) if snap is not None else self._new_renderer()
        r.fast_forward_to(target)
        return r

    def _render_chunk(self, renderer):
        """Produce one audio block, returns (block, content_start) or (None, None)."""
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
        if content_start is None:
            return None, None
        block = parts[0] if len(parts) == 1 else np.concatenate(parts)
        return block, content_start

    def _racer(self, token, yield_gil=True):
        """State-only run building checkpoints and the exact length."""
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
            if token is not None:
                audio.live_set_total(token, total, exact=True)
            if total == 0 or not r.sounding:
                # nothing audible: report the one-shot status + cause
                res, _audio, _rate, _loop = render_dse_one(self._archive, self._entry, self._rate, keep_audio=False)
                self._emit(self.meta, self._key, res)
                return
            res = RenderResult(
                self._entry.index, self._entry.name, 'ok', DSE_KIND_LABEL[self._entry.kind],
                total / self._rate, None, None, None, rate=self._rate)
            self._emit(self.meta, self._key, res)
        except Exception:
            pass

    def _wait_for_seek(self, token):
        """Stay alive after the natural end so a later drag can re-seed."""
        while not self._cancel.is_set():
            if audio.live_seek_pending(token):
                return True
            time.sleep(0.03)
        return False

    def _run(self):
        key = self._key
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

                block, content_start = self._render_chunk(renderer)
                if block is None:
                    # natural end of the raw render
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
                            break # abandon
                        audio.live_wait_space(token, 0.05)
                    else:
                        off += w
                time.sleep(PRODUCE_YIELD) # yield the GIL to the audio callback
        except Exception as exc:
            self._emit(self.failed, key, str(exc))

class DsePreviewWorker(_ThreadWorker):
    """Full in-memory render of one DSE sequence for preview."""

    done = Signal(object, object)
    failed = Signal(object, str)

    def __init__(self, key, archive, entry, parent=None):
        super().__init__(parent)
        self._key = key
        self._archive = archive
        self._entry = entry
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def _run(self):
        try:
            res, _audio, _rate, _loop = render_dse_one(self._archive, self._entry, DSE_RATE)
        except Exception as exc:
            self._emit(self.failed, self._key, str(exc))
            return
        if self._cancel.is_set():
            return
        self._emit(self.done, self._key, res)

class StrmPreviewWorker(_ThreadWorker):
    """In-memory decode of one SDAT streamed sound for preview."""

    done = Signal(object, object)
    failed = Signal(object, str)

    def __init__(self, key, sdat, stream_id, name, parent=None):
        super().__init__(parent)
        self._key = key
        self._sdat = sdat
        self._stream_id = stream_id
        self._name = name
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def _run(self):
        try:
            res, _audio, _rate, _loop = render_strm_one(self._sdat, self._stream_id, self._name)
        except Exception as exc:
            self._emit(self.failed, self._key, str(exc))
            return
        if self._cancel.is_set():
            return
        self._emit(self.done, self._key, res)

class BatchWorker(_ThreadWorker):
    """Rip a list of tagged jobs to disk with progress and cancellation."""

    batch_progress = Signal(int, int, object) # done, total, RenderResult
    archive_done = Signal(object) # per-job summary dict
    batch_done = Signal(object) # list of summaries
    failed = Signal(str)

    def __init__(self, sdats, jobs, out_root, rate, override_map=None, stream_mode='multiple', parent=None):
        super().__init__(parent)
        self._sdats = sdats
        self._jobs = jobs
        self._out_root = out_root
        self._rate = rate
        self._override = override_map
        self._stream_mode = stream_mode
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

    def _strm_ids(self, sk, sel):
        if sel is not None:
            return sorted(sel)
        return [sid for sid, _n, _v in self._sdat(sk).stream_list]

    def _job_size(self, sk, kind, ident, sel, layers=None):
        if kind == 'seq':
            return len(self._seq_ids(sk, sel))
        if kind == 'strm':
            return len(self._strm_ids(sk, sel))
        if kind == 'carc':
            # files written, so a multi-layer stream counts for what it produces
            sdat = self._sdat(sk)
            members = sdat.folders[ident]
            if sel is not None:
                members = [s for s in members if s.index in sel]
            total = 0
            for s in members:
                if sdat.kind_label(s) != 'strm':
                    total += 1
                    continue
                only = (layers or {}).get(s.index)
                n = len(only) if only else sdat.stream_layers(s)
                if n <= 1:
                    total += 1
                elif self._stream_mode == 'multiple':
                    total += n
                elif self._stream_mode == 'single':
                    total += 1
                else:
                    total += n + 1
            return total
        if sel is not None:
            return len(sel)
        if kind == 'dse':
            return len(self._sdat(sk).folders[ident])
        return len(self._sdat(sk).seqarc(ident).entries)

    def _run(self):
        resume_after = audio.state() == audio.PLAYING
        if resume_after:
            audio.pause()
        try:
            summaries = []
            grand_total = sum(self._job_size(j[0], j[1], j[2], j[3], j[4] if len(j) > 4 else None)
                              for j in self._jobs)
            base = 0
            for sk, kind, ident, sel, layers in self._jobs:
                if self._cancel.is_set():
                    break
                sdat = self._sdat(sk)

                def progress(done, _total, res, _base=base):
                    self._emit(self.batch_progress, _base + done, grand_total, res)

                if kind == 'carc':
                    summary = rip_ctr_folder(
                        sdat,
                        ident,
                        self._out_root,
                        rate=self._rate,
                        only=sel,
                        layers=layers,
                        stream_mode=self._stream_mode,
                        progress=progress,
                        should_cancel=self._cancel.is_set,
                    )
                elif kind == 'dse':
                    summary = rip_dse_folder(
                        sdat,
                        ident,
                        self._out_root,
                        rate=DSE_RATE,
                        only=sel,
                        progress=progress,
                        should_cancel=self._cancel.is_set,
                    )
                elif kind == 'seq':
                    summary = rip_sequences(
                        sdat,
                        self._seq_ids(sk, sel),
                        self._out_root,
                        rate=self._rate,
                        override_map=self._override,
                        progress=progress,
                        should_cancel=self._cancel.is_set,
                    )
                elif kind == 'strm':
                    summary = rip_streams(
                        sdat,
                        self._strm_ids(sk, sel),
                        self._out_root,
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
                base += self._job_size(sk, kind, ident, sel, layers)
            self._emit(self.batch_done, summaries)
        except Exception as exc:
            self._emit(self.failed, str(exc))
        finally:
            if resume_after and audio.state() == audio.PAUSED:
                audio.play()
