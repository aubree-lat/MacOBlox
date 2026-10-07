# Native distro packages

These are package recipes for the published **0.21** release. Each package
builds the shim and native Wayland helper during packaging, installs desktop
and link handlers, and downloads Roblox on first setup. Personal settings and
sign-in are stored in the user's directories. The website and user logs are
excluded. Package updates use the distro's package manager.

| Distro family | Recipe | Derivatives using the same package format |
| --- | --- | --- |
| Arch Linux | [AUR recipes](aur/README.md) | Manjaro, EndeavourOS, CachyOS |
| Debian 13+, Ubuntu 24.04+ | [Debian overlay](debian/control) | Linux Mint 22 and compatible Debian/Ubuntu derivatives |
| Fedora | [RPM spec](rpm/macoblox.spec) | Nobara and compatible Fedora derivatives |
| openSUSE | [RPM spec](rpm/macoblox.spec) | Tumbleweed; Leap releases supplying the required UI versions |

The Debian and RPM recipes have **not been built in their target distros**.
The stable AUR recipe builds locally. Debian source package creation and RPM
source package creation succeed; both RPM dependency branches parse. These
files prepare packages for maintainers; they do not publish to AUR, Debian,
Fedora, openSUSE or a release.
The `noreply@aubree.wtf` maintainer address is a placeholder: set the real
packaging contact before submitting to a distro.

## Requirements

- x86_64 Linux with glibc. This macOS client and shim do not provide ARM or
  Alpine/musl packages.
- A **full Darling** package, including GUI frameworks and stubs, installed
  in the build environment and on the user's system. A CLI-only installation
  does not suffice. Darling is a separate dependency; these recipes do not
  bundle or rebuild it. On Debian, the upstream `darling` metapackage pulls
  the GUI components. RPM maintainers need a repository supplying a full
  `darling` RPM; a source installation alone does not satisfy package-manager
  dependency checks.
- GTK 4.14+, libadwaita 1.5+, WebKitGTK API 6.0, Python/PyGObject/Cairo,
  SDL2 and Wayland. Debian 12 and Ubuntu 22.04's original UI libraries are
  below these requirements.
- Clang, LLD and pkg-config for the native build. Compile on the target distro
  or its build chroot so the helper uses that distro's library versions.

The recipes declare a PulseAudio-compatible playback helper, which works
with PipeWire's PulseAudio service too. Native PipeWire playback and MangoHud
are optional. Install the GPU's OpenGL/EGL driver; Vulkan/Zink also needs Mesa
and the appropriate Vulkan driver.

## Prepare sources

From the MacOBlox checkout:

```bash
./packaging/prepare-source.sh "$PWD/work/native-packages"
```

This downloads the checksum-verified 0.21 release and places the Debian
overlay and RPM inputs in a new output directory. It requires `curl`, `tar`
and `sha256sum`. To use an already downloaded release archive:

```bash
./packaging/prepare-source.sh "$PWD/work/native-packages" /path/to/v0.21.tar.gz
```

The output directory must not already exist. Arch builds use their own AUR
recipes and do not require this preparation step. Interrupted preparations
remove their temporary files and leave the requested output directory free
for a retry.

## Debian / Ubuntu / Mint

Install the full Darling Debian packages in the build environment first.
Then install the packaging tools and native development dependencies:

```bash
sudo apt install build-essential debhelper dh-python python3 clang lld \
  pkg-config libsdl2-dev libwayland-dev
cd work/native-packages/MacOBlox-0.21
dpkg-buildpackage -b -us -uc
```

Build dependencies are checked by `dpkg-buildpackage`. Binary packages appear
one directory above the source directory. Install using `apt`, which resolves
dependencies from the configured repositories:

```bash
sudo apt install ../macoblox_0.21-1_amd64.deb
```

The recipe uses debhelper for Python bytecode and desktop/icon/MIME cache
integration. Builds do not launch Roblox or run tests.

To create an unsigned Debian source package, run `dpkg-source -b .` from that
source directory. Keep its `.dsc`, `.debian.tar.xz` and original release
archive together. Creating a source package does not compile a binary package.

## Fedora / openSUSE

Install a full Darling RPM and the `BuildRequires` in the spec first.
For Fedora, the package tools and native build libraries are:

```bash
sudo dnf install rpm-build clang lld gcc-c++ python3 pkgconf-pkg-config \
  SDL2-devel wayland-devel
```

For openSUSE:

```bash
sudo zypper install rpm-build clang lld gcc-c++ python3 pkg-config \
  libSDL2-devel wayland-devel
```

From the checkout, build with the prepared RPM inputs:

```bash
rpmbuild --define "_topdir $PWD/work/native-packages/rpm" \
  -ba work/native-packages/rpm/SPECS/macoblox.spec
```

The spec selects openSUSE's typelib package names using `suse_version` and
Fedora's GTK/WebKit package names otherwise. Native dependencies are also
generated from the ELF helper. Output is in `rpm/RPMS/x86_64/`; install the
result using `dnf install ./package.rpm` or `zypper install ./package.rpm`.
Use the target distro's build tools; defining `suse_version` on another
distro does not make its binaries an openSUSE build.

For a source RPM only, use `-bs` instead of `-ba`. The resulting `.src.rpm`
contains the source archive, payload installer and spec for a target-distro
rebuild. It is not an installable application RPM.

## Updating the recipes

For a new release, update the version in `prepare-source.sh`, the checksum,
the Debian changelog, the RPM `Version` and changelog, and the AUR recipes.
Increment the Debian revision / RPM release / AUR `pkgrel` for packaging-only
changes. Keep the release source archive immutable and regenerate AUR
`.SRCINFO` with `makepkg --printsrcinfo`.

Dependency names were checked against the official
[Debian WebKit package](https://packages.debian.org/trixie/gir1.2-webkit-6.0),
[Debian SDL development package](https://packages.debian.org/trixie/libsdl2-dev)
and [Fedora WebKit package](https://packages.fedoraproject.org/pkgs/webkitgtk/).
The openSUSE names follow its official
[GTK 4](https://api.opensuse.org/public/source/openSUSE:Factory/gtk4/gtk4.spec),
[libadwaita](https://api.opensuse.org/public/source/openSUSE:Factory/libadwaita/libadwaita.spec),
[WebKitGTK](https://api.opensuse.org/public/source/openSUSE:Factory/webkitgtk/webkitgtk.spec)
and [PyGObject](https://api.opensuse.org/public/source/openSUSE:Factory/python-gobject/python-gobject.spec)
specs. Its optional playback helper is supplied by
[pipewire-tools](https://manpages.opensuse.org/Tumbleweed/pipewire-tools/pw-cat.1.en.html).
