#!/usr/bin/env bash
# Mac O' Blox installer: Darling, the tools the launcher needs, and the
# launcher itself with its app menu entry. Run it again to update, or to
# uninstall.
#
#   curl -fsSL https://raw.githubusercontent.com/aubree-lat/MacOBlox/main/install.sh | bash
#
# In a terminal it guides setup; without one it installs. The choices
# also work as options (with curl: ... | bash -s -- --uninstall), see --help.
#
# Everything runs inside main(), called on the last line, so a download cut
# off halfway does nothing. The exit on that same line matters: main points
# stdin at the terminal (curl | bash), and bash would then read and run
# whatever is typed there as the rest of the script.

set -euo pipefail

REPO=https://github.com/aubree-lat/MacOBlox.git
DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}
DIR=$DATA_HOME/MacOBlox
CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}
CACHE_HOME=${XDG_CACHE_HOME:-$HOME/.cache}
PREFIX=${DPREFIX:-$HOME/.darling}
# Darling's Debian packages, pinned to the release the Flatpak uses
# (flatpak/wtf.aubree.MacOBlox.yml; change both together). The checksum makes
# sure the download is that release, and a new Darling release cannot break
# installs before the shim was tested with it.
DARLING_TAG=v0.1.20260608
DARLING_DEBS_SHA256=27469ef3932da2e91dd7fb34b70e3628a3e54b7af9fb5480051f44af35eca1fd

# Colours unless NO_COLOR is set or the output is no terminal; arrows and
# dots only where the locale is UTF-8.
if [[ -t 1 && -z ${NO_COLOR:-} && ${TERM:-dumb} != dumb ]]; then
  BOLD=$'\033[1m' DIM=$'\033[2m' RESET=$'\033[0m' ACCENT=$'\033[1;35m'
  GOOD=$'\033[1;32m' BAD=$'\033[1;31m' SELECTED=$'\033[1;97;45m'
  SHADES=($'\033[38;5;213m' $'\033[38;5;177m' $'\033[38;5;141m' $'\033[38;5;105m')
else
  BOLD='' DIM='' RESET='' ACCENT='' GOOD='' BAD='' SELECTED=''
  SHADES=('' '' '' '')
fi
case ${LC_ALL:-${LC_CTYPE:-${LANG:-}}} in
  *[Uu][Tt][Ff]-8* | *[Uu][Tt][Ff]8*)
    POINTER='❯' BULLET='•' ON='●' OFF='○' KEYS='↑/↓ move · enter choose · q quit' ;;
  *) POINTER='>' BULLET='-' ON='*' OFF='-' KEYS='up/down move, enter choose, q quit' ;;
esac

say() { printf '  %s%s%s %s\n' "$ACCENT" "$BULLET" "$RESET" "$*"; }
die() { printf '%sError:%s %s\n' "$BAD" "$RESET" "$*" >&2; exit 1; }
UPDATE_BACKUP=''
INSTALL_LOG=''
# NEW: Track whether we're showing live output
SHOW_BUILD_OUTPUT=${SHOW_BUILD_OUTPUT:-1}

step() { printf '\n%s[%s/4] %s%s\n' "$BOLD" "$1" "$2" "$RESET"; }

distribution_name() {
  local PRETTY_NAME='' ID='' ID_LIKE=''
  [[ ! -r /etc/os-release ]] || . /etc/os-release
  printf '%s' "${PRETTY_NAME:-${NAME:-Linux}}"
}

# IMPROVED: More robust distro detection with fallback
detect_package_manager() {
  local ID='' ID_LIKE='' VERSION_ID='' family manager
  [[ ! -r /etc/os-release ]] || . /etc/os-release
  family=" ${ID:-} ${ID_LIKE:-} "
  
  # Check immutable/special systems first
  if [[ -e /run/ostree-booted ]]; then
    printf '%s' manual; return
  elif [[ -e /etc/NIXOS ]]; then
    printf '%s' manual; return
  elif [[ -e /etc/transactional-update.conf ]]; then
    printf '%s' manual; return
  elif [[ -f /etc/os-release ]] && grep -q "^ID.*=.*\(nixos\|guix\|steamos\|microos\|aeon\|kalpa\)" /etc/os-release; then
    printf '%s' manual; return
  fi
  
  # Try ID first, then ID_LIKE
  case "$ID" in
    arch|archarm) printf '%s' pacman; return ;;
    debian|ubuntu|devuan|pop) printf '%s' apt-get; return ;;
    fedora|rhel|centos|alma|rocky) printf '%s' dnf; return ;;
    opensuse*|suse) printf '%s' zypper; return ;;
    alpine) printf '%s' apk; return ;;
    gentoo|gentoo-prefix) printf '%s' emerge; return ;;
    void) printf '%s' xbps-install; return ;;
    solus) printf '%s' eopkg; return ;;
  esac
  
  # Try ID_LIKE patterns as fallback
  if [[ "$family" == *" arch "* ]]; then
    printf '%s' pacman; return
  elif [[ "$family" == *" debian "* ]] || [[ "$family" == *" ubuntu "* ]]; then
    printf '%s' apt-get; return
  elif [[ "$family" == *" fedora "* ]] || [[ "$family" == *" rhel "* ]] || [[ "$family" == *" centos "* ]]; then
    printf '%s' dnf; return
  elif [[ "$family" == *" suse "* ]] || [[ "$family" == *" opensuse "* ]]; then
    printf '%s' zypper; return
  elif [[ "$family" == *" alpine "* ]]; then
    printf '%s' apk; return
  fi
  
  # Fall back to checking which manager exists on PATH
  for manager in pacman apt-get dnf zypper apk xbps-install eopkg emerge yum; do
    if command -v "$manager" >/dev/null 2>&1; then
      printf '%s' "$manager"; return
    fi
  done
  
  printf '%s' manual
}

