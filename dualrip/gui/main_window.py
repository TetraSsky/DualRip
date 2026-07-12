"""
DualRip main window: SDAT explorer, entry details, audio preview, export.

Supports opening multiple SDATs simultaneously (e.g. from a .nds ROM that
contains several sound archives).  The tree groups entries by SDAT so the user
can browse and export across all of them without re-opening the file.
"""

import os
from collections import OrderedDict
from PySide6.QtCore import QEvent, QPoint, Qt, QTimer
from PySide6.QtGui import QAction, QIcon, QKeySequence, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
try:
    from PySide6.QtMultimedia import QMediaDevices
except ImportError:
    QMediaDevices = None
try:
    from PySide6.QtMultimedia import QMediaDevices
except ImportError:
    QMediaDevices = None
from .. import __version__
from ..bankmap import BankResolver, parse_bank_map
from ..formats.rom import find_sdats_in_rom
from ..formats.sdat import SdatFile
from . import audio
from .dialogs import (
    ExportDialog,
    SettingsDialog,
    load_recent_files,
    load_settings,
    save_recent_files,
)
from .player import PlayerBar
from .workers import LiveWorker, StreamWorker

CACHE_SIZE = 48
CACHE_MAX_BYTES = 256 * 1024 * 1024

# --- layout constants ---
FORM_HSPACING = 32
SPLITTER_LEFT = 580
SPLITTER_RIGHT = 440
TREE_COL_NAME = 330
TREE_COL_ID = 46
STATUSBAR_MSG_LEFT = 6
RECENT_MAX = 8

# ROLE_KIND: 'sdat' | 'cat' | 'seqcat' | 'arc' | 'entry' | 'seq' | 'bank' | 'war'
ROLE_KIND = Qt.UserRole
ROLE_ARC = Qt.UserRole + 1
ROLE_INDEX = Qt.UserRole + 2
ROLE_SDAT = Qt.UserRole + 3 # sdat_key on every item, identifies owning SdatFile


