# AUR packaging

Two independent recipes are ready for AUR repositories:

| Package | Recipe | Source |
| --- | --- | --- |
| `macoblox` | `macoblox/PKGBUILD` and `macoblox/.SRCINFO` | Checked release archive, currently 0.21patch1 |
| `macoblox-git` | `PKGBUILD` and `.SRCINFO` | Current GitHub checkout |

The recipes build the Darling shim and native Wayland helper during package
creation. They install the launcher, icons, desktop entries, MIME definition
and startup-patch helper. Roblox itself is downloaded by the launcher on first
setup. Settings and sign-in stay in the user's directories; package updates
are handled by pacman or an AUR helper.

## Build locally

Install Arch's `base-devel` group first. The `darling` dependency is provided by
[`darling-bin`](https://aur.archlinux.org/packages/darling-bin) or the full
[`darling-git`](https://aur.archlinux.org/packages/darling-git) package; a CLI-only
Darling installation is insufficient. Install the provider with an AUR helper,
then build the stable package from this checkout:

```bash
cd packaging/aur/macoblox
makepkg -si
```

For the development package, use `packaging/aur` instead. Optional dependencies
in each recipe describe the Vulkan renderer, MangoHud and native PipeWire
playback. PulseAudio-compatible playback is included through `libpulse`.

## Submit or update

The stable `macoblox` name was available when these recipes were prepared.
`macoblox-git` already has an AUR listing; its existing maintainer controls that
repository. An AUR account and SSH key are required to submit a package.

In a separate AUR checkout, copy only the selected recipe's `PKGBUILD` and
`.SRCINFO`. Regenerate metadata after editing a recipe:

```bash
makepkg --printsrcinfo > .SRCINFO
git add PKGBUILD .SRCINFO
git commit -m 'Update MacOBlox package'
git push
```

For stable updates, change `pkgver`, reset `pkgrel` to 1, update the release
archive checksum and regenerate `.SRCINFO`. Increase `pkgrel` for packaging
changes that keep the same release version. Do not submit generated package
archives, build directories, downloaded clients or user logs.
