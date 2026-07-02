#!/usr/bin/env python3
"""
Stock History Manager — symbol summary view
===========================================

Variant of stkhist_gui.py with a master–detail layout:

  * The top table shows ONE row per stock symbol (AA, BB, CC, ...) with the
    most recent stored values and a count of records.
  * Clicking a symbol in the top table fills the bottom table with all of
    that symbol's dated history rows.
  * Add / update / delete of individual records works on the bottom
    (detail) table; everything else (CSV import, yfinance fetch, charts)
    is unchanged from stkhist_gui.py.

Run with:  python3 stkhist_gui_A.py
"""

import csv
import os
import sqlite3
from datetime import date, datetime, timedelta

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import matplotlib

matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

try:
    import yfinance as yf
except ImportError:  # allow the GUI to open even if yfinance is missing
    yf = None

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stkhist.db")

COLUMNS = ("symbol", "date", "open", "current", "close", "pct_change", "volume")
COLUMN_HEADINGS = {
    "symbol": "Symbol",
    "date": "Date",
    "open": "Open",
    "current": "Current",
    "close": "Close",
    "pct_change": "% Change",
    "volume": "Volume",
}

SUMMARY_COLUMNS = COLUMNS + ("n_records",)
SUMMARY_HEADINGS = dict(COLUMN_HEADINGS, date="Latest Date", n_records="Records")


