param([string]$PythonExe = 'python')

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$release = Join-Path $root 'release'
$dist = Join-Path $root 'dist'
Set-Location $root
Remove-Item -Recurse -Force $release, $dist -ErrorAction SilentlyContinue
& $PythonExe -m PyInstaller --noconfirm --clean --onedir --console --name jar-deploy-manager --add-data "static;static" --collect-all flask_socketio --collect-all engineio --collect-all socketio app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
New-Item -ItemType Directory -Force $release | Out-Null
$zip = Join-Path $release 'jar-deploy-manager-windows-x64-v0.1.0.zip'
Compress-Archive -Path (Join-Path $dist 'jar-deploy-manager\*') -DestinationPath $zip
(Get-FileHash $zip -Algorithm SHA256).Hash.ToLowerInvariant() + '  ' + (Split-Path $zip -Leaf) | Set-Content -NoNewline ("$zip.sha256")
