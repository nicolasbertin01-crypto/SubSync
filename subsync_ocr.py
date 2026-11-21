# -*- coding: utf-8 -*-
"""SubSync - conversion des sous-titres « image » (DVD/VobSub, Blu-ray/PGS) en .srt par reconnaissance de texte
(modèle PP-OCRv5 latin, licence Apache 2.0, exécuté avec onnxruntime ; aucune installation supplémentaire)."""
import os, re, datetime
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(HERE, "models", "ocr")
REPO = "https://huggingface.co/PaddlePaddle/latin_PP-OCRv5_mobile_rec_onnx/resolve/main/"
IMAGE_SUBS = ("dvd_subtitle", "hdmv_pgs_subtitle", "dvb_subtitle", "xsub")


def ensure_model(log=print):
    import urllib.request, shutil
    os.makedirs(MODEL_DIR, exist_ok=True)
    for f in ("inference.onnx", "inference.yml"):
        p = os.path.join(MODEL_DIR, f)
        if os.path.exists(p) and os.path.getsize(p) > 1000: continue
        log("  téléchargement du modèle de lecture de texte (%s)…" % f)
        with urllib.request.urlopen(urllib.request.Request(REPO + f, headers={"User-Agent": "SubSync"}), timeout=300) as r, open(p + ".part", "wb") as o:
            shutil.copyfileobj(r, o)
        os.replace(p + ".part", p)


def _charset():
    path = os.path.join(MODEL_DIR, "inference.yml")
    try:
        import yaml
        with open(path, encoding="utf-8") as f: chars = [str(c) for c in yaml.safe_load(f)["PostProcess"]["character_dict"]]
    except ImportError:
        chars = []; on = False
        for line in open(path, encoding="utf-8"):
            if line.strip().startswith("character_dict:"): on = True; continue
            if on:
                t = line.rstrip("\n")
                if not t.lstrip().startswith("- "): break
                v = t.lstrip()[2:]
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                    v = v[1:-1].replace("''", "'") if v[0] == "'" else v[1:-1].encode().decode("unicode_escape")
                chars.append(v)
    return [""] + chars + [" "]


