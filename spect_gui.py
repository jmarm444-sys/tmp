#!/usr/bin/env python3
"""
spectA - approximate model of a two-channel spectrum analyser for
arbitrary data files, with a built-in signal generator and a waveform
library stored in a SQLite database ("wlib.db").

Features
--------
* Signal generator: sine, square, triangle, sawtooth, uniform / gaussian
  noise, and arbitrary waveforms written as expressions of t using
  trig. and other math functions (e.g.  sin(2*pi*5*t) + 0.3*cos(2*pi*40*t)).
* Two channels (CH1 / CH2), shown as two stacked chart panes.  Each pane
  can display the time-domain waveform or its FFT magnitude spectrum
  (window: none / Hann / Hamming / Blackman).
* Waveform math: quick CH1+CH2 / CH1-CH2 / CH1*CH2 buttons plus a free
  expression box using W1 and W2 (e.g.  W1 + W2/3), with the result
  routed to a chosen channel and optionally saved straight to wlib.
* Basic signal processing: FFT spectra, N-point moving average and
  exponential smoothing.
* Timebase editor: attach real time data to a waveform (start datetime
  + interval), e.g. stock data over 1 week with 1 hour intervals; the
  chart x-axis then shows dates and spectra are labelled in cycles/day.
* Library ("wlib"): save, load, rename and delete waveforms; import
  arbitrary CSV data files (with or without a datetime column) and
  export channels back to CSV.

Dependencies: numpy, matplotlib (TkAgg), tkinter, sqlite3 (stdlib).
"""

import json
import math
import os
import sqlite3
from datetime import datetime, timedelta

import numpy as np

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import matplotlib

matplotlib.use("TkAgg")

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wlib.db")

DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
)

TIME_UNITS = {
    "seconds": 1.0,
    "minutes": 60.0,
    "hours": 3600.0,
    "days": 86400.0,
}


def parse_datetime(text):
    """Parse a datetime string using a list of common formats."""
    text = text.strip()
    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise ValueError("Unrecognised date/time: %r" % text)


# ---------------------------------------------------------------------------
# Safe expression evaluation
# ---------------------------------------------------------------------------

_RNG = np.random.default_rng()


def _rand_like(x):
    """Uniform noise in [-1, 1] with the same shape as x."""
    return _RNG.uniform(-1.0, 1.0, np.shape(np.asarray(x, dtype=float)))


def _randn_like(x):
    """Gaussian noise (std = 1) with the same shape as x."""
    return _RNG.normal(0.0, 1.0, np.shape(np.asarray(x, dtype=float)))


SAFE_FUNCS = {
    "sin": np.sin, "cos": np.cos, "tan": np.tan,
    "asin": np.arcsin, "acos": np.arccos, "atan": np.arctan,
    "arcsin": np.arcsin, "arccos": np.arccos, "arctan": np.arctan,
    "sinh": np.sinh, "cosh": np.cosh, "tanh": np.tanh,
    "exp": np.exp, "log": np.log, "log10": np.log10,
    "sqrt": np.sqrt, "abs": np.abs, "sign": np.sign,
    "floor": np.floor, "ceil": np.ceil, "clip": np.clip,
    "minimum": np.minimum, "maximum": np.maximum, "where": np.where,
    "rand": _rand_like, "randn": _randn_like,
    "pi": np.pi, "e": np.e,
}


def safe_eval(expr, extra_names):
    """Evaluate a math expression with only whitelisted names available."""
    expr = expr.strip()
    if not expr:
        raise ValueError("Expression is empty.")
    if "__" in expr:
        raise ValueError("Double underscores are not allowed in expressions.")
    namespace = dict(SAFE_FUNCS)
    namespace.update(extra_names)
    return eval(expr, {"__builtins__": {}}, namespace)  # noqa: S307


# ---------------------------------------------------------------------------
# Waveform model
# ---------------------------------------------------------------------------

class Waveform:
    """Samples + sample interval (dt, seconds) + optional start datetime.

    t_start is a numeric offset (seconds) applied to the x-axis when no
    datetime timebase is attached; correlation results use it to place
    the axis symmetrically around zero lag.
    """

    def __init__(self, samples, dt, name="wave", t0=None, params="",
                 t_start=0.0):
        self.samples = np.asarray(samples, dtype=float).ravel()
        self.dt = float(dt)
        self.name = name
        self.t0 = t0            # datetime or None
        self.params = params    # free-text description
        self.t_start = float(t_start)

    @property
    def n(self):
        return len(self.samples)

    @property
    def fs(self):
        return 1.0 / self.dt if self.dt > 0 else 0.0

    def time_axis(self):
        """Numeric seconds axis, or datetimes when a timebase is attached."""
        if self.t0 is not None:
            return [self.t0 + timedelta(seconds=i * self.dt) for i in range(self.n)]
        return np.arange(self.n) * self.dt + self.t_start

    def copy(self, name=None):
        return Waveform(self.samples.copy(), self.dt,
                        name=name or self.name, t0=self.t0,
                        params=self.params, t_start=self.t_start)

    def describe(self):
        base = "%s  |  %d pts  |  dt=%s s" % (self.name, self.n, fmt_num(self.dt))
        if self.t0 is not None:
            base += "  |  t0=%s" % self.t0.strftime("%Y-%m-%d %H:%M")
        return base


