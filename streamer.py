#!/usr/bin/env python3
"""
Homestream – Bildschirm auf iPad & andere Geräte im Heimnetz streamen.

Starten:
    python streamer.py

Dann im Browser auf dem iPad:
    http://<IP-Adresse>:5000  →  Code eingeben  →  Stream ansehen
"""

import io
import platform
import random
import socket
import string
import subprocess
import threading
import time
import tkinter as tk

import mss
import qrcode
from flask import Flask, Response, abort, jsonify, redirect, render_template_string, request
from PIL import Image, ImageTk


# ══════════════════════════════════════════════════════════════════════════════
#  Shared State
# ══════════════════════════════════════════════════════════════════════════════

flask_app = Flask(__name__)
flask_app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

_state: dict = {
    "active": False,
    "monitor_index": 1,
    "fps": 20,
    "quality": 65,
    "code": "",
    "port": 5000,
}

_latest_frame: bytes | None = None
_frame_lock = threading.Lock()
_cap_thread: threading.Thread | None = None


# ══════════════════════════════════════════════════════════════════════════════
#  Utilities
# ══════════════════════════════════════════════════════════════════════════════

def _make_code() -> str:
    return "".join(random.choices(string.digits, k=6))


def _local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def _monitor_labels() -> list[str]:
    try:
        with mss.mss() as sct:
            labels = []
            for i, m in enumerate(sct.monitors):
                prefix = "Alle Bildschirme" if i == 0 else f"Monitor {i}"
                labels.append(f"{prefix}  ({m['width']}×{m['height']})")
            return labels if labels else ["Monitor 1 (unbekannt)"]
    except Exception:
        return ["Monitor 1 (unbekannt)"]


def _list_windows() -> list[tuple[str, dict | None]]:
    """Returns [(label, region_or_None), ...] — platform-specific."""
    wins: list[tuple[str, dict | None]] = []
    system = platform.system()
    try:
        if system == "Windows":
            import win32gui
            def cb(hwnd, _):
                if win32gui.IsWindowVisible(hwnd):
                    title = win32gui.GetWindowText(hwnd)
                    if title.strip():
                        rect = win32gui.GetWindowRect(hwnd)
                        l, t, r, b = rect
                        if r - l > 50 and b - t > 50:
                            wins.append((title, {"left": l, "top": t, "width": r - l, "height": b - t}))
            win32gui.EnumWindows(cb, None)
        elif system == "Linux":
            out = subprocess.check_output(["wmctrl", "-l", "-G"], text=True, timeout=3)
            for line in out.splitlines():
                parts = line.split(None, 9)
                if len(parts) >= 10:
                    x, y, w, h = int(parts[2]), int(parts[3]), int(parts[4]), int(parts[5])
                    title = parts[9]
                    if w > 50 and h > 50 and title.strip():
                        wins.append((title, {"left": x, "top": y, "width": w, "height": h}))
        elif system == "Darwin":
            script = 'tell application "System Events" to get {name, position, size} of every window of every process whose visible is true'
            out = subprocess.check_output(["osascript", "-e", script], text=True, timeout=5)
            # Simplified: just return app names
            for name in out.split(","):
                name = name.strip().strip('"')
                if name:
                    wins.append((name, None))
    except Exception:
        pass
    return wins