setup_plan() {
  local operation='Install' dependencies='your package manager'
  is_installed && operation='Update'
  dependencies=$(detect_package_manager)
  [[ $dependencies != pacman ]] || dependencies='pacman and the AUR'
  printf '  %sSetup plan%s\n\n' "$BOLD" "$RESET"
  printf '    1. Prepare Darling and the system tools (%s).\n' "$dependencies"
  printf '    2. %s the Mac O\047 Blox launcher.\n' "$operation"
  printf '    3. Build its compatibility libraries.\n'
  printf '    4. Add the app menu entry and macoblox command.\n\n'
  printf '  Computer    %s (%s)\n' "$(distribution_name)" "$(uname -m)"
  printf '  Destination %s\n' "${DIR/#$HOME/\~}"
  printf '  %sSystem packages may ask for your sudo password.%s\n' "$DIM" "$RESET"
  if is_installed; then
    printf '  %sUpdates keep settings, Roblox, Studio and existing backups.%s\n' "$DIM" "$RESET"
    printf '  %sLocal source changes are backed up before replacement.%s\n' "$DIM" "$RESET"
  else
    printf '  %sFirst launch guides Roblox installation and sign-in.%s\n' "$DIM" "$RESET"
  fi
}

# Keep recovery material outside the checkout. A reset can remove untracked
# files that obstruct incoming tracked paths; back those up too, and leave
# other untracked/ignored files in place instead of using git clean.
backup_checkout_changes() {
  local list_dir relative backup
  list_dir=$(mktemp -d)
  git -C "$DIR" diff --name-only -z HEAD > "$list_dir/tracked"
  git -C "$DIR" ls-files --others --exclude-standard -z > "$list_dir/untracked"
  if [[ ! -s $list_dir/tracked && ! -s $list_dir/untracked ]]; then
    rm -rf -- "$list_dir"
    return
  fi
  backup=$(umask 077; mkdir -p -- "$DATA_HOME/MacOBlox-backups";
    mktemp -d "$DATA_HOME/MacOBlox-backups/update-$(date -u +%Y%m%d-%H%M%S)-XXXXXX")
  git -C "$DIR" rev-parse HEAD > "$backup/revision"
  git -C "$DIR" diff --binary HEAD > "$backup/tracked.patch"
  cp -- "$list_dir/tracked" "$backup/tracked-paths"
  cp -- "$list_dir/untracked" "$backup/untracked-paths"
  while IFS= read -r -d '' relative; do
    [[ -e $DIR/$relative || -L $DIR/$relative ]] || continue
    mkdir -p -- "$backup/files/$(dirname -- "$relative")"
    cp -a -- "$DIR/$relative" "$backup/files/$relative"
  done < <(cat -- "$list_dir/tracked" "$list_dir/untracked")
  rm -rf -- "$list_dir"
  UPDATE_BACKUP=$backup
  say "Local changes saved to ${backup/#$HOME/\~}"
}

