# Release notes

## Unreleased

- Decode native Wayland custom cursors directly from their source bitmap,
  sharing the X11 decoder instead of drawing through Darling's image
  compositing path. Keep logical cursor size and hot spots when resampling
  larger representations, and clear invalid transparent color channels.
  Supply straight alpha to SDL and premultiplied alpha to locked-cursor
  Wayland surfaces, avoiding a second multiplication at translucent edges.
  The native shim and helper build; the reporting user's appearance still
  needs confirmation.
- Move the experimental Flatpak recipe to Freedesktop 26.08, bundling the
  GTK 4, libadwaita, Adwaita icons, Python bindings and WebKitGTK UI stack.
  Disable Vulkan rendering in those UI libraries while retaining Roblox's
  renderer choices.
  Match graphics and Vulkan layer extensions to the new runtime branch.
  Include the startup-patch helper missing from the previous Flatpak payload.
  Normalize WebKit bindings into the standard introspection lookup directory
  and apply cleanup across the dependency payload. The full experimental
  Freedesktop build succeeds locally; the reported overlay issue still needs
  confirmation.
- Prepare a checksum-pinned 0.21 `macoblox` AUR recipe and update the existing
  `macoblox-git` recipe. Build the shim and Wayland helper during package
  creation, include the startup-patch helper and desktop/MIME integration,
  declare audio and renderer dependencies, and generate `.SRCINFO` files.
  The stable package builds locally; no AUR submission has been made.
- Add Debian/Ubuntu packaging and a Fedora/openSUSE RPM spec using a shared
  payload installer and checksum-pinned release source preparation. Declare
  distro-specific UI and audio dependencies, prebuild the shim and native
  helper, and include desktop/MIME integration. Require a separate full
  Darling package. Debian and RPM source packages build; target-distro binary
  builds remain unverified.

## 0.21 — 2026-10-06

- Fix required-update loops when Roblox requests a different deployment
  channel from the launcher's default. Recover the validated version and
  upload ID from the client's update response, download that official upload,
  and verify the bundle version before replacing the installed client.
  Remember and show the client's update channel, handle gated channel checks
  without sending copied credentials, and avoid offering older deployments.
- Detect whether `pw-cat` supports raw audio. Use `pacat` through PipeWire's
  PulseAudio server on older versions, including PipeWire 1.0.5. Install the
  fallback on Debian/Ubuntu/Mint, retain playback errors in session logs and
  fall back after native playback failures.
- Apply intentional locked-cursor position changes to the visible overlay and
  frozen event coordinates on the event thread. Keep native confinement and
  unlock restoration, coalesce repeated requests, and suppress background
  cursor warps after focus loss. Shift-lock visibility still needs gameplay
  confirmation on the reporting system.
- Prevent public Darling source downloads from prompting for Git credentials.
  Skip the separate optional Swift SDK LFS downloads and exclude their pointer
  placeholders from source installs. The current Roblox client does not link
  that runtime; source-built Darling does not gain Swift SDK support.

### Packaging and verification

This is a source release. No Flatpak bundle is included in 0.21.

The native shim and framework stubs build successfully; changed Python sources
parse successfully and the installer passes shell syntax checks. The official
0.742 deployment requested in the report has the expected bundle version and
no linked Swift libraries. Mint playback, shift-lock icon visibility and a full
Darling source installation have not been verified on the reporting system.

## 0.20 — 2026-10-05

### Input fixes

- Replace the successful empty Carbon key-label translation with a valid ANSI
  fallback layout and bounded printable-key translation. This repairs the
  missing keyboard labels used by Roblox's E-key interaction prompts on both
  rendering backends; normal AppKit text input keeps its existing path.
- Pair custom Roblox UI scaling with absolute mouse-position compensation to
  correct the growing click offset at 200% and other custom scales. Preserve
  raw camera deltas and native cursor coordinates. Unsupported client input
  layouts skip custom scaling and report the reason in the launch log.
- Confine camera capture to the native game window, use absolute recentering,
  and restore the saved pointer position on unlock. Release capture on focus
  loss and window teardown; correct the X11 focus-event window field.
  Apply capture changes on the event thread, acknowledge cursor hiding for
  each lock transition, and recover from cursor-worker connection failures.
