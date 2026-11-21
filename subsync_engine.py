# -*- coding: utf-8 -*-
"""
SubSync - moteur de synchronisation automatique de sous-titres (.srt) sur l'audio.

Deux méthodes :
  * simple  : détection de voix (Silero) -> vitesse (25/23.976...) -> alass
              (décalage + coupures). Rapide, couvre la grande majorité des cas.
  * avancée : transcription (Whisper) + traduction des sous-titres (Opus-MT)
              -> appariement réplique par réplique, scènes déplacées ou
              supprimées, un .srt réparti sur plusieurs épisodes.
"""
import os, re, sys, json, math, time, hashlib, subprocess, tempfile, shutil, unicodedata
from datetime import timedelta as T
import numpy as np
import srt

APP_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(APP_DIR, "models")
CACHE_DIR = os.path.join(APP_DIR, "cache")
BIN_DIR = os.path.join(APP_DIR, "bin")

VIDEO_EXT = (".mkv", ".mka", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".m2ts", ".webm",
             ".wmv", ".mpg", ".mpeg", ".mp3", ".aac", ".ac3", ".flac", ".wav", ".m4a", ".ogg", ".opus")
SR = 100  # trames de détection de voix par seconde

# rapports de vitesse usuels (NTSC film / PAL / 24 / 30)
_FPS = [23.976, 24.0, 25.0, 29.97, 30.0]
SPEED_CANDIDATES = sorted({round(a / b, 6) for a in _FPS for b in _FPS if 0.75 < a / b < 1.34})


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- outils

def _noconsole():
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return dict(startupinfo=si, creationflags=0x08000000)
    return {}


def ffmpeg_exe():
    local = os.path.join(BIN_DIR, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if os.path.exists(local):
        return local
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg") or "ffmpeg"


def alass_exe():
    for n in ("alass-cli.exe", "alass.exe", "alass-cli", "alass"):
        p = os.path.join(BIN_DIR, n)
        if os.path.exists(p):
            return p
    return shutil.which("alass-cli") or shutil.which("alass")


def _cache_key(path, tag):
    st = os.stat(path)
    h = hashlib.sha1(("%s|%d|%d|%s" % (os.path.abspath(path), st.st_size, int(st.st_mtime), tag)).encode()).hexdigest()[:16]
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, h)


def load_audio(path, log=None):
    """Décode l'audio en mono 16 kHz float32."""
    cmd = [ffmpeg_exe(), "-v", "error", "-nostdin", "-i", path, "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"]
    p = subprocess.run(cmd, capture_output=True, **_noconsole())
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError("Impossible de lire l'audio de %s :\n%s" % (os.path.basename(path), p.stderr.decode(errors="replace")[-500:]))
    return np.frombuffer(p.stdout, np.int16).astype(np.float32) / 32768.0


# --------------------------------------------------------------------------- détection de voix

_vad_session = None


def _silero():
    global _vad_session
    if _vad_session is None:
        import onnxruntime as ort
        m = os.path.join(MODELS_DIR, "silero_vad.onnx")
        so = ort.SessionOptions(); so.inter_op_num_threads = 1; so.intra_op_num_threads = 1
        _vad_session = ort.InferenceSession(m, sess_options=so, providers=["CPUExecutionProvider"])
    return _vad_session


def speech_probs(path, audio=None, log=None, progress=None, cancel=None):
    """Probabilité de parole à 100 trames/s (mise en cache)."""
    cache = _cache_key(path, "silero1") + ".npy"
    if os.path.exists(cache):
        return np.load(cache)
    if audio is None:
        audio = load_audio(path)
    s = _silero()
    n = len(audio) // 512
    state = np.zeros((2, 1, 128), np.float32); ctx = np.zeros(64, np.float32)
    probs = np.zeros(n, np.float32); sr = np.array(16000, dtype=np.int64)
    for i in range(n):
        if cancel and i % 2000 == 0 and cancel():
            raise Cancelled()
        ch = audio[i * 512:(i + 1) * 512]
        out, state = s.run(None, {"input": np.concatenate([ctx, ch])[None, :], "state": state, "sr": sr})
        probs[i] = out[0, 0]; ctx = ch[-64:]
        if progress and i % 5000 == 0:
            progress(i / max(1, n))
    t = np.arange(int(len(audio) / 160)) / 100.0
    idx = np.minimum((t * 16000 / 512).astype(int), n - 1)
    p100 = probs[idx]
    np.save(cache, p100)
    return p100


# --------------------------------------------------------------------------- sous-titres

def read_srt(path):
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            txt = raw.decode(enc); break
        except UnicodeDecodeError:
            continue
    txt = txt.replace("\r\n", "\n").replace("\r", "\n")
    try:
        subs = list(srt.parse(txt, ignore_errors=True))
    except TypeError:
        subs = list(srt.parse(txt))
    subs = [s for s in subs if s.end > s.start]
    for i, s in enumerate(subs):
        s.index = i + 1
    return subs


def write_srt(path, items):
    """items: liste de (debut_s, fin_s, texte)."""
    items = sorted(items, key=lambda x: x[0])
    out = []
    for i, (a, b, c) in enumerate(items):
        a = max(0.0, a); b = max(a + 0.2, b)
        out.append(srt.Subtitle(i + 1, T(seconds=a), T(seconds=b), c))
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(srt.compose(out))


def _times(subs):
    st = np.array([x.start.total_seconds() for x in subs]); en = np.array([x.end.total_seconds() for x in subs])
    return st, en


# --------------------------------------------------------------------------- mesures

