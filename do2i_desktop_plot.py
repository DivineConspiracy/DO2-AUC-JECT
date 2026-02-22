#!/usr/bin/env python3

"""
DO2i AUC Monitor
Real-time DO₂i plotting with AUC deficit metrics.

────────────────────────────────────────
LINUX / macOS
────────────────────────────────────────

Run with LIVE serial data:
    ./do2i_desktop_plot.py --port /dev/ttyUSB0 --baud 38400

Run in SIMULATION mode:
    ./do2i_desktop_plot.py -sim

(If not executable, use:)
    python3 do2i_desktop_plot.py -sim


────────────────────────────────────────
WINDOWS (Command Prompt or PowerShell)
────────────────────────────────────────

Run with LIVE serial data:
    python do2i_desktop_plot.py --port COM3 --baud 38400

Run in SIMULATION mode:
    python do2i_desktop_plot.py -sim

(If 'python' is not recognized, try:)
    py do2i_desktop_plot.py -sim

Replace COM3 with the correct serial port from Device Manager.
"""

import argparse, time, re, math, sys, threading, queue
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional, Tuple

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from matplotlib.widgets import Button
from matplotlib.ticker import FuncFormatter
from matplotlib.collections import PolyCollection

try:
    import serial
except Exception:
    serial = None

# -------------------- Constants --------------------
DO2I_ALERT = 270.0
AUC_TOTAL_ALERT = 1555.0
AUC_SINGLE_ALERT = 880.0
DEFAULT_WINDOW_MINS = 60.0
HARD_MAX_POINTS = 200_000

# -------------------- CLI --------------------
def get_args():
    p = argparse.ArgumentParser(description="Live DO2i plot with AUC metrics and right-side view buttons.")
    p.add_argument("-sim", action="store_true", help="Run with simulated data stream")
    p.add_argument("--port", type=str, default="/dev/ttyUSB0", help="Serial port")
    p.add_argument("--baud", type=int, default=38400, help="Baud rate")
    p.add_argument("--param", "-param", type=int, default=None,
                   help="Start by plotting numeric field N (1-based) from each serial line")
    p.add_argument("--title", type=str, default="DO₂i AUC Monitor")
    return p.parse_args()

# -------------------- Data Source --------------------
class DataSource:
    """
    - Default: extracts labeled 'do2i' if present.
    - Param-cycle/boot mode: when param_index is not None, selects the Nth numeric
      field from each serial line (numbers matched by regex, 0-based index).
    """
    def __init__(self, simulate: bool, port: str, baud: int, start_param_index: Optional[int] = None):
        self.sim = simulate
        self.q = queue.Queue(maxsize=1000)
        self._stop = threading.Event()
        self.param_index: Optional[int] = start_param_index  # 0-based; None = labeled mode
        self.last_field_count: int = 0
        self.thread = threading.Thread(target=self._run, args=(port, baud), daemon=True)
        self.thread.start()

    # --- control from UI ---
    def set_param_index(self, idx: Optional[int]):
        self.param_index = idx

    def cycle_param(self):
        n = max(self.last_field_count, 1)
        if self.param_index is None:
            self.param_index = 0
        else:
            self.param_index = (self.param_index + 1) % n

    # --- worker thread ---
    def _run(self, port: str, baud: int):
        if self.sim:
            self._sim_loop()
        else:
            self._serial_loop(port, baud)

    def _sim_loop(self):
        t0 = time.time()
        do2i = 220.0
        drift = -0.02
        rng = np.random.default_rng(42)
        while not self._stop.is_set():
            t = time.time() - t0
            wobble = 25.0 * math.sin(2*math.pi*t/500.0) + 10.0*rng.normal()
            trend = drift * (t/60.0)
            val = max(50.0, min(600.0, do2i + wobble + trend))
            self.q.put((t, val))
            time.sleep(0.5)

    def _serial_loop(self, port: str, baud: int):
        if serial is None:
            print("pyserial not installed; use -sim.", file=sys.stderr)
            return
        try:
            with serial.Serial(port, baudrate=baud, timeout=1) as ser:
                ser.setDTR(True); ser.setRTS(True)
                t0 = time.time()
                buff = b""
                while not self._stop.is_set():
                    chunk = ser.read(2048)
                    if not chunk:
                        continue
                    buff += chunk
                    *lines, buff = buff.split(b"\n")
                    for raw in lines:
                        line = raw.decode("utf-8", errors="ignore").strip()
                        v = self._extract_value(line)
                        if v is None:
                            continue
                        t = time.time() - t0
                        self.q.put((t, v))
        except Exception as e:
            print(f"[Serial] {e}", file=sys.stderr)

    def _extract_value(self, line: str) -> Optional[float]:
        # Param mode: select numeric field
        if self.param_index is not None:
            fields = re.findall(r"[-+]?\d+(?:\.\d+)?", line)
            self.last_field_count = len(fields)
            if self.last_field_count == 0:
                return None
            idx = self.param_index % self.last_field_count
            try:
                return float(fields[idx])
            except Exception:
                return None

        # Default: labeled do2i
        m = re.search(r"\bdo2i\b[:=\s]+([-+]?\d+(?:\.\d+)?)", line, flags=re.I)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
        for tok in re.split(r"[,\s]+", line):
            if "=" in tok:
                k, _, val = tok.partition("=")
                if k.strip().lower() == "do2i":
                    try:
                        return float(val)
                    except ValueError:
                        pass
        return None

    def get_many(self) -> List[Tuple[float, float]]:
        out = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                break
        return out

    def stop(self):
        self._stop.set()