- Supply real attributed text substrings for Roblox's composition offsets and
  repair Darling's collapsed-selection anchor in the Roblox text handler.
- Add optional `MACOBLOX_TRACE_TEXT_INPUT=1` diagnostics for typing reports.
  They record lengths, ranges and composition state without typed content.

### Crash diagnostics

- Record session lifecycle state before cleanup, identify packaging and client
  version, and prevent guest log writes from overwriting lifecycle records.
  Preserve Darling warmup output and its exit status in the launch log.
- Expand opt-in fatal signal coverage and remove unsafe stack scans and
  signal-time symbol lookup. Record registers and bounded window-startup
  call markers; rename the setting to **Crash diagnostics**.
- Correct misleading EGL retry messages: report whether a retry occurred,
  its actual error, and the native window ID.

### Stability and responsiveness

- Add **Graphics card** under Game settings when multiple GPUs are detected.
  Store PCI identities, apply Mesa/NVIDIA PRIME selection to both the host
  probe and Darwin game, and use the selected adapter's measured VRAM budget.
  Automatic preserves desktop/terminal selections; unavailable stored adapters
  produce an actionable error instead of silently choosing another GPU. Clear
  stale Darling GPU selectors before restoring each session's selection.
- Recognize Roblox quitting for a required update even when it exits through
  the normal quit sentinel. Offer a fresh official-client download and keep
  pending game links until update recovery completes.
- Forward the host configuration directory to Linux graphics layers, avoiding
  attempts by layers such as LSFG to create settings under Darwin's `/Users`.
- Bound embedded-browser socket work per UI turn and cap queued frames,
  callbacks, pages and injected scripts. Dispose old pages and cancel pending
  work on disconnect; reject stale replies after a new game session connects.
  Release unused guest web views and suppress SIGPIPE on private socket writes.
- Serialize Discord IPC, coalesce presence updates on one worker, and disconnect
  after incomplete replies. Keep one metadata worker per tracker, atomically
  save a bounded game cache, and ignore activity callbacks from ended sessions.
  Start tracking only while Rich Presence is enabled and restart it on enable.
- Include queued DNS work in the upstream deadline and cap pending queries.
  Close active connections on shutdown, bound custom hostname lookups, and honor
  short/zero DNS TTLs with aged cached replies.
- Limit live-log reading and highlighting per UI update, retain fragmented
  UTF-8 lines, and bound displayed text and search matches. Reuse the scroll
  mark and preserve the selected search match during automatic updates.
- Skip unchanged X11 drawable geometry and visibility requests after validating
  the backend ABI and native-window identity. Preserve OpenGL context preparation
  and invalidate cached state after off-main-thread changes.
- Bound private X11 setup/visual queries to 250 ms and pointer snapshots/cursor
  transitions to 100 ms. Retry interrupted I/O, limit reply sizes, suppress
  SIGPIPE, and move the one-time Xauthority lookup off input/render callers.
- Read shader diagnostic sources from the current GL context instead of a
  process-global shader-name cache. Cap diagnostic allocations and avoid extra
  compilation-status waits when shader tracing is disabled.
- Make watchdog location capture signal-safe with atomic request ownership.
  Record only the interrupted instruction pointer without scanning application
  stacks or taking dynamic-loader locks.
- Poll and clean up game sessions outside GTK's main thread. Keep one watcher
  per launch and ignore callbacks from old sessions.
- Roll back partially initialized host audio, make audio shutdown idempotent,
  and reap microphone recorders after a forced shutdown.
- Recognize rootless Flatpak guests and orphans from their loader and mapped
  prefix runtime files, restoring game tracking and scoped cleanup without
  treating the shared sandbox mount namespace as ownership evidence.

### Packaging

This is a source release. No Flatpak bundle is included in 0.20.

### Verification and remaining work

The native shim, Wayland helper and framework stubs build successfully;
changed Python sources parse successfully. This stability/performance pass
has not run gameplay checks or benchmarks, and these changes have not been
verified on the reporting users' hardware. E-key prompt visibility and hybrid
laptop GPU offloading still need gameplay confirmation. Keyboard labels use
an ANSI fallback; selecting among multiple proprietary NVIDIA cards with
OpenGL requires an X11 provider selection.