def speed_scan(vad, subs, candidates=SPEED_CANDIDATES):
    """Corrélation globale voix/sous-titres pour chaque vitesse. Renvoie [(z, vitesse, décalage)] triés."""
    p = vad[: len(vad) // 2 * 2].reshape(-1, 2).mean(1)  # 50 Hz
    v = (p > 0.5) * 2.0 - 1
    st, en = _times(subs)
    res = []
    for f in candidates:
        L = int(en.max() * f * 50) + 1
        s = np.zeros(L)
        for a, b in zip(st, en):
            s[int(a * f * 50):int(b * f * 50)] = 1
        s -= s.mean()
        F = 1 << int(math.ceil(math.log2(len(v) + L)))
        c = np.fft.irfft(np.fft.rfft(v, F) * np.conj(np.fft.rfft(s, F)), F)
        lags = np.arange(F); lags[lags > F // 2] -= F
        k = int(np.argmax(c))
        res.append((float(c[k] / (np.std(c) + 1e-9)), f, lags[k] / 50.0))
    res.sort(reverse=True)
    return res


def vad_confidence(vad, items):
    """Score 0-100 : part des sous-titres posée sur de la parole, comparée à un placement au hasard."""
    sp = vad > 0.5; N = len(sp)
    cs = np.concatenate([[0], np.cumsum(sp)])
    def cov(shift):
        tot = hit = 0
        for a, b, _ in items:
            i = int((a + shift) * SR); j = int((b + shift) * SR)
            i = max(0, min(N, i)); j = max(0, min(N, j))
            if j > i:
                hit += cs[j] - cs[i]; tot += j - i
        return hit / tot if tot else 0
    c0 = cov(0)
    rnd = np.mean([cov(s) for s in (-240, -150, -90, -45, 45, 90, 150, 240)])
    if rnd >= 0.999:
        return 0.0, c0, rnd
    raw = max(0.0, (c0 - rnd) / (1 - rnd)) * 100      # ~35-50 pour une synchro correcte, <15 si décalée
    return float(min(100.0, raw * 2.2)), c0, rnd


# --------------------------------------------------------------------------- méthode simple

def _vad_ref_srt(vad, path):
    p = vad > 0.5
    segs = []; start = None
    for i, x in enumerate(np.append(p, False)):
        if x and start is None: start = i
        if not x and start is not None: segs.append([start, i]); start = None
    m = []
    for s in segs:
        if m and s[0] - m[-1][1] < 30: m[-1][1] = s[1]
        else: m.append(s)
    m = [s for s in m if s[1] - s[0] >= 20]
    with open(path, "w", encoding="utf-8") as f:
        f.write(srt.compose([srt.Subtitle(i + 1, T(seconds=a / SR), T(seconds=b / SR), "x") for i, (a, b) in enumerate(m)]))


def simple_sync(subs, vad, log, split_penalty=7.0):
    """Renvoie dict(items, speed, blocks, z)."""
    scan = speed_scan(vad, subs)
    z, speed, off0 = scan[0]
    if abs(speed - 1) > 1e-6 and scan[0][0] < 1.15 * next((r[0] for r in scan if abs(r[1] - 1) < 1e-6), 0):
        # gain trop faible : on garde la vitesse normale
        z, speed, off0 = next(r for r in scan if abs(r[1] - 1) < 1e-6)
    log("  vitesse retenue : x%.4f (pic de corrélation %.1f, décalage approx. %+.1f s)" % (speed, z, off0))
    exe = alass_exe()
    tmp = tempfile.mkdtemp(prefix="subsync_")
    try:
        ref = os.path.join(tmp, "ref.srt"); inp = os.path.join(tmp, "in.srt"); out = os.path.join(tmp, "out.srt")
        _vad_ref_srt(vad, ref)
        scaled = [srt.Subtitle(i + 1, T(seconds=x.start.total_seconds() * speed), T(seconds=x.end.total_seconds() * speed), x.content)
                  for i, x in enumerate(subs)]
        with open(inp, "w", encoding="utf-8") as f:
            f.write(srt.compose(scaled))
        blocks = []
        if exe:
            p = subprocess.run([exe, ref, inp, out, "--disable-fps-guessing", "--split-penalty", str(split_penalty)],
                               capture_output=True, text=True, **_noconsole())
            txt = (p.stdout or "") + (p.stderr or "")
            for m in re.finditer(r"shifted block of (\d+) subtitles with length ([\d:.]+) by (-?)(\d+):(\d+):(\d+)\.(\d+)", txt):
                sec = int(m.group(4)) * 3600 + int(m.group(5)) * 60 + int(m.group(6)) + int(m.group(7)) / 1000.0
                blocks.append((int(m.group(1)), -sec if m.group(3) else sec))
            if p.returncode != 0 or not os.path.exists(out):
                raise RuntimeError("alass a échoué :\n" + txt[-600:])
            res = read_srt(out)
            items = [(x.start.total_seconds(), x.end.total_seconds(), x.content) for x in res]
        else:
            log("  (alass introuvable : décalage global uniquement)")
            items = [(a.start.total_seconds() + off0, a.end.total_seconds() + off0, a.content) for a in scaled]
            blocks = [(len(items), off0)]
        return dict(items=items, speed=speed, blocks=blocks, z=z)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- méthode avancée : modèles

_whisper = {}
_mt = {}


def _hf_download(repo, log):
    from huggingface_hub import snapshot_download
    dest = os.path.join(MODELS_DIR, repo.replace("/", "__"))
    if not os.path.exists(os.path.join(dest, "model.bin")):
        log("  téléchargement du modèle %s (une seule fois)…" % repo)
        snapshot_download(repo, local_dir=dest)
    return dest


def transcribe(path, audio, lang, log, cancel=None, model_size="base"):
    cache = _cache_key(path, "whisper-%s-%s" % (model_size, lang)) + ".json"
    if os.path.exists(cache):
        return json.load(open(cache, encoding="utf-8"))
    from faster_whisper import WhisperModel
    name = model_size + (".en" if lang == "en" else "")
    if name not in _whisper:
        d = _hf_download("Systran/faster-whisper-" + name, log)
        _whisper[name] = WhisperModel(d, device="cpu", compute_type="int8", cpu_threads=max(1, (os.cpu_count() or 2) - 1))
    if audio is None:
        audio = load_audio(path)
    log("  transcription de l'audio (quelques minutes)…")
    segs, _ = _whisper[name].transcribe(audio, language=lang, vad_filter=True, beam_size=1, condition_on_previous_text=False)
    out = []
    for s in segs:
        if cancel and cancel():
            raise Cancelled()
        out.append((s.start, s.end, s.text))
    json.dump(out, open(cache, "w", encoding="utf-8"))
    return out


def translate(subs, src, tgt, log, cache_path=None):
    if src == tgt:
        return {x.index: x.content.replace("\n", " ") for x in subs}
    if cache_path and os.path.exists(cache_path):
        d = json.load(open(cache_path, encoding="utf-8"))
        return {int(k): v for k, v in d.items()}
    tr, sp, tp = _load_mt(src, tgt, log)
    log("  traduction des sous-titres pour comparaison…")
    toks = [sp.encode(re.sub(r"<[^>]+>", "", x.content).replace("\n", " "), out_type=str) + ["</s>"] for x in subs]
    res = tr.translate_batch(toks, max_batch_size=32, beam_size=2, max_decoding_length=80)
    out = {x.index: tp.decode(r.hypotheses[0]) for x, r in zip(subs, res)}
    if cache_path:
        json.dump(out, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False)
    return out


_STOP_EN = set(("a an the to of and or i you he she it we they is are was were be been am do does did not no yes in on at for with "
                "that this what me my your his her our us them him have has had will would can could just so but if there here all oh "
                "well now then get got go going know s t m re ll d ve don sir").split())


def _norm(w):
    return unicodedata.normalize("NFKD", w).encode("ascii", "ignore").decode().lower()


def make_toks(lang):
    stop = _STOP_EN if lang == "en" else set()
    def toks(t):
        out = set()
        for w in re.findall(r"[\w']+", t.lower()):
            w = _norm(w).replace("'s", "").strip("'")
            if w in stop or len(w) < 2:
                continue
            if len(w) > 4 and w.endswith("s"):
                w = w[:-1]
            out.add(w[:6])
        return out
    return toks


# --------------------------------------------------------------------------- méthode avancée : alignement

def _sim_matrix(A, B):
    voc = {w: i for i, w in enumerate(sorted(set().union(*A, *B)))}
    if not voc:
        return np.zeros((len(A), len(B)), np.float32)
    MA = np.zeros((len(A), len(voc)), np.float32); MB = np.zeros((len(B), len(voc)), np.float32)
    for i, s in enumerate(A):
        for w in s: MA[i, voc[w]] = 1
    for j, s in enumerate(B):
        for w in s: MB[j, voc[w]] = 1
    df = MA.sum(0) + MB.sum(0); idf = np.log((len(A) + len(B)) / (1 + df)); MA *= idf; MB *= idf
    return (MA @ MB.T) / (np.linalg.norm(MA, axis=1)[:, None] + 1e-6) / (np.linalg.norm(MB, axis=1)[None, :] + 1e-6)


def _chains(P, S, st, segs, F):
    """Chaînes d'ancrages au décalage continu. Renvoie (chaines, poids total)."""
    stF = st * F
    off = [segs[j][0] - stF[i] for i, j in P]; w = [float(S[i, j]) for i, j in P]
    n = len(P); best = np.array(w, dtype=float); prev = [-1] * n
    for b in range(n):
        ib, jb = P[b]
        for a in range(max(0, b - 60), b):
            ia, ja = P[a]
            if ia < ib and ja <= jb and abs(off[a] - off[b]) < 2.5 and stF[ib] - stF[ia] < 45 and segs[jb][0] - segs[ja][0] < 45:
                if best[a] + w[b] > best[b]:
                    best[b] = best[a] + w[b]; prev[b] = a
    order = np.argsort(-best); taken = set(); chains = []
    for e in order:
        if e in taken: continue
        ch = []; k = e
        while k != -1 and k not in taken:
            ch.append(k); k = prev[k]
        ch = ch[::-1]
        for k in ch: taken.add(k)
        if len(ch) < 2 and w[ch[0]] < 0.7:
            continue
        subs_ = [P[k][0] for k in ch]; a0 = segs[P[ch[0]][1]][0]; a1 = segs[P[ch[-1]][1]][1]
        i0, i1 = min(subs_), max(subs_)
        if any(not (a1 < c["a0"] or a0 > c["a1"]) for c in chains) or any(not (i1 < c["i0"] or i0 > c["i1"]) for c in chains):
            continue
        chains.append(dict(i0=i0, i1=i1, a0=a0, a1=a1, anc=[(P[k][0], off[k]) for k in ch], wt=float(sum(w[k] for k in ch))))
    return chains, sum(c["wt"] for c in chains)


def advanced_align(subs, trans, segs, vad, F, audio_len, toks):
    """Aligne la liste complète de sous-titres sur UN audio. Renvoie [(debut, fin, texte, index)]."""
    n = len(subs)
    st0, en0 = _times(subs); st = st0 * F; en = en0 * F
    A = [toks(trans[x.index]) for x in subs]
    segs = [s for s in segs if s[1] - s[0] < 20]
    B = [toks(s[2]) for s in segs]
    if not segs:
        return []
    S = _sim_matrix(A, B)
    v = np.where(vad > 0.5, 1.0, -0.25); N = len(v); cs = np.concatenate([[0], np.cumsum(v)])
    def vs(a, b):
        i = max(0, int(a * SR)); j = min(N, int(b * SR)); return cs[j] - cs[i] if j > i else -1e6
    def istitle(k):
        c = subs[k].content.strip(); return c.startswith('"') and c.endswith('"')
    seg_st = np.array([s[0] for s in segs]); seg_en = np.array([s[1] for s in segs])
    def W(a, b):
        js = np.nonzero((seg_en > a - 3) & (seg_st < b + 3))[0]
        return set().union(*[B[j] for j in js]) if len(js) else set()
    P = set()
    for j in range(len(segs)):
        for i in np.argsort(S[:, j])[::-1][:4]:
            if S[i, j] >= 0.3: P.add((int(i), j))
    for i in range(n):
        for j in np.argsort(S[i])[::-1][:4]:
            if S[i, j] >= 0.3: P.add((i, int(j)))
    P = sorted(P, key=lambda p: (segs[p[1]][0], p[0]))
    chains, _ = _chains(P, S, st0, segs, F)
    chains.sort(key=lambda c: c["a0"])
    place = {}
    for c in chains:
        d = {}
        for i, o in c["anc"]: d.setdefault(i, []).append(o)
        ai = sorted(d); ao = [float(np.median(d[i])) for i in ai]
        for k in range(c["i0"], c["i1"] + 1):
            place[k] = float(np.interp(st[k], [st[i] for i in ai], ao))
        ks = range(c["i0"], c["i1"] + 1)
        sh = max((sum(vs(st[k] + place[k] + d_, en[k] + place[k] + d_) for k in ks), d_) for d_ in np.arange(-1.5, 1.51, 0.1))[1]
        for k in ks: place[k] += sh
        c["offs"] = place[c["i0"]], place[c["i1"]]; c["a0"] = st[c["i0"]] + place[c["i0"]]; c["a1"] = en[c["i1"]] + place[c["i1"]]
    used = set(place)
    seq = [dict(i0=None, i1=-1, a0=0, a1=0, offs=(None, None))] + chains + [dict(i0=n, i1=None, a0=audio_len, a1=audio_len, offs=(None, None))]
    def sc(k, o):
        if istitle(k): return 1.0
        a, b = st[k] + o, en[k] + o
        w = W(a, b); base = vs(a, b)
        if len(A[k]) >= 2 and len(w) >= 2:
            ov = len(A[k] & w)
            return base + 200 * ov if ov else -1e5
        return base
    for Pc, Nc in zip(seq, seq[1:]):
        lo, hi = Pc["a1"], Nc["a0"]; po = Pc["offs"][1]; no = Nc["offs"][0]
        fw = []; k = Pc["i1"] + 1 if po is not None else None
        while k is not None and k < n and k not in used and st[k] - en[Pc["i1"]] < 30: fw.append(k); k += 1
        bw = []; k = Nc["i0"] - 1 if no is not None else None
        while k is not None and k >= 0 and k not in used and st[Nc["i0"]] - en[k] < 30: bw.append(k); k -= 1
        shared = bool(fw) and bool(bw) and fw[0] <= bw[0]
        best_ = (0, 0, 0)
        for f in range(len(fw) + 1):
            if f and not (st[fw[f - 1]] + po >= lo - 0.5 and en[fw[f - 1]] + po <= hi + 0.5): break
            sf = sum(sc(k, po) for k in fw[:f]); fend = en[fw[f - 1]] + po if f else lo
            for b_ in range(len(bw) + 1):
                if b_ and not (st[bw[b_ - 1]] + no >= lo - 0.5 and en[bw[b_ - 1]] + no <= hi + 0.5): break
                if shared and f + b_ > len([k for k in fw if k <= bw[0]]): break
                bstart = st[bw[b_ - 1]] + no if b_ else hi
                if bstart < fend - 0.3: continue
                tot = sf + sum(sc(k, no) for k in bw[:b_])
                if tot > best_[0]: best_ = (tot, f, b_)
        for k in fw[:best_[1]]: place[k] = po
        for k in bw[:best_[2]]: place[k] = no
    cur = [[st[k] + o, en[k] + o, k] for k, o in place.items()]
    dur = en - st
    def sim(a, b): return len(a & b) / np.sqrt(len(a) * len(b)) if a and b else 0
    def contra(c):
        w = W(c[0], c[1]); return len(A[c[2]]) >= 2 and len(w) >= 2 and not (A[c[2]] & w)
    def tidy(cur):
        cur.sort()
        for a, b in zip(cur, cur[1:]):
            if a[1] > b[0] - 0.04: a[1] = max(a[0] + 0.3, b[0] - 0.04)
        return cur
    def post(cur):
        for c in cur:
            a = A[c[2]]
            if len(a) < 2: continue
            here = max([sim(a, B[j]) for j in np.nonzero((seg_en > c[0] - 2) & (seg_st < c[1] + 2))[0]] or [0])
            js = np.nonzero(np.abs(seg_st - c[0]) < 40)[0]
            if not len(js): continue
            b, j = max((sim(a, B[j]), j) for j in js)
            if b >= 0.55 and b > here + 0.2 and abs(segs[j][0] - c[0]) > 2:
                c[0] = segs[j][0]; c[1] = segs[j][0] + dur[c[2]]
        cur.sort()
        fl = [contra(c) for c in cur]; drop = set(); i = 0
        while i < len(cur):
            if fl[i]:
                j = i
                while j < len(cur) and fl[j]: j += 1
                if j - i >= 3: drop.update(range(i, j))
                i = j
            else: i += 1
        return tidy([c for k, c in enumerate(cur) if k not in drop])
    cur = post(tidy(cur))
    # 2e passe : répliques orphelines dans les trous
    usedk = set(c[2] for c in cur)
    def free(a, b): return all(b <= c[0] + 0.05 or a >= c[1] - 0.05 for c in cur)
    for k in [k for k in range(n) if k not in usedk]:
        if k in usedk or not A[k]: continue
        best = None
        for j, s in enumerate(segs):
            if not B[j] or not free(s[0], s[1]): continue
            sm = sim(A[k], B[j])
            if sm >= 0.5 and (best is None or sm > best[0]): best = (sm, s)
        if best:
            o = best[1][0] - st[k]
            for kk in [k] + [j for j in range(k - 6, k + 7) if j != k and 0 <= j < n and j not in usedk]:
                a, b = st[kk] + o, en[kk] + o
                if 0 <= a and b <= audio_len + 0.3 and free(a, b) and kk not in usedk:
                    cur.append([a, b, kk]); usedk.add(kk)
    cur = post(tidy(cur))
    # nettoyage final
    seen = set(); cur = [c for c in sorted(cur) if not (c[2] in seen or seen.add(c[2]))]
    for _ in range(2):
        fl = [contra(c) for c in cur]; keep = []
        for i, c in enumerate(cur):
            nb = [fl[j] for j in range(max(0, i - 2), min(len(cur), i + 3)) if j != i]
            if fl[i] and sum(nb) >= 2: continue
            keep.append(c)
        cur = keep
    def runs_of(cur):
        rs = []
        for c in cur:
            if rs and abs(c[2] - rs[-1][-1][2]) <= 20 and c[0] - rs[-1][-1][0] < 30: rs[-1].append(c)
            else: rs.append([c])
        return rs
    tmp = []
    for r in runs_of(cur):
        fl = [contra(c) for c in r]
        if len(r) <= 6 and sum(fl) >= max(1, 0.6 * len(r)): continue
        tmp += r
    cur = tmp; out = []
    for r in runs_of(cur):
        r = sorted(r, key=lambda c: c[2])
        y = [c[0] for c in r]; wts = [3.0 if sim(A[c[2]], W(c[0], c[1])) >= 0.5 else 1.0 for c in r]
        blocks = [[y[i], wts[i], 1] for i in range(len(r))]; i = 0
        while i < len(blocks) - 1:
            if blocks[i][0] > blocks[i + 1][0]:
                a, b = blocks[i], blocks[i + 1]; w_ = a[1] + b[1]
                blocks[i] = [(a[0] * a[1] + b[0] * b[1]) / w_, w_, a[2] + b[2]]; del blocks[i + 1]; i = max(0, i - 1)
            else: i += 1
        fit = []
        for val, _, cnt in blocks: fit += [val] * cnt
        for c, t in zip(r, fit):
            d = c[1] - c[0]; c[0] = t; c[1] = t + d
        out += r
    out.sort(key=lambda c: (c[0], c[2]))
    for i in range(1, len(out)):
        if out[i][0] < out[i - 1][0] + 0.3 and abs(out[i][2] - out[i - 1][2]) <= 20:
            d = out[i][1] - out[i][0]
            if out[i - 1][1] > out[i][0]: out[i][0] = out[i - 1][0] + 0.3
            out[i][1] = out[i][0] + d
    out.sort(key=lambda c: c[0])
    for a, b in zip(out, out[1:]):
        if a[1] > b[0] - 0.04: a[1] = b[0] - 0.04
    out = [c for c in out if c[1] - c[0] >= min(0.7, 0.8 * dur[c[2]])]
    return [(a, b, subs[k].content, k) for a, b, k in out]


def choose_speed(subs, trans, segs_list, toks):
    """Vitesse = pente la plus fréquente entre paires de répliques fiables (FR traduit <-> transcription),
    arrondie au rapport standard le plus proche."""
    st0, _ = _times(subs)
    A = [toks(trans[x.index]) for x in subs]
    slopes = []
    for segs in segs_list:
        segs = [s for s in segs if s[1] - s[0] < 20]
        if not segs: continue
        B = [toks(s[2]) for s in segs]; S = _sim_matrix(A, B)
        pr = sorted((st0[i], segs[int(np.argmax(S[i]))][0]) for i in range(len(subs)) if S[i].max() >= 0.6)
        for x in range(len(pr)):
            for y in range(x + 1, min(len(pr), x + 15)):
                ds = pr[y][0] - pr[x][0]
                if 15 <= ds <= 120:
                    slopes.append((pr[y][1] - pr[x][1]) / ds)
    if len(slopes) < 50:
        return 1.0
    h, bins = np.histogram(slopes, bins=np.arange(0.70, 1.40, 0.0025))
    m = float(bins[int(np.argmax(h))] + 0.00125)
    best = min(SPEED_CANDIDATES, key=lambda f: abs(f - m))
    if abs(best - 1.0) < 0.002 or abs(m - 1.0) < 0.008:
        return 1.0
    return best


def text_check(items_idx, trans, segs, toks):
    """% de sous-titres partageant au moins un mot avec ce qui est dit, et couverture des dialogues."""
    seg_st = np.array([s[0] for s in segs]); seg_en = np.array([s[1] for s in segs])
    B = [toks(s[2]) for s in segs]
    hits = []
    for a, b, _, idx in items_idx:
        A = toks(trans[idx])
        if not A: continue
        js = np.nonzero((seg_en > a - 2) & (seg_st < b + 2))[0]
        w = set().union(*[B[j] for j in js]) if len(js) else set()
        hits.append(1.0 if A & w else 0.0)
    ww = [s for s in segs if len(s[2].split()) >= 3 and s[1] - s[0] < 15]
    cov = [any(a < s[1] and b > s[0] for a, b, _, _ in items_idx) for s in ww]
    return (100 * float(np.mean(hits)) if hits else 0.0), (100 * float(np.mean(cov)) if cov else 0.0)


# --------------------------------------------------------------------------- appariement des fichiers

_EP_PATTERNS = [
    re.compile(r"[sS](\d{1,2})[ ._-]?[eE](\d{1,3})(?:[ ._-]*(?:[eE]|-)(\d{1,3}))?"),
    re.compile(r"(?<!\d)(\d{1,2})[xX](\d{1,3})(?:[ ._-]*(?:-|[xX]\d{1,2}[xX]?|&)(\d{1,3}))?"),
]


def episode_keys(name):
    """('S', saison, [épisodes]) ou None."""
    base = os.path.basename(name)
    for pat in _EP_PATTERNS:
        m = pat.search(base)
        if m:
            s = int(m.group(1)); e1 = int(m.group(2)); e2 = int(m.group(3)) if m.group(3) else e1
            if e2 < e1 or e2 - e1 > 4: e2 = e1
            return s, list(range(e1, e2 + 1))
    return None


_TAGS = re.compile(r"\b(\d{3,4}p|x26[45]|h\.?26[45]|hevc|avc|aac|ac3|e-?ac-?3|dts|web[- .]?dl|web[- .]?rip|bluray|blu-ray|bdrip|brrip|"
                   r"dvdrip|hdtv|remux|10 ?bit|8 ?bit|multi|vff|vfq|vf|vo|vostfr|truefrench|french|english|subs?|hdr|sdr|proper|repack|"
                   r"ita|eng|fre|fra|pl|lektor|hq|hd)\b", re.I)


def _clean_part(t):
    t = re.sub(r"[\[(][^\])]*[\])]", " ", t)                 # [10Bit HQ] (1998)...
    t = t.replace("_", " ")
    if t.count(".") >= 2 and " " not in t.strip(): t = t.replace(".", " ")
    t = _TAGS.split(t)[0] if _TAGS.search(t) else t
    t = re.sub(r"\s+", " ", t).strip(" -._–")
    return t


def plex_name(paths, prefer_title_from=None):
    """« Série - S04E01 - Titre » d'après les noms de fichiers (titre pris de préférence dans `prefer_title_from`,
    ex. la version française). None si le numéro d'épisode est introuvable."""
    best = None
    for p in ([prefer_title_from] if prefer_title_from else []) + list(paths):
        if not p: continue
        base = os.path.splitext(os.path.basename(p))[0]
        for pat in _EP_PATTERNS:
            m = pat.search(base)
            if not m: continue
            se, e1 = int(m.group(1)), int(m.group(2)); e2 = int(m.group(3)) if m.group(3) else e1
            series = _clean_part(base[:m.start()]); title = _clean_part(base[m.end():])
            series = re.sub(r"\s*[`´'’]\s*", "'", series)
            cand = dict(series=series, se=se, e1=e1, e2=e2 if e2 >= e1 and e2 - e1 <= 4 else e1, title=title)
            if best is None: best = cand
            else:
                if not best["title"] and title: best["title"] = title
                if not best["series"] and series: best["series"] = series
            break
    if not best or not best["series"]: return None
    if best["series"].islower(): best["series"] = best["series"].title()
    ep = "S%02dE%02d" % (best["se"], best["e1"]) + ("-E%02d" % best["e2"] if best["e2"] != best["e1"] else "")
    name = "%s - %s" % (best["series"], ep) + (" - %s" % best["title"] if best["title"] else "")
    return re.sub(r'[<>:"/\\|?*]', "", name).strip()


def _similar(a, b):
    import difflib
    na = re.sub(r"[^a-z0-9]+", " ", os.path.splitext(os.path.basename(a))[0].lower())
    nb = re.sub(r"[^a-z0-9]+", " ", os.path.splitext(os.path.basename(b))[0].lower())
    return difflib.SequenceMatcher(None, na, nb).ratio()


def pair_files(srts, videos):
    """Associe chaque .srt à une ou plusieurs vidéos. Renvoie [(srt, [videos])], vidéos restantes."""
    left = list(videos); pairs = []
    keyed = {v: episode_keys(v) for v in videos}
    for s in srts:
        k = episode_keys(s); match = []
        if k:
            for v in left:
                kv = keyed.get(v)
                if kv and kv[0] == k[0] and kv[1][0] in k[1]:
                    match.append(v)
            match.sort(key=lambda v: keyed[v][1][0])
        if not match and left:
            core = lambda p: re.sub(r"[^a-z0-9]", "", os.path.splitext(os.path.basename(p))[0].lower().rsplit(".", 1)[0])
            cs = core(s)
            inc = [v for v in left if len(cs) >= 4 and cs in core(v)]
            if len(inc) == 1:
                match = inc
            else:
                best = max(left, key=lambda v: _similar(s, v))
                if _similar(s, best) > 0.5:
                    match = [best]

        for v in match:
            left.remove(v)
        pairs.append((s, match))
    if len(pairs) == 1 and not pairs[0][1] and len(left) == 1:
        pairs[0] = (pairs[0][0], [left.pop()])
    return pairs, left


def output_path(video, sub_lang, out_dir=None):
    stem = os.path.splitext(os.path.basename(video))[0]
    d = out_dir or os.path.dirname(video)
    return os.path.join(d, "%s.%s.srt" % (stem, sub_lang))


# --------------------------------------------------------------------------- tâche complète

class Result:
    def __init__(self, video, out_path, method, confidence, level, summary, details, items):
        self.video = video; self.out_path = out_path; self.method = method
        self.confidence = confidence; self.level = level; self.summary = summary
        self.details = details; self.items = items  # items: [(debut, fin, texte)]


def _fmt_blocks(blocks, speed):
    parts = []
    if abs(speed - 1) > 1e-6:
        parts.append("vitesse x%.4f" % speed)
    if not blocks:
        return ", ".join(parts) or "aucune correction"
    if len(blocks) == 1:
        parts.append("décalage %+.2f s" % blocks[0][1])
    else:
        parts.append("%d blocs : " % len(blocks) + " / ".join("%+.2f s" % b[1] for b in blocks))
    return ", ".join(parts)


def level_of(conf, z=99):
    if conf >= 55 and z >= 10: return "ok"
    if conf >= 33 or z >= 8: return "warn"
    return "bad"


def run_job(srt_path, videos, mode="auto", sub_lang="fr", audio_lang="en", out_dir=None,
            log=print, progress=None, cancel=None, whisper_size="base"):
    """Synchronise srt_path sur une ou plusieurs vidéos. Renvoie une liste de Result."""
    def chk():
        if cancel and cancel(): raise Cancelled()
    subs = read_srt(srt_path)
    if not subs:
        raise RuntimeError("Aucun sous-titre lisible dans %s" % os.path.basename(srt_path))
    log("Sous-titres : %s (%d répliques, %s)" % (os.path.basename(srt_path), len(subs), str(subs[-1].end).split(".")[0]))
    vads, audios, lens = [], [], []
    for i, v in enumerate(videos):
        chk(); log("Analyse de la voix : %s" % os.path.basename(v))
        aud = None
        cache = _cache_key(v, "silero1") + ".npy"
        if not os.path.exists(cache):
            aud = load_audio(v)
        vad = speech_probs(v, aud, progress=(lambda f, i=i: progress and progress((i + f) / len(videos) * 0.3)), cancel=cancel)
        vads.append(vad); audios.append(aud); lens.append(len(vad) / SR)
    total_audio = sum(lens); sub_span = subs[-1].end.total_seconds()
    results = []
    use_adv = mode == "advanced" or len(videos) > 1
    if mode == "simple" and len(videos) > 1:
        log("  un même .srt pour %d vidéos : la méthode avancée est nécessaire" % len(videos))
    if not use_adv:
        v = videos[0]; vad = vads[0]
        log("Méthode simple…")
        r = simple_sync(subs, vad, log)
        conf, c0, rnd = vad_confidence(vad, r["items"])
        log("  confiance %.0f/100 (parole sous les sous-titres %.0f %% contre %.0f %% au hasard)" % (conf, 100 * c0, 100 * rnd))
        span_ratio = (sub_span * r["speed"]) / max(1, lens[0])
        suspicious = conf < 44 or r["z"] < 8 or span_ratio > 1.06
        if mode == "auto" and suspicious:
            log("  résultat incertain -> passage en méthode avancée")
            use_adv = True
        else:
            out = output_path(v, sub_lang, out_dir)
            if os.path.abspath(out) == os.path.abspath(srt_path):
                out = out[:-4] + ".sync.srt"
            write_srt(out, r["items"])
            results.append(Result(v, out, "simple", conf, level_of(conf, r["z"]), _fmt_blocks(r["blocks"], r["speed"]),
                                  dict(blocks=r["blocks"], speed=r["speed"], z=r["z"]), r["items"]))
            if progress: progress(1.0)
            return results
    # ------------------------------------------------ méthode avancée
    log("Méthode avancée (transcription + traduction)…")
    toks = make_toks(audio_lang)
    trans = translate(subs, sub_lang, audio_lang, log, cache_path=_cache_key(srt_path, "mt-%s-%s" % (sub_lang, audio_lang)) + ".json")
    chk()
    segs_list = []
    for i, v in enumerate(videos):
        chk(); log("Transcription : %s" % os.path.basename(v))
        segs_list.append(transcribe(v, audios[i], audio_lang, log, cancel=cancel, model_size=whisper_size))
        audios[i] = None
        if progress: progress(0.3 + 0.5 * (i + 1) / len(videos))
    F = choose_speed(subs, trans, segs_list, toks)
    log("  vitesse retenue : x%.4f" % F)
    used_all = set()
    for i, v in enumerate(videos):
        chk(); log("Alignement : %s" % os.path.basename(v))
        items_idx = advanced_align(subs, trans, segs_list[i], vads[i], F, lens[i], toks)
        used_all.update(k for *_, k in items_idx)
        items_idx_named = [(a, b, c, subs[k].index) for a, b, c, k in items_idx]
        match, cov = text_check(items_idx_named, trans, segs_list[i], toks)
        conf_v, _, _ = vad_confidence(vads[i], [(a, b, c) for a, b, c, _ in items_idx])
        conf = 0.6 * match + 0.4 * cov if items_idx else 0
        items = [(a, b, c) for a, b, c, _ in items_idx]
        out = output_path(v, sub_lang, out_dir)
        if os.path.abspath(out) == os.path.abspath(srt_path):
            out = out[:-4] + ".sync.srt"
        write_srt(out, items)
        lvl = "ok" if match >= 72 and cov >= 85 else ("warn" if match >= 60 else "bad")
        summ = "avancée : %d répliques placées, correspondance %.0f %%, dialogues couverts %.0f %%" % (len(items), match, cov)
        log("  " + summ)
        results.append(Result(v, out, "avancée", conf, lvl, summ, dict(speed=F, match=match, coverage=cov, voice=conf_v), items))
    unused = len(subs) - len(used_all)
    if unused:
        log("  %d répliques du .srt non placées (scènes absentes de la vidéo ou non retrouvées)" % unused)
    if progress: progress(1.0)
    return results


# --------------------------------------------------------------------------- traduction par IA locale (llama.cpp)

LLM_DIR = os.path.join(APP_DIR, "llm")
LLAMA_BUILD = "b12541"
LLM_MODEL = ("bartowski/mistralai_Mistral-Small-3.2-24B-Instruct-2506-GGUF",
             "mistralai_Mistral-Small-3.2-24B-Instruct-2506-Q4_K_M.gguf", "Mistral Small 3.2 24B", 14.3)


def llama_server_exe():
    for root, _, fs in os.walk(os.path.join(LLM_DIR, "bin")):
        for n in ("llama-server.exe", "llama-server"):
            if n in fs:
                return os.path.join(root, n)
    return None


def llm_model_path():
    p = os.path.join(LLM_DIR, "models", LLM_MODEL[1])
    if os.path.exists(p):
        return p
    # n'importe quel autre .gguf déposé à la main
    d = os.path.join(LLM_DIR, "models")
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            if f.lower().endswith(".gguf"):
                return os.path.join(d, f)
    return None


def llm_ready():
    return bool(llama_server_exe() and llm_model_path())


def ensure_llm(log, gpu=True):
    """Télécharge llama.cpp et le modèle si besoin (une seule fois)."""
    import urllib.request, zipfile, tarfile
    if not llama_server_exe():
        os.makedirs(os.path.join(LLM_DIR, "bin"), exist_ok=True)
        if os.name == "nt":
            name = "llama-%s-bin-win-%s-x64.zip" % (LLAMA_BUILD, "vulkan" if gpu else "cpu")
        else:
            name = "llama-%s-bin-ubuntu-x64.tar.gz" % LLAMA_BUILD
        url = "https://github.com/ggml-org/llama.cpp/releases/download/%s/%s" % (LLAMA_BUILD, name)
        log("  téléchargement du moteur d'IA llama.cpp (%s)…" % name)
        tmp = os.path.join(tempfile.gettempdir(), name)
        urllib.request.urlretrieve(url, tmp)
        if name.endswith(".zip"):
            zipfile.ZipFile(tmp).extractall(os.path.join(LLM_DIR, "bin"))
        else:
            tarfile.open(tmp).extractall(os.path.join(LLM_DIR, "bin"))
        os.remove(tmp)
        if os.name != "nt":
            os.chmod(llama_server_exe(), 0o755)
    if not llm_model_path():
        from huggingface_hub import hf_hub_download
        log("  téléchargement du modèle %s (%.1f Go, une seule fois — cela peut prendre longtemps)…" % (LLM_MODEL[2], LLM_MODEL[3]))
        hf_hub_download(LLM_MODEL[0], LLM_MODEL[1], local_dir=os.path.join(LLM_DIR, "models"))
    return llama_server_exe(), llm_model_path()


class LlamaServer:
    """Lance llama-server en arrière-plan le temps d'une traduction."""
    def __init__(self, log, ctx=12288, gpu_layers=999, threads=None):
        self.log = log; self.ctx = ctx; self.ngl = gpu_layers; self.threads = threads; self.proc = None; self.port = None

    def __enter__(self):
        import socket, urllib.request
        exe, model = llama_server_exe(), llm_model_path()
        sk = socket.socket(); sk.bind(("127.0.0.1", 0)); self.port = sk.getsockname()[1]; sk.close()
        cmd = [exe, "-m", model, "-c", str(self.ctx), "--port", str(self.port), "--host", "127.0.0.1",
               "-ngl", str(self.ngl), "-np", "1", "--no-webui"]
        if self.threads: cmd += ["-t", str(self.threads)]
        logf = open(os.path.join(APP_DIR, "logs", "llama-server.log"), "w", encoding="utf-8", errors="replace") \
            if os.path.isdir(os.path.join(APP_DIR, "logs")) else subprocess.DEVNULL
        self.log("  démarrage de l'IA locale (%s)…" % os.path.basename(model))
        self.proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, cwd=os.path.dirname(exe), **_noconsole())
        t0 = time.time()
        while time.time() - t0 < 600:
            if self.proc.poll() is not None:
                raise RuntimeError("L'IA locale n'a pas pu démarrer (voir logs\\llama-server.log).")
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/health" % self.port, timeout=2) as r:
                    if r.status == 200 and b"ok" in r.read():
                        self.log("  IA prête (%.0f s)." % (time.time() - t0)); return self
            except Exception:
                pass
            time.sleep(1)
        raise RuntimeError("L'IA locale ne répond pas.")

    def chat(self, messages, schema=None, max_tokens=2048, temperature=0.2):
        import urllib.request
        body = {"messages": messages, "temperature": temperature, "top_p": 0.9, "max_tokens": max_tokens, "cache_prompt": True}
        if schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "reponse", "schema": schema}}
        req = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % self.port, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=900) as r:
            d = json.loads(r.read())
        return d["choices"][0]["message"]["content"]

    def __exit__(self, *a):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try: self.proc.wait(10)
            except Exception: self.proc.kill()