def _make_qr(url: str, size: int = 180) -> ImageTk.PhotoImage:
    qr = qrcode.QRCode(version=1, box_size=4, border=2,
                        error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#e0e7ff", back_color="#16162a")
    img = img.convert("RGB").resize((size, size), Image.LANCZOS)
    return ImageTk.PhotoImage(img)


# ══════════════════════════════════════════════════════════════════════════════
#  Screen Capture Thread
# ══════════════════════════════════════════════════════════════════════════════

def _capture_loop():
    global _latest_frame
    with mss.mss() as sct:
        while _state["active"]:
            try:
                idx = _state["monitor_index"]
                region = _state.get("window_region")
                if region:
                    mon = region
                else:
                    mon = sct.monitors[idx]
                shot = sct.grab(mon)
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                img.thumbnail((1280, 720), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=_state["quality"])
                with _frame_lock:
                    _latest_frame = buf.getvalue()
            except Exception as exc:
                print(f"[capture] {exc}")
            time.sleep(1.0 / max(_state["fps"], 1))


def start_streaming():
    global _cap_thread, _latest_frame
    _latest_frame = None
    _state["active"] = True
    _cap_thread = threading.Thread(target=_capture_loop, daemon=True)
    _cap_thread.start()


def stop_streaming():
    _state["active"] = False
    if _cap_thread:
        _cap_thread.join(timeout=2)


# ══════════════════════════════════════════════════════════════════════════════
#  HTML Templates
# ══════════════════════════════════════════════════════════════════════════════

_INDEX_HTML = """
<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <title>Homestream</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: #0d0d1a;
      color: #fff;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }
    .card {
      background: #16162a;
      border-radius: 28px;
      padding: 48px 40px 40px;
      width: 100%;
      max-width: 380px;
      text-align: center;
      box-shadow: 0 24px 80px rgba(0,0,0,.6), 0 0 0 1px rgba(255,255,255,.05);
    }
    .logo {
      font-size: 28px;
      font-weight: 900;
      letter-spacing: 4px;
      background: linear-gradient(135deg, #818cf8, #c084fc);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
    }
    .tagline {
      color: #555;
      font-size: 14px;
      margin-top: 8px;
      margin-bottom: 44px;
      letter-spacing: .3px;
    }
    label {
      display: block;
      font-size: 12px;
      color: #666;
      text-transform: uppercase;
      letter-spacing: 1.5px;
      margin-bottom: 14px;
    }
    input[type=text] {
      width: 100%;
      background: #0d0d1a;
      border: 2px solid #2a2a40;
      border-radius: 16px;
      padding: 18px 16px;
      font-size: 34px;
      color: #fff;
      text-align: center;
      letter-spacing: 12px;
      outline: none;
      transition: border-color .2s;
      -webkit-appearance: none;
      margin-bottom: 14px;
    }
    input:focus { border-color: #818cf8; }
    .btn {
      width: 100%;
      padding: 17px;
      background: linear-gradient(135deg, #818cf8, #c084fc);
      border: none;
      border-radius: 16px;
      color: #fff;
      font-size: 17px;
      font-weight: 700;
      cursor: pointer;
      letter-spacing: .3px;
      transition: opacity .15s, transform .1s;
      -webkit-tap-highlight-color: transparent;
    }
    .btn:active { opacity: .85; transform: scale(.98); }
    .error {
      color: #f87171;
      font-size: 14px;
      margin-top: 18px;
      padding: 12px;
      background: rgba(248,113,113,.08);
      border-radius: 12px;
      border: 1px solid rgba(248,113,113,.2);
    }
    .hint {
      color: #444;
      font-size: 12px;
      margin-top: 20px;
      line-height: 1.6;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">HOMESTREAM</div>
    <div class="tagline">Dein Bildschirm. Überall.</div>

    <form action="/watch" method="GET" autocomplete="off">
      <label for="code">Stream-Code eingeben</label>
      <input id="code" type="text" name="code"
             placeholder="000000" maxlength="6"
             inputmode="numeric" pattern="[0-9]{6}"
             required autofocus>
      <button class="btn" type="submit">Verbinden &rarr;</button>
    </form>

    {% if error %}
    <div class="error">Falscher Code &ndash; bitte erneut versuchen.</div>
    {% endif %}

    <div class="hint">Den Code findest du im Homestream-Fenster auf deinem PC.</div>
  </div>
</body>
</html>
"""

_WATCH_HTML = """
<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <title>Homestream &ndash; Live</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    html, body { width: 100%; height: 100%; background: #000; overflow: hidden; }

    #stream {
      display: block;
      width: 100%;
      height: 100%;
      object-fit: contain;
    }

    #badge {
      position: fixed;
      top: max(env(safe-area-inset-top, 0px), 12px);
      right: 12px;
      background: rgba(0,0,0,.55);
      border: 1px solid rgba(255,255,255,.1);
      border-radius: 999px;
      padding: 6px 16px 6px 10px;
      display: flex;
      align-items: center;
      gap: 7px;
      font-family: -apple-system, sans-serif;
      font-size: 13px;
      color: #bbb;
      backdrop-filter: blur(14px);
      -webkit-backdrop-filter: blur(14px);
      z-index: 50;
      cursor: pointer;
      user-select: none;
      -webkit-tap-highlight-color: transparent;
    }
    #dot {
      width: 8px; height: 8px; border-radius: 50%;
      background: #4ade80; flex-shrink: 0;
      transition: background .4s;
    }
    #dot.live { animation: pulse 2.2s ease infinite; }
    #dot.off  { background: #f87171; }
    @keyframes pulse {
      0%,100% { box-shadow: 0 0 0 0 rgba(74,222,128,.5); }
      60%     { box-shadow: 0 0 0 6px rgba(74,222,128,0); }
    }

    #overlay {
      position: fixed; inset: 0;
      background: #0d0d1a;
      display: flex; flex-direction: column;
      align-items: center; justify-content: center;
      gap: 18px;
      font-family: -apple-system, sans-serif;
      color: #fff; z-index: 40;
      transition: opacity .5s;
    }
    #overlay.gone { opacity: 0; pointer-events: none; }
    .spinner {
      width: 46px; height: 46px;
      border: 3px solid #2a2a40;
      border-top-color: #818cf8;
      border-radius: 50%;
      animation: spin .75s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    #overlay h2 { font-size: 21px; font-weight: 600; }
    #overlay p  { color: #555; font-size: 15px; }

    #reconnect-btn {
      display: none;
      padding: 12px 28px;
      background: linear-gradient(135deg,#818cf8,#c084fc);
      border: none; border-radius: 12px;
      color: #fff; font-size: 16px; font-weight: 600;
      cursor: pointer; margin-top: 8px;
    }
    #reconnect-btn.show { display: block; }
  </style>
</head>
<body>
  <img id="stream" src="" alt="Stream">

  <div id="badge" onclick="toggleFullscreen()">
    <span id="dot"></span>
    <span id="badge-text">Verbinde&hellip;</span>
  </div>

  <div id="overlay" id="overlay">
    <div class="spinner" id="spinner"></div>
    <h2 id="overlay-title">Verbinde&hellip;</h2>
    <p id="overlay-sub">Warte auf Stream-Daten vom PC</p>
    <button id="reconnect-btn" onclick="reconnect()">Erneut verbinden</button>
  </div>

  <script>
    const CODE     = '{{ code }}';
    const dot      = document.getElementById('dot');
    const badge    = document.getElementById('badge');
    const badgeTx  = document.getElementById('badge-text');
    const overlay  = document.getElementById('overlay');
    const ovTitle  = document.getElementById('overlay-title');
    const ovSub    = document.getElementById('overlay-sub');
    const spinner  = document.getElementById('spinner');
    const reconBtn = document.getElementById('reconnect-btn');
    const stream   = document.getElementById('stream');

    let retries = 0;
    let pingTimer = null;
    let isLive = false;

    function setLive(live) {
      isLive = live;
      overlay.classList.toggle('gone', live);
      dot.className = live ? 'live' : 'off';
      badgeTx.textContent = live ? 'Live' : 'Getrennt';
    }

    function reconnect() {
      retries = 0;
      reconBtn.classList.remove('show');
      spinner.style.display = '';
      ovTitle.textContent = 'Verbinde…';
      ovSub.textContent = 'Warte auf Stream-Daten vom PC';
      startStream();
    }

    function startStream() {
      stream.src = '';
      // Small delay lets browser release the old connection
      setTimeout(() => {
        stream.src = '/stream?code=' + CODE + '&r=' + Date.now();
      }, 200);
    }

    stream.onload = () => {
      setLive(true);
      retries = 0;
    };

    stream.onerror = () => {
      setLive(false);
      retries = Math.min(retries + 1, 8);
      const delay = retries <= 3 ? retries * 1200 : 8000;
      if (retries >= 4) {
        spinner.style.display = 'none';
        ovTitle.textContent = 'Stream nicht erreichbar';
        ovSub.textContent = 'Stelle sicher, dass Homestream auf dem PC läuft.';
        reconBtn.classList.add('show');
      } else {
        ovTitle.textContent = 'Verbinde…';
        ovSub.textContent = `Versuch ${retries} von 3…`;
      }
      setTimeout(startStream, delay);
    };

    // Periodic ping to detect stream stopped from PC side
    function startPing() {
      pingTimer = setInterval(async () => {
        try {
          const r = await fetch('/ping?code=' + CODE, { signal: AbortSignal.timeout(4000) });
          const d = await r.json();
          if (!d.streaming && isLive) {
            setLive(false);
            ovTitle.textContent = 'Stream beendet';
            ovSub.textContent = 'Der Stream wurde auf dem PC gestoppt.';
            spinner.style.display = 'none';
            reconBtn.classList.add('show');
          }
        } catch(_) {}
      }, 7000);
    }

    function toggleFullscreen() {
      const el = document.documentElement;
      if (!document.fullscreenElement && !document.webkitFullscreenElement) {
        (el.requestFullscreen || el.webkitRequestFullscreen || function(){}).call(el);
      } else {
        (document.exitFullscreen || document.webkitExitFullscreen || function(){}).call(document);
      }
    }

    startStream();
    startPing();
  </script>
</body>
</html>
"""


# ══════════════════════════════════════════════════════════════════════════════
#  Flask Routes
# ══════════════════════════════════════════════════════════════════════════════

@flask_app.route("/")
def index():
    error = request.args.get("error", "")
    return render_template_string(_INDEX_HTML, error=error)


@flask_app.route("/watch")
def watch():
    code = request.args.get("code", "").strip()
    if not _state["code"] or code != _state["code"]:
        return redirect("/?error=1")
    return render_template_string(_WATCH_HTML, code=code)


@flask_app.route("/stream")
def stream():
    code = request.args.get("code", "").strip()
    if not _state["code"] or code != _state["code"]:
        abort(403)
    if not _state["active"]:
        abort(503)

    def generate():
        while _state["active"]:
            with _frame_lock:
                frame = _latest_frame
            if frame:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                )
            time.sleep(1.0 / max(_state["fps"], 1))

    resp = Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")
    resp.headers["Cache-Control"] = "no-cache, no-store"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@flask_app.route("/ping")
