# Installation de l'amélioration vidéo pour SubSync (IA sur la carte graphique via DirectML)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here
$env:UV_PYTHON_INSTALL_DIR = Join-Path $here "python"
$env:UV_CACHE_DIR = Join-Path $here ".uvcache"
$env:UV_PYTHON_PREFERENCE = "only-managed"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = "SilentlyContinue"
function Etape($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }
try {
    if (-not (Test-Path ".venv\Scripts\python.exe")) { throw "Installez d'abord SubSync (Installer SubSync.bat)." }
    $uv = Join-Path $here "bin\uv.exe"
    if (-not (Test-Path $uv)) {
        $zip = Join-Path $env:TEMP "uv-subsync.zip"
        Invoke-WebRequest -UseBasicParsing "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip" -OutFile $zip
        Expand-Archive -Force $zip (Join-Path $here "bin"); Remove-Item $zip
    }
    Etape "1/3  Environnement de calcul vidéo (onnxruntime DirectML pour carte AMD / NVIDIA / Intel)"
    & $uv python install 3.11
    if (-not (Test-Path ".venv_video\Scripts\python.exe")) {
        & $uv venv --python 3.11 .venv_video
        if ($LASTEXITCODE -ne 0) { throw "échec de la création de l'environnement vidéo" }
    }
    & $uv pip install --python ".venv_video\Scripts\python.exe" onnxruntime-directml numpy opencv-python-headless
    if ($LASTEXITCODE -ne 0) { throw "échec de l'installation des bibliothèques" }

    Etape "2/3  Vérification de la carte graphique"
    & ".venv_video\Scripts\python.exe" -c "import onnxruntime as o; p=o.get_available_providers(); print('Moteurs disponibles :', ', '.join(p)); print('Carte graphique utilisable : ' + ('OUI' if 'DmlExecutionProvider' in p else 'NON (le calcul sera très lent)'))"

    Etape "3/3  Téléchargement des modèles de visages (~630 Mo)"
    $dir = Join-Path $here "models\video"
    New-Item -ItemType Directory -Force $dir | Out-Null
    foreach ($m in @("scrfd_2.5g.onnx", "gfpgan_1.4.onnx", "gpen_bfr_512.onnx")) {
        $dst = Join-Path $dir $m
        if ((Test-Path $dst) -and ((Get-Item $dst).Length -gt 1000000)) { Write-Host "  $m : déjà présent"; continue }
        Write-Host "  $m …"
        Invoke-WebRequest -UseBasicParsing "https://github.com/facefusion/facefusion-assets/releases/download/models-3.0.0/$m" -OutFile ($dst + ".part")
        Move-Item -Force ($dst + ".part") $dst
    }
    Write-Host ""
    Write-Host "Amélioration vidéo installée. Dans SubSync, choisissez le mode « Améliorer »." -ForegroundColor Green
}
catch {
    Write-Host ""
    Write-Host "ERREUR : $_" -ForegroundColor Red
    Write-Host "Copiez ce message et envoyez-le-moi."
}
Write-Host ""
Read-Host "Appuyez sur Entrée pour fermer"
