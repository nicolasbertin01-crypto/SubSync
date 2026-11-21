# -*- coding: utf-8 -*-
"""SubSync - amélioration vidéo (agrandissement IA Real-ESRGAN + restauration des visages).

Lancé par SubSync dans son propre environnement (.venv_video : onnxruntime-directml, numpy, opencv),
avec un fichier de tâche JSON. Communique par lignes JSON sur la sortie standard :
  {"t": "log", "m": "..."}  {"t": "prog", "done": n, "total": N}  {"t": "done", "out": "..."}  {"t": "err", "m": "..."}
Le travail est découpé en tronçons : en cas d'arrêt, la reprise repart du dernier tronçon terminé.
"""
import json, os, sys, time, shutil, subprocess, threading, queue
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(HERE, "models", "video")
SEG = int(os.environ.get("SUBSYNC_SEG", "2400"))   # images par tronçon (~1 min 40 à 24 i/s)

JCJ = "https://raw.githubusercontent.com/jcj83429/upscaling/"
PRESETS = {
    # modèles « LiveAction » de jcj83429 (licence CC BY-NC-SA 4.0, usage personnel) : entraînés pour la vidéo
    # réelle (compression, flou, halos, IVTC), agrandissement x2 fidèle aux textures (peau, grain).
    "fidele": dict(kind="raw2x", file="2xLiveActionV1_SPAN.onnx", mix=1.0,
                   url=JCJ + "f73a3a02874360ec6ced18f8bdd8e43b5d7bba57/2xLiveActionV1_SPAN/2xLiveActionV1_SPAN_490000.onnx",
                   sha="bfa72f3c6347076aed140d0836cee30c27ea434c047beeaf9466469483836ecc"),
    "net": dict(kind="raw2x", file="2xLiveActionHeavy_ESRGAN_lite_f16.onnx", mix=1.0,
                url=JCJ + "9c785a8c63ee33742797e4ee354dbcf505fc7e1d/2xLiveActionHeavy_ESRGAN_lite/2xLiveActionHeavy_ESRGAN_lite_f16.onnx",
                sha="3184b023f0892f0d4441f02a1376d8308f36223fc0ea829e201db0fe3aa20fd6"),
    # Real-ESRGAN general x4v3 (graphe SubSync) : plus lissant, pour les sources très bruitées
    "lisse": dict(kind="graph", file="upscale_lisse.onnx", mix=0.85),
    "naturel": dict(kind="graph", file="upscale_naturel.onnx", mix=0.55),
    "equilibre": dict(kind="graph", file="upscale_equilibre.onnx", mix=0.70),
}
BITRATE_M = 10                   # Mb/s pour le 1080p (réduit au prorata pour le 720p)
# nettoyage de la source avant l'IA (filtres ffmpeg) : blocs de compression puis léger débruitage temporel
PRECLEAN = {
    "leger": "deblock=filter=weak:block=8,hqdn3d=1.0:1.0:2.5:2.5",
    "fort": "deblock=filter=strong:block=8,hqdn3d=1.5:1.5:3.5:3.5",
}
FACE_MODELS = {"gfpgan": "gfpgan_1.4.onnx", "gpen": "gpen_bfr_512.onnx"}
NOWIN = 0x08000000 if os.name == "nt" else 0


def emit(**k):
    sys.stdout.write(json.dumps(k, ensure_ascii=True) + "\n"); sys.stdout.flush()   # ASCII : sûr quel que soit l'encodage Windows


def log(m): emit(t="log", m=m)


def providers():
    import onnxruntime as ort
    av = ort.get_available_providers()
    if "DmlExecutionProvider" in av: return ["DmlExecutionProvider", "CPUExecutionProvider"], "carte graphique (DirectML)"
    return ["CPUExecutionProvider"], "processeur (TRÈS lent : installez l'amélioration vidéo pour utiliser la carte graphique)"


