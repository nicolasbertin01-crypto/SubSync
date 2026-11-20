# Installation de l'IA de traduction pour SubSync (llama.cpp + Mistral Small 24B)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here
$build = "b12541"
try {
    if (-not (Test-Path ".venv\Scripts\python.exe")) { throw "Installez d'abord SubSync (Installer SubSync.bat)." }
    $bin = Join-Path $here "llm\bin"
    New-Item -ItemType Directory -Force $bin | Out-Null
    $srv = Get-ChildItem -Path $bin -Recurse -Filter "llama-server.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $srv) {
        Write-Host "==> 1/3  Téléchargement du moteur llama.cpp (Vulkan, pour carte graphique AMD/NVIDIA/Intel)" -ForegroundColor Cyan
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $zip = Join-Path $env:TEMP "llama-vulkan.zip"
        $ProgressPreference = "SilentlyContinue"
        Invoke-WebRequest -UseBasicParsing "https://github.com/ggml-org/llama.cpp/releases/download/$build/llama-$build-bin-win-vulkan-x64.zip" -OutFile $zip
        Expand-Archive -Force $zip $bin
        Remove-Item $zip
        $srv = Get-ChildItem -Path $bin -Recurse -Filter "llama-server.exe" | Select-Object -First 1
    } else { Write-Host "==> 1/3  Moteur llama.cpp déjà présent" -ForegroundColor Cyan }

    Write-Host ""
    Write-Host "==> 2/3  Vérification de la carte graphique" -ForegroundColor Cyan
    & $srv.FullName --list-devices 2>&1 | Out-Host

    Write-Host ""
    Write-Host "==> 3/3  Téléchargement du modèle Mistral Small 3.2 24B (14,3 Go — peut prendre longtemps)" -ForegroundColor Cyan
    $env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
    & ".venv\Scripts\python.exe" -c "import os; from huggingface_hub import hf_hub_download; p=hf_hub_download('bartowski/mistralai_Mistral-Small-3.2-24B-Instruct-2506-GGUF','mistralai_Mistral-Small-3.2-24B-Instruct-2506-Q4_K_M.gguf',local_dir=os.path.join('llm','models')); print('Modèle OK :', p)"
    if ($LASTEXITCODE -ne 0) { throw "échec du téléchargement du modèle (relancez ce fichier : le téléchargement reprendra)" }
    Write-Host ""
    Write-Host "IA de traduction installée. Dans SubSync, mode « Créer », choisissez « IA locale (qualité) »." -ForegroundColor Green
}
catch {
    Write-Host ""
    Write-Host "ERREUR : $_" -ForegroundColor Red
    Write-Host "Copiez ce message et envoyez-le-moi."
}
Write-Host ""
Read-Host "Appuyez sur Entrée pour fermer"
