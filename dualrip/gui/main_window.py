"""
DualRip main window: SDAT explorer, entry details, audio preview, export.
"""

import os
from collections import OrderedDict
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from .. import __version__
from ..bankmap import BankResolver, parse_bank_map
from ..formats.sdat import SdatFile
from . import audio
from .dialogs import ExportDialog, SettingsDialog, load_settings
from .player import PlayerBar
from .workers import RenderWorker

CACHE_SIZE = 48
# Music renders are large (a 4-minute song is ~40 MB at 44.1 kHz stereo), so
# the preview cache is also bounded by total audio bytes, not just count.
CACHE_MAX_BYTES = 256 * 1024 * 1024

# --- layout constants ---
FORM_HSPACING = 32   # horizontal gap between label and value in the details panel
SPLITTER_LEFT = 580   # initial left-panel width
SPLITTER_RIGHT = 440  # initial right-panel width
TREE_COL_NAME = 330   # "Name" column width in the tree
TREE_COL_ID = 46      # "ID" column width in the tree

# ROLE_KIND: 'cat' | 'seqcat' | 'arc' | 'entry' | 'seq' | 'bank' | 'war'
# 'seqcat' is the Sequences category node; selecting it exports all music.
ROLE_KIND = Qt.UserRole
ROLE_ARC = Qt.UserRole + 1
ROLE_INDEX = Qt.UserRole + 2