class Reader:
    def __init__(self):
        import onnxruntime as ort
        so = ort.SessionOptions(); so.log_severity_level = 3
        self.s = ort.InferenceSession(os.path.join(MODEL_DIR, "inference.onnx"), so, providers=["CPUExecutionProvider"])
        self.chars = _charset()

    def _line(self, gray):
        """gray : ligne de texte clair sur fond sombre (uint8)."""
        h, w = gray.shape
        nw = max(16, min(3200, int(round(w * 48 / h))))
        from PIL import Image
        im = Image.fromarray(gray).resize((nw, 48), Image.BILINEAR)
        a = np.asarray(im, np.float32) / 255.0
        a = 1.0 - a                                     # texte sombre sur fond clair, comme à l'entraînement
        x = np.repeat(((a - 0.5) / 0.5)[None, None], 3, axis=1).astype(np.float32)
        p = self.s.run(None, {"x": x})[0][0]
        idx = p.argmax(1); out = []; prev = -1
        for i in idx:
            if i != prev and i != 0 and i < len(self.chars): out.append(self.chars[i])
            prev = i
        return "".join(out).strip()

    def read(self, rgba):
        """Image de sous-titre (RGBA ou RGB) -> texte (lignes séparées par des sauts de ligne)."""
        a = np.asarray(rgba)
        if a.ndim == 2:
            lum = a.astype(np.float32)
        elif a.shape[2] == 4:
            alpha = a[..., 3].astype(np.float32) / 255.0
            lum = a[..., :3].astype(np.float32).mean(2) * alpha
        else:
            lum = a.astype(np.float32).mean(2) if a.ndim == 3 else a.astype(np.float32)
        lum = np.clip(lum, 0, 255).astype(np.uint8)
        rows = (lum > 100).sum(1)
        # découpe en lignes par projection horizontale
        on = rows > max(1, lum.shape[1] * 0.002); lines = []; i = 0; n = len(on)
        while i < n:
            if on[i]:
                j = i
                while j < n and (on[j] or (j + 3 < n and on[j:j + 4].any())): j += 1
                if j - i >= 6: lines.append((i, j))
                i = j
            else: i += 1
        txt = []
        for y0, y1 in lines:
            pad = max(2, (y1 - y0) // 6)
            seg = lum[max(0, y0 - pad):min(n, y1 + pad)]
            cols = np.where((seg > 100).any(0))[0]
            if not len(cols): continue
            seg = seg[:, max(0, cols[0] - pad):cols[-1] + pad + 1]
            t = self._line(seg)
            if t: txt.append(t)
        return "\n".join(_fix(t) for t in txt)


def _fix(t):
    t = t.replace(" ,", ",").replace(" .", ".").replace("''", '"')
    t = re.sub(r"\s+([?!:;»])", r" \1", t)          # typographie française
    return re.sub(r"\s{2,}", " ", t).strip()


def convert(path, stream_index, out_srt, log=print, progress=None, cancel=None):
    """Lit la piste de sous-titres image `stream_index` de `path` et écrit un .srt."""
    import av, srt
    ensure_model(log); rd = Reader()
    c = av.open(path); st = [s for s in c.streams if s.index == stream_index][0]
    dur = float(c.duration or 0) / 1e6 or 1.0
    subs = []; pending = None
    for pkt in c.demux(st):
        if cancel and cancel(): raise RuntimeError("arrêté")
        if pkt.pts is None: continue
        t = float(pkt.pts * pkt.time_base)
        try: out = pkt.decode()
        except Exception: continue
        rects = []
        for o in (out or []):
            if hasattr(o, "planes") or hasattr(o, "width"): rects.append(o)
            else:
                try: rects += list(o)                    # anciennes versions : ensemble de rectangles
                except TypeError: pass
        if pending is not None and (not rects or t > pending[0]):
            if pending[1] is None: pending[1] = t
            subs.append(pending); pending = None
        if not rects: continue
        text = "\n".join(x for x in (rd.read(img) for img in (_rect_image(r) for r in rects) if img is not None) if x)
        if text:
            d = float(pkt.duration * pkt.time_base) if pkt.duration else 0
            pending = [t, (t + d) if d > 0.2 else None, text]
        if progress: progress(min(1.0, t / dur))
    if pending:
        if pending[1] is None: pending[1] = pending[0] + 3
        subs.append(pending)
    c.close()
    items = [srt.Subtitle(i + 1, datetime.timedelta(seconds=a), datetime.timedelta(seconds=min(b, a + 10)), t) for i, (a, b, t) in enumerate(subs)]
    with open(out_srt, "w", encoding="utf-8") as f: f.write(srt.compose(items))
    return len(items)


def _rect_image(r):
    """Image en niveaux de gris (texte clair sur fond noir) d'un rectangle de sous-titre PyAV (bitmap palettisé).
    La palette n'étant pas toujours fournie, les couleurs sont déduites de la forme : fond = couleur majoritaire,
    remplissage des lettres = couleur la moins en contact avec le fond, contour = les autres."""
    try:
        w, h = r.width, r.height
        if not w or not h: return None
        planes = list(r.planes) if hasattr(r, "planes") else list(r)
        raw = bytes(planes[0]); stride = len(raw) // h
        idx = np.frombuffer(raw, np.uint8)[:stride * h].reshape(h, stride)[:, :w]
        if len(planes) > 1:
            pal = np.frombuffer(bytes(planes[1]), np.uint8)
            pal = pal[:len(pal) // 4 * 4].reshape(-1, 4)          # BGRA
            if len(pal) < 256: pal = np.vstack([pal, np.zeros((256 - len(pal), 4), np.uint8)])
            rgba = pal[idx]
            return (rgba[..., :3].astype(np.float32).mean(2) * (rgba[..., 3] / 255.0)).astype(np.uint8)
        vals, counts = np.unique(idx, return_counts=True)
        bg = vals[counts.argmax()]
        fgm = idx != bg
        if not fgm.any(): return None
        pad = np.pad(idx, 1, constant_values=bg)
        near_bg = np.zeros_like(fgm)
        for dy, dx in ((0, 1), (2, 1), (1, 0), (1, 2)):
            near_bg |= pad[dy:dy + h, dx:dx + w] == bg
        out = np.zeros((h, w), np.uint8); best = None
        for v in vals:
            if v == bg: continue
            m = idx == v
            ratio = (m & near_bg).sum() / max(1, m.sum())
            if best is None or ratio < best[0]: best = (ratio, v)
        out[fgm] = 70
        out[idx == best[1]] = 255
        return out
    except Exception:
        return None