# --------------------------------------------------------------------------- #
# Database layer
# --------------------------------------------------------------------------- #
class StockDB:
    """Thin wrapper around the stkhist.db SQLite database."""

    def __init__(self, path=DB_FILE):
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_history (
                symbol      TEXT    NOT NULL,
                date        TEXT    NOT NULL,          -- ISO yyyy-mm-dd
                open        REAL,
                current     REAL,
                close       REAL,
                pct_change  REAL,                      -- % change close vs open
                volume      INTEGER,
                PRIMARY KEY (symbol, date)
            )
            """
        )
        self.conn.commit()

    def upsert(self, rows):
        """Insert rows, replacing any existing (symbol, date) records."""
        self.conn.executemany(
            """
            INSERT INTO stock_history
                   (symbol, date, open, current, close, pct_change, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, date) DO UPDATE SET
                open = excluded.open,
                current = excluded.current,
                close = excluded.close,
                pct_change = excluded.pct_change,
                volume = excluded.volume
            """,
            rows,
        )
        self.conn.commit()

    def update(self, symbol, day, open_, current, close, pct_change, volume):
        self.conn.execute(
            """
            UPDATE stock_history
               SET open = ?, current = ?, close = ?, pct_change = ?, volume = ?
             WHERE symbol = ? AND date = ?
            """,
            (open_, current, close, pct_change, volume, symbol, day),
        )
        self.conn.commit()

    def delete(self, symbol, day):
        self.conn.execute(
            "DELETE FROM stock_history WHERE symbol = ? AND date = ?",
            (symbol, day),
        )
        self.conn.commit()

    def delete_symbol(self, symbol):
        self.conn.execute("DELETE FROM stock_history WHERE symbol = ?", (symbol,))
        self.conn.commit()

    def fetch(self, symbol=None):
        if symbol:
            cur = self.conn.execute(
                "SELECT * FROM stock_history WHERE symbol = ? "
                "ORDER BY symbol, date DESC",
                (symbol,),
            )
        else:
            cur = self.conn.execute(
                "SELECT * FROM stock_history ORDER BY symbol, date DESC"
            )
        return cur.fetchall()

    def fetch_summary(self, symbol=None):
        """One row per symbol: values from its latest date plus a record count.

        SQLite's "bare columns in an aggregate query" rule guarantees the
        non-aggregated columns come from the row that supplied MAX(date).
        """
        sql = """
            SELECT symbol, MAX(date) AS date, open, current, close,
                   pct_change, volume, COUNT(*) AS n_records
              FROM stock_history
        """
        params = ()
        if symbol:
            sql += " WHERE symbol = ?"
            params = (symbol,)
        sql += " GROUP BY symbol ORDER BY symbol"
        cur = self.conn.execute(sql, params)
        return cur.fetchall()

    def fetch_range(self, symbol, start, end):
        cur = self.conn.execute(
            """
            SELECT date, open, current, close, pct_change, volume
              FROM stock_history
             WHERE symbol = ? AND date BETWEEN ? AND ?
             ORDER BY date
            """,
            (symbol, start, end),
        )
        return cur.fetchall()

    def symbols(self):
        cur = self.conn.execute(
            "SELECT DISTINCT symbol FROM stock_history ORDER BY symbol"
        )
        return [r[0] for r in cur.fetchall()]

    def close(self):
        self.conn.close()


# --------------------------------------------------------------------------- #
# yfinance download helpers
# --------------------------------------------------------------------------- #
def read_symbols_csv(path):
    """Read a CSV file and return a list of upper-cased ticker symbols.

    Symbols may appear one per line and/or comma separated. A header cell
    containing the word 'symbol' is skipped.
    """
    symbols = []
    with open(path, newline="") as fh:
        for row in csv.reader(fh):
            for cell in row:
                cell = cell.strip().upper()
                if not cell or cell in ("SYMBOL", "TICKER"):
                    continue
                symbols.append(cell)
    # de-duplicate while preserving order
    return list(dict.fromkeys(symbols))


def download_history(symbol, period="1mo"):
    """Download OHLCV history for one symbol; return rows for StockDB.upsert."""
    if yf is None:
        raise RuntimeError(
            "The 'yfinance' package is not installed.\n"
            "Install it with:  pip install yfinance"
        )
    ticker = yf.Ticker(symbol)
    hist = ticker.history(period=period, auto_adjust=False)
    if hist.empty:
        return []

    # "Current" price: the most recent traded price known to Yahoo. For
    # historical rows the close of the day is used.
    try:
        latest_price = ticker.fast_info["last_price"]
    except Exception:
        latest_price = None

    rows = []
    last_index = hist.index[-1]
    for idx, r in hist.iterrows():
        open_, close = float(r["Open"]), float(r["Close"])
        current = close
        if idx == last_index and latest_price is not None:
            current = float(latest_price)
        pct = round((close - open_) / open_ * 100.0, 4) if open_ else None
        rows.append(
            (
                symbol,
                idx.strftime("%Y-%m-%d"),
                round(open_, 4),
                round(current, 4),
                round(close, 4),
                pct,
                int(r["Volume"]),
            )
        )
    return rows


# --------------------------------------------------------------------------- #
# Record add / edit dialog
# --------------------------------------------------------------------------- #
class RecordDialog(tk.Toplevel):
    """Modal dialog used both for adding and editing a record."""

    FIELDS = [
        ("Symbol", "symbol"),
        ("Date (YYYY-MM-DD)", "date"),
        ("Open", "open"),
        ("Current", "current"),
        ("Close", "close"),
        ("% Change", "pct_change"),
        ("Volume", "volume"),
    ]

    def __init__(self, parent, title, initial=None, lock_key=False):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.result = None
        self.entries = {}

        body = ttk.Frame(self, padding=12)
        body.grid(sticky="nsew")

        for i, (label, key) in enumerate(self.FIELDS):
            ttk.Label(body, text=label + ":").grid(
                row=i, column=0, sticky="e", padx=(0, 8), pady=3
            )
            entry = ttk.Entry(body, width=22)
            entry.grid(row=i, column=1, pady=3)
            if initial is not None:
                value = initial.get(key, "")
                entry.insert(0, "" if value is None else str(value))
            if lock_key and key in ("symbol", "date"):
                entry.configure(state="disabled")
            self.entries[key] = entry

        btns = ttk.Frame(body)
        btns.grid(row=len(self.FIELDS), column=0, columnspan=2, pady=(12, 0))
        ttk.Button(btns, text="OK", command=self._on_ok).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left", padx=4)

        self.bind("<Return>", lambda _e: self._on_ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.transient(parent)
        self.grab_set()
        self.entries["symbol"].focus_set()
        self.wait_window(self)

    def _on_ok(self):
        values = {}
        try:
            values["symbol"] = self.entries["symbol"].get().strip().upper()
            values["date"] = self.entries["date"].get().strip()
            if not values["symbol"]:
                raise ValueError("Symbol is required.")
            datetime.strptime(values["date"], "%Y-%m-%d")  # validate

            def num(key, cast):
                text = self.entries[key].get().strip()
                return cast(text) if text else None

            values["open"] = num("open", float)
            values["current"] = num("current", float)
            values["close"] = num("close", float)
            values["pct_change"] = num("pct_change", float)
            values["volume"] = num("volume", int)

            # auto-compute % change when possible and not supplied
            if (
                values["pct_change"] is None
                and values["open"]
                and values["close"] is not None
            ):
                values["pct_change"] = round(
                    (values["close"] - values["open"]) / values["open"] * 100.0, 4
                )
        except ValueError as exc:
            messagebox.showerror("Invalid input", str(exc), parent=self)
            return
        self.result = values
        self.destroy()


# --------------------------------------------------------------------------- #
# Chart window
# --------------------------------------------------------------------------- #
class ChartWindow(tk.Toplevel):
    def __init__(self, parent, symbol, rows, start, end):
        super().__init__(parent)
        self.title(f"{symbol}  —  {start} to {end}")
        self.geometry("860x560")

        dates = [datetime.strptime(r[0], "%Y-%m-%d").date() for r in rows]
        opens = [r[1] for r in rows]
        closes = [r[3] for r in rows]
        volumes = [r[5] or 0 for r in rows]

        fig = Figure(figsize=(8.4, 5.2), dpi=100)
        ax_price = fig.add_subplot(211)
        ax_price.plot(dates, closes, label="Close", color="#1f77b4", marker="o", ms=3)
        ax_price.plot(
            dates, opens, label="Open", color="#ff7f0e", marker="o", ms=3, alpha=0.7
        )
        ax_price.set_ylabel("Price")
        ax_price.set_title(f"{symbol} price history")
        ax_price.legend(loc="best")
        ax_price.grid(True, alpha=0.3)

        ax_vol = fig.add_subplot(212, sharex=ax_price)
        ax_vol.bar(dates, volumes, color="#2ca02c", alpha=0.6)
        ax_vol.set_ylabel("Volume")
        ax_vol.grid(True, alpha=0.3)
        fig.autofmt_xdate()
        fig.tight_layout()

        canvas = FigureCanvasTkAgg(fig, master=self)
        canvas.draw()
        canvas.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(canvas, self)


class ChartDialog(tk.Toplevel):
    """Asks for symbol + date range, then opens a ChartWindow."""

    def __init__(self, parent, db, preselect=None):
        super().__init__(parent)
        self.db = db
        self.title("Display price chart")
        self.resizable(False, False)

        body = ttk.Frame(self, padding=12)
        body.grid(sticky="nsew")

        ttk.Label(body, text="Symbol:").grid(row=0, column=0, sticky="e", pady=3)
        self.symbol_var = tk.StringVar()
        symbols = db.symbols()
        self.symbol_box = ttk.Combobox(
            body, textvariable=self.symbol_var, values=symbols, width=18
        )
        self.symbol_box.grid(row=0, column=1, pady=3)
        if preselect and preselect in symbols:
            self.symbol_box.current(symbols.index(preselect))
        elif symbols:
            self.symbol_box.current(0)

        today = date.today()
        ttk.Label(body, text="From (YYYY-MM-DD):").grid(
            row=1, column=0, sticky="e", pady=3, padx=(0, 8)
        )
        self.start_entry = ttk.Entry(body, width=20)
        self.start_entry.insert(0, (today - timedelta(days=30)).isoformat())
        self.start_entry.grid(row=1, column=1, pady=3)

        ttk.Label(body, text="To (YYYY-MM-DD):").grid(
            row=2, column=0, sticky="e", pady=3, padx=(0, 8)
        )
        self.end_entry = ttk.Entry(body, width=20)
        self.end_entry.insert(0, today.isoformat())
        self.end_entry.grid(row=2, column=1, pady=3)

        btns = ttk.Frame(body)
        btns.grid(row=3, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(btns, text="Show chart", command=self._show).pack(
            side="left", padx=4
        )
        ttk.Button(btns, text="Close", command=self.destroy).pack(side="left", padx=4)

        self.transient(parent)
        self.grab_set()

    def _show(self):
        symbol = self.symbol_var.get().strip().upper()
        start = self.start_entry.get().strip()
        end = self.end_entry.get().strip()
        try:
            datetime.strptime(start, "%Y-%m-%d")
            datetime.strptime(end, "%Y-%m-%d")
        except ValueError:
            messagebox.showerror(
                "Invalid date", "Dates must be in YYYY-MM-DD format.", parent=self
            )
            return
        if not symbol:
            messagebox.showerror("No symbol", "Please choose a symbol.", parent=self)
            return
        rows = self.db.fetch_range(symbol, start, end)
        if not rows:
            messagebox.showinfo(
                "No data",
                f"No stored data for {symbol} between {start} and {end}.\n"
                "Import a CSV or fetch data first.",
                parent=self,
            )
            return
        ChartWindow(self, symbol, rows, start, end)


# --------------------------------------------------------------------------- #
# Main application window
# --------------------------------------------------------------------------- #
class App(tk.Tk):
    PERIODS = ("5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "max")

    def __init__(self):
        super().__init__()
        self.title("Stock History Manager (summary view)  —  stkhist.db")
        self.geometry("1020x680")
        self.db = StockDB()

        self._build_menu()
        self._build_toolbar()
        self._build_tables()
        self._build_statusbar()
        self.refresh_table()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI
    def _build_menu(self):
        menubar = tk.Menu(self)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(
            label="Import CSV && fetch data…", command=self.import_csv
        )
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        edit_menu = tk.Menu(menubar, tearoff=0)
        edit_menu.add_command(label="Add record…", command=self.add_record)
        edit_menu.add_command(label="Update selected…", command=self.update_record)
        edit_menu.add_command(label="Delete selected", command=self.delete_records)
        edit_menu.add_separator()
        edit_menu.add_command(
            label="Delete ALL rows for a symbol…", command=self.delete_symbol
        )
        edit_menu.add_separator()
        edit_menu.add_command(
            label="Refresh data for a symbol…", command=self.refetch_symbol
        )
        menubar.add_cascade(label="Edit", menu=edit_menu)

        view_menu = tk.Menu(menubar, tearoff=0)
        view_menu.add_command(label="Price chart…", command=self.show_chart)
        view_menu.add_command(label="Reload table", command=self.refresh_table)
        menubar.add_cascade(label="View", menu=view_menu)

        self.config(menu=menubar)

    def _build_toolbar(self):
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill="x")

        ttk.Button(bar, text="Import CSV & Fetch", command=self.import_csv).pack(
            side="left", padx=(0, 4)
        )
        ttk.Button(bar, text="Add", command=self.add_record).pack(side="left", padx=4)
        ttk.Button(bar, text="Update", command=self.update_record).pack(
            side="left", padx=4
        )
        ttk.Button(bar, text="Delete", command=self.delete_records).pack(
            side="left", padx=4
        )
        ttk.Button(bar, text="Price Chart", command=self.show_chart).pack(
            side="left", padx=4
        )

        ttk.Label(bar, text="History period:").pack(side="left", padx=(16, 4))
        self.period_var = tk.StringVar(value="1mo")
        ttk.Combobox(
            bar,
            textvariable=self.period_var,
            values=self.PERIODS,
            width=5,
            state="readonly",
        ).pack(side="left")

        ttk.Label(bar, text="Filter symbol:").pack(side="left", padx=(16, 4))
        self.filter_var = tk.StringVar()
        filter_entry = ttk.Entry(bar, textvariable=self.filter_var, width=10)
        filter_entry.pack(side="left")
        filter_entry.bind("<Return>", lambda _e: self.refresh_table())
        ttk.Button(bar, text="Apply", command=self.refresh_table).pack(
            side="left", padx=4
        )
        ttk.Button(
            bar,
            text="Clear",
            command=lambda: (self.filter_var.set(""), self.refresh_table()),
        ).pack(side="left")

    def _build_tables(self):
        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=8)

        # ------------------------------- summary: one row per symbol
        top = ttk.Frame(paned)
        ttk.Label(top, text="Symbols (latest values)", anchor="w").pack(fill="x")

        summary_wrap = ttk.Frame(top)
        summary_wrap.pack(fill="both", expand=True)
        self.summary_tree = ttk.Treeview(
            summary_wrap, columns=SUMMARY_COLUMNS, show="headings", height=8
        )
        for col in SUMMARY_COLUMNS:
            self.summary_tree.heading(col, text=SUMMARY_HEADINGS[col])
            width = 90 if col in ("symbol", "date") else 100
            anchor = "w" if col in ("symbol", "date") else "e"
            self.summary_tree.column(col, width=width, anchor=anchor)

        s_vsb = ttk.Scrollbar(
            summary_wrap, orient="vertical", command=self.summary_tree.yview
        )
        self.summary_tree.configure(yscrollcommand=s_vsb.set)
        self.summary_tree.pack(side="left", fill="both", expand=True)
        s_vsb.pack(side="right", fill="y")

        self.summary_tree.bind("<<TreeviewSelect>>", self._on_symbol_selected)
        self.summary_tree.bind("<Double-1>", lambda _e: self.show_chart())

        # ------------------------------- detail: all dates for one symbol
        bottom = ttk.Frame(paned)
        self.detail_label_var = tk.StringVar(
            value="History  (click a symbol above)"
        )
        ttk.Label(bottom, textvariable=self.detail_label_var, anchor="w").pack(
            fill="x"
        )

        detail_wrap = ttk.Frame(bottom)
        detail_wrap.pack(fill="both", expand=True)
        self.detail_tree = ttk.Treeview(
            detail_wrap, columns=COLUMNS, show="headings"
        )
        for col in COLUMNS:
            self.detail_tree.heading(col, text=COLUMN_HEADINGS[col])
            width = 90 if col in ("symbol", "date") else 110
            anchor = "w" if col in ("symbol", "date") else "e"
            self.detail_tree.column(col, width=width, anchor=anchor)

        d_vsb = ttk.Scrollbar(
            detail_wrap, orient="vertical", command=self.detail_tree.yview
        )
        self.detail_tree.configure(yscrollcommand=d_vsb.set)
        self.detail_tree.pack(side="left", fill="both", expand=True)
        d_vsb.pack(side="right", fill="y")

        self.detail_tree.bind("<Double-1>", lambda _e: self.update_record())

        paned.add(top, weight=1)
        paned.add(bottom, weight=2)

    def _build_statusbar(self):
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(
            self, textvariable=self.status_var, anchor="w", padding=(8, 4)
        ).pack(fill="x", side="bottom")

    def set_status(self, text):
        self.status_var.set(text)
        self.update_idletasks()

    # ------------------------------------------------------------ table ops
    def selected_symbol(self):
        """Symbol currently selected in the summary table, or None."""
        sel = self.summary_tree.selection()
        if not sel:
            return None
        return self.summary_tree.item(sel[0], "values")[0]

    def refresh_table(self):
        """Refill the summary table (and re-fill detail if still relevant)."""
        filter_symbol = self.filter_var.get().strip().upper() or None
        previous = self.selected_symbol()

        self.summary_tree.delete(*self.summary_tree.get_children())
        rows = self.db.fetch_summary(filter_symbol)
        for row in rows:
            display = tuple("" if v is None else v for v in row)
            self.summary_tree.insert("", "end", values=display)

        # keep the previously selected symbol's detail visible if possible
        symbols_shown = [r[0] for r in rows]
        if previous in symbols_shown:
            for item in self.summary_tree.get_children():
                if self.summary_tree.item(item, "values")[0] == previous:
                    self.summary_tree.selection_set(item)
                    break
        else:
            self._clear_detail()

        self.set_status(
            f"{len(rows)} symbol(s) shown"
            + (f" (filter: {filter_symbol})" if filter_symbol else "")
            + f"  |  database: {DB_FILE}"
        )

    def _clear_detail(self):
        self.detail_tree.delete(*self.detail_tree.get_children())
        self.detail_label_var.set("History  (click a symbol above)")

    def _on_symbol_selected(self, _event=None):
        symbol = self.selected_symbol()
        if symbol is None:
            self._clear_detail()
            return
        self.detail_tree.delete(*self.detail_tree.get_children())
        rows = self.db.fetch(symbol)
        for row in rows:
            display = tuple("" if v is None else v for v in row)
            self.detail_tree.insert("", "end", values=display)
        self.detail_label_var.set(f"History for {symbol}  ({len(rows)} record(s))")

    def _selected_keys(self):
        """(symbol, date) pairs selected in the DETAIL table."""
        keys = []
        for item in self.detail_tree.selection():
            values = self.detail_tree.item(item, "values")
            keys.append((values[0], values[1]))
        return keys

    # ------------------------------------------------------------- actions
    def import_csv(self):
        path = filedialog.askopenfilename(
            title="Choose CSV file of stock symbols",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            symbols = read_symbols_csv(path)
        except OSError as exc:
            messagebox.showerror("Cannot read file", str(exc))
            return
        if not symbols:
            messagebox.showwarning("Empty file", "No symbols found in the CSV file.")
            return
        self.fetch_symbols(symbols)

    def fetch_symbols(self, symbols):
        period = self.period_var.get()
        errors, total_rows = [], 0
        for i, sym in enumerate(symbols, start=1):
            self.set_status(f"[{i}/{len(symbols)}] Fetching {sym} ({period})…")
            try:
                rows = download_history(sym, period=period)
                if rows:
                    self.db.upsert(rows)
                    total_rows += len(rows)
                else:
                    errors.append(f"{sym}: no data returned")
            except Exception as exc:  # noqa: BLE001 - report to user
                errors.append(f"{sym}: {exc}")
        self.refresh_table()
        msg = f"Stored/updated {total_rows} rows for {len(symbols)} symbol(s)."
        if errors:
            msg += "\n\nProblems:\n" + "\n".join(errors[:15])
            messagebox.showwarning("Fetch finished with problems", msg)
        else:
            messagebox.showinfo("Fetch complete", msg)
        self.set_status("Ready.")

    def refetch_symbol(self):
        symbol = self.selected_symbol() or simpledialog.askstring(
            "Refresh symbol", "Ticker symbol to (re)fetch:", parent=self
        )
        if symbol and symbol.strip():
            self.fetch_symbols([symbol.strip().upper()])

    def add_record(self):
        dialog = RecordDialog(self, "Add record")
        if dialog.result is None:
            return
        v = dialog.result
        self.db.upsert(
            [
                (
                    v["symbol"],
                    v["date"],
                    v["open"],
                    v["current"],
                    v["close"],
                    v["pct_change"],
                    v["volume"],
                )
            ]
        )
        self.refresh_table()
        self.set_status(f"Added/updated {v['symbol']} {v['date']}.")

    def update_record(self):
        keys = self._selected_keys()
        if len(keys) != 1:
            messagebox.showinfo(
                "Select one row",
                "Please select exactly one row in the history table to update.",
            )
            return
        item = self.detail_tree.selection()[0]
        values = self.detail_tree.item(item, "values")
        initial = dict(zip(COLUMNS, values))
        dialog = RecordDialog(self, "Update record", initial=initial, lock_key=True)
        if dialog.result is None:
            return
        v = dialog.result
        symbol, day = keys[0]
        self.db.update(
            symbol, day, v["open"], v["current"], v["close"], v["pct_change"], v["volume"]
        )
        self.refresh_table()
        self._on_symbol_selected()
        self.set_status(f"Updated {symbol} {day}.")

    def delete_records(self):
        keys = self._selected_keys()
        if not keys:
            messagebox.showinfo(
                "Nothing selected",
                "Select one or more rows in the history table to delete.",
            )
            return
        if not messagebox.askyesno(
            "Confirm delete", f"Delete {len(keys)} selected record(s)?"
        ):
            return
        for symbol, day in keys:
            self.db.delete(symbol, day)
        self.refresh_table()
        self._on_symbol_selected()
        self.set_status(f"Deleted {len(keys)} record(s).")

    def delete_symbol(self):
        symbol = self.selected_symbol() or simpledialog.askstring(
            "Delete symbol", "Delete ALL rows for which symbol?", parent=self
        )
        if not symbol or not symbol.strip():
            return
        symbol = symbol.strip().upper()
        if messagebox.askyesno(
            "Confirm delete", f"Really delete every record for {symbol}?"
        ):
            self.db.delete_symbol(symbol)
            self.refresh_table()
            self.set_status(f"Deleted all records for {symbol}.")

    def show_chart(self):
        ChartDialog(self, self.db, preselect=self.selected_symbol())

    def _on_close(self):
        self.db.close()
        self.destroy()


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
