# -*- coding: utf-8 -*-
"""
SubSync - fusion VF : prend l'image d'une version HD et la piste audio française d'une autre version
(de moindre qualité), cale la VF sur la HD (vitesse, décalage, coupures) et assemble un .mkv.

Le calage ne s'appuie pas sur les voix (langues différentes) mais sur la musique et les bruitages,
communs aux deux versions : on compare les attaques sonores (débuts de sons) dans les graves et les aigus.
"""
import os, re, json, math, time, subprocess, tempfile, shutil
import numpy as np
import subsync_engine as eng

FR_CODES = ("fre", "fra", "fr", "french", "français", "francais")
FEAT_SR = 100      # trames de caractéristiques par seconde
BANDS = [30, 80, 150, 300, 600, 1200, 2400, 4000, 6000, 8000]
USE_BANDS = [0, 1, 2, 6, 7, 8]   # graves + aigus : musique/bruitages, moins sensibles aux voix


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- sondage des fichiers

def probe(path):
    """Renvoie dict(duration, video=[...], audio=[...], subs=[...]) via PyAV."""
    import av
    c = av.open(path)
    info = dict(duration=(c.duration or 0) / 1e6, video=[], audio=[], subs=[])
    for s in c.streams:
        lang = (s.metadata.get("language") or s.language or "").lower()
        title = s.metadata.get("title", "")
        st = float(s.start_time * s.time_base) if s.start_time is not None and s.time_base else 0.0
        d = dict(index=s.index, lang=lang, title=title, start=st, codec=s.codec_context.name if s.codec_context else "")
        if s.type == "video":
            d.update(height=s.codec_context.height or 0, width=s.codec_context.width or 0,
                     fps=float(s.average_rate) if s.average_rate else 0.0)
            if d["codec"] in ("mjpeg", "png", "bmp", "gif", "webp"):  # pochette intégrée, pas la vidéo
                continue
            info["video"].append(d)
        elif s.type == "audio":
            d.update(channels=s.codec_context.channels or 2, rate=s.codec_context.sample_rate or 48000)
            info["audio"].append(d)
        elif s.type == "subtitle":
            try: d["forced"] = bool(s.disposition & av.stream.Disposition.forced)
            except Exception: d["forced"] = "forced" in (title or "").lower()
            d["forced"] = d["forced"] or any(w in (title or "").lower() for w in ("forced", "forcé"))
            info["subs"].append(d)
    c.close()
    return info


def video_info(path):
    """Caractéristiques de la vidéo principale pour l'amélioration (taille, cadence, nombre d'images…)."""
    import av
    from fractions import Fraction
    c = av.open(path)
    v = [s for s in c.streams.video if (s.codec_context.name or "") not in ("mjpeg", "png", "bmp", "gif", "webp")][0]
    rate = v.average_rate or v.guessed_rate or Fraction(25)
    rate = Fraction(rate).limit_denominator(1001)
    dur = float(v.duration * v.time_base) if v.duration else (c.duration or 0) / 1e6
    frames = v.frames or int(round(dur * float(rate)))
    W, H = v.codec_context.width, v.codec_context.height
    sar = v.sample_aspect_ratio or v.codec_context.sample_aspect_ratio
    dar = W / H * (float(sar) if sar and float(sar) > 0 else 1.0)
    st = float(v.start_time * v.time_base) if v.start_time is not None else 0.0
    subs = [s.codec_context.name if s.codec_context else "" for s in c.streams.subtitles]
    auds = [int(getattr(s.codec_context, "channels", 2) or 2) for s in c.streams.audio]
    c.close()
    return dict(width=W, height=H, fps_num=rate.numerator, fps_den=rate.denominator, frames=frames, dar=dar,
                vstart=max(0.0, st), sub_codecs=subs, audio_ch=auds, duration=dur)


def is_fr(a):
    return a["lang"] in FR_CODES or "fran" in a["title"].lower() or a["title"].upper().startswith("VF")


# codes de langue -> code ISO 639-2 normalisé
_LANG_ALIASES = {
    "eng": ("en", "eng", "english", "anglais"), "fre": ("fr", "fre", "fra", "french", "français", "francais"),
    "ger": ("de", "ger", "deu", "german", "deutsch", "allemand"), "spa": ("es", "spa", "spanish", "español", "espagnol"),
    "ita": ("it", "ita", "italian", "italiano", "italien"), "jpn": ("ja", "jpn", "japanese", "japonais"),
    "pol": ("pl", "pol", "polish", "polski", "polonais"), "rus": ("ru", "rus", "russian", "russe"),
    "por": ("pt", "por", "portuguese", "português", "portugais"), "dut": ("nl", "dut", "nld", "dutch", "néerlandais"),
    "cze": ("cs", "cze", "ces", "czech", "tchèque"), "hun": ("hu", "hun", "hungarian", "hongrois"),
    "tur": ("tr", "tur", "turkish", "turc"), "chi": ("zh", "chi", "zho", "chinese", "chinois"),
    "kor": ("ko", "kor", "korean", "coréen"),
}
_LANG_NAMES = {"eng": "anglais", "fre": "français", "ger": "allemand", "spa": "espagnol", "ita": "italien", "jpn": "japonais",
               "pol": "polonais", "rus": "russe", "por": "portugais", "dut": "néerlandais", "cze": "tchèque", "hun": "hongrois",
               "tur": "turc", "chi": "chinois", "kor": "coréen"}


