#!/usr/bin/env python3
"""
C-CoderDBC GUI
==============
A modern graphical interface for the *coderdbc* C code generation tool.

Features
--------
* Load and preview any DBC file — messages and their signals shown in a tree.
* Select / deselect individual messages (or entire groups) to control which
  pack/unpack functions get generated.
* Auto-group similar messages by common name prefix + numeric suffix
  (e.g. 40 radar-object messages become one collapsible group).
* Live search / filter across message names.
* All CLI options of the ``coderdbc`` binary are exposed as toggle switches.
* Generation runs in a background thread; a progress bar and coloured log
  pane keep you informed.

Requirements
------------
    pip install ttkbootstrap>=1.10.0

Usage
-----
    python3 gui.py

Build the C++ binary first if you haven't already::

    cmake -S src -B build
    cmake --build build
"""

import os
import re
import sys
import subprocess
import tempfile
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:
    import ttkbootstrap as ttk
    from ttkbootstrap.constants import *
except ImportError:
    print(
        "ERROR: ttkbootstrap is required.\n"
        "Install it with:  pip install ttkbootstrap>=1.10.0",
        file=sys.stderr,
    )
    sys.exit(1)

import tkinter as tk
from tkinter import filedialog, messagebox

# ── Constants ──────────────────────────────────────────────────────────────────

CHECKBOX_ON = "☑"
CHECKBOX_OFF = "☐"
CHECKBOX_PARTIAL = "⊟"

GROUP_MIN_SIZE = 2       # Minimum messages required to form an auto-group
APP_VERSION = "1.0"


# ── Data Classes ───────────────────────────────────────────────────────────────

@dataclass
class Signal:
    """Represents one CAN signal parsed from a DBC file."""
    name: str
    start_bit: int
    bit_length: int
    byte_order: str   # 'little_endian' or 'big_endian'
    value_type: str   # 'unsigned' or 'signed'
    factor: float
    offset: float
    raw_line: str


@dataclass
class Message:
    """Represents one CAN message parsed from a DBC file."""
    msg_id: int
    name: str
    dlc: int
    transmitter: str
    signals: List[Signal] = field(default_factory=list)
    selected: bool = True


# ── DBC Parser ─────────────────────────────────────────────────────────────────

