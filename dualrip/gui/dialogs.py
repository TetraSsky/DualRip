"""
DualRip dialogs: Settings and the Export confirmation/log window.
"""

import json
import os
from PySide6.QtCore import QSettings
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)
from ..bankmap import parse_bank_map
from .workers import BatchWorker

RATES = ('32728', '44100', '48000') # presets; combo stays editable
RATE_MIN, RATE_MAX = 8000, 192000
EXPORT_DIALOG_SIZE = (680, 460)
LOG_MAX_LINES = 20000 # prevent unbounded log growth on huge batches

def load_settings():
    s = QSettings('DualRip', 'DualRip')
    try:
        rate = int(s.value('rate', 44100))
    except (TypeError, ValueError):
        rate = 44100
    return {
        'out_dir': s.value('out_dir', '') or '',
        'rate': rate,
        'bank_map': s.value('bank_map', '') or '',
    }

def save_settings(values):
    s = QSettings('DualRip', 'DualRip')
    for k, v in values.items():
        s.setValue(k, v)

def load_recent_files():
    """[{'path': str, 'sdats': [int, ...] or None}], most recent first."""
    s = QSettings('DualRip', 'DualRip')
    try:
        entries = json.loads(s.value('recent_files', '[]') or '[]')
        return [e for e in entries if isinstance(e, dict) and e.get('path')]
    except (TypeError, ValueError):
        return []

def save_recent_files(entries):
    s = QSettings('DualRip', 'DualRip')
    s.setValue('recent_files', json.dumps(entries))