# -------------------- AUC math --------------------
@dataclass
class EpisodeStats:
    minutes_under: float
    auc_total: float
    longest_episode_min: float
    max_single_auc: float
    nadir: float

def compute_stats(times: np.ndarray, values: np.ndarray, threshold: float) -> EpisodeStats:
    if len(times) < 2:
        vmin = float(values[-1]) if len(values) else float("nan")
        return EpisodeStats(0.0, 0.0, 0.0, 0.0, vmin)

    t, v = times, values
    below = v < threshold
    dt_sec = np.diff(t)
    ind_avg = (below[:-1].astype(float) + below[1:].astype(float)) / 2.0
    minutes_under = float(np.sum(dt_sec * ind_avg) / 60.0)

    deficit_left = np.maximum(0.0, threshold - v[:-1])
    deficit_right = np.maximum(0.0, threshold - v[1:])
    auc_total = float(np.sum(0.5 * (deficit_left + deficit_right) * (dt_sec / 60.0)))

    longest_minutes = 0.0
    max_single_auc = 0.0
    i, n = 0, len(v)
    while i < n:
        if not below[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and below[j + 1]:
            j += 1
        epi_t = t[i:j+1]
        epi_v = v[i:j+1]
        if len(epi_t) >= 2:
            epi_dt = np.diff(epi_t) / 60.0
            epi_def_left = np.maximum(0.0, threshold - epi_v[:-1])
            epi_def_right = np.maximum(0.0, threshold - epi_v[1:])
            epi_auc = float(np.sum(0.5 * (epi_def_left + epi_def_right) * epi_dt))
            epi_minutes = float(np.sum(epi_dt))
        else:
            epi_auc = 0.0
            epi_minutes = 0.0
        longest_minutes = max(longest_minutes, epi_minutes)
        max_single_auc = max(max_single_auc, epi_auc)
        i = j + 1

    nadir = float(np.min(v))
    return EpisodeStats(minutes_under, auc_total, longest_minutes, max_single_auc, nadir)

# -------------------- Plot App --------------------
class App:
    def __init__(self, args):
        self.args = args
        self.window_mins = DEFAULT_WINDOW_MINS
        self.t_buf: Deque[float] = deque(maxlen=HARD_MAX_POINTS)
        self.v_buf: Deque[float] = deque(maxlen=HARD_MAX_POINTS)

        # NEW: pause + flashing border state
        self.paused = True          # ← start paused
        self._pause_flash_on = True

        self.fig = plt.figure(figsize=(11, 6))
        self.fig.canvas.manager.set_window_title(args.title)

        # Left metric panel
        self.ax_left = self.fig.add_axes([0.03, 0.08, 0.20, 0.84])
        self.ax_left.axis("off")

        # Main plot
        self.ax_main = self.fig.add_axes([0.28, 0.12, 0.52, 0.78])

        # Buttons (existing stack + Next param + Start/Pause)
        self._make_buttons()

        (self.line_do2i,) = self.ax_main.plot([], [], lw=1.8)
        self.ax_main.set_xlabel("Time (minutes)")
        self.ax_main.set_ylabel("DO₂i (mL/min/m²)")
        self.ax_main.grid(True, alpha=0.25)

        self.th_line = self.ax_main.axhline(DO2I_ALERT, color="red", lw=1.2, alpha=0.9)

        # Persistent fill artist to prevent alpha flicker (PINK)
        self.fill_poly = PolyCollection(
            [],
            facecolors=["#f6a6c1"],  # soft pink
            alpha=0.22,
            edgecolors="none"
        )
        self.ax_main.add_collection(self.fill_poly)
        self.fill_poly.set_visible(False)

        self.metric_text_artists: List[plt.Text] = []

        self.ax_main.xaxis.set_major_formatter(FuncFormatter(lambda x, pos: f"{x:.0f}"))

        # Data source with optional starting param index (convert 1-based -> 0-based)
        start_idx = (args.param - 1) if args.param and args.param > 0 else None
        self.src = DataSource(simulate=args.sim, port=args.port, baud=args.baud, start_param_index=start_idx)

        self.ani = FuncAnimation(self.fig, self._on_frame, interval=500, blit=False)
        self.fig.tight_layout(pad=0.6)

        # Initialize param-button label if boot param provided
        if start_idx is not None:
            self.btn_param.label.set_text(f"Param {start_idx+1}")

        # Ensure initial button style reflects initial state
        self._apply_pause_button_style()

    # ---------- Buttons ----------
    def _make_buttons(self):
        bx, bw, bh, gap = 0.83, 0.13, 0.06, 0.012
        step = bh + gap
        y0 = 0.48

        self.btn_ax_5   = self.fig.add_axes([bx, y0 + 4*step, bw, bh])
        self.btn_ax_30  = self.fig.add_axes([bx, y0 + 3*step, bw, bh])
        self.btn_ax_60  = self.fig.add_axes([bx, y0 + 2*step, bw, bh])
        self.btn_ax_120 = self.fig.add_axes([bx, y0 + 1*step, bw, bh])
        self.btn_ax_all = self.fig.add_axes([bx, y0 + 0*step, bw, bh])

        self.btn_5   = Button(self.btn_ax_5, "5 min")
        self.btn_30  = Button(self.btn_ax_30, "30 min")
        self.btn_60  = Button(self.btn_ax_60, "60 min")
        self.btn_120 = Button(self.btn_ax_120, "120 min")
        self.btn_all = Button(self.btn_ax_all, "All")

        self.btn_5.on_clicked(lambda evt: self._set_window(5.0))
        self.btn_30.on_clicked(lambda evt: self._set_window(30.0))
        self.btn_60.on_clicked(lambda evt: self._set_window(60.0))
        self.btn_120.on_clicked(lambda evt: self._set_window(120.0))
        self.btn_all.on_clicked(lambda evt: self._set_window(float("inf")))

        # Next param (below "All")
        self.btn_ax_param = self.fig.add_axes([bx, y0 - 1*step, bw, bh])
        self.btn_param = Button(self.btn_ax_param, "Next param")
        self.btn_param.on_clicked(self._next_param)

        # NEW: Start/Pause (below "Next param")
        self.btn_ax_pause = self.fig.add_axes([bx, y0 - 2*step, bw, bh])
        # Set explicit colors so hover doesn't turn gray
        self.btn_pause = Button(self.btn_ax_pause, "Pause", color="#00cc44", hovercolor="#00cc44")
        self.btn_pause.on_clicked(self._toggle_pause)

        # Give the pause axes a border we can animate
        self.btn_ax_pause.patch.set_edgecolor("#00ff66")
        self.btn_ax_pause.patch.set_linewidth(1.8)

    def _set_pause_button_colors(self, face: str, edge: str, lw: float):
        # Button has its own face + hover colors; axes patch also needs to match.
        self.btn_pause.color = face
        self.btn_pause.hovercolor = face  # prevents default gray hover
        self.btn_ax_pause.set_facecolor(face)
        self.btn_ax_pause.patch.set_edgecolor(edge)
        self.btn_ax_pause.patch.set_linewidth(lw)

    def _apply_pause_button_style(self):
        if self.paused:
            # Paused: yellow, black border, text "Start", no flashing border
            self.btn_pause.label.set_text("Start")
            self._set_pause_button_colors(face="#ffeb3b", edge="black", lw=1.0)
        else:
            # Running: green, thin-ish flashing green border, text "Pause"
            self.btn_pause.label.set_text("Pause")
            # base style; border flashing is handled in _on_frame
            self._set_pause_button_colors(face="#00cc44", edge="#00ff66", lw=1.8)

    def _toggle_pause(self, _evt):
        self.paused = not self.paused
        self._pause_flash_on = True  # reset flash phase when toggling
        self._apply_pause_button_style()
        self.fig.canvas.draw_idle()

    def _next_param(self, _evt):
        # Advance source index
        self.src.cycle_param()
        # Clear buffers so new parameter takes over immediately (fresh autoscale)
        self.t_buf.clear()
        self.v_buf.clear()
        # Update label (1-based)
        idx = (self.src.param_index or 0) + 1
        self.btn_param.label.set_text(f"Param {idx}")

    def _set_window(self, mins: float):
        self.window_mins = mins

    # ---------- Drawing ----------
    def _on_frame(self, _):
        # Drain queue every frame so it doesn't backlog.
        new_points = self.src.get_many()

        # Only append new points when running (not paused).
        if not self.paused:
            for t, v in new_points:
                self.t_buf.append(t)
                self.v_buf.append(v)
        # If paused: discard new_points intentionally (freeze display, no backlog)

        if not self.t_buf:
            return

        t = np.fromiter(self.t_buf, dtype=float)
        v = np.fromiter(self.v_buf, dtype=float)

        t_min, t_max = t[0], t[-1]
        left_bound_sec = max(t_min, t_max - self.window_mins * 60.0) if math.isfinite(self.window_mins) else t_min

        vis = t >= left_bound_sec
        tv = (t[vis] - left_bound_sec) / 60.0  # minutes, visible window
        vv = v[vis]

        # Update line
        self.line_do2i.set_data(tv, vv)

        # X-limits
        if len(tv):
            self.ax_main.set_xlim(tv[0], max(tv[-1], tv[0] + 1.0))

        # Y-limits: autoscale from VISIBLE series so a param change rescales instantly
        self._autoscale_y(vv)

        # Threshold + fill
        self.th_line.set_ydata([DO2I_ALERT, DO2I_ALERT])
        self._update_fill(tv, vv, DO2I_ALERT)

        # Metrics from full history of current param
        self._draw_metrics(t, v)

        # Keep param label in sync if field count changes
        if self.src.param_index is not None and self.src.last_field_count > 0:
            idx = (self.src.param_index % self.src.last_field_count) + 1
            self.btn_param.label.set_text(f"Param {idx}")

        # Flashing outline ONLY when running (Pause visible)
        if not self.paused:
            self._pause_flash_on = not self._pause_flash_on
            edge = "#00ff66" if self._pause_flash_on else "#00a83a"  # both green, different brightness
            self.btn_ax_pause.patch.set_edgecolor(edge)
            self.btn_ax_pause.patch.set_linewidth(1.8)

            # NEW: flash "Pause" text color black <-> white at same rate as border
            self.btn_pause.label.set_color("white" if self._pause_flash_on else "black")
        else:
            # Ensure paused is stable black border + stable text color
            self.btn_ax_pause.patch.set_edgecolor("black")
            self.btn_ax_pause.patch.set_linewidth(1.0)
            self.btn_pause.label.set_color("black")

    def _autoscale_y(self, values_visible):
        if len(values_visible) == 0:
            return
        vmin, vmax = np.min(values_visible), np.max(values_visible)
        pad = 30.0
        lo, hi = min(vmin, DO2I_ALERT) - pad, max(vmax, DO2I_ALERT) + pad
        if hi <= lo:
            hi = lo + 10.0
        self.ax_main.set_ylim(lo, hi)

    def _update_fill(self, x_min, y, thresh):
        # Update a persistent PolyCollection's verts (no remove/recreate = no alpha flicker)
        n = len(x_min)
        if n < 2:
            self.fill_poly.set_visible(False)
            self.fill_poly.set_verts([])
            return

        x = np.asarray(x_min, dtype=float)
        y = np.asarray(y, dtype=float)
        below = y < thresh

        polys = []
        i = 0
        while i < n:
            if not below[i]:
                i += 1
                continue

            start = i
            while i + 1 < n and below[i + 1]:
                i += 1
            end = i  # inclusive
            i += 1

            # left threshold crossing
            if start > 0:
                x0, y0 = x[start - 1], y[start - 1]
                x1, y1 = x[start], y[start]
                if y1 != y0:
                    x_left = x0 + (thresh - y0) * (x1 - x0) / (y1 - y0)
                else:
                    x_left = x1
            else:
                x_left = x[start]

            # right threshold crossing
            if end < n - 1:
                x0, y0 = x[end], y[end]
                x1, y1 = x[end + 1], y[end + 1]
                if y1 != y0:
                    x_right = x0 + (thresh - y0) * (x1 - x0) / (y1 - y0)
                else:
                    x_right = x0
            else:
                x_right = x[end]

            xs = [x_left]
            ys = [thresh]
            xs.extend(x[start:end + 1].tolist())
            ys.extend(y[start:end + 1].tolist())
            xs.append(x_right)
            ys.append(thresh)

            polys.append(np.column_stack([xs, ys]))

        if not polys:
            self.fill_poly.set_visible(False)
            self.fill_poly.set_verts([])
            return

        # keep pink (do NOT match line color)
        self.fill_poly.set_verts(polys)
        self.fill_poly.set_visible(True)

    # ---------- Metrics ----------
    def _draw_metrics(self, t_all, v_all):
        # clear old text
        if hasattr(self, "metric_text_artists"):
            for txt in self.metric_text_artists:
                txt.remove()
        self.metric_text_artists = []

        stats = compute_stats(t_all, v_all, DO2I_ALERT)
        current_val = float(v_all[-1]) if len(v_all) else float("nan")
        entries = [
            ("Current DO₂i", "mL/min/m²", current_val, lambda val: val < DO2I_ALERT),
            ("Threshold", "mL/min/m²", DO2I_ALERT, lambda val: False),
            ("Minutes under threshold", "min", stats.minutes_under, lambda val: val > 15.0),
            ("Total AUC deficit", "min·(mL/min/m²)", stats.auc_total, lambda val: stats.auc_total > AUC_TOTAL_ALERT),
            ("Nadir", "mL/min/m²", stats.nadir, lambda val: val < DO2I_ALERT),
            ("Longest episode", "min", stats.longest_episode_min, lambda val: False),
            ("Max single-episode AUC", "min·(mL/min/m²)", stats.max_single_auc,
             lambda val: stats.max_single_auc > AUC_SINGLE_ALERT),
        ]
        y0, dy = 0.92, 0.122
        for i, (label, units, val, is_alert) in enumerate(entries):
            y = y0 - i * dy
            lab = self.ax_left.text(0.02, y, f"{label} ({units})", fontsize=10.5, ha="left", va="top")
            val_str = "—" if (val is None or not math.isfinite(val)) else f"{val:,.2f}"
            color = "red" if (math.isfinite(val) and is_alert(val)) else "black"
            val_txt = self.ax_left.text(
                0.02, y - 0.055, val_str, fontsize=16, fontweight="bold",
                ha="left", va="top", color=color
            )
            self.metric_text_artists.extend([lab, val_txt])

    def close(self):
        self.src.stop()

# -------------------- Main --------------------
def main():
    args = get_args()
    app = App(args)
    try:
        plt.show()
    finally:
        app.close()

if __name__ == "__main__":
    main()