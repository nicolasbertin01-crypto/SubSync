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

PRESETS = {                      # modèle, part de l'IA dans le mélange
    "naturel": ("upscale_naturel.onnx", 0.55),
    "equilibre": ("upscale_equilibre.onnx", 0.70),
    "lisse": ("upscale_lisse.onnx", 0.85),
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


def session(path, prov):
    import onnxruntime as ort
    so = ort.SessionOptions(); so.log_severity_level = 3
    if prov[0] == "DmlExecutionProvider":
        so.enable_mem_pattern = False; so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    return ort.InferenceSession(path, so, providers=prov)


def pick_encoder(ff):
    """Encodeur matériel AMD si disponible (rapide), sinon x264 sur le processeur."""
    cands = [("hevc_amf", ["-c:v", "hevc_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", "18", "-qp_p", "20"]),
             ("hevc_nvenc", ["-c:v", "hevc_nvenc", "-preset", "p6", "-rc", "constqp", "-qp", "19"]),
             ("hevc_qsv", ["-c:v", "hevc_qsv", "-global_quality", "20"]),
             ("libx264", ["-c:v", "libx264", "-preset", "medium", "-crf", "17"])]
    for name, args in cands:
        if name == "libx264": return name, args
        try:
            r = subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=1280x720:d=0.2", "-frames:v", "3"] + args + ["-f", "null", "-"],
                               capture_output=True, timeout=60, creationflags=NOWIN)
            if r.returncode == 0: return name, args
        except Exception:
            pass
    return cands[-1]