The ordinary-character deletion report, the two previously reported crashes,
the pause after leaving games and intermittent graphics stutters remain under
investigation. Native Wayland remains experimental; Vulkan still uses Zink.

## 0.19 — 2026-10-04

### Bug fixes and settings

- Fix invisible X11 cursors with Vulkan (Zink), including fast image creation,
  cursor changes and recreated drawable windows.
- Allow independent Darwin DNS lookups to run concurrently instead of waiting
  behind one slow lookup. Retain serialized fallback when resolver isolation
  cannot be confirmed.
- Add **Roblox UI scale** in Game settings, from 100–400%. The `dpi_scale`
  setting now reaches Roblox; 200% enlarges its interface while preserving the
  rendering resolution. Default scaling stays at 100%.
- Stop adding `DFFlagDisableDPIScale` through the texture-quality preset.
  Existing custom flags are preserved; remove that flag to use UI scaling.
- Preserve the Flatpak Mesa driver discovery improvements and show useful
  Vulkan probe failures without inheriting Darling's preload library.

### Experimental Wayland

- Give concurrent AppKit views separate EGL drawables; repair resizing,
  hide/show and drawable lifetime handling.
- Repair recursive window lookup and display initialization cleanup.
- Improve NVIDIA Vulkan discovery without X11, while preserving explicit
  Vulkan driver selections and filters.
- Keep Native Wayland opt-in and experimental; X11 / Xwayland stays the default.

### Verification and remaining work

The native shim and helper built successfully. Sixteen compiled regression
fixtures and 99 Python tests passed. Actual Darling/X11 cursor and concurrent
DNS fixtures passed. The production UI scaling wrapper visibly enlarged the
actual Roblox interface at 200%; scaled click alignment still needs an
end-to-end check. Hardware EGL, shared contexts and Wayland window lifecycle
fixtures passed, but full native Wayland client presentation is not verified.

The pause after leaving a game remains unresolved; a focused client reproduced
an approximately 14-second frame gap. Intermittent Vulkan stutters, direct
client Vulkan and the other user's exact crash remain under investigation.
Vulkan rendering still uses Mesa Zink. Custom Darlingserver multiworker builds
are isolated experiments and are not included in this release.
The Flatpak is a testing package; this release does not claim new Flatpak
gameplay verification.

## 0.18 — 2026-10-04

### Setup

- The terminal installer now welcomes you, explains its destination and system
  packages, shows four installation steps, and gives clear next steps.
- First launch has a welcome, installation overview, download progress, retry
  screen and sign-in guidance. Reopen it from **Setup guide** in the menu.
- Existing installations keep their settings and Roblox session. Source updates
  back up local changes before replacing tracked files and preserve other files.

### Bug fixes

- Fix raw mouse and right-click camera freezes caused by synchronous modifier
  queries. Bound event processing and preserve mouse deltas and button events.
- Restore requested mouse capture after Alt-Tab or another focus change.
- Fix crashes while rendering Private Servers and other embedded pages; improve
  browser bridge timeouts and page closure handling.
- Improve Zink context switching, drawable handling, shader compatibility,
  GPU memory reporting and frame pacing.
- Reduce Darling file-close cleanup costs during the initial leave-game
  transition. Repair event wakeups and lock error handling.
- Keep cleanup within the selected Darling prefix and use stable process
  handles to avoid affecting other app environments.

### Verification and remaining work

The repaired input, focus, Private Servers and initial leave transition were
checked with the actual macOS client on an RTX 3060 Ti using Xwayland. Native
regressions and mocked launcher/process regressions passed. The installer and
setup changes received syntax/build checks and a local visual preview; system
package installation was not repeated for this release.

A later approximately 10–11 second pause after leaving a game is still under
investigation. Intermittent graphics stutters need more gameplay measurements.
Vulkan gameplay still uses Mesa Zink; the Metal-to-Vulkan work is a prototype.
Native Wayland is an opt-in experimental feature and is not complete.
