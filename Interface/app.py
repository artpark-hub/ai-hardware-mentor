"""
Arduino Vision Interface  –  Dual AI Edition
=============================================
DroidCam live video added via HTTP stream (no USB needed).
Enter your phone IP in the DroidCam URL field and click Connect.

FIXES:
  1. DroidCam JPEG polling now forces a UI refresh after every successful frame.
  2. COM3 "Access is denied" error now shows a helpful message about closing Arduino IDE.
  3. Serial errors are captured and passed to AI for code correction.
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog
import threading
import queue
import serial
import serial.tools.list_ports
import cv2
from PIL import Image, ImageTk
import subprocess
import os
import tempfile
import time
import base64
import re
import io
import urllib.request

# ── Default config ─────────────────────────────────────────────────────────
DEFAULT_PORT        = "COM3"
DEFAULT_BAUD        = 9600
DEFAULT_CAM_IDX     = 1
DEFAULT_DROIDCAM_IP = "172.25.61.212"
DEFAULT_DROIDCAM_PORT = "4747"
ANTHROPIC_API_KEY   = ""
GEMINI_API_KEY      = ""

BASE_SKETCH = """\
#define touchPin 2
#define IN1 9
#define IN2 10
#define ENA 6

int motorSpeed = 0;
int speedStep = 50;
bool lastState = LOW;

void setup() {
  pinMode(touchPin, INPUT);
  pinMode(IN1, OUTPUT);
  pinMode(IN2, OUTPUT);
  pinMode(ENA, OUTPUT);
  Serial.begin(9600);
  Serial.println("Touch Sensor DC Motor Speed Control Initialized");
  digitalWrite(IN1, HIGH);
  digitalWrite(IN2, LOW);
}

void loop() {
  int touchStatus = digitalRead(touchPin);
  if (touchStatus == HIGH && lastState == LOW) {
    motorSpeed += speedStep;
    if (motorSpeed > 255) motorSpeed = 0;
    analogWrite(ENA, motorSpeed);
    Serial.print("Touch Detected! New PWM Speed: ");
    Serial.println(motorSpeed);
    float approxRPM = map(motorSpeed, 0, 255, 0, 3000);
    Serial.print("Estimated Motor Speed: ");
    Serial.print(approxRPM);
    Serial.println(" RPM");
  }
  lastState = touchStatus;
  delay(200);
}
"""

# ── Colour palette ─────────────────────────────────────────────────────────
BG        = "#0d0f14"
PANEL     = "#151821"
BORDER    = "#1f2535"
ACCENT    = "#00e5ff"
GEMINI_C  = "#4285f4"
ACCENT2   = "#ff4081"
TEXT      = "#e8eaf0"
MUTED     = "#6b7280"
SUCCESS   = "#00e676"
WARNING   = "#ffab40"
CODE_BG   = "#0a0c10"
THINKING  = "#b388ff"
FONT_MONO = ("Consolas", 10)
FONT_UI   = ("Segoe UI", 10)
FONT_HEAD = ("Segoe UI Semibold", 11)

CLAUDE_LEVELS = {
    "quick":    {"label": "⚡ Quick",    "model": "claude-haiku-4-5",  "thinking": False, "max_tokens": 2000},
    "standard": {"label": "🔍 Standard", "model": "claude-sonnet-4-5", "thinking": False, "max_tokens": 8000},
    "deep":     {"label": "🧠 Deep",     "model": "claude-sonnet-4-5", "thinking": True,  "max_tokens": 16000,
                 "thinking_budget": 10000},
}
GEMINI_LEVELS = {
    "quick":    {"label": "⚡ Quick",    "model": "gemini-2.0-flash-lite",  "thinking": False},
    "standard": {"label": "🔍 Standard", "model": "gemini-2.0-flash",       "thinking": False},
    "deep":     {"label": "🧠 Deep",     "model": "gemini-1.5-pro",         "thinking": False,
                 "extra": "Think step by step, reason carefully before answering."},
}

SYSTEM_PROMPT = """\
You are an expert Arduino engineer and embedded systems developer.
The user sends their Arduino sketch, recent serial monitor output, and optionally a camera image of their hardware.
Your job: analyse everything and return a corrected / improved sketch.

Rules:
1. Output the COMPLETE updated sketch in a single ```cpp ... ``` block so it can be applied directly.
2. Before the code block write a SHORT explanation (max 5 bullet points) of what you changed and why.
3. After the code block add any wiring / hardware notes if relevant.
4. If you see the camera image, check for wiring mistakes, missing components, or incorrect pin connections.
5. If nothing needs changing, say so clearly and still output the original sketch unchanged in a ```cpp``` block.
6. If the serial log contains upload errors (e.g. "Access is denied", "port busy"), note that these are
   connection issues — not code bugs — and still provide the corrected sketch as requested.