class MainWindow(QMainWindow):
    def __init__(self, icon_path=None):
        super().__init__()
        self.setWindowTitle('DualRip')
        if icon_path and os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        self.resize(1060, 680)
        self.setAcceptDrops(True)

        # _sdats: OrderedDict[sdat_key, (label, SdatFile)]
        self._sdats = OrderedDict()
        self._sdat_path = None # display label for title bar / status
        self.settings = load_settings()
        self._resolvers = {} # (sdat_key, arc_id) -> BankResolver
        self._cache = OrderedDict() # (gen, sdat_key, arc_id, index, rate) -> RenderResult
        self._preview_worker = None
        self._preview_key = None
        self._playing_item = None
        self._item_index = {}
        self._generation = 0

        self._build_menu()
        self._build_ui()
        self._set_loaded(False)

        self._media_devices = None
        if QMediaDevices is not None:
            self._media_devices = QMediaDevices(self)
            self._media_devices.audioOutputsChanged.connect(self._on_audio_outputs_changed)

    def _on_audio_outputs_changed(self):
        dev = self._media_devices.defaultAudioOutput()
        audio.recheck_device(None if dev.isNull() else bytes(dev.id()))

    # -- backward-compat accessors for single-SDAT code paths --
    @property
    def sdat(self):
        """First/only SdatFile (None when nothing is loaded)."""
        if self._sdats:
            return next(iter(self._sdats.values()))[1]
        return None

    @property
    def sdat_path(self):
        return self._sdat_path

    def _build_menu(self):
        m_file = self.menuBar().addMenu('&File')
        act_open = QAction('&Open SDAT/NDS...', self)
        act_open.setShortcut(QKeySequence.Open)
        act_open.triggered.connect(self.open_sdat)
        m_file.addAction(act_open)
        self.menu_file = m_file
        self.menu_recent = m_file.addMenu('Open &Recent')
        self.menu_recent.aboutToShow.connect(self._populate_recent_menu)
        # grey the submenu out when the list is empty, decided as File opens
        m_file.aboutToShow.connect(self._update_recent_enabled)
        self.act_close_sdat = QAction('&Close file', self)
        self.act_close_sdat.setShortcut('Ctrl+W')
        self.act_close_sdat.triggered.connect(self._close_sdat)
        m_file.addAction(self.act_close_sdat)
        m_file.addSeparator()
        self.act_export_sel = QAction('Export &selection...', self)
        self.act_export_sel.setShortcut('Ctrl+E')
        self.act_export_sel.triggered.connect(self.export_selection)
        m_file.addAction(self.act_export_sel)
        self.act_export_all = QAction('Export &all...', self)
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

        m_view = self.menuBar().addMenu('&View')
        self.act_toggle_panel = QAction('&Toggle Panel', self)
        self.act_toggle_panel.setShortcut('Ctrl+D')
        self.act_toggle_panel.triggered.connect(self._toggle_right_panel)
        m_view.addAction(self.act_toggle_panel)

        m_help = self.menuBar().addMenu('&Help')
        act_about = QAction('&About DualRip', self)
        act_about.triggered.connect(self.show_about)
        m_help.addAction(act_about)

    def _build_ui(self):
        splitter = QSplitter(self)
        self.splitter = splitter
        self._last_right_size = SPLITTER_RIGHT

        # left: filter + tree
        left = QWidget(splitter)
        self._left_panel = left
        left.installEventFilter(self)
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
        self.tree.setAnimated(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.currentItemChanged.connect(self._selection_changed)
        self.tree.itemActivated.connect(lambda *_: self.play_selected())
        self.tree.installEventFilter(self)

        self.empty_placeholder = QLabel(
            'Open a sound_data.sdat or .nds ROM to get started\n'
            '(File > Open SDAT/NDS..., Ctrl+O or drag & drop a file)'
        )
        self.empty_placeholder.setAlignment(Qt.AlignCenter)
        self.empty_placeholder.setWordWrap(True)
        self.empty_placeholder.setStyleSheet('color: gray;')

        self.tree_stack = QStackedWidget(left)
        self.tree_stack.addWidget(self.tree)
        self.tree_stack.addWidget(self.empty_placeholder)
        lv.addWidget(self.tree_stack)

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
        self.player.loaded_changed.connect(self._on_loaded_changed)
        self.player.setVisible(False)
        rv.addWidget(self.player)
        rv.addStretch(1)

        note = QLabel(
            'Raw export: steady-state loop (2 passes), native silences, full '
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
        self.btn_export_sel.setFocusPolicy(Qt.NoFocus)
        self.btn_export_all.setFocusPolicy(Qt.NoFocus)
        export_row.addStretch(1)
        export_row.addWidget(self.btn_export_sel)
        export_row.addWidget(self.btn_export_all)
        rv.addLayout(export_row)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setSizes([SPLITTER_LEFT, SPLITTER_RIGHT])
        self.setCentralWidget(splitter)

        self._render_status = QWidget(self)
        row = QHBoxLayout(self._render_status)
        row.setContentsMargins(0, 0, 0, 0)
        self._render_status_label = QLabel(self._render_status)
        self._render_status_bar = QProgressBar(self._render_status)
        self._render_status_bar.setRange(0, 0)
        self._render_status_bar.setTextVisible(False)
        row.addWidget(self._render_status_label)
        row.addWidget(self._render_status_bar, 1)
        self.statusBar().setStyleSheet('QStatusBar::item { border: none; }')
        self.statusBar().addWidget(self._render_status, 1)
        self._render_status.hide()

    def _set_loaded(self, loaded):
        for w in (
            self.act_export_sel,
            self.act_export_all,
            self.act_close_sdat,
            self.player,
            self.btn_export_sel,
            self.btn_export_all,
            self.filter_edit,
        ):
            w.setEnabled(loaded)
        self.tree_stack.setCurrentWidget(self.tree if loaded else self.empty_placeholder)

    def _toggle_right_panel(self):
        sizes = self.splitter.sizes()
        if sizes[1] > 0:
            self._last_right_size = sizes[1]
            self.splitter.setSizes([sizes[0] + sizes[1], 0])
        else:
            total = sum(sizes)
            right = self._last_right_size or SPLITTER_RIGHT
            self.splitter.setSizes([max(total - right, 100), right])

    def open_sdat(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Open SDAT or NDS ROM', '',
            'Audio files (*.sdat *.nds);;SDAT files (*.sdat);;NDS ROMs (*.nds);;All files (*)'
        )
        if not path:
            return
        self._open_path(path)

    def _open_path(self, path, nds_preset=None):
        """Open a .sdat or .nds by path, nds_preset preselects SDAT indices when reopening a multi-SDAT ROM from Recent (skips picker dialog)."""
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            self._cancel_preview()
            if path.lower().endswith('.nds'):
                self._open_nds(path, preset=nds_preset)
            else:
                self._open_sdat_file(path)
        except Exception as exc:
            QMessageBox.critical(self, 'DualRip', f'Cannot open:\n{exc}')
            return
        finally:
            QApplication.restoreOverrideCursor()
        self._set_loaded(True)
        self._update_status_and_title()

    # -- recent files -----------------------------------------------------------

    def _remember_recent(self, path, sdat_indices):
        path = os.path.abspath(path)
        entries = [e for e in load_recent_files() if os.path.normcase(e['path']) != os.path.normcase(path)]
        entries.insert(0, {'path': path, 'sdats': sdat_indices})
        save_recent_files(entries[:RECENT_MAX])

    @staticmethod
    def _recent_label(path):
        """Compact display path."""
        home = os.path.expanduser('~')
        if os.path.normcase(path).startswith(os.path.normcase(home)):
            path = '~' + path[len(home):]
        return path.replace('\\', '/')

    def _update_recent_enabled(self):
        self.menu_recent.setEnabled(bool(load_recent_files()))

    def _populate_recent_menu(self):
        self.menu_recent.clear()
        entries = load_recent_files()
        if not entries:
            return
        for e in entries:
            act = self.menu_recent.addAction(self._recent_label(e['path']))
            act.setToolTip(e['path'])
            act.triggered.connect(lambda _=False, entry=e: self._open_recent(entry))
        self.menu_recent.addSeparator()
        self.menu_recent.addAction('Clear list', lambda: save_recent_files([]))

    def _open_recent(self, entry):
        if not os.path.exists(entry['path']):
            QMessageBox.warning(self, 'DualRip', f'File not found:\n{entry["path"]}')
            entries = [e for e in load_recent_files()
                       if os.path.normcase(e['path']) != os.path.normcase(entry['path'])]
            save_recent_files(entries)
            return
        self._open_path(entry['path'], nds_preset=entry.get('sdats'))

    # -- drag & drop -----------------------------------------------------------

    def _drag_path(self, event):
        """Single local .sdat/.nds path from a drag, or None if not droppable."""
        if self._sdats:
            return None
        urls = event.mimeData().urls()
        if len(urls) != 1 or not urls[0].isLocalFile():
            return None
        path = urls[0].toLocalFile()
        if path.lower().endswith(('.sdat', '.nds')):
            return path
        return None

    def _set_drop_hint(self, active):
        self.empty_placeholder.setAutoFillBackground(active)
        if active:
            pal = self.empty_placeholder.palette()
            pal.setColor(QPalette.Window, self.palette().color(QPalette.Window).darker)
            self.empty_placeholder.setPalette(pal)
        else:
            self.empty_placeholder.setPalette(self.palette())

    def dragEnterEvent(self, event):
        if self._drag_path(event) is not None:
            event.acceptProposedAction()
            self._set_drop_hint(True)

    def dragLeaveEvent(self, event):
        self._set_drop_hint(False)

    def dropEvent(self, event):
        self._set_drop_hint(False)
        path = self._drag_path(event)
        if path is None:
            return
        event.acceptProposedAction()
        QTimer.singleShot(0, lambda: self._open_path(path))

    def _open_sdat_file(self, path):
        """Open a plain .sdat file — single SDAT, key='0'."""
        self._clear_all()
        sdat = SdatFile(path)
        self._sdats['0'] = (os.path.basename(path), sdat)
        self._sdat_path = path
        self._generation += 1
        self._fill_tree()
        self._remember_recent(path, None)

    def _open_nds(self, path, preset=None):
        """
        Open a .nds ROM: extract SDAT(s), allow multi-select.

        preset: SDAT indices remembered by the Recent menu.
        """
        sdats = find_sdats_in_rom(path)
        chosen = None
        if preset:
            chosen = [s for s in sdats if s['index'] in set(preset)] or None
        if chosen is None:
            if len(sdats) == 1:
                chosen = [sdats[0]]
            else:
                QApplication.restoreOverrideCursor()
                chosen = self._pick_sdats_from_rom(path, sdats)
                QApplication.setOverrideCursor(Qt.WaitCursor)
                if not chosen:
                    raise ValueError('No SDAT selected.')
        self._clear_all()
        rom_name = os.path.basename(path)
        for i, s in enumerate(chosen):
            key = str(i)
            label = f'{rom_name} [SDAT #{s["index"]}]'
            sdat = SdatFile.from_bytes(s['data'], label=label)
            self._sdats[key] = (label, sdat)
        self._sdat_path = rom_name
        self._generation += 1
        self._fill_tree()
        self._remember_recent(path, [s['index'] for s in chosen])

    def _clear_all(self):
        self._sdats.clear()
        self._resolvers.clear()
        self._cache.clear()
        self.player.clear()
        self._item_index = {}
        self.filter_edit.clear()
        for lbl in (
            self.lbl_name,
            self.lbl_kind,
            self.lbl_where,
            self.lbl_bank,
            self.lbl_volume,
            self.lbl_duration,
            self.lbl_loop,
            self.lbl_status,
        ):
            lbl.setText('-')
        self.player.setVisible(False)

    def _pick_sdats_from_rom(self, rom_path, sdats):
        """Show a dialog listing all SDATs — allow multi-selection (Ctrl/Shift-click)."""
        dlg = QDialog(self)
        dlg.setWindowTitle(f'Select SDAT(s) — {os.path.basename(rom_path)}')
        dlg.resize(680, 440)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(f'This ROM contains <b>{len(sdats)} SDAT files</b>.<br>Select one or more (Ctrl/Shift-click) to open:'))
        lst = QListWidget(dlg)
        lst.setSelectionMode(QAbstractItemView.ExtendedSelection)
        for s in sdats:
            size_kb = s['size'] / 1024
            text = (
                f'SDAT #{s["index"]:>3d}  —  {size_kb:>6.0f} KB  |  '
                f'{s["seqarcs"]:>2d} SSAR, {s["sseqs"]:>2d} SSEQ, '
                f'{s["banks"]:>2d} banks, {s["swars"]:>2d} SWAR'
            )
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, s)
            lst.addItem(item)
        lst.setCurrentRow(0)
        lst.itemDoubleClicked.connect(dlg.accept)
        layout.addWidget(lst)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        if dlg.exec() != QDialog.Accepted:
            return None
        return [lst.item(i).data(Qt.UserRole)
            for i in range(lst.count())
            if lst.item(i).isSelected()]

    def _close_sdat(self):
        self._cancel_preview()
        self._clear_all()
        self._sdats.clear()
        self._sdat_path = None
        self._generation += 1
        self.tree.clear()
        self._set_loaded(False)
        self.setWindowTitle('DualRip')
        self.statusBar().clearMessage()

    # -- tree population -----------------------------------------------------

    def _fill_tree(self):
        self.tree.clear()
        self._item_index = {}
        single = len(self._sdats) == 1

        for sdat_key, (label, sdat) in self._sdats.items():
            n_arcs = len(sdat.seqarc_list)
            n_sseq = len(sdat.sequence_list)
            n_banks = sdat.num_banks
            n_swar = len(sdat.wave_archive_list)
            total_entries = sum(c for _i, _n, c in sdat.seqarc_list)
            info_parts = [f'{n_arcs} SSAR, {total_entries} entries']
            if n_sseq:
                info_parts.append(f'{n_sseq} SSEQ')
            info_parts.append(f'{n_banks} banks, {n_swar} SWAR')
            info = ', '.join(info_parts)

            sdat_root = QTreeWidgetItem([label, '', info])
            sdat_root.setData(0, ROLE_KIND, 'sdat')
            sdat_root.setData(0, ROLE_SDAT, sdat_key)
            sdat_root.setFlags(sdat_root.flags() & ~Qt.ItemIsSelectable)

            def _set_sdat(item):
                item.setData(0, ROLE_SDAT, sdat_key)

            # --- Sequence Archives (SSAR) ---
            cat_arcs = QTreeWidgetItem(['Sequence Archives (SSAR)', '', ''])
            cat_arcs.setData(0, ROLE_KIND, 'cat')
            _set_sdat(cat_arcs)
            for arc_id, name, _count in sdat.seqarc_list:
                seqarc = sdat.seqarc(arc_id)
                top = QTreeWidgetItem([name, str(arc_id), ''])
                top.setData(0, ROLE_KIND, 'arc')
                top.setData(0, ROLE_ARC, arc_id)
                _set_sdat(top)
                playable = 0
                for e in seqarc.entries:
                    if e.offset is None:
                        continue
                    bank = str(e.bank_id)
                    if sdat.bank_is_null(e.bank_id):
                        bank += ' (auto)'
                    it = QTreeWidgetItem([e.name, str(e.index), f'bank {bank}'])
                    it.setData(0, ROLE_KIND, 'entry')
                    it.setData(0, ROLE_ARC, arc_id)
                    it.setData(0, ROLE_INDEX, e.index)
                    _set_sdat(it)
                    self._item_index[(sdat_key, arc_id, e.index)] = it
                    top.addChild(it)
                    playable += 1
                top.setText(2, f'{playable} entries')
                cat_arcs.addChild(top)
            sdat_root.addChild(cat_arcs)

            # --- Sequences (SSEQ) ---
            seqs = sdat.sequence_list
            cat_seq = QTreeWidgetItem(['Sequences (SSEQ)', '', f'{len(seqs)}'])
            cat_seq.setData(0, ROLE_KIND, 'seqcat')
            _set_sdat(cat_seq)
            for sid, name, bank_id in seqs:
                bank = ''
                if bank_id is not None:
                    bank = f'bank {bank_id}'
                    if sdat.bank_is_null(bank_id):
                        bank += ' (auto)'
                it = QTreeWidgetItem([name, str(sid), bank])
                it.setData(0, ROLE_KIND, 'seq')
                it.setData(0, ROLE_INDEX, sid)
                _set_sdat(it)
                self._item_index[(sdat_key, ('SSEQ', sid), sid)] = it
                cat_seq.addChild(it)
            sdat_root.addChild(cat_seq)

            # --- Banks (SBNK) ---
            banks = sdat.bank_list
            cat_bank = QTreeWidgetItem(['Banks (SBNK)', '', f'{len(banks)}'])
            cat_bank.setData(0, ROLE_KIND, 'cat')
            _set_sdat(cat_bank)
            for bid, name, wids in banks:
                if wids is None:
                    it = QTreeWidgetItem([name or '(null / dynamic slot)', str(bid), 'filled at runtime'])
                else:
                    shown = ', '.join(str(w) for w in wids if w is not None)
                    it = QTreeWidgetItem([name, str(bid), f'wave archives {shown}'])
                it.setData(0, ROLE_KIND, 'bank')
                it.setData(0, ROLE_INDEX, bid)
                _set_sdat(it)
                cat_bank.addChild(it)
            sdat_root.addChild(cat_bank)

            # --- Wave Archives (SWAR) ---
            wars = sdat.wave_archive_list
            cat_war = QTreeWidgetItem(['Wave Archives (SWAR)', '', f'{len(wars)}'])
            cat_war.setData(0, ROLE_KIND, 'cat')
            _set_sdat(cat_war)
            for wid, name, cnt in wars:
                it = QTreeWidgetItem([name, str(wid), f'{cnt} waves'])
                it.setData(0, ROLE_KIND, 'war')
                it.setData(0, ROLE_INDEX, wid)
                _set_sdat(it)
                cat_war.addChild(it)
            sdat_root.addChild(cat_war)

            self.tree.addTopLevelItem(sdat_root)
            if single:
                sdat_root.setExpanded(True)

    def _update_status_and_title(self):
        if not self._sdats:
            return
        total_entries = 0
        total_sseq = 0
        n_sdats = len(self._sdats)
        for _label, sdat in self._sdats.values():
            total_entries += sum(c for _i, _n, c in sdat.seqarc_list)
            total_sseq += len(sdat.sequence_list)
        base = os.path.basename(self._sdat_path or '')
        if n_sdats == 1:
            self.statusBar().showMessage(
                f'{base} - {total_entries} entries, {total_sseq} music sequences.'
            )
        else:
            self.statusBar().showMessage(
                f'{base} - {n_sdats} SDATs, {total_entries} entries, {total_sseq} music sequences.'
            )
        self.setWindowTitle(f'DualRip - {base}' if base else 'DualRip')

    def _apply_filter(self, text):
        text = text.strip().lower()
        matches = []

        def filter_children(item):
            visible = 0
            for j in range(item.childCount()):
                child = item.child(j)
                if child.childCount():
                    sub = filter_children(child)
                    child.setHidden(bool(text) and sub == 0)
                    if text and sub:
                        child.setExpanded(True)
                    visible += sub
                else:
                    match = not text or text in child.text(0).lower()
                    child.setHidden(not match)
                    if text and match:
                        matches.append(child)
                    visible += match
            return visible

        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            vis = filter_children(top)
            top.setHidden(bool(text) and vis == 0)
            if text and vis:
                top.setExpanded(True)

        if text:
            self.tree.clearSelection()
            for it in matches:
                it.setSelected(True)
            if matches:
                self.tree.scrollToItem(matches[0])
            n = len(matches)
            self.statusBar().showMessage(f'{n} match' + ('' if n == 1 else 'es'))
        else:
            self._update_status_and_title()

    # -- selection & details helpers -----------------------------------------

    def _sdat_for(self, it):
        """Return the SdatFile that owns this tree item, or None."""
        key = it.data(0, ROLE_SDAT)
        if key is not None and key in self._sdats:
            return self._sdats[key][1]
        return None

    def _sdat_key_for(self, it):
        return it.data(0, ROLE_SDAT)

    def _current_playable(self):
        """Return (sdat, sdat_key, seqarc, entry) for current item if renderable, else (None, None, None, None)."""
        it = self.tree.currentItem()
        if it is None or not self._sdats:
            return None, None, None, None
        sdat = self._sdat_for(it)
        if sdat is None:
            return None, None, None, None
        kind = it.data(0, ROLE_KIND)
        if kind == 'entry':
            seqarc = sdat.seqarc(it.data(0, ROLE_ARC))
            return sdat, self._sdat_key_for(it), seqarc, seqarc.entries[it.data(0, ROLE_INDEX)]
        if kind == 'seq':
            seqarc = sdat.sequence(it.data(0, ROLE_INDEX))
            return sdat, self._sdat_key_for(it), seqarc, seqarc.entries[0]
        return None, None, None, None

    def _selection_changed(self, *_):
        it = self.tree.currentItem()
        if it is None or not self._sdats:
            return
        sdat = self._sdat_for(it)
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
            _sd, sk, seqarc, entry = self._current_playable()
            self.lbl_name.setText(entry.name)
            if kind == 'entry':
                self.lbl_kind.setText('Sound effect (SSAR entry)')
                self.lbl_where.setText(f'{seqarc.arc_id:03d}  {seqarc.name}  [{entry.index}]')
            else:
                self.lbl_kind.setText('Music sequence (SSEQ)')
                self.lbl_where.setText(f'sequence {entry.index}')
            bank = str(entry.bank_id)
            if sdat is not None:
                bname = sdat.bank_name(entry.bank_id)
                if sdat.bank_is_null(entry.bank_id):
                    bank += ' (dynamic slot, resolved automatically)'
                elif bname:
                    bank += f' {bname}'
            self.lbl_bank.setText(bank)
            self.lbl_volume.setText(str(entry.volume))
            self._show_render(self._cache.get(self._cache_key(sk, seqarc, entry)))
        elif kind == 'arc':
            if sdat is not None:
                arc_id = it.data(0, ROLE_ARC)
                seqarc = sdat.seqarc(arc_id)
                self.lbl_name.setText(seqarc.name)
                self.lbl_kind.setText('Sequence archive (SSAR)')
                self.lbl_where.setText(f'archive {arc_id:03d}')
                self.lbl_status.setText(it.text(2))
        elif kind == 'seqcat':
            self.lbl_name.setText('Sequences (SSEQ)')
            self.lbl_kind.setText('Music sequence collection')
            self.lbl_where.setText(f'{it.text(2)} sequences')
            if sdat is not None:
                sk = self._sdat_key_for(it)
                lbl = self._sdats[sk][0] if sk in self._sdats else ''
                self.lbl_status.setText(f'from {lbl}' if lbl else 'select to export all music')
        elif kind == 'bank':
            if sdat is not None:
                bid = it.data(0, ROLE_INDEX)
                self.lbl_name.setText(it.text(0))
                self.lbl_kind.setText('Instrument bank (SBNK)')
                self.lbl_where.setText(f'bank {bid}')
                if sdat.bank_is_null(bid):
                    self.lbl_bank.setText('NULL slot - filled at runtime by the game; use Settings > Bank map to pin a substitute, or let auto-resolution pick one per entry')
                else:
                    meta = sdat.bank_meta(bid)
                    if meta:
                        _e, _c, wids = meta
                        names = []
                        for w in wids:
                            if w is not None:
                                wn = next((n for i, n, _c2 in sdat.wave_archive_list if i == w), str(w))
                                names.append(f'{w} ({wn})')
                        self.lbl_bank.setText('wave archives: ' + (', '.join(names) or 'none'))
        elif kind == 'war':
            self.lbl_name.setText(it.text(0))
            self.lbl_kind.setText('Wave archive (SWAR)')
            self.lbl_where.setText(f'wave archive {it.text(1)}')
            self.lbl_status.setText(it.text(2))
        elif kind == 'sdat':
            self.lbl_name.setText(it.text(0))
            self.lbl_kind.setText('SDAT container')
            self.lbl_where.setText(it.text(2))
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
        self.lbl_status.setText(res.status + (f'({res.error})' if res.error else ''))
        if res.bank_label and '->' in res.bank_label:
            self.lbl_bank.setText(self.lbl_bank.text().split('(')[0] + f'(auto: {res.bank_label})')

    # -- bank resolver -------------------------------------------------------

    def _override_map(self):
        try:
            return parse_bank_map(self.settings['bank_map'])
        except Exception:
            return {}

    def _resolver(self, sdat, sk, seqarc):
        key = (sk, seqarc.arc_id)
        if key not in self._resolvers:
            self._resolvers[key] = BankResolver(sdat, seqarc, self._override_map())
        return self._resolvers[key]

    def _cache_key(self, sk, seqarc, entry):
        return (self._generation, sk, seqarc.arc_id, entry.index, self.settings['rate'])

    # -- playback ------------------------------------------------------------

    def _show_render_progress(self, name):
        self.statusBar().clearMessage()
        self._render_status_label.setText(f'Rendering {name}')
        self._render_status.show()
        self._update_render_progress_width()

    def _update_render_progress_width(self):
        """Keep the render indicator aligned and sized."""
        if not self._render_status.isVisible():
            return
        self.statusBar().layout().activate()
        item_x = self._render_status.mapTo(self.statusBar(), QPoint(0, 0)).x()
        self._render_status.layout().setContentsMargins(max(STATUSBAR_MSG_LEFT - item_x, 0), 0, 0, 0)
        panel_right = self.splitter.mapTo(self, QPoint(self.splitter.sizes()[0], 0)).x()
        offset = self._render_status.mapTo(self, QPoint(0, 0)).x()
        self._render_status.setMaximumWidth(max(panel_right - offset, 0))

    def _hide_render_progress(self):
        self._render_status.hide()

    def eventFilter(self, obj, event):
        if obj is getattr(self, '_left_panel', None) and event.type() == QEvent.Resize:
            self._update_render_progress_width()
        elif (
            obj is getattr(self, 'tree', None)
            and event.type() == QEvent.KeyPress
            and event.key() == Qt.Key_Space
            and not event.modifiers()
            and not event.isAutoRepeat()
        ):
            self._toggle_play_pause()
            return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Space and not event.modifiers() and not event.isAutoRepeat():
            self._toggle_play_pause()
            return
        super().keyPressEvent(event)

    def _on_loaded_changed(self, key):
        """
        Player's loaded track changed: bold the matching tree row.

        key is a cache key (generation, sdat_key, arc_id, entry_index, rate) or None.
        """
        item = None if key is None else self._item_index.get(tuple(key[1:4]))
        self._set_playing_item(item)

    def _set_playing_item(self, item):
        """Bold row for tree item currently loaded in player."""
        if self._playing_item is item:
            return
        if self._playing_item is not None:
            self._set_row_bold(self._playing_item, False)
        self._playing_item = item
        if item is not None:
            self._set_row_bold(item, True)

    def _set_row_bold(self, item, bold):
        for col in range(self.tree.columnCount()):
            f = item.font(col)
            f.setBold(bold)
            item.setFont(col, f)

    def _toggle_play_pause(self):
        if not self._sdats:
            return
        st = audio.state()
        if st == audio.PLAYING:
            audio.pause()
        elif st == audio.PAUSED:
            self.player.resume()
        else:
            self._on_play_clicked()

    def _on_play_clicked(self):
        sdat, sk, seqarc, entry = self._current_playable()
        if entry is None or sdat is None:
            return
        same = self.player.loaded_key == self._cache_key(sk, seqarc, entry)
        if same and audio.is_live():
            st = audio.state()
            if st == audio.PLAYING:
                return
            if st == audio.PAUSED:
                self.player.resume()
                return
            self.play_selected()
            return
        if same and (audio.state() == audio.PAUSED or audio.position() > 0):
            self.player.resume()
            return
        self.play_selected()

    def play_selected(self):
        sdat, sk, seqarc, entry = self._current_playable()
        if entry is None or sdat is None:
            return
        it = self.tree.currentItem()
        kind = it.data(0, ROLE_KIND) if it is not None else None
        key = self._cache_key(sk, seqarc, entry)
        if kind == 'seq':
            if self.player.loaded_key == key and audio.is_live():
                audio.request_seek(0)
                self.player.resume()
                return
            self._play_music_live(sdat, sk, key, seqarc, entry)
            return
        cached = self._cache.get(key)
        if cached is not None:
            self._cancel_preview()
            self._show_render(cached)
            if cached.audio is not None:
                self._play(key, cached)
            else:
                self.statusBar().showMessage(
                    f'{cached.name}: {cached.status}'
                    + (f'({cached.error})' if cached.error else '')
                )
            return
        self._cancel_preview()
        audio.unload()
        self._show_render_progress(entry.name)
        worker = StreamWorker(key, sdat, seqarc, entry, self.settings['rate'], self._resolver(sdat, sk, seqarc))
        worker.done.connect(self._preview_done)
        worker.failed.connect(self._preview_failed)
        self._preview_worker = worker
        self._preview_key = key
        self.player.begin_stream(key, self.settings['rate'])
        worker.start()

    def _play_music_live(self, sdat, sk, key, seqarc, entry):
        """Start seamless live music preview (LiveWorker)."""
        self._cancel_preview()
        audio.unload()
        self._show_render_progress(entry.name)
        worker = LiveWorker(key, sdat, seqarc, entry, self.settings['rate'], self._resolver(sdat, sk, seqarc))
        worker.meta.connect(self._music_meta)
        worker.failed.connect(self._preview_failed)
        self._preview_worker = worker
        self._preview_key = key
        self.player.begin_live(key, self.settings['rate'])
        worker.start()

    def _music_meta(self, key, res):
        """LiveWorker reported exact length/loops."""
        if key != self._preview_key:
            return
        self._hide_render_progress()
        self._cache[key] = res
        self._cache.move_to_end(key)
        self._evict_cache()
        _sd, sk, cur_arc, cur_entry = self._current_playable()
        if cur_entry is not None and self._cache_key(sk, cur_arc, cur_entry) == key:
            self._show_render(res)
            if res.status in ('ok', 'loop'):
                self.statusBar().showMessage(f'{res.name}: {res.duration:.2f}s')
            else:
                self.statusBar().showMessage(f'{res.name}: {res.status}' + (f'({res.error})' if res.error else ''))

    def _cancel_preview(self):
        self._hide_render_progress()
        worker, self._preview_worker = self._preview_worker, None
        self._preview_key = None
        if worker is not None:
            worker.cancel()
            worker.wait()

    def _finish_preview_worker(self):
        worker, self._preview_worker = self._preview_worker, None
        if worker is not None:
            worker.wait()

    def _play(self, key, res):
        if self.player.load_result(key, res, self.settings['rate']):
            self.statusBar().showMessage(f'{res.name}: {res.duration:.2f}s')
            return True
        self.statusBar().showMessage('Playback unavailable (sounddevice missing or no audio output device).')
        return False

    def _evict_cache(self):
        def total_bytes():
            return sum(r.audio.nbytes for r in self._cache.values()
            if r.audio is not None)

        while len(self._cache) > CACHE_SIZE or (len(self._cache) > 1 and total_bytes() > CACHE_MAX_BYTES):
            self._cache.popitem(last=False)

    def _preview_done(self, key, res):
        if key != self._preview_key:
            return
        self._hide_render_progress()
        self._finish_preview_worker()
        self._preview_key = None
        if res.audio is None:
            self.player.clear()
        self._cache[key] = res
        self._cache.move_to_end(key)
        self._evict_cache()
        _sd, sk, cur_arc, cur_entry = self._current_playable()
        if cur_entry is not None and self._cache_key(sk, cur_arc, cur_entry) == key:
            self._show_render(res)
            if res.audio is None:
                self.statusBar().showMessage(
                    f'{res.name}: {res.status}'
                    + (f'({res.error})' if res.error else '')
                )
            else:
                self.statusBar().showMessage(f'{res.name}: {res.duration:.2f}s')

    def _preview_failed(self, key, msg):
        if key != self._preview_key:
            return
        self._hide_render_progress()
        self._finish_preview_worker()
        self._preview_key = None
        self.player.clear()
        self.statusBar().showMessage(f'Render failed: {msg}')

    # -- export --------------------------------------------------------------

    def _selected_jobs(self):
        """
        Build tagged jobs [(sdat_key, kind, ident, sel)] from tree selection.
        Each job belongs to the SDAT identified by sdat_key.
        """
        per_sdat = {}
        for it in self.tree.selectedItems():
            sk = self._sdat_key_for(it)
            if sk is None:
                continue
            if sk not in per_sdat:
                per_sdat[sk] = {'whole': set(), 'partial': {}, 'seq': set(), 'all_seqs': False}
            p = per_sdat[sk]
            kind = it.data(0, ROLE_KIND)
            if kind == 'arc':
                p['whole'].add(it.data(0, ROLE_ARC))
            elif kind == 'entry':
                arc = it.data(0, ROLE_ARC)
                p['partial'].setdefault(arc, set()).add(it.data(0, ROLE_INDEX))
            elif kind == 'seq':
                p['seq'].add(it.data(0, ROLE_INDEX))
            elif kind == 'seqcat':
                p['all_seqs'] = True
        jobs = []
        for sk, p in per_sdat.items():
            for a in sorted(p['whole']):
                jobs.append((sk, 'arc', a, None))
            for a, idxs in sorted(p['partial'].items()):
                if a not in p['whole']:
                    jobs.append((sk, 'arc', a, idxs))
            if p['all_seqs']:
                jobs.append((sk, 'seq', None, None))
            elif p['seq']:
                jobs.append((sk, 'seq', None, p['seq']))
        return jobs

    def export_selection(self):
        jobs = self._selected_jobs()
        if not jobs:
            QMessageBox.information(
                self, 'DualRip',
                'Select entries, archives or sequences first (Ctrl/Shift-click for multiple).',
            )
            return
        self._open_export(jobs)

    def export_all(self):
        jobs = []
        for sk, (_label, sdat) in self._sdats.items():
            for i, _n, _c in sdat.seqarc_list:
                jobs.append((sk, 'arc', i, None))
            if sdat.sequence_list:
                jobs.append((sk, 'seq', None, None))
        self._open_export(jobs)

    def _open_export(self, jobs):
        self.settings = load_settings()
        dlg = ExportDialog(self._sdats, jobs, self.settings['rate'], self._override_map(), self)
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
                self._cancel_preview()
                self._cache.clear()
                self._resolvers.clear()
                self.player.clear()
            self.statusBar().showMessage('Settings saved.')

    def show_about(self):
        QMessageBox.about(
            self,'About DualRip',
            f'<b>DualRip {__version__}</b><br>'
            'Nintendo DS SDAT sound-effect (SSAR) and music (SSEQ) ripper.<br><br>'
            'Playback core is a Python port of the FeOS Sound System (fincs), as adapted by Naram Qashat (CyberBotX) for the NCSF player (in_xsf). Driver tables originate from disassembly of Nintendo\'s NNS sound driver by those authors.',
        )

    def closeEvent(self, event):
        self._cancel_preview()
        audio.shutdown()
        event.accept()
