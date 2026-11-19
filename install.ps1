# MG Shorts Studio - installation Windows (a lancer via install.bat)
# Installe ce qui manque : Python, ffmpeg, Whisper (whisper.cpp), le modele, les musiques.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$App = Split-Path -Parent $MyInvocation.MyCommand.Path
$Desk = [Environment]::GetFolderPath("Desktop")
$Bin = Join-Path $App "bin"
New-Item -ItemType Directory -Force -Path $Bin, (Join-Path $App "models"), (Join-Path $App "musiques"), (Join-Path $App "projets") | Out-Null

function Step($m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor Yellow }
function Ok($m) { Write-Host "    OK  $m" -ForegroundColor Green }

# 1. Python ---------------------------------------------------------------
Step "Python"
$py = $null
foreach ($c in @("py", "python")) {
  try { $v = & $c --version 2>&1; if ($LASTEXITCODE -eq 0 -and "$v" -match "Python 3") { $py = $c; break } } catch {}
}
if (-not $py) {
  Write-Host "    Python absent : installation avec winget..."
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
  $py = "py"
}
Ok "Python ($py)"

# 1b. OpenCV (detection des visages pour recadrer les videos longues en vertical)
Step "Detection des visages (OpenCV)"
$ErrorActionPreference = "Continue"
& $py -m pip install --user --disable-pip-version-check -q "opencv-python-headless<5" 2>&1 | Out-Null
& $py -c "import cv2" 2>$null
if ($LASTEXITCODE -eq 0) { Ok "OpenCV" } else { Write-Host "    OpenCV non installe : les videos longues seront recadrees au centre." -ForegroundColor DarkYellow }
$ErrorActionPreference = "Stop"

# 2. ffmpeg (build BtbN, avec libass pour les sous-titres) ---------------
Step "ffmpeg"
if (-not (Test-Path (Join-Path $Bin "ffmpeg.exe"))) {
  $zip = Join-Path $env:TEMP "ffmpeg-mg.zip"
  Write-Host "    Telechargement de ffmpeg (~130 Mo)..."
  Invoke-WebRequest "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip" -OutFile $zip
  $tmp = Join-Path $env:TEMP "ffmpeg-mg"; Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
  Expand-Archive $zip $tmp -Force
  Get-ChildItem $tmp -Recurse -Include ffmpeg.exe, ffprobe.exe | Copy-Item -Destination $Bin -Force
  Remove-Item -Recurse -Force $tmp, $zip
}
Ok "ffmpeg"

# 3. Whisper (whisper.cpp ; version GPU si une carte NVIDIA est presente) --
Step "Whisper"
$wdir = Join-Path $Bin "whisper"
$wexe = Get-ChildItem $wdir -Recurse -Include whisper-cli.exe -ErrorAction SilentlyContinue | Select-Object -First 1
function Get-Whisper($pattern) {
  # Les binaires Windows sont publies dans les pre-releases "bNNNN" : on prend la plus recente qui a l'archive voulue
  $rels = Invoke-RestMethod "https://api.github.com/repos/ggml-org/whisper.cpp/releases?per_page=30" -Headers @{ "User-Agent" = "mg-shorts" }
  foreach ($r in $rels) {
    $a = $r.assets | Where-Object { $_.name -match $pattern } | Select-Object -First 1
    if ($a) {
      Write-Host "    Telechargement de $($a.name) ($($r.tag_name))..."
      $zip = Join-Path $env:TEMP "whisper-mg.zip"
      Invoke-WebRequest $a.browser_download_url -OutFile $zip
      Remove-Item -Recurse -Force $wdir -ErrorAction SilentlyContinue
      Expand-Archive $zip $wdir -Force; Remove-Item $zip
      $exe = Get-ChildItem $wdir -Recurse -Include whisper-cli.exe | Select-Object -First 1
      if (-not $exe) { $exe = Get-ChildItem $wdir -Recurse -Include main.exe | Select-Object -First 1 }
      return $exe
    }
  }
  return $null
}
function Test-Whisper($exe) {
  if (-not $exe) { return $false }
  $ErrorActionPreference = "Continue"  # whisper ecrit son aide sur stderr : ne pas la prendre pour une erreur
  try { $null = & $exe.FullName --help 2>&1; return ($LASTEXITCODE -eq 0) } catch { return $false }
}
# Carte Nvidia mais Whisper installe en version processeur : on passe a la version carte graphique (5 a 10 fois plus rapide)
$hasNv = [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue)
$gpuW = Get-ChildItem $wdir -Recurse -Include "*cuda*.dll", "*cublas*.dll" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($hasNv -and $wexe -and -not $gpuW) { Write-Host "    Carte Nvidia detectee : passage a Whisper version carte graphique."; $wexe = $null }
if (-not (Test-Whisper $wexe)) {
  $wexe = $null
  if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $wexe = Get-Whisper "^whisper-bin-win-cuda-12[.0-9]*-x64\.zip$|cublas-12.*bin-x64\.zip$"
    if (-not (Test-Whisper $wexe)) { Write-Host "    Version carte graphique inutilisable ici, passage a la version processeur."; $wexe = $null }
  }
  if (-not $wexe) { $wexe = Get-Whisper "^whisper-bin-x64\.zip$" }
  if (-not (Test-Whisper $wexe)) { throw "Whisper n'a pas pu etre installe. Copie ce message a Claude." }
}
Ok "Whisper : $($wexe.FullName)"

