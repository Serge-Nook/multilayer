$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
python -m PyInstaller --noconfirm --clean --name Multilayer --windowed --onedir --collect-submodules pycdlib --hidden-import win32pipe --hidden-import win32file packaging/entry.py
if ($LASTEXITCODE -ne 0) { throw 'GUI build failed' }
python -m PyInstaller --noconfirm --clean --name multilayer-cli --console --onedir --collect-submodules pycdlib --hidden-import win32pipe --hidden-import win32file packaging/cli_entry.py
if ($LASTEXITCODE -ne 0) { throw 'CLI build failed' }
$version = python -c 'from multilayer import __version__; print(__version__)'
$iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if (-not $iscc) { $iscc = Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe' }
& $iscc "/DAppVersion=$version" packaging/windows.iss
if ($LASTEXITCODE -ne 0) { throw 'Installer build failed' }
