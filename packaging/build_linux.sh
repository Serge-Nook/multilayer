#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python -m PyInstaller --noconfirm --clean --name multilayer --onedir --collect-submodules pycdlib packaging/entry.py
version=$(python -c 'from multilayer import __version__; print(__version__)')
stage="build/deb"
rm -rf "$stage"
mkdir -p "$stage/DEBIAN" "$stage/opt/multilayer" "$stage/usr/bin" "$stage/usr/share/applications" "$stage/usr/share/icons/hicolor/scalable/apps"
cp -a dist/multilayer/. "$stage/opt/multilayer/"
ln -s /opt/multilayer/multilayer "$stage/usr/bin/multilayer"
cp packaging/multilayer.desktop "$stage/usr/share/applications/"
cp packaging/multilayer.svg "$stage/usr/share/icons/hicolor/scalable/apps/"
cat > "$stage/DEBIAN/control" <<EOF
Package: multilayer
Version: $version
Section: misc
Priority: optional
Architecture: amd64
Maintainer: Горшков Сергей Владимирович <nookbat@gmail.com>
Homepage: https://nookbat.ru
Depends: libc6 (>= 2.35), libegl1, libgl1, libxkbcommon0, libxkbcommon-x11-0, libxcb-cursor0, libxcb-icccm4, libxcb-image0, libxcb-keysyms1, libxcb-render-util0, libxcb-xinerama0, libxcb-xkb1, libxcb-shape0, libdbus-1-3, qemu-system-x86 (>= 1:6.2), qemu-system-gui, qemu-utils
Recommends: ovmf, swtpm, bpftool, pkexec, iproute2
Description: Мультислой — локальные виртуальные машины QEMU/KVM
 GUI и CLI, ISO, QCOW2, снимки дисков и управление сетью.
EOF
dpkg-deb --root-owner-group --build "$stage" "dist/multilayer_${version}_amd64.deb"
appdir="build/Multilayer.AppDir"
rm -rf "$appdir"
mkdir -p "$appdir/usr/bin"
cp -a dist/multilayer/. "$appdir/usr/bin/"
cp packaging/multilayer.desktop "$appdir/"
cp packaging/multilayer.svg "$appdir/"
cat > "$appdir/AppRun" <<'EOF'
#!/bin/sh
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$HERE/usr/bin/multilayer" "$@"
EOF
chmod +x "$appdir/AppRun"
if [ -n "${APPIMAGETOOL:-}" ]; then
  ARCH=x86_64 "$APPIMAGETOOL" --appimage-extract-and-run "$appdir" "dist/Multilayer-${version}-x86_64.AppImage"
else
  echo "DEB and AppDir built. Set APPIMAGETOOL to an official appimagetool AppImage to produce AppImage." >&2
fi