# langues proposées individuellement dans l'interface ; "other" = toutes les autres
MAIN_LANGS = ("eng", "ger", "spa", "ita", "jpn")


def norm_lang(a):
    """Code de langue normalisé d'une piste (d'après l'étiquette, sinon le titre) ; "" si inconnu."""
    l = (a.get("lang") or "").lower()
    if l in ("und", "unk", "mis", "zxx"): l = ""
    for k, al in _LANG_ALIASES.items():
        if l in al: return k
    t = (a.get("title") or "").lower()
    for k, al in _LANG_ALIASES.items():
        if any(len(x) > 3 and x in t for x in al): return k
    return l


def lang_name(code):
    return _LANG_NAMES.get(code, code or "langue inconnue")


def pick_audio(info, french):
    """Choisit la piste française (french=True) ou la piste originale (première non française, de préférence anglaise)."""
    A = info["audio"]
    if not A:
        return None
    if french:
        fr = [a for a in A if is_fr(a)]
        return fr[0] if fr else A[0]
    def rank(a):
        code = norm_lang(a); t = (a.get("title") or "").lower()
        dub = any(w in t for w in ("lektor", "voice-over", "voiceover", "dub", "doublage", "commentaire", "commentary", "audio description", "audiodescription"))
        if code == "eng" and not dub: return 0
        if code == "" and not dub: return 1        # piste sans langue : très souvent la VO
        if code == "eng": return 2
        if code == "": return 3
        return 4 if not dub else 5                 # autre langue (doublage, lektor…) en dernier recours
    cand = [a for a in A if not is_fr(a)] or A
    return min(cand, key=lambda a: (rank(a), A.index(a)))


def classify_pair(p1, p2):
    """Détermine laquelle des deux vidéos est la HD (image) et laquelle est la VF (son). Renvoie (hd, vf)."""
    i1, i2 = probe(p1), probe(p2)
    h1 = max([v["height"] * v["width"] for v in i1["video"]] or [0]); h2 = max([v["height"] * v["width"] for v in i2["video"]] or [0])
    f1 = any(is_fr(a) for a in i1["audio"]); f2 = any(is_fr(a) for a in i2["audio"])
    if h1 != h2:
        return (p1, p2) if h1 > h2 else (p2, p1)
    if f1 != f2:
        return (p2, p1) if f1 else (p1, p2)
    s1, s2 = os.path.getsize(p1), os.path.getsize(p2)
    return (p1, p2) if s1 >= s2 else (p2, p1)


# --------------------------------------------------------------------------- audio

def _ff(args, **kw):
    p = subprocess.run([eng.ffmpeg_exe(), "-v", "error", "-nostdin", "-y"] + args, capture_output=True, **eng._noconsole(), **kw)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg : " + p.stderr.decode(errors="replace")[-600:])
    return p


def decode_raw(path, stream_index, out, rate, channels, extra_af=None):
    """Décode une piste en PCM 16 bits brut (fichier .raw)."""
    af = ["-af", extra_af] if extra_af else []
    _ff(["-i", path, "-map", "0:%d" % stream_index, "-vn", "-sn"] + af + ["-ar", str(rate), "-ac", str(channels), "-f", "s16le", out])
    return np.memmap(out, dtype=np.int16, mode="r").reshape(-1, channels)


