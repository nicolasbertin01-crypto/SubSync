# Installation de SubSync (a lancer via "Installer SubSync.bat")
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here
$env:UV_PYTHON_INSTALL_DIR = Join-Path $here "python"
$env:UV_CACHE_DIR = Join-Path $here ".uvcache"
$env:UV_PYTHON_PREFERENCE = "only-managed"
$uv = Join-Path $here "bin\uv.exe"
if (-not (Test-Path $uv)) {
    Write-Host "Téléchargement de l'outil d'installation uv…"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $zip = Join-Path $env:TEMP "uv-subsync.zip"
    Invoke-WebRequest -UseBasicParsing "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip" -OutFile $zip
    Expand-Archive -Force $zip (Join-Path $here "bin")
    Remove-Item $zip
}

function Etape($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }

try {
    Write-Host "Installation de SubSync dans : $here" -ForegroundColor Green
    Etape "1/4  Installation de Python 3.11 (privé, n'affecte pas le reste du PC)"
    & $uv python install 3.11
    if ($LASTEXITCODE -ne 0) { throw "échec de l'installation de Python" }

    Etape "2/4  Création de l'environnement"
    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        & $uv venv --python 3.11 .venv
        if ($LASTEXITCODE -ne 0) { throw "échec de la création de l'environnement" }
    }

    Etape "3/4  Installation des bibliothèques (quelques minutes)"
    & $uv pip install --python ".venv\Scripts\python.exe" -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "échec de l'installation des bibliothèques" }
    & ".venv\Scripts\python.exe" -c "import customtkinter, tkinterdnd2, onnxruntime, faster_whisper, ctranslate2, imageio_ffmpeg, subsync_engine; print('Bibliothèques OK')"
    if ($LASTEXITCODE -ne 0) { throw "une bibliothèque ne se charge pas" }

    Etape "4/4  Raccourcis"
    $sh = New-Object -ComObject WScript.Shell
    $targets = @([Environment]::GetFolderPath("Desktop"), (Join-Path ([Environment]::GetFolderPath("Programs")) ""))
    foreach ($d in $targets) {
        $lnk = $sh.CreateShortcut((Join-Path $d "SubSync.lnk"))
        $lnk.TargetPath = (Join-Path $here ".venv\Scripts\pythonw.exe")
        $lnk.Arguments = '"' + (Join-Path $here "app.py") + '"'
        $lnk.WorkingDirectory = $here
        $lnk.IconLocation = (Join-Path $here "assets\subsync.ico")
        $lnk.Description = "SubSync - synchronisation automatique de sous-titres"
        $lnk.Save()
    }
    Write-Host "Raccourci 'SubSync' créé sur le Bureau et dans le menu Démarrer."

    Write-Host ""
    $r = Read-Host "Télécharger dès maintenant les modèles des modes Avancé et Créer (~1 Go) ? [O/N]"
    if ($r -match "^[oOyY]") {
        & ".venv\Scripts\python.exe" -c "import subsync_engine as e; [e._hf_download(m, print) for m in ('Systran/faster-whisper-base.en','michaelfeil/ct2fast-opus-mt-fr-en','Systran/faster-whisper-small.en','michaelfeil/ct2fast-opus-mt-en-fr')]; print('Modèles OK')"
    } else {
        Write-Host "Ils seront téléchargés automatiquement à la première utilisation."
    }
    Write-Host ""
    Write-Host "Installation terminée. Lancez SubSync depuis le raccourci du Bureau." -ForegroundColor Green
}
catch {
    Write-Host ""
    Write-Host "ERREUR : $_" -ForegroundColor Red
    Write-Host "Copiez ce message et envoyez-le-moi."
}
Write-Host ""
Read-Host "Appuyez sur Entrée pour fermer"