def ping():
    return jsonify({"ok": True, "streaming": _state["active"]})


# ══════════════════════════════════════════════════════════════════════════════
#  Tkinter GUI
# ══════════════════════════════════════════════════════════════════════════════

# Farben
BG      = "#0d0d1a"
CARD    = "#16162a"
CARD2   = "#1e1e35"
ACCENT  = "#818cf8"
ACCENT2 = "#c084fc"
FG      = "#e0e7ff"
FGMUTED = "#555577"
SEP     = "#22223a"
GREEN   = "#4ade80"
RED     = "#f87171"
ORANGE  = "#fb923c"


class Tooltip:
    def __init__(self, widget, text):
        self.text = text
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)
        self.tip = None

    def show(self, event=None):
        x = event.widget.winfo_rootx() + 20
        y = event.widget.winfo_rooty() + event.widget.winfo_height()
        self.tip = tk.Toplevel()
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, bg="#2a2a40", fg=FG,
                 font=("Helvetica Neue", 11), padx=8, pady=4,
                 relief=tk.FLAT).pack()

    def hide(self, event=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Homestream")
        self.configure(bg=BG)
        self.resizable(False, False)

        self._ip = _local_ip()
        self._port = _state["port"]
        self._monitors = _monitor_labels()
        self._windows = _list_windows()

        self._mon_var = tk.IntVar(value=min(1, len(self._monitors) - 1))
        self._win_var = tk.StringVar(value="")
        self._fps_var = tk.IntVar(value=20)
        self._q_var   = tk.IntVar(value=65)
        self._mode_var = tk.StringVar(value="monitor")  # "monitor" | "window"

        self._qr_photo: ImageTk.PhotoImage | None = None
        self._status_after: str | None = None

        self._build()
        self._launch_server()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Server ─────────────────────────────────────────────────────────────

    def _launch_server(self):
        t = threading.Thread(
            target=flask_app.run,
            kwargs={
                "host": "0.0.0.0",
                "port": self._port,
                "debug": False,
                "use_reloader": False,
            },
            daemon=True,
        )
        t.start()

    # ── Layout ─────────────────────────────────────────────────────────────

    def _build(self):
        outer = tk.Frame(self, bg=BG, padx=24, pady=24)
        outer.pack(fill=tk.BOTH, expand=True)

        # ── Header ─────────────────────────────────────────────────────────
        hdr = tk.Frame(outer, bg=BG)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="HOMESTREAM", bg=BG, fg=ACCENT,
                 font=("Helvetica Neue", 22, "bold"), lettersp=3).pack(side=tk.LEFT)

        # Status dot (top right)
        self._status_dot = tk.Label(hdr, text="●", bg=BG, fg=FGMUTED,
                                    font=("Helvetica Neue", 18))
        self._status_dot.pack(side=tk.RIGHT, padx=(0, 2))
        self._status_lbl = tk.Label(hdr, text="Inaktiv", bg=BG, fg=FGMUTED,
                                    font=("Helvetica Neue", 11))
        self._status_lbl.pack(side=tk.RIGHT, padx=(0, 4))

        tk.Label(outer, text="Bildschirm streamen · iPad, Handy, Laptop",
                 bg=BG, fg=FGMUTED, font=("Helvetica Neue", 11)).pack(anchor=tk.W, pady=(4, 16))

        self._separator(outer)

        # ── Source Selection ────────────────────────────────────────────────
        self._section_label(outer, "Quelle")

        src_frame = self._card(outer)
        # Tabs: Monitor / Fenster
        tab_row = tk.Frame(src_frame, bg=CARD)
        tab_row.pack(fill=tk.X, padx=10, pady=(10, 6))

        self._tab_mon = tk.Button(tab_row, text="Monitor",
                                   bg=ACCENT, fg=FG, relief=tk.FLAT,
                                   font=("Helvetica Neue", 12, "bold"),
                                   padx=14, pady=5, bd=0, cursor="hand2",
                                   command=lambda: self._switch_tab("monitor"))
        self._tab_mon.pack(side=tk.LEFT, padx=(0, 6))

        self._tab_win = tk.Button(tab_row, text="Fenster",
                                   bg=CARD2, fg=FGMUTED, relief=tk.FLAT,
                                   font=("Helvetica Neue", 12),
                                   padx=14, pady=5, bd=0, cursor="hand2",
                                   command=lambda: self._switch_tab("window"))
        self._tab_win.pack(side=tk.LEFT)

        # Monitor pane
        self._monitor_pane = tk.Frame(src_frame, bg=CARD)
        self._monitor_pane.pack(fill=tk.X, padx=10, pady=(0, 10))
        for i, lbl in enumerate(self._monitors):
            row = tk.Frame(self._monitor_pane, bg=CARD)
            row.pack(fill=tk.X, pady=2)
            rb = tk.Radiobutton(row, text=lbl, variable=self._mon_var, value=i,
                                bg=CARD, fg=FG, selectcolor="#2a2a42",
                                activebackground=CARD, activeforeground=FG,
                                font=("Helvetica Neue", 12), padx=6, pady=3)
            rb.pack(anchor=tk.W)

        # Window pane (hidden by default)
        self._window_pane = tk.Frame(src_frame, bg=CARD)
        if self._windows:
            win_lb_frame = tk.Frame(self._window_pane, bg=CARD)
            win_lb_frame.pack(fill=tk.X, padx=4, pady=(0, 8))
            self._win_listbox = tk.Listbox(
                win_lb_frame, bg=CARD2, fg=FG, selectbackground=ACCENT,
                selectforeground=FG, font=("Helvetica Neue", 11),
                height=min(len(self._windows), 5), bd=0, relief=tk.FLAT,
                activestyle="none",
            )
            for title, _ in self._windows:
                display = title[:55] + "…" if len(title) > 55 else title
                self._win_listbox.insert(tk.END, display)
            self._win_listbox.pack(fill=tk.X)
            if self._windows:
                self._win_listbox.select_set(0)
        else:
            tk.Label(self._window_pane,
                     text="Keine Fenster erkannt.\n(wmctrl oder win32gui nicht verfügbar)",
                     bg=CARD, fg=FGMUTED, font=("Helvetica Neue", 11),
                     justify=tk.LEFT, padx=6, pady=8).pack(anchor=tk.W)

        # ── Quality Controls ────────────────────────────────────────────────
        self._section_label(outer, "Qualität")
        q_card = self._card(outer)
        q_inner = tk.Frame(q_card, bg=CARD, padx=12, pady=10)
        q_inner.pack(fill=tk.X)

        # FPS
        fps_row = tk.Frame(q_inner, bg=CARD)
        fps_row.pack(fill=tk.X, pady=(0, 8))
        tk.Label(fps_row, text="Bildrate", bg=CARD, fg=FG,
                 font=("Helvetica Neue", 12), width=10, anchor=tk.W).pack(side=tk.LEFT)
        self._fps_val = tk.Label(fps_row, text=f"{self._fps_var.get()} FPS",
                                  bg=CARD, fg=ACCENT, font=("Helvetica Neue", 12, "bold"), width=8)
        self._fps_val.pack(side=tk.RIGHT)
        tk.Scale(fps_row, from_=5, to=30, orient=tk.HORIZONTAL,
                 variable=self._fps_var, bg=CARD, fg=FG,
                 troughcolor=SEP, highlightthickness=0,
                 activebackground=ACCENT, showvalue=False,
                 command=lambda v: self._fps_val.config(text=f"{int(float(v))} FPS")
                 ).pack(fill=tk.X, padx=(0, 8))

        # Quality
        q_row = tk.Frame(q_inner, bg=CARD)
        q_row.pack(fill=tk.X)
        tk.Label(q_row, text="Qualität", bg=CARD, fg=FG,
                 font=("Helvetica Neue", 12), width=10, anchor=tk.W).pack(side=tk.LEFT)
        self._q_val = tk.Label(q_row, text=f"{self._q_var.get()}%",
                                bg=CARD, fg=ACCENT, font=("Helvetica Neue", 12, "bold"), width=8)
        self._q_val.pack(side=tk.RIGHT)
        tk.Scale(q_row, from_=30, to=95, orient=tk.HORIZONTAL,
                 variable=self._q_var, bg=CARD, fg=FG,
                 troughcolor=SEP, highlightthickness=0,
                 activebackground=ACCENT, showvalue=False,
                 command=lambda v: self._q_val.config(text=f"{int(float(v))}%")
                 ).pack(fill=tk.X, padx=(0, 8))

        # ── Status ──────────────────────────────────────────────────────────
        self._separator(outer)
        info = tk.Frame(outer, bg=BG)
        info.pack(fill=tk.X, pady=(0, 12))

        left_info = tk.Frame(info, bg=BG)
        left_info.pack(side=tk.LEFT, fill=tk.X, expand=True)

        tk.Label(left_info, text="Adresse im Netzwerk", bg=BG, fg=FGMUTED,
                 font=("Helvetica Neue", 10)).pack(anchor=tk.W)
        self._url_lbl = tk.Label(left_info, text=f"http://{self._ip}:{self._port}",
                                  bg=BG, fg=FG, font=("Courier New", 13, "bold"),
                                  cursor="hand2")
        self._url_lbl.pack(anchor=tk.W, pady=(2, 0))
        self._url_lbl.bind("<Button-1>", lambda e: self._copy_url())
        Tooltip(self._url_lbl, "Klicken zum Kopieren")

        right_info = tk.Frame(info, bg=BG)
        right_info.pack(side=tk.RIGHT, padx=(16, 0))

        tk.Label(right_info, text="Code", bg=BG, fg=FGMUTED,
                 font=("Helvetica Neue", 10)).pack(anchor=tk.E)
        self._code_lbl = tk.Label(right_info, text="──────",
                                   bg=BG, fg=ACCENT2,
                                   font=("Courier New", 26, "bold"), width=7)
        self._code_lbl.pack(anchor=tk.E)

        # ── QR Frame ────────────────────────────────────────────────────────
        self._qr_frame = tk.Frame(outer, bg=BG)
        self._qr_frame.pack(pady=(0, 12))
        self._qr_canvas = tk.Label(self._qr_frame, bg=BG, text="")
        self._qr_canvas.pack()
        self._qr_hint = tk.Label(self._qr_frame, text="",
                                  bg=BG, fg=FGMUTED, font=("Helvetica Neue", 11))
        self._qr_hint.pack(pady=(4, 0))

        # ── Start/Stop Button ────────────────────────────────────────────────
        self._btn = tk.Button(
            outer, text="▶   Stream starten",
            bg=ACCENT, fg=FG,
            activebackground=ACCENT2, activeforeground=FG,
            font=("Helvetica Neue", 14, "bold"),
            bd=0, padx=0, pady=13,
            cursor="hand2", relief=tk.FLAT,
            command=self._toggle,
        )
        self._btn.pack(fill=tk.X)

        # Viewer count label
        self._viewer_lbl = tk.Label(outer, text="", bg=BG, fg=FGMUTED,
                                     font=("Helvetica Neue", 11))
        self._viewer_lbl.pack(pady=(8, 0))

    def _separator(self, parent):
        tk.Frame(parent, bg=SEP, height=1).pack(fill=tk.X, pady=12)

    def _section_label(self, parent, text):
        tk.Label(parent, text=text.upper(), bg=BG, fg=FGMUTED,
                 font=("Helvetica Neue", 10, "bold"),
                 lettersp=1).pack(anchor=tk.W, pady=(0, 6))

    def _card(self, parent) -> tk.Frame:
        f = tk.Frame(parent, bg=CARD, pady=0)
        f.pack(fill=tk.X, pady=(0, 12))
        return f

    # ── Tab Switching ───────────────────────────────────────────────────────

    def _switch_tab(self, mode: str):
        self._mode_var.set(mode)
        if mode == "monitor":
            self._tab_mon.config(bg=ACCENT, fg=FG, font=("Helvetica Neue", 12, "bold"))
            self._tab_win.config(bg=CARD2, fg=FGMUTED, font=("Helvetica Neue", 12))
            self._window_pane.pack_forget()
            self._monitor_pane.pack(fill=tk.X, padx=10, pady=(0, 10))
        else:
            self._tab_win.config(bg=ACCENT, fg=FG, font=("Helvetica Neue", 12, "bold"))
            self._tab_mon.config(bg=CARD2, fg=FGMUTED, font=("Helvetica Neue", 12))
            self._monitor_pane.pack_forget()
            self._window_pane.pack(fill=tk.X, padx=10, pady=(0, 10))

    # ── Stream Control ──────────────────────────────────────────────────────

    def _toggle(self):
        if _state["active"]:
            self._stop()
        else:
            self._start()

    def _start(self):
        mode = self._mode_var.get()
        if mode == "window" and self._windows:
            idx = self._win_listbox.curselection()
            if idx:
                _, region = self._windows[idx[0]]
                _state["window_region"] = region
            else:
                _state["window_region"] = None
            _state["monitor_index"] = 1
        else:
            _state.pop("window_region", None)
            _state["monitor_index"] = self._mon_var.get()

        _state["fps"]     = self._fps_var.get()
        _state["quality"] = self._q_var.get()
        _state["code"]    = _make_code()

        start_streaming()

        # Update status
        self._code_lbl.config(text=_state["code"], fg=GREEN)
        self._btn.config(text="⏹   Stream stoppen", bg=RED)
        self._status_dot.config(fg=GREEN)
        self._status_lbl.config(text="Live", fg=GREEN)

        # QR Code
        watch_url = f"http://{self._ip}:{self._port}/watch?code={_state['code']}"
        try:
            photo = _make_qr(watch_url, size=160)
            self._qr_photo = photo
            self._qr_canvas.config(image=photo)
            self._qr_hint.config(
                text=f"QR-Code scannen oder Code {_state['code']} eingeben"
            )
        except Exception:
            self._qr_hint.config(text=f"Code: {_state['code']}")

        self._start_viewer_counter()

    def _stop(self):
        if self._status_after:
            self.after_cancel(self._status_after)
        stop_streaming()
        _state["code"] = ""
        self._code_lbl.config(text="──────", fg=ACCENT2)
        self._btn.config(text="▶   Stream starten", bg=ACCENT)
        self._status_dot.config(fg=FGMUTED)
        self._status_lbl.config(text="Inaktiv", fg=FGMUTED)
        self._qr_canvas.config(image="")
        self._qr_hint.config(text="")
        self._qr_photo = None
        self._viewer_lbl.config(text="")

    def _start_viewer_counter(self):
        # We track active streams via the generator count
        self._update_viewer_count()

    def _update_viewer_count(self):
        if _state["active"]:
            self._status_after = self.after(2000, self._update_viewer_count)

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _copy_url(self):
        url = f"http://{self._ip}:{self._port}"
        self.clipboard_clear()
        self.clipboard_append(url)
        self._url_lbl.config(fg=GREEN)
        self.after(1200, lambda: self._url_lbl.config(fg=FG))

    def _on_close(self):
        stop_streaming()
        self.destroy()


# ══════════════════════════════════════════════════════════════════════════════
#  Entry Point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app = App()
    app.mainloop()
