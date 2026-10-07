Name:           macoblox
Version:        0.21patch1
Release:        1%{?dist}
Summary:        Run the macOS Roblox client on Linux through Darling
License:        MIT AND OFL-1.1
URL:            https://github.com/aubree-lat/MacOBlox
Source0:        https://github.com/aubree-lat/MacOBlox/archive/refs/tags/v%{version}.tar.gz#/macoblox-%{version}.tar.gz
Source1:        macoblox-install-payload.sh
ExclusiveArch:  x86_64

BuildRequires:  darling
BuildRequires:  clang
BuildRequires:  lld
BuildRequires:  gcc-c++
BuildRequires:  python3
BuildRequires:  pkgconfig(sdl2)
BuildRequires:  pkgconfig(wayland-client)
BuildRequires:  pkgconfig(wayland-egl)
BuildRequires:  pkgconfig(wayland-cursor)

Requires:       darling
Requires:       python3
Requires:       unzip
Requires:       hicolor-icon-theme
Requires:       shared-mime-info
Requires:       desktop-file-utils
Requires:       libEGL.so.1()(64bit)
Requires:       libX11.so.6()(64bit)
Requires:       libXext.so.6()(64bit)
Requires:       libXfixes.so.3()(64bit)
Requires:       libXi.so.6()(64bit)
Requires:       libXcursor.so.1()(64bit)

%if 0%{?suse_version}
Requires:       python3-gobject
Requires:       python3-gobject-cairo
Requires:       python3-cairo
Requires:       typelib-1_0-Gtk-4_0 >= 4.14
Requires:       typelib-1_0-Adw-1 >= 1.5
Requires:       typelib-1_0-WebKit-6_0
Requires:       pulseaudio-utils
Recommends:     pipewire-tools
%else
Requires:       python3-gobject
Requires:       python3-cairo
Requires:       gtk4 >= 4.14
Requires:       libadwaita >= 1.5
Requires:       webkitgtk6.0
Requires:       pulseaudio-utils
Recommends:     pipewire-utils
%endif
Suggests:       mangohud

# Keep private Mach-O frameworks out of ELF dependency generation and strip
# only the Linux helper. Compile the private Python modules explicitly;
# icon/MIME cache handling comes from the package manager.
%global __requires_exclude_from ^%{_libdir}/macoblox/shim/(.*\\.framework/.*|.*\\.dylib)$
%global __provides_exclude_from ^%{_libdir}/macoblox/shim/.*$
%global __strip /bin/true
%global debug_package %{nil}

%description
Mac O' Blox provides a GTK launcher and compatibility shims for the macOS
Roblox player. It includes desktop shortcuts and Roblox link handlers.
Roblox is downloaded on first setup. A full Darling installation including
GUI frameworks and stubs is required.

%prep
%setup -q -n MacOBlox-%{version}

%build
MACOBLOX_BUILD_DIR="$PWD/native-build" ./build_debug_shim.sh
strip --strip-unneeded native-build/libmacoblox-wayland.so

%install
bash "%{SOURCE1}" "$PWD" "$PWD/native-build" "%{buildroot}" "%{_libdir}"
python3 -m compileall -q -d "%{_datadir}/macoblox/launcher" "%{buildroot}%{_datadir}/macoblox/launcher"

%files
%license %{_datadir}/licenses/macoblox/
%{_bindir}/macoblox
%{_libdir}/macoblox/
%{_datadir}/macoblox/
%{_datadir}/applications/wtf.aubree.MacOBlox*.desktop
%{_datadir}/applications/macoblox-roblox-window.desktop
%{_datadir}/icons/hicolor/*/apps/macoblox.png
%{_datadir}/mime/packages/wtf.aubree.MacOBlox.xml

%changelog
* Wed Oct 07 2026 MacOBlox maintainers <noreply@aubree.wtf> - 0.21patch1-1
- Update scaled input, X11 capture, cursor overlay and Roblox 0.742 transport compatibility.

* Tue Oct 06 2026 MacOBlox maintainers <noreply@aubree.wtf> - 0.21-1
- Initial Fedora and openSUSE package with prebuilt shims and desktop integration.