def ensure_model(spec):
    """Télécharge le modèle s'il manque (et vérifie son empreinte)."""
    import hashlib, urllib.request
    path = os.path.join(MODELS, spec["file"])
    if os.path.exists(path): return path
    if "url" not in spec: raise RuntimeError("modèle manquant : %s" % spec["file"])
    log("  téléchargement du modèle %s…" % spec["file"])
    tmp = path + ".part"
    with urllib.request.urlopen(urllib.request.Request(spec["url"], headers={"User-Agent": "SubSync"}), timeout=300) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    h = hashlib.sha256(open(tmp, "rb").read()).hexdigest()
    if spec.get("sha") and h != spec["sha"]:
        os.remove(tmp); raise RuntimeError("modèle %s corrompu (empreinte inattendue)" % spec["file"])
    os.replace(tmp, path)
    return path


def session(path, prov):
    import onnxruntime as ort
    so = ort.SessionOptions(); so.log_severity_level = 3
    if prov[0] == "DmlExecutionProvider":
        so.enable_mem_pattern = False; so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    return ort.InferenceSession(path, so, providers=prov)


def _works(ff, args):
    try:
        r = subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=1280x720:d=0.2", "-frames:v", "3"] + args + ["-f", "null", "-"],
                           capture_output=True, timeout=60, creationflags=NOWIN)
        return r.returncode == 0
    except Exception:
        return False


def pick_encoder(ff, size="compact"):
    """compact : x265 sur le processeur (qualité constante, fichiers 3 à 4 fois plus petits) ;
    max : encodeur de la carte graphique à débit élevé (rapide, fichiers gros)."""
    rate = ["-b:v", "%dM" % BITRATE_M, "-maxrate", "%dM" % int(BITRATE_M * 1.6), "-bufsize", "%dM" % (BITRATE_M * 2)]
    x265 = ("libx265", ["-c:v", "libx265", "-preset", "fast", "-crf", "21", "-x265-params", "log-level=error:aq-mode=3"])
    hw = [("hevc_amf", ["-c:v", "hevc_amf", "-quality", "quality", "-rc", "vbr_peak"] + rate),
          ("hevc_nvenc", ["-c:v", "hevc_nvenc", "-preset", "p6", "-rc", "vbr"] + rate),
          ("hevc_qsv", ["-c:v", "hevc_qsv"] + rate)]
    cands = ([x265] + hw) if size == "compact" else (hw + [x265])
    for name, args in cands:
        if _works(ff, args): return name, args
    return "libx264", ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]


# réduction du grain après l'IA (débruitage spatial + temporel, rapide) : image plus propre et fichier plus petit
DEGRAIN = {"reduire": "hqdn3d=2:1.5:4:3", "fort": "hqdn3d=3.5:2.5:7:5"}


