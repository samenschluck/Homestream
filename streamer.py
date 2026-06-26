#!/usr/bin/env python3
"""
Homestream – Bildschirm auf iPad & andere Geräte streamen.
Funktioniert im Heimnetz UND von überall über das Internet (Cloudflare/ngrok).

Starten:
    python streamer.py          (Windows)
    python3 streamer.py         (macOS / Linux)
    oder einfach start.bat / start.sh doppelklicken
"""

import io
import json
import os
import platform
import random
import re
import shutil
import socket
import string
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
from tkinter import messagebox

import mss
import qrcode
from flask import Flask, Response, abort, jsonify, redirect, render_template_string, request
from PIL import Image, ImageTk


# ══════════════════════════════════════════════════════════════════════════════
#  Pfade & Config
# ══════════════════════════════════════════════════════════════════════════════

_BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
_CONFIG_FILE = os.path.join(_BASE_DIR, "config.json")


def _load_cfg() -> dict:
    try:
        with open(_CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cfg(data: dict):
    try:
        with open(_CONFIG_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


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
_frame_lock   = threading.Lock()
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
            return labels or ["Monitor 1"]
    except Exception:
        return ["Monitor 1"]


def _list_windows() -> list[tuple[str, dict | None]]:
    wins: list[tuple[str, dict | None]] = []
    system = platform.system()
    try:
        if system == "Windows":
            import win32gui
            def cb(hwnd, _):
                if win32gui.IsWindowVisible(hwnd):
                    title = win32gui.GetWindowText(hwnd)
                    if title.strip():
                        l, t, r, b = win32gui.GetWindowRect(hwnd)
                        if r - l > 50 and b - t > 50:
                            wins.append((title, {"left": l, "top": t, "width": r-l, "height": b-t}))
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
    except Exception:
        pass
    return wins


def _make_qr(url: str, size: int = 170) -> ImageTk.PhotoImage:
    qr = qrcode.QRCode(version=1, box_size=4, border=2,
                        error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#e0e7ff", back_color="#16162a")
    img = img.convert("RGB").resize((size, size), Image.LANCZOS)
    return ImageTk.PhotoImage(img)


# ══════════════════════════════════════════════════════════════════════════════
#  Tunnel Manager
# ══════════════════════════════════════════════════════════════════════════════

class TunnelManager:
    """Verwaltet Cloudflare Quick Tunnels oder ngrok für Internetzugriff."""

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._url: str | None = None

    @property
    def url(self) -> str | None:
        return self._url

    # ── Cloudflare ──────────────────────────────────────────────────────────

    def start_cloudflare(self, port: int, on_ready, on_error):
        """Startet einen Cloudflare Quick Tunnel (kein Account nötig)."""
        def run():
            cf_bin = self._get_cloudflared(on_error)
            if not cf_bin:
                return
            try:
                self._proc = subprocess.Popen(
                    [cf_bin, "tunnel", "--url", f"http://localhost:{port}",
                     "--no-autoupdate"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                url_pattern = re.compile(r"https://[a-z0-9\-]+\.trycloudflare\.com")
                for line in self._proc.stdout:
                    m = url_pattern.search(line)
                    if m:
                        self._url = m.group(0)
                        on_ready(self._url)
                        break
                self._proc.wait()
            except Exception as exc:
                on_error(f"Cloudflare-Fehler: {exc}")

        threading.Thread(target=run, daemon=True).start()

    def _get_cloudflared(self, on_error) -> str | None:
        cf = shutil.which("cloudflared")
        if cf:
            return cf

        system = platform.system()
        ext = ".exe" if system == "Windows" else ""
        local = os.path.join(_BASE_DIR, f"cloudflared{ext}")
        if os.path.exists(local):
            return local

        return self._download_cloudflared(local, system, on_error)

    def _download_cloudflared(self, dest: str, system: str, on_error) -> str | None:
        machine = platform.machine().lower()
        arch = "amd64" if machine in ("x86_64", "amd64") else "arm64"
        urls = {
            "Windows": f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-{arch}.exe",
            "Darwin":  f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-{arch}",
            "Linux":   f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{arch}",
        }
        url = urls.get(system)
        if not url:
            on_error("Unbekanntes Betriebssystem für Cloudflare-Download.")
            return None
        try:
            urllib.request.urlretrieve(url, dest)
            if system != "Windows":
                os.chmod(dest, 0o755)
            return dest
        except Exception as exc:
            on_error(f"Download fehlgeschlagen: {exc}")
            return None

    # ── ngrok ───────────────────────────────────────────────────────────────

    def start_ngrok(self, port: int, token: str, on_ready, on_error):
        """Startet einen ngrok-Tunnel (kostenloser Account erforderlich)."""
        def run():
            try:
                from pyngrok import ngrok, conf
                if token:
                    conf.get_default().auth_token = token
                tunnel = ngrok.connect(port, "http")
                self._url = tunnel.public_url.replace("http://", "https://")
                on_ready(self._url)
            except Exception as exc:
                on_error(f"ngrok-Fehler: {exc}")

        threading.Thread(target=run, daemon=True).start()

    # ── Stop ────────────────────────────────────────────────────────────────

    def stop(self):
        self._url = None
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None
        try:
            from pyngrok import ngrok
            ngrok.kill()
        except Exception:
            pass


_tunnel = TunnelManager()


# ══════════════════════════════════════════════════════════════════════════════
#  Screen Capture
# ══════════════════════════════════════════════════════════════════════════════

def _capture_loop():
    global _latest_frame
    with mss.mss() as sct:
        while _state["active"]:
            try:
                region = _state.get("window_region")
                mon    = region if region else sct.monitors[_state["monitor_index"]]
                shot   = sct.grab(mon)
                img    = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
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
    .tagline { color: #555; font-size: 14px; margin-top: 8px; margin-bottom: 44px; }
    label { display: block; font-size: 12px; color: #666; text-transform: uppercase; letter-spacing: 1.5px; margin-bottom: 14px; }
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
      transition: opacity .15s, transform .1s;
      -webkit-tap-highlight-color: transparent;
    }
    .btn:active { opacity: .85; transform: scale(.98); }
    .error {
      color: #f87171; font-size: 14px; margin-top: 18px;
      padding: 12px; background: rgba(248,113,113,.08);
      border-radius: 12px; border: 1px solid rgba(248,113,113,.2);
    }
    .hint { color: #444; font-size: 12px; margin-top: 20px; line-height: 1.6; }
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
             inputmode="numeric" pattern="[0-9]{6}" required autofocus>
      <button class="btn" type="submit">Verbinden &rarr;</button>
    </form>
    {% if error %}
    <div class="error">Falscher Code &ndash; bitte erneut versuchen.</div>
    {% endif %}
    <div class="hint">Den Code findest du im Homestream-Fenster auf dem PC.</div>
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
    #stream { display: block; width: 100%; height: 100%; object-fit: contain; }
    #badge {
      position: fixed;
      top: max(env(safe-area-inset-top, 0px), 12px);
      right: 12px;
      background: rgba(0,0,0,.55);
      border: 1px solid rgba(255,255,255,.1);
      border-radius: 999px;
      padding: 6px 16px 6px 10px;
      display: flex; align-items: center; gap: 7px;
      font-family: -apple-system, sans-serif;
      font-size: 13px; color: #bbb;
      backdrop-filter: blur(14px);
      -webkit-backdrop-filter: blur(14px);
      z-index: 50; cursor: pointer;
      user-select: none; -webkit-tap-highlight-color: transparent;
    }
    #dot {
      width: 8px; height: 8px; border-radius: 50%;
      background: #4ade80; flex-shrink: 0; transition: background .4s;
    }
    #dot.live { animation: pulse 2.2s ease infinite; }
    #dot.off  { background: #f87171; }
    @keyframes pulse {
      0%,100% { box-shadow: 0 0 0 0 rgba(74,222,128,.5); }
      60%      { box-shadow: 0 0 0 6px rgba(74,222,128,0); }
    }
    #overlay {
      position: fixed; inset: 0; background: #0d0d1a;
      display: flex; flex-direction: column;
      align-items: center; justify-content: center;
      gap: 18px; font-family: -apple-system, sans-serif;
      color: #fff; z-index: 40; transition: opacity .5s;
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
      display: none; padding: 12px 28px;
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
  <div id="overlay">
    <div class="spinner" id="spinner"></div>
    <h2 id="ov-title">Verbinde&hellip;</h2>
    <p id="ov-sub">Warte auf Stream-Daten</p>
    <button id="reconnect-btn" onclick="reconnect()">Erneut verbinden</button>
  </div>
  <script>
    const CODE     = '{{ code }}';
    const dot      = document.getElementById('dot');
    const badgeTx  = document.getElementById('badge-text');
    const overlay  = document.getElementById('overlay');
    const ovTitle  = document.getElementById('ov-title');
    const ovSub    = document.getElementById('ov-sub');
    const spinner  = document.getElementById('spinner');
    const reconBtn = document.getElementById('reconnect-btn');
    const stream   = document.getElementById('stream');
    let retries = 0;
    let isLive  = false;

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
      ovSub.textContent   = 'Warte auf Stream-Daten';
      startStream();
    }

    function startStream() {
      stream.src = '';
      setTimeout(() => { stream.src = '/stream?code=' + CODE + '&r=' + Date.now(); }, 150);
    }

    stream.onload = () => { setLive(true); retries = 0; };
    stream.onerror = () => {
      setLive(false);
      retries = Math.min(retries + 1, 8);
      const delay = retries <= 3 ? retries * 1500 : 10000;
      if (retries >= 4) {
        spinner.style.display = 'none';
        ovTitle.textContent = 'Stream nicht erreichbar';
        ovSub.textContent   = 'Stelle sicher, dass Homestream auf dem PC läuft.';
        reconBtn.classList.add('show');
      } else {
        ovTitle.textContent = 'Verbinde…';
        ovSub.textContent   = 'Versuch ' + retries + ' von 3…';
      }
      setTimeout(startStream, delay);
    };

    // Ping – erkennt wenn Stream auf PC-Seite gestoppt wird
    setInterval(async () => {
      try {
        const r = await fetch('/ping?code=' + CODE, { signal: AbortSignal.timeout(4000) });
        const d = await r.json();
        if (!d.streaming && isLive) {
          setLive(false);
          spinner.style.display = 'none';
          ovTitle.textContent = 'Stream beendet';
          ovSub.textContent   = 'Der Stream wurde auf dem PC gestoppt.';
          reconBtn.classList.add('show');
        }
      } catch(_) {}
    }, 7000);

    function toggleFullscreen() {
      const el = document.documentElement;
      if (!document.fullscreenElement && !document.webkitFullscreenElement)
        (el.requestFullscreen || el.webkitRequestFullscreen || function(){}).call(el);
      else
        (document.exitFullscreen || document.webkitExitFullscreen || function(){}).call(document);
    }

    startStream();
  </script>
</body>
</html>
"""


# ══════════════════════════════════════════════════════════════════════════════
#  Flask Routes
# ══════════════════════════════════════════════════════════════════════════════

@flask_app.route("/")
def index():
    return render_template_string(_INDEX_HTML, error=request.args.get("error", ""))

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
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
            time.sleep(1.0 / max(_state["fps"], 1))
    resp = Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")
    resp.headers["Cache-Control"]    = "no-cache, no-store"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp

@flask_app.route("/ping")
def ping():
    return jsonify({"ok": True, "streaming": _state["active"]})


# ══════════════════════════════════════════════════════════════════════════════
#  Farben / Design
# ══════════════════════════════════════════════════════════════════════════════

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
YELLOW  = "#fbbf24"


# ══════════════════════════════════════════════════════════════════════════════
#  Tkinter GUI
# ══════════════════════════════════════════════════════════════════════════════

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Homestream")
        self.configure(bg=BG)
        self.resizable(False, False)

        self._cfg      = _load_cfg()
        self._ip       = _local_ip()
        self._port     = _state["port"]
        self._monitors = _monitor_labels()
        self._windows  = _list_windows()

        self._mon_var    = tk.IntVar(value=min(1, len(self._monitors) - 1))
        self._fps_var    = tk.IntVar(value=self._cfg.get("fps", 20))
        self._q_var      = tk.IntVar(value=self._cfg.get("quality", 65))
        self._mode_var   = tk.StringVar(value="monitor")
        self._tunnel_var = tk.StringVar(value=self._cfg.get("tunnel", "none"))
        self._token_var  = tk.StringVar(value=self._cfg.get("ngrok_token", ""))

        self._qr_photo: ImageTk.PhotoImage | None = None
        self._after_id: str | None = None

        self._build()
        self._launch_server()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Server ─────────────────────────────────────────────────────────────

    def _launch_server(self):
        threading.Thread(
            target=flask_app.run,
            kwargs={"host": "0.0.0.0", "port": self._port,
                    "debug": False, "use_reloader": False},
            daemon=True,
        ).start()

    # ── Build UI ───────────────────────────────────────────────────────────

    def _build(self):
        outer = tk.Frame(self, bg=BG, padx=22, pady=22)
        outer.pack(fill=tk.BOTH, expand=True)

        # Header
        hdr = tk.Frame(outer, bg=BG)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="HOMESTREAM", bg=BG, fg=ACCENT,
                 font=("Helvetica Neue", 21, "bold")).pack(side=tk.LEFT)
        self._dot = tk.Label(hdr, text="●", bg=BG, fg=FGMUTED, font=("Helvetica Neue", 16))
        self._dot.pack(side=tk.RIGHT, padx=(0,2))
        self._status_lbl = tk.Label(hdr, text="Inaktiv", bg=BG, fg=FGMUTED,
                                     font=("Helvetica Neue", 11))
        self._status_lbl.pack(side=tk.RIGHT, padx=(0,4))

        tk.Label(outer, text="Bildschirm streamen · iPad, Handy, Laptop",
                 bg=BG, fg=FGMUTED, font=("Helvetica Neue", 11)).pack(anchor=tk.W, pady=(4, 14))

        self._sep(outer)

        # ─ Quelle ─
        self._slbl(outer, "Quelle")
        src = self._card(outer)
        tabs = tk.Frame(src, bg=CARD)
        tabs.pack(fill=tk.X, padx=10, pady=(10, 6))
        self._tab_mon = tk.Button(tabs, text="Monitor", bg=ACCENT, fg=FG,
                                   relief=tk.FLAT, font=("Helvetica Neue", 12, "bold"),
                                   padx=14, pady=5, bd=0, cursor="hand2",
                                   command=lambda: self._switch_tab("monitor"))
        self._tab_mon.pack(side=tk.LEFT, padx=(0, 6))
        self._tab_win = tk.Button(tabs, text="Fenster", bg=CARD2, fg=FGMUTED,
                                   relief=tk.FLAT, font=("Helvetica Neue", 12),
                                   padx=14, pady=5, bd=0, cursor="hand2",
                                   command=lambda: self._switch_tab("window"))
        self._tab_win.pack(side=tk.LEFT)

        # Monitor pane
        self._mon_pane = tk.Frame(src, bg=CARD)
        self._mon_pane.pack(fill=tk.X, padx=10, pady=(0, 10))
        for i, lbl in enumerate(self._monitors):
            tk.Radiobutton(self._mon_pane, text=lbl, variable=self._mon_var, value=i,
                           bg=CARD, fg=FG, selectcolor="#2a2a42",
                           activebackground=CARD, activeforeground=FG,
                           font=("Helvetica Neue", 12), padx=6, pady=3
                           ).pack(anchor=tk.W)

        # Window pane
        self._win_pane = tk.Frame(src, bg=CARD)
        if self._windows:
            self._win_lb = tk.Listbox(
                self._win_pane, bg=CARD2, fg=FG, selectbackground=ACCENT,
                selectforeground=FG, font=("Helvetica Neue", 11),
                height=min(len(self._windows), 5), bd=0, relief=tk.FLAT,
                activestyle="none",
            )
            for title, _ in self._windows:
                self._win_lb.insert(tk.END, title[:58] + ("…" if len(title)>58 else ""))
            self._win_lb.pack(fill=tk.X, padx=4, pady=(0, 8))
            self._win_lb.select_set(0)
        else:
            tk.Label(self._win_pane,
                     text="Keine Fenster erkannt (wmctrl/win32gui fehlt).",
                     bg=CARD, fg=FGMUTED, font=("Helvetica Neue", 11),
                     padx=6, pady=8).pack(anchor=tk.W)

        # ─ Qualität ─
        self._slbl(outer, "Qualität")
        qc = self._card(outer)
        qi = tk.Frame(qc, bg=CARD, padx=12, pady=10)
        qi.pack(fill=tk.X)

        self._fps_lbl = tk.Label(qi, text=f"{self._fps_var.get()} FPS",
                                  bg=CARD, fg=ACCENT, font=("Helvetica Neue", 12, "bold"), width=8)
        self._make_slider(qi, "Bildrate", 5, 30, self._fps_var, self._fps_lbl, " FPS")

        self._q_lbl = tk.Label(qi, text=f"{self._q_var.get()}%",
                                bg=CARD, fg=ACCENT, font=("Helvetica Neue", 12, "bold"), width=8)
        self._make_slider(qi, "Qualität", 30, 95, self._q_var, self._q_lbl, "%")

        # ─ Internetzugriff ─
        self._sep(outer)
        self._slbl(outer, "Internetzugriff")

        tunnel_card = self._card(outer)
        ti = tk.Frame(tunnel_card, bg=CARD, padx=12, pady=10)
        ti.pack(fill=tk.X)

        # Info-Zeile
        tk.Label(ti, text="Damit der Stream von überall erreichbar ist (z.B. unterwegs),",
                 bg=CARD, fg=FGMUTED, font=("Helvetica Neue", 10),
                 justify=tk.LEFT).pack(anchor=tk.W)
        tk.Label(ti, text="wähle einen Tunnel-Anbieter.",
                 bg=CARD, fg=FGMUTED, font=("Helvetica Neue", 10),
                 justify=tk.LEFT).pack(anchor=tk.W, pady=(0, 8))

        opts = [
            ("none",       "Kein Tunnel  (nur Heimnetz)"),
            ("cloudflare", "Cloudflare Quick Tunnel  ✦ Empfohlen – kein Account nötig"),
            ("ngrok",      "ngrok  (kostenloser Account erforderlich)"),
        ]
        for val, txt in opts:
            tk.Radiobutton(ti, text=txt, variable=self._tunnel_var, value=val,
                           bg=CARD, fg=FG, selectcolor="#2a2a42",
                           activebackground=CARD, activeforeground=FG,
                           font=("Helvetica Neue", 12), padx=6, pady=2,
                           command=self._on_tunnel_change,
                           ).pack(anchor=tk.W)

        # ngrok-Token-Feld (initially hidden)
        self._token_frame = tk.Frame(ti, bg=CARD)
        tk.Label(self._token_frame, text="ngrok Auth-Token:",
                 bg=CARD, fg=FG, font=("Helvetica Neue", 11)).pack(anchor=tk.W, pady=(8, 3))
        self._token_entry = tk.Entry(self._token_frame, textvariable=self._token_var,
                                      bg=CARD2, fg=FG, insertbackground=FG,
                                      font=("Courier New", 11), bd=0, relief=tk.FLAT,
                                      width=36)
        self._token_entry.pack(fill=tk.X, ipady=6, padx=2)
        tk.Label(self._token_frame,
                 text="Token unter ngrok.com/dashboard/get-started/your-authtoken",
                 bg=CARD, fg=FGMUTED, font=("Helvetica Neue", 10)).pack(anchor=tk.W, pady=(3, 0))
        self._on_tunnel_change()  # show/hide token field

        # ─ Status ─
        self._sep(outer)
        info = tk.Frame(outer, bg=BG)
        info.pack(fill=tk.X, pady=(0, 10))

        left = tk.Frame(info, bg=BG)
        left.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(left, text="Adresse", bg=BG, fg=FGMUTED,
                 font=("Helvetica Neue", 10)).pack(anchor=tk.W)
        self._url_lbl = tk.Label(left, text=f"http://{self._ip}:{self._port}",
                                  bg=BG, fg=FG, font=("Courier New", 12, "bold"),
                                  cursor="hand2")
        self._url_lbl.pack(anchor=tk.W, pady=(2, 0))
        self._url_lbl.bind("<Button-1>", lambda _: self._copy_url())

        right = tk.Frame(info, bg=BG)
        right.pack(side=tk.RIGHT)
        tk.Label(right, text="Code", bg=BG, fg=FGMUTED,
                 font=("Helvetica Neue", 10)).pack(anchor=tk.E)
        self._code_lbl = tk.Label(right, text="──────", bg=BG, fg=ACCENT2,
                                   font=("Courier New", 26, "bold"), width=7)
        self._code_lbl.pack(anchor=tk.E)

        # Tunnel-Status
        self._tunnel_status = tk.Label(outer, text="", bg=BG, fg=FGMUTED,
                                        font=("Helvetica Neue", 11))
        self._tunnel_status.pack(anchor=tk.W, pady=(0, 6))

        # QR code
        self._qr_frame = tk.Frame(outer, bg=BG)
        self._qr_frame.pack(pady=(0, 10))
        self._qr_lbl = tk.Label(self._qr_frame, bg=BG, text="")
        self._qr_lbl.pack()
        self._qr_hint = tk.Label(self._qr_frame, text="", bg=BG, fg=FGMUTED,
                                  font=("Helvetica Neue", 11))
        self._qr_hint.pack(pady=(4, 0))

        # Start/Stop
        self._btn = tk.Button(
            outer, text="▶   Stream starten",
            bg=ACCENT, fg=FG, activebackground=ACCENT2, activeforeground=FG,
            font=("Helvetica Neue", 14, "bold"),
            bd=0, pady=13, cursor="hand2", relief=tk.FLAT,
            command=self._toggle,
        )
        self._btn.pack(fill=tk.X)

    # ── Helpers ────────────────────────────────────────────────────────────

    def _sep(self, p):
        tk.Frame(p, bg=SEP, height=1).pack(fill=tk.X, pady=12)

    def _slbl(self, p, text):
        tk.Label(p, text=text.upper(), bg=BG, fg=FGMUTED,
                 font=("Helvetica Neue", 10, "bold")).pack(anchor=tk.W, pady=(0, 6))

    def _card(self, p) -> tk.Frame:
        f = tk.Frame(p, bg=CARD)
        f.pack(fill=tk.X, pady=(0, 10))
        return f

    def _make_slider(self, parent, label, lo, hi, var, val_lbl, suffix):
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill=tk.X, pady=(0, 8))
        tk.Label(row, text=label, bg=CARD, fg=FG,
                 font=("Helvetica Neue", 12), width=10, anchor=tk.W).pack(side=tk.LEFT)
        val_lbl.pack(side=tk.RIGHT)
        tk.Scale(row, from_=lo, to=hi, orient=tk.HORIZONTAL, variable=var,
                 bg=CARD, fg=FG, troughcolor=SEP, highlightthickness=0,
                 activebackground=ACCENT, showvalue=False,
                 command=lambda v: val_lbl.config(text=f"{int(float(v))}{suffix}")
                 ).pack(fill=tk.X, padx=(0, 8))

    def _switch_tab(self, mode: str):
        self._mode_var.set(mode)
        if mode == "monitor":
            self._tab_mon.config(bg=ACCENT, fg=FG, font=("Helvetica Neue", 12, "bold"))
            self._tab_win.config(bg=CARD2, fg=FGMUTED, font=("Helvetica Neue", 12))
            self._win_pane.pack_forget()
            self._mon_pane.pack(fill=tk.X, padx=10, pady=(0, 10))
        else:
            self._tab_win.config(bg=ACCENT, fg=FG, font=("Helvetica Neue", 12, "bold"))
            self._tab_mon.config(bg=CARD2, fg=FGMUTED, font=("Helvetica Neue", 12))
            self._mon_pane.pack_forget()
            self._win_pane.pack(fill=tk.X, padx=10, pady=(0, 10))

    def _on_tunnel_change(self, *_):
        if self._tunnel_var.get() == "ngrok":
            self._token_frame.pack(fill=tk.X, pady=(4, 4))
        else:
            self._token_frame.pack_forget()

    def _copy_url(self):
        self.clipboard_clear()
        self.clipboard_append(self._url_lbl.cget("text"))
        self._url_lbl.config(fg=GREEN)
        self.after(1400, lambda: self._url_lbl.config(fg=FG))

    # ── Stream Control ─────────────────────────────────────────────────────

    def _toggle(self):
        if _state["active"]:
            self._stop()
        else:
            self._start()

    def _start(self):
        # Source
        if self._mode_var.get() == "window" and self._windows and hasattr(self, "_win_lb"):
            sel = self._win_lb.curselection()
            if sel:
                _, region = self._windows[sel[0]]
                _state["window_region"] = region
            else:
                _state.pop("window_region", None)
                _state["monitor_index"] = self._mon_var.get()
        else:
            _state.pop("window_region", None)
            _state["monitor_index"] = self._mon_var.get()

        _state["fps"]     = self._fps_var.get()
        _state["quality"] = self._q_var.get()
        _state["code"]    = _make_code()

        start_streaming()

        self._code_lbl.config(text=_state["code"], fg=GREEN)
        self._btn.config(text="⏹   Stream stoppen", bg=RED)
        self._dot.config(fg=GREEN)
        self._status_lbl.config(text="Live", fg=GREEN)

        # Save prefs
        _save_cfg({
            "fps": _state["fps"],
            "quality": _state["quality"],
            "tunnel": self._tunnel_var.get(),
            "ngrok_token": self._token_var.get(),
        })

        # Start tunnel or show local info immediately
        choice = self._tunnel_var.get()
        if choice == "cloudflare":
            self._tunnel_status.config(
                text="⏳ Cloudflare Tunnel wird gestartet…", fg=YELLOW
            )
            self._show_qr(f"http://{self._ip}:{self._port}")  # temp QR
            _tunnel.start_cloudflare(
                self._port,
                on_ready=lambda url: self.after(0, lambda: self._on_tunnel_ready(url)),
                on_error=lambda msg: self.after(0, lambda: self._on_tunnel_error(msg)),
            )
        elif choice == "ngrok":
            token = self._token_var.get().strip()
            if not token:
                messagebox.showwarning(
                    "ngrok Token fehlt",
                    "Bitte trage deinen ngrok Auth-Token ein.\n"
                    "Kostenlos erhältlich unter ngrok.com",
                )
                self._stop()
                return
            self._tunnel_status.config(
                text="⏳ ngrok Tunnel wird gestartet…", fg=YELLOW
            )
            self._show_qr(f"http://{self._ip}:{self._port}")  # temp QR
            _tunnel.start_ngrok(
                self._port, token,
                on_ready=lambda url: self.after(0, lambda: self._on_tunnel_ready(url)),
                on_error=lambda msg: self.after(0, lambda: self._on_tunnel_error(msg)),
            )
        else:
            self._tunnel_status.config(text="")
            base = f"http://{self._ip}:{self._port}"
            self._url_lbl.config(text=base)
            self._show_qr(base)

    def _stop(self):
        if self._after_id:
            self.after_cancel(self._after_id)
        stop_streaming()
        _tunnel.stop()
        _state["code"] = ""
        self._code_lbl.config(text="──────", fg=ACCENT2)
        self._btn.config(text="▶   Stream starten", bg=ACCENT)
        self._dot.config(fg=FGMUTED)
        self._status_lbl.config(text="Inaktiv", fg=FGMUTED)
        self._tunnel_status.config(text="")
        self._url_lbl.config(text=f"http://{self._ip}:{self._port}", fg=FG)
        self._qr_lbl.config(image="")
        self._qr_hint.config(text="")
        self._qr_photo = None

    def _on_tunnel_ready(self, url: str):
        watch_url = f"{url}/watch?code={_state['code']}"
        self._url_lbl.config(text=url, fg=GREEN)
        self._tunnel_status.config(
            text="✓ Tunnel aktiv – von überall erreichbar", fg=GREEN
        )
        self._show_qr(url)

    def _on_tunnel_error(self, msg: str):
        self._tunnel_status.config(text=f"✗ {msg}", fg=RED)
        # Fall back to local URL
        base = f"http://{self._ip}:{self._port}"
        self._url_lbl.config(text=base, fg=YELLOW)
        self._show_qr(base)

    def _show_qr(self, base_url: str):
        watch_url = f"{base_url}/watch?code={_state['code']}"
        try:
            photo = _make_qr(watch_url, size=160)
            self._qr_photo = photo
            self._qr_lbl.config(image=photo)
            self._qr_hint.config(text=f"QR scannen oder Code  {_state['code']}  eingeben")
        except Exception:
            self._qr_hint.config(text=f"Code: {_state['code']}")

    def _on_close(self):
        stop_streaming()
        _tunnel.stop()
        self.destroy()


# ══════════════════════════════════════════════════════════════════════════════
#  Entry Point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    App().mainloop()
