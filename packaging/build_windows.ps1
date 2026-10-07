$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
$qemuImg = Join-Path $env:ProgramFiles 'qemu\qemu-img.exe'
if ($env:QEMU_IMG) { $qemuImg = $env:QEMU_IMG }
python packaging/bundle_qemu.py --binary $qemuImg --firmware build/firmware
if ($LASTEXITCODE -ne 0) { throw 'QEMU runtime bundle failed' }
python packaging/build_icons.py
if ($LASTEXITCODE -ne 0) { throw 'Application icon build failed' }
$qtTranslations = python -c 'from PySide6.QtCore import QLibraryInfo; print(QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))'
python -m PyInstaller --noconfirm --clean --name Multilayer --windowed --onedir --icon build/multilayer.ico --collect-submodules pycdlib --hidden-import win32pipe --hidden-import win32file --add-data 'build/qemu-runtime:qemu' --add-data 'multilayer/assets:multilayer/assets' --add-data "${qtTranslations}/qtbase_ru.qm:qt-translations" packaging/entry.py
if ($LASTEXITCODE -ne 0) { throw 'GUI build failed' }
python -m PyInstaller --noconfirm --clean --name multilayer-cli --console --onedir --icon build/multilayer.ico --collect-submodules pycdlib --hidden-import win32pipe --hidden-import win32file --add-data 'build/qemu-runtime:qemu' --add-data 'multilayer/assets:multilayer/assets' --add-data "${qtTranslations}/qtbase_ru.qm:qt-translations" packaging/cli_entry.py
if ($LASTEXITCODE -ne 0) { throw 'CLI build failed' }
python packaging/smoke_bundled_img.py dist/multilayer-cli/multilayer-cli.exe
if ($LASTEXITCODE -ne 0) { throw 'Bundled QEMU runtime smoke test failed' }
$version = python -c 'from multilayer import __version__; print(__version__)'
$iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if (-not $iscc) { $iscc = Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe' }
& $iscc "/DAppVersion=$version" packaging/windows.iss
if ($LASTEXITCODE -ne 0) { throw 'Installer build failed' }
