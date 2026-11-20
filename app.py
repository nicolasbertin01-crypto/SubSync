# -*- coding: utf-8 -*-
"""SubSync - interface graphique."""
import os, sys, queue, threading, traceback, subprocess, time
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
ENH_PRECLEAN = {"Aucun": "", "Léger (vidéos compressées)": "leger", "Fort (blocs très visibles en mouvement)": "fort"}
ENH_GRAIN = {"Garder": "", "Réduire (conseillé)": "reduire", "Fortement réduire": "fort"}
ENH_SIZE = {"Compact ≈ 1 Go (conseillé)": "compact", "Qualité maximale ≈ 3 Go": "max"}
ENH_FACES = {"Naturels (GFPGAN, conseillé)": "gfpgan", "Détaillés (GPEN)": "gpen", "Ne pas retoucher les visages": ""}
VIDEO_PY = os.path.join(APP_DIR, ".venv_video", "Scripts", "python.exe") if os.name == "nt" else os.environ.get("SUBSYNC_VIDEO_PY", sys.executable)


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
        ctk.CTkLabel(self.enh_row, text="Fichier").grid(row=10, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.enh_size = ctk.CTkComboBox(self.enh_row, values=list(ENH_SIZE), width=265, command=lambda v: self._save_enh())
        self.enh_size.set(es.get("size", "Compact ≈ 1 Go (conseillé)")); self.enh_size.grid(row=11, column=0, columnspan=2, sticky="w")
        self.enh_help = ctk.CTkLabel(self.enh_row, text="", text_color=("gray35", "gray65"), font=ctk.CTkFont(size=11), wraplength=262, justify="left")
        self.enh_help.grid(row=12, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.enh_row.grid_remove()
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
                                  grain=self.enh_grain.get(), size=self.enh_size.get())
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
    def start(self):
        if self.worker: return
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
                             size=ENH_SIZE.get(self.enh_size.get(), "compact")))
        self.go.configure(state="disabled"); self.stop_btn.configure(state="normal"); self.mode.configure(state="disabled")
        self.prog.set(0); self._timer_start(len(todo))
        self.worker = threading.Thread(target=self._work, args=(todo, opts), daemon=True); self.worker.start()

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
                    o = {k_: v_ for k_, v_ in opts.items() if k_ not in ("quality", "translator", "keep_langs", "enh")}
                    res = eng.run_job(r.srt, r.videos, log=lg, progress=prog, cancel=cc, **o)
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

    def _enhance(self, video, opts, log, progress, cancel):
        """Lance subsync_enhance.py dans l'environnement vidéo et suit sa progression."""
        import json, subprocess
        info = merge.video_info(video)
        e = opts["enh"]
        d = opts["out_dir"] or os.path.dirname(video); os.makedirs(d, exist_ok=True)
        stem = os.path.splitext(os.path.basename(video))[0]
        out = os.path.join(d, stem + " - HD.mkv")
        if os.path.abspath(out) == os.path.abspath(video): out = os.path.join(d, stem + " - HD (2).mkv")
        tag = "v3_%s_%d_%s_%d%s" % (e["preset"], e["target_h"], e["faces"] or "sans", int(e["face_strength"] * 100),
                                    ("_" + e["preclean"] if e.get("preclean") else "") + ("_g" + e["degrain"] if e.get("degrain") else "") + "_" + e.get("size", "compact"))
        work = os.path.join(d, ".subsync_hd_" + stem + "_" + tag)
        job = dict(info, ffmpeg=eng.ffmpeg_exe(), src=video, out=out, work=work, sharpen=0.35, **e)
        os.makedirs(work, exist_ok=True)
        jp = os.path.join(work, "tache_gui.json")
        with open(jp, "w", encoding="utf-8") as f: json.dump(job, f)
        log("Amélioration de « %s » : %d×%d, %.3f i/s, ~%d images" % (os.path.basename(video), info["width"], info["height"],
                                                                    info["fps_num"] / info["fps_den"], info["frames"]))
        p = subprocess.Popen([VIDEO_PY, "-u", os.path.join(APP_DIR, "subsync_enhance.py"), jp], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             cwd=APP_DIR, env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"),
                             creationflags=0x08000000 if os.name == "nt" else 0)
        result = {}; err = None
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
            elif m.get("t") == "done": result = m
            elif m.get("t") == "err": err = m["m"]
        p.wait()
        if cancel():
            log("  arrêté : relancez plus tard, le travail reprendra où il s'est arrêté."); raise eng.Cancelled()
        if err or not result: raise RuntimeError(err or "l'amélioration s'est arrêtée sans résultat")
        fps = info["frames"] / max(1.0, result.get("seconds", 1))
        summ = "%s → %dp%s · %.1f images/s" % (os.path.basename(out), e["target_h"],
                                               (", %d visages restaurés" % result.get("faces", 0)) if e["faces"] else "", fps)
        return eng.Result(video, out, "amélioration", 100.0, "ok", summ, {}, [])

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
                    if m[1] in self.rows:
                        r = self.rows[m[1]]; r.state = m[2]; r.results = m[3]; self._refresh(m[1])
                elif kind == "err":
                    if m[1] in self.rows:
                        r = self.rows[m[1]]; r.state = "err"; r.error = m[2]; self._refresh(m[1])
                elif kind == "upd_found":
                    self._on_update_found(m[1], m[2], m[3])
                elif kind == "upd_done":
                    self._after_update(m[1])
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