class DbcParser:
    """
    Lightweight DBC file parser used for GUI preview and filtered output.

    This is intentionally minimal — it extracts only the information needed
    to populate the tree view and to write a filtered copy of the DBC file.
    The ``coderdbc`` binary performs the authoritative full parse for code
    generation.
    """

    # BO_ <id> <name> : <dlc> <transmitter>
    _MSG_RE = re.compile(r"^BO_\s+(\d+)\s+(\w+)\s*:\s*(\d+)\s+(\S+)")

    # SG_ <name> [M | m<n>] : <start>|<len>@<order><sign> (<factor>,<offset>) [<min>|<max>] "<unit>"
    # The optional multiplexor indicator is either "M" (multiplexer) or "m<digits>" (multiplexed).
    _SIG_RE = re.compile(
        r"^\s+SG_\s+(\w+)\s*(?:[Mm]\d*)?\s*:\s*"
        r"(\d+)\|(\d+)@([01])([+-])\s*"
        r"\(([^,]+),([^)]+)\)\s*\[[^\]]*\]\s*\"[^\"]*\""
    )

    # BO_TX_BU_ or any other line starting with BO_ but not a message definition
    _MSG_START_RE = re.compile(r"^BO_\s+(\d+)\s+")

    def parse(self, filepath: str) -> List[Message]:
        """Parse *filepath* and return a list of :class:`Message` objects."""
        messages: List[Message] = []
        current: Optional[Message] = None

        with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                m = self._MSG_RE.match(raw_line)
                if m:
                    if current is not None:
                        messages.append(current)
                    current = Message(
                        msg_id=int(m.group(1)),
                        name=m.group(2),
                        dlc=int(m.group(3)),
                        transmitter=m.group(4),
                    )
                    continue

                if current is not None:
                    s = self._SIG_RE.match(raw_line)
                    if s:
                        try:
                            sig = Signal(
                                name=s.group(1),
                                start_bit=int(s.group(2)),
                                bit_length=int(s.group(3)),
                                byte_order="little_endian" if s.group(4) == "1" else "big_endian",
                                value_type="signed" if s.group(5) == "-" else "unsigned",
                                factor=float(s.group(6)),
                                offset=float(s.group(7)),
                                raw_line=raw_line.rstrip(),
                            )
                            current.signals.append(sig)
                        except ValueError:
                            pass

        if current is not None:
            messages.append(current)

        return messages

    def write_filtered(
        self,
        src_path: str,
        selected_ids: Set[int],
        dst_path: str,
    ) -> None:
        """
        Copy *src_path* to *dst_path*, keeping only ``BO_`` blocks whose
        message ID is in *selected_ids*.  All non-message sections
        (``NS_``, ``BS_``, ``BU_``, ``CM_``, ``BA_``, ``VAL_``, …) are
        preserved unchanged so that the ``coderdbc`` binary receives a
        well-formed DBC file.
        """
        with open(src_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()

        output: List[str] = []
        in_msg_block = False
        skip_block = False

        for line in lines:
            m = self._MSG_START_RE.match(line)
            if m:
                in_msg_block = True
                skip_block = int(m.group(1)) not in selected_ids
            elif in_msg_block:
                stripped = line.strip()
                # A BO_ block ends on a blank line OR when a non-indented line
                # is encountered (DBC signals always start with whitespace).
                if stripped == "" or (stripped and not line[0].isspace()):
                    in_msg_block = False
                    if skip_block:
                        skip_block = False
                        if stripped == "":
                            # Drop the trailing blank line of the skipped block
                            continue
                        # Non-blank top-level line: fall through to output it

            if not skip_block:
                output.append(line)

        with open(dst_path, "w", encoding="utf-8") as fh:
            fh.writelines(output)


# ── Auto-grouping ──────────────────────────────────────────────────────────────

def detect_groups(
    messages: List[Message],
    min_size: int = GROUP_MIN_SIZE,
) -> Dict[str, List[Message]]:
    """
    Detect groups of similarly-named messages.

    Messages whose names match ``<prefix><digits>`` (with an optional
    trailing underscore before the digits) are placed in a group named
    ``<prefix>``.  Groups with fewer than *min_size* members are kept as
    individual entries.

    Returns an ``OrderedDict``-style plain dict: ``group_name → [Message, …]``.
    """
    _NUM_SUFFIX = re.compile(r"^(.*?)_?(\d+)$")
    bucket: Dict[str, List[Message]] = defaultdict(list)

    for msg in messages:
        m = _NUM_SUFFIX.match(msg.name)
        if m:
            prefix = m.group(1).rstrip("_") or msg.name
            bucket[prefix].append(msg)
        else:
            bucket[msg.name].append(msg)

    groups: Dict[str, List[Message]] = {}
    for prefix, msgs in bucket.items():
        if len(msgs) >= min_size:
            groups[prefix] = sorted(msgs, key=lambda x: x.name)
        else:
            for msg in msgs:
                groups[msg.name] = [msg]

    return groups


# ── Main Application ──────────────────────────────────────────────────────────

class CoderDbcGui(ttk.Window):
    """
    Main application window for the C-CoderDBC GUI.

    Layout
    ------
    Left pane  — DBC tree (messages → signals) with checkbox selection,
                 search bar, and group controls.
    Right pane — Tabbed panel: Settings tab (paths, options, generate button)
                 and Output Log tab (coloured text console).
    """

    APP_TITLE = f"C-CoderDBC GUI  v{APP_VERSION}"

    def __init__(self) -> None:
        super().__init__(
            title=self.APP_TITLE,
            themename="cosmo",
            size=(1200, 800),
            minsize=(860, 600),
        )

        # ── Application variables ───────────────────────────────────────────
        self._dbc_path = tk.StringVar()
        self._out_path = tk.StringVar()
        self._drv_name = tk.StringVar()
        self._binary_path = tk.StringVar(value=self._find_binary())
        self._search_var = tk.StringVar()
        self._summary_var = tk.StringVar(value="No DBC file loaded")
        self._sel_summary = tk.StringVar(value="0 messages selected")

        # Code-generation option toggles (map to coderdbc CLI flags)
        self._opt_rw = tk.BooleanVar(value=True)
        self._opt_nodeutils = tk.BooleanVar(value=False)
        self._opt_driverdir = tk.BooleanVar(value=False)
        self._opt_gendate = tk.BooleanVar(value=False)
        self._opt_noconfig = tk.BooleanVar(value=False)
        self._opt_noinc = tk.BooleanVar(value=False)
        self._opt_nofmon = tk.BooleanVar(value=False)

        # ── Internal state ──────────────────────────────────────────────────
        self._messages: List[Message] = []
        self._groups: Dict[str, List[Message]] = {}
        # Maps tree-item IID → ("group", group_name) | ("msg", Message)
        self._tree_items: Dict[str, Tuple[str, object]] = {}
        self._parser = DbcParser()

        # ── Build UI ────────────────────────────────────────────────────────
        self._build_ui()
        self._check_binary()

    # ── Binary detection ────────────────────────────────────────────────────

    def _find_binary(self) -> str:
        """Locate the coderdbc binary, checking common build locations first."""
        import shutil
        here = Path(__file__).parent
        candidates = [
            here / "build" / "coderdbc",
            here / "build" / "Release" / "coderdbc",
            here / "build" / "Debug" / "coderdbc",
            here / "build" / "coderdbc.exe",
            here / "build" / "Release" / "coderdbc.exe",
        ]
        for c in candidates:
            if c.exists():
                return str(c.resolve())
        found = shutil.which("coderdbc")
        return found or ""

    def _check_binary(self) -> None:
        bp = self._binary_path.get()
        if not bp or not Path(bp).exists():
            self._log(
                "⚠  coderdbc binary not found.  "
                "Build the project first:\n"
                "    cmake -S src -B build && cmake --build build\n"
                "Then update the Binary path in the Settings tab.",
                tag="warning",
            )

    # ── UI Construction ──────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Title bar
        hdr = ttk.Frame(self, padding=(10, 6))
        hdr.pack(fill=X)
        ttk.Label(
            hdr, text="C-CoderDBC", font=("", 18, "bold"), bootstyle="primary"
        ).pack(side=LEFT)
        ttk.Label(
            hdr, text="  DBC Preview & C Code Generator", bootstyle="secondary"
        ).pack(side=LEFT)

        ttk.Separator(self, orient=HORIZONTAL).pack(fill=X)

        # Main split: left = tree, right = settings+log
        paned = ttk.Panedwindow(self, orient=HORIZONTAL)
        paned.pack(fill=BOTH, expand=True, padx=6, pady=6)

        left = ttk.Frame(paned, padding=4)
        right = ttk.Frame(paned, padding=4)
        paned.add(left, weight=3)
        paned.add(right, weight=2)

        self._build_tree_panel(left)
        self._build_right_panel(right)

    # ── Left panel — DBC tree ────────────────────────────────────────────────

    def _build_tree_panel(self, parent: ttk.Frame) -> None:
        # Header
        hdr = ttk.Frame(parent)
        hdr.pack(fill=X, pady=(0, 4))
        ttk.Label(hdr, text="DBC Preview", font=("", 12, "bold")).pack(side=LEFT)
        ttk.Button(
            hdr, text="📂  Load DBC…", command=self._browse_dbc, bootstyle="primary"
        ).pack(side=RIGHT)

        # File path (read-only)
        ttk.Entry(
            parent, textvariable=self._dbc_path, state="readonly", bootstyle="secondary"
        ).pack(fill=X, pady=(0, 4))

        # Toolbar
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill=X, pady=4)

        ttk.Label(toolbar, text="🔍").pack(side=LEFT)
        ttk.Entry(toolbar, textvariable=self._search_var, width=20).pack(
            side=LEFT, padx=(2, 8)
        )
        self._search_var.trace_add("write", lambda *_: self._apply_filter())

        ttk.Button(
            toolbar, text="Auto-Group", command=self._auto_group, bootstyle="info-outline"
        ).pack(side=LEFT, padx=2)
        ttk.Button(
            toolbar, text="Ungroup", command=self._ungroup, bootstyle="secondary-outline"
        ).pack(side=LEFT, padx=2)

        ttk.Separator(toolbar, orient=VERTICAL).pack(side=LEFT, fill=Y, padx=8)

        ttk.Button(
            toolbar, text="✓ All", command=self._select_all, bootstyle="success-outline"
        ).pack(side=LEFT, padx=2)
        ttk.Button(
            toolbar, text="✗ None", command=self._deselect_all, bootstyle="danger-outline"
        ).pack(side=LEFT, padx=2)

        # Summary label
        ttk.Label(
            parent, textvariable=self._summary_var, bootstyle="secondary"
        ).pack(anchor=W, pady=(2, 0))

        # Tree container
        tree_container = ttk.Frame(parent)
        tree_container.pack(fill=BOTH, expand=True, pady=(4, 0))

        cols = ("id", "dlc", "signals", "transmitter")
        self._tree = ttk.Treeview(
            tree_container,
            columns=cols,
            show="tree headings",
            selectmode="browse",
            bootstyle="primary",
        )
        self._tree.heading("#0", text="  Name")
        self._tree.heading("id", text="ID (dec)")
        self._tree.heading("dlc", text="DLC")
        self._tree.heading("signals", text="Signals")
        self._tree.heading("transmitter", text="Transmitter")

        self._tree.column("#0", width=250, minwidth=150)
        self._tree.column("id", width=90, anchor=CENTER, minwidth=60)
        self._tree.column("dlc", width=50, anchor=CENTER, minwidth=40)
        self._tree.column("signals", width=60, anchor=CENTER, minwidth=40)
        self._tree.column("transmitter", width=110, anchor=CENTER, minwidth=70)

        vsb = ttk.Scrollbar(tree_container, orient=VERTICAL, command=self._tree.yview)
        hsb = ttk.Scrollbar(tree_container, orient=HORIZONTAL, command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_container.rowconfigure(0, weight=1)
        tree_container.columnconfigure(0, weight=1)

        self._tree.bind("<ButtonRelease-1>", self._on_tree_click)

    # ── Right panel — Settings + Log ─────────────────────────────────────────

    def _build_right_panel(self, parent: ttk.Frame) -> None:
        nb = ttk.Notebook(parent, bootstyle="primary")
        nb.pack(fill=BOTH, expand=True)

        settings_frame = ttk.Frame(nb, padding=10)
        nb.add(settings_frame, text=" ⚙  Settings ")
        self._build_settings_tab(settings_frame)

        log_frame = ttk.Frame(nb, padding=6)
        nb.add(log_frame, text=" 📋  Output Log ")
        self._build_log_tab(log_frame)

    def _build_settings_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(
            parent, text="Code Generation Settings", font=("", 11, "bold")
        ).pack(anchor=W, pady=(0, 8))

        # Output directory
        ttk.Label(parent, text="Output directory").pack(anchor=W)
        out_row = ttk.Frame(parent)
        out_row.pack(fill=X, pady=(2, 8))
        ttk.Entry(out_row, textvariable=self._out_path).pack(
            side=LEFT, fill=X, expand=True
        )
        ttk.Button(out_row, text="…", width=3, command=self._browse_out_dir).pack(
            side=LEFT, padx=(4, 0)
        )

        # Driver name
        ttk.Label(parent, text="Driver name").pack(anchor=W)
        ttk.Entry(parent, textvariable=self._drv_name).pack(fill=X, pady=(2, 8))

        # Binary path
        ttk.Label(parent, text="coderdbc binary").pack(anchor=W)
        bin_row = ttk.Frame(parent)
        bin_row.pack(fill=X, pady=(2, 8))
        ttk.Entry(bin_row, textvariable=self._binary_path).pack(
            side=LEFT, fill=X, expand=True
        )
        ttk.Button(bin_row, text="…", width=3, command=self._browse_binary).pack(
            side=LEFT, padx=(4, 0)
        )

        ttk.Separator(parent).pack(fill=X, pady=8)
        ttk.Label(parent, text="Options", font=("", 10, "bold")).pack(
            anchor=W, pady=(0, 4)
        )

        opts = [
            (self._opt_rw,        "Rewrite existing files  (-rw)"),
            (self._opt_nodeutils, "Per-node utilities  (-nodeutils)"),
            (self._opt_driverdir, "Driver subdirectory  (-driverdir)"),
            (self._opt_gendate,   "Add generation date  (-gendate)"),
            (self._opt_noconfig,  "Skip config file  (-noconfig)"),
            (self._opt_noinc,     "Skip canmonitorutil  (-noinc)"),
            (self._opt_nofmon,    "Skip fmon header  (-nofmon)"),
        ]
        for var, label in opts:
            ttk.Checkbutton(
                parent, text=label, variable=var, bootstyle="primary-round-toggle"
            ).pack(anchor=W, pady=2)

        ttk.Separator(parent).pack(fill=X, pady=8)

        ttk.Label(parent, textvariable=self._sel_summary, bootstyle="info").pack(
            anchor=W
        )

        self._gen_btn = ttk.Button(
            parent,
            text="⚡  Generate C Code",
            command=self._generate,
            bootstyle="success",
        )
        self._gen_btn.pack(fill=X, pady=(8, 4))

        self._progress = ttk.Progressbar(
            parent, bootstyle="success-striped", mode="indeterminate"
        )
        self._progress.pack(fill=X)

    def _build_log_tab(self, parent: ttk.Frame) -> None:
        text_frame = ttk.Frame(parent)
        text_frame.pack(fill=BOTH, expand=True)

        self._log_text = tk.Text(
            text_frame,
            state="disabled",
            wrap="word",
            font=("Courier", 9),
            bg="#1e1e1e",
            fg="#d4d4d4",
            insertbackground="white",
            relief="flat",
        )
        vsb = ttk.Scrollbar(text_frame, orient=VERTICAL, command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=vsb.set)
        self._log_text.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)

        # Colour tags for log levels
        self._log_text.tag_config("warning", foreground="#f0ad4e")
        self._log_text.tag_config("success", foreground="#5cb85c")
        self._log_text.tag_config("danger",  foreground="#d9534f")
        self._log_text.tag_config("info",    foreground="#5bc0de")

        ttk.Button(
            parent, text="Clear Log", command=self._clear_log, bootstyle="secondary-outline"
        ).pack(anchor=E, pady=4)

    # ── DBC loading ──────────────────────────────────────────────────────────

    def _browse_dbc(self) -> None:
        path = filedialog.askopenfilename(
            title="Select DBC file",
            filetypes=[("DBC files", "*.dbc"), ("All files", "*.*")],
        )
        if path:
            self._dbc_path.set(path)
            self._load_dbc(path)

    def _load_dbc(self, path: str) -> None:
        self._log(f"Loading: {path}", tag="info")
        try:
            self._messages = self._parser.parse(path)
        except Exception as exc:
            self._log(f"ERROR parsing DBC file: {exc}", tag="danger")
            return

        # Start with a flat (one group per message) layout
        self._groups = {msg.name: [msg] for msg in self._messages}
        self._populate_tree()
        self._update_summary()
        self._log(f"Loaded {len(self._messages)} message(s).", tag="success")

        # Auto-fill driver name and output dir from the filename
        stem = Path(path).stem
        if not self._drv_name.get():
            self._drv_name.set(stem)
        if not self._out_path.get():
            self._out_path.set(str(Path(path).parent / "gencode"))

    # ── Tree population ───────────────────────────────────────────────────────

    def _populate_tree(self) -> None:
        """Rebuild the entire Treeview from *self._groups*, respecting the search filter."""
        self._tree.delete(*self._tree.get_children())
        self._tree_items.clear()

        filter_text = self._search_var.get().lower()

        for group_name, msgs in self._groups.items():
            visible = [
                m for m in msgs
                if not filter_text or filter_text in m.name.lower()
            ]
            if not visible:
                continue

            is_group = len(msgs) > 1

            if is_group:
                all_sel = all(m.selected for m in visible)
                any_sel = any(m.selected for m in visible)
                chk = (
                    CHECKBOX_ON if all_sel
                    else CHECKBOX_PARTIAL if any_sel
                    else CHECKBOX_OFF
                )
                total_sigs = sum(len(m.signals) for m in visible)
                g_iid = f"group::{group_name}"
                self._tree.insert(
                    "", END, iid=g_iid,
                    text=f"{chk}  {group_name}  ({len(visible)} messages)",
                    values=("", "", total_sigs, ""),
                    open=False,
                    tags=("group",),
                )
                self._tree_items[g_iid] = ("group", group_name)
                for msg in visible:
                    self._insert_message(g_iid, msg)
            else:
                self._insert_message("", msgs[0])

        self._tree.tag_configure("group",  font=("", 9, "bold"))
        self._tree.tag_configure("signal", foreground="#888888")

    def _insert_message(self, parent_iid: str, msg: Message) -> None:
        chk = CHECKBOX_ON if msg.selected else CHECKBOX_OFF
        m_iid = f"msg::{msg.msg_id}"
        self._tree.insert(
            parent_iid, END, iid=m_iid,
            text=f"{chk}  {msg.name}",
            values=(msg.msg_id, msg.dlc, len(msg.signals), msg.transmitter),
            tags=("msg",),
        )
        self._tree_items[m_iid] = ("msg", msg)

        for sig in msg.signals:
            s_iid = f"sig::{msg.msg_id}::{sig.name}"
            bo = "LE" if sig.byte_order == "little_endian" else "BE"
            vt = "S" if sig.value_type == "signed" else "U"
            self._tree.insert(
                m_iid, END, iid=s_iid,
                text=f"    {sig.name}",
                values=(f"{sig.start_bit}|{sig.bit_length}", bo, vt, ""),
                tags=("signal",),
            )

    # ── Tree interaction ──────────────────────────────────────────────────────

    def _on_tree_click(self, event: "tk.Event[ttk.Treeview]") -> None:
        """Toggle message/group selection when the user clicks the name column."""
        region = self._tree.identify_region(event.x, event.y)
        if region not in ("tree", "cell"):
            return

        iid = self._tree.identify_row(event.y)
        if not iid or iid not in self._tree_items:
            return

        # Only react to clicks in the tree/name column (#0)
        if self._tree.identify_column(event.x) != "#0":
            return

        kind, data = self._tree_items[iid]
        if kind == "group":
            self._toggle_group(str(data))
        elif kind == "msg":
            self._toggle_message(data)  # type: ignore[arg-type]

        self._update_summary()

    def _toggle_message(self, msg: Message) -> None:
        msg.selected = not msg.selected
        chk = CHECKBOX_ON if msg.selected else CHECKBOX_OFF
        m_iid = f"msg::{msg.msg_id}"
        if self._tree.exists(m_iid):
            self._tree.item(m_iid, text=f"{chk}  {msg.name}")
        self._refresh_parent_group(msg)

    def _toggle_group(self, group_name: str) -> None:
        msgs = self._groups[group_name]
        new_sel = not all(m.selected for m in msgs)
        for msg in msgs:
            msg.selected = new_sel
            chk = CHECKBOX_ON if new_sel else CHECKBOX_OFF
            m_iid = f"msg::{msg.msg_id}"
            if self._tree.exists(m_iid):
                self._tree.item(m_iid, text=f"{chk}  {msg.name}")
        self._refresh_group_item(group_name)

    def _refresh_group_item(self, group_name: str) -> None:
        g_iid = f"group::{group_name}"
        if not self._tree.exists(g_iid):
            return
        msgs = self._groups[group_name]
        all_sel = all(m.selected for m in msgs)
        any_sel = any(m.selected for m in msgs)
        chk = (
            CHECKBOX_ON if all_sel
            else CHECKBOX_PARTIAL if any_sel
            else CHECKBOX_OFF
        )
        n = len(msgs)
        self._tree.item(g_iid, text=f"{chk}  {group_name}  ({n} messages)")

    def _refresh_parent_group(self, msg: Message) -> None:
        for group_name, msgs in self._groups.items():
            if msg in msgs and len(msgs) > 1:
                self._refresh_group_item(group_name)
                break

    # ── Grouping ──────────────────────────────────────────────────────────────

    def _auto_group(self) -> None:
        if not self._messages:
            return
        self._groups = detect_groups(self._messages)
        self._populate_tree()
        n_groups = sum(1 for v in self._groups.values() if len(v) > 1)
        self._log(
            f"Auto-grouping complete: {n_groups} group(s) detected.", tag="info"
        )

    def _ungroup(self) -> None:
        if not self._messages:
            return
        self._groups = {msg.name: [msg] for msg in self._messages}
        self._populate_tree()

    # ── Selection helpers ─────────────────────────────────────────────────────

    def _select_all(self) -> None:
        for msg in self._messages:
            msg.selected = True
        self._populate_tree()
        self._update_summary()

    def _deselect_all(self) -> None:
        for msg in self._messages:
            msg.selected = False
        self._populate_tree()
        self._update_summary()

    def _apply_filter(self) -> None:
        self._populate_tree()

    def _update_summary(self) -> None:
        total = len(self._messages)
        sel = sum(1 for m in self._messages if m.selected)
        self._summary_var.set(f"{total} messages  |  {sel} selected")
        self._sel_summary.set(f"{sel} / {total} messages selected for generation")

    # ── Code generation ───────────────────────────────────────────────────────

    def _generate(self) -> None:
        if not self._messages:
            messagebox.showwarning("No DBC", "Please load a DBC file first.")
            return

        selected = [m for m in self._messages if m.selected]
        if not selected:
            messagebox.showwarning(
                "Nothing Selected",
                "Select at least one message to generate code for.",
            )
            return

        binary = self._binary_path.get().strip()
        if not binary or not Path(binary).exists():
            messagebox.showerror(
                "Binary Not Found",
                f"Cannot find the coderdbc binary:\n{binary or '(not set)'}\n\n"
                "Build the project first:\n"
                "  cmake -S src -B build\n"
                "  cmake --build build\n\n"
                "Then update the 'coderdbc binary' path in Settings.",
            )
            return

        out_dir = self._out_path.get().strip()
        drv_name = self._drv_name.get().strip()
        dbc_path = self._dbc_path.get().strip()

        if not out_dir:
            messagebox.showwarning("Missing", "Please specify an output directory.")
            return
        if not drv_name:
            messagebox.showwarning("Missing", "Please specify a driver name.")
            return

        self._gen_btn.configure(state="disabled")
        self._progress.start(10)
        threading.Thread(
            target=self._run_generation,
            args=(selected, dbc_path, out_dir, drv_name, binary),
            daemon=True,
        ).start()

    def _run_generation(
        self,
        selected: List[Message],
        dbc_path: str,
        out_dir: str,
        drv_name: str,
        binary: str,
    ) -> None:
        tmp_path: Optional[str] = None
        try:
            selected_ids = {m.msg_id for m in selected}
            all_ids = {m.msg_id for m in self._messages}

            if selected_ids == all_ids:
                work_dbc = dbc_path
                self._log(
                    f"All {len(selected)} messages selected — using original DBC.",
                    tag="info",
                )
            else:
                fd, tmp_path = tempfile.mkstemp(suffix=".dbc")
                os.close(fd)
                self._parser.write_filtered(dbc_path, selected_ids, tmp_path)
                work_dbc = tmp_path
                self._log(
                    f"Filtered DBC created "
                    f"({len(selected)}/{len(self._messages)} messages).",
                    tag="info",
                )

            cmd: List[str] = [
                binary, "-dbc", work_dbc, "-out", out_dir, "-drvname", drv_name
            ]
            if self._opt_rw.get():        cmd.append("-rw")
            if self._opt_nodeutils.get(): cmd.append("-nodeutils")
            if self._opt_driverdir.get(): cmd.append("-driverdir")
            if self._opt_gendate.get():   cmd.append("-gendate")
            if self._opt_noconfig.get():  cmd.append("-noconfig")
            if self._opt_noinc.get():     cmd.append("-noinc")
            if self._opt_nofmon.get():    cmd.append("-nofmon")

            self._log("Running: " + " ".join(cmd))

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120
            )

            if result.stdout.strip():
                self._log(result.stdout.strip())
            if result.stderr.strip():
                self._log(result.stderr.strip(), tag="warning")

            if result.returncode == 0:
                self._log(
                    f"✅  Generation complete!  Output → {out_dir}", tag="success"
                )
            else:
                self._log(
                    f"❌  coderdbc exited with code {result.returncode}", tag="danger"
                )

        except subprocess.TimeoutExpired:
            self._log("❌  Code generation timed out (> 120 s).", tag="danger")
        except Exception as exc:
            self._log(f"❌  Unexpected error: {exc}", tag="danger")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            self.after(0, self._generation_done)

    def _generation_done(self) -> None:
        self._progress.stop()
        self._gen_btn.configure(state="normal")

    # ── File dialogs ──────────────────────────────────────────────────────────

    def _browse_out_dir(self) -> None:
        path = filedialog.askdirectory(title="Select output directory")
        if path:
            self._out_path.set(path)

    def _browse_binary(self) -> None:
        path = filedialog.askopenfilename(
            title="Select coderdbc binary",
            filetypes=[("All files", "*")],
        )
        if path:
            self._binary_path.set(path)

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log(self, message: str, tag: str = "") -> None:
        """Append *message* to the log pane (thread-safe)."""
        def _append() -> None:
            self._log_text.configure(state="normal")
            self._log_text.insert(END, message + "\n", (tag,) if tag else ())
            self._log_text.configure(state="disabled")
            self._log_text.see(END)

        self.after(0, _append)

    def _clear_log(self) -> None:
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", END)
        self._log_text.configure(state="disabled")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    app = CoderDbcGui()
    app.mainloop()


if __name__ == "__main__":
    main()
