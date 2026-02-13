# DO2-AUC-JECT
Development of Open-Source Real-Time DO₂i Area Under the Curve (AUC) software for Goal-Directed Perfusion using Artificial Intelligence

This project should be published in JECT if all goes as planned.  I hope anyone that accesses it finds it useful to their practice.  If you think you have made a good change, contact me on social media.  You should be able to find me on LinkedIn or Google. search: John Morton ECMO Perfusion

I ran this on a budget laptop with minimal specs: Intel r UHD graphics 600, 4GB RAM, Intel R Celeron R N4120.  It is very slow, but this program is so small that it could run it easily.  A Raspberry Pi with external monitor might be a better option, but I haven't tested this yet.

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