def run(job):
    ff = job["ffmpeg"]; src = job["src"]; out = job["out"]
    W, H = job["width"], job["height"]
    fps_n, fps_d = job["fps_num"], job["fps_den"]; fps = fps_n / fps_d
    total = int(job["frames"])
    TH = int(job["target_h"]); TW = int(round(TH * job["dar"] / 2)) * 2
    prov, where = providers()
    model, mix = PRESETS[job.get("preset", "equilibre")]
    log("Amélioration : %s → %d×%d (%s, réglage « %s »), calcul sur %s" % (os.path.basename(src), TW, TH,
        "visages " + job["faces"] if job.get("faces") else "sans visages", job.get("preset"), where))
    up = session(os.path.join(MODELS, model), prov)
    fr = None
    if job.get("faces"):
        sys.path.insert(0, HERE)
        import subsync_faces
        fr = subsync_faces.FaceRestorer(os.path.join(MODELS, "scrfd_2.5g.onnx"), os.path.join(MODELS, FACE_MODELS[job["faces"]]), prov)
    fstr = float(job.get("face_strength", 0.55))
    enc_name, enc = pick_encoder(ff)
    log("  encodeur vidéo : %s" % enc_name)
    work = job["work"]; os.makedirs(work, exist_ok=True)
    with open(os.path.join(work, "tache.json"), "w", encoding="utf-8") as f: json.dump(job, f)
    nseg = (total + SEG - 1) // SEG
    done_frames = sum(min(SEG, total - k * SEG) for k in range(nseg) if os.path.exists(os.path.join(work, "seg_%05d.mkv" % k)))
    if done_frames: log("  reprise : %d / %d images déjà traitées" % (done_frames, total))
    emit(t="prog", done=done_frames, total=total)
    sizes = np.array([1, 3, TH, TW], np.int64); mixa = np.array([mix], np.float32)
    vf = "cas=%.2f,scale=out_color_matrix=bt709:out_range=tv,format=yuv420p" % float(job.get("sharpen", 0.35))
    t_start = time.time(); nfaces = 0
    for k in range(nseg):
        seg = os.path.join(work, "seg_%05d.mkv" % k)
        if os.path.exists(seg): continue
        n = min(SEG, total - k * SEG); t0 = k * SEG * fps_d / fps_n
        last = k == nseg - 1          # le dernier tronçon lit jusqu'à la fin (nombre d'images estimé)
        dec = subprocess.Popen([ff, "-v", "error", "-ss", "%.6f" % t0, "-i", src, "-map", "0:v:0"] + ([] if last else ["-frames:v", str(n)]) +
                               ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE, creationflags=NOWIN)
        part = seg[:-4] + ".part.mkv"
        encp = subprocess.Popen([ff, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (TW, TH),
                                 "-r", "%d/%d" % (fps_n, fps_d), "-i", "-", "-vf", vf] + enc +
                                ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", part],
                                stdin=subprocess.PIPE, creationflags=NOWIN)
        q_in = queue.Queue(8); q_out = queue.Queue(8)
        fsz = W * H * 3
        def reader():
            while True:
                b = dec.stdout.read(fsz)
                if len(b) < fsz: q_in.put(None); return
                q_in.put(b)
        def writer():
            while True:
                b = q_out.get()
                if b is None: return
                encp.stdin.write(b)
        tr = threading.Thread(target=reader, daemon=True); tw = threading.Thread(target=writer, daemon=True)
        tr.start(); tw.start(); got = 0
        while True:
            b = q_in.get()
            if b is None: break
            x = (np.frombuffer(b, np.uint8).reshape(H, W, 3).astype(np.float32) / 255.0).transpose(2, 0, 1)[None]
            y = up.run(None, {"input": x, "mix": mixa, "sizes": sizes})[0][0]
            img = (y.transpose(1, 2, 0) * 255.0 + 0.5).astype(np.uint8)
            if fr is not None:
                img, nf = fr.restore(img, fstr); nfaces += nf
            q_out.put(np.ascontiguousarray(img).tobytes()); got += 1
            if got % 24 == 0: emit(t="prog", done=done_frames + got, total=total)
        q_out.put(None); tw.join(); encp.stdin.close(); encp.wait(); dec.wait()
        if got == 0:
            if k == 0: raise RuntimeError("aucune image lue dans la vidéo")
            try: os.remove(part)
            except OSError: pass
            nseg = k; break              # durée surestimée : la vidéo est finie
        if encp.returncode != 0: raise RuntimeError("l'encodage du tronçon %d a échoué" % (k + 1))
        os.replace(part, seg); done_frames += got
        el = time.time() - t_start
        log("  tronçon %d/%d terminé (%.1f images/s)" % (k + 1, nseg, got / max(1e-6, el) if k == 0 else done_frames / max(1e-6, el)))
        emit(t="prog", done=done_frames, total=total)
    # assemblage : vidéo améliorée + audio, sous-titres, chapitres et pièces jointes d'origine
    log("  assemblage du fichier final (sons et sous-titres d'origine)…")
    lst = os.path.join(work, "liste.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for k in range(nseg): f.write("file '%s'\n" % os.path.join(work, "seg_%05d.mkv" % k).replace("\\", "/").replace("'", "'\\''"))
    def mux(with_subs):
        a = [ff, "-v", "error", "-y"]
        if job.get("vstart", 0) > 0.001: a += ["-itsoffset", "%.3f" % job["vstart"]]
        a += ["-f", "concat", "-safe", "0", "-i", lst, "-i", src, "-map", "0:v:0", "-map", "1:a?"]
        if with_subs: a += ["-map", "1:s?"]
        a += ["-map", "1:t?", "-c", "copy", "-map_metadata", "1", "-map_chapters", "1"]
        if with_subs:
            for i, c in enumerate(job.get("sub_codecs", [])):
                if c == "mov_text": a += ["-c:s:%d" % i, "srt"]
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


if __name__ == "__main__":
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
    try:
        with open(sys.argv[1], encoding="utf-8") as f: job = json.load(f)
        run(job)
    except Exception as e:
        import traceback
        emit(t="log", m=traceback.format_exc()); emit(t="err", m=str(e)); sys.exit(1)
