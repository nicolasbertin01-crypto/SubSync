# -*- coding: utf-8 -*-
"""SubSync - mises à jour depuis le dépôt GitHub (code, scripts, modèles validés) et des moteurs (bibliothèques)."""
import os, sys, json, shutil, zipfile, tempfile, subprocess, urllib.request, urllib.error, time, datetime

APP_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = "nicolasbertin01-crypto/subsync"
BRANCH = "main"
API = "https://api.github.com/repos/%s" % REPO
RAW = "https://raw.githubusercontent.com/%s/%s/" % (REPO, BRANCH)          # dépôt public : sans limite d'appels
ZIP = "https://codeload.github.com/%s/zip/refs/heads/%s" % (REPO, BRANCH)
# fichiers personnels jamais écrasés s'ils existent déjà
KEEP_IF_EXISTS = {"glossaire.txt", "reglages.json", "file_attente.json"}
# dossiers locaux jamais touchés
SKIP_DIRS = {".git", ".venv", ".venv_video", "python", ".uvcache", "cache", "logs", "llm", "backup_maj", "__pycache__"}
NOWIN = 0x08000000 if os.name == "nt" else 0
SETTINGS = os.path.join(APP_DIR, "reglages.json")


def local_version():
    try:
        with open(os.path.join(APP_DIR, "VERSION"), encoding="utf-8") as f: return f.read().strip()
    except OSError:
        return "0"


def _vtuple(v):
    out = []
    for p in v.strip().lstrip("v").split("."):
        try: out.append(int(p))
        except ValueError: out.append(0)
    return tuple(out)


def settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f: return json.load(f)
    except Exception:
        return {}


def save_settings(**kw):
    d = settings(); d.update(kw)
    with open(SETTINGS, "w", encoding="utf-8") as f: json.dump(d, f, ensure_ascii=False, indent=1)


def _req(url, raw=False, timeout=30):
    h = {"User-Agent": "SubSync", "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json"}
    tok = settings().get("github_token", "").strip()
    if tok: h["Authorization"] = "Bearer " + tok
    return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout)


def _get(name, raw=True, timeout=30):
    """Fichier du dépôt : adresse publique directe, sinon API GitHub (avec la clé d'accès si dépôt privé)."""
    if not settings().get("github_token", "").strip():
        try:
            return urllib.request.urlopen(urllib.request.Request(
                (ZIP if name is None else RAW + name), headers={"User-Agent": "SubSync"}), timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code != 404: raise
    return _req(API + "/zipball/" + BRANCH if name is None else API + "/contents/%s?ref=%s" % (name, BRANCH), raw=raw, timeout=timeout)


def check():
    """Renvoie (version distante, notes) ; lève une exception explicite en cas de problème d'accès."""
    try:
        v = _get("VERSION").read().decode("utf-8").strip()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            raise RuntimeError("accès au dépôt refusé : dépôt privé sans clé d'accès valide (voir « Clé d'accès GitHub »)")
        raise RuntimeError("GitHub a répondu %d" % e.code)
    except urllib.error.URLError as e:
        raise RuntimeError("pas de connexion à GitHub (%s)" % e.reason)
    notes = ""
    try: notes = _get("NOUVEAUTES.txt").read().decode("utf-8")
    except Exception: pass
    save_settings(last_check=time.time())
    return v, notes


def is_newer(remote):
    return _vtuple(remote) > _vtuple(local_version())


def install(log=print):
    """Télécharge la version du dépôt et remplace les fichiers du programme (sauvegarde de l'ancienne version).
    Renvoie (nouvelle version, dépendances changées ?)."""
    old = local_version()
    tmp = tempfile.mkdtemp(prefix="subsync_maj_")
    try:
        log("téléchargement de la mise à jour…")
        zp = os.path.join(tmp, "maj.zip")
        with _get(None, timeout=300) as r, open(zp, "wb") as f: shutil.copyfileobj(r, f)
        with zipfile.ZipFile(zp) as z: z.extractall(tmp)
        roots = [d for d in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, d))]
        if len(roots) != 1: raise RuntimeError("archive de mise à jour inattendue")
        src = os.path.join(tmp, roots[0])
        if not os.path.exists(os.path.join(src, "app.py")): raise RuntimeError("archive de mise à jour incomplète")
        with open(os.path.join(src, "VERSION"), encoding="utf-8") as f: new = f.read().strip()
        req_changed = _read(os.path.join(src, "requirements.txt")) != _read(os.path.join(APP_DIR, "requirements.txt"))
        backup = os.path.join(APP_DIR, "backup_maj", old)
        n = 0
        for dp, dns, fns in os.walk(src):
            rel = os.path.relpath(dp, src)
            dns[:] = [d for d in dns if d not in SKIP_DIRS]
            for fn in fns:
                r_ = os.path.normpath(os.path.join(rel, fn))
                dst = os.path.join(APP_DIR, r_)
                if r_ in KEEP_IF_EXISTS and os.path.exists(dst): continue
                s_ = os.path.join(dp, fn)
                if os.path.exists(dst):
                    if _read(dst, binary=True) == _read(s_, binary=True): continue
                    b = os.path.join(backup, r_); os.makedirs(os.path.dirname(b), exist_ok=True); shutil.copy2(dst, b)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(s_, dst); n += 1
        log("%d fichier(s) mis à jour (ancienne version sauvegardée dans backup_maj\\%s)" % (n, old))
        if req_changed:
            log("nouvelles bibliothèques nécessaires : installation…")
            update_engines(log, main_only=True)
        return new, req_changed
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def rollback(log=print):
    """Remet la dernière version sauvegardée."""
    base = os.path.join(APP_DIR, "backup_maj")
    if not os.path.isdir(base) or not os.listdir(base): raise RuntimeError("aucune version précédente sauvegardée")
    ver = sorted(os.listdir(base), key=_vtuple)[-1]; src = os.path.join(base, ver)
    for dp, _, fns in os.walk(src):
        for fn in fns:
            s_ = os.path.join(dp, fn); dst = os.path.join(APP_DIR, os.path.relpath(s_, src))
            os.makedirs(os.path.dirname(dst), exist_ok=True); shutil.copy2(s_, dst)
    with open(os.path.join(APP_DIR, "VERSION"), "w", encoding="utf-8") as f: f.write(ver + "\n")
    shutil.rmtree(src, ignore_errors=True)
    log("version %s remise en place" % ver)
    return ver