def run(job):
    ff = job["ffmpeg"]; src = job["src"]; out = job["out"]
    W, H = job["width"], job["height"]
    fps_n, fps_d = job["fps_num"], job["fps_den"]; fps = fps_n / fps_d
    total = int(job["frames"])
    TH = int(job["target_h"]); TW = int(round(TH * job["dar"] / 2)) * 2
    prov, where = providers()
    spec = PRESETS.get(job.get("preset", "fidele"), PRESETS["fidele"]); mix = spec["mix"]
    log("Amélioration : %s → %d×%d (%s, réglage « %s »%s), calcul sur %s" % (os.path.basename(src), TW, TH,
        "visages " + job["faces"] if job.get("faces") else "sans visages", job.get("preset"),
        (", nettoyage " + job["preclean"] if job.get("preclean") else "") + (", grain " + job["degrain"] if job.get("degrain") else ""), where))
    import cv2
    up = session(ensure_model(spec), prov)
    up_in = up.get_inputs()[0].name
    up_f16 = "float16" in up.get_inputs()[0].type
    fr = None
    if job.get("faces"):
        sys.path.insert(0, HERE)
        import subsync_faces
        fr = subsync_faces.FaceRestorer(os.path.join(MODELS, "scrfd_2.5g.onnx"), os.path.join(MODELS, FACE_MODELS[job["faces"]]), prov)
    fstr = float(job.get("face_strength", 0.4))
    global BITRATE_M
    BITRATE_M = max(4, int(round(10 * (TW * TH) / (1440 * 1080))))
    enc_name, enc = pick_encoder(ff, job.get("size", "compact"))
    log("  encodeur vidéo : %s" % enc_name)
    work = job["work"]; os.makedirs(work, exist_ok=True)
    with open(os.path.join(work, "tache.json"), "w", encoding="utf-8") as f: json.dump(job, f)
    nseg = (total + SEG - 1) // SEG
    done_frames = sum(min(SEG, total - k * SEG) for k in range(nseg) if os.path.exists(os.path.join(work, "seg_%05d.mkv" % k)))
    if done_frames: log("  reprise : %d / %d images déjà traitées" % (done_frames, total))
    emit(t="prog", done=done_frames, total=total)
    sizes = np.array([1, 3, TH, TW], np.int64); mixa = np.array([mix], np.float32)
    dg = DEGRAIN.get(job.get("degrain") or "")
    vf = (dg + "," if dg else "") + "cas=%.2f,scale=out_color_matrix=bt709:out_range=tv,format=yuv420p" % float(job.get("sharpen", 0.35))
    t_start = time.time(); nfaces = 0
    stats = {"faces": 0, "frames": 0}

    def do_segment(k, n, last, part, t0=None, prog_base=None):
        """Traite un tronçon ; renvoie le nombre d'images, lève OSError/RuntimeError en cas de problème."""
        if t0 is None: t0 = k * SEG * fps_d / fps_n
        if os.environ.get("SUBSYNC_FAIL_ONCE") == str(k) and not stats.get("failed"):   # test de la reprise automatique
            stats["failed"] = True; raise OSError("panne simulée")
        pre = PRECLEAN.get(job.get("preclean") or "")
        dec = subprocess.Popen([ff, "-v", "error", "-ss", "%.6f" % t0, "-i", src, "-map", "0:v:0"] + (["-vf", pre] if pre else []) +
                               ([] if last else ["-frames:v", str(n)]) +
                               ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE, creationflags=NOWIN)
        encp = subprocess.Popen([ff, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (TW, TH),
                                 "-r", "%d/%d" % (fps_n, fps_d), "-i", "-", "-vf", vf] + enc +
                                ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", part],
                                stdin=subprocess.PIPE, creationflags=NOWIN)
        q_in = queue.Queue(8); q_out = queue.Queue(8); fsz = W * H * 3; werr = []
        def reader():
            try:
                while True:
                    b = dec.stdout.read(fsz)
                    if len(b) < fsz: break
                    q_in.put(b)
            except Exception as e:
                werr.append(e)
            q_in.put(None)
        def writer():
            while True:
                b = q_out.get()
                if b is None: return
                if werr: continue                      # on vide la file sans écrire
                try: encp.stdin.write(b)
                except Exception as e: werr.append(e)
        tr = threading.Thread(target=reader, daemon=True); tw = threading.Thread(target=writer, daemon=True)
        tr.start(); tw.start(); got = 0; nf_seg = 0
        try:
            while True:
                b = q_in.get()
                if b is None or werr: break
                src8 = np.frombuffer(b, np.uint8).reshape(H, W, 3)
                x = (src8.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]
                if spec["kind"] == "graph":
                    y = up.run(None, {"input": x, "mix": mixa, "sizes": sizes})[0][0]
                    img = (y.transpose(1, 2, 0) * 255.0 + 0.5).astype(np.uint8)
                else:
                    y = up.run(None, {up_in: x.astype(np.float16) if up_f16 else x})[0][0].astype(np.float32)
                    img = (np.clip(y.transpose(1, 2, 0), 0, 1) * 255.0 + 0.5).astype(np.uint8)
                    if img.shape[1] != TW or img.shape[0] != TH:
                        img = cv2.resize(img, (TW, TH), interpolation=cv2.INTER_AREA if img.shape[1] > TW else cv2.INTER_CUBIC)
                    if mix < 0.999:
                        base = cv2.resize(src8, (TW, TH), interpolation=cv2.INTER_CUBIC)
                        img = cv2.addWeighted(img, mix, base, 1 - mix, 0)
                if fr is not None:
                    img, nf = fr.restore(img, fstr); nf_seg += nf
                q_out.put(np.ascontiguousarray(img).tobytes()); got += 1
                if got % 24 == 0:
                    if prog_base is None: emit(t="prog", done=done_frames + got, total=total)
                    else: emit(t="prog", done=prog_base[0] + got, total=prog_base[1])
        finally:
            q_out.put(None); tw.join()
            try: encp.stdin.close()
            except Exception: pass
            if werr:
                for p_ in (dec, encp):
                    try: p_.kill()
                    except Exception: pass
            encp.wait(); dec.wait()
        if werr: raise OSError("lecture/écriture interrompue (%s)" % werr[0])
        if dec.returncode != 0: raise OSError("lecture de la vidéo interrompue (code %s)" % dec.returncode)
        if encp.returncode != 0: raise OSError("encodage interrompu (code %s)" % encp.returncode)
        if not last and got != n: raise OSError("tronçon incomplet (%d images sur %d)" % (got, n))
        stats["faces"] += nf_seg
        return got

    if job.get("preview"):
        return preview(job, do_segment, stats, fps_n, fps_d, TW, TH, work)

    k = 0
    while k < nseg:
        seg = os.path.join(work, "seg_%05d.mkv" % k)
        if os.path.exists(seg): k += 1; continue
        n = min(SEG, total - k * SEG); last = k == nseg - 1
        part = seg[:-4] + ".part.mkv"
        for attempt in range(1, 5):
            try:
                got = do_segment(k, n, last, part); break
            except OSError as e:
                try: os.remove(part)
                except OSError: pass
                if attempt == 4:
                    raise RuntimeError("tronçon %d : %s. Le disque de la vidéo est-il toujours accessible ? "
                                       "Relancez : le travail reprendra à ce tronçon." % (k + 1, e))
                wait = float(os.environ.get("SUBSYNC_RETRY_WAIT", 15)) * attempt
                log("  tronçon %d : %s → nouvel essai dans %.0f s (%d/3)" % (k + 1, e, wait, attempt))
                time.sleep(wait)
        if got == 0:
            if k == 0: raise RuntimeError("aucune image lue dans la vidéo")
            try: os.remove(part)
            except OSError: pass
            nseg = k; break              # durée surestimée : la vidéo est finie
        os.replace(part, seg); done_frames += got; stats["frames"] += got
        el = time.time() - t_start
        try: mbps = os.path.getsize(seg) * 8 / (got * fps_d / fps_n) / 1e6
        except OSError: mbps = 0
        log("  tronçon %d/%d terminé (%.1f images/s, %.1f Mb/s)" % (k + 1, nseg, stats["frames"] / max(1e-6, el), mbps))
        emit(t="prog", done=done_frames, total=total)
        k += 1
    nfaces = stats["faces"]
    # assemblage : vidéo améliorée + audio, sous-titres, chapitres et pièces jointes d'origine
    log("  assemblage du fichier final (sons et sous-titres d'origine)…")
    lst = os.path.join(work, "liste.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for k in range(nseg): f.write("file '%s'\n" % os.path.join(work, "seg_%05d.mkv" % k).replace("\\", "/").replace("'", "'\\''"))
    extra = [x for x in job.get("extra_subs", []) if os.path.exists(x["path"])]

    def mux(with_subs):
        a = [ff, "-v", "error", "-y"]
        if job.get("vstart", 0) > 0.001: a += ["-itsoffset", "%.3f" % job["vstart"]]
        a += ["-f", "concat", "-safe", "0", "-i", lst, "-i", src]
        for x in extra: a += ["-sub_charenc", "UTF-8", "-i", x["path"]]
        a += ["-map", "0:v:0", "-map", "1:a?"]
        if with_subs: a += ["-map", "1:s?"]
        for j in range(len(extra)): a += ["-map", "%d:0" % (2 + j)]
        # max_interleave_delta 0 : son et image strictement entrelacés dans le fichier (sinon tout le son peut
        # se retrouver regroupé au début du fichier et les lecteurs n'en jouent rien)
        a += ["-map", "1:t?", "-c", "copy", "-map_metadata", "1", "-map_chapters", "1", "-max_interleave_delta", "0"]
        if with_subs:
            for i, c in enumerate(job.get("sub_codecs", [])):
                if c == "mov_text": a += ["-c:s:%d" % i, "srt"]
        base = len(job.get("sub_codecs", [])) if with_subs else 0
        for j, x in enumerate(extra):
            k = base + j
            a += ["-c:s:%d" % k, "srt", "-metadata:s:s:%d" % k, "language=" + x.get("lang", "fre"),
                  "-metadata:s:s:%d" % k, "title=" + x.get("title", "SubSync")]
        return subprocess.run(a + [tmp_out], capture_output=True, creationflags=NOWIN)
    tmp_out = out[:-4] + ".part.mkv"
    r = mux(True)
    if r.returncode != 0:      # sous-titres incompatibles : on réessaie sans eux
        log("  (sous-titres non recopiables, fichier créé sans eux)")
        r = mux(False)
        if r.returncode != 0: raise RuntimeError("assemblage : " + r.stderr.decode(errors="replace")[-400:])
    os.replace(tmp_out, out)
    shutil.rmtree(work, ignore_errors=True)
    el = time.time() - t_start
    log("  terminé : %s" % os.path.basename(out) + (" · %d visages restaurés" % nfaces if fr is not None else ""))
    emit(t="done", out=out, faces=nfaces, seconds=el)


def preview(job, do_segment, stats, fps_n, fps_d, TW, TH, work):
    """Aperçu : quelques extraits traités avec les réglages choisis + images avant/après + vidéos côte à côte."""
    ff = job["ffmpeg"]; src = job["src"]; fps = fps_n / fps_d
    pv = job["preview"]; dur = float(os.environ.get("SUBSYNC_PREVIEW_DUR", pv.get("dur", 5))); times = [float(t) for t in pv["times"]]
    n = max(12, int(round(dur * fps))); tot = n * len(times)
    items = []; t_proc = 0.0; enc_bytes = 0
    for i, t in enumerate(times):
        log("  aperçu %d/%d : extrait à %d:%02d" % (i + 1, len(times), int(t) // 60, int(t) % 60))
        part = os.path.join(work, "apercu_%d.mkv" % i)
        t1 = time.time()
        n_i = max(1, min(n, int(job["frames"]) - int(t * fps) - 1))
        got = do_segment(i, n_i, False, part, t0=t, prog_base=(i * n, tot))
        if i > 0 or len(times) == 1: t_proc += (time.time() - t1) / max(1, got) * n
        enc_bytes += os.path.getsize(part)
        mid = t + dur / 2
        o_png = os.path.join(work, "avant_%d.png" % i); e_png = os.path.join(work, "apres_%d.png" % i)
        sbs = os.path.join(work, "cote_a_cote_%d.mp4" % i)
        subprocess.run([ff, "-v", "error", "-y", "-ss", "%.3f" % mid, "-i", src, "-frames:v", "1",
                        "-vf", "scale=%d:%d:flags=bicubic" % (TW, TH), o_png], creationflags=NOWIN)
        subprocess.run([ff, "-v", "error", "-y", "-ss", "%.3f" % (dur / 2), "-i", part, "-frames:v", "1", e_png], creationflags=NOWIN)
        subprocess.run([ff, "-v", "error", "-y", "-ss", "%.3f" % t, "-t", "%.3f" % dur, "-i", src, "-i", part, "-filter_complex",
                        "[0:v]scale=%d:%d:flags=bicubic,setsar=1[a];[1:v]setsar=1[b];[a][b]hstack,format=yuv420p" % (TW, TH),
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-an", sbs], creationflags=NOWIN)
        items.append(dict(time=t, avant=o_png, apres=e_png, video=sbs if os.path.exists(sbs) else part))
    per_frame = t_proc / max(1, (len(times) - 1 if len(times) > 1 else 1) * n)
    total = int(job["frames"]); dur_all = total * fps_d / fps_n
    mbps = enc_bytes * 8 / (len(times) * n / fps) / 1e6
    emit(t="preview", items=items, fps=(1 / per_frame if per_frame else 0), est_seconds=per_frame * total,
         est_size_mb=(mbps * dur_all / 8) + job.get("audio_mb", 0), mbps=mbps)
    emit(t="done", out="", faces=stats["faces"], seconds=0)


if __name__ == "__main__":
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
    try:
        with open(sys.argv[1], encoding="utf-8") as f: job = json.load(f)
        run(job)
    except Exception as e:
        import traceback
        emit(t="log", m=traceback.format_exc()); emit(t="err", m=str(e)); sys.exit(1)