class SettingsDialog(QDialog):
    """Output folder, sample rate, bank-map override."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Settings')
        self.setMinimumWidth(480)
        cur = load_settings()

        form = QFormLayout()

        row = QHBoxLayout()
        self.ed_out = QLineEdit(cur['out_dir'])
        self.ed_out.setPlaceholderText('Select Folder...')
        btn = QPushButton('Browse...')
        btn.clicked.connect(self._browse)
        row.addWidget(self.ed_out, 1)
        row.addWidget(btn)
        form.addRow('Default output folder', row)

        self.cb_rate = QComboBox()
        self.cb_rate.setEditable(True)
        self.cb_rate.addItems(RATES)
        self.cb_rate.setCurrentText(str(cur['rate']))
        form.addRow('Sample rate (Hz)', self.cb_rate)

        self.ed_map = QLineEdit(cur['bank_map'])
        self.ed_map.setPlaceholderText('e.g. 4=32+33+43,30=6  (empty = automatic)')
        form.addRow('Bank map override', self.ed_map)

        hint = QLabel('Bank map replaces the automatic resolution of NULL/ dynamic bank slots. Candidates separated by "+" are tried in order; the first bank able to play all the entry\'s instruments wins.')
        hint.setWordWrap(True)
        hint.setStyleSheet('color: gray;')

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(hint)
        lay.addWidget(buttons)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, 'Default output folder', self.ed_out.text())
        if d:
            self.ed_out.setText(d)

    def _accept(self):
        try:
            rate = int(self.cb_rate.currentText())
            if not RATE_MIN <= rate <= RATE_MAX:
                raise ValueError
        except ValueError:
            QMessageBox.warning(self, 'Settings', 'Invalid sample rate.')
            return
        try:
            parse_bank_map(self.ed_map.text().strip())
        except Exception:
            QMessageBox.warning(self, 'Settings', 'Invalid bank map. Expected form: 4=32+33,30=6')
            return
        save_settings(
            {
                'out_dir': self.ed_out.text().strip(),
                'rate': rate,
                'bank_map': self.ed_map.text().strip(),
            }
        )
        self.accept()

class ExportDialog(QDialog):
    """
    Confirmation + live log window for a batch export.

    sdats: OrderedDict[sdat_key, (label, SdatFile)]
    jobs: [(sdat_key, kind, ident, sel)] where kind is 'arc' (ident=arc_id) or 'seq' (ident=None); sel is a set of indices/ids or None for all.
    """

    def __init__(self, sdats, jobs, rate, override_map, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Export')
        self.resize(*EXPORT_DIALOG_SIZE)
        self._sdats = sdats
        self._jobs = jobs
        self._rate = rate
        self._override = override_map
        self._worker = None

        settings = load_settings()

        lay = QVBoxLayout(self)

        total = sum(self._job_size(sk, kind, ident, sel) for sk, kind, ident, sel in jobs)
        self.lbl_summary = QLabel(f'{total} items from {len(jobs)} group(s), {rate} Hz')
        lay.addWidget(self.lbl_summary)

        row = QHBoxLayout()
        self.ed_out = QLineEdit(settings['out_dir'])
        self.ed_out.setPlaceholderText('Select Folder...')
        btn_browse = QPushButton('Browse...')
        btn_browse.clicked.connect(self._browse)
        row.addWidget(QLabel('Output'))
        row.addWidget(self.ed_out, 1)
        row.addWidget(btn_browse)
        lay.addLayout(row)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(LOG_MAX_LINES)
        self.log.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        lay.addWidget(self.log, 1)
        self._plan()

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        lay.addWidget(self.progress)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.btn_export = QPushButton('Export')
        self.btn_export.clicked.connect(self._start)
        self.btn_cancel = QPushButton('Cancel')
        self.btn_cancel.clicked.connect(self._cancel_or_close)
        btns.addWidget(self.btn_export)
        btns.addWidget(self.btn_cancel)
        lay.addLayout(btns)

    def _sdat(self, sk):
        """Return the SdatFile for sdat_key sk."""
        return self._sdats[sk][1]

    def _all_seq_ids(self, sk):
        sdat = self._sdat(sk)
        return [sid for sid, _n, _b in sdat.sequence_list]

    def _job_size(self, sk, kind, ident, sel):
        sdat = self._sdat(sk)
        if kind == 'seq':
            return len(sel) if sel is not None else len(self._all_seq_ids(sk))
        if sel is not None:
            return len(sel)
        return len(sdat.seqarc(ident).entries)

    def _plan(self):
        self.log.appendPlainText('Export :')
        for sk, kind, ident, sel in self._jobs:
            sdat = self._sdat(sk)
            sdat_label = self._sdats[sk][0]
            if kind == 'seq':
                seq_names = {sid: name for sid, name, _b in sdat.sequence_list}
                ids = sorted(sel) if sel is not None else self._all_seq_ids(sk)
                if sel is None:
                    self.log.appendPlainText(f'[{sdat_label}] SSEQ (music) - all {len(ids)} sequences')
                else:
                    self.log.appendPlainText(f'[{sdat_label}] SSEQ (music) - {len(ids)} sequences:')
                    for sid in ids:
                        self.log.appendPlainText(f'[{sid:3d}] {seq_names.get(sid, sid)}')
            else:
                seqarc = sdat.seqarc(ident)
                if sel is None:
                    self.log.appendPlainText(f'[{sdat_label}] {ident:03d} {seqarc.name} - all {len(seqarc.entries)} entries')
                else:
                    self.log.appendPlainText(f'[{sdat_label}] {ident:03d} {seqarc.name} - {len(sel)} entries:')
                    for idx in sorted(sel):
                        self.log.appendPlainText(f'[{idx:3d}] {seqarc.entries[idx].name}')
        self.log.appendPlainText('')

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, 'Output folder', self.ed_out.text())
        if d:
            self.ed_out.setText(d)

    def _start(self):
        out = self.ed_out.text().strip()
        if not out:
            QMessageBox.warning(self, 'Export', 'Choose an output folder.')
            return
        os.makedirs(out, exist_ok=True)
        self.btn_export.setEnabled(False)
        self.ed_out.setEnabled(False)
        self.progress.setVisible(True)
        self.log.appendPlainText(f'Exporting to {out}')
        self._worker = BatchWorker(self._sdats, self._jobs, out, self._rate, self._override)
        self._worker.batch_progress.connect(self._on_progress)
        self._worker.archive_done.connect(self._on_archive_done)
        self._worker.batch_done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_progress(self, done, total, res):
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        if res.status in ('ok', 'loop'):
            extra = f'{res.duration:7.2f}s'
            if res.loop_start is not None:
                extra += f' loop {res.loop_start:.3f}-{res.loop_end:.3f}s'
            line = f'[{res.index:3d}] {res.name:44s} {res.status:5s} {extra}'
        elif res.status == 'error':
            line = f'[{res.index:3d}] {res.name:44s} ERROR {res.error}'
        else:
            line = f'[{res.index:3d}] {res.name:44s} {res.status}'
        self.log.appendPlainText(line)

    def _on_archive_done(self, summary):
        if summary.get('note'):
            self.log.appendPlainText(f"note: {summary['note']}")
        self.log.appendPlainText(
            f"== {summary['arc_name']}: "
            f"{summary['ok'] + summary['loop']} WAV written "
            f"({summary['loop']} looping), {summary['empty']} empty"
            + (f", {summary['error']} errors" if summary['error'] else '')
            + (' - CANCELLED' if summary['cancelled'] else '')
        )
        self.log.appendPlainText('')

    def _finish_worker(self):
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.wait()

    def _on_done(self, _summaries):
        self._finish_worker()
        self.log.appendPlainText('Done.')
        self.progress.setValue(self.progress.maximum())
        self.btn_cancel.setText('Close')

    def _on_failed(self, msg):
        self._finish_worker()
        self.log.appendPlainText(f'FAILED: {msg}')
        self.btn_cancel.setText('Close')

    def _cancel_or_close(self):
        if self._worker is not None:
            self._worker.cancel()
            self.log.appendPlainText('Cancelling...')
        else:
            self.reject()

    def closeEvent(self, event):
        if self._worker is not None:
            self._worker.cancel()
        event.accept()
