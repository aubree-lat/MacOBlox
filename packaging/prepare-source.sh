#!/usr/bin/env bash
# Prepare pristine, checksum-pinned release sources and packaging overlays.
# All output stays in the requested directory. No installation or publication.
set -euo pipefail
if [[ $# -lt 1 || $# -gt 2 ]]; then
  printf 'Usage: %s OUTPUT_DIR [RELEASE_ARCHIVE]\n' "$0" >&2
  exit 2
fi
packaging_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
version=0.21
checksum=d269a2fe5e77f6d9dfe6f9ad1281044aecb6affb868cbd9478e9efb37c21450e
output=$(realpath -m -- "$1")
[[ ! -e $output ]] || {
  printf 'Output already exists: %s\n' "$output" >&2
  exit 1
}
mkdir -p -- "$(dirname -- "$output")"
staging=$(mktemp -d "$(dirname -- "$output")/.macoblox-sources.XXXXXX")
trap 'rm -rf -- "$staging"' EXIT
archive=$staging/macoblox_$version.orig.tar.gz
if [[ $# == 2 ]]; then
  cp -- "$2" "$archive"
else
  curl --fail --location --retry 3 \
    "https://github.com/aubree-lat/MacOBlox/archive/refs/tags/v$version.tar.gz" \
    --output "$archive"
fi
printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check --status
tar -xzf "$archive" -C "$staging"
source_root=$staging/MacOBlox-$version
cp -r --no-preserve=ownership "$packaging_dir/debian" "$source_root/debian"
install -m755 "$packaging_dir/install-payload.sh" "$source_root/debian/install-payload.sh"
install -d "$staging/rpm/SOURCES" "$staging/rpm/SPECS"
cp -- "$archive" "$staging/rpm/SOURCES/macoblox-$version.tar.gz"
install -m755 "$packaging_dir/install-payload.sh" "$staging/rpm/SOURCES/macoblox-install-payload.sh"
install -m644 "$packaging_dir/rpm/macoblox.spec" "$staging/rpm/SPECS/macoblox.spec"
mv --no-clobber -T -- "$staging" "$output"
[[ ! -d $staging ]] || {
  printf 'Output was created by another process: %s\n' "$output" >&2
  exit 1
}
printf 'Debian source: %s\nRPM top directory: %s\n' "$output/MacOBlox-$version" "$output/rpm"