"""


class ArduinoVisionApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Arduino Vision Interface  –  Dual AI Edition")
        self.configure(bg=BG)
        self.geometry("1500x900")
        self.minsize(1150, 740)

        self.serial_conn         = None
        self.serial_thread       = None
        self.cam_thread          = None
        self.droidcam_thread     = None
        self.serial_running      = False
        self.cam_running         = False
        self.droidcam_running    = False
        self.serial_queue        = queue.Queue()
        self.cam_frame_queue     = queue.Queue(maxsize=2)
        self.current_sketch      = BASE_SKETCH
        self.port_var            = tk.StringVar(value=DEFAULT_PORT)
        self.baud_var            = tk.StringVar(value=str(DEFAULT_BAUD))
        self.last_ai_code        = ""
        self._latest_cam_frame   = None
        self._ai_busy            = False
        self._engine_var         = tk.StringVar(value="claude")
        self._cam_source         = tk.StringVar(value="droidcam")

        self._build_ui()
        self._refresh_ports()
        self._poll_serial_queue()
        self._poll_cam_frame()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ─────────────────────────────────────────────────────────────────────
    # UI BUILD
    # ─────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        topbar = tk.Frame(self, bg=PANEL, height=60)
        topbar.pack(fill=tk.X, side=tk.TOP)
        topbar.pack_propagate(False)

        tk.Label(topbar, text="Arduino Vision  –  Dual AI",
                 bg=PANEL, fg=ACCENT, font=("Segoe UI Semibold", 13)
                 ).pack(side=tk.LEFT, padx=16, pady=14)

        tk.Button(topbar, text="Test Gemini Key", cursor="hand2",
                  bg=BORDER, fg=GEMINI_C, relief=tk.FLAT, font=FONT_UI,
                  command=self._test_gemini_key).pack(side=tk.RIGHT, padx=(0, 6), pady=10)
        tk.Label(topbar, text="Gemini Key:",
                 bg=PANEL, fg=MUTED, font=FONT_UI).pack(side=tk.RIGHT, padx=(0, 4))
        self.gemini_entry = tk.Entry(topbar, width=38, show="*",
                                     bg=BORDER, fg=TEXT, insertbackground=GEMINI_C,
                                     relief=tk.FLAT, font=FONT_MONO)
        self.gemini_entry.pack(side=tk.RIGHT, padx=(0, 6), pady=10, ipady=4)
        if GEMINI_API_KEY:
            self.gemini_entry.insert(0, GEMINI_API_KEY)

        tk.Label(topbar, text="Claude Key:",
                 bg=PANEL, fg=MUTED, font=FONT_UI).pack(side=tk.RIGHT, padx=(0, 4))
        self.claude_entry = tk.Entry(topbar, width=38, show="*",
                                     bg=BORDER, fg=TEXT, insertbackground=ACCENT,
                                     relief=tk.FLAT, font=FONT_MONO)
        self.claude_entry.pack(side=tk.RIGHT, padx=(0, 12), pady=10, ipady=4)
        if ANTHROPIC_API_KEY:
            self.claude_entry.insert(0, ANTHROPIC_API_KEY)

        main = tk.Frame(self, bg=BG)
        main.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 8))
        main.columnconfigure(0, weight=3, minsize=320)
        main.columnconfigure(1, weight=4, minsize=420)
        main.columnconfigure(2, weight=3, minsize=360)
        main.rowconfigure(0, weight=1)

        self._build_left(main)
        self._build_center(main)
        self._build_right(main)

    # ── LEFT ──────────────────────────────────────────────────────────────
    def _build_left(self, parent):
        f = self._panel(parent, 0, 0)

        self._section_label(f, "CAMERA FEED")

        dc_row = tk.Frame(f, bg=PANEL)
        dc_row.pack(fill=tk.X, padx=10, pady=(0, 4))

        tk.Label(dc_row, text="DroidCam IP:Port",
                 bg=PANEL, fg=MUTED, font=FONT_UI).pack(side=tk.LEFT)

        self.droidcam_ip_var = tk.StringVar(
            value=f"{DEFAULT_DROIDCAM_IP}:{DEFAULT_DROIDCAM_PORT}")
        ip_entry = tk.Entry(dc_row, textvariable=self.droidcam_ip_var,
                            width=20, bg=BORDER, fg=TEXT,
                            insertbackground=ACCENT, relief=tk.FLAT, font=FONT_MONO)
        ip_entry.pack(side=tk.LEFT, padx=4, ipady=3)

        self.droidcam_btn = tk.Button(
            dc_row, text="▶ Connect DroidCam",
            command=self._toggle_droidcam,
            bg=ACCENT, fg=BG, relief=tk.FLAT,
            font=("Segoe UI Semibold", 9), cursor="hand2", padx=8, pady=4)
        self.droidcam_btn.pack(side=tk.LEFT, padx=4)

        self.droidcam_status = tk.Label(dc_row, text="●", bg=PANEL, fg=MUTED,
                                         font=("Segoe UI", 14))
        self.droidcam_status.pack(side=tk.LEFT, padx=2)

        self.cam_label = tk.Label(f, bg="#000")
        self.cam_label.pack(fill=tk.X, padx=10, pady=(0, 4))
        self._placeholder_cam()

        snap_row = tk.Frame(f, bg=PANEL)
        snap_row.pack(fill=tk.X, padx=10, pady=(0, 4))
        tk.Button(snap_row, text="📸 Capture Snapshot",
                  command=self._capture_snapshot,
                  bg=BORDER, fg=ACCENT, relief=tk.FLAT,
                  font=FONT_UI, cursor="hand2",
                  padx=8, pady=4).pack(side=tk.LEFT)
        self.snapshot_label = tk.Label(snap_row, text="", bg=PANEL, fg=MUTED, font=FONT_UI)
        self.snapshot_label.pack(side=tk.LEFT, padx=6)

        local_row = tk.Frame(f, bg=PANEL)
        local_row.pack(fill=tk.X, padx=10, pady=(0, 6))
        tk.Label(local_row, text="Local cam idx:",
                 bg=PANEL, fg=MUTED, font=FONT_UI).pack(side=tk.LEFT)
        self.cam_idx_var = tk.StringVar(value=str(DEFAULT_CAM_IDX))
        tk.Entry(local_row, textvariable=self.cam_idx_var, width=4,
                 bg=BORDER, fg=TEXT, insertbackground=ACCENT,
                 relief=tk.FLAT, font=FONT_MONO).pack(side=tk.LEFT, padx=4)
        self.cam_btn = self._btn(local_row, "Start Local Cam",
                                 self._toggle_camera, side=tk.LEFT)

        tk.Frame(f, bg=BORDER, height=1).pack(fill=tk.X, padx=10, pady=8)
        self._section_label(f, "SERIAL MONITOR")

        row = tk.Frame(f, bg=PANEL)
        row.pack(fill=tk.X, padx=10, pady=(0, 4))
        tk.Label(row, text="Port:", bg=PANEL, fg=MUTED, font=FONT_UI).pack(side=tk.LEFT)
        self.port_combo = ttk.Combobox(row, textvariable=self.port_var,
                                       width=8, font=FONT_MONO)
        self.port_combo.pack(side=tk.LEFT, padx=4)
        tk.Button(row, text="Refresh", bg=BORDER, fg=ACCENT, relief=tk.FLAT,
                  font=FONT_UI, cursor="hand2",
                  command=self._refresh_ports).pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(row, text="Baud:", bg=PANEL, fg=MUTED, font=FONT_UI).pack(side=tk.LEFT)
        ttk.Combobox(row, textvariable=self.baud_var,
                     values=["9600", "115200", "57600"],
                     width=7, font=FONT_MONO).pack(side=tk.LEFT, padx=4)

        self.serial_btn = self._btn(f, "Connect Serial", self._toggle_serial,
                                    fill=tk.X, padx=10)

        self.serial_log = scrolledtext.ScrolledText(
            f, bg=CODE_BG, fg=SUCCESS, font=FONT_MONO,
            relief=tk.FLAT, height=8, wrap=tk.WORD,
            insertbackground=ACCENT, state=tk.DISABLED)
        self.serial_log.pack(fill=tk.BOTH, expand=True, padx=10, pady=(4, 10))
        self.serial_log.tag_config("system", foreground=MUTED,   font=("Segoe UI Italic", 9))
        self.serial_log.tag_config("error",  foreground=ACCENT2, font=FONT_MONO)

        self._btn(f, "Clear Log", lambda: self._clear_text(self.serial_log),
                  fill=tk.X, padx=10, pady=(0, 8), color=MUTED)

    # ── CENTER ────────────────────────────────────────────────────────────
    def _build_center(self, parent):
        f = self._panel(parent, 0, 1)
        self._section_label(f, "ARDUINO SKETCH")

        tb = tk.Frame(f, bg=PANEL)
        tb.pack(fill=tk.X, padx=10, pady=(0, 4))
        self._btn(tb, "Load .ino",        self._load_sketch,  side=tk.LEFT)
        self._btn(tb, "Save",             self._save_sketch,  side=tk.LEFT, padx=(4, 0))
        self._btn(tb, "Reset to Default", self._reset_sketch, side=tk.RIGHT, color=MUTED)

        self.code_editor = scrolledtext.ScrolledText(
            f, bg=CODE_BG, fg="#a8d8ea", font=("Consolas", 11),
            relief=tk.FLAT, wrap=tk.NONE,
            insertbackground=ACCENT, undo=True, tabs="    ")
        self.code_editor.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 6))
        self.code_editor.insert(tk.END, self.current_sketch)

        up = tk.Frame(f, bg=PANEL)
        up.pack(fill=tk.X, padx=10, pady=(0, 10))
        self.upload_btn = self._btn(up, "⬆ Upload to Arduino",
                                    self._upload_sketch, side=tk.LEFT, color=ACCENT2)
        self.upload_status = tk.Label(up, text="", bg=PANEL, fg=MUTED, font=FONT_UI)
        self.upload_status.pack(side=tk.LEFT, padx=8)

    # ── RIGHT ─────────────────────────────────────────────────────────────
    def _build_right(self, parent):
        f = self._panel(parent, 0, 2)
        self._section_label(f, "AI CODE ASSISTANT")

        eng_frame = tk.Frame(f, bg=BORDER)
        eng_frame.pack(fill=tk.X, padx=10, pady=(0, 8))
        eng_frame.columnconfigure(0, weight=1)
        eng_frame.columnconfigure(1, weight=1)

        self._claude_tab = tk.Button(
            eng_frame, text="🤖 Claude (Anthropic)",
            command=lambda: self._switch_engine("claude"),
            bg=ACCENT, fg=BG,
            relief=tk.FLAT, font=("Segoe UI Semibold", 10),
            cursor="hand2", padx=8, pady=6)
        self._claude_tab.grid(row=0, column=0, sticky="nsew")

        self._gemini_tab = tk.Button(
            eng_frame, text="✨ Gemini Pro (Google)",
            command=lambda: self._switch_engine("gemini"),
            bg=BORDER, fg=GEMINI_C,
            relief=tk.FLAT, font=("Segoe UI Semibold", 10),
            cursor="hand2", padx=8, pady=6)
        self._gemini_tab.grid(row=0, column=1, sticky="nsew")

        self.engine_desc = tk.Label(f, text="", bg=PANEL, fg=MUTED,
                                    font=("Segoe UI Italic", 9))
        self.engine_desc.pack(fill=tk.X, padx=10, pady=(0, 4))
        self._switch_engine("claude")

        opts = tk.Frame(f, bg=PANEL)
        opts.pack(fill=tk.X, padx=10, pady=(0, 4))
        self.include_cam_var    = tk.BooleanVar(value=True)
        self.include_serial_var = tk.BooleanVar(value=True)
        for var, lbl in [(self.include_cam_var,    "📷 Camera"),
                         (self.include_serial_var, "📟 Serial log")]:
            tk.Checkbutton(opts, text=lbl, variable=var,
                           bg=PANEL, fg=TEXT, selectcolor=BORDER,
                           activebackground=PANEL, activeforeground=ACCENT,
                           font=FONT_UI).pack(side=tk.LEFT, padx=(0, 10))

        self.ai_log = scrolledtext.ScrolledText(
            f, bg=CODE_BG, fg=TEXT, font=FONT_UI,
            relief=tk.FLAT, wrap=tk.WORD,
            insertbackground=ACCENT, state=tk.DISABLED)
        self.ai_log.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 6))
        self.ai_log.tag_config("assistant", foreground=TEXT,      font=FONT_UI)
        self.ai_log.tag_config("code",      foreground="#a8d8ea", font=FONT_MONO, background=CODE_BG)
        self.ai_log.tag_config("thinking",  foreground=THINKING,  font=("Segoe UI Italic", 9))
        self.ai_log.tag_config("error",     foreground=ACCENT2,   font=FONT_UI)
        self.ai_log.tag_config("system",    foreground=MUTED,     font=("Segoe UI Italic", 9))
        self.ai_log.tag_config("label_c",   foreground=ACCENT,    font=("Segoe UI Semibold", 9))
        self.ai_log.tag_config("label_g",   foreground=GEMINI_C,  font=("Segoe UI Semibold", 9))
        self.ai_log.tag_config("label_w",   foreground=WARNING,   font=("Segoe UI Semibold", 9))

        self._ai_log_append("system",
            "Select engine above, then click Analyse.\n"
            "DroidCam snapshot is captured automatically.\n\n")

        tk.Label(f, text="Optional extra instruction (leave blank = auto-fix):",
                 bg=PANEL, fg=MUTED, font=("Segoe UI Italic", 9)).pack(fill=tk.X, padx=10)
        self.prompt_entry = scrolledtext.ScrolledText(
            f, bg=BORDER, fg=TEXT, font=FONT_UI,
            relief=tk.FLAT, height=2, wrap=tk.WORD,
            insertbackground=ACCENT)
        self.prompt_entry.pack(fill=tk.X, padx=10, pady=(2, 6))

        tk.Frame(f, bg=BORDER, height=1).pack(fill=tk.X, padx=10, pady=(6, 4))

        self.analyse_btn = tk.Button(
            f, text="🔍  Analyse",
            command=lambda: self._ask_ai("standard"),
            bg=ACCENT, fg=BG,
            activebackground=THINKING, activeforeground=BG,
            relief=tk.FLAT, font=("Segoe UI Semibold", 13),
            cursor="hand2", pady=14)
        self.analyse_btn.pack(fill=tk.X, padx=10, pady=(6, 6))
        self._level_btns = {"standard": self.analyse_btn}

        br = tk.Frame(f, bg=PANEL)
        br.pack(fill=tk.X, padx=10, pady=(4, 10))
        self._btn(br, "✅ Apply Last Code", self._apply_last_code, side=tk.LEFT, color=SUCCESS)
        self._btn(br, "Clear Log", lambda: self._clear_text(self.ai_log), side=tk.RIGHT, color=MUTED)

    # ─────────────────────────────────────────────────────────────────────
    # DROIDCAM LIVE STREAM
    # ─────────────────────────────────────────────────────────────────────

    def _toggle_droidcam(self):
        if self.droidcam_running:
            self._stop_droidcam()
        else:
            self._start_droidcam()

    def _start_droidcam(self):
        ip_port = self.droidcam_ip_var.get().strip()
        if not ip_port:
            messagebox.showwarning("DroidCam", "Enter IP:Port first.")
            return
        if ":" in ip_port:
            ip, port = ip_port.rsplit(":", 1)
        else:
            ip, port = ip_port, "4747"

        self._droidcam_ip   = ip
        self._droidcam_port = port
        stream_url = f"http://{ip}:{port}/video"
        self.droidcam_running = True
        self.droidcam_btn.configure(text="■ Disconnect", bg=ACCENT2, fg=BG)
        self.droidcam_status.configure(fg=WARNING, text="●")

        self.droidcam_thread = threading.Thread(
            target=self._droidcam_reader,
            args=(stream_url,), daemon=True)
        self.droidcam_thread.start()

    def _stop_droidcam(self):
        self.droidcam_running = False
        self.droidcam_btn.configure(text="▶ Connect DroidCam", bg=ACCENT, fg=BG)
        self.droidcam_status.configure(fg=MUTED, text="●")
        self._latest_cam_frame = None
        self._placeholder_cam()

    def _droidcam_reader(self, stream_url: str):
        """
        Try every known DroidCam URL pattern in order:
          1. /video          – OpenCV MJPEG  (DroidCam ≤ 6.x)
          2. /mjpegfeed      – OpenCV MJPEG  (DroidCam 7.x)
          3. /mjpegfeed?640x480 – with resolution hint
          4. /shot.jpg       – JPEG snapshot polling (old)
          5. /jpeg           – alternative snapshot endpoint
        The first one that delivers a real frame wins.
        """
        ip   = getattr(self, "_droidcam_ip",   "192.168.1.1")
        port = getattr(self, "_droidcam_port", "4747")
        base = f"http://{ip}:{port}"

        # ── 1 & 2 & 3: try OpenCV MJPEG URLs ─────────────────────────────
        mjpeg_candidates = [
            stream_url,                          # /video
            f"{base}/mjpegfeed",                 # DroidCam 7+
            f"{base}/mjpegfeed?640x480",
            f"{base}/video?640x480",
        ]

        for url in mjpeg_candidates:
            if not self.droidcam_running:
                return
            cap = cv2.VideoCapture(url)
            # Give OpenCV up to 3 seconds to get a real frame
            got_frame = False
            deadline  = time.time() + 3.0
            while time.time() < deadline and self.droidcam_running:
                ret, frame = cap.read()
                if ret and frame is not None and frame.size > 0:
                    got_frame = True
                    break
                time.sleep(0.1)

            if got_frame:
                self.after(0, lambda u=url: self._ai_log_append(
                    "system", f"DroidCam stream connected: {u}\n"))
                self.after(0, lambda: self.droidcam_status.configure(fg=SUCCESS))
                # ── stream loop ───────────────────────────────────────────
                while self.droidcam_running:
                    ret, frame = cap.read()
                    if not ret or frame is None:
                        break
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    pil = Image.fromarray(rgb)
                    self._latest_cam_frame = pil
                    try:
                        self.cam_frame_queue.put_nowait(pil)
                    except queue.Full:
                        try:
                            self.cam_frame_queue.get_nowait()
                        except queue.Empty:
                            pass
                        self.cam_frame_queue.put_nowait(pil)
                    time.sleep(0.03)
                cap.release()
                # stream ended — report and stop
                if self.droidcam_running:
                    self.after(0, lambda: self._ai_log_append(
                        "error", "DroidCam stream dropped. Reconnect to retry.\n\n"))
                    self.after(0, self._stop_droidcam)
                return
            cap.release()

        # ── 4 & 5: JPEG snapshot polling fallback ─────────────────────────
        snap_candidates = [
            f"{base}/shot.jpg",
            f"{base}/jpeg",
            f"{base}/photo.jpg",
        ]

        self.after(0, lambda: self.droidcam_status.configure(fg=WARNING))
        self.after(0, lambda: self._ai_log_append(
            "system",
            "OpenCV MJPEG failed — trying JPEG snapshot polling…\n"
            f"Tried: {', '.join(mjpeg_candidates)}\n"))

        # Find the first snapshot URL that actually works
        working_snap = None
        for surl in snap_candidates:
            try:
                req  = urllib.request.urlopen(surl, timeout=3)
                data = req.read()
                img  = Image.open(io.BytesIO(data))
                img.load()
                working_snap = surl
                self.after(0, lambda u=surl: self._ai_log_append(
                    "system", f"Snapshot URL works: {u}\n"))
                break
            except Exception:
                pass

        if working_snap is None:
            self.after(0, lambda: self._ai_log_append(
                "error",
                f"Cannot reach DroidCam at {base}.\n"
                "Checklist:\n"
                "  • Phone and PC must be on the SAME Wi-Fi network\n"
                "  • DroidCam app must be running and showing 'Waiting for connection'\n"
                "  • Firewall / Windows Defender may be blocking port 4747\n"
                "  • Try opening this in your browser: " + base + "/video\n\n"))
            self.after(0, lambda: self.droidcam_status.configure(fg=ACCENT2))
            self.after(0, self._stop_droidcam)
            return

        consecutive_errors = 0
        while self.droidcam_running:
            try:
                req  = urllib.request.urlopen(working_snap, timeout=3)
                data = req.read()
                pil  = Image.open(io.BytesIO(data))
                pil.load()
                self._latest_cam_frame = pil
                try:
                    self.cam_frame_queue.get_nowait()
                except queue.Empty:
                    pass
                self.cam_frame_queue.put_nowait(pil)
                self.after(0, lambda: self.droidcam_status.configure(fg=SUCCESS, text="●"))
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                self.after(0, lambda: self.droidcam_status.configure(fg=ACCENT2, text="●"))
                if consecutive_errors == 5:
                    self.after(0, lambda err=str(e): self._ai_log_append(
                        "error",
                        f"Snapshot polling error (5 in a row): {err}\n\n"))
            time.sleep(0.15)

    def _capture_snapshot(self):
        if self._latest_cam_frame is None:
            messagebox.showinfo("No Frame",
                "No camera frame available.\nConnect DroidCam or start local camera first.")
            return
        self.snapshot_label.configure(text="✓ Snapshot ready for AI", fg=SUCCESS)
        self.after(3000, lambda: self.snapshot_label.configure(text=""))

    # ─────────────────────────────────────────────────────────────────────
    # LOCAL CAMERA
    # ─────────────────────────────────────────────────────────────────────

    def _toggle_camera(self):
        if self.cam_running:
            self._stop_camera()
        else:
            self._start_camera()

    def _start_camera(self):
        idx = int(self.cam_idx_var.get())
        self.cam_running = True
        self.cam_btn.configure(text="Stop Local Cam", fg=ACCENT2)
        self.cam_thread = threading.Thread(
            target=self._cam_reader, args=(idx,), daemon=True)
        self.cam_thread.start()

    def _stop_camera(self):
        self.cam_running = False
        self.cam_btn.configure(text="Start Local Cam", fg=TEXT)
        self._placeholder_cam()

    def _cam_reader(self, idx):
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap = cv2.VideoCapture(idx)
        if not cap.isOpened():
            self.after(0, lambda: messagebox.showerror(
                "Camera Error", f"Cannot open camera index {idx}."))
            self.cam_running = False
            self.after(0, lambda: self.cam_btn.configure(text="Start Local Cam", fg=TEXT))
            return
        while self.cam_running:
            ret, frame = cap.read()
            if ret:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                pil = Image.fromarray(rgb)
                self._latest_cam_frame = pil
                try:
                    self.cam_frame_queue.put_nowait(pil)
                except queue.Full:
                    pass
            time.sleep(0.03)
        cap.release()

    def _poll_cam_frame(self):
        try:
            while True:
                pil = self.cam_frame_queue.get_nowait()
                self._set_cam_image(pil)
        except queue.Empty:
            pass
        self.after(30, self._poll_cam_frame)

    # ─────────────────────────────────────────────────────────────────────
    # ENGINE SWITCHER
    # ─────────────────────────────────────────────────────────────────────

    def _switch_engine(self, engine: str):
        self._engine_var.set(engine)
        if engine == "claude":
            self._claude_tab.configure(bg=ACCENT, fg=BG)
            self._gemini_tab.configure(bg=BORDER, fg=GEMINI_C)
            self.engine_desc.configure(
                text="Claude Haiku · Sonnet · Sonnet + Extended Thinking", fg=ACCENT)
        else:
            self._gemini_tab.configure(bg=GEMINI_C, fg=BG)
            self._claude_tab.configure(bg=BORDER, fg=ACCENT)
            self.engine_desc.configure(
                text="Quick: 2.0 Flash-Lite  ·  Standard: 2.0 Flash  ·  Deep: 1.5 Pro",
                fg=GEMINI_C)

    # ─────────────────────────────────────────────────────────────────────
    # WIDGET HELPERS
    # ─────────────────────────────────────────────────────────────────────

    def _panel(self, parent, row, col):
        outer = tk.Frame(parent, bg=BORDER)
        outer.grid(row=row, column=col, sticky="nsew", padx=5, pady=5)
        inner = tk.Frame(outer, bg=PANEL)
        inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        return inner

    def _section_label(self, parent, text):
        tk.Label(parent, text=text, bg=PANEL, fg=ACCENT,
                 font=FONT_HEAD, anchor="w").pack(fill=tk.X, padx=10, pady=(10, 4))

    def _btn(self, parent, text, cmd, side=None, fill=None,
             padx=0, pady=0, color=None):
        b = tk.Button(parent, text=text, command=cmd,
                      bg=BORDER, fg=color or TEXT,
                      activebackground=ACCENT, activeforeground=BG,
                      relief=tk.FLAT, font=FONT_UI, cursor="hand2",
                      padx=10, pady=5)
        kw = {}
        if side: kw["side"] = side
        if fill: kw["fill"] = fill
        if padx: kw["padx"] = padx
        if pady: kw["pady"] = pady
        b.pack(**kw)
        return b

    def _placeholder_cam(self):
        img = Image.new("RGB", (320, 240), color=(20, 22, 30))
        from PIL import ImageDraw
        d = ImageDraw.Draw(img)
        d.text((20, 100), "Enter IP:Port above", fill=(100, 110, 130))
        d.text((20, 118), "then click Connect DroidCam", fill=(100, 110, 130))
        self._set_cam_image(img)

    def _set_cam_image(self, pil_img):
        pil_img = pil_img.resize((320, 240), Image.LANCZOS)
        tk_img  = ImageTk.PhotoImage(pil_img)
        self.cam_label.configure(image=tk_img)
        self.cam_label.image = tk_img

    # ─────────────────────────────────────────────────────────────────────
    # SERIAL  (FIX: better COM3 access-denied message)
    # ─────────────────────────────────────────────────────────────────────

    def _refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_combo["values"] = ports
        if DEFAULT_PORT in ports:
            self.port_var.set(DEFAULT_PORT)
        elif ports:
            self.port_var.set(ports[0])

    def _toggle_serial(self):
        if self.serial_running:
            self._stop_serial()
        else:
            self._start_serial()

    def _start_serial(self):
        port = self.port_var.get()
        baud = int(self.baud_var.get())
        try:
            self.serial_conn = serial.Serial(port, baud, timeout=1)
            self.serial_running = True
            self.serial_btn.configure(text="Disconnect Serial", fg=ACCENT2)
            self.serial_thread = threading.Thread(
                target=self._serial_reader, daemon=True)
            self.serial_thread.start()
            self._serial_log(f"[Connected to {port} @ {baud} baud]\n", "system")
        except serial.SerialException as e:
            err_str = str(e)
            # ── FIX: friendly message for the most common error ───────────
            if "access is denied" in err_str.lower() or "permissionerror" in err_str.lower():
                messagebox.showerror(
                    "COM Port Access Denied",
                    f"Cannot open {port} — another program is using it.\n\n"
                    "Most likely fix:\n"
                    "  • Close Arduino IDE (it locks the port)\n"
                    "  • Close any other serial monitor apps\n"
                    "  • Unplug and re-plug the Arduino\n\n"
                    f"Original error:\n{e}")
                self._serial_log(
                    f"[Serial Error] Access denied on {port}.\n"
                    "Close Arduino IDE / other serial apps, then try again.\n",
                    "error")
            else:
                messagebox.showerror("Serial Error", str(e))
                self._serial_log(f"[Serial Error] {e}\n", "error")
        except Exception as e:
            messagebox.showerror("Serial Error", str(e))

    def _stop_serial(self):
        self.serial_running = False
        if self.serial_conn:
            try:
                self.serial_conn.close()
            except Exception:
                pass
        self.serial_btn.configure(text="Connect Serial", fg=TEXT)
        self._serial_log("[Disconnected]\n", "system")

    def _serial_reader(self):
        while self.serial_running:
            try:
                if self.serial_conn and self.serial_conn.in_waiting:
                    line = self.serial_conn.readline().decode("utf-8", errors="replace")
                    self.serial_queue.put(line)
            except Exception:
                break
            time.sleep(0.05)

    def _poll_serial_queue(self):
        try:
            while True:
                line = self.serial_queue.get_nowait()
                self._serial_log(line)
        except queue.Empty:
            pass
        self.after(100, self._poll_serial_queue)

    def _serial_log(self, text, tag=None):
        self.serial_log.configure(state=tk.NORMAL)
        self.serial_log.insert(tk.END, text, tag or "")
        self.serial_log.see(tk.END)
        self.serial_log.configure(state=tk.DISABLED)

    def _get_serial_log_text(self):
        self.serial_log.configure(state=tk.NORMAL)
        text = self.serial_log.get("1.0", tk.END)
        self.serial_log.configure(state=tk.DISABLED)
        return text[-3000:]

    # ─────────────────────────────────────────────────────────────────────
    # SKETCH MANAGEMENT
    # ─────────────────────────────────────────────────────────────────────

    def _load_sketch(self):
        path = filedialog.askopenfilename(
            title="Open Arduino Sketch",
            filetypes=[("Arduino sketch", "*.ino"), ("All files", "*.*")])
        if path:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    code = fh.read()
                self.code_editor.delete("1.0", tk.END)
                self.code_editor.insert(tk.END, code)
                self.current_sketch = code
            except Exception as e:
                messagebox.showerror("Load Error", str(e))

    def _save_sketch(self):
        path = filedialog.asksaveasfilename(
            title="Save Arduino Sketch",
            defaultextension=".ino",
            filetypes=[("Arduino sketch", "*.ino"), ("All files", "*.*")])
        if path:
            try:
                code = self.code_editor.get("1.0", tk.END)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(code)
                messagebox.showinfo("Saved", f"Sketch saved to:\n{path}")
            except Exception as e:
                messagebox.showerror("Save Error", str(e))

    def _reset_sketch(self):
        if messagebox.askyesno("Reset Sketch", "Reset editor to the default sketch?"):
            self.code_editor.delete("1.0", tk.END)
            self.code_editor.insert(tk.END, BASE_SKETCH)
            self.current_sketch = BASE_SKETCH

    # ─────────────────────────────────────────────────────────────────────
    # UPLOAD
    # ─────────────────────────────────────────────────────────────────────

    def _upload_sketch(self):
        port = self.port_var.get()
        code = self.code_editor.get("1.0", tk.END)
        self.upload_btn.configure(state=tk.DISABLED)
        self.upload_status.configure(text="Compiling…", fg=WARNING)

        def do_upload():
            try:
                tmp_dir    = tempfile.mkdtemp()
                sketch_dir = os.path.join(tmp_dir, "sketch")
                os.makedirs(sketch_dir)
                with open(os.path.join(sketch_dir, "sketch.ino"), "w", encoding="utf-8") as fh:
                    fh.write(code)
                cmd = ["D:\\Arduinotest\\Arduino\\arduino-cli", "compile", "--fqbn", "arduino:avr:uno",
                       "--upload", "--port", port, sketch_dir]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                if result.returncode == 0:
                    self.after(0, lambda: self.upload_status.configure(
                        text="Upload successful!", fg=SUCCESS))
                    self.after(0, lambda: self._serial_log(
                        "[Upload successful]\n", "system"))
                else:
                    err = result.stderr or result.stdout
                    self.after(0, lambda: self.upload_status.configure(
                        text="Upload failed – see serial log", fg=ACCENT2))
                    self.after(0, lambda: self._serial_log(
                        f"[Upload error]\n{err}\n", "error"))
            except FileNotFoundError:
                self.after(0, lambda: messagebox.showerror(
                    "arduino-cli not found",
                    "Download: https://arduino.github.io/arduino-cli/"))
                self.after(0, lambda: self.upload_status.configure(
                    text="arduino-cli not found", fg=ACCENT2))
            except Exception as e:
                self.after(0, lambda: self.upload_status.configure(
                    text=f"Error: {e}", fg=ACCENT2))
            finally:
                self.after(0, lambda: self.upload_btn.configure(state=tk.NORMAL))

        threading.Thread(target=do_upload, daemon=True).start()

    # ─────────────────────────────────────────────────────────────────────
    # AI DISPATCH
    # ─────────────────────────────────────────────────────────────────────

    def _ask_ai(self, level_key: str):
        if self._ai_busy:
            return
        engine = self._engine_var.get()
        if engine == "claude":
            api_key = self.claude_entry.get().strip()
            if not api_key:
                messagebox.showwarning("API Key Missing",
                                       "Please enter your Anthropic (Claude) API key.")
                return
            cfg = CLAUDE_LEVELS[level_key]
        else:
            api_key = self.gemini_entry.get().strip()
            if not api_key:
                messagebox.showwarning("API Key Missing",
                                       "Please enter your Gemini API key.")
                return
            cfg = GEMINI_LEVELS[level_key]

        sketch_code  = self.code_editor.get("1.0", tk.END)
        serial_text  = self._get_serial_log_text() if self.include_serial_var.get() else ""
        cam_frame    = self._latest_cam_frame       if self.include_cam_var.get()    else None
        extra_prompt = self.prompt_entry.get("1.0", tk.END).strip()

        self._ai_busy = True
        self._set_level_btns_state(tk.DISABLED)

        tag         = "label_c" if engine == "claude" else "label_g"
        engine_name = "Claude"  if engine == "claude" else "Gemini"
        cam_status  = "DroidCam frame captured ✓" if cam_frame else "No camera frame"
        self._ai_log_append(tag, f"── {engine_name} Analysis ──\n")
        self._ai_log_append("system", f"Camera: {cam_status}\nSending…\n\n")

        target = self._claude_request if engine == "claude" else self._gemini_request
        threading.Thread(
            target=target,
            args=(api_key, cfg, sketch_code, serial_text, cam_frame, extra_prompt),
            daemon=True
        ).start()

    def _build_text_prompt(self, sketch_code, serial_text, cam_frame, extra_prompt,
                           extra_instruction=""):
        parts = [f"Here is the current Arduino sketch:\n\n```cpp\n{sketch_code.strip()}\n```\n"]
        if serial_text.strip():
            parts.append(f"\nRecent serial monitor output:\n```\n{serial_text.strip()}\n```\n")
        else:
            parts.append("\n(No serial output captured.)\n")
        if cam_frame is None:
            parts.append("\n(Camera not active – no image available.)\n")
        if extra_instruction:
            parts.append(f"\n{extra_instruction}\n")
        if extra_prompt:
            parts.append(f"\nExtra instruction from user:\n{extra_prompt}\n")
        else:
            parts.append("\nPlease analyse and return a corrected sketch with a short explanation.\n")
        return "\n".join(parts)

    # ─────────────────────────────────────────────────────────────────────
    # CLAUDE
    # ─────────────────────────────────────────────────────────────────────

    def _claude_request(self, api_key, cfg, sketch_code, serial_text, cam_frame, extra_prompt):
        try:
            import anthropic
            client  = anthropic.Anthropic(api_key=api_key)
            text    = self._build_text_prompt(sketch_code, serial_text, cam_frame, extra_prompt)
            content = [{"type": "text", "text": text}]

            if cam_frame is not None:
                buf = io.BytesIO()
                cam_frame.save(buf, format="JPEG", quality=80)
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg",
                               "data": base64.b64encode(buf.getvalue()).decode()}
                })
                content.append({"type": "text",
                                 "text": "The image shows the hardware setup. Check for wiring issues."})

            kwargs = dict(
                model=cfg["model"], max_tokens=cfg["max_tokens"],
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}]
            )
            if cfg.get("thinking"):
                kwargs["thinking"] = {"type": "enabled",
                                      "budget_tokens": cfg["thinking_budget"]}
                self.after(0, lambda: self._ai_log_append(
                    "thinking", "🧠 Extended thinking enabled…\n\n"))

            response      = client.messages.create(**kwargs)
            thinking_text = ""
            reply_text    = ""
            for block in response.content:
                if block.type == "thinking":
                    thinking_text = block.thinking
                elif block.type == "text":
                    reply_text = block.text

            self._extract_code(reply_text)
            self.after(0, lambda: self._handle_response(reply_text, thinking_text, "Claude"))

        except ImportError:
            self.after(0, lambda: self._ai_log_append("error",
                "Error: 'anthropic' not installed.\nRun: pip install anthropic\n\n"))
        except Exception as e:
            self.after(0, lambda: self._ai_log_append("error", f"Claude error: {e}\n\n"))
        finally:
            self.after(0, self._unlock_ai)

    # ─────────────────────────────────────────────────────────────────────
    # GEMINI
    # ─────────────────────────────────────────────────────────────────────

    def _gemini_request(self, api_key, cfg, sketch_code, serial_text, cam_frame, extra_prompt):
        self._gemini_timer_running = False
        try:
            from google import genai as google_genai
            from google.genai import types as genai_types

            client = google_genai.Client(api_key=api_key)
            extra_instr = cfg.get("extra", "")
            body = self._build_text_prompt(
                sketch_code, serial_text, cam_frame, extra_prompt, extra_instr)
            full_text = SYSTEM_PROMPT + "\n\n---\n\n" + body
            parts = [genai_types.Part.from_text(text=full_text)]

            if cam_frame is not None:
                buf = io.BytesIO()
                cam_frame.save(buf, format="JPEG", quality=85)
                parts.append(genai_types.Part.from_bytes(
                    data=buf.getvalue(), mime_type="image/jpeg"))
                parts.append(genai_types.Part.from_text(
                    text="The image shows the hardware. Check for wiring issues."))

            start_time = time.time()
            self._gemini_timer_running = True

            def _tick():
                if self._gemini_timer_running:
                    elapsed = int(time.time() - start_time)
                    self.after(0, lambda e=elapsed: self.engine_desc.configure(
                        text=f"Gemini thinking… {e}s elapsed", fg=WARNING))
                    t = threading.Timer(1.0, _tick)
                    t.daemon = True
                    t.start()
            _tick()

            response = client.models.generate_content(
                model=cfg["model"],
                contents=[genai_types.Content(role="user", parts=parts)],
                config=genai_types.GenerateContentConfig(
                    max_output_tokens=8000, temperature=0.2)
            )
            self._gemini_timer_running = False
            elapsed_total = int(time.time() - start_time)
            reply_text = response.text
            self._extract_code(reply_text)
            self.after(0, lambda: self._handle_response(
                reply_text, "", f"Gemini ({elapsed_total}s)"))

        except ImportError:
            self._gemini_timer_running = False
            self.after(0, lambda: self._ai_log_append("error",
                "Error: 'google-genai' not installed.\nRun: pip install google-genai\n\n"))
        except Exception as e:
            self._gemini_timer_running = False
            err_str = str(e)
            if "429" in err_str or "quota" in err_str.lower():
                msg = "Gemini quota exceeded. Wait ~60s or switch to Claude.\n\n"
            elif "API_KEY_INVALID" in err_str or "401" in err_str:
                msg = "Invalid Gemini API key.\n\n"
            else:
                msg = f"Gemini error: {err_str}\n\n"
            self.after(0, lambda m=msg: self._ai_log_append("error", m))
        finally:
            self._gemini_timer_running = False
            self.after(0, self._unlock_ai)

    # ─────────────────────────────────────────────────────────────────────
    # RESPONSE HANDLER
    # ─────────────────────────────────────────────────────────────────────

    def _extract_code(self, reply: str):
        code_blocks = re.findall(r"```(?:cpp|c|arduino)?\n(.*?)```", reply, re.DOTALL)
        if code_blocks:
            self.last_ai_code = code_blocks[-1].strip()

    def _handle_response(self, reply: str, thinking: str, engine_name: str):
        if thinking:
            self._ai_log_append("thinking",
                f"── Thinking ──\n{thinking[:600]}"
                f"{'…' if len(thinking) > 600 else ''}\n\n")
        parts = re.split(r"(```(?:cpp|c|arduino)?\n.*?```)", reply, flags=re.DOTALL)
        self._ai_log_append("assistant", f"{engine_name}: ")
        for part in parts:
            if part.startswith("```"):
                inner = re.sub(r"```(?:cpp|c|arduino)?\n", "", part).rstrip("`").strip()
                self._ai_log_append("code", f"\n{inner}\n")
            else:
                self._ai_log_append("assistant", part)
        self._ai_log_append("assistant", "\n\n")
        if self.last_ai_code:
            self._ai_log_append("system",
                '→ Click "Apply Last Code" to load the updated sketch.\n\n')

    def _unlock_ai(self):
        self._ai_busy = False
        self._set_level_btns_state(tk.NORMAL)

    def _set_level_btns_state(self, state):
        for b in self._level_btns.values():
            b.configure(state=state)

    def _apply_last_code(self):
        if not self.last_ai_code:
            messagebox.showinfo("No Code", "No AI-generated code available yet.")
            return
        if messagebox.askyesno("Apply Code",
                               "Replace the editor with the last AI-generated code?"):
            self.code_editor.delete("1.0", tk.END)
            self.code_editor.insert(tk.END, self.last_ai_code)

    def _ai_log_append(self, tag, text):
        self.ai_log.configure(state=tk.NORMAL)
        self.ai_log.insert(tk.END, text, tag)
        self.ai_log.see(tk.END)
        self.ai_log.configure(state=tk.DISABLED)

    def _clear_text(self, widget):
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.configure(state=tk.DISABLED)

    def _test_gemini_key(self):
        api_key = self.gemini_entry.get().strip()
        if not api_key:
            messagebox.showwarning("No Key", "Enter your Gemini API key first.")
            return
        self._switch_engine("gemini")
        self._ai_log_append("label_g", "── Gemini Key Test ──\n")

        def do_test():
            try:
                from google import genai as google_genai
                client = google_genai.Client(api_key=api_key)
                models = client.models.list()
                gemini_models = sorted(
                    [m.name for m in models if "gemini" in m.name.lower()])
                if gemini_models:
                    lines = "Available Gemini models:\n" + \
                            "\n".join(f"  • {m}" for m in gemini_models) + "\n\n"
                    self.after(0, lambda: self._ai_log_append("assistant", lines))
                else:
                    self.after(0, lambda: self._ai_log_append("error",
                        "No Gemini models — enable billing.\n\n"))
            except Exception as e:
                self.after(0, lambda: self._ai_log_append("error", f"Test error: {e}\n\n"))

        threading.Thread(target=do_test, daemon=True).start()

    def _on_close(self):
        self.cam_running      = False
        self.serial_running   = False
        self.droidcam_running = False
        if self.serial_conn:
            try:
                self.serial_conn.close()
            except Exception:
                pass
        self.destroy()


if __name__ == "__main__":
    app = ArduinoVisionApp()
    app.mainloop()