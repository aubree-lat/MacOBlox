#!/usr/bin/env bash
# Shared staging step for Debian and RPM builds. Never installs to the host.
set -euo pipefail
if [[ $# != 4 ]]; then
  printf 'Usage: %s SOURCE_ROOT BUILD_DIR DESTDIR LIBDIR\n' "$0" >&2
  exit 2
fi
source_root=$(realpath -- "$1")
build_dir=$(realpath -- "$2")
destdir=$(realpath -m -- "$3")
libdir=$4
[[ $destdir != / && $libdir =~ ^/usr/lib(64|/[A-Za-z0-9_-]+)?$ ]] || {
  printf '%s\n' 'Use a package staging directory and an absolute /usr/lib path.' >&2
  exit 2
}
for file in libMacOBloxShims.dylib libmacoblox-wayland.so; do
  [[ -f $build_dir/$file ]] || {
    printf 'Missing build artifact: %s\n' "$build_dir/$file" >&2
    exit 1
  }
done
share=$destdir/usr/share/macoblox
shim=$destdir$libdir/macoblox/shim
install -d "$share/branding" "$shim" "$destdir/usr/bin"
cp -r --no-preserve=ownership "$source_root/launcher" "$share/"
rm -f "$share/launcher/install.sh"
find "$share/launcher" -type d -name __pycache__ -exec rm -rf -- {} +
# Include runtime branding; design sources and the website are not installed.
cp -r --no-preserve=ownership "$source_root/branding/icons" \
  "$source_root/branding/contributors" "$share/branding/"
install -m644 "$source_root/branding/"*.png "$share/branding/"
install -m644 "$source_root/patch_startup_throttle.py" "$share/"
install -m644 "$source_root/README.md" "$share/"
install -m755 "$build_dir/libMacOBloxShims.dylib" \
  "$build_dir/libmacoblox-wayland.so" "$shim/"
cp -r --no-preserve=ownership "$build_dir/frameworks" "$shim/"
cat > "$destdir/usr/bin/macoblox" <<WRAPPER
#!/bin/sh
export MACOBLOX_PREBUILT_SHIM=$libdir/macoblox/shim
exec python3 /usr/share/macoblox/launcher/macoblox-launcher "\$@"
WRAPPER
chmod 755 "$destdir/usr/bin/macoblox"
for size in 16 22 24 32 48 64 128 256 512; do
  install -Dm644 "$source_root/branding/icons/macoblox-$size.png" \
    "$destdir/usr/share/icons/hicolor/${size}x${size}/apps/macoblox.png"
done
for file in wtf.aubree.MacOBlox.desktop wtf.aubree.MacOBlox.URI.desktop \
  wtf.aubree.MacOBlox.Studio.desktop macoblox-roblox-window.desktop; do
  install -Dm644 "$source_root/packaging/$file" \
    "$destdir/usr/share/applications/$file"
done
install -Dm644 "$source_root/packaging/wtf.aubree.MacOBlox.xml" \
  "$destdir/usr/share/mime/packages/wtf.aubree.MacOBlox.xml"
install -Dm644 "$source_root/LICENSE" \
  "$destdir/usr/share/licenses/macoblox/LICENSE"
install -m644 "$source_root/launcher/macoblox/assets/fonts/"*-OFL.txt \
  "$destdir/usr/share/licenses/macoblox/"