def features(mono16k):
    """Attaques sonores par bande (100 trames/s), normalisées."""
    x = mono16k.astype(np.float32) / 32768.0
    hop, n = 160, 1024
    nfr = max(0, (len(x) - n) // hop)
    win = np.hanning(n).astype(np.float32)
    freqs = np.fft.rfftfreq(n, 1 / 16000)
    masks = [(freqs >= a) & (freqs < b) for a, b in zip(BANDS, BANDS[1:])]
    out = np.zeros((nfr, len(masks)), np.float32)
    for i in range(0, nfr, 4000):
        idx = np.arange(i, min(nfr, i + 4000))[:, None] * hop + np.arange(n)[None, :]
        S = np.abs(np.fft.rfft(x[idx] * win, axis=1)) ** 2
        out[i:i + len(idx)] = np.stack([S[:, m].sum(1) for m in masks], 1)
    L = np.log(out + 1e-8)
    D = np.maximum(0, np.diff(L, axis=0, prepend=L[:1]))
    D = (D - D.mean(0)) / (D.std(0) + 1e-6)
    f = D[:, USE_BANDS].sum(1)
    return (f - f.mean()) / (f.std() + 1e-6)


def _xcorr(a, b):
    """c[o] : corrélation de b décalé de o (b[t+o] ~ a[t]). Renvoie (c, lags)."""
    n = len(a) + len(b); N = 1 << int(np.ceil(np.log2(n)))
    c = np.fft.irfft(np.conj(np.fft.rfft(a, N)) * np.fft.rfft(b, N), N)
    lags = np.arange(N); lags[lags > N // 2] -= N
    return c, lags


def detect_speed(fe, fv):
    """Rapport de vitesse VF/HD (ex. 25/23.976 pour une VF PAL). Renvoie (ratio, z)."""
    res = []
    for r in eng.SPEED_CANDIDATES:
        # fv_r[t] = fv[t * r] : VF ramenée sur la durée de la HD
        t = np.arange(int(len(fv) / r)) * r
        fvr = np.interp(t, np.arange(len(fv)), fv) if abs(r - 1) > 1e-9 else fv
        c, l = _xcorr(fe, fvr)
        k = int(np.argmax(c)); z = (c[k] - c.mean()) / (c.std() + 1e-9)
        res.append((z, r))
    res.sort(reverse=True)
    z1 = next(z for z, r in res if abs(r - 1) < 1e-9)
    if abs(res[0][1] - 1) > 1e-9 and res[0][0] > 1.15 * z1:
        return res[0][1], res[0][0]
    return 1.0, z1


def vad100(mono16k, cancel=None):
    """Détection de voix (Silero) à 100 trames/s sur un signal mono 16 kHz int16."""
    x = mono16k.astype(np.float32) / 32768.0
    sess = eng._silero(); n = len(x) // 512
    st = np.zeros((2, 1, 128), np.float32); ctx = np.zeros(64, np.float32); pr = np.zeros(n, np.float32)
    sr = np.array(16000, dtype=np.int64)
    for i in range(n):
        if cancel and i % 5000 == 0 and cancel(): raise eng.Cancelled()
        ch = x[i * 512:(i + 1) * 512]
        o, st = sess.run(None, {"input": np.concatenate([ctx, ch])[None, :], "state": st, "sr": sr}); pr[i] = o[0, 0]; ctx = ch[-64:]
    t = np.arange(int(len(x) / 160)) / 100.0
    return pr[np.minimum((t * 16000 / 512).astype(int), max(0, n - 1))] > 0.5


def _vagree(ve, vv, o, n=None):
    """+1 si les deux versions parlent, -1 si une seule parle, 0 sinon (chronologie HD, longueur n)."""
    N = n or len(ve)
    e = np.zeros(N, bool); e[:min(N, len(ve))] = ve[:N]
    k = int(round(o * FEAT_SR)); j = np.arange(N) + k; m = (j >= 0) & (j < len(vv))
    v = np.zeros(N, bool); v[m] = vv[j[m]]
    return np.where(e & v, 1.0, np.where(e ^ v, -1.0, 0.0)).astype(np.float32)


# --------------------------------------------------------------------------- calage par morceaux

def local_offsets(fe, fv, win=40, step=10, search=40):
    W, S, R = win * FEAT_SR, step * FEAT_SR, search * FEAT_SR
    out = []
    for t in range(0, max(1, len(fe) - W), S):
        a = fe[t:t + W] - fe[t:t + W].mean()
        lo = max(0, t - R); hi = min(len(fv) - W, t + R)
        if hi <= lo:
            continue
        b = fv[lo:hi + W]
        c = np.correlate(b - b.mean(), a, "valid")
        k = int(np.argmax(c)); z = (c[k] - c.mean()) / (c.std() + 1e-9)
        out.append((t / FEAT_SR, (lo + k - t) / FEAT_SR, float(z)))
    return out


def _prod(fe, fv, o):
    k = int(round(o * FEAT_SR)); N = len(fe); p = np.zeros(N, np.float32)
    j = np.arange(N) + k; m = (j >= 0) & (j < len(fv)); p[m] = fe[m] * fv[j[m]]
    return p


def plan_segments(fe, fv, log, ve=None, vv=None):
    """Segments [(debut, fin, décalage ou None)] dans la chronologie HD : VF(t+décalage) = HD(t)."""
    loc = local_offsets(fe, fv)
    zs = np.array([z for *_, z in loc]) if loc else np.array([0])
    thr = max(6.0, float(np.percentile(zs, 40)))
    vals = sorted(o for _, o, z in loc if z >= thr)
    cands = []
    for o in vals:  # regroupement des décalages proches (<60 ms)
        if cands and abs(o - cands[-1][-1]) <= 0.06: cands[-1].append(o)
        else: cands.append([o])
    cands = [float(np.median(c)) for c in cands if len(c) >= 2 or len(vals) < 6]
    if not cands:
        raise RuntimeError("Impossible de caler les deux versions (bandes son trop différentes).")
    N = len(fe); T = N // FEAT_SR; W = 6 * FEAT_SR
    def score(o):
        cs = np.concatenate([[0], np.cumsum(_prod(fe, fv, o))]); sc = np.zeros(T)
        for t in range(T):
            a = max(0, t * FEAT_SR - W // 2); b = min(N, t * FEAT_SR + W // 2); sc[t] = (cs[b] - cs[a]) / max(1, b - a)
        return sc
    S = np.stack([score(o) for o in cands] + [np.full(T, -0.02)])
    labels = cands + [None]; K = len(labels); P = 2.0
    # coût de changement : un grand saut de décalage doit être nettement justifié
    cost = np.zeros((K, K))
    for i in range(K):
        for j in range(K):
            if i == j: continue
            if labels[i] is None or labels[j] is None: cost[i, j] = 2 * P
            else: cost[i, j] = P + 0.25 * abs(labels[i] - labels[j])
    D = np.zeros((T, K)); B = np.zeros((T, K), int); D[0] = S[:, 0]
    for t in range(1, T):
        M = D[t - 1][:, None] - cost          # M[i, j] : venir de i pour aller en j
        B[t] = np.argmax(M, 0); D[t] = M[B[t], np.arange(K)] + S[:, t]
    path = [int(np.argmax(D[-1]))]
    for t in range(T - 1, 0, -1): path.append(B[t, path[-1]])
    path = path[::-1]
    segs = []
    for t, k in enumerate(path):
        if segs and segs[-1][2] == k: segs[-1][1] = t + 1
        else: segs.append([t, t + 1, k])
    # segments très courts (< 8 s) absorbés par leurs voisins
    i = 0
    while len(segs) > 1 and i < len(segs):
        if segs[i][1] - segs[i][0] < 8:
            if i > 0: segs[i - 1][1] = segs[i][1]
            else: segs[i + 1][0] = segs[i][0]
            del segs[i]
            j = 1
            while j < len(segs):
                if segs[j][2] == segs[j - 1][2]: segs[j - 1][1] = segs[j][1]; del segs[j]
                else: j += 1
            i = 0
        else:
            i += 1
    # affinage du décalage de chaque segment (±0,10 s au 1/100 s)
    plan = []
    for a, b, k in segs:
        o = labels[k]
        if o is not None:
            aa, bb = (a + 2, b - 2) if b - a > 8 else (a, b)
            best = max((_prod(fe, fv, o + d)[aa * FEAT_SR:bb * FEAT_SR].mean(), round(o + d, 2)) for d in np.arange(-0.10, 0.101, 0.01))
            o = best[1]
        plan.append([float(a), float(b), o])
    # frontières précises (±40 s, sans déborder sur le coeur des segments voisins) : point de bascule optimal
    for i in range(len(plan) - 1):
        o1, o2 = plan[i][2], plan[i + 1][2]
        b1 = plan[i][1]
        if o1 is None or o2 is None:
            continue
        p1, p2 = _prod(fe, fv, o1), _prod(fe, fv, o2)
        if ve is not None and vv is not None:
            # concordance des voix : décisive dans les scènes dialoguées sans musique
            sd = max(1e-6, float(np.std(p1)))
            p1 = p1 / sd + 1.5 * _vagree(ve, vv, o1, len(p1)); p2 = p2 / sd + 1.5 * _vagree(ve, vv, o2, len(p2))
        lo = int(max(plan[i][0] + 2, b1 - 40) * FEAT_SR); hi = int(min(plan[i + 1][1] - 2, b1 + 40) * FEAT_SR)
        if hi - lo < 100: continue
        c1 = np.concatenate([[0], np.cumsum(p1[lo:hi])]); c2 = np.concatenate([[0], np.cumsum(p2[lo:hi])])
        tot = c1 + (c2[-1] - c2)
        s = lo + int(np.argmax(tot[50:-50])) + 50
        plan[i][1] = plan[i + 1][0] = s / FEAT_SR
    plan[-1][1] = len(fe) / FEAT_SR
    for a, b, o in plan:
        log("    %7.2f – %7.2f s : %s" % (a, b, ("décalage %+.2f s" % o) if o is not None else "pas d'équivalent dans la VF (son original)"))
    return plan


def build_pieces(plan, dub_len, hd_len):
    """Liste de (source 'vf'|'vo', debut, fin) dans la chronologie de chaque source."""
    pieces = []; last_vf_end = None
    for a, b, o in plan:
        b = min(b, hd_len)
        if b <= a: continue
        if o is None:
            pieces.append(("vo", a, b)); last_vf_end = None; continue
        s = a
        if s + o < 0:                              # début de la HD sans équivalent VF
            e = min(b, -o); pieces.append(("vo", s, e)); s = e
        if last_vf_end is not None and s + o < last_vf_end - 0.005:   # images en plus dans la HD
            fill = min(b - s, last_vf_end - (s + o)); pieces.append(("vo", s, s + fill)); s += fill
        if s >= b: continue
        e_vf = min(b + o, dub_len)
        if e_vf > s + o:
            pieces.append(("vf", s + o, e_vf)); last_vf_end = e_vf
        rest = (b + o) - e_vf
        if rest > 0.005:
            pieces.append(("vo", b - rest, b))
    return pieces


# --------------------------------------------------------------------------- sous-titres de la VF

MKV_SUB_CODECS = ("subrip", "ass", "ssa", "dvd_subtitle", "hdmv_pgs_subtitle", "dvb_subtitle", "webvtt")


def piece_map(pieces, pad):
    """Fonction temps VF corrigé -> temps du fichier final (None si le passage n'existe pas dans la HD)."""
    segs = []; T = pad
    for src, a, b in pieces:
        if src == "vf": segs.append((a, b, T))
        T += b - a
    def f(t):
        for a, b, T0 in segs:
            if a - 0.05 <= t < b: return T0 + max(0.0, t - a)
        return None
    return f


def retime_subs(vf_path, streams, tmap, ratio, vf_audio_start, out_path, log=print):
    """Copie les pistes de sous-titres choisies de la VF (texte ou image) en recalant chaque
    sous-titre sur la HD. Renvoie le nombre de sous-titres gardés par piste."""
    import av
    inp = av.open(vf_path); out = av.open(out_path, "w", format="matroska")
    ist = {s.index: s for s in inp.streams if s.index in [x["index"] for x in streams]}
    ost = {}
    for d in streams:
        s = ist[d["index"]]
        o = out.add_stream_from_template(s)
        for k, v in s.metadata.items(): o.metadata[k] = v
        ost[d["index"]] = o
    kept = {i: 0 for i in ost}; lost = {i: 0 for i in ost}
    for pk in inp.demux(list(ist.values())):
        if pk.pts is None or pk.size == 0: continue
        si = pk.stream.index; tb = pk.stream.time_base
        t = float(pk.pts * tb) - vf_audio_start
        new = tmap(t / ratio)
        if new is None: lost[si] += 1; continue
        pk.stream = ost[si]
        pk.time_base = tb
        pk.pts = pk.dts = max(0, int(round(new / tb)))
        if pk.duration: pk.duration = int(round(pk.duration / ratio))
        out.mux(pk); kept[si] += 1
    out.close(); inp.close()
    return kept, lost


# --------------------------------------------------------------------------- tâche complète

def merge_dub(hd_path, vf_path, out_dir=None, keep_langs=("eng",), log=print, progress=None, cancel=None):
    """keep_langs : langues des pistes audio de la HD à garder en plus de la VF (codes ISO 639-2 :
    "eng", "ger"…), "all" pour tout garder, () pour ne garder que la VF.
    "orig" dans la liste = garder la piste originale (celle servant au calage) quelle que soit sa langue."""
    def chk():
        if cancel and cancel(): raise eng.Cancelled()
    def prog(f):
        if progress: progress(max(0, min(1, f)))
    t0 = time.time()
    log("Fusion VF : image de « %s » + son français de « %s »" % (os.path.basename(hd_path), os.path.basename(vf_path)))
    ihd, ivf = probe(hd_path), probe(vf_path)
    if not ihd["video"]: raise RuntimeError("Pas de piste vidéo dans %s" % os.path.basename(hd_path))
    a_vo = pick_audio(ihd, french=False); a_vf = pick_audio(ivf, french=True)
    if not a_vo or not a_vf: raise RuntimeError("Piste audio introuvable.")
    if any(is_fr(a) for a in ihd["audio"]):
        log("  (la version HD contient déjà une piste française — elle sera conservée en plus)")
    log("  pistes audio de la HD : " + ", ".join("n°%d %s" % (a["index"], lang_name(norm_lang(a))) for a in ihd["audio"]))
    log("  piste originale : n°%d (%s, %d canaux) · piste française : n°%d (%s, %d canaux)" % (
        a_vo["index"], a_vo["lang"] or "?", a_vo["channels"], a_vf["index"], a_vf["lang"] or "?", a_vf["channels"]))
    tmp = tempfile.mkdtemp(prefix="subsync_fusion_")
    try:
        # 1) caractéristiques sonores des deux versions
        log("  analyse des deux bandes son…")
        mo = decode_raw(hd_path, a_vo["index"], os.path.join(tmp, "vo16.raw"), 16000, 1)[:, 0]; chk(); prog(0.08)
        mf = decode_raw(vf_path, a_vf["index"], os.path.join(tmp, "vf16.raw"), 16000, 1)[:, 0]; chk(); prog(0.16)
        fe = features(mo); fv = features(mf); chk(); prog(0.22)
        same = same_episode(_decim(fe), _decim(fv))
        log("  même épisode ? %.0f %% des passages se retrouvent dans les deux versions" % (same * 100))
        if same < MATCH_MIN:
            log("  ressemblance faible (mixage différent ?) : le calage dira si c'est bien le même épisode")
        log("  détection des voix (pour placer précisément les coupures)…")
        ve = vad100(mo, cancel); vv = vad100(mf, cancel); chk(); prog(0.3)
        del mo, mf
        # 2) vitesse
        ratio, z = detect_speed(fe, fv)
        af = None
        if abs(ratio - 1) > 1e-6:
            # VF plus rapide (PAL) : on la ralentit en restaurant aussi la hauteur des voix
            af = "asetrate=%d,aresample=48000" % round(a_vf["rate"] * ratio)
            log("  vitesse : la VF est %s de %.1f %% (corrigé, hauteur des voix restaurée)" % ("plus rapide" if ratio < 1 else "plus lente", abs(1 / ratio - 1) * 100))
            mf = decode_raw(vf_path, a_vf["index"], os.path.join(tmp, "vf16.raw"), 16000, 1,
                            extra_af="asetrate=%d,aresample=16000" % round(a_vf["rate"] * ratio))[:, 0]
            fv = features(mf); vv = vad100(mf, cancel); del mf
        else:
            log("  vitesse identique (pic de corrélation %.0f)" % z)
        chk(); prog(0.4)
        # 3) calage par morceaux
        log("  recherche des décalages et des coupures…")
        plan = plan_segments(fe, fv, log, ve, vv); chk(); prog(0.55)
        # 4) construction de la piste VF calée
        C = max(1, min(a_vf["channels"], 6))
        log("  construction de la piste française calée (%d canaux)…" % C)
        VF = decode_raw(vf_path, a_vf["index"], os.path.join(tmp, "vf48.raw"), 48000, C,
                        extra_af=(af if af else None)); chk(); prog(0.65)
        VO = decode_raw(hd_path, a_vo["index"], os.path.join(tmp, "vo48.raw"), 48000, C); chk(); prog(0.72)
        hd_len = len(fe) / FEAT_SR
        pieces = build_pieces(plan, len(VF) / 48000.0, min(hd_len, len(VO) / 48000.0))
        n_vo = sum(b - a for s, a, b in pieces if s == "vo")
        out_raw = os.path.join(tmp, "vf_calee.raw")
        fade = int(0.015 * 48000)
        pad = int(round(a_vo["start"] * 48000))   # la piste originale démarre parfois après l'image
        with open(out_raw, "wb") as f:
            if pad > 0: f.write(np.zeros((pad, C), np.int16).tobytes())
            for src, a, b in pieces:
                arr = VF if src == "vf" else VO
                i, j = int(round(a * 48000)), int(round(b * 48000))
                seg = np.array(arr[max(0, i):min(len(arr), j)], dtype=np.float32)
                need = j - i
                if len(seg) < need: seg = np.concatenate([seg, np.zeros((need - len(seg), C), np.float32)])
                if len(seg) > 2 * fade:
                    ramp = np.linspace(0, 1, fade, dtype=np.float32)[:, None]
                    seg[:fade] *= ramp; seg[-fade:] *= ramp[::-1]
                f.write(np.clip(seg, -32768, 32767).astype(np.int16).tobytes())
        chk(); prog(0.8)
        # 5) vérification indépendante : la piste construite doit coïncider avec la VO partout
        log("  vérification du calage…")
        chk_raw = os.path.join(tmp, "chk16.raw")
        _ff(["-f", "s16le", "-ar", "48000", "-ac", str(C), "-ss", "%.3f" % (pad / 48000.0), "-i", out_raw,
             "-ac", "1", "-ar", "16000", "-f", "s16le", chk_raw])
        fb = features(np.memmap(chk_raw, dtype=np.int16, mode="r"))
        chkp = local_offsets(fe, fb, win=40, step=60, search=3)
        good = [abs(o) <= 0.04 for t, o, z in chkp if z >= 6]
        pct = 100.0 * sum(good) / len(good) if good else 0.0
        log("  %.0f %% des points de contrôle calés à moins de 40 ms (%d points fiables)" % (pct, len(good)))
        if same < MATCH_MIN and pct < 40:
            raise RuntimeError("Ces deux vidéos ne semblent pas être le même épisode (%.0f %% de passages communs, calage impossible). "
                               "Vérifiez l'association (bouton « Associer une vidéo… »)." % (same * 100))
        # 6) assemblage
        enc = os.path.join(tmp, "vf.ac3")
        _ff(["-f", "s16le", "-ar", "48000", "-ac", str(C), "-i", out_raw, "-c:a", "ac3", "-b:a", "448k" if C > 2 else "192k", enc])
        chk(); prog(0.88)
        d = out_dir or os.path.dirname(hd_path)
        os.makedirs(d, exist_ok=True)
        out = os.path.join(d, os.path.splitext(os.path.basename(hd_path))[0] + " - VF.mkv")
        if os.path.abspath(out) in (os.path.abspath(hd_path), os.path.abspath(vf_path)):
            out = out[:-4] + " (2).mkv"
        # sous-titres français / anglais de la VF, recalés sur la HD
        vf_subs = [x for x in ivf["subs"] if norm_lang(x) in ("fre", "eng", "")]
        sub_file = None; vf_sub_ok = []
        if vf_subs:
            sub_file = os.path.join(tmp, "st_vf.mkv")
            try:
                src = vf_path; subs_in = vf_subs
                if any(x["codec"] not in MKV_SUB_CODECS for x in vf_subs):
                    # sous-titres texte d'un MP4 (mov_text…) : conversion préalable en SRT, temps conservés
                    src = os.path.join(tmp, "st_src.mkv"); cv = ["-copyts", "-i", vf_path]
                    for x in vf_subs: cv += ["-map", "0:%d" % x["index"]]
                    for k, x in enumerate(vf_subs): cv += ["-c:s:%d" % k, "copy" if x["codec"] in MKV_SUB_CODECS else "srt"]
                    _ff(cv + [src]); subs_in = [dict(x, index=k) for k, x in enumerate(vf_subs)]
                kept_n, lost_n = retime_subs(src, subs_in, piece_map(pieces, pad / 48000.0), ratio, a_vf["start"], sub_file, log)
                kept_n = {vf_subs[k]["index"]: kept_n.get(y["index"], 0) for k, y in enumerate(subs_in)}
                lost_n = {vf_subs[k]["index"]: lost_n.get(y["index"], 0) for k, y in enumerate(subs_in)}
                for x in vf_subs:
                    if kept_n.get(x["index"]):
                        vf_sub_ok.append(x)
                        log("  sous-titres de la VF gardés : %s%s (%d répliques%s)" % (
                            lang_name(norm_lang(x)), " (forcés)" if x["forced"] else "", kept_n[x["index"]],
                            ", %d sans image correspondante retirées" % lost_n[x["index"]] if lost_n[x["index"]] else ""))
            except Exception as e:
                log("  sous-titres de la VF non récupérables (%s)" % e); sub_file = None; vf_sub_ok = []
        args = ["-i", hd_path, "-i", enc]
        if vf_sub_ok: args += ["-i", sub_file]
        args += ["-map", "0:%d" % ihd["video"][0]["index"], "-map", "1:a:0"]
        keep_all = keep_langs == "all"
        want = set() if keep_all else set(keep_langs or ())
        kept, dropped = [], []
        for a in ihd["audio"]:
            code = norm_lang(a)
            if a["index"] == a_vo["index"] or not code:
                # piste sans étiquette de langue : presque toujours la VO (anglaise) -> suit la case « Anglais »
                ok = keep_all or "orig" in want or (code or "eng") in want
            else:
                ok = (keep_all or code in want or code == "fre"
                      or ("other" in want and code not in MAIN_LANGS))   # une VF déjà présente dans la HD est gardée
            (kept if ok else dropped).append(a)
        for a in kept: args += ["-map", "0:%d" % a["index"]]
        # sous-titres de la HD : français + langues cochées (+ sans étiquette)
        hd_subs = [x for x in ihd["subs"] if keep_all or norm_lang(x) in ("fre", "") or norm_lang(x) in want
                   or ("other" in want and norm_lang(x) not in MAIN_LANGS)]
        drop_subs = [x for x in ihd["subs"] if x not in hd_subs]
        all_subs = [(0, x) for x in hd_subs] + [(2, x) for x in vf_sub_ok]
        all_subs.sort(key=lambda p: ({"fre": 0, "": 1, "eng": 2}.get(norm_lang(p[1]), 3), p[0]))
        for src, x in all_subs:
            args += ["-map", "%d:%d" % (src, x["index"] if src == 0 else vf_subs.index(x))]
        if drop_subs: log("  sous-titres de la HD retirés : " + ", ".join(lang_name(norm_lang(x)) for x in drop_subs))
        args += ["-map", "0:t?", "-c", "copy",
                 "-metadata:s:a:0", "language=fre", "-metadata:s:a:0", "title=Français (VF)", "-disposition:a:0", "default"]
        for k, a in enumerate(kept, start=1):
            args += ["-disposition:a:%d" % k, "0"]
            if a["index"] == a_vo["index"]:
                code = norm_lang(a) or "eng"
                args += ["-metadata:s:a:%d" % k, "language=%s" % code,
                         "-metadata:s:a:%d" % k, "title=%s (VO)" % lang_name(code).capitalize()]
        log("  pistes audio : VF" + "".join(" + " + lang_name(norm_lang(a) or ("eng" if a["index"] == a_vo["index"] else "")) for a in kept)
            + ((" · retirées : " + ", ".join(lang_name(norm_lang(a)) for a in dropped)) if dropped else ""))
        for k, (src, x) in enumerate(all_subs):
            args += ["-disposition:s:%d" % k, "forced" if x["forced"] else "0"]
            if src == 2:
                code = norm_lang(x) or "fre"
                ttl = x["title"] or lang_name(code).capitalize()
                args += ["-metadata:s:s:%d" % k, "language=%s" % code, "-metadata:s:s:%d" % k, "title=" + (ttl if "VF" in ttl else ttl + " (version VF)")]
        args += ["-default_mode", "infer_no_subs", "-max_interleave_delta", "0", out]
        log("  assemblage du fichier final (copie de la vidéo, sans réencodage)…")
        _ff(args); prog(1.0)
        lvl = "ok" if pct >= 85 else ("warn" if pct >= 60 else "bad")
        summ = "VF calée : %d morceau(x), %s%.0f %% de contrôles OK%s" % (
            sum(1 for s, *_ in pieces if s == "vf"),
            ("vitesse corrigée, " if abs(ratio - 1) > 1e-6 else ""), pct,
            (", %.1f s sans VF (son original)" % n_vo) if n_vo > 0.05 else "")
        log("  " + summ + " — %.0f s" % (time.time() - t0))
        return [eng.Result(hd_path, out, "fusion", pct, lvl, summ, dict(plan=plan, ratio=ratio, pieces=pieces), [])]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- reconnaissance par le contenu
FP_DECIM = 4          # empreintes à 25 trames/s
MATCH_MIN = 0.12      # part minimale de passages communs pour dire « même épisode » (différents : < 0,08)


def _decim(f, D=FP_DECIM):
    return f[:len(f) // D * D].reshape(-1, D, *f.shape[1:]).mean(1)


def fingerprint(path, cancel=None):
    """Empreinte sonore (attaques par bande, 25 trames/s) mise en cache à côté des autres caches."""
    cache = eng._cache_key(path, "fp1") + ".npy"
    if os.path.exists(cache):
        try: return np.load(cache)
        except Exception: pass
    info = probe(path); a = pick_audio(info, french=False)
    if not a: raise RuntimeError("pas de piste audio")
    tmp = tempfile.mkdtemp(prefix="subsync_fp_")
    try:
        x = decode_raw(path, a["index"], os.path.join(tmp, "a.raw"), 16000, 1)[:, 0]
        if cancel and cancel(): raise eng.Cancelled()
        fp = _decim(features(x)).astype(np.float32); del x
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    try: np.save(cache, fp)
    except Exception: pass
    return fp


def match_score(fa, fb, chunk=60):
    """Part (0-1) des minutes de A retrouvées dans B avec un décalage cohérent, et vitesse correspondante.
    Même épisode : 0,4 à 0,9 ; épisodes différents d'une même série (seul le générique est commun) : < 0,08."""
    SR = FEAT_SR / FP_DECIM; W = int(chunk * SR)
    if len(fa) < 2 * W or len(fb) < W: W = max(int(10 * SR), min(len(fa), len(fb)) // 4)
    best = (0.0, 1.0)
    for r in eng.SPEED_CANDIDATES:
        t = np.arange(int(len(fb) / r)) * r
        fbr = np.interp(t, np.arange(len(fb)), fb) if abs(r - 1) > 1e-9 else fb
        N = 1 << int(np.ceil(np.log2(len(fbr) + W))); FB = np.fft.rfft(fbr, N)
        offs = []; n = 0
        for st in range(0, len(fa) - W + 1, W):
            a = fa[st:st + W]
            if a.std() < 1e-6: continue
            c = np.fft.irfft(np.conj(np.fft.rfft(a, N)) * FB, N)
            k = int(np.argmax(c)); z = (c[k] - c.mean()) / (c.std() + 1e-9); n += 1
            if z >= 6: offs.append((k if k <= N // 2 else k - N) - st)
        good = 0
        if offs:
            med = np.median(offs); good = sum(abs(o - med) < 60 * SR for o in offs)
        f = good / max(n, 1)
        if f > best[0]: best = (f, r)
    return best


def same_episode(fa, fb):
    """Score symétrique 0-1 (le calcul dépend du sens quand les vitesses diffèrent)."""
    return max(match_score(fa, fb)[0], match_score(fb, fa)[0])


def content_pairs(videos, log=print, progress=None, cancel=None):
    """Associe des vidéos aux noms différents en comparant leur bande son. Renvoie ([(hd, vf, score)], restantes)."""
    fps = {}
    for k, v in enumerate(videos):
        if cancel and cancel(): raise eng.Cancelled()
        log("  empreinte sonore %d/%d : %s" % (k + 1, len(videos), os.path.basename(v)))
        try: fps[v] = fingerprint(v, cancel)
        except eng.Cancelled: raise
        except Exception as e: log("    illisible (%s)" % e)
        if progress: progress(0.6 * (k + 1) / len(videos))
    vs = list(fps); sc = []
    tot = max(1, len(vs) * (len(vs) - 1) // 2); done = 0
    for i in range(len(vs)):
        for j in range(i + 1, len(vs)):
            if cancel and cancel(): raise eng.Cancelled()
            f = same_episode(fps[vs[i]], fps[vs[j]]); sc.append((f, vs[i], vs[j])); done += 1
            if progress: progress(0.6 + 0.4 * done / tot)
    sc.sort(reverse=True); used = set(); pairs = []
    for f, a, b in sc:
        if f < MATCH_MIN: break
        if a in used or b in used: continue
        used |= {a, b}
        try: hd, vf = classify_pair(a, b)
        except Exception: hd, vf = a, b
        pairs.append((hd, vf, f))
    return pairs, [v for v in videos if v not in used]


def pair_videos(videos):
    """Regroupe les vidéos par épisode et renvoie [(hd, vf)] + vidéos non appariées."""
    groups = {}; loose = []
    for v in videos:
        k = eng.episode_keys(v)
        if k: groups.setdefault((k[0], k[1][0]), []).append(v)
        else: loose.append(v)
    pairs = []; left = []
    for k, vs in sorted(groups.items()):
        if len(vs) == 2:
            pairs.append(classify_pair(*vs))
        else:
            left += vs
    if len(loose) == 2 and not pairs and not left:
        pairs.append(classify_pair(*loose)); loose = []
    return pairs, left + loose


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="SubSync - fusion VF")
    ap.add_argument("hd"); ap.add_argument("vf"); ap.add_argument("--out-dir")
    a = ap.parse_args()
    for r in merge_dub(a.hd, a.vf, a.out_dir):
        print("=>", r.out_path, "|", r.level, "|", r.summary)
