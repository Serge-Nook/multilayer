#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python packaging/bundle_qemu.py --binary "$(command -v qemu-img)"
python -m PyInstaller --noconfirm --clean --name multilayer --onedir --collect-submodules pycdlib --add-data build/qemu-runtime:qemu packaging/entry.py
python packaging/smoke_bundled_img.py dist/multilayer/multilayer
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
Depends: libc6 (>= 2.35)
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
HERE=${APPDIR:-${0%/*}}
exec "$HERE/usr/bin/multilayer" "$@"
EOF
chmod +x "$appdir/AppRun"
if [ -n "${APPIMAGETOOL:-}" ]; then
  ARCH=x86_64 "$APPIMAGETOOL" --appimage-extract-and-run "$appdir" "dist/Multilayer-${version}-x86_64.AppImage"
  python packaging/smoke_bundled_img.py "dist/Multilayer-${version}-x86_64.AppImage"
else
  echo "DEB and AppDir built. Set APPIMAGETOOL to an official appimagetool AppImage to produce AppImage." >&2
fi