# 4. Modele Whisper ------------------------------------------------------
Step "Modele Whisper (large-v3-turbo q5_0)"
$model = Join-Path $App "models\ggml-large-v3-turbo-q5_0.bin"
if (-not (Test-Path $model)) {
  $onDesk = Join-Path $Desk "ggml-large-v3-turbo-q5_0.bin"
  if (Test-Path $onDesk) { Copy-Item $onDesk $model; Write-Host "    Copie depuis le Bureau." }
  else {
    Write-Host "    Telechargement du modele (~550 Mo)..."
    Invoke-WebRequest "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin" -OutFile $model
  }
}
Ok "Modele"

# 5. Musiques de reference ----------------------------------------------
Step "Musiques"
$mdir = Join-Path $App "musiques"
Get-ChildItem $Desk -Filter "ES_*.wav" -ErrorAction SilentlyContinue | ForEach-Object {
  $dst = Join-Path $mdir $_.Name
  if (-not (Test-Path $dst)) { Copy-Item $_.FullName $dst; Write-Host "    + $($_.Name)" }
}
Ok "$((Get-ChildItem $mdir -File).Count) musique(s) dans $mdir"

# 6. config.json --------------------------------------------------------
Step "Configuration"
$cfgPath = Join-Path $App "config.json"
$cfg = @{}
if (Test-Path $cfgPath) { (Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json).psobject.Properties | ForEach-Object { $cfg[$_.Name] = $_.Value } }
$cfg["ffmpeg"] = "bin/ffmpeg.exe"; $cfg["ffprobe"] = "bin/ffprobe.exe"
$cfg["whisper"] = $wexe.FullName
$cfg["model"] = "models/ggml-large-v3-turbo-q5_0.bin"
if (-not $cfg["sfx_dir"]) { $s = Join-Path $Desk "SoundsPack - MG 2026"; if (Test-Path $s) { $cfg["sfx_dir"] = $s } }
if (-not $cfg["port"]) { $cfg["port"] = 8765 }
if (-not $cfg["projects_dir"]) { $cfg["projects_dir"] = "projets" }
if (-not $cfg["anthropic_api_key"]) {
  Write-Host ""
  Write-Host "    Cle API Claude (pour que l'outil trouve seul les meilleurs moments des videos longues)."
  Write-Host "    Colle-la puis Entree. Sans cle, appuie juste sur Entree : tu feras un copier-coller dans Claude."
  $k = Read-Host "    Cle API"
  $cfg["anthropic_api_key"] = "$k".Trim()
}
$json = $cfg | ConvertTo-Json -Depth 5
[IO.File]::WriteAllText($cfgPath, $json, (New-Object Text.UTF8Encoding $false))
Ok "config.json"

# 7. Raccourci sur le Bureau ---------------------------------------------
Step "Raccourci"
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut((Join-Path $Desk "MG Shorts Studio.lnk"))
$lnk.TargetPath = Join-Path $App "lancer.bat"; $lnk.WorkingDirectory = $App; $lnk.WindowStyle = 7
$lnk.Save()
Ok "Raccourci 'MG Shorts Studio' cree sur le Bureau"

Write-Host ""; Write-Host "Installation terminee. Double-clique sur 'MG Shorts Studio' sur ton Bureau." -ForegroundColor Green