_LANG_NAME = {"fr": "français", "en": "anglais", "es": "espagnol", "de": "allemand", "it": "italien", "pt": "portugais", "nl": "néerlandais"}


def _llm_system(src, tgt, gl, note):
    S, Tn = _LANG_NAME.get(src, src), _LANG_NAME.get(tgt, tgt)
    rules = [
        "Tu es un adaptateur professionnel de sous-titres pour la télévision, du %s vers le %s." % (S, Tn),
        "On te donne des répliques numérotées, transcrites automatiquement d'un épisode. Pour CHAQUE réplique, écris sa traduction, dans le même ordre, sans en fusionner ni en sauter.",
        "Règles d'adaptation :",
        "- Traduis le sens, pas mot à mot : un %s oral, naturel et idiomatique, comme un vrai sous-titre." % Tn,
        "- Reste concis : idéalement moins de 80 caractères par réplique ; supprime les hésitations et répétitions inutiles.",
        "- Tutoiement / vouvoiement : déduis-le du contexte et reste COHÉRENT d'une réplique à l'autre (supérieurs hiérarchiques, inconnus, personnes âgées : vous ; amis proches, couple, famille, enfants : tu).",
        "- Garde les noms propres, grades et noms de vaisseaux ; applique strictement le glossaire.",
        "- La transcription peut contenir des erreurs (mots ou noms mal entendus) : corrige-les quand le contexte le rend évident.",
        "- Ponctuation française : espace avant ? ! : ; et guillemets « ».",
        "- Ne traduis pas les répliques de contexte : elles servent seulement à comprendre la scène.",
    ]
    if gl:
        rules.append("Glossaire imposé (terme -> traduction) :")
        rules += ["- %s -> %s" % (a.lstrip("~"), b) for a, b in gl if not a.startswith("~")]
    if note:
        rules.append("Informations sur la série : " + note)
    rules.append("Réponds uniquement avec le JSON demandé : {\"t\": [{\"en\": réplique 1 recopiée à l'identique, \"fr\": sa traduction}, ...]} — une entrée par réplique à traduire, dans l'ordre ; recopie exactement la réplique d'origine dans \"en\" et mets SA traduction dans \"fr\".")
    return "\n".join(rules)


