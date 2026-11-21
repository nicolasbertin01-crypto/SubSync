# -*- coding: utf-8 -*-
"""SubSync - interface graphique."""
import os, sys, queue, threading, traceback, subprocess, time, shutil
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import customtkinter as ctk

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
if sys.stdout is None or sys.stderr is None:  # lancement sans console (pythonw)
    os.makedirs(os.path.join(APP_DIR, "logs"), exist_ok=True)
    _f = open(os.path.join(APP_DIR, "logs", "console.log"), "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _f; sys.stderr = sys.stderr or _f
import subsync_engine as eng
import subsync_merge as merge

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except Exception:
    HAS_DND = False

import subsync_update as upd
VERSION = upd.local_version() if upd.local_version() != "0" else "1.6"
LANGS = ["fr", "en", "es", "de", "it", "pt", "nl"]
ENH_PRESETS = {"Fidèle (conseillé)": "fidele", "Net": "net", "Lissé (sources très bruitées)": "lisse"}
END_ACTIONS = {"À la fin : rien": "", "À la fin : mettre en veille": "sleep", "À la fin : éteindre le PC": "shutdown"}
QUEUE_FILE = os.path.join(APP_DIR, "file_attente.json")
ENH_PRECLEAN = {"Aucun": "", "Léger (vidéos compressées)": "leger", "Fort (blocs très visibles en mouvement)": "fort"}
ENH_GRAIN = {"Garder": "", "Réduire (conseillé)": "reduire", "Fortement réduire": "fort"}
ENH_SIZE = {"Compact ≈ 1 Go (conseillé)": "compact", "Qualité maximale ≈ 3 Go": "max"}
ENH_FACES = {"Naturels (GFPGAN, conseillé)": "gfpgan", "Détaillés (GPEN)": "gpen", "Ne pas retoucher les visages": ""}
VIDEO_PY = os.path.join(APP_DIR, ".venv_video", "Scripts", "python.exe") if os.name == "nt" else os.environ.get("SUBSYNC_VIDEO_PY", sys.executable)


class PreviewWindow(ctk.CTkToplevel):
    """Comparaison avant / après : image fixe ou vidéo, séparation à glisser, zoom à la molette (synchronisé
    des deux côtés), déplacement clic droit glissé, double-clic = vue entière."""
    W, H = 960, 720

    def __init__(self, app, pv, other=None):
        super().__init__(app)
        self.title("Aperçu — " + os.path.basename(pv["video"])); self.geometry("1000x930"); self.transient(app)
        self.pv = pv; self.other = other; self.idx = 0; self.split = 0.5
        self.z = 1.0; self.cx = 0.5; self.cy = 0.5          # zoom et centre de la vue (coordonnées 0-1)
        self.playing = False; self._dec = None; self._frame = None; self._pan = None
        d = pv["data"]
        ctk.CTkLabel(self, text="Réglage : " + pv["label"], font=ctk.CTkFont(weight="bold"), wraplength=960, justify="left").pack(anchor="w", padx=14, pady=(10, 0))
        est = ""
        if d.get("est_seconds"):
            m = int(d["est_seconds"] // 60)
            est = "Épisode complet : environ %s de traitement (%.1f images/s) · fichier ≈ %d Mo (%.1f Mb/s pour l'image)" % (
                ("%d h %02d min" % (m // 60, m % 60)) if m >= 60 else "%d min" % m, d.get("fps", 0), round(d.get("est_size_mb", 0)), d.get("mbps", 0))
        ctk.CTkLabel(self, text=est, text_color=("gray30", "gray70")).pack(anchor="w", padx=14)
        bar = ctk.CTkFrame(self, fg_color="transparent"); bar.pack(fill="x", padx=14, pady=6)
        items = d.get("items", [])
        if len(items) > 1:
            sb = ctk.CTkSegmentedButton(bar, values=["Extrait %d (%d:%02d)" % (i + 1, int(it["time"]) // 60, int(it["time"]) % 60) for i, it in enumerate(items)],
                                        command=self._pick)
            sb.set(sb.cget("values")[0]); sb.pack(side="left")
        self.left = ctk.StringVar(value="Original")
        if other:
            ctk.CTkLabel(bar, text="  À gauche :").pack(side="left")
            ctk.CTkSegmentedButton(bar, values=["Original", "Réglage précédent"], variable=self.left, command=lambda v: self._reload()).pack(side="left", padx=4)
        ctk.CTkButton(bar, text="Ouvrir dans le lecteur", width=150, fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=self._video).pack(side="right")
        bar2 = ctk.CTkFrame(self, fg_color="transparent"); bar2.pack(fill="x", padx=14)
        self.play_btn = ctk.CTkButton(bar2, text="▶  Lire la vidéo", width=140, command=self._toggle_play); self.play_btn.pack(side="left")
        ctk.CTkButton(bar2, text="Image suivante ▸", width=130, command=self._step).pack(side="left", padx=6)
        self.zlabel = ctk.CTkLabel(bar2, text="Zoom ×1"); self.zlabel.pack(side="left", padx=10)
        ctk.CTkButton(bar2, text="Vue entière", width=100, command=self._reset).pack(side="left")
        self.cv = ctk.CTkCanvas(self, width=self.W, height=self.H, highlightthickness=0, bg="black"); self.cv.pack(padx=14, pady=4)
        self.legend = ctk.CTkLabel(self, text=""); self.legend.pack(anchor="w", padx=14)
        ctk.CTkLabel(self, text="Clic gauche glissé : séparation avant/après · molette : zoom (les deux côtés) · clic droit glissé : déplacer · double-clic : vue entière",
                     text_color=("gray35", "gray65")).pack(anchor="w", padx=14, pady=(0, 10))
        self.cv.bind("<B1-Motion>", self._drag); self.cv.bind("<Button-1>", self._drag)
        self.cv.bind("<MouseWheel>", self._wheel)                                   # Windows
        self.cv.bind("<Button-4>", lambda e: self._wheel(e, 120)); self.cv.bind("<Button-5>", lambda e: self._wheel(e, -120))
        self.cv.bind("<ButtonPress-3>", self._pan_start); self.cv.bind("<B3-Motion>", self._pan_move)
        self.cv.bind("<Double-Button-1>", lambda e: self._reset())
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._load(); self._draw()

    # --- données
    def _pick(self, v):
        self.idx = int(v.split()[1]) - 1; self._reload()

    def _reload(self):
        was = self.playing; self._stop_video(); self._load(); self._draw()
        if was: self._toggle_play()

    def _load(self):
        from PIL import Image
        it = self.pv["data"]["items"][self.idx]
        self.after_img = Image.open(it["apres"]).convert("RGB")
        self.before_img = Image.open(it["avant"]).convert("RGB").resize(self.after_img.size)
        self.prev_img = None
        if self.other:
            try: self.prev_img = Image.open(self.other["data"]["items"][self.idx]["apres"]).convert("RGB").resize(self.after_img.size)
            except Exception: self.prev_img = None
        self._frame = None

    def _use_prev(self):
        return self.left.get() == "Réglage précédent" and self.prev_img is not None

    # --- vidéo (les fichiers côte à côte : moitié gauche = original, moitié droite = amélioré)
    def _sbs(self, pv):
        v = pv["data"]["items"][self.idx]["video"]
        return v if v.endswith(".mp4") else None

    def _toggle_play(self):
        if self.playing:
            self.playing = False; self.play_btn.configure(text="▶  Lire la vidéo"); return
        if self._dec is None:
            import av
            cur = self._sbs(self.pv); prv = self._sbs(self.other) if (self.other and self._use_prev()) else None
            if not cur:
                messagebox.showinfo("SubSync", "Vidéo d'aperçu indisponible.", parent=self); return
            try:
                self._dec = [av.open(cur)] + ([av.open(prv)] if prv else [])
                self._it = [c.decode(video=0) for c in self._dec]
                self._fps = float(self._dec[0].streams.video[0].average_rate or 24)
            except Exception as e:
                self._dec = None; messagebox.showinfo("SubSync", "Lecture impossible : %s" % e, parent=self); return
        self.playing = True; self.play_btn.configure(text="⏸  Pause"); self._tick()

    def _next_frames(self):
        try:
            fr = [next(i).to_image() for i in self._it]
        except StopIteration:                       # boucle
            self._stop_video(); self._toggle_play_reopen = True
            return None
        w = fr[0].width // 2
        a = fr[1].crop((w, 0, 2 * w, fr[1].height)) if len(fr) > 1 else fr[0].crop((0, 0, w, fr[0].height))
        b = fr[0].crop((w, 0, 2 * w, fr[0].height))
        return a, b

    def _tick(self):
        if not self.playing: return
        t0 = time.time()
        f = self._next_frames()
        if f is None:
            if getattr(self, "_toggle_play_reopen", False):
                self._toggle_play_reopen = False; self._toggle_play()
            return
        self._frame = f; self._draw()
        self.after(max(1, int(1000 / self._fps - (time.time() - t0) * 1000)), self._tick)

    def _step(self):
        if self.playing: self._toggle_play()
        if self._dec is None:
            self._toggle_play(); self.playing = False; self.play_btn.configure(text="▶  Lire la vidéo")
        f = self._next_frames()
        if f is None:
            self._toggle_play_reopen = False
            return
        self._frame = f; self._draw()

    def _stop_video(self):
        self.playing = False
        try: self.play_btn.configure(text="▶  Lire la vidéo")
        except Exception: pass
        for c in (self._dec or []):
            try: c.close()
            except Exception: pass
        self._dec = None; self._frame = None

    def _close(self):
        self._stop_video(); self.destroy()

    # --- affichage
    def _view(self, im):
        w, h = im.size; cw, ch = w / self.z, h / self.z
        x0 = min(max(0.0, self.cx * w - cw / 2), w - cw); y0 = min(max(0.0, self.cy * h - ch / 2), h - ch)
        self.cx = (x0 + cw / 2) / w; self.cy = (y0 + ch / 2) / h
        return im.crop((int(x0), int(y0), int(x0 + cw), int(y0 + ch))).resize((self.W, self.H))

    def _draw(self):
        from PIL import ImageTk
        if self._frame is not None:
            left, right = self._frame
        else:
            left = self.prev_img if self._use_prev() else self.before_img; right = self.after_img
        if left.size != right.size: left = left.resize(right.size)
        a = self._view(left); b = self._view(right)
        x = int(self.split * self.W)
        comp = b.copy(); comp.paste(a.crop((0, 0, x, self.H)), (0, 0))
        self._tk = ImageTk.PhotoImage(comp)
        self.cv.delete("all"); self.cv.create_image(0, 0, image=self._tk, anchor="nw")
        self.cv.create_line(x, 0, x, self.H, fill="white", width=2)
        lt = "Réglage précédent : " + self.other["label"] if self._use_prev() else "Original (agrandi simplement)"
        self.legend.configure(text="◀ %s      |      Réglage actuel ▶" % lt)
        self.zlabel.configure(text="Zoom ×%g" % round(self.z, 1))

    def _drag(self, ev):
        self.split = min(1.0, max(0.0, ev.x / self.W)); self._draw()

    def _wheel(self, ev, delta=None):
        d = delta if delta is not None else ev.delta
        mx, my = ev.x / self.W, ev.y / self.H
        # point de l'image sous la souris, avant zoom
        px = self.cx + (mx - 0.5) / self.z; py = self.cy + (my - 0.5) / self.z
        self.z = min(8.0, max(1.0, self.z * (1.25 if d > 0 else 0.8)))
        if self.z <= 1.0001: self.cx = self.cy = 0.5
        else: self.cx = px - (mx - 0.5) / self.z; self.cy = py - (my - 0.5) / self.z
        self._draw()

    def _pan_start(self, ev):
        self._pan = (ev.x, ev.y, self.cx, self.cy)

    def _pan_move(self, ev):
        if not self._pan or self.z <= 1: return
        x0, y0, cx, cy = self._pan
        self.cx = cx - (ev.x - x0) / self.W / self.z; self.cy = cy - (ev.y - y0) / self.H / self.z; self._draw()

    def _reset(self):
        self.z = 1.0; self.cx = self.cy = 0.5; self._draw()

    def _video(self):
        v = self.pv["data"]["items"][self.idx]["video"]
        try:
            if os.name == "nt": os.startfile(v)
            else: subprocess.Popen(["xdg-open", v])
        except Exception as e:
            messagebox.showinfo("SubSync", "Impossible d'ouvrir la vidéo : %s" % e, parent=self)


class ModePicker(ctk.CTkFrame):
    """Choix du mode sur deux lignes (synchronisation / autres outils), utilisé comme un seul bouton segmenté."""
    ROWS = (["Automatique", "Simple", "Avancé"], ["Créer", "Fusion VF", "Améliorer"])

    def __init__(self, master):
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(0, weight=1); self._cmd = None; self._val = None
        self.segs = []
        for i, vals in enumerate(self.ROWS):
            sb = ctk.CTkSegmentedButton(self, values=vals, command=lambda v, i=i: self._pick(i, v))
            sb.grid(row=i, column=0, sticky="ew", pady=(0, 4)); self.segs.append(sb)

    def _pick(self, i, v):
        self.set(v)
        if self._cmd: self._cmd(v)

    def set(self, v):
        self._val = v
        for vals, sb in zip(self.ROWS, self.segs): sb.set(v if v in vals else "")

    def get(self):
        return self._val

    def configure(self, **kw):
        if "command" in kw: self._cmd = kw.pop("command")
        if "state" in kw:
            for sb in self.segs: sb.configure(state=kw["state"])
            kw.pop("state")
        if kw: super().configure(**kw)


def lang_title(code):
    return {"fre": "Français", "eng": "Anglais", "ger": "Allemand", "spa": "Espagnol", "ita": "Italien"}.get(code, code)


def video_ready():
    d = os.path.join(APP_DIR, "models", "video")
    return os.path.exists(VIDEO_PY) and all(os.path.exists(os.path.join(d, f)) for f in ("upscale_equilibre.onnx", "scrfd_2.5g.onnx", "gpen_bfr_512.onnx", "gfpgan_1.4.onnx"))


MODES = {"Automatique": "auto", "Simple": "simple", "Avancé": "advanced", "Créer": "create", "Fusion VF": "merge", "Améliorer": "enhance"}
STATE_TXT = {"created": "✎ Créé (à relire)", "wait": "En attente", "run": "En cours…", "ok": "✔ Bon", "warn": "⚠ À vérifier", "bad": "✖ Douteux",
             "err": "✖ Erreur", "nopair": "Vidéo manquante", "cancel": "Annulé", "video": "Sans sous-titres"}
COLORS = {"created": "#3a7bd5", "ok": "#2e9d5b", "warn": "#d08a1a", "bad": "#c94040", "err": "#c94040"}


class Row:
    def __init__(self, srt=None, videos=None, kind="sync"):
        self.srt = srt; self.videos = list(videos or []); self.state = "wait"; self.kind = kind
        self.results = []; self.error = ""; self.shift = 0.0


BaseTk = ctk.CTk
if HAS_DND:
    class BaseTk(ctk.CTk, TkinterDnD.DnDWrapper):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.TkdndVersion = TkinterDnD._require(self)


class App(BaseTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")
        self.title("SubSync %s — synchronisation automatique de sous-titres et amélioration vidéo" % VERSION)
        self.geometry("1220x800"); self.minsize(1040, 680)
        ico = os.path.join(APP_DIR, "assets", "subsync.ico")
        if os.name == "nt" and os.path.exists(ico):
            try: self.iconbitmap(ico)
            except Exception: pass
        self.rows = {}; self.q = queue.Queue(); self.worker = None; self.stop_flag = False
        self.sync_rows = []; self.fus_rows = []; self.view = "sync"
        self.out_dir = None
        self._build()
        self.after(100, self._poll)
        self._log("SubSync prêt. Ajoutez vos fichiers .srt et vos vidéos (ou glissez-les dans la fenêtre).")
        self.after(4000, self._auto_update_check)
        self.after(1200, self._offer_resume)
        if not eng.alass_exe():
            self._log("Attention : alass introuvable dans le dossier bin — seule la correction de décalage global sera possible.")

    # ------------------------------------------------------------------ interface
    def _build(self):
        self.grid_columnconfigure(0, weight=1); self.grid_rowconfigure(2, weight=1)
        head = ctk.CTkFrame(self, fg_color="transparent"); head.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 4))
        ctk.CTkLabel(head, text="SubSync", font=ctk.CTkFont(size=26, weight="bold")).pack(side="left")
        ctk.CTkLabel(head, text="   Sous-titres recalés ou créés, voix française ajoutée, image améliorée.",
                     font=ctk.CTkFont(size=14), text_color=("gray35", "gray70")).pack(side="left", pady=(6, 0))

        bar = ctk.CTkFrame(self, fg_color="transparent"); bar.grid(row=1, column=0, sticky="ew", padx=18, pady=6)
        ctk.CTkButton(bar, text="＋ Ajouter des fichiers", width=170, command=self.add_files).pack(side="left")
        ctk.CTkButton(bar, text="＋ Ajouter un dossier", width=160, command=self.add_folder).pack(side="left", padx=8)
        ctk.CTkButton(bar, text="Associer une vidéo…", width=150, fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=self.assign_video).pack(side="left")
        ctk.CTkButton(bar, text="Retirer", width=80, fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=self.remove_sel).pack(side="left", padx=8)
        ctk.CTkButton(bar, text="Vider", width=70, fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=self.clear).pack(side="left")
        ctk.CTkButton(bar, text="Ouvrir le dossier du résultat", width=190, fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=self.open_folder).pack(side="right")
        self.upd_btn = ctk.CTkButton(bar, text="Mises à jour…", width=120, fg_color="transparent", border_width=1,
                                     text_color=("gray10", "gray90"), command=self.open_updates)
        self.upd_btn.pack(side="right", padx=8)
        ctk.CTkButton(bar, text="Notifications…", width=120, fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=self.open_notifications).pack(side="right")
        hint = "  (glisser-déposer accepté)" if HAS_DND else ""
        ctk.CTkLabel(bar, text=hint, text_color=("gray40", "gray60")).pack(side="left", padx=10)

        mid = ctk.CTkFrame(self, fg_color="transparent"); mid.grid(row=2, column=0, sticky="nsew", padx=18, pady=4)
        mid.grid_columnconfigure(0, weight=3); mid.grid_columnconfigure(1, weight=1, minsize=320); mid.grid_rowconfigure(0, weight=1)

        # tableau
        tf = ctk.CTkFrame(mid); tf.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        tf.grid_columnconfigure(0, weight=1); tf.grid_rowconfigure(0, weight=1)
        self._style_tree()
        self.tree = ttk.Treeview(tf, columns=("srt", "vid", "state", "res"), show="headings", style="S.Treeview", selectmode="extended")
        for c, t, w in (("srt", "Sous-titres", 260), ("vid", "Vidéo(s)", 300), ("state", "État", 110), ("res", "Résultat", 330)):
            self.tree.heading(c, text=t, anchor="w"); self.tree.column(c, width=w, anchor="w", stretch=True)
        for k, col in COLORS.items():
            self.tree.tag_configure(k, foreground=col)
        self.tree.tag_configure("grey", foreground="#8a8a8a")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview); self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8); sb.grid(row=0, column=1, sticky="ns", pady=8, padx=(0, 6))
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_detail())
        self.tree.bind("<Double-1>", lambda e: self.open_folder())
        if HAS_DND:
            for w in (self, self.tree):
                try:
                    w.drop_target_register(DND_FILES); w.dnd_bind("<<Drop>>", self._on_drop)
                except Exception:
                    pass

        # panneau de droite
        side = ctk.CTkScrollableFrame(mid); side.grid(row=0, column=1, sticky="nsew")
        side.grid_columnconfigure(0, weight=1)
        r = 0
        ctk.CTkLabel(side, text="Réglages", font=ctk.CTkFont(size=16, weight="bold")).grid(row=r, column=0, sticky="w", padx=14, pady=(12, 6)); r += 1
        prf = ctk.CTkFrame(side, fg_color="transparent"); prf.grid(row=r, column=0, sticky="ew", padx=14, pady=(0, 8)); r += 1
        ctk.CTkLabel(prf, text="Profil").pack(side="left")
        self.profile = ctk.CTkComboBox(prf, values=self._profile_names(), width=140, command=self._load_profile)
        self.profile.set("—"); self.profile.pack(side="left", padx=6)
        ctk.CTkButton(prf, text="Enregistrer", width=74, command=self._save_profile).pack(side="left")
        ctk.CTkButton(prf, text="✕", width=28, fg_color="transparent", border_width=1, text_color=("gray10", "gray90"),
                      command=self._del_profile).pack(side="left", padx=(4, 0))
        ctk.CTkLabel(side, text="Méthode").grid(row=r, column=0, sticky="w", padx=14); r += 1
        self.mode = ModePicker(side); self.mode.set("Automatique")
        self.mode.grid(row=r, column=0, sticky="ew", padx=14, pady=(2, 2)); r += 1
        self.mode_help = ctk.CTkLabel(side, text="", justify="left", wraplength=262, text_color=("gray35", "gray65"), font=ctk.CTkFont(size=12))
        self.mode_help.grid(row=r, column=0, sticky="w", padx=14, pady=(0, 8)); r += 1
        self.mode.configure(command=lambda v: self._mode_help())
        self._mode_help()
        lf = ctk.CTkFrame(side, fg_color="transparent"); lf.grid(row=r, column=0, sticky="ew", padx=14); r += 1
        self.lang_frame = lf
        ctk.CTkLabel(lf, text="Langue des sous-titres").grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(lf, text="Langue de l'audio").grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.sub_lang = ctk.CTkComboBox(lf, values=LANGS, width=120); self.sub_lang.set("fr"); self.sub_lang.grid(row=1, column=0, sticky="w")
        self.aud_lang = ctk.CTkComboBox(lf, values=LANGS, width=120); self.aud_lang.set("en"); self.aud_lang.grid(row=1, column=1, sticky="w", padx=(12, 0))
        self.qual_row = ctk.CTkFrame(side, fg_color="transparent"); self.qual_row.grid(row=r, column=0, sticky="ew", padx=14, pady=(10, 0)); r += 1
        ctk.CTkLabel(self.qual_row, text="Transcription").grid(row=0, column=0, sticky="w")
        self.quality = ctk.CTkComboBox(self.qual_row, values=["rapide", "précise"], width=120); self.quality.set("rapide")
        self.quality.grid(row=1, column=0, sticky="w")
        ctk.CTkButton(self.qual_row, text="Glossaire…", width=110, command=self.open_glossary).grid(row=1, column=1, padx=(12, 0))
        ctk.CTkLabel(self.qual_row, text="Traduction").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.translator = ctk.CTkComboBox(self.qual_row, values=["IA locale (qualité)", "Standard (rapide)"], width=250)
        self.translator.set("IA locale (qualité)" if eng.llm_ready() else "Standard (rapide)")
        self.translator.grid(row=4, column=0, columnspan=2, sticky="w")
        self.tr_help = ctk.CTkLabel(self.qual_row, text="", text_color=("gray35", "gray65"), font=ctk.CTkFont(size=11), wraplength=262, justify="left")
        self.tr_help.grid(row=5, column=0, columnspan=2, sticky="w")
        self.tr_help.configure(text=("IA locale : Mistral Small 24B sur la carte graphique — contexte, tutoiement/vouvoiement cohérent. ~10-20 min de plus par épisode."
                                     if eng.llm_ready() else "IA locale non installée : lancez « Installer l'IA de traduction.bat » (14 Go)."))
        ctk.CTkLabel(self.qual_row, text="rapide ≈ 10 min/épisode · précise ≈ 30 min/épisode (+1,5 Go la 1re fois)",
                     text_color=("gray35", "gray65"), font=ctk.CTkFont(size=11), wraplength=262, justify="left").grid(row=2, column=0, columnspan=2, sticky="w")
        self.qual_row.grid_remove()
        self.fus_row = ctk.CTkFrame(side, fg_color="transparent"); self.fus_row.grid(row=r, column=0, sticky="ew", padx=14, pady=(10, 0)); r += 1
        ctk.CTkLabel(self.fus_row, text="Garder aussi ces langues de la HD :").grid(row=0, column=0, columnspan=2, sticky="w")
        saved = self._load_settings().get("fusion_langs", ["eng"])
        self.keep_langs = {}
        for k, (code, name) in enumerate([("eng", "Anglais"), ("ger", "Allemand"), ("spa", "Espagnol"), ("ita", "Italien"),
                                          ("jpn", "Japonais"), ("other", "Autres langues")]):
            cb = ctk.CTkCheckBox(self.fus_row, text=name, width=120, command=self._save_fusion_langs)
            cb.grid(row=1 + k // 2, column=k % 2, sticky="w", pady=2)
            if code in saved: cb.select()
            self.keep_langs[code] = cb
        ctk.CTkLabel(self.fus_row, text="Tout décocher = VF seule. Une piste française déjà présente dans la HD est toujours gardée.",
                     text_color=("gray35", "gray65"), font=ctk.CTkFont(size=11), wraplength=262, justify="left").grid(row=4, column=0, columnspan=2, sticky="w")
        self.fus_row.grid_remove()
        # --- amélioration vidéo
        self.enh_row = ctk.CTkFrame(side, fg_color="transparent"); self.enh_row.grid(row=r, column=0, sticky="ew", padx=14, pady=(10, 0)); r += 1
        es = self._load_settings().get("enhance3", {})
        ctk.CTkLabel(self.enh_row, text="Rendu").grid(row=0, column=0, sticky="w")
        self.enh_preset = ctk.CTkComboBox(self.enh_row, values=list(ENH_PRESETS), width=170, command=lambda v: self._save_enh())
        self.enh_preset.set(es.get("preset", "Fidèle (conseillé)")); self.enh_preset.grid(row=1, column=0, sticky="w")
        ctk.CTkLabel(self.enh_row, text="Résolution").grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.enh_res = ctk.CTkComboBox(self.enh_row, values=["1080p", "720p"], width=85, command=lambda v: self._save_enh())
        self.enh_res.set(es.get("res", "1080p")); self.enh_res.grid(row=1, column=1, sticky="w", padx=(10, 0))
        ctk.CTkLabel(self.enh_row, text="Visages").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.enh_faces = ctk.CTkComboBox(self.enh_row, values=list(ENH_FACES), width=265, command=lambda v: self._save_enh())
        self.enh_faces.set(es.get("faces", "Naturels (GFPGAN, conseillé)")); self.enh_faces.grid(row=3, column=0, columnspan=2, sticky="w")
        ctk.CTkLabel(self.enh_row, text="Force sur les visages").grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.enh_fstr = ctk.CTkSlider(self.enh_row, from_=0.2, to=1.0, number_of_steps=16, width=170, command=lambda v: self._save_enh())
        self.enh_fstr.set(es.get("fstr", 0.4)); self.enh_fstr.grid(row=5, column=0, columnspan=2, sticky="w")
        self.enh_fstr_lbl = ctk.CTkLabel(self.enh_row, text="%d %%" % round(es.get("fstr", 0.4) * 100)); self.enh_fstr_lbl.grid(row=5, column=1, sticky="e")
        ctk.CTkLabel(self.enh_row, text="Nettoyage de la source (avant l'IA)").grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.enh_pre = ctk.CTkComboBox(self.enh_row, values=list(ENH_PRECLEAN), width=265, command=lambda v: self._save_enh())
        self.enh_pre.set(es.get("pre", "Aucun")); self.enh_pre.grid(row=7, column=0, columnspan=2, sticky="w")
        ctk.CTkLabel(self.enh_row, text="Grain (après l'IA)").grid(row=8, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.enh_grain = ctk.CTkComboBox(self.enh_row, values=list(ENH_GRAIN), width=265, command=lambda v: self._save_enh())
        self.enh_grain.set(es.get("grain", "Réduire (conseillé)")); self.enh_grain.grid(row=9, column=0, columnspan=2, sticky="w")
        ctk.CTkLabel(self.enh_row, text="Fichier").grid(row=15, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.enh_size = ctk.CTkComboBox(self.enh_row, values=list(ENH_SIZE), width=265, command=lambda v: self._save_enh())
        self.enh_size.set(es.get("size", "Compact ≈ 1 Go (conseillé)")); self.enh_size.grid(row=16, column=0, columnspan=2, sticky="w")
        pf2 = ctk.CTkFrame(self.enh_row, fg_color="transparent"); pf2.grid(row=18, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        ctk.CTkLabel(self.enh_row, text="Aperçu : moment (facultatif) et durée des extraits").grid(row=17, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.enh_time = ctk.CTkEntry(pf2, width=62, placeholder_text="10:05"); self.enh_time.pack(side="left", padx=(0, 4))
        self.enh_pdur = ctk.CTkComboBox(pf2, values=["3 s", "5 s", "10 s", "20 s", "30 s", "60 s"], width=72, command=lambda v: self._save_enh())
        self.enh_pdur.set(es.get("pdur", "5 s")); self.enh_pdur.pack(side="left", padx=(0, 6))
        self.prev_btn = ctk.CTkButton(pf2, text="Aperçu", width=80, command=self.preview); self.prev_btn.pack(side="left")
        self.enh_help = ctk.CTkLabel(self.enh_row, text="", text_color=("gray35", "gray65"), font=ctk.CTkFont(size=11), wraplength=262, justify="left")
        self.enh_help.grid(row=19, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ctk.CTkLabel(self.enh_row, text="Corrections automatiques").grid(row=10, column=0, columnspan=2, sticky="w", pady=(8, 0))
        cf = ctk.CTkFrame(self.enh_row, fg_color="transparent"); cf.grid(row=11, column=0, columnspan=2, sticky="w")
        self.enh_deint = ctk.CTkCheckBox(cf, text="Désentrelacer si nécessaire", command=self._save_enh)
        self.enh_crop = ctk.CTkCheckBox(cf, text="Rogner les bandes noires", command=self._save_enh)
        self.enh_color = ctk.CTkCheckBox(cf, text="Raviver couleurs et contraste (doux)", command=self._save_enh)
        for i_, (wdg, key, dflt) in enumerate(((self.enh_deint, "deint", True), (self.enh_crop, "crop", True), (self.enh_color, "color", False))):
            wdg.grid(row=i_, column=0, sticky="w", pady=1)
            if es.get(key, dflt): wdg.select()
        self.enh_row.grid_remove()
        self.chain = ctk.CTkCheckBox(side, text="Tout-en-un : puis améliorer l'image\n(réglages du mode Améliorer, vidéos < 1080p)")
        self.chain.grid(row=r, column=0, sticky="w", padx=14, pady=(10, 0)); r += 1
        if upd.settings().get("chain"): self.chain.select()
        self.chain.configure(command=lambda: upd.save_settings(chain=bool(self.chain.get())))
        self.only_sel = ctk.CTkCheckBox(side, text="Seulement les lignes sélectionnées")
        self.only_sel.grid(row=r, column=0, sticky="w", padx=14, pady=(10, 0)); r += 1
        self.out_title = ctk.CTkLabel(side, text="Enregistrer les sous-titres")
        self.out_title.grid(row=r, column=0, sticky="w", padx=14, pady=(10, 0)); r += 1
        of = ctk.CTkFrame(side, fg_color="transparent"); of.grid(row=r, column=0, sticky="ew", padx=14); r += 1
        of.grid_columnconfigure(0, weight=1)
        self.out_lbl = ctk.CTkLabel(of, text="À côté de chaque vidéo", anchor="w"); self.out_lbl.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(of, text="Changer…", width=80, command=self.choose_out).grid(row=0, column=1, padx=(6, 0))

        ctk.CTkFrame(side, height=2, fg_color=("gray80", "gray30")).grid(row=r, column=0, sticky="ew", padx=14, pady=12); r += 1
        ctk.CTkLabel(side, text="Détail", font=ctk.CTkFont(size=16, weight="bold")).grid(row=r, column=0, sticky="w", padx=14); r += 1
        self.det = ctk.CTkLabel(side, text="Sélectionnez une ligne.", justify="left", anchor="nw", wraplength=262)
        self.det.grid(row=r, column=0, sticky="ew", padx=14, pady=(4, 4)); r += 1
        self.conf = ctk.CTkProgressBar(side); self.conf.set(0); self.conf.grid(row=r, column=0, sticky="ew", padx=14, pady=(2, 8)); r += 1
        ctk.CTkLabel(side, text="Réglage fin (tous les sous-titres de la ligne)").grid(row=r, column=0, sticky="w", padx=14); r += 1
        adj = ctk.CTkFrame(side, fg_color="transparent"); adj.grid(row=r, column=0, sticky="w", padx=14, pady=(2, 2)); r += 1
        for t, d in (("−0,5 s", -0.5), ("−0,1 s", -0.1), ("+0,1 s", 0.1), ("+0,5 s", 0.5)):
            ctk.CTkButton(adj, text=t, width=62, command=lambda d=d: self.nudge(d)).pack(side="left", padx=(0, 5))
        self.shift_lbl = ctk.CTkLabel(side, text="Ajustement manuel : 0,0 s", text_color=("gray35", "gray65"))
        self.shift_lbl.grid(row=r, column=0, sticky="w", padx=14, pady=(0, 12)); r += 1

        # bas : lancement, progression, journal
        bot = ctk.CTkFrame(self, fg_color="transparent"); bot.grid(row=3, column=0, sticky="ew", padx=18, pady=(6, 4))
        bot.grid_columnconfigure(2, weight=1)
        self.go = ctk.CTkButton(bot, text="▶  Lancer la synchronisation", height=40, font=ctk.CTkFont(size=15, weight="bold"), command=self.start)
        self.go.grid(row=0, column=0)
        self.stop_btn = ctk.CTkButton(bot, text="Arrêter", height=40, width=90, fg_color="#9b3b3b", hover_color="#7d2f2f",
                                      command=self.stop, state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=8)
        self.end_action = ctk.CTkComboBox(bot, values=list(END_ACTIONS), width=190, command=lambda v: upd.save_settings(end_action=v))
        self.end_action.set(upd.settings().get("end_action", "À la fin : rien") if upd.settings().get("end_action") in END_ACTIONS else "À la fin : rien")
        self.end_action.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        pf = ctk.CTkFrame(bot, fg_color="transparent"); pf.grid(row=0, column=2, sticky="ew", padx=(8, 0))
        pf.grid_columnconfigure(0, weight=1)
        self.status = ctk.CTkLabel(pf, text="", anchor="w"); self.status.grid(row=0, column=0, sticky="ew")
        self.timer = ctk.CTkLabel(pf, text="", anchor="e", text_color=("gray30", "gray70")); self.timer.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.prog = ctk.CTkProgressBar(pf); self.prog.set(0); self.prog.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.t_start = self.t_item = None
        self.logbox = ctk.CTkTextbox(self, height=120, font=ctk.CTkFont(family="Consolas", size=12))
        self.logbox.grid(row=4, column=0, sticky="ew", padx=18, pady=(4, 14)); self.logbox.configure(state="disabled")

    def _style_tree(self):
        st = ttk.Style(self)
        try: st.theme_use("clam")
        except Exception: pass
        dark = ctk.get_appearance_mode() == "Dark"
        bg = "#2b2b2b" if dark else "#ffffff"; fg = "#e8e8e8" if dark else "#1d1d1d"; hb = "#333333" if dark else "#eef1f5"
        st.configure("S.Treeview", background=bg, fieldbackground=bg, foreground=fg, rowheight=28, borderwidth=0, font=("Segoe UI", 10))
        st.configure("S.Treeview.Heading", background=hb, foreground=fg, relief="flat", font=("Segoe UI", 10, "bold"))
        st.map("S.Treeview", background=[("selected", "#1f6aa5")], foreground=[("selected", "#ffffff")])

    def _mode_help(self):
        t = {"Automatique": "Méthode simple d'abord ; bascule seule en méthode avancée si le résultat est douteux ou si un .srt couvre plusieurs épisodes.",
             "Simple": "Décalage, vitesse et coupures, à partir de la voix. Rapide (1 à 2 min par épisode).",
             "Avancé": "Transcription + traduction pour les montages différents : scènes déplacées ou absentes, .srt sur plusieurs épisodes. Plus lent (5 à 10 min par épisode) ; télécharge ~300 Mo de modèles la première fois.",
             "Créer": "Crée des sous-titres à partir de l'audio seul (transcription puis traduction automatique). Fonctionne aussi pour les vidéos sans .srt. Traduction automatique : relisez les passages importants. Utilisez le glossaire pour les noms et termes récurrents.",
             "Améliorer": "Améliore l'image de vieilles vidéos : agrandissement par IA spécialisée vidéo réelle (LiveAction, fidèle aux textures), puis restauration douce des visages de face. Sons, sous-titres et chapitres sont conservés. Calcul sur la carte graphique : compter environ 1 h par épisode. Arrêt possible à tout moment : la reprise repart où elle s'était arrêtée.",
             "Fusion VF": "Ajoute la voix française d'une version (même de moins bonne qualité) à la vidéo HD d'une autre version, calée automatiquement (vitesse, décalage, coupures). L'image HD est copiée sans perte. Glissez les deux vidéos de chaque épisode : SubSync les associe par numéro d'épisode. 2 à 5 min par épisode."}
        self.mode_help.configure(text=t[self.mode.get()])
        m = self.mode.get()
        if hasattr(self, "fus_row"):
            if m == "Fusion VF": self.fus_row.grid(); self.lang_frame.grid_remove()
            else: self.fus_row.grid_remove(); self.lang_frame.grid()
            self.out_title.configure(text="Enregistrer les vidéos" if m in ("Fusion VF", "Améliorer") else "Enregistrer les sous-titres")
            if m == "Améliorer": self.chain.grid_remove()
            else: self.chain.grid()
            if m == "Améliorer":
                self.enh_row.grid(); self.lang_frame.grid_remove()
                self.enh_help.configure(text=("Résultat : « nom - HD.mkv ». Seuls les visages presque de face sont retouchés (les profils sont laissés tels quels pour éviter les déformations). 30-45 % conseillé."
                                              if video_ready() else "Amélioration vidéo non installée : lancez une fois « Installer l'amélioration vidéo.bat » (~700 Mo)."))
            else: self.enh_row.grid_remove()
        if hasattr(self, "tree"):
            self._set_view("merge" if m == "Fusion VF" else "sync")
        if hasattr(self, "go"):
            self.go.configure(text={"Créer": "▶  Créer les sous-titres", "Fusion VF": "▶  Créer les versions VF", "Améliorer": "▶  Améliorer les vidéos"}.get(self.mode.get(), "▶  Lancer la synchronisation"))
        if hasattr(self, "qual_row"):
            if self.mode.get() == "Créer": self.qual_row.grid()
            else: self.qual_row.grid_remove()

    # ------------------------------------------------------------------ réglages mémorisés
    SETTINGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reglages.json")

    def _load_settings(self):
        try:
            import json
            with open(self.SETTINGS, encoding="utf-8") as f: return json.load(f)
        except Exception:
            return {}

    def _save_enh(self):
        try:
            import json
            d = self._load_settings()
            self.enh_fstr_lbl.configure(text="%d %%" % round(float(self.enh_fstr.get()) * 100))
            d["enhance3"] = dict(preset=self.enh_preset.get(), res=self.enh_res.get(), faces=self.enh_faces.get(), fstr=round(float(self.enh_fstr.get()), 2), pre=self.enh_pre.get(),
                                  grain=self.enh_grain.get(), size=self.enh_size.get(), pdur=self.enh_pdur.get(),
                                  deint=bool(self.enh_deint.get()), crop=bool(self.enh_crop.get()), color=bool(self.enh_color.get()))
            with open(self.SETTINGS, "w", encoding="utf-8") as f: json.dump(d, f)
        except Exception:
            pass

    def _save_fusion_langs(self):
        try:
            import json
            d = self._load_settings(); d["fusion_langs"] = [c for c, cb in self.keep_langs.items() if cb.get()]
            with open(self.SETTINGS, "w", encoding="utf-8") as f: json.dump(d, f)
        except Exception:
            pass

    # ------------------------------------------------------------------ vues (synchro / fusion)
    def _active(self):
        return self.fus_rows if self.view == "merge" else self.sync_rows

    def _set_view(self, view, force=False):
        if view == self.view and not force: return
        if self.worker and not force:
            return
        self.view = view
        self.tree.delete(*self.tree.get_children()); self.rows = {}
        if view == "merge":
            self.tree.heading("srt", text="Vidéo VF (son français)"); self.tree.heading("vid", text="Vidéo HD (image)")
        else:
            self.tree.heading("srt", text="Sous-titres"); self.tree.heading("vid", text="Vidéo(s)")
        for row in self._active():
            iid = self.tree.insert("", "end"); self.rows[iid] = row; self._refresh(iid)
        self._show_detail()

    def _update_fusion(self, new_vids):
        busy = {v for r in self.fus_rows if len(r.videos) == 2 for v in r.videos}
        pool = [v for v in new_vids if v not in busy]
        singles = [r for r in self.fus_rows if len(r.videos) == 1]
        pool += [r.videos[0] for r in singles if r.videos[0] not in pool]
        self.fus_rows = [r for r in self.fus_rows if len(r.videos) == 2]
        if not pool: 
            if self.view == "merge": self._set_view("merge", force=True)
            return
        try:
            pairs, left = merge.pair_videos(pool)
        except Exception as e:
            self._log("Fusion : lecture des vidéos impossible (%s)" % e); pairs, left = [], pool
        for hd, vf in pairs: self.fus_rows.append(Row(None, [hd, vf], kind="fus"))
        for v in left:
            r = Row(None, [v], kind="fus"); r.state = "nopair"; self.fus_rows.append(r)
        if self.view == "merge": self._set_view("merge", force=True)
        if pairs: self._log("Fusion VF : %d couple(s) HD + VF reconnu(s) par le nom." % len(pairs))
        if len(left) >= 2: self._schedule_content_match()

    def _schedule_content_match(self):
        # on attend quelques secondes : l'utilisateur ajoute souvent les HD puis les VF en deux fois
        if getattr(self, "_cm_job", None): self.after_cancel(self._cm_job)
        self._cm_job = self.after(5000, self._run_content_match)

    def _run_content_match(self):
        self._cm_job = None
        if self.worker: self._schedule_content_match(); return
        vids = [r.videos[0] for r in self.fus_rows if len(r.videos) == 1]
        if len(vids) >= 2: self._content_match(vids)

    def _content_match(self, vids):
        """Associe par le son les vidéos dont le nom ne suffit pas (tâche de fond)."""
        if self.worker:
            self._log("Fusion VF : %d vidéo(s) non associée(s) — utilisez « Associer une vidéo… » ou ré-ajoutez-les après le traitement." % len(vids)); return
        self._log("Fusion VF : %d vidéo(s) sans partenaire d'après le nom → comparaison des bandes son pour retrouver les mêmes épisodes…" % len(vids))
        self.status.configure(text="Reconnaissance des épisodes par le son…"); self.prog.set(0)
        self.go.configure(state="disabled"); self.stop_btn.configure(state="normal"); self.mode.configure(state="disabled")
        self.stop_flag = False; self._timer_start(1)
        def run():
            try:
                pairs, left = merge.content_pairs(vids, log=lambda m: self.q.put(("log", m)),
                                                  progress=lambda f: self.q.put(("prog", f)), cancel=lambda: self.stop_flag)
                self.q.put(("fuspairs", pairs))
                self.q.put(("status", "Reconnaissance terminée : %d couple(s) trouvé(s), %d vidéo(s) sans partenaire." % (len(pairs), len(left))))
            except eng.Cancelled:
                self.q.put(("status", "Reconnaissance arrêtée."))
            except Exception:
                self.q.put(("log", traceback.format_exc()))
            self.q.put(("fusend",))
        self.worker = threading.Thread(target=run, daemon=True); self.worker.start()

    def _apply_content_pairs(self, pairs):
        for hd, vf, f in pairs:
            if any(len(r.videos) == 2 and (hd in r.videos or vf in r.videos) for r in self.fus_rows): continue
            self.fus_rows = [r for r in self.fus_rows if not (len(r.videos) == 1 and r.videos[0] in (hd, vf))]
            r = Row(None, [hd, vf], kind="fus"); r.note = "Associées par le son (%.0f %% de passages communs)" % (f * 100)
            self.fus_rows.append(r)
            self._log("  même épisode : « %s » + « %s » (%.0f %% de passages communs)" % (os.path.basename(hd), os.path.basename(vf), f * 100))
        if self.view == "merge": self._set_view("merge", force=True)

    # ------------------------------------------------------------------ fichiers
    def _on_drop(self, ev):
        paths = self.tk.splitlist(ev.data)
        files = []
        for p in paths:
            if os.path.isdir(p):
                for root, _, fs in os.walk(p):
                    files += [os.path.join(root, f) for f in fs]
            else:
                files.append(p)
        self._add(files)

    def add_files(self):
        fs = filedialog.askopenfilenames(title="Choisir des sous-titres et des vidéos",
                                         filetypes=[("Sous-titres et vidéos", "*.srt " + " ".join("*" + e for e in eng.VIDEO_EXT)),
                                                    ("Sous-titres", "*.srt"), ("Tous les fichiers", "*.*")])
        if fs: self._add(fs)

    def add_folder(self):
        d = filedialog.askdirectory(title="Choisir un dossier contenant les .srt et les vidéos")
        if d:
            files = []
            for root, _, fs in os.walk(d):
                files += [os.path.join(root, f) for f in fs]
            self._add(files)

    def _add(self, files):
        srts = [f for f in files if f.lower().endswith(".srt")]
        vids = [f for f in files if f.lower().endswith(eng.VIDEO_EXT)]
        outs_fus = {res.out_path for r in self.fus_rows for res in r.results}
        fus_new = [v for v in vids if v not in outs_fus and not v.endswith(" - VF.mkv")]
        cur_view = self.view
        if cur_view == "merge":
            # en vue fusion, on met à jour la liste de synchro sans l'afficher
            self.view = "sync"; self.tree.delete(*self.tree.get_children()); self.rows = {}
            for row in self.sync_rows:
                iid = self.tree.insert("", "end"); self.rows[iid] = row
        # ne pas reprendre nos propres sorties ni les doublons
        known = {r.srt for r in self.rows.values() if r.srt} | {v for r in self.rows.values() for v in r.videos}
        outs = {res.out_path for r in self.rows.values() for res in r.results}
        srts = [s for s in srts if s not in known and s not in outs]
        vids = [v for v in vids if v not in known]
        # vidéos orphelines déjà présentes : on les recycle pour l'appariement
        orphan_ids = [i for i, r in self.rows.items() if r.srt is None]
        vids += [self.rows[i].videos[0] for i in orphan_ids]
        for i in orphan_ids:
            if self.rows[i] in self.sync_rows: self.sync_rows.remove(self.rows[i])
            self.tree.delete(i); del self.rows[i]
        # srt sans vidéo déjà présents
        lonely = [i for i, r in self.rows.items() if r.srt and not r.videos and r.state in ("wait", "nopair")]
        srts += [self.rows[i].srt for i in lonely]
        for i in lonely:
            if self.rows[i] in self.sync_rows: self.sync_rows.remove(self.rows[i])
            self.tree.delete(i); del self.rows[i]
        pairs, left = eng.pair_files(srts, vids)
        for s, vs in pairs:
            self._insert(Row(s, vs))
        for v in left:
            self._insert(Row(None, [v]))
        n = len(pairs)
        if (n or left) and cur_view != "merge":
            self._log("%d fichier(s) de sous-titres ajouté(s), %d vidéo(s) sans sous-titres." % (n, len(left)))
        self._update_fusion(fus_new)
        if cur_view == "merge":
            self.view = "sync"; self._set_view("merge", force=True)

    def _insert(self, row):
        iid = self.tree.insert("", "end"); self.rows[iid] = row
        if row not in self._active(): self._active().append(row)
        if row.srt is None: row.state = "video"
        elif not row.videos: row.state = "nopair"
        self._refresh(iid)

    def _refresh(self, iid):
        r = self.rows[iid]
        if r.kind == "fus":
            c1 = os.path.basename(r.videos[1]) if len(r.videos) > 1 else "— cliquez « Associer une vidéo… » —"
            c2 = os.path.basename(r.videos[0])
            res = " | ".join(x.summary for x in r.results) if r.results else (r.error.splitlines()[0][:120] if r.error else "")
            tag = r.state if r.state in COLORS else ("grey" if r.state in ("video", "nopair") else "")
            st = "VF manquante" if r.state == "nopair" else STATE_TXT.get(r.state, r.state)
            self.tree.item(iid, values=(c1, c2, st, res), tags=(tag,)); return
        vids = " + ".join(os.path.basename(v) for v in r.videos) if r.videos else "— cliquez « Associer une vidéo… » —"
        res = ""
        if r.results:
            res = " | ".join(x.summary for x in r.results)
        elif r.error:
            res = r.error.splitlines()[0][:120]
        tag = r.state if r.state in COLORS else ("grey" if r.state in ("video", "nopair") else "")
        self.tree.item(iid, values=(os.path.basename(r.srt) if r.srt else "—", vids, STATE_TXT.get(r.state, r.state), res), tags=(tag,))

    def assign_video(self):
        if self.view == "merge":
            sel = list(self.tree.selection())
            if len(sel) != 1:
                messagebox.showinfo("SubSync", "Sélectionnez une ligne."); return
            r = self.rows[sel[0]]
            f = filedialog.askopenfilename(title="Choisir l'autre version de « %s »" % os.path.basename(r.videos[0]),
                                           filetypes=[("Vidéos", " ".join("*" + e for e in eng.VIDEO_EXT)), ("Tous les fichiers", "*.*")])
            if not f: return
            try:
                hd, vf = merge.classify_pair(r.videos[0], f)
            except Exception:
                hd, vf = r.videos[0], f
            r.videos = [hd, vf]; r.state = "wait"; r.results = []
            for other in [x for x in self.fus_rows if x is not r and len(x.videos) == 1 and x.videos[0] == f]:
                self.fus_rows.remove(other)
            self._set_view("merge", force=True); return
        sel = [i for i in self.tree.selection() if self.rows[i].srt]
        if len(sel) != 1:
            messagebox.showinfo("SubSync", "Sélectionnez une ligne de sous-titres."); return
        fs = filedialog.askopenfilenames(title="Choisir la ou les vidéos (plusieurs si le .srt couvre plusieurs épisodes)",
                                         filetypes=[("Vidéos / audio", " ".join("*" + e for e in eng.VIDEO_EXT)), ("Tous les fichiers", "*.*")])
        if fs:
            r = self.rows[sel[0]]; r.videos = sorted(fs, key=lambda v: (eng.episode_keys(v) or (0, [0]))[1][0]); r.state = "wait"
            for i in [i for i, x in self.rows.items() if x.srt is None and x.videos[0] in fs]:
                if self.rows[i] in self.sync_rows: self.sync_rows.remove(self.rows[i])
                self.tree.delete(i); del self.rows[i]
            self._refresh(sel[0])

    def remove_sel(self):
        for i in self.tree.selection():
            if self.rows[i].state != "run":
                row = self.rows[i]
                if row in self._active(): self._active().remove(row)
                self.tree.delete(i); del self.rows[i]

    def clear(self):
        if self.worker: return
        self.tree.delete(*self.tree.get_children()); self.rows.clear(); self.sync_rows.clear(); self.fus_rows.clear(); self._show_detail()

    def choose_out(self):
        d = filedialog.askdirectory(title="Dossier où enregistrer les sous-titres synchronisés")
        if d:
            self.out_dir = d; self.out_lbl.configure(text=d)
        else:
            self.out_dir = None; self.out_lbl.configure(text="À côté de chaque vidéo")

    def open_glossary(self):
        p = eng.GLOSSARY_FILE
        if not os.path.exists(p):
            open(p, "w", encoding="utf-8-sig").write("# Glossaire SubSync\n")
        if os.name == "nt":
            os.startfile(p)
        else:
            subprocess.Popen(["xdg-open", p])

    # ------------------------------------------------------------------ détail / réglage fin
    def _sel_row(self):
        s = self.tree.selection()
        return (s[0], self.rows[s[0]]) if s else (None, None)

    def _show_detail(self):
        iid, r = self._sel_row()
        if not r:
            self.det.configure(text="Sélectionnez une ligne."); self.conf.set(0); return
        lines = []
        if r.srt: lines.append("Sous-titres : " + os.path.basename(r.srt))
        if r.kind == "fus":
            if getattr(r, "note", ""): lines.append(r.note)
            lines.append("Image HD : " + os.path.basename(r.videos[0]))
            if len(r.videos) > 1: lines.append("Son VF : " + os.path.basename(r.videos[1]))
        else:
            for v in r.videos: lines.append("Vidéo : " + os.path.basename(v))
        lines.append("État : " + STATE_TXT.get(r.state, r.state))
        if getattr(r, "elapsed", None): lines.append("Durée du traitement : " + self._fmt_dur(r.elapsed))
        for x in r.results:
            lines.append("")
            lines.append("→ %s" % os.path.basename(x.out_path))
            lines.append("Méthode %s — %s" % (x.method, x.summary))
            if x.method == "fusion": lines.append("Contrôles de calage OK : %.0f %%" % x.confidence)
            elif x.method not in ("création", "amélioration"): lines.append("Confiance : %.0f / 100" % x.confidence)
        if r.error: lines.append("Erreur : " + r.error[:300])
        self.det.configure(text="\n".join(lines))
        c = min([x.confidence for x in r.results] or [0]) / 100.0
        self.conf.set(c)
        lvl = r.state if r.state in COLORS else None
        self.conf.configure(progress_color=COLORS.get(lvl, "#1f6aa5"))
        self.shift_lbl.configure(text=("Ajustement manuel : %+.1f s" % r.shift).replace(".", ","))

    def nudge(self, d):
        iid, r = self._sel_row()
        if not r or not r.results or r.kind == "fus":
            messagebox.showinfo("SubSync", "Sélectionnez une ligne de sous-titres déjà synchronisée."); return
        for x in r.results:
            eng.shift_file(x.out_path, d)
        r.shift += d
        self._log("Réglage fin %+.1f s appliqué à %s (total %+.1f s)." % (d, ", ".join(os.path.basename(x.out_path) for x in r.results), r.shift))
        self._show_detail()

    def open_folder(self):
        iid, r = self._sel_row()
        if not r: return
        p = r.results[0].out_path if r.results else (r.videos[0] if r.videos else r.srt)
        if not p: return
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(p)])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(p)])

    # ------------------------------------------------------------------ traitement
    def start(self, resume=None):
        if self.worker: return
        if resume:
            todo, opts = resume
            self._queue_save(todo, opts)
            for i in todo:
                r = self.rows[i]; r.state = "wait"; r.results = []; r.error = ""; self._refresh(i)
            self.stop_flag = False
            self.go.configure(state="disabled"); self.stop_btn.configure(state="normal"); self.mode.configure(state="disabled")
            self.prog.set(0); self._timer_start(len(todo))
            self.worker = threading.Thread(target=self._work, args=(todo, opts), daemon=True); self.worker.start()
            return
        mode = MODES[self.mode.get()]; creating = mode == "create"
        if mode == "merge":
            todo = [i for i, r in self.rows.items() if r.kind == "fus" and len(r.videos) == 2 and r.state != "run"]
        elif creating or mode == "enhance":
            todo = [i for i, r in self.rows.items() if r.videos and r.state != "run"]
            if mode == "enhance" and not video_ready():
                messagebox.showinfo("SubSync", "L'amélioration vidéo n'est pas installée.\n\nFermez SubSync, double-cliquez une fois sur « Installer l'amélioration vidéo.bat », puis relancez SubSync."); return
        else:
            todo = [i for i, r in self.rows.items() if r.srt and r.videos and r.state not in ("run",)]
        if self.only_sel.get():
            sel = set(self.tree.selection()); todo = [i for i in todo if i in sel]
            if not todo:
                messagebox.showinfo("SubSync", "Aucune ligne sélectionnée à traiter (décochez « seulement les lignes sélectionnées » pour tout traiter)."); return
        if not todo:
            messagebox.showinfo("SubSync", {"merge": "Ajoutez les deux versions (HD + VF) de chaque épisode.", "create": "Ajoutez des vidéos."}.get(mode, "Rien à synchroniser : ajoutez des .srt avec leur vidéo.")); return
        for i in todo:
            r = self.rows[i]; r.state = "wait"; r.results = []; r.error = ""; r.shift = 0.0; self._refresh(i)
        self.stop_flag = False
        opts = dict(mode=MODES[self.mode.get()], sub_lang=self.sub_lang.get().strip() or "fr",
                    audio_lang=self.aud_lang.get().strip() or "en", out_dir=self.out_dir, quality=self.quality.get(),
                    translator="ia" if self.translator.get().startswith("IA") else "standard", keep_langs=[c for c, cb in self.keep_langs.items() if cb.get()],
                    enh=dict(preset=ENH_PRESETS.get(self.enh_preset.get(), "fidele"), target_h=1080 if self.enh_res.get() == "1080p" else 720,
                             faces=ENH_FACES.get(self.enh_faces.get(), "gfpgan"), face_strength=round(float(self.enh_fstr.get()), 2),
                             preclean=ENH_PRECLEAN.get(self.enh_pre.get(), ""), degrain=ENH_GRAIN.get(self.enh_grain.get(), "reduire"),
                             size=ENH_SIZE.get(self.enh_size.get(), "compact"), auto_deint=bool(self.enh_deint.get()),
                             auto_crop=bool(self.enh_crop.get()), color="doux" if self.enh_color.get() else ""), chain=bool(self.chain.get()))
        if opts["chain"] and mode != "enhance" and not video_ready():
            messagebox.showinfo("SubSync", "« Tout-en-un » demande l'amélioration vidéo : lancez une fois « Installer l'amélioration vidéo.bat »."); return
        self._queue_save(todo, opts)
        self.go.configure(state="disabled"); self.stop_btn.configure(state="normal"); self.mode.configure(state="disabled")
        self.prog.set(0); self._timer_start(len(todo))
        self.worker = threading.Thread(target=self._work, args=(todo, opts), daemon=True); self.worker.start()

    # ------------------------------------------------------------------ file d'attente persistante
    def _queue_save(self, todo, opts):
        import json
        self._queue = dict(opts=opts, ui_mode=self.mode.get(), items=[dict(iid=i, srt=self.rows[i].srt, videos=self.rows[i].videos,
                                                                          kind=self.rows[i].kind, done=False) for i in todo])
        self._queue_write()

    def _queue_write(self):
        import json
        q = getattr(self, "_queue", None)
        try:
            if q and any(not it["done"] for it in q["items"]):
                with open(QUEUE_FILE, "w", encoding="utf-8") as f:
                    json.dump(dict(q, items=[{k: v for k, v in it.items() if k != "iid"} for it in q["items"]]), f, ensure_ascii=False)
            elif os.path.exists(QUEUE_FILE):
                os.remove(QUEUE_FILE)
        except Exception:
            pass

    def _queue_mark(self, iid):
        q = getattr(self, "_queue", None)
        if not q: return
        for it in q["items"]:
            if it.get("iid") == iid: it["done"] = True
        self._queue_write()

    def _offer_resume(self):
        import json
        try:
            with open(QUEUE_FILE, encoding="utf-8") as f: q = json.load(f)
        except Exception:
            return
        left = [it for it in q.get("items", []) if not it.get("done") and all(os.path.exists(v) for v in it.get("videos", []))]
        if not left:
            try: os.remove(QUEUE_FILE)
            except OSError: pass
            return
        names = "\n".join("• " + os.path.basename(it["videos"][0]) for it in left[:8]) + ("\n…" if len(left) > 8 else "")
        if not messagebox.askyesno("SubSync", "Un traitement « %s » a été interrompu (%d élément(s) restant(s)) :\n\n%s\n\nLe reprendre maintenant ?\n"
                                   "(Les améliorations vidéo repartent du dernier tronçon terminé.)" % (q.get("ui_mode", ""), len(left), names)):
            if messagebox.askyesno("SubSync", "Oublier définitivement cette file d'attente ?"):
                try: os.remove(QUEUE_FILE)
                except OSError: pass
            return
        mode = q.get("ui_mode", "Automatique")
        self.mode.set(mode); self._mode_help()
        todo = []
        for it in left:
            row = Row(it.get("srt"), it["videos"], kind=it.get("kind", "sync"))
            if row.kind == "fus":
                self.fus_rows.append(row)
            else:
                self.sync_rows.append(row)
        self._set_view("merge" if mode == "Fusion VF" else "sync", force=True)
        wanted = {(it.get("srt"), tuple(it["videos"])) for it in left}
        todo = [i for i, r in self.rows.items() if (r.srt, tuple(r.videos)) in wanted]
        self._log("Reprise de la file d'attente interrompue : %d élément(s)." % len(todo))
        self.start(resume=(todo, q["opts"]))

    # ------------------------------------------------------------------ fin de traitement : action et notification
    def _finish_actions(self, stopped):
        rs = [r for r in self.rows.values() if r.results or r.error]
        ok = sum(1 for r in rs if r.state in ("ok", "created")); bad = len(rs) - ok
        msg = ("SubSync : traitement arrêté." if stopped else
               "SubSync : traitement terminé — %d réussi(s)%s." % (ok, (", %d à vérifier ou en erreur" % bad) if bad else ""))
        try: self.bell(); self.focus_force()
        except Exception: pass
        self._notify(msg)
        act = END_ACTIONS.get(self.end_action.get(), "")
        if act and not stopped: self._countdown(act)

    def _notify(self, msg):
        st = upd.settings(); topic = (st.get("ntfy_topic") or "").strip()
        if not st.get("ntfy_on") or not topic: return
        def run():
            try:
                import urllib.request
                urllib.request.urlopen(urllib.request.Request("https://ntfy.sh/" + topic, data=msg.encode("utf-8"),
                                       headers={"Title": "SubSync", "Tags": "movie_camera"}), timeout=20).read()
            except Exception as e:
                self.q.put(("log", "Notification impossible : %s" % e))
        threading.Thread(target=run, daemon=True).start()

    def _countdown(self, act):
        w = ctk.CTkToplevel(self); w.title("SubSync"); w.geometry("420x170"); w.transient(self); w.attributes("-topmost", True)
        what = "mise en veille" if act == "sleep" else "arrêt du PC"
        lab = ctk.CTkLabel(w, text="", font=ctk.CTkFont(size=15)); lab.pack(pady=(28, 12))
        st = {"n": 60, "cancel": False}
        ctk.CTkButton(w, text="Annuler", width=140, command=lambda: (st.update(cancel=True), w.destroy())).pack()
        def tick():
            if st["cancel"]: return
            if st["n"] <= 0:
                w.destroy(); self._do_power(act); return
            lab.configure(text="Traitement terminé : %s dans %d s…" % (what, st["n"])); st["n"] -= 1; w.after(1000, tick)
        tick()

    def _do_power(self, act):
        if os.name != "nt":
            self._log("(action de fin « %s » ignorée hors Windows)" % act); return
        try:
            if act == "shutdown": subprocess.Popen(["shutdown", "/s", "/t", "0"], creationflags=0x08000000)
            else: subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], creationflags=0x08000000)
        except Exception as e:
            self._log("Action de fin impossible : %s" % e)

    def open_notifications(self):
        import secrets
        st = upd.settings()
        w = ctk.CTkToplevel(self); w.title("Notifications sur téléphone"); w.geometry("560x380"); w.transient(self); w.grab_set()
        on = ctk.CTkCheckBox(w, text="M'envoyer une notification sur mon téléphone à la fin des traitements")
        on.pack(anchor="w", padx=16, pady=(16, 8))
        if st.get("ntfy_on"): on.select()
        topic = st.get("ntfy_topic") or "subsync-" + secrets.token_hex(5)
        ctk.CTkLabel(w, justify="left", wraplength=520, text=(
            "1. Installez l'application gratuite « ntfy » sur votre téléphone (Play Store / App Store).\n"
            "2. Dans ntfy, touchez « + » (s'abonner) et tapez exactement ce nom de sujet :")).pack(anchor="w", padx=16)
        e = ctk.CTkEntry(w, width=300); e.insert(0, topic); e.pack(anchor="w", padx=16, pady=6)
        ctk.CTkLabel(w, justify="left", wraplength=520, text_color=("gray35", "gray65"), text=(
            "Ce nom est votre « adresse » : gardez-le pour vous (toute personne qui le connaît peut lire ces notifications)."
            " Les messages indiquent seulement la fin du traitement et le nombre d'épisodes réussis.")).pack(anchor="w", padx=16)
        def save():
            upd.save_settings(ntfy_on=bool(on.get()), ntfy_topic=e.get().strip())
        def test():
            save(); was = upd.settings().get("ntfy_on"); upd.save_settings(ntfy_on=True)
            self._notify("SubSync : ceci est une notification de test."); upd.save_settings(ntfy_on=was)
            messagebox.showinfo("SubSync", "Notification de test envoyée : elle doit arriver sur votre téléphone d'ici quelques secondes.", parent=w)
        b = ctk.CTkFrame(w, fg_color="transparent"); b.pack(anchor="w", padx=16, pady=14)
        ctk.CTkButton(b, text="Envoyer un test", command=test).pack(side="left")
        ctk.CTkButton(b, text="Enregistrer", command=lambda: (save(), w.destroy())).pack(side="left", padx=8)
        w.protocol("WM_DELETE_WINDOW", lambda: (save(), w.destroy()))

    # ------------------------------------------------------------------ profils de réglages
    def _profile_names(self):
        return sorted(upd.settings().get("profiles", {})) or ["—"]

    def _collect(self):
        return dict(mode=self.mode.get(), sub_lang=self.sub_lang.get(), aud_lang=self.aud_lang.get(), quality=self.quality.get(),
                    translator=self.translator.get(), keep_langs=[c for c, cb in self.keep_langs.items() if cb.get()],
                    enh_preset=self.enh_preset.get(), enh_res=self.enh_res.get(), enh_faces=self.enh_faces.get(),
                    enh_fstr=float(self.enh_fstr.get()), enh_pre=self.enh_pre.get(), enh_grain=self.enh_grain.get(),
                    enh_size=self.enh_size.get(), enh_pdur=self.enh_pdur.get(), out_dir=self.out_dir,
                    enh_deint=bool(self.enh_deint.get()), enh_crop=bool(self.enh_crop.get()), enh_color=bool(self.enh_color.get()),
                    chain=bool(self.chain.get()))

    def _apply(self, d):
        if d.get("mode") in MODES: self.mode.set(d["mode"])
        for k, wdg in (("sub_lang", self.sub_lang), ("aud_lang", self.aud_lang), ("quality", self.quality), ("translator", self.translator),
                       ("enh_preset", self.enh_preset), ("enh_res", self.enh_res), ("enh_faces", self.enh_faces), ("enh_pre", self.enh_pre),
                       ("enh_grain", self.enh_grain), ("enh_size", self.enh_size), ("enh_pdur", self.enh_pdur)):
            if d.get(k): wdg.set(d[k])
        if "enh_fstr" in d: self.enh_fstr.set(d["enh_fstr"])
        for k, wdg in (("enh_deint", self.enh_deint), ("enh_crop", self.enh_crop), ("enh_color", self.enh_color), ("chain", self.chain)):
            if k in d: (wdg.select() if d[k] else wdg.deselect())
        if "keep_langs" in d:
            for c, cb in self.keep_langs.items(): (cb.select() if c in d["keep_langs"] else cb.deselect())
        if "out_dir" in d:
            self.out_dir = d["out_dir"] if d["out_dir"] and os.path.isdir(d["out_dir"]) else None
            self.out_lbl.configure(text=self.out_dir or "À côté de chaque vidéo")
        self._mode_help(); self._save_enh()

    def _save_profile(self):
        name = ctk.CTkInputDialog(title="Profil", text="Nom du profil (ex. « Au-delà du réel ») :").get_input()
        if not name or not name.strip(): return
        st = upd.settings(); pr = st.get("profiles", {}); pr[name.strip()] = self._collect(); upd.save_settings(profiles=pr)
        self.profile.configure(values=self._profile_names()); self.profile.set(name.strip())
        self._log("Profil « %s » enregistré." % name.strip())

    def _load_profile(self, name):
        d = upd.settings().get("profiles", {}).get(name)
        if d: self._apply(d); self._log("Profil « %s » appliqué." % name)

    def _del_profile(self):
        name = self.profile.get(); pr = upd.settings().get("profiles", {})
        if name not in pr: return
        if not messagebox.askyesno("SubSync", "Supprimer le profil « %s » ?" % name): return
        pr.pop(name); upd.save_settings(profiles=pr)
        self.profile.configure(values=self._profile_names()); self.profile.set("—")

    @staticmethod
    def _fmt_dur(sec):
        sec = int(sec)
        h, m, s_ = sec // 3600, sec % 3600 // 60, sec % 60
        return "%d h %02d min" % (h, m) if h else ("%d min %02d s" % (m, s_) if m else "%d s" % s_)

    def _tick(self):
        if self.t_start is None: return
        now = time.time(); txt = "Temps écoulé : " + self._fmt_dur(now - self.t_start)
        if self.t_item and self.n_items > 1: txt = "Épisode : %s · total : %s" % (self._fmt_dur(now - self.t_item), self._fmt_dur(now - self.t_start))
        p = self.prog.get()
        if 0.03 < p < 0.995 and now - self.t_start > 20:
            txt += " · reste ≈ %s" % self._fmt_dur((now - self.t_start) * (1 - p) / p)
        self.timer.configure(text=txt)
        self.after(1000, self._tick)

    def _timer_start(self, n=1):
        self.t_start = time.time(); self.t_item = None; self.n_items = n; self._tick()

    def _timer_stop(self):
        if self.t_start is not None:
            self.timer.configure(text="Durée totale : " + self._fmt_dur(time.time() - self.t_start))
        self.t_start = self.t_item = None

    def stop(self):
        self.stop_flag = True; self.status.configure(text="Arrêt demandé… (fin de l'étape en cours)")

    def _work(self, todo, opts):
        n = len(todo)
        for k, iid in enumerate(todo):
            if self.stop_flag:
                self.q.put(("state", iid, "cancel", None)); continue
            r = self.rows.get(iid)
            if not r: continue
            self.q.put(("state", iid, "run", None))
            self.q.put(("status", "Épisode %d/%d : %s" % (k + 1, n, os.path.basename(r.srt or r.videos[0]))))
            def prog(f, k=k): self.q.put(("prog", (k + max(0, min(1, f))) / n))
            try:
                t0 = time.time()
                lg = lambda m: self.q.put(("log", m)); cc = lambda: self.stop_flag
                if opts["mode"] == "merge":
                    res = merge.merge_dub(r.videos[0], r.videos[1], opts["out_dir"], opts["keep_langs"], log=lg, progress=prog, cancel=cc)
                elif opts["mode"] == "enhance":
                    res = []
                    for j, v in enumerate(r.videos):
                        res.append(self._enhance(v, opts, lg, lambda f, j=j: prog((j + f) / len(r.videos)), cc))
                elif opts["mode"] == "create":
                    res = []
                    for j, v in enumerate(r.videos):
                        res += eng.create_subs(v, opts["out_dir"], opts["audio_lang"], opts["sub_lang"], opts["quality"],
                                               log=lg, progress=lambda f, j=j: prog((j + f) / len(r.videos)), cancel=cc,
                                               translator=opts["translator"])
                else:
                    o = {k_: v_ for k_, v_ in opts.items() if k_ not in ("quality", "translator", "keep_langs", "enh", "chain")}
                    res = eng.run_job(r.srt, r.videos, log=lg, progress=prog, cancel=cc, **o)
                if opts.get("chain") and opts["mode"] != "enhance" and not self.stop_flag:
                    res = self._chain_enhance(opts, res, lg, cc)
                lvl = "created" if opts["mode"] == "create" else "ok"
                for x in ([] if opts["mode"] == "create" else res):
                    if x.level == "bad": lvl = "bad"
                    elif x.level == "warn" and lvl == "ok": lvl = "warn"
                r.elapsed = time.time() - t0
                self.q.put(("done", iid, lvl, res))
                self.q.put(("log", "  terminé en %s." % self._fmt_dur(r.elapsed)))
            except eng.Cancelled:
                self.q.put(("state", iid, "cancel", None))
            except Exception as e:
                self.q.put(("log", traceback.format_exc()))
                self.q.put(("err", iid, str(e), None))
        self.q.put(("end",))

    def _enh_tag(self, e):
        return "v3_%s_%d_%s_%d%s" % (e["preset"], e["target_h"], e["faces"] or "sans", int(e["face_strength"] * 100),
                                     ("_" + e["preclean"] if e.get("preclean") else "") + ("_g" + e["degrain"] if e.get("degrain") else "") + "_" + e.get("size", "compact")
                                     + ("_c" if e.get("color") else "") + ("" if e.get("auto_crop", True) else "_nocrop") + ("" if e.get("auto_deint", True) else "_nodeint"))

    def _run_video_job(self, job, log, progress, cancel):
        """Lance subsync_enhance.py dans l'environnement vidéo ; renvoie les messages « done » et « preview »."""
        import json, subprocess
        os.makedirs(job["work"], exist_ok=True)
        jp = os.path.join(job["work"], "tache_gui.json")
        with open(jp, "w", encoding="utf-8") as f: json.dump(job, f)
        p = subprocess.Popen([VIDEO_PY, "-u", os.path.join(APP_DIR, "subsync_enhance.py"), jp], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             cwd=APP_DIR, env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"),
                             creationflags=0x08000000 if os.name == "nt" else 0)
        res = {}; err = None
        def watch():
            while p.poll() is None:
                if cancel():
                    p.terminate(); return
                time.sleep(0.5)
        threading.Thread(target=watch, daemon=True).start()
        for line in iter(p.stdout.readline, b""):
            try: m = json.loads(line.decode("utf-8", errors="replace"))
            except Exception:
                t = line.decode("utf-8", errors="replace").strip()
                if t: log("  " + t)
                continue
            if m.get("t") == "log": log(m["m"])
            elif m.get("t") == "prog" and m.get("total"): progress(m["done"] / m["total"])
            elif m.get("t") in ("done", "preview"): res[m["t"]] = m
            elif m.get("t") == "err": err = m["m"]
        p.wait()
        if cancel(): raise eng.Cancelled()
        if err or "done" not in res: raise RuntimeError(err or "le traitement s'est arrêté sans résultat")
        return res

    def _enhance(self, video, opts, log, progress, cancel):
        info = merge.video_info(video)
        e = opts["enh"]
        d = opts["out_dir"] or os.path.dirname(video); os.makedirs(d, exist_ok=True)
        stem = os.path.splitext(os.path.basename(video))[0]
        out = os.path.join(d, stem + " - HD.mkv")
        if os.path.abspath(out) == os.path.abspath(video): out = os.path.join(d, stem + " - HD (2).mkv")
        work = os.path.join(d, ".subsync_hd_" + stem + "_" + self._enh_tag(e))
        job = dict(info, ffmpeg=eng.ffmpeg_exe(), src=video, out=out, work=work, sharpen=0.35, extra_subs=opts.get("extra_subs", []), **e)
        log("Amélioration de « %s » : %d×%d, %.3f i/s, ~%d images" % (os.path.basename(video), info["width"], info["height"],
                                                                    info["fps_num"] / info["fps_den"], info["frames"]))
        try:
            result = self._run_video_job(job, log, progress, cancel)["done"]
        except eng.Cancelled:
            log("  arrêté : relancez plus tard, le travail reprendra où il s'est arrêté."); raise
        fps = info["frames"] / max(1.0, result.get("seconds", 1))
        summ = "%s → %dp%s · %.1f images/s" % (os.path.basename(out), e["target_h"],
                                               (", %d visages restaurés" % result.get("faces", 0)) if e["faces"] else "", fps)
        return eng.Result(video, out, "amélioration", 100.0, "ok", summ, {}, [])

    LANG3 = {"fr": "fre", "en": "eng", "de": "ger", "es": "spa", "it": "ita", "pt": "por", "nl": "dut", "ja": "jpn", "pl": "pol", "ru": "rus"}

    def _chain_enhance(self, opts, res, log, cancel):
        """Tout-en-un : après la fusion VF ou les sous-titres, améliore l'image et y intègre les sous-titres."""
        out = list(res)
        jobs = []
        if opts["mode"] == "merge":
            for x in res:
                if x.out_path and os.path.exists(x.out_path): jobs.append((x.out_path, [], True))
        else:
            by_video = {}
            for x in res:
                if getattr(x, "out_path", None) and x.out_path.lower().endswith(".srt") and x.level != "bad":
                    by_video.setdefault(x.video, []).append(x.out_path)
            for v, srts in by_video.items(): jobs.append((v, srts, False))
        for v, srts, temp in jobs:
            if cancel(): raise eng.Cancelled()
            try: h = merge.video_info(v)["height"]
            except Exception: h = 0
            if h >= 1000:
                log("Tout-en-un : « %s » est déjà en HD (%dp), amélioration de l'image inutile." % (os.path.basename(v), h)); continue
            lang = self.LANG3.get(opts.get("sub_lang", "fr"), opts.get("sub_lang", "fre"))
            extra = [dict(path=s_, lang=lang, title="%s (SubSync)" % lang_title(lang)) for s_ in srts]
            log("Tout-en-un : amélioration de l'image de « %s »%s…" % (os.path.basename(v), (" + %d fichier(s) de sous-titres intégré(s)" % len(srts)) if srts else ""))
            o2 = dict(opts, extra_subs=extra)
            r2 = self._enhance(v, o2, log, lambda f: None, cancel)
            if temp and os.path.exists(r2.out_path):
                # fichier intermédiaire de la fusion : remplacé par la version améliorée
                final = v[:-4] + " HD.mkv" if v.lower().endswith(".mkv") else r2.out_path
                try:
                    os.replace(r2.out_path, final); os.remove(v); r2.out_path = final
                    r2.summary = os.path.basename(final) + " · " + r2.summary.split(" · ", 1)[-1]
                except OSError: pass
            out.append(r2)
        return out

    # ------------------------------------------------------------------ aperçu
    def _enh_opts(self):
        return dict(preset=ENH_PRESETS.get(self.enh_preset.get(), "fidele"), target_h=1080 if self.enh_res.get() == "1080p" else 720,
                    faces=ENH_FACES.get(self.enh_faces.get(), "gfpgan"), face_strength=round(float(self.enh_fstr.get()), 2),
                    preclean=ENH_PRECLEAN.get(self.enh_pre.get(), ""), degrain=ENH_GRAIN.get(self.enh_grain.get(), "reduire"),
                    size=ENH_SIZE.get(self.enh_size.get(), "compact"), auto_deint=bool(self.enh_deint.get()),
                    auto_crop=bool(self.enh_crop.get()), color="doux" if self.enh_color.get() else "")

    def _enh_label(self):
        g = self.enh_grain.get().split(" (")[0]; pre = self.enh_pre.get().split(" (")[0]
        fc = ENH_FACES.get(self.enh_faces.get(), "gfpgan")
        vis = ("visages %s %d %%" % (fc.upper(), round(float(self.enh_fstr.get()) * 100))) if fc else "visages non retouchés"
        return "%s · %s · %s · nettoyage %s · grain %s%s · fichier %s" % (
            self.enh_preset.get().split(" (")[0], self.enh_res.get(), vis, pre.lower(), g.lower(),
            " · couleurs ravivées" if self.enh_color.get() else "", self.enh_size.get().split(" ≈")[0].lower())

    @staticmethod
    def _parse_time(txt):
        txt = (txt or "").strip().replace("h", ":").replace("m", ":").replace("s", "")
        if not txt: return None
        parts = [float(x) for x in txt.split(":") if x.strip() != ""]
        t = 0.0
        for x in parts: t = t * 60 + x
        return t

    def preview(self):
        if self.worker:
            messagebox.showinfo("SubSync", "Attendez la fin du traitement en cours."); return
        if not video_ready():
            messagebox.showinfo("SubSync", "L'amélioration vidéo n'est pas installée (« Installer l'amélioration vidéo.bat »)."); return
        sel = [self.rows[i] for i in self.tree.selection() if self.rows[i].videos] or [r for r in self.rows.values() if r.videos]
        if not sel:
            messagebox.showinfo("SubSync", "Ajoutez une vidéo (et sélectionnez-la) avant de demander un aperçu."); return
        video = sel[0].videos[0]
        try: t_user = self._parse_time(self.enh_time.get())
        except ValueError:
            messagebox.showinfo("SubSync", "Moment non compris : écrivez par exemple 10:05 ou 1:02:30."); return
        e = self._enh_opts(); label = self._enh_label()
        try: pdur = max(1.0, min(120.0, float(self.enh_pdur.get().replace("s", "").replace(",", ".").strip())))
        except ValueError: pdur = 5.0
        self.stop_flag = False
        self.go.configure(state="disabled"); self.stop_btn.configure(state="normal"); self.mode.configure(state="disabled")
        self.prev_btn.configure(state="disabled"); self.prog.set(0); self._timer_start(1)
        self.status.configure(text="Aperçu de « %s »…" % os.path.basename(video))
        def run():
            try:
                info = merge.video_info(video)
                dur = info["duration"] or info["frames"] * info["fps_den"] / info["fps_num"]
                last = getattr(self, "_last_preview", None)
                if t_user is not None: times = [min(max(0, t_user), max(0, dur - pdur - 1))]
                elif last and last["video"] == video and last.get("pdur") == pdur: times = last["times"]
                else: times = [round(dur * f) for f in (0.2, 0.5, 0.8)]
                stem = os.path.splitext(os.path.basename(video))[0]
                work = os.path.join(APP_DIR, "cache", "apercu", "%s_%s_%s_%gs" % (stem[:40], self._enh_tag(e), "-".join(str(int(t)) for t in times), pdur))
                if os.path.isdir(work): shutil.rmtree(work, ignore_errors=True)
                audio_mb = sum(0.224 for _ in range(2)) * dur / 8   # estimation grossière des pistes audio (2 x 224 kb/s)
                job = dict(info, ffmpeg=eng.ffmpeg_exe(), src=video, out="", work=work, sharpen=0.35,
                           preview=dict(times=times, dur=pdur), audio_mb=audio_mb, **e)
                self.q.put(("log", "Aperçu (%s) : %d extrait(s) de %g s…" % (label, len(times), pdur)))
                res = self._run_video_job(job, lambda m: self.q.put(("log", m)), lambda f: self.q.put(("prog", f)), lambda: self.stop_flag)
                self.q.put(("preview_ready", dict(video=video, times=times, pdur=pdur, label=label, data=res.get("preview", {}))))
            except eng.Cancelled:
                self.q.put(("log", "Aperçu arrêté."))
            except Exception:
                self.q.put(("log", traceback.format_exc()))
            self.q.put(("preview_end",))
        self.worker = threading.Thread(target=run, daemon=True); self.worker.start()

    def _show_preview(self, pv):
        prev = getattr(self, "_last_preview", None)
        other = prev if (prev and prev["video"] == pv["video"] and prev["times"] == pv["times"] and prev.get("pdur") == pv.get("pdur") and prev["label"] != pv["label"]) else None
        PreviewWindow(self, pv, other)
        self._last_preview = pv

    # ------------------------------------------------------------------ mises à jour
    def _auto_update_check(self):
        mode = upd.settings().get("update_mode", "ask")
        if mode == "off" or not upd.due(1): return
        def run():
            try:
                v, notes = upd.check()
                if upd.is_newer(v): self.q.put(("upd_found", v, notes, mode))
            except Exception as e:
                self.q.put(("log", "Vérification des mises à jour impossible : %s" % e))
        threading.Thread(target=run, daemon=True).start()

    def _on_update_found(self, v, notes, mode):
        self.upd_btn.configure(text="⬆ Mise à jour %s" % v, fg_color=("#2f9e44", "#2f9e44"), text_color="white")
        self._log("Nouvelle version de SubSync disponible : %s (vous avez la %s)." % (v, VERSION))
        if mode == "auto":
            if self.worker: self._log("  elle sera installée à la fin du traitement en cours."); self._pending_update = True; return
            self._install_update()
        else:
            self.open_updates(found=(v, notes))

    def _install_update(self, win=None):
        if self.worker:
            messagebox.showinfo("SubSync", "Attendez la fin du traitement en cours (ou arrêtez-le) avant de mettre à jour."); return
        self._log("Mise à jour de SubSync…")
        def run():
            try:
                new, _ = upd.install(log=lambda m: self.q.put(("log", "  " + m)))
                self.q.put(("upd_done", new))
            except Exception as e:
                self.q.put(("log", "  mise à jour impossible : %s" % e))
        threading.Thread(target=run, daemon=True).start()
        if win: win.destroy()

    def _after_update(self, new):
        self.upd_btn.configure(text="Mises à jour…", fg_color="transparent", text_color=("gray10", "gray90"))
        if messagebox.askyesno("SubSync", "SubSync %s est installé.\n\nRedémarrer SubSync maintenant pour l'utiliser ?" % new):
            bat = os.path.join(APP_DIR, "SubSync.bat")
            if os.name == "nt" and os.path.exists(bat):
                subprocess.Popen(["cmd", "/c", "start", "", bat], cwd=APP_DIR, creationflags=0x08000000)
            self.destroy()

    def open_updates(self, found=None):
        w = ctk.CTkToplevel(self); w.title("Mises à jour de SubSync"); w.geometry("560x560"); w.transient(self); w.grab_set()
        st = upd.settings()
        ctk.CTkLabel(w, text="Version installée : %s" % VERSION, font=ctk.CTkFont(size=15, weight="bold")).pack(anchor="w", padx=16, pady=(14, 4))
        info = ctk.CTkLabel(w, text="", justify="left", wraplength=520); info.pack(anchor="w", padx=16)
        notes = ctk.CTkTextbox(w, height=170, wrap="word"); notes.pack(fill="x", padx=16, pady=6); notes.configure(state="disabled")
        bar = ctk.CTkFrame(w, fg_color="transparent"); bar.pack(fill="x", padx=16, pady=4)
        inst = ctk.CTkButton(bar, text="Installer la mise à jour", state="disabled", command=lambda: self._install_update(w))
        def show(v, txt):
            if upd.is_newer(v):
                info.configure(text="Nouvelle version disponible : %s" % v); inst.configure(state="normal")
            else:
                info.configure(text="SubSync est à jour (dernière version : %s)." % v); inst.configure(state="disabled")
            notes.configure(state="normal"); notes.delete("1.0", "end"); notes.insert("1.0", txt or "(pas de notes de version)"); notes.configure(state="disabled")
        def verify():
            info.configure(text="Vérification…"); w.update()
            try: show(*upd.check())
            except Exception as e: info.configure(text="Vérification impossible : %s" % e)
        ctk.CTkButton(bar, text="Vérifier maintenant", command=verify).pack(side="left")
        inst.pack(side="left", padx=8)
        ctk.CTkLabel(w, text="Quand une nouvelle version est disponible :").pack(anchor="w", padx=16, pady=(10, 0))
        modes = {"Me demander": "ask", "L'installer automatiquement": "auto", "Ne pas vérifier": "off"}
        mv = ctk.StringVar(value=next((k for k, v in modes.items() if v == st.get("update_mode", "ask")), "Me demander"))
        ctk.CTkSegmentedButton(w, values=list(modes), variable=mv, command=lambda v: upd.save_settings(update_mode=modes[v])).pack(anchor="w", padx=16, pady=4)
        ctk.CTkLabel(w, text="Clé d'accès GitHub (nécessaire seulement si le dépôt est privé) :").pack(anchor="w", padx=16, pady=(10, 0))
        tok = ctk.CTkEntry(w, width=520, show="•"); tok.pack(anchor="w", padx=16, pady=4); tok.insert(0, st.get("github_token", ""))
        tok.bind("<FocusOut>", lambda e: upd.save_settings(github_token=tok.get().strip()))
        b2 = ctk.CTkFrame(w, fg_color="transparent"); b2.pack(fill="x", padx=16, pady=(12, 4))
        def engines():
            if self.worker: messagebox.showinfo("SubSync", "Attendez la fin du traitement en cours."); return
            if not messagebox.askyesno("SubSync", "Mettre à jour les moteurs de calcul (transcription, traduction, carte graphique) ?\n\nLes versions actuelles sont sauvegardées et remises automatiquement si un test échoue."): return
            w.destroy(); self._log("Mise à jour des moteurs de calcul…")
            def run():
                try:
                    ok = upd.update_engines(log=lambda m: self.q.put(("log", "  " + m)))
                    self.q.put(("log", "Moteurs à jour." if ok else "Moteurs : un problème a été détecté, les versions précédentes ont été remises."))
                except Exception as e:
                    self.q.put(("log", "  mise à jour des moteurs impossible : %s" % e))
            threading.Thread(target=run, daemon=True).start()
        def back():
            if not messagebox.askyesno("SubSync", "Revenir à la version précédente de SubSync ?"): return
            try:
                v = upd.rollback(log=lambda m: self._log("  " + m)); w.destroy(); self._after_update(v)
            except Exception as e: messagebox.showinfo("SubSync", str(e))
        ctk.CTkButton(b2, text="Mettre à jour les moteurs", fg_color="transparent", border_width=1, text_color=("gray10", "gray90"), command=engines).pack(side="left")
        ctk.CTkButton(b2, text="Revenir à la version précédente", fg_color="transparent", border_width=1, text_color=("gray10", "gray90"), command=back).pack(side="left", padx=8)
        w.protocol("WM_DELETE_WINDOW", lambda: (upd.save_settings(github_token=tok.get().strip()), w.destroy()))
        if found: show(*found)
        else: w.after(200, verify)

    def _poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                kind = m[0]
                if kind == "log": self._log(m[1])
                elif kind == "status": self.status.configure(text=m[1])
                elif kind == "prog": self.prog.set(m[1])
                elif kind == "state":
                    if m[2] == "run": self.t_item = time.time()
                    if m[1] in self.rows: self.rows[m[1]].state = m[2]; self._refresh(m[1])
                elif kind == "done":
                    self._queue_mark(m[1])
                    if m[1] in self.rows:
                        r = self.rows[m[1]]; r.state = m[2]; r.results = m[3]; self._refresh(m[1])
                elif kind == "err":
                    self._queue_mark(m[1])
                    if m[1] in self.rows:
                        r = self.rows[m[1]]; r.state = "err"; r.error = m[2]; self._refresh(m[1])
                elif kind == "upd_found":
                    self._on_update_found(m[1], m[2], m[3])
                elif kind == "upd_done":
                    self._after_update(m[1])
                elif kind == "preview_ready":
                    self._show_preview(m[1])
                elif kind == "preview_end":
                    self.worker = None; self.go.configure(state="normal"); self.stop_btn.configure(state="disabled")
                    self.mode.configure(state="normal"); self.prev_btn.configure(state="normal"); self._timer_stop(); self.prog.set(1)
                    self.status.configure(text="Aperçu terminé.")
                elif kind == "fuspairs":
                    self._apply_content_pairs(m[1])
                elif kind == "fusend":
                    self.worker = None; self.go.configure(state="normal"); self.stop_btn.configure(state="disabled"); self.mode.configure(state="normal")
                    self._timer_stop(); self.prog.set(1)
                    if self.view == "merge": self._set_view("merge", force=True)
                elif kind == "end":
                    self._timer_stop()
                    if getattr(self, "_pending_update", False):
                        self._pending_update = False; self.after(1500, self._install_update)
                    self.worker = None; self.go.configure(state="normal"); self.stop_btn.configure(state="disabled"); self.mode.configure(state="normal")
                    rs = [r for r in self.rows.values() if r.results]
                    nb_ok = sum(1 for r in rs if r.state == "ok"); nb_c = sum(1 for r in rs if r.state == "created")
                    if nb_c and not nb_ok and nb_c == len(rs):
                        self.status.configure(text="Terminé : %d fichier(s) de sous-titres créé(s)." % nb_c)
                    else:
                        self.status.configure(text="Terminé : %d bon(s), %d à vérifier." % (nb_ok, len(rs) - nb_ok - nb_c))
                    self.prog.set(1)
                    self._finish_actions(self.stop_flag)
                self._show_detail()
        except queue.Empty:
            pass
        self.after(120, self._poll)

    def _log(self, msg):
        self.logbox.configure(state="normal"); self.logbox.insert("end", msg.rstrip() + "\n"); self.logbox.see("end")
        self.logbox.configure(state="disabled")
        try:
            os.makedirs(os.path.join(APP_DIR, "logs"), exist_ok=True)
            with open(os.path.join(APP_DIR, "logs", "subsync.log"), "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg.rstrip() + "\n")
        except Exception:
            pass


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App().mainloop()


if __name__ == "__main__":
    main()