# The website lives on main for hosting, but is not part of an app install.
# Partial fetches omit blobs until checkout requests them; sparse checkout
# keeps hosting paths from requesting those blobs or entering the worktree.
configure_launcher_checkout() {
  git -C "$DIR" config remote.origin.promisor true
  git -C "$DIR" config remote.origin.partialclonefilter blob:none
  git -C "$DIR" sparse-checkout set --no-cone --stdin <<'PATTERNS'
/*
!/website/
!/.github/
!/vercel.json
!/.vercel/
!/.vercelignore
PATTERNS
}

setup_success() {
  local version
  version=$(installed_version)
  printf '\n%s%s Setup complete%s\n' "$GOOD" "$ON" "$RESET"
  printf '  Mac O\047 Blox%s is ready in your app menu.\n' "${version:+ $version}"
  printf '\n  %sNext steps%s\n' "$BOLD" "$RESET"
  printf '    1. Open Mac O\047 Blox from the app menu, or run macoblox.\n'
  if [[ -d $DIR/RobloxPlayer.app ]]; then
    printf '    2. Launch Roblox from the launcher.\n'
    printf '    3. Sign in if needed, then choose a game.\n'
  else
    printf '    2. Follow its welcome screen to install Roblox.\n'
    printf '    3. Sign in to Roblox and choose a game.\n'
  fi
  [[ -z $UPDATE_BACKUP ]] || printf '\n  Source backup: %s\n' "${UPDATE_BACKUP/#$HOME/\~}"
  [[ -z $INSTALL_LOG ]] || printf '  Build log: %s\n' "${INSTALL_LOG/#$HOME/\~}"
  printf '\n'
}

# ------------------------------------------------------------------ install

install_arch() {
  # Only packages that are not installed at all: asking pacman for an
  # installed but outdated one (pipewire-audio 1.6.8 with 1.6.9 in the repo)
  # makes it a partial upgrade that breaks on pinned dependencies.
  local wanted=(git curl base-devel clang lld unzip python python-gobject gtk4 libadwaita webkitgtk-6.0 sdl2-compat wayland pkgconf)
  command -v pw-cat >/dev/null || wanted+=(pipewire-audio)
  local missing
  missing=$(pacman -T "${wanted[@]}" || true)
  if [[ -n $missing ]]; then
    say "Installing tools (pacman): $(echo $missing)"
    # shellcheck disable=SC2086
    sudo pacman -S --needed --noconfirm $missing ||
      die "pacman could not install them. Update the system with 'sudo pacman -Syu' and run this again."
  fi
}

install_debian() {
  say "Installing tools (apt)"
  sudo apt-get update
  sudo apt-get install -y git curl unzip clang lld pipewire-bin python3 python3-gi \
    gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-webkit-6.0 libsdl2-dev libwayland-dev pkg-config
}

install_fedora() {
  say "Installing tools (dnf)"
  sudo dnf install -y git curl clang lld unzip pipewire-utils python3 python3-gobject gtk4 libadwaita webkitgtk6.0 SDL2-devel wayland-devel pkgconf-pkg-config
}

# Launcher dependencies; Darling is handled centrally after validation.
install_opensuse() {
  say "Installing tools (zypper)"
  sudo zypper --non-interactive install git curl unzip clang lld pipewire-tools \
    python3 python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 \
    typelib-1_0-WebKit-6_0 libSDL2-devel wayland-devel pkg-config
}

install_alpine() {
  say "Installing tools (apk); Darling needs an Alpine-specific source build"
  sudo apk add git curl unzip clang lld pipewire-tools python3 py3-gobject3 \
    gtk4.0 libadwaita webkit2gtk-6.0 sdl2-dev wayland-dev pkgconf
}

install_gentoo() {
  say "Installing tools (emerge); enable introspection and GTK 4 WebKit support in Portage"
  sudo emerge --noreplace dev-vcs/git net-misc/curl app-arch/unzip \
    llvm-core/clang llvm-core/lld media-video/pipewire dev-lang/python \
    dev-python/pygobject gui-libs/gtk:4 gui-libs/libadwaita \
    net-libs/webkit-gtk:6 media-libs/libsdl2 dev-libs/wayland dev-util/pkgconf
}

install_void() {
  say "Installing tools (xbps-install)"
  sudo xbps-install -Sy git curl unzip clang lld pipewire python3 python3-gobject \
    gtk4 libadwaita webkitgtk6 SDL2-devel wayland-devel pkg-config
}

install_solus() {
  say "Installing tools (eopkg)"
  sudo eopkg install -y git curl unzip clang lld pipewire python3 python-gobject \
    gtk4 libadwaita webkit-gtk sdl2-devel wayland-devel pkg-config
}

install_tools() {
  case $(detect_package_manager) in
    pacman) install_arch ;;
    apt-get) install_debian ;;
    dnf) install_fedora ;;
    yum)
      say "Installing tools (yum); older enterprise releases may lack GTK 4/WebKit 6"
      sudo yum install -y git curl clang lld unzip pipewire-utils python3 python3-gobject \
        gtk4 libadwaita webkitgtk6.0 SDL2-devel wayland-devel pkgconf-pkg-config ;;
    zypper) install_opensuse ;;
    apk) install_alpine ;;
    emerge) install_gentoo ;;
    xbps-install) install_void ;;
    eopkg) install_solus ;;
    manual)
      say "Manual dependencies required on this host (including immutable Linux, NixOS and Guix)."
      say "Install Darling, git, clang, lld, unzip, PipeWire, Python 3/PyGObject, GTK 4, libadwaita, WebKit 6, SDL2, Wayland and pkg-config using your host's supported method."
      ;;
  esac
}

# Only offer compilation after the configured repositories have been checked.
try_darling_package() {
  local manager=$1
  case $manager in
    pacman) pacman -Si darling >/dev/null 2>&1 || return 1
      sudo pacman -S --needed --noconfirm darling || return 1 ;;
    apt-get) apt-cache show darling >/dev/null 2>&1 || return 1
      sudo apt-get install -y darling || return 1 ;;
    dnf|yum) "$manager" list --available darling >/dev/null 2>&1 || return 1
      sudo "$manager" install -y darling || return 1 ;;
    zypper) zypper --non-interactive search --match-exact --type package darling 2>/dev/null | grep -q "darling" || return 1
      sudo zypper --non-interactive install darling || return 1 ;;
    apk) apk search -x darling 2>/dev/null | grep -q "^darling" || return 1
      sudo apk add darling || return 1 ;;
    xbps-install) xbps-query -R darling >/dev/null 2>&1 || return 1
      sudo xbps-install -y darling || return 1 ;;
    emerge) emerge --pretend --quiet app-emulation/darling >/dev/null 2>&1 || return 1
      # Portage packages may themselves compile: ask before invoking emerge.
      confirm_darling_build || die "Darling compilation declined. Setup stopped."
      sudo emerge --noreplace app-emulation/darling || return 1 ;;
    eopkg) eopkg info darling >/dev/null 2>&1 || return 1
      sudo eopkg install -y darling || return 1 ;;
    *) return 1 ;;
  esac
  hash -r
  command -v darling >/dev/null
}

confirm_darling_build() {
  local answer=''
  say "Darling needs compilation on $(distribution_name)."
  say "This installs build dependencies and Darling into /usr/local using sudo."
  say "Allow several hours, at least 4 GiB RAM and about 25 GiB free disk space."
  say "The build uses the pinned $DARLING_TAG release, 64-bit components and a saved log."
  # Never consume piped installer text, and --yes does not bypass this prompt.
  if ! (: </dev/tty) 2>/dev/null; then
    die "Source compilation requires confirmation in a terminal. Rerun interactively."
  fi
  printf '  Compile Darling and continue? [y/N] ' >/dev/tty
  IFS= read -r answer </dev/tty || return 1
  [[ $answer == [Yy] || $answer == [Yy][Ee][Ss] ]]
}

install_darling_build_dependencies() {
  case $1 in
    apt-get) sudo apt-get install -y build-essential cmake clang bison flex xz-utils git-lfs \
      libfuse-dev libudev-dev libcap2-bin libglu1-mesa-dev libcairo2-dev libgl-dev \
      libtiff-dev libfreetype-dev libxml2-dev libegl-dev libfontconfig-dev libbsd-dev \
      libxrandr-dev libxcursor-dev libgif-dev libpulse-dev libavformat-dev libavcodec-dev \
      libswresample-dev libdbus-1-dev libxkbfile-dev libssl-dev llvm-dev libelf-dev \
      libvulkan-dev libcurl4-openssl-dev libedit-dev ;;
    pacman) sudo pacman -S --needed --noconfirm make cmake clang flex bison icu fuse \
      pkgconf fontconfig cairo libtiff mesa glu llvm libbsd libxkbfile libxcursor \
      libxext libxkbcommon libxrandr ffmpeg git-lfs ;;
    dnf|yum) sudo "$1" install -y make gcc gcc-c++ cmake clang bison flex git-lfs \
      dbus-devel glibc-devel fuse-devel systemd-devel elfutils-libelf-devel cairo-devel \
      freetype-devel libjpeg-turbo-devel fontconfig-devel libglvnd-devel mesa-libGL-devel \
      mesa-libEGL-devel mesa-libGLU-devel libtiff-devel libxml2-devel libbsd-devel \
      libXcursor-devel libXrandr-devel giflib-devel pulseaudio-libs-devel libxkbfile-devel \
      openssl-devel llvm-devel libcap-devel libavcodec-free-devel libavformat-free-devel ;;
    zypper) sudo zypper --non-interactive install make gcc gcc-c++ cmake clang bison flex \
      git-lfs fuse-devel systemd-devel libelf-devel cairo-devel freetype2-devel \
      fontconfig-devel Mesa-libGL-devel Mesa-libEGL-devel glu-devel libxml2-devel \
      libbsd-devel libXcursor-devel libXrandr-devel giflib-devel libpulse-devel \
      libxkbfile-devel libopenssl-devel llvm-devel libcap-devel libtiff-devel \
      libjpeg-devel dbus-1-devel libavcodec-devel libavformat-devel libswresample-devel ;;
    apk) sudo apk add build-base cmake clang bison flex xz fuse-dev libcap-dev git-lfs \
      python3 glu-dev cairo-dev mesa-dev tiff-dev freetype-dev libxml2-dev fontconfig-dev \
      libbsd-dev libxrandr-dev libxcursor-dev giflib-dev pulseaudio-dev ffmpeg-dev \
      dbus-dev libxkbfile-dev openssl-dev linux-headers llvm-dev xdg-user-dirs ;;
    xbps-install) sudo xbps-install -y base-devel cmake clang bison flex xz git-lfs \
      fuse-devel libcap-devel eudev-libudev-devel glu-devel cairo-devel MesaLib-devel \
      tiff-devel freetype-devel libxml2-devel fontconfig-devel libbsd-devel \
      libXrandr-devel libXcursor-devel giflib-devel libpulseaudio-devel ffmpeg-devel \
      dbus-devel libxkbfile-devel openssl-devel llvm-devel ;;
    emerge) sudo emerge --noreplace dev-build/cmake llvm-core/clang llvm-core/llvm \
      sys-devel/bison sys-devel/flex dev-vcs/git-lfs sys-fs/fuse:0 sys-libs/libcap \
      virtual/libudev media-libs/glu x11-libs/cairo media-libs/mesa media-libs/tiff \
      media-libs/freetype dev-libs/libxml2 media-libs/fontconfig dev-libs/libbsd \
      x11-libs/libXrandr x11-libs/libXcursor media-libs/giflib media-libs/libpulse \
      media-video/ffmpeg sys-apps/dbus x11-libs/libxkbfile dev-libs/openssl ;;
    eopkg) sudo eopkg install -y -c system.devel || return 1
      sudo eopkg install -y cmake clang llvm-devel bison flex git-lfs fuse-devel \
        libcap-devel systemd-devel mesalib-devel cairo-devel libtiff-devel freetype2-devel \
        libxml2-devel fontconfig-devel libbsd-devel libxrandr-devel libxcursor-devel \
        giflib-devel pulseaudio-devel ffmpeg-devel dbus-devel libxkbfile-devel openssl-devel ;;
    *) die "Automatic source installation is unavailable on this host; use https://docs.darlinghq.org/build-instructions.html with your host's supported installation method." ;;
  esac
}

build_darling_source() {
  local manager=$1 build log jobs=${DARLING_BUILD_JOBS:-2}
  [[ $jobs =~ ^[1-9][0-9]*$ ]] || die "DARLING_BUILD_JOBS must be a positive integer."
  install_darling_build_dependencies "$manager" || die "Could not install Darling build dependencies. Check enabled repositories and package names for your distro, then rerun."
  mkdir -p -- "$CACHE_HOME/macoblox/installer"
  build=$(umask 077; mktemp -d "$CACHE_HOME/macoblox/installer/darling-source-XXXXXX")
  log=$build/build.log
  say "Building Darling; sources and log: $build"
  
  # Clone with live output
  GIT_CLONE_PROTECTION_ACTIVE=false git clone --recursive --branch "$DARLING_TAG" \
    https://github.com/darlinghq/darling.git "$build/source" 2>&1 | tee "$log" ||
    die "Darling source download failed. Log: $log"
  
  git -C "$build/source" lfs install --local 2>&1 | tee -a "$log" || die "Git LFS initialization failed. Log: $log"
  git -C "$build/source" lfs pull 2>&1 | tee -a "$log" || die "Git LFS download failed. Log: $log"
  
  # Configure with live output
  say "Configuring Darling (this may take a few minutes)..."
  cmake -S "$build/source" -B "$build/build" -DTARGET_i386=OFF \
    -DCMAKE_INSTALL_PREFIX=/usr/local 2>&1 | tee -a "$log" ||
    die "Darling configuration failed; check distro build dependencies. Log: $log"
  
  # Build with live output
  say "Compiling Darling (using $jobs parallel jobs; this will take a while)..."
  cmake --build "$build/build" --parallel "$jobs" 2>&1 | tee -a "$log" ||
    die "Darling compilation failed. Log: $log"
  
  # Install with live output
  say "Installing Darling..."
  sudo cmake --install "$build/build" 2>&1 | tee -a "$log" || die "Darling installation failed. Log: $log"
  
  export PATH="/usr/local/bin:$PATH"
  hash -r
  command -v darling >/dev/null || die "Build completed but darling was not installed. Log: $log"
  say "Darling installed. Build files retained at $build"
}

# Retain the existing checksum-verified upstream Debian binary option.
try_darling_debs() {
  local build
  build=$(mktemp -d)
  say "Trying Darling $DARLING_TAG upstream Debian packages"
  if ! curl -fL --progress-bar -o "$build/debs.zip" \
    "https://github.com/darlinghq/darling/releases/download/$DARLING_TAG/debs_${DARLING_TAG##*.}.zip"; then
    rm -rf -- "$build"; return 1
  fi
  if ! printf '%s  %s\n' "$DARLING_DEBS_SHA256" "$build/debs.zip" | sha256sum -c --quiet -; then
    rm -rf -- "$build"
    die "The Darling download does not match its checksum. Setup stopped."
  fi
  if ! unzip -q "$build/debs.zip" -d "$build"; then rm -rf -- "$build"; return 1; fi
  if ! sudo apt-get install -y "$build"/debs_*/*.deb; then rm -rf -- "$build"; return 1; fi
  rm -rf -- "$build"
  hash -r
  command -v darling >/dev/null
}

