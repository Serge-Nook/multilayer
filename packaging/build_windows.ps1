$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
$qemuImg = Join-Path $env:ProgramFiles 'qemu\qemu-img.exe'
if ($env:QEMU_IMG) { $qemuImg = $env:QEMU_IMG }
python packaging/bundle_qemu.py --binary $qemuImg --firmware build/firmware
if ($LASTEXITCODE -ne 0) { throw 'qemu-img bundle failed' }
python -m PyInstaller --noconfirm --clean --name Multilayer --windowed --onedir --collect-submodules pycdlib --hidden-import win32pipe --hidden-import win32file --add-data 'build/qemu-runtime:qemu' packaging/entry.py
if ($LASTEXITCODE -ne 0) { throw 'GUI build failed' }
python -m PyInstaller --noconfirm --clean --name multilayer-cli --console --onedir --collect-submodules pycdlib --hidden-import win32pipe --hidden-import win32file --add-data 'build/qemu-runtime:qemu' packaging/cli_entry.py
if ($LASTEXITCODE -ne 0) { throw 'CLI build failed' }
python packaging/smoke_bundled_img.py dist/multilayer-cli/multilayer-cli.exe
if ($LASTEXITCODE -ne 0) { throw 'Bundled qemu-img smoke test failed' }
$version = python -c 'from multilayer import __version__; print(__version__)'
$iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if (-not $iscc) { $iscc = Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe' }
& $iscc "/DAppVersion=$version" packaging/windows.iss
if ($LASTEXITCODE -ne 0) { throw 'Installer build failed' }
