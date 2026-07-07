"""
Background workers: rendering happens in plain Python threads, results
come back to the GUI thread through queued signal deliveries.

Each worker is a QObject created in the GUI thread that owns a daemon
thread. Connections to these signals MUST be bound methods of a QObject
living in the GUI thread (never lambdas or free functions): Qt then
delivers them as queued events in the GUI thread. A lambda connection can
run in the worker thread and touch widgets from there, which corrupts Qt's
heap and crashes later.

Owners must keep a Python reference to the worker until its terminal
signal (done/failed or batch_done/failed) has been handled, then call
wait() before dropping the reference so the QObject is destroyed from the
GUI thread, never from the worker thread.
"""

import threading
from PySide6.QtCore import QObject, Signal
from ..export import render_one, rip_archive, rip_sequences

class _ThreadWorker(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def wait(self):
        """Join the worker thread. Called from the terminal-signal handler,
        where the thread is already past its last emit, so this returns
        almost immediately."""
        if self._thread.is_alive():
            self._thread.join()

    def _emit(self, sig, *args):
        try:
            sig.emit(*args)
        except RuntimeError:
            pass  # C++ side already destroyed during application shutdown

    def _run(self):
        raise NotImplementedError

class RenderWorker(_ThreadWorker):
    """Render a single entry in memory (preview / details)."""

    done = Signal(object, object)  # request key, RenderResult
    failed = Signal(object, str)  # request key, error message

    def __init__(self, key, sdat, seqarc, entry, rate, resolver, parent=None):
        super().__init__(parent)
        self._key = key
        self._args = (sdat, seqarc, entry, rate, resolver)

    def _run(self):
        try:
            res = render_one(*self._args)
        except Exception as exc:
            self._emit(self.failed, self._key, str(exc))
            return
        self._emit(self.done, self._key, res)

class BatchWorker(_ThreadWorker):
    """Rip a list of tagged jobs to disk, with per-entry progress, per-job
    summaries and cancellation.

    Each job is (kind, ident, sel):
      ('arc', arc_id, only_set_or_None)  -> one SSAR archive
      ('seq', None, seq_set_or_None)     -> standalone SSEQ music (None = all)
    """

    batch_progress = Signal(int, int, object)  # done, total, RenderResult
    archive_done = Signal(object)  # per-job summary dict
    batch_done = Signal(object)  # list of summaries
    failed = Signal(str)

    def __init__(self, sdat, jobs, out_root, rate, override_map=None, parent=None):
        super().__init__(parent)
        self._sdat = sdat
        self._jobs = jobs
        self._out_root = out_root
        self._rate = rate
        self._override = override_map
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def _seq_ids(self, sel):
        if sel is not None:
            return sorted(sel)
        return [sid for sid, _n, _b in self._sdat.sequence_list]

    def _job_size(self, kind, ident, sel):
        if kind == 'seq':
            return len(self._seq_ids(sel))
        if sel is not None:
            return len(sel)
        return len(self._sdat.seqarc(ident).entries)

    def _run(self):
        try:
            summaries = []
            grand_total = sum(self._job_size(*j) for j in self._jobs)
            base = 0
            for kind, ident, sel in self._jobs:
                if self._cancel.is_set():
                    break

                def progress(done, _total, res, _base=base):
                    self._emit(self.batch_progress, _base + done, grand_total, res)

                if kind == 'seq':
                    summary = rip_sequences(
                        self._sdat,
                        self._seq_ids(sel),
                        self._out_root,
                        rate=self._rate,
                        override_map=self._override,
                        progress=progress,
                        should_cancel=self._cancel.is_set,
                    )
                else:
                    summary = rip_archive(
                        self._sdat,
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
                base += self._job_size(kind, ident, sel)
            self._emit(self.batch_done, summaries)
        except Exception as exc:
            self._emit(self.failed, str(exc))