ensure_darling() {
  command -v darling >/dev/null && return 0
  local manager
  manager=$(detect_package_manager)
  [[ $manager != manual ]] || die "Install Darling using your immutable/NixOS/Guix host's supported method."
  say "Checking configured repositories for Darling"
  if try_darling_package "$manager"; then return 0; fi
  if [[ $manager == apt-get ]] && try_darling_debs; then return 0; fi
  say "No usable Darling package was installed from the configured repositories."
  confirm_darling_build || die "Darling compilation declined. Setup stopped."
  build_darling_source "$manager"
}

validate_tools() {
  local tool
  for tool in git clang ld.lld unzip python3 pw-cat pkg-config; do
    command -v "$tool" >/dev/null || {
      if [[ $tool == darling ]]; then
        die "Darling is not installed. Follow https://docs.darlinghq.org/build-instructions.html for your distro (Alpine needs its specific instructions), then rerun this installer."
      fi
      die "$tool is missing. Install it using your distribution's supported method and rerun this installer."
    }
  done
  pkg-config --exists sdl2 wayland-client ||
    die "SDL2/Wayland development files are missing. Install their development packages and rerun."
  python3 - <<'PYTHON' || die "Python bindings for GTK 4, Adwaita 1 or WebKit 6 are missing. Check your distro packages/Portage USE flags and rerun."
import gi
for namespace, version in [('Gtk', '4.0'), ('Adw', '1'), ('WebKit', '6.0')]:
    gi.require_version(namespace, version)
from gi.repository import Gtk, Adw, WebKit
PYTHON
}

