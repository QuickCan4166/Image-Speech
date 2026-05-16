"""
AI Virtual Camera - Free & Local
Keyword -> image switcher with idle GIF support.
No API needed. Runs 100% offline.
Dependencies are installed automatically on first run.
"""

# ── Auto-installer (runs before anything else) ────────────────────────────────
import sys, subprocess, importlib, importlib.util

REQUIRED = {
    "vosk":        "vosk",
    "sounddevice": "sounddevice",
    "numpy":       "numpy",
    "PIL":         "Pillow",
}

def _install(pkg_import, pkg_install):
    print(f"Installing {pkg_install}...")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet", pkg_install],
        stdout=subprocess.DEVNULL
    )

_needed = [(i, p) for i, p in REQUIRED.items()
           if importlib.util.find_spec(i) is None]

if _needed:
    print("First run — installing dependencies...\n")
    for imp_name, pip_name in _needed:
        try:
            _install(imp_name, pip_name)
            print(f"  ✓ {pip_name}")
        except subprocess.CalledProcessError:
            print(f"  ✗ Failed to install {pip_name}. Try manually:\n"
                  f"      pip install {pip_name}")
            sys.exit(1)
    print("\nAll dependencies installed! Starting app...\n")
# ─────────────────────────────────────────────────────────────────────────────

import os, json, queue, threading, re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