def fmt_num(x):
    """Compact number formatting for labels."""
    if x == 0:
        return "0"
    if abs(x) >= 1000 or abs(x) < 0.001:
        return "%.4g" % x
    return ("%.6f" % x).rstrip("0").rstrip(".")


def spectrum(wf, window_name="none"):
    """Return (freqs, magnitude, freq_unit_label) for the FFT of a waveform."""
    n = wf.n
    if n < 2:
        raise ValueError("Need at least 2 samples for an FFT.")
    y = wf.samples - np.mean(wf.samples)  # remove DC so the spectrum is readable
    if window_name == "hann":
        y = y * np.hanning(n)
    elif window_name == "hamming":
        y = y * np.hamming(n)
    elif window_name == "blackman":
        y = y * np.blackman(n)
    mag = np.abs(np.fft.rfft(y)) * 2.0 / n
    freqs = np.fft.rfftfreq(n, d=wf.dt)
    # For slow "stock-like" data a Hz axis is unreadable; use cycles/day.
    if wf.dt >= 60.0:
        return freqs * 86400.0, mag, "Frequency (cycles/day)"
    return freqs, mag, "Frequency (Hz)"


def moving_average(samples, npts):
    npts = int(npts)
    if npts < 1:
        raise ValueError("Window size must be >= 1.")
    if npts > len(samples):
        raise ValueError("Window size is larger than the waveform.")
    kernel = np.ones(npts) / npts
    return np.convolve(samples, kernel, mode="same")


def exp_smooth(samples, alpha):
    alpha = float(alpha)
    if not 0.0 < alpha <= 1.0:
        raise ValueError("Alpha must be in (0, 1].")
    out = np.empty_like(samples)
    acc = samples[0]
    for i, x in enumerate(samples):
        acc = alpha * x + (1.0 - alpha) * acc
        out[i] = acc
    return out


def correlate(a, b, dt):
    """Normalized cross-correlation of a with b over all lags.

    Returns (lags_seconds, coefficients).  Both inputs are mean-removed
    and the result is scaled so that a perfect match gives 1.0 (the
    autocorrelation of any signal is exactly 1.0 at zero lag).  A
    positive peak lag means b is delayed relative to a.
    """
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if len(a) < 2 or len(b) < 2:
        raise ValueError("Need at least 2 samples in each waveform.")
    a0 = a - a.mean()
    b0 = b - b.mean()
    # correlate(b, a) so that a positive peak lag means b lags (is
    # delayed relative to) a
    c = np.correlate(b0, a0, mode="full")
    denom = math.sqrt(float(np.dot(a0, a0)) * float(np.dot(b0, b0)))
    if denom > 0:
        c = c / denom
    lags = (np.arange(len(c)) - (len(a0) - 1)) * dt
    return lags, c


def fmt_lag(seconds):
    """Human-friendly lag label: seconds, plus hours/days when large."""
    text = "%s s" % fmt_num(seconds)
    if abs(seconds) >= 86400:
        text += " (%s days)" % fmt_num(seconds / 86400.0)
    elif abs(seconds) >= 3600:
        text += " (%s h)" % fmt_num(seconds / 3600.0)
    return text


# ---------------------------------------------------------------------------
# Waveform library database ("wlib")
# ---------------------------------------------------------------------------