def _read(p, binary=False):
    try:
        with open(p, "rb") as f: d = f.read()
        return d if binary else d.decode("utf-8", errors="replace").replace("\r\n", "\n")
    except OSError:
        return None


def _uv():
    u = os.path.join(APP_DIR, "bin", "uv.exe" if os.name == "nt" else "uv")
    return u if os.path.exists(u) else shutil.which("uv")


def _run(cmd, log):
    env = dict(os.environ, UV_CACHE_DIR=os.path.join(APP_DIR, ".uvcache"), UV_PYTHON_INSTALL_DIR=os.path.join(APP_DIR, "python"))
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, creationflags=NOWIN, encoding="utf-8", errors="replace")
    out = (p.stdout + p.stderr).strip().splitlines()
    for line in out[-6:]: log("  " + line)
    return p.returncode == 0


ENV_TESTS = {
    ".venv": "import numpy, onnxruntime, faster_whisper, ctranslate2, customtkinter, av, srt; print('ok')",
    ".venv_video": "import numpy, cv2, onnxruntime as o; print('ok')",
}
ENV_PKGS = {
    ".venv": ["-r", os.path.join(APP_DIR, "requirements.txt")],
    ".venv_video": ["onnxruntime-directml", "numpy", "opencv-python-headless"],
}


def update_engines(log=print, main_only=False):
    """Met à jour les bibliothèques de calcul ; en cas d'échec du test, remet exactement les versions précédentes."""
    uv = _uv()
    if not uv: raise RuntimeError("outil d'installation uv introuvable (relancez « Installer SubSync.bat »)")
    ok_all = True
    for env in ([".venv"] if main_only else [".venv", ".venv_video"]):
        py = os.path.join(APP_DIR, env, "Scripts", "python.exe") if os.name == "nt" else os.path.join(APP_DIR, env, "bin", "python")
        if not os.path.exists(py): continue
        log("moteurs « %s » : sauvegarde des versions actuelles…" % env)
        frz = subprocess.run([uv, "pip", "freeze", "--python", py], capture_output=True, text=True, creationflags=NOWIN).stdout
        fp = os.path.join(APP_DIR, "backup_maj", "moteurs_%s_%s.txt" % (env.strip("."), datetime.date.today().isoformat()))
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f: f.write(frz)
        log("moteurs « %s » : mise à jour…" % env)
        ok = _run([uv, "pip", "install", "--python", py, "--upgrade"] + ENV_PKGS[env], log)
        test = subprocess.run([py, "-c", ENV_TESTS[env]], capture_output=True, text=True, creationflags=NOWIN)
        if not ok or test.returncode != 0 or "ok" not in test.stdout:
            log("  problème après la mise à jour : retour aux versions précédentes…")
            _run([uv, "pip", "install", "--python", py, "-r", fp, "--reinstall"], log)
            ok_all = False
        else:
            log("  moteurs « %s » à jour et testés." % env)
    return ok_all


def due(days=1):
    """Vrai si la dernière vérification date de plus de `days` jours."""
    return time.time() - float(settings().get("last_check", 0)) > days * 86400