# ─────────────────────────────────────────────────────────────
# Camera Output Window  (OBS captures this)
# ─────────────────────────────────────────────────────────────
class CameraWindow(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.title("AI Camera Output  —  capture this in OBS")
        self.geometry("1280x720")
        self.configure(bg="black")
        self.protocol("WM_DELETE_WINDOW", lambda: None)

        self.canvas = tk.Label(self, bg="black", cursor="none")
        self.canvas.place(relwidth=1, relheight=1)

        self.badge = tk.Label(self, text="IDLE", bg="black", fg="#333",
                              font=("Consolas", 12, "bold"))
        self.badge.place(x=12, rely=1, anchor="sw", y=-10)

        self._photo = None
        self._gif_frames = []
        self._gif_delays = []
        self._gif_idx = 0
        self._gif_job = None

    def show_image(self, path):
        self._stop_gif()
        self._load_static(path)
        self.badge.config(text="● LIVE", fg="#44ff44")

    def show_gif(self, path):
        self._stop_gif()
        self.badge.config(text="IDLE", fg="#333333")
        if path and os.path.exists(path):
            self._load_gif(path)
        else:
            self.canvas.config(image="", text="[ set an idle GIF in settings ]",
                               fg="#222", font=("Consolas", 14))

    def _wh(self):
        return (self.winfo_width() or 1280, self.winfo_height() or 720)

    def _load_static(self, path):
        from PIL import Image, ImageTk
        try:
            img = Image.open(path).convert("RGBA")
            img.thumbnail(self._wh(), Image.LANCZOS)
            self._photo = ImageTk.PhotoImage(img)
            self.canvas.config(image=self._photo, text="")
        except Exception as e:
            self.canvas.config(image="", text=f"Error: {e}", fg="red",
                               font=("Consolas", 12))

    def _load_gif(self, path):
        from PIL import Image, ImageTk
        try:
            gif = Image.open(path)
            self._gif_frames, self._gif_delays = [], []
            for i in range(getattr(gif, "n_frames", 1)):
                gif.seek(i)
                f = gif.copy().convert("RGBA")
                f.thumbnail(self._wh(), Image.LANCZOS)
                self._gif_frames.append(ImageTk.PhotoImage(f))
                self._gif_delays.append(gif.info.get("duration", 80))
            self._gif_idx = 0
            self._tick_gif()
        except Exception as e:
            self.canvas.config(image="", text=f"GIF Error: {e}", fg="red",
                               font=("Consolas", 12))

    def _tick_gif(self):
        if not self._gif_frames:
            return
        i = self._gif_idx % len(self._gif_frames)
        self.canvas.config(image=self._gif_frames[i], text="")
        self._gif_idx += 1
        self._gif_job = self.after(self._gif_delays[i], self._tick_gif)

    def _stop_gif(self):
        if self._gif_job:
            self.after_cancel(self._gif_job)
            self._gif_job = None
        self._gif_frames = []


# ─────────────────────────────────────────────────────────────
# Keyword → Image Rule Engine
# ─────────────────────────────────────────────────────────────
class RuleEngine:
    def __init__(self):
        self.rules: list[dict] = []

    def match(self, words: list[str], base_dir: str = "") -> str | None:
        # Build keyword -> image lookup from all rules
        kw_map = {}
        for rule in self.rules:
            img = rule.get("image", "")
            # Resolve relative paths at match time
            if img and not os.path.isabs(img) and base_dir:
                img = os.path.normpath(os.path.join(base_dir, img))
            for k in rule.get("keywords", []):
                k = k.lower().strip()
                if k:
                    kw_map[k] = img
        # Walk words in order, keep the LAST match so the most
        # recently spoken trigger wins (supports mid-sentence switching)
        last_match = None
        for word in words:
            if word in kw_map:
                last_match = kw_map[word]
        return last_match

    def load(self, path):
        try:
            with open(path) as f:
                self.rules = json.load(f)
        except Exception:
            self.rules = []

    def save(self, path):
        # Store paths relative to the script directory
        base = os.path.dirname(os.path.abspath(__file__))
        portable = []
        for rule in self.rules:
            r = dict(rule)
            try:
                r["image"] = os.path.relpath(r["image"], base).replace("\\", "/")
            except ValueError:
                pass  # different drive, keep absolute
            portable.append(r)
        with open(path, "w") as f:
            json.dump(portable, f, indent=2)


# ─────────────────────────────────────────────────────────────
# Speech Thread (Vosk, 100% offline)
# ─────────────────────────────────────────────────────────────
class SpeechThread(threading.Thread):
    def __init__(self, model_path, on_words, on_volume):
        super().__init__(daemon=True)
        self.model_path = model_path
        self.on_words   = on_words
        self.on_volume  = on_volume
        self._stop      = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        try:
            import sounddevice as sd
            import numpy as np
            from vosk import Model, KaldiRecognizer
        except ImportError as e:
            print(f"\n[MISSING] {e}")
            print("Run:  pip install vosk sounddevice numpy Pillow\n")
            return

        try:
            model = Model(self.model_path)
        except Exception as e:
            print(f"\n[MODEL ERROR] {e}\n")
            return

        rec = KaldiRecognizer(model, 16000)
        q   = queue.Queue()

        def cb(indata, frames, t, status):
            import numpy as np
            vol = float(np.abs(indata).mean() / 32768)
            self.on_volume(min(vol * 40, 1.0))
            q.put(bytes(indata))

        with sd.RawInputStream(samplerate=16000, blocksize=4000,
                               dtype="int16", channels=1, callback=cb):
            while not self._stop.is_set():
                try:
                    data = q.get(timeout=0.5)
                except queue.Empty:
                    continue

                if rec.AcceptWaveform(data):
                    text = json.loads(rec.Result()).get("text", "").strip()
                else:
                    text = json.loads(rec.PartialResult()).get("partial", "").strip()

                if text:
                    words = re.findall(r"[a-z]+", text.lower())
                    if words:
                        self.on_words(words)


# ─────────────────────────────────────────────────────────────
# Control Panel
# ─────────────────────────────────────────────────────────────
class App(tk.Tk):
    _DATA_DIR     = os.path.dirname(os.path.abspath(__file__))
    RULES_FILE    = os.path.join(_DATA_DIR, "rules.json")
    SETTINGS_FILE = os.path.join(_DATA_DIR, "settings.json")

    @classmethod
    def _ensure_data_dir(cls):
        os.makedirs(cls._DATA_DIR, exist_ok=True)

    def __init__(self):
        super().__init__()
        self.title("AI Virtual Camera – Control Panel")
        self.geometry("740x820")
        self.resizable(False, True)
        self.configure(bg="#111")

        self._ensure_data_dir()
        self.engine         = RuleEngine()
        self.cam_win        = None
        self.speech         = None
        self._last_img      = None
        self._silence_job   = None

        # Vosk model is always one directory up in a folder named "Vosk"
        self.v_gif     = tk.StringVar(value="")
        self.v_default = tk.StringVar(value="")
        self.v_silence = tk.IntVar(value=3)

        self._load_settings()
        self._build_ui()
        self.engine.load(self.RULES_FILE)
        self._refresh_tree()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI ────────────────────────────────────────────────────
    def _build_ui(self):
        tk.Label(self, text="AI VIRTUAL CAMERA", bg="#111", fg="#444",
                 font=("Consolas", 12, "bold")).pack(pady=(14, 1))
        tk.Label(self, text="keyword → image  |  free & offline",
                 bg="#111", fg="#2a2a2a", font=("Consolas", 9)).pack()

        # ── Vosk model ──
        self._section("VOSK MODEL")
        tk.Label(self, text="  ↳ Vosk model: place your model inside a folder named \"Vosk\" one level above this script",
                 bg="#111", fg="#2e2e2e", font=("Consolas", 8)).pack(anchor="w", padx=18)

        # ── Idle / silence ──
        self._section("IDLE & SILENCE")
        self._file_row("Idle GIF:",        self.v_gif,     self._pick_gif)
        self._file_row("Default image:",   self.v_default, self._pick_default)

        sil = tk.Frame(self, bg="#111")
        sil.pack(fill=tk.X, padx=18, pady=3)
        tk.Label(sil, text="Silence timeout:", width=18, anchor="w",
                 bg="#111", fg="#555", font=("Consolas", 9)).pack(side=tk.LEFT)
        tk.Spinbox(sil, textvariable=self.v_silence, from_=1, to=30, width=4,
                   bg="#0d0d0d", fg="#ccc", buttonbackground="#1e1e1e",
                   relief="flat", font=("Consolas", 10)).pack(side=tk.LEFT)
        tk.Label(sil, text=" seconds", bg="#111", fg="#555",
                 font=("Consolas", 9)).pack(side=tk.LEFT)

        # ── Rules ──
        self._section("KEYWORD → IMAGE RULES")

        editor = tk.Frame(self, bg="#111")
        editor.pack(fill=tk.X, padx=18, pady=(4, 2))

        tk.Label(editor, text="Keywords:", width=12, anchor="w",
                 bg="#111", fg="#555", font=("Consolas", 9)).grid(row=0, column=0, sticky="w", pady=2)
        self.e_kw = self._entry(editor, width=46)
        self.e_kw.grid(row=0, column=1, sticky="ew", padx=(0, 4), pady=2)

        tk.Label(editor, text="Image file:", width=12, anchor="w",
                 bg="#111", fg="#555", font=("Consolas", 9)).grid(row=1, column=0, sticky="w", pady=2)
        img_f = tk.Frame(editor, bg="#111")
        img_f.grid(row=1, column=1, sticky="ew", pady=2)
        self.e_img = self._entry(img_f, width=38)
        self.e_img.pack(side=tk.LEFT)
        self._btn(img_f, "Browse…", self._pick_rule_img).pack(side=tk.LEFT, padx=(4, 0))
        editor.columnconfigure(1, weight=1)

        btns = tk.Frame(self, bg="#111")
        btns.pack(fill=tk.X, padx=18, pady=(2, 6))
        self._btn(btns, "+ Add Rule",      self._add_rule,  fg="#4f4").pack(side=tk.LEFT, padx=(0, 4))
        self._btn(btns, "Delete",          self._del_rule,  fg="#f44").pack(side=tk.LEFT, padx=4)
        self._btn(btns, "▲",               self._move_up).pack(side=tk.LEFT, padx=4)
        self._btn(btns, "▼",               self._move_down).pack(side=tk.LEFT, padx=4)

        tree_f = tk.Frame(self, bg="#0a0a0a")
        tree_f.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 8))

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", background="#0a0a0a", foreground="#888",
                        fieldbackground="#0a0a0a", font=("Consolas", 9), rowheight=22)
        style.configure("Treeview.Heading", background="#141414",
                        foreground="#444", font=("Consolas", 9, "bold"))
        style.map("Treeview", background=[("selected", "#1a3a1a")],
                  foreground=[("selected", "#4f4")])

        self.tree = ttk.Treeview(tree_f, columns=("#", "keywords", "image"),
                                 show="headings", height=9, selectmode="browse")
        self.tree.heading("#",        text="#")
        self.tree.heading("keywords", text="KEYWORDS")
        self.tree.heading("image",    text="IMAGE")
        self.tree.column("#",        width=28, stretch=False)
        self.tree.column("keywords", width=220)
        self.tree.column("image",    width=340)
        sb = ttk.Scrollbar(tree_f, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        # ── Volume meter ──
        self._section("MIC LEVEL")
        self.vol_c = tk.Canvas(self, height=12, bg="#0a0a0a",
                               highlightthickness=0)
        self.vol_c.pack(fill=tk.X, padx=18, pady=(4, 8))
        self._vol_bar = self.vol_c.create_rectangle(0, 0, 0, 12,
                                                     fill="#4af", outline="")

        # ── Start/Stop ──
        ctrl = tk.Frame(self, bg="#111")
        ctrl.pack(pady=10)
        self.btn_start = tk.Button(
            ctrl, text="▶  START", command=self._toggle,
            bg="#0f2a0f", fg="#4f4", font=("Consolas", 13, "bold"),
            relief="flat", cursor="hand2", padx=28, pady=10,
            activebackground="#1a3a1a", activeforeground="#6f6")
        self.btn_start.pack(side=tk.LEFT, padx=10)

        self.lbl_status = tk.Label(ctrl, text="stopped", bg="#111", fg="#333",
                                   font=("Consolas", 10))
        self.lbl_status.pack(side=tk.LEFT)

    # ── widget helpers ─────────────────────────────────────────
    def _section(self, title):
        tk.Label(self, text=f" {title} ", bg="#111", fg="#333",
                 font=("Consolas", 8, "bold")).pack(anchor="w", padx=14,
                                                     pady=(10, 2))
        tk.Frame(self, bg="#1e1e1e", height=1).pack(fill=tk.X, padx=14)

    def _entry(self, parent, width=36):
        return tk.Entry(parent, width=width, bg="#0d0d0d", fg="#ccc",
                        insertbackground="#ccc", relief="flat",
                        font=("Consolas", 10), bd=0,
                        highlightthickness=1, highlightcolor="#333",
                        highlightbackground="#1e1e1e")

    def _btn(self, parent, text, cmd, fg="#999"):
        return tk.Button(parent, text=text, command=cmd,
                         bg="#1a1a1a", fg=fg, relief="flat",
                         font=("Consolas", 9), cursor="hand2",
                         activebackground="#252525", activeforeground="#fff",
                         padx=8, pady=3)

    def _file_row(self, label, var, cmd, is_dir=False):
        f = tk.Frame(self, bg="#111")
        f.pack(fill=tk.X, padx=18, pady=3)
        tk.Label(f, text=label, width=18, anchor="w", bg="#111",
                 fg="#555", font=("Consolas", 9)).pack(side=tk.LEFT)
        e = self._entry(f, width=34)
        e.pack(side=tk.LEFT)
        e.insert(0, var.get())
        e.bind("<FocusOut>", lambda _: var.set(e.get()))
        self._btn(f, "Browse…", cmd).pack(side=tk.LEFT, padx=(4, 0))

    # ── rule management ───────────────────────────────────────
    def _refresh_tree(self):
        for r in self.tree.get_children():
            self.tree.delete(r)
        for i, rule in enumerate(self.engine.rules):
            kws = ", ".join(rule.get("keywords", []))
            img = os.path.basename(rule.get("image", ""))
            self.tree.insert("", "end", iid=str(i), values=(i + 1, kws, img))

    def _add_rule(self):
        kws = [k.strip() for k in self.e_kw.get().split(",") if k.strip()]
        img = self.e_img.get().strip()
        if not kws:
            messagebox.showwarning("Missing", "Enter at least one keyword."); return
        if not img:
            messagebox.showwarning("Missing", "Choose an image."); return
        self.engine.rules.append({"keywords": kws, "image": img})
        self.engine.save(self.RULES_FILE)
        self._refresh_tree()
        self.e_kw.delete(0, tk.END)
        self.e_img.delete(0, tk.END)

    def _del_rule(self):
        sel = self.tree.selection()
        if not sel: return
        self.engine.rules.pop(int(sel[0]))
        self.engine.save(self.RULES_FILE)
        self._refresh_tree()

    def _move_up(self):
        sel = self.tree.selection()
        if not sel: return
        i = int(sel[0])
        if i == 0: return
        r = self.engine.rules
        r[i-1], r[i] = r[i], r[i-1]
        self.engine.save(self.RULES_FILE)
        self._refresh_tree()
        self.tree.selection_set(str(i-1))

    def _move_down(self):
        sel = self.tree.selection()
        if not sel: return
        i = int(sel[0])
        if i >= len(self.engine.rules) - 1: return
        r = self.engine.rules
        r[i+1], r[i] = r[i], r[i+1]
        self.engine.save(self.RULES_FILE)
        self._refresh_tree()
        self.tree.selection_set(str(i+1))

    def _on_select(self, _):
        sel = self.tree.selection()
        if not sel: return
        rule = self.engine.rules[int(sel[0])]
        self.e_kw.delete(0, tk.END)
        self.e_kw.insert(0, ", ".join(rule.get("keywords", [])))
        self.e_img.delete(0, tk.END)
        self.e_img.insert(0, rule.get("image", ""))

    # ── file pickers ──────────────────────────────────────────
    @property
    def _images_dir(self):
        d = os.path.join(self._DATA_DIR, "images")
        return d if os.path.isdir(d) else self._DATA_DIR

    def _pick_model(self):
        pass  # model path is fixed

    def _pick_gif(self):
        f = filedialog.askopenfilename(title="Select idle GIF",
            initialdir=self._images_dir,
            filetypes=[("GIF", "*.gif"), ("All", "*.*")])
        if f: self.v_gif.set(f)

    def _pick_default(self):
        f = filedialog.askopenfilename(title="Select default image",
            initialdir=self._images_dir,
            filetypes=[("Images", "*.png *.jpg *.jpeg *.webp *.gif"),
                       ("All", "*.*")])
        if f: self.v_default.set(f)

    def _pick_rule_img(self):
        f = filedialog.askopenfilename(title="Select image for rule",
            initialdir=self._images_dir,
            filetypes=[("Images", "*.png *.jpg *.jpeg *.webp *.gif"),
                       ("All", "*.*")])
        if f:
            self.e_img.delete(0, tk.END)
            self.e_img.insert(0, f)

    # ── start / stop ──────────────────────────────────────────
    def _toggle(self):
        if self.speech and self.speech.is_alive():
            self._stop()
        else:
            self._start()

    def _start(self):
        model = os.path.normpath(os.path.join(self._DATA_DIR, "Vosk"))
        if not os.path.isdir(model):
            messagebox.showerror("Vosk model not found",
                f"Expected a folder named \"Vosk\" here:\n{model}\n\n"
                "Download a model from:\nhttps://alphacephei.com/vosk/models\n\n"
                "Extract it into that Vosk folder so it contains a subfolder like\n"
                "vosk-model-small-en-us-0.15")
            return

        if self.cam_win is None or not self.cam_win.winfo_exists():
            self.cam_win = CameraWindow(self)
        self.cam_win.show_gif(self.v_gif.get())

        self.speech = SpeechThread(model, self._on_words, self._on_volume)
        self.speech.start()

        self.btn_start.config(text="■  STOP", bg="#2a0f0f", fg="#f44")
        self.lbl_status.config(text="listening…", fg="#4af")
        self._save_settings()

    def _stop(self):
        if self.speech:
            self.speech.stop()
            self.speech = None
        if self._silence_job:
            self.after_cancel(self._silence_job)
            self._silence_job = None
        self.btn_start.config(text="▶  START", bg="#0f2a0f", fg="#4f4")
        self.lbl_status.config(text="stopped", fg="#333")

    # ── speech callbacks ──────────────────────────────────────
    def _on_words(self, words):
        self.after(0, self._handle_words, words)

    def _on_volume(self, vol):
        self.after(0, self._draw_vol, vol)

    def _handle_words(self, words):
        if self._silence_job:
            self.after_cancel(self._silence_job)
        self._silence_job = self.after(self.v_silence.get() * 1000, self._go_idle)

        matched = self.engine.match(words, base_dir=self._DATA_DIR)
        target  = matched or self._resolve(self.v_default.get()) or None

        if target and os.path.exists(target):
            if target != self._last_img:
                self._last_img = target
                if self.cam_win and self.cam_win.winfo_exists():
                    self.cam_win.show_image(target)
        elif self.cam_win and self.cam_win.winfo_exists():
            self.cam_win.badge.config(text="● LIVE", fg="#fa4")

        snippet = " ".join(words[:7])
        match_name = os.path.basename(matched) if matched else "no match"
        self.lbl_status.config(
            text=f'heard: "{snippet}"  →  {match_name}', fg="#4af")

    def _resolve(self, path):
        """Resolve a possibly-relative path against the script directory."""
        if not path:
            return path
        if not os.path.isabs(path):
            path = os.path.normpath(os.path.join(self._DATA_DIR, path))
        return path

    def _go_idle(self):
        self._last_img    = None
        self._silence_job = None
        if self.cam_win and self.cam_win.winfo_exists():
            self.cam_win.show_gif(self._resolve(self.v_gif.get()))
        self.lbl_status.config(text="idle", fg="#555")

    def _draw_vol(self, vol):
        w = self.vol_c.winfo_width()
        self.vol_c.coords(self._vol_bar, 0, 0, int(w * vol), 12)
        color = "#4af" if vol < 0.6 else "#fa4" if vol < 0.85 else "#f44"
        self.vol_c.itemconfig(self._vol_bar, fill=color)

    # ── settings ──────────────────────────────────────────────
    def _save_settings(self):
        try:
            with open(self.SETTINGS_FILE, "w") as f:
                json.dump({
                    # model path is fixed, not saved
                    "gif":     self.v_gif.get(),
                    "default": self.v_default.get(),
                    "silence": self.v_silence.get(),
                }, f, indent=2)
        except Exception:
            pass

    def _load_settings(self):
        try:
            with open(self.SETTINGS_FILE) as f:
                s = json.load(f)
            pass  # model path is fixed
            self.v_gif.set(s.get("gif", ""))
            self.v_default.set(s.get("default", ""))
            self.v_silence.set(s.get("silence", 3))
        except Exception:
            pass

    def _on_close(self):
        self._save_settings()
        self._stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