do_install() {
  [[ $(uname -m) == x86_64 ]] || die "Darling runs only on x86_64."
  [[ ! -e $DIR || -d $DIR/.git ]] ||
    die "$DIR already exists without a Git checkout. Move it aside before installing."
  step 1 "Prepare the system tools"
  install_tools
  validate_tools
  ensure_darling

  step 2 "Prepare Mac O' Blox"
  if [[ -d $DIR/.git ]]; then
    say "Updating Mac O' Blox"

    # Checkouts from before the move to this fork still point at the original
    # repository, which does not have its fixes.
    case $(git -C "$DIR" remote get-url origin 2>/dev/null) in
      https://github.com/narezy/MacOBlox | https://github.com/narezy/MacOBlox.git)
        git -C "$DIR" remote set-url origin "$REPO" ;;
    esac

    # Fetch first, then preserve local repairs before replacing tracked files.
    # Session data, downloads and backups stay in the checkout unchanged.
    git -C "$DIR" config remote.origin.promisor true
    git -C "$DIR" config remote.origin.partialclonefilter blob:none
    git -C "$DIR" fetch --filter=blob:none origin main
    backup_checkout_changes
    configure_launcher_checkout
    git -C "$DIR" reset --hard origin/main

  else
    say "Downloading Mac O' Blox"
    git clone --filter=blob:none --no-checkout --depth 1 --single-branch --branch main "$REPO" "$DIR"
    configure_launcher_checkout
    git -C "$DIR" reset --hard HEAD
  fi
  step 3 "Build the compatibility libraries"
  say "This can take a few minutes."
  mkdir -p -- "$CACHE_HOME/macoblox/installer"
  INSTALL_LOG=$(umask 077; mktemp "$CACHE_HOME/macoblox/installer/build-$(date -u +%Y%m%d-%H%M%S)-XXXXXX.log")
  
  # IMPROVED: Show build output live with tee
  if ! "$DIR/build_debug_shim.sh" 2>&1 | tee "$INSTALL_LOG"; then
    say "Build output saved to: $INSTALL_LOG"
    say "Last 40 lines of output:"
    tail -n 40 -- "$INSTALL_LOG" >&2
    die "Build failed. See full log above or at: $INSTALL_LOG"
  fi
  say "Compatibility libraries built."
  step 4 "Add the launcher to your desktop"
  "$DIR/launcher/install.sh"
  setup_success
}

