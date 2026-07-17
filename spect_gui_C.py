#!/usr/bin/env python3
"""


spectA variant C - spectrum analyser with yfinance stock data
=============================================================

Same two-channel spectrum analyser as spect_gui.py, plus a
"Stocks (yfinance)" tab that reuses the retrieval code from
stkhist_gui_C.py (StockDB, download_history, read_symbols_csv) to fetch
and store stock data files, sharing the same stkhist.db database as the
stkhist tools.  Stored symbols can be selected and loaded into either
channel (open / current / close / % change / volume), with the real
date timebase attached, ready for charting, FFTs, correlation, math and
saving to wlib.

-----prev--Run with:  python3 spectA/spect_gui_C.py
now just python3 spect_gui_C.py

"""

import os
import sys
from datetime import datetime

import numpy as np

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spect_gui import SpectApp, Waveform, fmt_num

# Retrieval code shared with the stock history manager (variant C).
from stkhist_gui_C import (  # noqa: E402
    DB_FILE as STOCK_DB_FILE,
    StockDB,
    download_history,
    read_symbols_csv,
)

PERIODS = ("5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "max")

# Column index of each loadable field in a stock_history row
# (symbol, date, open, current, close, pct_change, volume).
FIELD_INDEX = {
    "open": 2,
    "current": 3,
    "close": 4,
    "pct_change": 5,
    "volume": 6,
}


def rows_to_waveform(symbol, rows, field):
    """Convert stock_history rows (any order) into a dated Waveform.

    Rows with a NULL value for the chosen field are skipped.  The sample
    interval is the median spacing of the remaining dates (daily rows
    give 86400 s across weekday runs; weekend gaps do not skew it).
    """
    idx = FIELD_INDEX[field]
    dated = sorted(
        (datetime.strptime(r[1], "%Y-%m-%d"), float(r[idx]))
        for r in rows
        if r[idx] is not None
    )
    if not dated:
        raise ValueError("No stored %s values for %s." % (field, symbol))
    dates = [d for d, _v in dated]
    values = [v for _d, v in dated]
    if len(dates) > 1:
        deltas = [(dates[i + 1] - dates[i]).total_seconds()
                  for i in range(len(dates) - 1)]
        dt = float(np.median(deltas)) or 86400.0
    else:
        dt = 86400.0
    return Waveform(values, dt, name="%s %s" % (symbol, field), t0=dates[0],
                    params="yfinance %s of %s (%d rows from %s)"
                           % (field, symbol, len(values),
                              os.path.basename(STOCK_DB_FILE)))


class SpectAppC(SpectApp):
    """SpectApp plus a stock-data tab backed by stkhist.db / yfinance."""

    def __init__(self):
        super().__init__()
        self.title("spectA C - 2-channel spectrum analyser + yfinance stocks")
        self.stock_db = StockDB()
        self._build_stock_tab(self.notebook)
        self.refresh_stock_table()

    # -- stock tab ----------------------------------------------------------

    def _build_stock_tab(self, notebook):
        tab = ttk.Frame(notebook)
        notebook.add(tab, text="Stocks (yfinance)")

        cols = ("symbol", "latest", "open", "current", "close",
                "pct_change", "records")
        headings = ("Symbol", "Latest Date", "Open", "Current", "Close",
                    "% Change", "Records")
        self.stock_tree = ttk.Treeview(tab, columns=cols, show="headings",
                                       height=5)
        for col, head in zip(cols, headings):
            self.stock_tree.heading(col, text=head)
            anchor = tk.W if col in ("symbol", "latest") else tk.E
            self.stock_tree.column(col, width=90, anchor=anchor,
                                   stretch=(col == "symbol"))
        self.stock_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                             padx=4, pady=4)

        sb = ttk.Scrollbar(tab, orient=tk.VERTICAL,
                           command=self.stock_tree.yview)
        self.stock_tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.LEFT, fill=tk.Y, pady=4)

        btns = ttk.Frame(tab)
        btns.pack(side=tk.LEFT, fill=tk.Y, padx=4, pady=4)

        prow = ttk.Frame(btns)
        prow.grid(row=0, column=0, columnspan=2, sticky=tk.W)
        ttk.Label(prow, text="Period:").pack(side=tk.LEFT)
        self.stock_period = tk.StringVar(value="1mo")
        ttk.Combobox(prow, textvariable=self.stock_period, state="readonly",
                     width=5, values=PERIODS).pack(side=tk.LEFT, padx=2)
        ttk.Label(prow, text="Field:").pack(side=tk.LEFT, padx=(8, 0))
        self.stock_field = tk.StringVar(value="close")
        ttk.Combobox(prow, textvariable=self.stock_field, state="readonly",
                     width=9, values=tuple(FIELD_INDEX)).pack(side=tk.LEFT,
                                                              padx=2)

        actions = (
            ("Fetch symbol...", self.fetch_symbol_dialog),
            ("Import CSV & fetch", self.import_symbols_csv),
            ("Load -> CH1", lambda: self.load_stock("CH1")),
            ("Load -> CH2", lambda: self.load_stock("CH2")),
            ("Refresh table", self.refresh_stock_table),
            ("Delete symbol", self.delete_stock_symbol),
        )
        for i, (label, cmd) in enumerate(actions):
            ttk.Button(btns, text=label, width=17, command=cmd).grid(
                row=1 + i % 3, column=i // 3, padx=2, pady=1, sticky=tk.EW)

    # -- retrieval (same flow as stkhist_gui_C.App.fetch_symbols) ------------

    def fetch_symbols(self, symbols, period=None):
        period = period or self.stock_period.get()
        errors, total_rows = [], 0
        for i, sym in enumerate(symbols, start=1):
            self.set_status("[%d/%d] Fetching %s (%s)..."
                            % (i, len(symbols), sym, period))
            self.update_idletasks()
            try:
                rows = download_history(sym, period=period)
                if rows:
                    self.stock_db.upsert(rows)
                    total_rows += len(rows)
                else:
                    errors.append("%s: no data returned" % sym)
            except Exception as exc:  # noqa: BLE001 - report to user
                errors.append("%s: %s" % (sym, exc))
        self.refresh_stock_table()
        msg = ("Stored/updated %d rows for %d symbol(s)."
               % (total_rows, len(symbols)))
        if errors:
            msg += "\n\nProblems:\n" + "\n".join(errors[:15])
            messagebox.showwarning("Fetch finished with problems", msg,
                                   parent=self)
        else:
            messagebox.showinfo("Fetch complete", msg, parent=self)
        self.set_status(msg.splitlines()[0])

    def fetch_symbol_dialog(self):
        symbol = simpledialog.askstring(
            "Fetch symbol", "Ticker symbol to fetch (e.g. AAPL):",
            parent=self)
        if symbol and symbol.strip():
            self.fetch_symbols([symbol.strip().upper()])

    def import_symbols_csv(self):
        path = filedialog.askopenfilename(
            parent=self, title="Choose CSV file of stock symbols",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        try:
            symbols = read_symbols_csv(path)
        except OSError as exc:
            return self._report(exc)
        if not symbols:
            messagebox.showwarning("Empty file",
                                   "No symbols found in the CSV file.",
                                   parent=self)
            return
        self.fetch_symbols(symbols)

    # -- table / channel loading ---------------------------------------------

    def refresh_stock_table(self):
        self.stock_tree.delete(*self.stock_tree.get_children())
        for row in self.stock_db.fetch_summary():
            symbol, latest, open_, current, close, pct, _vol, n = row
            self.stock_tree.insert(
                "", tk.END, iid=symbol,
                values=(symbol, latest,
                        "" if open_ is None else fmt_num(open_),
                        "" if current is None else fmt_num(current),
                        "" if close is None else fmt_num(close),
                        "" if pct is None else fmt_num(pct),
                        n))

    def _selected_symbol(self):
        sel = self.stock_tree.selection()
        if not sel:
            raise ValueError("Select a symbol in the stock table first.")
        return sel[0]

    def load_stock(self, target):
        try:
            symbol = self._selected_symbol()
            field = self.stock_field.get()
            wf = rows_to_waveform(symbol, self.stock_db.fetch(symbol), field)
        except Exception as exc:
            return self._report(exc)
        self.channel(target).set_waveform(wf)
        self.set_status("Loaded %d %s values of %s into %s."
                        % (wf.n, field, symbol, target))

    def delete_stock_symbol(self):
        try:
            symbol = self._selected_symbol()
        except ValueError as exc:
            return self._report(exc)
        if not messagebox.askyesno(
                "Delete symbol",
                "Really delete every stored record for %s?" % symbol,
                parent=self):
            return
        self.stock_db.delete_symbol(symbol)
        self.refresh_stock_table()
        self.set_status("Deleted all records for %s." % symbol)


def main():
    SpectAppC().mainloop()


if __name__ == "__main__":
    main()