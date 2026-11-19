# MG Shorts Studio - mise a jour automatique (lancee par lancer.bat avant le demarrage)
# Compare le fichier VERSION local avec celui de GitHub ; si une nouvelle version existe, la telecharge
# et remplace les fichiers de l'appli. Ne touche jamais a config.json, projets, musiques, bin, models\*.bin.
# Pour la couper : mettre "auto_update": false dans config.json.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$App = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo = "MGPROD01/MG-SHORT"
try {
  $cfgPath = Join-Path $App "config.json"
  if (Test-Path $cfgPath) {
    $cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($cfg.auto_update -eq $false) { exit 0 }
  }
  $local = ""; $vf = Join-Path $App "VERSION"
  if (Test-Path $vf) { $local = (Get-Content $vf -Raw).Trim() }
  $remote = (Invoke-WebRequest "https://raw.githubusercontent.com/$Repo/main/VERSION?t=$([DateTime]::UtcNow.Ticks)" -UseBasicParsing -TimeoutSec 8).Content.Trim()
  if (-not $remote -or $remote -eq $local) { exit 0 }
  Write-Host "Mise a jour de MG Shorts Studio : $local -> $remote ..." -ForegroundColor Yellow
  $zip = Join-Path $env:TEMP "mg-shorts-maj.zip"; $tmp = Join-Path $env:TEMP "mg-shorts-maj"
  Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
  Invoke-WebRequest "https://codeload.github.com/$Repo/zip/refs/heads/main" -OutFile $zip -UseBasicParsing -TimeoutSec 120
  Expand-Archive $zip $tmp -Force
  $src = Get-ChildItem $tmp -Directory | Select-Object -First 1
  # lancer.bat est en cours d'execution : on ne le remplace jamais (cmd le lit au fil de l'eau)
  Get-ChildItem $src.FullName -Recurse -File | Where-Object { $_.Name -ne "lancer.bat" } | ForEach-Object {
    $rel = $_.FullName.Substring($src.FullName.Length + 1)
    $dst = Join-Path $App $rel
    New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
    Copy-Item $_.FullName $dst -Force
  }
  Remove-Item -Recurse -Force $tmp, $zip -ErrorAction SilentlyContinue
  Write-Host "    OK, version $remote installee." -ForegroundColor Green
} catch {
  Write-Host "Mise a jour impossible pour l'instant (pas d'internet ?). On demarre avec la version actuelle." -ForegroundColor DarkYellow
}
exit 0