# ---------------------------------------------------------------- uninstall

# The checkout this script makes: nothing is deleted unless $DIR is one.
is_installed() { [[ -f $DIR/launcher/macoblox-launcher && -f $DIR/build_debug_shim.sh ]]; }
installed_version() { sed -n 's/^__version__ = "\(.*\)"/\1/p' "$DIR/launcher/macoblox/__init__.py" 2>/dev/null; }

# Removes what do_install and the launcher put on this computer; with an
# argument, Darling's prefix as well. Darling and the other packages stay.
do_uninstall() {
  local purge=${1:-}
  if [[ -e $DIR ]] && ! is_installed; then
    die "$DIR does not look like Mac O' Blox, so it is left alone."
  fi
  # Nothing in the prefix may change under a running Darling (it keeps
  # showing deleted files), and nothing should run from the folder that goes.
  if pgrep -u "$(id -u)" -x darlingserver >/dev/null 2>&1; then
    say "Stopping Roblox and Darling"
    darling shutdown >/dev/null 2>&1 || true
  fi
  if [[ -x $DIR/studio/wine/bin/wineserver ]]; then
    WINEPREFIX=$DIR/studio/prefix "$DIR/studio/wine/bin/wineserver" -k >/dev/null 2>&1 || true
  fi
  if [[ -e $DIR ]]; then
    say "Removing the launcher, Roblox and Studio (${DIR/#$HOME/\~})"
    rm -rf -- "$DIR"
  fi
  say "Removing the app menu entries, icons and the macoblox command"
  local apps=$DATA_HOME/applications
  # The xyz.narez.* names are the app ID before 0.15.
  rm -f -- "$apps/wtf.aubree.MacOBlox.desktop" "$apps/wtf.aubree.MacOBlox.URI.desktop" \
    "$apps/wtf.aubree.MacOBlox.Studio.desktop" \
    "$apps/xyz.narez.MacOBlox.desktop" "$apps/xyz.narez.MacOBlox.Studio.desktop" \
    "$apps/macoblox-roblox-window.desktop" "$apps/org.macoblox.Launcher.desktop" \
    "$DATA_HOME"/icons/hicolor/*/apps/macoblox.png "$DATA_HOME/mime/packages/wtf.aubree.MacOBlox.xml" \
    "$DATA_HOME/mime/packages/xyz.narez.MacOBlox.xml"
  local link=$HOME/.local/bin/macoblox
  if [[ -L $link && $(readlink "$link") == */macoblox-launcher ]]; then
    rm -f -- "$link"
  fi
  # Studio as the handler of roblox-studio: links and place files.
  if [[ -f $CONFIG_HOME/mimeapps.list ]]; then
    sed -i -e 's/wtf\.aubree\.MacOBlox\.Studio\.desktop;\{0,1\}//g' \
      -e 's/wtf\.aubree\.MacOBlox\.URI\.desktop;\{0,1\}//g' \
      -e 's/xyz\.narez\.MacOBlox\.Studio\.desktop;\{0,1\}//g' -e '/^[^=[]*=$/d' "$CONFIG_HOME/mimeapps.list"
  fi
  update-mime-database "$DATA_HOME/mime" >/dev/null 2>&1 || true
  update-desktop-database "$apps" >/dev/null 2>&1 || true
  say "Removing settings and cache"
  rm -rf -- "$CONFIG_HOME/macoblox" "$CACHE_HOME/macoblox"
  if [[ -n $purge && -e $PREFIX ]]; then
    [[ $PREFIX == "$HOME"/?* ]] || die "Darling's prefix $PREFIX is not in your home folder, so it is left alone."
    say "Removing Darling's prefix (${PREFIX/#$HOME/\~}), with your Roblox sign-in"
    rm -rf -- "$PREFIX"
  fi
  say "Mac O' Blox is uninstalled."
  if command -v darling >/dev/null; then
    printf '    %sDarling stays installed; remove it with your package manager if nothing else uses it.%s\n' \
      "$DIM" "$RESET"
  fi
}