class MainWindow(QMainWindow):
    def __init__(self, icon_path=None):
        super().__init__()
        self.setWindowTitle('DualRip')
        if icon_path and os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        self.resize(1060, 680)

        self.sdat = None
        self.sdat_path = None
        self.settings = load_settings()
        self._resolvers = {}
        self._cache = OrderedDict()  # (arc_id, index, rate) -> RenderResult
        self._preview_worker = None  # in-flight RenderWorker (one at a time)
        self._preview_pending = None  # (seqarc, entry) requested meanwhile
        self._generation = 0  # bumped per SDAT so stale renders miss

        self._build_menu()
        self._build_ui()
        self._set_loaded(False)

    def _build_menu(self):
        m_file = self.menuBar().addMenu('&File')
        act_open = QAction('&Open SDAT...', self)
        act_open.setShortcut(QKeySequence.Open)
        act_open.triggered.connect(self.open_sdat)
        m_file.addAction(act_open)
        m_file.addSeparator()
        self.act_export_sel = QAction('Export &selection...', self)
        self.act_export_sel.setShortcut('Ctrl+E')
        self.act_export_sel.triggered.connect(self.export_selection)
        m_file.addAction(self.act_export_sel)
        self.act_export_all = QAction('Export &all archives...', self)
        self.act_export_all.triggered.connect(self.export_all)
        m_file.addAction(self.act_export_all)
        m_file.addSeparator()
        act_settings = QAction('Se&ttings...', self)
        act_settings.triggered.connect(self.show_settings)
        m_file.addAction(act_settings)
        m_file.addSeparator()
        act_quit = QAction('&Quit', self)
        act_quit.setShortcut(QKeySequence.Quit)
        act_quit.triggered.connect(self.close)
        m_file.addAction(act_quit)

        m_help = self.menuBar().addMenu('&Help')
        act_about = QAction('&About DualRip', self)
        act_about.triggered.connect(self.show_about)
        m_help.addAction(act_about)

    def _build_ui(self):
        splitter = QSplitter(self)

        # left: filter + tree
        left = QWidget(splitter)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(6, 6, 3, 6)
        self.filter_edit = QLineEdit(left)
        self.filter_edit.setPlaceholderText('Filter...')
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)
        lv.addWidget(self.filter_edit)
        self.tree = QTreeWidget(left)
        self.tree.setHeaderLabels(['Name', 'ID', 'Info'])
        self.tree.setColumnWidth(0, TREE_COL_NAME)
        self.tree.setColumnWidth(1, TREE_COL_ID)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.currentItemChanged.connect(self._selection_changed)
        self.tree.itemActivated.connect(lambda *_: self.play_selected())
        lv.addWidget(self.tree)

        # right: details + actions
        right = QWidget(splitter)
        rv = QVBoxLayout(right)
        rv.setContentsMargins(3, 6, 6, 6)

        form_frame = QFrame(right)
        form_frame.setFrameShape(QFrame.StyledPanel)
        form = QFormLayout(form_frame)
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        form.setHorizontalSpacing(FORM_HSPACING)
        self.lbl_name = QLabel('-')
        self.lbl_name.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_kind = QLabel('-')
        self.lbl_kind.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_where = QLabel('-')
        self.lbl_where.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_bank = QLabel('-')
        self.lbl_bank.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_bank.setWordWrap(True)
        self.lbl_volume = QLabel('-')
        self.lbl_volume.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_duration = QLabel('-')
        self.lbl_duration.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_loop = QLabel('-')
        self.lbl_loop.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_status = QLabel('-')
        self.lbl_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow('Name', self.lbl_name)
        form.addRow('Type', self.lbl_kind)
        form.addRow('Location', self.lbl_where)
        form.addRow('Bank', self.lbl_bank)
        form.addRow('Entry volume', self.lbl_volume)
        form.addRow('Duration', self.lbl_duration)
        form.addRow('Loop', self.lbl_loop)
        form.addRow('Status', self.lbl_status)
        rv.addWidget(form_frame)

        self.player = PlayerBar(right)
        self.player.play_clicked.connect(self._on_play_clicked)
        self.player.setVisible(False)
        rv.addWidget(self.player)
        rv.addStretch(1)

        note = QLabel(
            'Raw export: one loop iteration, native silences, full '
            'releases.\nLoop points go to manifest.csv and into a '
            'smpl chunk in the WAV.\nCtrl/Shift-click to select several '
            'sound effects, archives or music sequences.'
        )
        note.setWordWrap(True)
        note.setStyleSheet('color: gray;')
        rv.addWidget(note)

        export_row = QHBoxLayout()
        self.btn_export_sel = QPushButton('Export selection...')
        self.btn_export_sel.clicked.connect(self.export_selection)
        self.btn_export_all = QPushButton('Export all...')
        self.btn_export_all.clicked.connect(self.export_all)
        export_row.addStretch(1)
        export_row.addWidget(self.btn_export_sel)
        export_row.addWidget(self.btn_export_all)
        rv.addLayout(export_row)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setSizes([SPLITTER_LEFT, SPLITTER_RIGHT])
        self.setCentralWidget(splitter)

    def _set_loaded(self, loaded):
        for w in (
            self.act_export_sel,
            self.act_export_all,
            self.player,
            self.btn_export_sel,
            self.btn_export_all,
            self.filter_edit,
        ):
            w.setEnabled(loaded)

    def open_sdat(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Open SDAT', '', 'SDAT files (*.sdat);;All files (*)'
        )
        if not path:
            return
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            self.sdat = SdatFile(path)
            self.sdat_path = path
            self._generation += 1
            self._preview_pending = None
            self._resolvers.clear()
            self._cache.clear()
            self.player.clear()
            self._fill_tree()
        except Exception as exc:
            QMessageBox.critical(self, 'DualRip', f'Cannot open SDAT:\n{exc}')
            return
        finally:
            QApplication.restoreOverrideCursor()
        self._set_loaded(True)
        n = sum(c for _i, _n, c in self.sdat.seqarc_list)
        nseq = len(self.sdat.sequence_list)
        self.statusBar().showMessage(
            f'{os.path.basename(path)} - '
            f'{len(self.sdat.seqarc_list)} sequence archives, {n} entries, '
            f'{nseq} music sequences.'
        )
        self.setWindowTitle(f'DualRip - {os.path.basename(path)}')

    def _fill_tree(self):
        self.tree.clear()

        cat_arcs = QTreeWidgetItem(['Sequence Archives (SSAR)', '', ''])
        cat_arcs.setData(0, ROLE_KIND, 'cat')
        for arc_id, name, _count in self.sdat.seqarc_list:
            seqarc = self.sdat.seqarc(arc_id)
            top = QTreeWidgetItem([name, str(arc_id), ''])
            top.setData(0, ROLE_KIND, 'arc')
            top.setData(0, ROLE_ARC, arc_id)
            playable = 0
            for e in seqarc.entries:
                if e.offset is None:
                    continue
                bank = str(e.bank_id)
                if self.sdat.bank_is_null(e.bank_id):
                    bank += ' (auto)'
                it = QTreeWidgetItem([e.name, str(e.index), f'bank {bank}'])
                it.setData(0, ROLE_KIND, 'entry')
                it.setData(0, ROLE_ARC, arc_id)
                it.setData(0, ROLE_INDEX, e.index)
                top.addChild(it)
                playable += 1
            top.setText(2, f'{playable} entries')
            cat_arcs.addChild(top)
        self.tree.addTopLevelItem(cat_arcs)

        seqs = self.sdat.sequence_list
        cat_seq = QTreeWidgetItem(['Sequences (SSEQ)', '', f'{len(seqs)}'])
        cat_seq.setData(0, ROLE_KIND, 'seqcat')
        for sid, name, bank_id in seqs:
            bank = ''
            if bank_id is not None:
                bank = f'bank {bank_id}'
                if self.sdat.bank_is_null(bank_id):
                    bank += ' (auto)'
            it = QTreeWidgetItem([name, str(sid), bank])
            it.setData(0, ROLE_KIND, 'seq')
            it.setData(0, ROLE_INDEX, sid)
            cat_seq.addChild(it)
        self.tree.addTopLevelItem(cat_seq)

        banks = self.sdat.bank_list
        cat_bank = QTreeWidgetItem(['Banks (SBNK)', '', f'{len(banks)}'])
        cat_bank.setData(0, ROLE_KIND, 'cat')
        for bid, name, wids in banks:
            if wids is None:
                it = QTreeWidgetItem(
                    [name or '(null / dynamic slot)', str(bid), 'filled at runtime']
                )
            else:
                shown = ', '.join(str(w) for w in wids if w is not None)
                it = QTreeWidgetItem([name, str(bid), f'wave archives {shown}'])
            it.setData(0, ROLE_KIND, 'bank')
            it.setData(0, ROLE_INDEX, bid)
            cat_bank.addChild(it)
        self.tree.addTopLevelItem(cat_bank)

        wars = self.sdat.wave_archive_list
        cat_war = QTreeWidgetItem(['Wave Archives (SWAR)', '', f'{len(wars)}'])
        cat_war.setData(0, ROLE_KIND, 'cat')
        for wid, name, cnt in wars:
            it = QTreeWidgetItem([name, str(wid), f'{cnt} waves'])
            it.setData(0, ROLE_KIND, 'war')
            it.setData(0, ROLE_INDEX, wid)
            cat_war.addChild(it)
        self.tree.addTopLevelItem(cat_war)

    def _apply_filter(self, text):
        text = text.strip().lower()

        def filter_children(item):
            visible = 0
            for j in range(item.childCount()):
                child = item.child(j)
                if child.childCount():
                    sub = filter_children(child)
                    child.setHidden(bool(text) and sub == 0)
                    visible += sub
                else:
                    match = not text or text in child.text(0).lower()
                    child.setHidden(not match)
                    visible += match
            return visible

        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            vis = filter_children(top)
            top.setHidden(bool(text) and vis == 0)
            if text and vis:
                top.setExpanded(True)

    def _current_playable(self):
        """(seqarc, entry) for the current tree item if it is renderable (an
        SSAR entry or a standalone SSEQ), else (None, None). A sequence is a
        one-entry synthetic SeqArc, so both share every downstream path."""
        it = self.tree.currentItem()
        if it is None or self.sdat is None:
            return None, None
        kind = it.data(0, ROLE_KIND)
        if kind == 'entry':
            seqarc = self.sdat.seqarc(it.data(0, ROLE_ARC))
            return seqarc, seqarc.entries[it.data(0, ROLE_INDEX)]
        if kind == 'seq':
            seqarc = self.sdat.sequence(it.data(0, ROLE_INDEX))
            return seqarc, seqarc.entries[0]
        return None, None

    def _selection_changed(self, *_):
        it = self.tree.currentItem()
        if it is None or self.sdat is None:
            return
        kind = it.data(0, ROLE_KIND)
        self.player.setVisible(kind in ('entry', 'seq'))
        for lbl in (
            self.lbl_bank,
            self.lbl_volume,
            self.lbl_duration,
            self.lbl_loop,
            self.lbl_status,
        ):
            lbl.setText('-')
        if kind in ('entry', 'seq'):
            seqarc, entry = self._current_playable()
            self.lbl_name.setText(entry.name)
            if kind == 'entry':
                self.lbl_kind.setText('Sound effect (SSAR entry)')
                self.lbl_where.setText(
                    f'{seqarc.arc_id:03d}  {seqarc.name}  [{entry.index}]'
                )
            else:
                self.lbl_kind.setText('Music sequence (SSEQ)')
                self.lbl_where.setText(f'sequence {entry.index}')
            bank = str(entry.bank_id)
            bname = self.sdat.bank_name(entry.bank_id)
            if self.sdat.bank_is_null(entry.bank_id):
                bank += '  (dynamic slot, resolved automatically)'
            elif bname:
                bank += f'  {bname}'
            self.lbl_bank.setText(bank)
            self.lbl_volume.setText(str(entry.volume))
            self._show_render(self._cache.get(self._cache_key(seqarc, entry)))
        elif kind == 'arc':
            arc_id = it.data(0, ROLE_ARC)
            seqarc = self.sdat.seqarc(arc_id)
            self.lbl_name.setText(seqarc.name)
            self.lbl_kind.setText('Sequence archive (SSAR)')
            self.lbl_where.setText(f'archive {arc_id:03d}')
            self.lbl_status.setText(it.text(2))
        elif kind == 'seqcat':
            self.lbl_name.setText('Sequences (SSEQ)')
            self.lbl_kind.setText('Music sequence collection')
            self.lbl_where.setText(f'{it.text(2)} sequences')
            self.lbl_status.setText('select to export all music')
        elif kind == 'bank':
            bid = it.data(0, ROLE_INDEX)
            self.lbl_name.setText(it.text(0))
            self.lbl_kind.setText('Instrument bank (SBNK)')
            self.lbl_where.setText(f'bank {bid}')
            if self.sdat.bank_is_null(bid):
                self.lbl_bank.setText(
                    'NULL slot - filled at runtime by the '
                    'game; use Settings > Bank map to pin '
                    'a substitute, or let auto-resolution '
                    'pick one per entry'
                )
            else:
                meta = self.sdat.bank_meta(bid)
                if meta:
                    _e, _c, wids = meta
                    names = []
                    for w in wids:
                        if w is not None:
                            wn = next(
                                (n for i, n, _c2 in self.sdat.wave_archive_list if i == w), str(w)
                            )
                            names.append(f'{w} ({wn})')
                    self.lbl_bank.setText('wave archives: ' + (', '.join(names) or 'none'))
        elif kind == 'war':
            self.lbl_name.setText(it.text(0))
            self.lbl_kind.setText('Wave archive (SWAR)')
            self.lbl_where.setText(f'wave archive {it.text(1)}')
            self.lbl_status.setText(it.text(2))
        else:
            self.lbl_name.setText(it.text(0))
            self.lbl_kind.setText('-')
            self.lbl_where.setText('-')

    def _show_render(self, res):
        if res is None:
            self.lbl_duration.setText('-')
            self.lbl_loop.setText('-')
            self.lbl_status.setText('-')
            return
        self.lbl_duration.setText(f'{res.duration:.3f}s' if res.duration else '-')
        if res.loop_start is not None:
            self.lbl_loop.setText(f'{res.loop_start:.3f}s - {res.loop_end:.3f}s')
        else:
            self.lbl_loop.setText('none')
        self.lbl_status.setText(res.status + (f' ({res.error})' if res.error else ''))
        if res.bank_label and '->' in res.bank_label:
            self.lbl_bank.setText(
                self.lbl_bank.text().split('  (')[0] + f'  (auto: {res.bank_label})'
            )

    def _override_map(self):
        try:
            return parse_bank_map(self.settings['bank_map'])
        except Exception:
            return {}

    def _resolver(self, seqarc):
        if seqarc.arc_id not in self._resolvers:
            self._resolvers[seqarc.arc_id] = BankResolver(self.sdat, seqarc, self._override_map())
        return self._resolvers[seqarc.arc_id]

    def _cache_key(self, seqarc, entry):
        return (self._generation, seqarc.arc_id, entry.index, self.settings['rate'])

    def _on_play_clicked(self):
        seqarc, entry = self._current_playable()
        if (
            entry is not None
            and self.player.loaded_key == self._cache_key(seqarc, entry)
            and (audio.state() == audio.PAUSED or audio.position() > 0)
        ):
            self.player.resume()
            return
        self.play_selected()

    def play_selected(self):
        seqarc, entry = self._current_playable()
        if entry is None:
            return
        key = self._cache_key(seqarc, entry)
        cached = self._cache.get(key)
        if cached is not None:
            self._show_render(cached)
            if cached.audio is not None:
                self._play(key, cached)
            else:
                self.statusBar().showMessage(
                    f'{cached.name}: {cached.status}'
                    + (f' ({cached.error})' if cached.error else '')
                )
            return
        audio.stop()

        self._preview_pending = (seqarc, entry)
        self.statusBar().showMessage(f'Rendering {entry.name}...')
        self._start_pending_preview()

    def _start_pending_preview(self):
        if self._preview_worker is not None or self._preview_pending is None:
            return
        seqarc, entry = self._preview_pending
        self._preview_pending = None
        key = self._cache_key(seqarc, entry)
        worker = RenderWorker(
            key, self.sdat, seqarc, entry, self.settings['rate'], self._resolver(seqarc)
        )
        worker.done.connect(self._preview_done)
        worker.failed.connect(self._preview_failed)
        self._preview_worker = worker
        worker.start()

    def _finish_preview_worker(self):
        worker, self._preview_worker = self._preview_worker, None
        if worker is not None:
            worker.wait()

    def _play(self, key, res):
        if self.player.load_result(key, res, self.settings['rate']):
            self.statusBar().showMessage(f'{res.name}: {res.duration:.2f}s')
        else:
            self.statusBar().showMessage(
                'Playback unavailable (sounddevice missing or no audio output device).'
            )

    def _evict_cache(self):
        def total_bytes():
            return sum(r.audio.nbytes for r in self._cache.values()
                       if r.audio is not None)

        while len(self._cache) > CACHE_SIZE or (
            len(self._cache) > 1 and total_bytes() > CACHE_MAX_BYTES
        ):
            self._cache.popitem(last=False)

    def _preview_done(self, key, res):
        self._finish_preview_worker()
        self._cache[key] = res
        self._cache.move_to_end(key)
        self._evict_cache()
        cur_arc, cur_entry = self._current_playable()
        if cur_entry is not None and self._cache_key(cur_arc, cur_entry) == key:
            self._show_render(res)
        if self._preview_pending is not None:
            self._start_pending_preview()
        elif res.audio is not None:
            self._play(key, res)
        else:
            self.statusBar().showMessage(
                f'{res.name}: {res.status}' + (f' ({res.error})' if res.error else '')
            )

    def _preview_failed(self, _key, msg):
        self._finish_preview_worker()
        self.statusBar().showMessage(f'Render failed: {msg}')
        self._start_pending_preview()

    def _selected_jobs(self):
        """Build tagged jobs [(kind, ident, sel)] from the tree selection.
        'arc'/'entry' -> SSAR jobs; 'seq'/'seqcat' -> one SSEQ music job."""
        whole = set()
        partial = {}
        seq_ids = set()
        all_seqs = False
        for it in self.tree.selectedItems():
            kind = it.data(0, ROLE_KIND)
            if kind == 'arc':
                whole.add(it.data(0, ROLE_ARC))
            elif kind == 'entry':
                arc = it.data(0, ROLE_ARC)
                partial.setdefault(arc, set()).add(it.data(0, ROLE_INDEX))
            elif kind == 'seq':
                seq_ids.add(it.data(0, ROLE_INDEX))
            elif kind == 'seqcat':
                all_seqs = True
        jobs = [('arc', a, None) for a in sorted(whole)]
        jobs += [('arc', a, idxs) for a, idxs in sorted(partial.items())
                 if a not in whole]
        if all_seqs:
            jobs.append(('seq', None, None))
        elif seq_ids:
            jobs.append(('seq', None, seq_ids))
        return jobs

    def export_selection(self):
        jobs = self._selected_jobs()
        if not jobs:
            QMessageBox.information(
                self,
                'DualRip',
                'Select entries, archives or sequences first '
                '(Ctrl/Shift-click for multiple).',
            )
            return
        self._open_export(jobs)

    def export_all(self):
        jobs = [('arc', i, None) for i, _n, _c in self.sdat.seqarc_list]
        if self.sdat.sequence_list:
            jobs.append(('seq', None, None))
        self._open_export(jobs)

    def _open_export(self, jobs):
        self.settings = load_settings()
        dlg = ExportDialog(self.sdat, jobs, self.settings['rate'], self._override_map(), self)
        dlg.exec()

    def show_settings(self):
        dlg = SettingsDialog(self)
        if dlg.exec():
            old = self.settings
            self.settings = load_settings()
            if (
                old['rate'] != self.settings['rate']
                or old['bank_map'] != self.settings['bank_map']
            ):
                self._cache.clear()
                self._resolvers.clear()
                self.player.clear()
            self.statusBar().showMessage('Settings saved.')

    def show_about(self):
        QMessageBox.about(
            self,
            'About DualRip',
            f'<b>DualRip {__version__}</b><br>'
            'Nintendo DS SDAT sound-effect (SSAR) and music (SSEQ) ripper.<br><br>'
            'Playback core is a Python port of the FeOS Sound System '
            '(fincs), as adapted by Naram Qashat (CyberBotX) for the NCSF '
            'player (in_xsf). Driver tables originate from disassembly of '
            "Nintendo's NNS sound driver by those authors.",
        )

    def closeEvent(self, event):
        audio.shutdown()
        event.accept()