def llm_translate(src_sents, src_lang, tgt_lang, gl, note, log, progress=None, cancel=None, batch=14, fallback=None):
    """Traduit une liste de phrases avec contexte. fallback(list)->list utilisé si un lot échoue."""
    out = [None] * len(src_sents)
    sysmsg = _llm_system(src_lang, tgt_lang, gl, note)
    import difflib
    def norm(x): return re.sub(r"[^a-z0-9]+", " ", x.lower()).strip()
    with LlamaServer(log) as srv:
        t0 = time.time(); done = [0]
        def run(i, j, tries):
            """Traduit src_sents[i:j]; renvoie la liste ou None."""
            prev = [(src_sents[k], out[k]) for k in range(max(0, i - 8), i) if out[k]]
            nxt = src_sents[j:j + 3]
            lines = []
            if prev:
                lines.append("Contexte (déjà traduit) :")
                lines += ["  %s  =>  %s" % (a_, b_) for a_, b_ in prev]
            lines.append("")
            lines.append("À traduire (%d répliques) :" % (j - i))
            lines += ["%d. %s" % (k - i + 1, src_sents[k]) for k in range(i, j)]
            if nxt:
                lines.append("")
                lines.append("Suite de la scène (contexte, ne pas traduire) :")
                lines += ["  " + x for x in nxt]
            n = j - i
            schema = {"type": "object", "properties": {"t": {"type": "array", "minItems": n, "maxItems": n, "items": {
                "type": "object", "properties": {"en": {"type": "string"}, "fr": {"type": "string"}}, "required": ["en", "fr"]}}},
                "required": ["t"]}
            best = None
            for attempt in range(tries):
                if cancel and cancel(): raise Cancelled()
                try:
                    txt = srv.chat([{"role": "system", "content": sysmsg}, {"role": "user", "content": "\n".join(lines)}],
                                   schema=schema, max_tokens=120 * n + 300, temperature=0.2 if attempt == 0 else 0.45)
                    d = json.loads(txt)["t"]
                    if len(d) != n: raise ValueError("nombre de répliques")
                    ok = [difflib.SequenceMatcher(None, norm(d[k]["en"]), norm(src_sents[i + k])).ratio() >= 0.6 for k in range(n)]
                    if all(ok): return [x["fr"].strip() for x in d], None
                    if best is None or sum(ok) > sum(best[1]): best = (d, ok)
                except Cancelled:
                    raise
                except Exception as e:
                    log("    (lot %d-%d : %s)" % (i + 1, j, str(e)[:80]))
            return None, best
        def solve(i, j):
            res, best = run(i, j, 2 if j - i > 4 else 3)
            if res is None and j - i > 4:
                log("    (lot %d-%d : alignement incertain, découpage en deux)" % (i + 1, j))
                m = (i + j) // 2
                solve(i, m); solve(m, j); return
            if res is None:
                if best and fallback:
                    d, ok = best; fb = iter(fallback([src_sents[i + k] for k in range(j - i) if not ok[k]]))
                    res = [d[k]["fr"].strip() if ok[k] else next(fb) for k in range(j - i)]
                else:
                    res = fallback(src_sents[i:j]) if fallback else src_sents[i:j]
                log("    lot %d-%d : traduction standard utilisée pour les répliques incertaines" % (i + 1, j))
            out[i:j] = res; done[0] += j - i
            if progress: progress(done[0] / len(src_sents))
        i = 0
        while i < len(src_sents):
            j = min(len(src_sents), i + batch)
            solve(i, j); i = j
            if i < len(src_sents) and (i // batch) % 5 == 0:
                el = time.time() - t0
                log("    %d/%d répliques traduites (reste ~%d min)" % (i, len(src_sents), el / i * (len(src_sents) - i) / 60 + 1))
    return out


# --------------------------------------------------------------------------- création de sous-titres (audio -> texte -> traduction)

GLOSSARY_FILE = os.path.join(APP_DIR, "glossaire.txt")
WHISPER_QUALITY = {"rapide": "small", "précise": "medium"}


def _load_mt(src, tgt, log):
    import ctranslate2, sentencepiece as spm
    repo = "michaelfeil/ct2fast-opus-mt-%s-%s" % (src, tgt)
    if repo not in _mt:
        d = _hf_download(repo, log)
        _mt[repo] = (ctranslate2.Translator(d, device="cpu", compute_type="int8", inter_threads=max(1, (os.cpu_count() or 2) - 1)),
                     spm.SentencePieceProcessor(model_file=os.path.join(d, "source.spm")),
                     spm.SentencePieceProcessor(model_file=os.path.join(d, "target.spm")))
    return _mt[repo]


def read_context_note(path=None):
    path = path or GLOSSARY_FILE
    notes = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8-sig"):
            m = re.match(r"\s*contexte\s*=\s*(.+)", line, re.I)
            if m: notes.append(m.group(1).strip())
    return " ".join(notes)


def read_glossary(path=GLOSSARY_FILE):
    """Lignes « terme source = traduction imposée ». Les lignes commençant par # sont ignorées."""
    pairs = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8-sig"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            a, b = line.split("=", 1)
            if a.strip().lower() == "contexte":
                continue
            if a.strip():
                pairs.append((a.strip(), b.strip()))
    pairs.sort(key=lambda p: -len(p[0]))
    return pairs


def transcribe_words(path, audio, lang, log, cancel=None, model_size="small", progress=None):
    """Transcription avec horodatage par mot. Renvoie [(debut, fin, mot)]."""
    cache = _cache_key(path, "whisperw-%s-%s" % (model_size, lang)) + ".json"
    if os.path.exists(cache):
        return [tuple(w) for w in json.load(open(cache, encoding="utf-8"))]
    from faster_whisper import WhisperModel
    name = model_size + (".en" if lang == "en" else "")
    if name not in _whisper:
        d = _hf_download("Systran/faster-whisper-" + name, log)
        _whisper[name] = WhisperModel(d, device="cpu", compute_type="int8", cpu_threads=max(1, (os.cpu_count() or 2) - 1))
    if audio is None:
        audio = load_audio(path)
    dur = len(audio) / 16000.0
    log("  transcription précise de l'audio (modèle %s) — c'est l'étape la plus longue…" % name)
    segs, _ = _whisper[name].transcribe(audio, language=lang, vad_filter=True, beam_size=5, word_timestamps=True,
                                        condition_on_previous_text=True, no_speech_threshold=0.6,
                                        compression_ratio_threshold=2.2)
    words = []; last_txt = None; rep = 0
    for s in segs:
        if cancel and cancel():
            raise Cancelled()
        if progress: progress(min(0.99, s.end / max(1, dur)))
        txt = s.text.strip()
        # anti-hallucination : segments répétés en boucle, segments très incertains
        if txt == last_txt:
            rep += 1
            if rep >= 1: continue
        else:
            rep = 0
        last_txt = txt
        if s.avg_logprob < -1.2 or (s.no_speech_prob > 0.7 and s.avg_logprob < -0.6):
            continue
        for w in (s.words or []):
            if w.word.strip():
                words.append((float(w.start), float(w.end), w.word))
    json.dump(words, open(cache, "w", encoding="utf-8"))
    return words


def _sentences(words, max_len=200, max_dur=12.0):
    """Regroupe les mots en phrases : coupe à la ponctuation finale, sur une longue pause,
    ou sur une pause moyenne après une virgule ; évite les fragments de 1-2 mots."""
    out = []; cur = []
    def ends(w, pat): return re.search(pat, w[2].strip()) is not None
    for w in words:
        if cur:
            gap = w[0] - cur[-1][1]
            if gap > 2.0 or (w[0] - cur[0][0]) > max_dur or (gap > 0.9 and ends(cur[-1], r"[,;:]$")):
                out.append(cur); cur = []
        cur.append(w)
        if ends(w, r"[.?!…]\W*$") or len("".join(x[2] for x in cur)) > max_len:
            out.append(cur); cur = []
    if cur: out.append(cur)
    # fusion des fragments sans ponctuation finale avec la phrase suivante proche
    merged = []
    for sg in out:
        if merged:
            prev = merged[-1]; txt = "".join(x[2] for x in prev).strip()
            short = len(prev) <= 2 and not re.search(r"[.?!…]\W*$", txt)
            if short and sg[0][0] - prev[-1][1] < 1.6 and len("".join(x[2] for x in prev + sg)) <= max_len:
                merged[-1] = prev + sg; continue
        merged.append(sg)
    return merged


# répliques courtes traduites directement (la traduction automatique les rate souvent)
_SHORT = {"en>fr": {
    "no": "Non.", "no!": "Non !", "no.": "Non.", "yes": "Oui.", "yes.": "Oui.", "yeah": "Ouais.", "yeah.": "Ouais.", "yep": "Ouais.",
    "okay": "D'accord.", "ok": "D'accord.", "okay.": "D'accord.", "all right": "D'accord.", "all right.": "D'accord.", "alright": "D'accord.",
    "right": "D'accord.", "right.": "D'accord.", "what?": "Quoi ?", "what": "Quoi ?", "huh?": "Hein ?", "hey": "Hé !", "hey!": "Hé !",
    "thank you": "Merci.", "thank you.": "Merci.", "thanks": "Merci.", "thanks.": "Merci.", "sorry": "Désolé.", "sorry.": "Désolé.",
    "come on": "Allez !", "come on!": "Allez !", "come on.": "Allez.", "let's go": "Allons-y.", "let's go!": "Allons-y !", "wait": "Attendez !",
    "wait!": "Attendez !", "look!": "Regardez !", "look": "Regardez.", "go!": "Allez !", "stop!": "Arrêtez !", "help!": "À l'aide !",
    "oh, no": "Oh non.", "oh no": "Oh non.", "oh no!": "Oh non !", "good": "Bien.", "good.": "Bien.", "fine": "Bien.", "fine.": "Bien.",
    "really?": "Vraiment ?", "why?": "Pourquoi ?", "who?": "Qui ?", "where?": "Où ?", "how?": "Comment ?", "me?": "Moi ?",
    "hello": "Bonjour.", "hello.": "Bonjour.", "hi": "Salut.", "goodbye": "Au revoir.", "goodbye.": "Au revoir.", "bye": "Salut.",
    "of course": "Bien sûr.", "of course.": "Bien sûr.", "sure": "Bien sûr.", "sure.": "Bien sûr.", "exactly": "Exactement.", "exactly.": "Exactement.",
    "i don't know": "Je ne sais pas.", "i don't know.": "Je ne sais pas.", "i see": "Je vois.", "i see.": "Je vois.",
    "yes, sir": "Oui, monsieur.", "yes, sir.": "Oui, monsieur.", "no, sir": "Non, monsieur.", "no, sir.": "Non, monsieur.",
    "uh-huh": "Hum hum.", "mm-hmm": "Hum hum.", "amen": "Amen.", "amen.": "Amen.", "buck!": "Buck !", "buck?": "Buck ?",
}}


_FR_FIX = [
    (r"\s+([?!:;»])", " \\1"),       # espace insécable avant ? ! : ; »
    (r"«\s*", "« "),
    (r"\s+([.,…])", r"\1"),
    (r"\.\.\.", "…"),
    (r"\s{2,}", " "),
]


def _fr_typo(t):
    t = t.replace("'", "’") if False else t
    for a, b in _FR_FIX:
        t = re.sub(a, b, t)
    return t.strip()


def _wrap2(t, width=42):
    """Coupe un texte en 1 ou 2 lignes équilibrées."""
    if len(t) <= width:
        return t
    best = None
    for m in re.finditer(r" ", t):
        i = m.start(); a, b = t[:i], t[i + 1:]
        if len(a) > width + 6 or len(b) > width + 6: continue
        bonus = (-6 if re.search(r"[,.;:?!…]$", a) else 0) + (12 if a.rsplit(" ", 1)[-1].lower().strip("'’") in _WEAK else 0)
        sc = abs(len(a) - len(b)) + bonus
        if best is None or sc < best[0]: best = (sc, a, b)
    if best is None:
        mid = len(t) // 2; i = t.rfind(" ", 0, mid) if " " in t else mid
        return t[:i] + "\n" + t[i + 1:]
    return best[1] + "\n" + best[2]


_WEAK = set("le la les l un une des du de d au aux à a et ou ni mais que qu qui en dans sur sous par pour avec sans ce cet cette ces "
             "mon ma mes ton ta tes son sa ses notre nos votre vos leur leurs je tu il elle on nous vous ils elles ne n se s me m te t y "
             "très plus moins si the a an of to and".split())


def _split_fr(t, n):
    """Découpe une phrase traduite en n morceaux, de préférence aux signes de ponctuation."""
    if n <= 1: return [t]
    words = t.split(" "); L = len(t); parts = []; start = 0
    for k in range(1, n):
        target = L * k / n; best = None; pos = 0
        for i in range(start + 1, len(words)):
            pos = len(" ".join(words[:i]))
            sc = abs(pos - target) - (14 if re.search(r"[,;:.?!…]$", words[i - 1]) else 0)
            if words[i - 1].lower().strip("'’") in _WEAK or re.search(r"[’']$", words[i - 1]): sc += 25
            if best is None or sc < best[0]: best = (sc, i)
        if best is None: break
        parts.append(" ".join(words[start:best[1]])); start = best[1]
    parts.append(" ".join(words[start:]))
    return [p for p in parts if p.strip()]


def create_subs(video, out_dir=None, audio_lang="en", sub_lang="fr", quality="rapide", log=print, progress=None, cancel=None,
                translator="ia"):
    """Crée un .srt dans sub_lang à partir de l'audio seul."""
    def chk():
        if cancel and cancel(): raise Cancelled()
    size = WHISPER_QUALITY.get(quality, quality)
    log("Création de sous-titres : %s" % os.path.basename(video))
    audio = load_audio(video); chk()
    words = transcribe_words(video, audio, audio_lang, log, cancel=cancel, model_size=size,
                             progress=(lambda f: progress and progress(0.05 + 0.75 * f)))
    del audio
    if not words:
        raise RuntimeError("Aucune parole détectée dans l'audio.")
    sents = _sentences(words)
    src = [re.sub(r"\s+", " ", "".join(w[2] for w in s)).strip() for s in sents]
    gl = read_glossary()
    def opus(texts):
        tr, sp, tp = _load_mt(audio_lang, sub_lang, log)
        def pre(t):
            for a_, b_ in gl:
                if not a_.startswith("~"):
                    t = re.sub(r"(?<!\w)%s(?!\w)" % re.escape(a_), b_, t, flags=re.I)
            return t
        table = _SHORT.get("%s>%s" % (audio_lang, sub_lang), {})
        res_ = [None] * len(texts); todo = []
        for i, t in enumerate(texts):
            k = re.sub(r"\s+", " ", t.strip().lower())
            if k in table: res_[i] = table[k]
            else: todo.append(i)
        batch = [sp.encode(pre(texts[i]), out_type=str) + ["</s>"] for i in todo]
        for k0 in range(0, len(batch), 32):
            chk()
            rr = tr.translate_batch(batch[k0:k0 + 32], beam_size=4, max_decoding_length=200, repetition_penalty=1.15)
            for i, r in zip(todo[k0:k0 + 32], rr):
                res_[i] = tp.decode(r.hypotheses[0])
        return res_
    if sub_lang == audio_lang:
        dst = src
    elif translator == "ia":
        ensure_llm(log)
        log("  traduction de %d phrases par l'IA locale (contexte, tutoiement/vouvoiement, glossaire)…" % len(src))
        dst = llm_translate(src, audio_lang, sub_lang, gl, read_context_note(), log,
                            progress=(lambda f: progress and progress(0.8 + 0.18 * f)), cancel=cancel, fallback=opus)
    else:
        log("  traduction de %d phrases…" % len(src))
        dst = opus(src)
    # post-glossaire (corrections exactes côté traduction)
    def post(t):
        for a, b in gl:
            if a.startswith("~"):  # « ~mauvaise traduction = bonne » : correction après traduction
                t = re.sub(re.escape(a[1:]), b, t, flags=re.I)
        return _fr_typo(t) if sub_lang == "fr" else t.strip()
    items = []
    for s, t in zip(sents, dst):
        t = post(t)
        if not t: continue
        t0, t1 = s[0][0], s[-1][1]
        dur = t1 - t0
        n = max(1, math.ceil(len(t) / 84), math.ceil(dur / 7.0))
        n = min(n, max(1, len(s)))
        chunks = _split_fr(t, n)
        # répartition du temps : sur les mots anglais, proportionnellement à la longueur des morceaux
        tot = sum(len(c) for c in chunks); acc = 0
        for c in chunks:
            a = t0 + dur * acc / tot; acc += len(c); b = t0 + dur * acc / tot
            # recaler sur la frontière de mot anglais la plus proche
            items.append([a, b, _wrap2(c)])
    # durées lisibles : min 1 s, ~17 caractères/s, sans chevaucher le suivant
    items.sort(key=lambda x: x[0])
    for i, it in enumerate(items):
        nxt = items[i + 1][0] if i + 1 < len(items) else it[1] + 10
        need = max(1.0, len(it[2].replace("\n", " ")) / 17.0)
        it[1] = min(max(it[1], it[0] + need), nxt - 0.08, it[0] + 7.0)
        if it[1] - it[0] < 0.5: it[1] = min(it[0] + 0.5, nxt - 0.02)
    out = output_path(video, sub_lang, out_dir)
    if os.path.exists(out):
        out = out[:-4] + ".auto.srt"
    write_srt(out, [tuple(x) for x in items])
    summ = "créés : %d sous-titres, %d phrases (transcription %s, traduction %s)" % (len(items), len(sents), size, "IA locale" if translator == "ia" and sub_lang != audio_lang else "standard")
    log("  " + summ)
    if progress: progress(1.0)
    return [Result(video, out, "création", 0, "warn", summ, dict(sentences=len(sents)), [tuple(x) for x in items])]


def shift_file(path, seconds):
    """Décale tout un fichier .srt (réglage manuel)."""
    subs = read_srt(path)
    write_srt(path, [(x.start.total_seconds() + seconds, x.end.total_seconds() + seconds, x.content) for x in subs])


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="SubSync en ligne de commande")
    ap.add_argument("files", nargs="+", help="synchro : fichier.srt video [video...] ; création (--create) : video [video...]")
    ap.add_argument("--mode", default="auto", choices=["auto", "simple", "advanced"])
    ap.add_argument("--sub-lang", default="fr"); ap.add_argument("--audio-lang", default="en")
    ap.add_argument("--out-dir"); ap.add_argument("--create", action="store_true")
    ap.add_argument("--quality", default="rapide", help="rapide (small) ou précise (medium)")
    ap.add_argument("--translator", default="ia", choices=["ia", "standard"])
    a = ap.parse_args()
    if a.create:
        res = []
        for v in a.files:
            res += create_subs(v, a.out_dir, a.audio_lang, a.sub_lang, a.quality, translator=a.translator)
    else:
        res = run_job(a.files[0], a.files[1:], a.mode, a.sub_lang, a.audio_lang, a.out_dir)
    for r in res:
        print("=>", r.out_path, "|", r.method, "|", r.level, "|", r.summary, "| confiance %.0f" % r.confidence)