# --------------------------------------------------------------------- menu

# Drawn on the terminal's alternate screen, which gets its old contents back
# when the menu closes.
MENU_ON=''
MENU_SCREEN=''
menu_open() {
  MENU_ON=1
  if [[ ${TERM:-dumb} != dumb ]]; then
    MENU_SCREEN=1
    printf '\033[?1049h\033[?25l' >/dev/tty
  fi
}
menu_close() {
  [[ -n $MENU_ON ]] || return 0
  MENU_ON=''
  if [[ -n $MENU_SCREEN ]]; then
    MENU_SCREEN=''
    printf '\033[?25h\033[?1049l' >/dev/tty
  fi
}

banner() {
  local line shade=0
  while IFS= read -r line; do
    printf '  %s%s%s\n' "${SHADES[shade]}" "$line" "$RESET"
    shade=$((shade + 1))
  done <<'ART'
 __  __          ___ _   ___ _
|  \/  |__ _ __ / _ ( ) | _ ) |_____ __
| |\/| / _` / _| (_) |/  | _ \ / _ \ \ /
|_|  |_\__,_\__|\___/    |___/_\___/_\_\
ART
}

# choose TEXT ITEM...: a menu of "label|hint" items below TEXT (printf %b).
# Sets CHOICE to the chosen item's index, or to -1 for quit.
CHOICE=-1
choose() {
  local text=$1
  shift
  local count=$# index=0 key rest item label hint i
  if [[ -z $MENU_SCREEN ]]; then
    {
      banner
      printf '\n%b\n\n' "$text"
      i=1
      for item; do
        printf '  %s. %s  %s\n' "$i" "${item%%|*}" "${item#*|}"
        i=$((i + 1))
      done
    } >/dev/tty
    while :; do
      printf '\n  Choose 1-%s [1], or q to quit: ' "$count" >/dev/tty
      IFS= read -r key </dev/tty || { CHOICE=-1; return; }
      [[ -n $key ]] || key=1
      case $key in
        q | Q) CHOICE=-1; return ;;
        [1-9]) if ((key <= count)); then CHOICE=$((key - 1)); return; fi ;;
      esac
    done
  fi
  while :; do
    {
      printf '\033[H\033[2J\n'
      banner
      printf '\n%b\n\n' "$text"
      i=0
      for item; do
        label=${item%%|*}
        hint=${item#*|}
        if ((i == index)); then
          printf '  %s %s %-22s%s %s\n' "$SELECTED" "$POINTER" "$label" "$RESET" "$hint"
        else
          printf '     %-22s %s%s%s\n' "$label" "$DIM" "$hint" "$RESET"
        fi
        i=$((i + 1))
      done
      printf '\n  %s%s%s\n' "$DIM" "$KEYS" "$RESET"
    } >/dev/tty
    IFS= read -rsn1 key </dev/tty || { CHOICE=-1; return; }
    case $key in
      $'\033')
        rest=''
        IFS= read -rsn2 -t 0.05 rest </dev/tty || true
        case $rest in
          '[A' | 'OA') index=$(((index + count - 1) % count)) ;;
          '[B' | 'OB') index=$(((index + 1) % count)) ;;
          '') CHOICE=-1; return ;;
        esac ;;
      k | K) index=$(((index + count - 1) % count)) ;;
      j | J) index=$(((index + 1) % count)) ;;
      [1-9]) if ((key <= count)); then CHOICE=$((key - 1)); return; fi ;;
      q | Q) CHOICE=-1; return ;;
      '') CHOICE=$index; return ;;
    esac
  done
}

menu_main() {
  local text first="Start setup|Review what will be installed"
  if is_installed; then
    text="  ${BOLD}Welcome back to Mac O' Blox${RESET}

  ${GOOD}${ON}${RESET} Version $(installed_version) is installed.
  Keep your launcher and compatibility libraries up to date.
  ${DIM}${DIR/#$HOME/\~}${RESET}"
    first="Update Mac O' Blox|Review the update plan"
  else
    text="  ${BOLD}Welcome to Mac O' Blox${RESET}
  ${DIM}First setup · 1 of 3${RESET}

  Play the macOS Roblox client on your Linux desktop.
  We'll prepare the launcher, Darling and the system tools.
  The launcher will guide you through installing Roblox and signing in."
  fi
  choose "$text" "$first" "Uninstall|Remove Mac O' Blox from this computer" "Quit|"
}

menu_install() {
  local operation='Install'
  is_installed && operation='Update'
  while :; do
    choose "  ${BOLD}Setup · 2 of 3${RESET}

$(setup_plan)" "Continue|Confirm this setup plan" "Back|Return to the welcome screen"
    [[ $CHOICE == 0 ]] || return 1
    choose "  ${BOLD}Ready to ${operation,,} Mac O' Blox${RESET}
  ${DIM}Setup · 3 of 3${RESET}

  Destination: ${DIR/#$HOME/\~}
  Setup downloads the launcher and builds the compatibility libraries.
  You'll see progress for each step. System packages may request sudo.

  ${DIM}Begin when you're ready.${RESET}" \
      "$operation Mac O' Blox|Begin setup" "Back|Review the plan again"
    case $CHOICE in
      0) return 0 ;;
      1) ;;
      *) return 1 ;;
    esac
  done
}

# The uninstall screen. Sets PURGE; false for "back".
PURGE=''
menu_uninstall() {
  local where=${DIR/#$HOME/\~} prefix=${PREFIX/#$HOME/\~}
  choose "  ${BOLD}Uninstall removes${RESET}
    ${BULLET} $where (the launcher, Roblox, Studio)
    ${BULLET} the app menu entries, icons and the macoblox command
    ${BULLET} settings and cache

  ${DIM}Darling stays installed. Its prefix, $prefix, holds your Roblox sign-in.${RESET}" \
    "Uninstall|keep $prefix" "Uninstall everything|also delete $prefix" "Back|"
  case $CHOICE in
    0)
      PURGE=''
      return 0 ;;
    1)
      choose "  ${BAD}Delete $prefix?${RESET}

  It is Darling's macOS home folder: your Roblox sign-in, and anything
  else installed or saved in Darling, goes with it." \
        "No, go back|" "Yes, delete it|"
      [[ $CHOICE == 1 ]] || return 1
      PURGE=1
      return 0 ;;
    *) return 1 ;;
  esac
}

usage() {
  cat <<USAGE
Mac O' Blox installer

  install.sh               a menu in a terminal; without one, install or update
  install.sh --install     review the setup plan, then install or update
  install.sh --update      same as --install
  install.sh --install --yes
                           install or update without setup prompts
  install.sh --uninstall   remove Mac O' Blox (asks first, unless --yes)
  install.sh --uninstall --purge
                           also delete Darling's prefix, ${PREFIX/#$HOME/\~} (your Roblox sign-in)

With curl, options go after "bash -s --":
  curl -fsSL https://raw.githubusercontent.com/aubree-lat/MacOBlox/main/install.sh | bash -s -- --uninstall
USAGE
}

main() {
  local action='' assume_yes='' reviewed='' arg
  for arg in "$@"; do
    case $arg in
      --install | --update) action=install ;;
      --uninstall) action=uninstall ;;
      --purge) PURGE=1 ;;
      -y | --yes) assume_yes=1 ;;
      -h | --help)
        usage
        return 0 ;;
      *) die "Unknown option: $arg (see --help)" ;;
    esac
  done
  # Package managers may ask questions; with curl | bash stdin is this script.
  if [[ ! -t 0 ]] && (: </dev/tty) 2>/dev/null; then exec </dev/tty; fi
  [[ $EUID -ne 0 ]] || die "Run this as your user, not root. sudo is used when needed."
  local terminal=''
  [[ -t 0 && -t 1 ]] && terminal=1

  if [[ -z $action && -n $terminal && -z $assume_yes ]]; then
    trap 'menu_close' EXIT
    trap 'menu_close; exit 130' INT TERM
    menu_open
    while [[ -z $action ]]; do
      menu_main
      case $CHOICE in
        0) if menu_install; then action=install; reviewed=1; fi ;;
        1) if menu_uninstall; then action=uninstall; fi ;;
        *) action=quit ;;
      esac
    done
    menu_close
    [[ $action == quit ]] || banner
  elif [[ $action == install && -n $terminal && -z $assume_yes ]]; then
    trap 'menu_close' EXIT
    trap 'menu_close; exit 130' INT TERM
    menu_open
    if ! menu_install; then
      menu_close
      say "Setup cancelled."
      return 0
    fi
    reviewed=1
    menu_close
    banner
  elif [[ $action == uninstall && -z $assume_yes ]]; then
    [[ -n $terminal ]] || die "Not uninstalling without a terminal to ask in; add --yes."
    local answer='' what="Mac O' Blox"
    [[ -n $PURGE ]] && what+=" and Darling's prefix ${PREFIX/#$HOME/\~}"
    printf 'Remove %s? [y/N] ' "$what"
    IFS= read -r answer || true
    if [[ $answer != [Yy]* ]]; then
      say "Nothing removed."
      return 0
    fi
  fi

  case ${action:-install} in
    install)
      if [[ -z $reviewed ]]; then
        banner
        printf '\n'
        setup_plan
      fi
      do_install ;;
    uninstall) do_uninstall "$PURGE" ;;
    quit) ;;
  esac
}

main "$@"; exit