class WlibDB:
    def __init__(self, path=DB_FILE):
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS waveforms (
                   id      INTEGER PRIMARY KEY AUTOINCREMENT,
                   name    TEXT NOT NULL,
                   created TEXT NOT NULL,
                   dt      REAL NOT NULL,
                   t0      TEXT,
                   params  TEXT,
                   samples TEXT NOT NULL,
                   t_start REAL NOT NULL DEFAULT 0
               )"""
        )
        cols = [row[1] for row in
                self.conn.execute("PRAGMA table_info(waveforms)")]
        if "t_start" not in cols:  # migrate databases from older versions
            self.conn.execute("ALTER TABLE waveforms "
                              "ADD COLUMN t_start REAL NOT NULL DEFAULT 0")
        self.conn.commit()

    def save(self, wf):
        cur = self.conn.execute(
            "INSERT INTO waveforms "
            "(name, created, dt, t0, params, samples, t_start) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                wf.name,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                wf.dt,
                wf.t0.isoformat(sep=" ") if wf.t0 is not None else None,
                wf.params,
                json.dumps([float(v) for v in wf.samples]),
                wf.t_start,
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def load(self, wid):
        row = self.conn.execute(
            "SELECT name, dt, t0, params, samples, t_start "
            "FROM waveforms WHERE id=?",
            (wid,),
        ).fetchone()
        if row is None:
            raise ValueError("No waveform with id %s" % wid)
        name, dt, t0, params, samples, t_start = row
        t0dt = parse_datetime(t0) if t0 else None
        return Waveform(json.loads(samples), dt, name=name, t0=t0dt,
                        params=params or "", t_start=t_start or 0.0)

    def delete(self, wid):
        self.conn.execute("DELETE FROM waveforms WHERE id=?", (wid,))
        self.conn.commit()

    def rename(self, wid, new_name):
        self.conn.execute("UPDATE waveforms SET name=? WHERE id=?", (new_name, wid))
        self.conn.commit()

    def list_all(self):
        return self.conn.execute(
            "SELECT id, name, created, dt, t0, params, "
            "       json_array_length(samples) "
            "FROM waveforms ORDER BY id"
        ).fetchall()


# ---------------------------------------------------------------------------
# Signal generator
# ---------------------------------------------------------------------------

def generate_waveform(kind, freq, amp, phase_deg, offset, fs, duration, expr=""):
    """Build a Waveform from the generator settings."""
    if fs <= 0:
        raise ValueError("Sample rate must be > 0.")
    if duration <= 0:
        raise ValueError("Duration must be > 0.")
    n = max(2, int(round(fs * duration)))
    t = np.arange(n) / fs
    ph = math.radians(phase_deg)
    w = 2.0 * np.pi * freq

    if kind == "sine":
        y = amp * np.sin(w * t + ph)
        desc = "sine f=%s Hz" % fmt_num(freq)
    elif kind == "square":
        y = amp * np.sign(np.sin(w * t + ph))
        desc = "square f=%s Hz" % fmt_num(freq)
    elif kind == "triangle":
        frac = np.mod(freq * t + ph / (2 * np.pi), 1.0)
        y = amp * (4.0 * np.abs(frac - 0.5) - 1.0)
        desc = "triangle f=%s Hz" % fmt_num(freq)
    elif kind == "sawtooth":
        frac = np.mod(freq * t + ph / (2 * np.pi), 1.0)
        y = amp * (2.0 * frac - 1.0)
        desc = "sawtooth f=%s Hz" % fmt_num(freq)
    elif kind == "noise (uniform)":
        y = amp * _RNG.uniform(-1.0, 1.0, n)
        desc = "uniform noise"
    elif kind == "noise (gaussian)":
        y = amp * _RNG.normal(0.0, 1.0, n)
        desc = "gaussian noise"
    elif kind == "expression":
        y = safe_eval(expr, {"t": t, "f": freq, "A": amp})
        y = np.asarray(y, dtype=float)
        if y.ndim == 0:
            y = np.full(n, float(y))
        if y.shape != t.shape:
            raise ValueError("Expression result has wrong length "
                             "(%s, expected %d)." % (y.shape, n))
        desc = expr
    else:
        raise ValueError("Unknown waveform type: %s" % kind)

    y = y + offset
    name = desc if kind == "expression" else "%s %sHz" % (kind, fmt_num(freq))
    params = "type=%s freq=%s amp=%s phase=%s offset=%s fs=%s dur=%s expr=%s" % (
        kind, freq, amp, phase_deg, offset, fs, duration, expr)
    return Waveform(y, 1.0 / fs, name=name, params=params)


# ---------------------------------------------------------------------------
# Channel display pane
# ---------------------------------------------------------------------------

class ChannelPane(ttk.LabelFrame):
    """One chart pane: waveform storage + time/spectrum matplotlib view."""

    def __init__(self, master, title):
        super().__init__(master, text=title)
        self.title = title
        self.waveform = None

        header = ttk.Frame(self)
        header.pack(fill=tk.X, padx=4, pady=(2, 0))

        self.info_var = tk.StringVar(value="(no data)")
        ttk.Label(header, textvariable=self.info_var).pack(side=tk.LEFT)

        self.window_var = tk.StringVar(value="none")
        win_box = ttk.Combobox(header, textvariable=self.window_var, width=9,
                               state="readonly",
                               values=("none", "hann", "hamming", "blackman"))
        win_box.pack(side=tk.RIGHT, padx=(2, 0))
        win_box.bind("<<ComboboxSelected>>", lambda e: self.redraw())
        ttk.Label(header, text="FFT window:").pack(side=tk.RIGHT)

        self.view_var = tk.StringVar(value="time")
        for text, val in (("Spectrum", "spectrum"), ("Time", "time")):
            ttk.Radiobutton(header, text=text, value=val,
                            variable=self.view_var,
                            command=self.redraw).pack(side=tk.RIGHT, padx=2)

        self.fig = Figure(figsize=(7.0, 2.4), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True, padx=4, pady=2)
        toolbar = NavigationToolbar2Tk(self.canvas, self, pack_toolbar=False)
        toolbar.update()
        toolbar.pack(fill=tk.X, padx=4)

        self.redraw()

    def set_waveform(self, wf):
        self.waveform = wf
        self.redraw()

    def clear(self):
        self.waveform = None
        self.redraw()

    def redraw(self):
        self.ax.clear()
        wf = self.waveform
        if wf is None or wf.n == 0:
            self.info_var.set("(no data)")
            self.ax.text(0.5, 0.5, "no data", ha="center", va="center",
                         transform=self.ax.transAxes, color="grey")
            self.ax.set_xticks([])
            self.ax.set_yticks([])
        else:
            self.info_var.set(wf.describe())
            try:
                if self.view_var.get() == "spectrum":
                    freqs, mag, unit = spectrum(wf, self.window_var.get())
                    self.ax.plot(freqs, mag, color="tab:red", lw=0.9)
                    self.ax.set_xlabel(unit, fontsize=8)
                    self.ax.set_ylabel("Magnitude", fontsize=8)
                    self.ax.set_title("Spectrum - %s" % wf.name, fontsize=9)
                else:
                    x = wf.time_axis()
                    xlabel = "Time (s)"
                    if wf.t0 is None and wf.t_start < 0:
                        # correlation result: symmetric lag axis
                        xlabel = "Lag (s)"
                        if wf.dt >= 60.0:
                            x = np.asarray(x) / 3600.0
                            xlabel = "Lag (hours)"
                    self.ax.plot(x, wf.samples, color="tab:blue", lw=0.9)
                    if wf.t0 is not None:
                        self.fig.autofmt_xdate(rotation=30)
                        self.ax.set_xlabel("Date/time", fontsize=8)
                    else:
                        self.ax.set_xlabel(xlabel, fontsize=8)
                    self.ax.set_ylabel("Amplitude", fontsize=8)
                    self.ax.set_title(wf.name, fontsize=9)
                self.ax.tick_params(labelsize=7)
                self.ax.grid(True, alpha=0.3)
            except Exception as exc:  # keep the GUI alive on plot errors
                self.ax.text(0.5, 0.5, "plot error: %s" % exc, ha="center",
                             va="center", transform=self.ax.transAxes, color="red")
        self.fig.tight_layout()
        self.canvas.draw_idle()


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class SpectApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("spectA - 2-channel spectrum analyser")
        self.geometry("1250x860")
        self.db = WlibDB()

        self.status_var = tk.StringVar(value="Ready.")

        root = ttk.Frame(self)
        root.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(root)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=4, pady=4)
        self._build_generator(left)

        right = ttk.Frame(root)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4, pady=4)

        self.ch1 = ChannelPane(right, "Channel 1")
        self.ch1.pack(fill=tk.BOTH, expand=True, pady=(0, 2))
        self.ch2 = ChannelPane(right, "Channel 2")
        self.ch2.pack(fill=tk.BOTH, expand=True, pady=(2, 0))

        bottom = ttk.Notebook(self)
        bottom.pack(fill=tk.X, padx=4, pady=(0, 2))
        self.notebook = bottom  # subclasses may add extra tabs
        self._build_math_tab(bottom)
        self._build_timebase_tab(bottom)
        self._build_library_tab(bottom)

        ttk.Label(self, textvariable=self.status_var, relief=tk.SUNKEN,
                  anchor=tk.W).pack(fill=tk.X, side=tk.BOTTOM)

        self.refresh_library()

    # -- helpers ------------------------------------------------------------

    def set_status(self, msg):
        self.status_var.set(msg)

    def channel(self, label):
        return self.ch1 if label == "CH1" else self.ch2

    def _require(self, label):
        wf = self.channel(label).waveform
        if wf is None:
            raise ValueError("%s is empty." % label)
        return wf

    def _report(self, exc):
        messagebox.showerror("spectA", str(exc), parent=self)
        self.set_status("Error: %s" % exc)

    # -- generator panel ----------------------------------------------------

    def _build_generator(self, parent):
        box = ttk.LabelFrame(parent, text="Signal generator")
        box.pack(fill=tk.Y, expand=False)

        self.gen_vars = {}
        rows = (
            ("Frequency (Hz)", "freq", "5"),
            ("Amplitude", "amp", "1.0"),
            ("Phase (deg)", "phase", "0"),
            ("Offset", "offset", "0"),
            ("Sample rate (Hz)", "fs", "1000"),
            ("Duration (s)", "dur", "1.0"),
        )

        ttk.Label(box, text="Type:").grid(row=0, column=0, sticky=tk.W,
                                          padx=4, pady=2)
        self.gen_type = tk.StringVar(value="sine")
        ttk.Combobox(box, textvariable=self.gen_type, state="readonly", width=16,
                     values=("sine", "square", "triangle", "sawtooth",
                             "noise (uniform)", "noise (gaussian)",
                             "expression")).grid(row=0, column=1, padx=4, pady=2)

        for i, (label, key, default) in enumerate(rows, start=1):
            ttk.Label(box, text=label + ":").grid(row=i, column=0, sticky=tk.W,
                                                  padx=4, pady=2)
            var = tk.StringVar(value=default)
            self.gen_vars[key] = var
            ttk.Entry(box, textvariable=var, width=18).grid(row=i, column=1,
                                                            padx=4, pady=2)

        r = len(rows) + 1
        ttk.Label(box, text="Expression y(t):").grid(row=r, column=0,
                                                     sticky=tk.W, padx=4, pady=2)
        self.gen_expr = tk.StringVar(value="sin(2*pi*5*t) + 0.3*cos(2*pi*40*t)")
        ttk.Entry(box, textvariable=self.gen_expr, width=18).grid(
            row=r, column=1, padx=4, pady=2)
        ttk.Label(box, text="funcs: sin cos tan exp log sqrt\n"
                            "abs sign rand randn pi e  (of t)",
                  foreground="grey", font=("TkDefaultFont", 8)).grid(
            row=r + 1, column=0, columnspan=2, padx=4, sticky=tk.W)

        ttk.Button(box, text="Generate -> CH1",
                   command=lambda: self.generate("CH1")).grid(
            row=r + 2, column=0, columnspan=2, sticky=tk.EW, padx=4, pady=(6, 2))
        ttk.Button(box, text="Generate -> CH2",
                   command=lambda: self.generate("CH2")).grid(
            row=r + 3, column=0, columnspan=2, sticky=tk.EW, padx=4, pady=2)

        misc = ttk.LabelFrame(parent, text="Channel tools")
        misc.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(misc, text="Clear CH1",
                   command=lambda: self.ch1.clear()).pack(fill=tk.X, padx=4, pady=2)
        ttk.Button(misc, text="Clear CH2",
                   command=lambda: self.ch2.clear()).pack(fill=tk.X, padx=4, pady=2)
        ttk.Button(misc, text="Copy CH1 -> CH2",
                   command=lambda: self.copy_channel("CH1", "CH2")).pack(
            fill=tk.X, padx=4, pady=2)
        ttk.Button(misc, text="Copy CH2 -> CH1",
                   command=lambda: self.copy_channel("CH2", "CH1")).pack(
            fill=tk.X, padx=4, pady=2)

    def generate(self, target):
        try:
            wf = generate_waveform(
                self.gen_type.get(),
                float(self.gen_vars["freq"].get()),
                float(self.gen_vars["amp"].get()),
                float(self.gen_vars["phase"].get()),
                float(self.gen_vars["offset"].get()),
                float(self.gen_vars["fs"].get()),
                float(self.gen_vars["dur"].get()),
                self.gen_expr.get(),
            )
        except Exception as exc:
            return self._report(exc)
        self.channel(target).set_waveform(wf)
        self.set_status("Generated %s into %s." % (wf.name, target))

    def copy_channel(self, src, dst):
        try:
            wf = self._require(src)
        except ValueError as exc:
            return self._report(exc)
        self.channel(dst).set_waveform(wf.copy())
        self.set_status("Copied %s to %s." % (src, dst))

    # -- math / DSP tab -----------------------------------------------------

    def _build_math_tab(self, notebook):
        tab = ttk.Frame(notebook)
        notebook.add(tab, text="Math & DSP")

        row1 = ttk.Frame(tab)
        row1.pack(fill=tk.X, padx=4, pady=3)
        ttk.Label(row1, text="Quick ops:").pack(side=tk.LEFT)
        for label, expr in (("CH1+CH2", "W1 + W2"),
                            ("CH1-CH2", "W1 - W2"),
                            ("CH1*CH2", "W1 * W2")):
            ttk.Button(row1, text=label, width=9,
                       command=lambda e=expr: self.apply_math(e)).pack(
                side=tk.LEFT, padx=2)

        ttk.Label(row1, text="   Result ->").pack(side=tk.LEFT)
        self.math_target = tk.StringVar(value="CH1")
        ttk.Combobox(row1, textvariable=self.math_target, state="readonly",
                     width=5, values=("CH1", "CH2")).pack(side=tk.LEFT, padx=4)

        row2 = ttk.Frame(tab)
        row2.pack(fill=tk.X, padx=4, pady=3)
        ttk.Label(row2, text="Expression (W1, W2):").pack(side=tk.LEFT)
        self.math_expr = tk.StringVar(value="W1 + W2/3")
        ttk.Entry(row2, textvariable=self.math_expr, width=42).pack(
            side=tk.LEFT, padx=4)
        ttk.Button(row2, text="Apply",
                   command=lambda: self.apply_math(self.math_expr.get())).pack(
            side=tk.LEFT, padx=2)
        ttk.Button(row2, text="Apply & Save to wlib...",
                   command=lambda: self.apply_math(self.math_expr.get(),
                                                   save=True)).pack(
            side=tk.LEFT, padx=2)
        ttk.Label(row2, text="e.g.  W1 + W2/3,  abs(W1),  W1*sin(2*pi*2*t)",
                  foreground="grey").pack(side=tk.LEFT, padx=6)

        row3 = ttk.Frame(tab)
        row3.pack(fill=tk.X, padx=4, pady=3)
        ttk.Label(row3, text="Moving average, N points:").pack(side=tk.LEFT)
        self.ma_n = tk.StringVar(value="9")
        ttk.Entry(row3, textvariable=self.ma_n, width=6).pack(side=tk.LEFT, padx=2)
        self.ma_target = tk.StringVar(value="CH1")
        ttk.Combobox(row3, textvariable=self.ma_target, state="readonly",
                     width=5, values=("CH1", "CH2")).pack(side=tk.LEFT, padx=2)
        ttk.Button(row3, text="Apply MA", command=self.apply_ma).pack(
            side=tk.LEFT, padx=4)

        ttk.Label(row3, text="   Exp. smoothing, alpha:").pack(side=tk.LEFT)
        self.sm_alpha = tk.StringVar(value="0.25")
        ttk.Entry(row3, textvariable=self.sm_alpha, width=6).pack(
            side=tk.LEFT, padx=2)
        self.sm_target = tk.StringVar(value="CH1")
        ttk.Combobox(row3, textvariable=self.sm_target, state="readonly",
                     width=5, values=("CH1", "CH2")).pack(side=tk.LEFT, padx=2)
        ttk.Button(row3, text="Apply smoothing", command=self.apply_smooth).pack(
            side=tk.LEFT, padx=4)

        row4 = ttk.Frame(tab)
        row4.pack(fill=tk.X, padx=4, pady=3)
        ttk.Label(row4, text="Correlation:").pack(side=tk.LEFT)
        ttk.Button(row4, text="Autocorr CH1",
                   command=lambda: self.apply_correlation("CH1")).pack(
            side=tk.LEFT, padx=2)
        ttk.Button(row4, text="Autocorr CH2",
                   command=lambda: self.apply_correlation("CH2")).pack(
            side=tk.LEFT, padx=2)
        ttk.Button(row4, text="Xcorr CH1 x CH2",
                   command=lambda: self.apply_correlation("XCORR")).pack(
            side=tk.LEFT, padx=2)
        ttk.Label(row4, text="result goes to the 'Result ->' channel; "
                             "positive xcorr peak lag = CH2 lags CH1",
                  foreground="grey").pack(side=tk.LEFT, padx=6)

    def apply_math(self, expr, save=False):
        try:
            names = {}
            ref = None
            w1 = self.ch1.waveform
            w2 = self.ch2.waveform
            if w1 is not None and w2 is not None:
                nmin = min(w1.n, w2.n)
                if w1.n != w2.n:
                    self.set_status("Note: channel lengths differ, "
                                    "truncated to %d points." % nmin)
                names["W1"] = w1.samples[:nmin]
                names["W2"] = w2.samples[:nmin]
                ref = w1
                if abs(w1.dt - w2.dt) > 1e-12:
                    messagebox.showwarning(
                        "spectA", "CH1 and CH2 have different sample intervals; "
                                  "using the CH1 timebase for the result.",
                        parent=self)
            elif w1 is not None:
                names["W1"] = w1.samples
                ref = w1
            elif w2 is not None:
                names["W2"] = w2.samples
                ref = w2
            else:
                raise ValueError("Both channels are empty.")

            n = len(names.get("W1", names.get("W2")))
            names["t"] = np.arange(n) * ref.dt
            result = safe_eval(expr, names)
            result = np.asarray(result, dtype=float)
            if result.ndim == 0:
                result = np.full(n, float(result))
            wf = Waveform(result, ref.dt, name=expr, t0=ref.t0,
                          params="math: %s" % expr)
        except Exception as exc:
            return self._report(exc)

        target = self.math_target.get()
        self.channel(target).set_waveform(wf)
        self.set_status("Applied %r -> %s." % (expr, target))
        if save:
            self.save_waveform_to_lib(wf)

    def apply_ma(self):
        try:
            target = self.ma_target.get()
            wf = self._require(target)
            new = wf.copy(name="MA%s(%s)" % (self.ma_n.get(), wf.name))
            new.samples = moving_average(wf.samples, int(self.ma_n.get()))
        except Exception as exc:
            return self._report(exc)
        self.channel(target).set_waveform(new)
        self.set_status("Applied %s-point moving average to %s."
                        % (self.ma_n.get(), target))

    def apply_smooth(self):
        try:
            target = self.sm_target.get()
            wf = self._require(target)
            new = wf.copy(name="smooth%s(%s)" % (self.sm_alpha.get(), wf.name))
            new.samples = exp_smooth(wf.samples, float(self.sm_alpha.get()))
        except Exception as exc:
            return self._report(exc)
        self.channel(target).set_waveform(new)
        self.set_status("Applied exponential smoothing (alpha=%s) to %s."
                        % (self.sm_alpha.get(), target))

    def apply_correlation(self, mode):
        try:
            if mode == "XCORR":
                w1 = self._require("CH1")
                w2 = self._require("CH2")
                if abs(w1.dt - w2.dt) > 1e-12:
                    messagebox.showwarning(
                        "spectA", "CH1 and CH2 have different sample "
                                  "intervals; using the CH1 interval for "
                                  "the lag axis.", parent=self)
                nmin = min(w1.n, w2.n)
                a = w1.samples[:nmin]
                b = w2.samples[:nmin]
                dt = w1.dt
                name = "xcorr(CH1, CH2)"
                params = "xcorr of %r and %r" % (w1.name, w2.name)
            else:
                src = self._require(mode)
                a = b = src.samples
                dt = src.dt
                name = "autocorr(%s)" % mode
                params = "autocorr of %r" % src.name
            lags, coeffs = correlate(a, b, dt)
        except Exception as exc:
            return self._report(exc)

        wf = Waveform(coeffs, dt, name=name, params=params,
                      t_start=float(lags[0]))
        target = self.math_target.get()
        self.channel(target).set_waveform(wf)

        peak = int(np.argmax(coeffs))
        if mode == "XCORR":
            self.set_status(
                "Xcorr -> %s. Peak r=%.3f at lag %s (positive = CH2 lags "
                "CH1)." % (target, coeffs[peak], fmt_lag(float(lags[peak]))))
        else:
            self.set_status("Autocorrelation of %s -> %s." % (mode, target))

    # -- timebase tab ---------------------------------------------------------

    def _build_timebase_tab(self, notebook):
        tab = ttk.Frame(notebook)
        notebook.add(tab, text="Timebase")

        row = ttk.Frame(tab)
        row.pack(fill=tk.X, padx=4, pady=6)
        ttk.Label(row, text="Channel:").pack(side=tk.LEFT)
        self.tb_target = tk.StringVar(value="CH1")
        ttk.Combobox(row, textvariable=self.tb_target, state="readonly",
                     width=5, values=("CH1", "CH2")).pack(side=tk.LEFT, padx=4)

        ttk.Label(row, text="Start (YYYY-MM-DD HH:MM):").pack(side=tk.LEFT,
                                                              padx=(10, 2))
        self.tb_start = tk.StringVar(
            value=datetime.now().strftime("%Y-%m-%d") + " 09:00")
        ttk.Entry(row, textvariable=self.tb_start, width=18).pack(side=tk.LEFT)

        ttk.Label(row, text="Interval:").pack(side=tk.LEFT, padx=(10, 2))
        self.tb_value = tk.StringVar(value="1")
        ttk.Entry(row, textvariable=self.tb_value, width=7).pack(side=tk.LEFT)
        self.tb_unit = tk.StringVar(value="hours")
        ttk.Combobox(row, textvariable=self.tb_unit, state="readonly", width=8,
                     values=tuple(TIME_UNITS)).pack(side=tk.LEFT, padx=4)

        ttk.Button(row, text="Attach timebase",
                   command=self.attach_timebase).pack(side=tk.LEFT, padx=8)
        ttk.Button(row, text="Clear timebase",
                   command=self.clear_timebase).pack(side=tk.LEFT)

        ttk.Label(tab, text="Example: 1 week of hourly stock data = 168 samples, "
                            "interval 1 hours. The chart x-axis then shows "
                            "dates and spectra are shown in cycles/day.",
                  foreground="grey").pack(anchor=tk.W, padx=6, pady=(0, 6))

    def attach_timebase(self):
        try:
            target = self.tb_target.get()
            wf = self._require(target)
            t0 = parse_datetime(self.tb_start.get())
            step = float(self.tb_value.get()) * TIME_UNITS[self.tb_unit.get()]
            if step <= 0:
                raise ValueError("Interval must be > 0.")
            new = wf.copy()
            new.t0 = t0
            new.dt = step
        except Exception as exc:
            return self._report(exc)
        self.channel(target).set_waveform(new)
        self.set_status("Attached timebase to %s: start %s, step %s s."
                        % (target, t0, fmt_num(step)))

    def clear_timebase(self):
        try:
            target = self.tb_target.get()
            wf = self._require(target)
        except ValueError as exc:
            return self._report(exc)
        new = wf.copy()
        new.t0 = None
        self.channel(target).set_waveform(new)
        self.set_status("Cleared timebase on %s." % target)

    # -- library tab ----------------------------------------------------------

    def _build_library_tab(self, notebook):
        tab = ttk.Frame(notebook)
        notebook.add(tab, text="Library (wlib)")

        cols = ("id", "name", "points", "dt", "t0", "created")
        self.tree = ttk.Treeview(tab, columns=cols, show="headings", height=5)
        widths = (40, 300, 70, 90, 140, 140)
        for col, width in zip(cols, widths):
            self.tree.heading(col, text=col)
            self.tree.column(col, width=width, anchor=tk.W, stretch=(col == "name"))
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4, pady=4)

        sb = ttk.Scrollbar(tab, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.LEFT, fill=tk.Y, pady=4)

        btns = ttk.Frame(tab)
        btns.pack(side=tk.LEFT, fill=tk.Y, padx=4, pady=4)
        actions = (
            ("Save CH1...", lambda: self.save_channel("CH1")),
            ("Save CH2...", lambda: self.save_channel("CH2")),
            ("Load -> CH1", lambda: self.load_selected("CH1")),
            ("Load -> CH2", lambda: self.load_selected("CH2")),
            ("Rename...", self.rename_selected),
            ("Delete", self.delete_selected),
            ("Import CSV -> CH1", lambda: self.import_csv("CH1")),
            ("Import CSV -> CH2", lambda: self.import_csv("CH2")),
            ("Export CH1 CSV...", lambda: self.export_csv("CH1")),
            ("Export CH2 CSV...", lambda: self.export_csv("CH2")),
        )
        for i, (label, cmd) in enumerate(actions):
            ttk.Button(btns, text=label, width=17, command=cmd).grid(
                row=i % 5, column=i // 5, padx=2, pady=1, sticky=tk.EW)

    def refresh_library(self):
        self.tree.delete(*self.tree.get_children())
        for wid, name, created, dt, t0, _params, npts in self.db.list_all():
            self.tree.insert("", tk.END, iid=str(wid),
                             values=(wid, name, npts, fmt_num(dt),
                                     t0 or "-", created))

    def _selected_id(self):
        sel = self.tree.selection()
        if not sel:
            raise ValueError("Select a waveform in the library list first.")
        return int(sel[0])

    def save_channel(self, label):
        try:
            wf = self._require(label)
        except ValueError as exc:
            return self._report(exc)
        self.save_waveform_to_lib(wf)

    def save_waveform_to_lib(self, wf):
        name = simpledialog.askstring("Save to wlib", "Waveform name:",
                                      initialvalue=wf.name, parent=self)
        if not name:
            return
        stored = wf.copy(name=name)
        wid = self.db.save(stored)
        self.refresh_library()
        self.set_status("Saved %r to wlib (id %d)." % (name, wid))

    def load_selected(self, target):
        try:
            wf = self.db.load(self._selected_id())
        except Exception as exc:
            return self._report(exc)
        self.channel(target).set_waveform(wf)
        self.set_status("Loaded %r into %s." % (wf.name, target))

    def rename_selected(self):
        try:
            wid = self._selected_id()
        except ValueError as exc:
            return self._report(exc)
        current = self.tree.item(str(wid))["values"][1]
        name = simpledialog.askstring("Rename", "New name:",
                                      initialvalue=current, parent=self)
        if not name:
            return
        self.db.rename(wid, name)
        self.refresh_library()
        self.set_status("Renamed waveform %d to %r." % (wid, name))

    def delete_selected(self):
        try:
            wid = self._selected_id()
        except ValueError as exc:
            return self._report(exc)
        name = self.tree.item(str(wid))["values"][1]
        if not messagebox.askyesno("Delete", "Delete %r from wlib?" % name,
                                   parent=self):
            return
        self.db.delete(wid)
        self.refresh_library()
        self.set_status("Deleted %r from wlib." % name)

    # -- CSV import / export --------------------------------------------------

    def import_csv(self, target):
        path = filedialog.askopenfilename(
            parent=self, title="Import data file",
            filetypes=[("CSV / text", "*.csv *.txt *.dat"), ("All files", "*")])
        if not path:
            return
        try:
            wf = load_data_file(path)
            if wf.t0 is None:
                ans = simpledialog.askstring(
                    "Sample interval",
                    "No datetime column found.\nSample interval in seconds:",
                    initialvalue="1.0", parent=self)
                if ans:
                    wf.dt = float(ans)
        except Exception as exc:
            return self._report(exc)
        self.channel(target).set_waveform(wf)
        self.set_status("Imported %d points from %s into %s."
                        % (wf.n, os.path.basename(path), target))

    def export_csv(self, label):
        try:
            wf = self._require(label)
        except ValueError as exc:
            return self._report(exc)
        path = filedialog.asksaveasfilename(
            parent=self, title="Export %s" % label, defaultextension=".csv",
            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        with open(path, "w") as fh:
            fh.write("time,value\n")
            axis = wf.time_axis()
            for x, y in zip(axis, wf.samples):
                if wf.t0 is not None:
                    fh.write("%s,%s\n" % (x.strftime("%Y-%m-%d %H:%M:%S"), y))
                else:
                    fh.write("%s,%s\n" % (x, y))
        self.set_status("Exported %s to %s." % (label, path))


def load_data_file(path):
    """Read an arbitrary CSV/text data file into a Waveform.

    Accepts either a single numeric column, or datetime + value columns
    (the datetime column sets the timebase; the last numeric column on
    each row is used as the sample value).  Header lines are skipped.
    """
    times, values = [], []
    with open(path, "r") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.replace(";", ",").split(",")]
            if len(parts) == 1:
                parts = line.split()
            value = None
            for part in reversed(parts):
                try:
                    value = float(part)
                    break
                except ValueError:
                    continue
            if value is None:
                continue  # header or non-numeric line
            stamp = None
            try:
                stamp = parse_datetime(parts[0])
            except ValueError:
                pass
            times.append(stamp)
            values.append(value)

    if not values:
        raise ValueError("No numeric data found in %s" % path)

    name = os.path.basename(path)
    if all(t is not None for t in times) and len(times) > 1:
        deltas = [(times[i + 1] - times[i]).total_seconds()
                  for i in range(len(times) - 1)]
        dt = float(np.median(deltas)) or 1.0
        return Waveform(values, dt, name=name, t0=times[0],
                        params="imported from %s" % path)
    return Waveform(values, 1.0, name=name, params="imported from %s" % path)


def main():
    app = SpectApp()
    app.mainloop()


if __name__ == "__main__":
    main()
