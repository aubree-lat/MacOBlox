"""Selected-prefix ownership for Darling's Flatpak runtime without overlayfs.

The Flatpak launcher and guests share one mount namespace. That namespace is
never ownership evidence. A native mldr executable and prefix-owned mapped
runtime files identify guests even after their darlingserver has disappeared.
All evidence is read afresh; callers retain their existing pidfd signal checks.
"""
import os
from pathlib import Path
import re


_RUNTIME_SUFFIXES = (
    "/usr/lib/libSystem.B.dylib",
    "/usr/lib/system/libsystem_kernel.dylib",
)
_MAPS_LIMIT = 2 * 1024 * 1024
_ENV_LIMIT = 256 * 1024
_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")


def _read_bounded(path, limit):
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Process evidence exceeds its bound")
    return data


def _identity(process):
    """Return this user's current process generation and parent, or None."""
    if process.stat().st_uid != os.getuid():
        return None
    # comm may contain whitespace and parentheses; fields start after its end.
    fields = _read_bounded(process / "stat", 8192).rsplit(b")", 1)[1].split()
    if fields[0] in (b"Z", b"X"):
        return None
    return int(fields[19]), int(fields[1])  # starttime and PPID


def _runtime_prefixes(maps):
    """Return every mapped runtime prefix, plus those with executable code.

    mldr itself loads dyld from a compiled host path, even without overlayfs;
    that shared host dyld is not selected-prefix or conflicting-prefix proof.
    The copied libSystem and libsystem_kernel paths provide that proof.
    """
    prefixes, executable = set(), set()
    for line in maps.decode(errors="surrogateescape").splitlines():
        fields = line.split(None, 5)
        if len(fields) != 6 or not fields[5].startswith("/"):
            continue
        if not fields[4].isdigit() or int(fields[4]) == 0:
            continue
        filename = _OCTAL_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), fields[5])
        if filename.endswith(" (deleted)"):
            filename = filename[:-10]
        for suffix in _RUNTIME_SUFFIXES:
            if filename.endswith(suffix):
                prefix = Path(filename[:-len(suffix)] or "/").resolve()
                # A symlink into another runtime must not establish ownership.
                if Path(filename).resolve() != prefix / suffix.lstrip("/"):
                    return None, None
                prefixes.add(prefix)
                if "x" in fields[1]:
                    executable.add(prefix)
                break
    return prefixes, executable


def _selected_server_ancestor(process, prefix):
    """Prove a live, same-user server ancestor from its native exe and argv."""
    seen = set()
    chain = []
    for _ in range(64):
        identity = _identity(process)
        if identity is None or process.name in seen:
            return False
        seen.add(process.name)
        executable = Path(os.readlink(process / "exe")).resolve()
        chain.append((process, identity, executable))
        if executable.name == "darlingserver":
            arguments = _read_bounded(process / "cmdline", 16384).split(b"\0")
            if len(arguments) < 2 or not arguments[0] or not arguments[1]:
                return False
            if Path(os.fsdecode(arguments[0])).name != "darlingserver":
                return False
            supplied = Path(os.fsdecode(arguments[1]))
            if not supplied.is_absolute():
                supplied = Path(os.readlink(process / "cwd")) / supplied
            if supplied.resolve() != prefix:
                return False
            # A cached PPID is insufficient after an ancestor exits or execs.
            # Revalidate every link, including its process generation, before
            # accepting the live server as bootstrap ownership evidence.
            for ancestor, generation, native_executable in chain:
                if (_identity(ancestor) != generation or
                        Path(os.readlink(ancestor / "exe")).resolve() != native_executable):
                    return False
            if _read_bounded(process / "cmdline", 16384).split(b"\0") != arguments:
                return False
            supplied = Path(os.fsdecode(arguments[1]))
            if not supplied.is_absolute():
                supplied = Path(os.readlink(process / "cwd")) / supplied
            return supplied.resolve() == prefix
        parent = identity[1]
        if parent <= 1:
            return False
        process = process.parent / str(parent)
    return False


def _bootstrap_provenance(process, prefix):
    """Cover loader bootstrap before mapped runtime code is visible.

    mldr removes its socket key and renames its root key before entering the
    guest. Accept an exact root only with an exact socket or a verified live
    server ancestor; arbitrary inherited environment values are insufficient.
    """
    values = {}
    keys = {b"__mldr_DYLD_ROOT_PATH", b"DYLD_ROOT_PATH", b"__mldr_sockpath"}
    for entry in _read_bounded(process / "environ", _ENV_LIMIT).split(b"\0"):
        key, separator, value = entry.partition(b"=")
        if separator and key in keys:
            values.setdefault(key, set()).add(value)
    roots = set()
    for key in (b"__mldr_DYLD_ROOT_PATH", b"DYLD_ROOT_PATH"):
        for value in values.get(key, ()):
            candidate = Path(os.fsdecode(value))
            if not candidate.is_absolute():
                return False
            roots.add(candidate.resolve())
    if roots != {prefix}:
        return False
    sockets = values.get(b"__mldr_sockpath", set())
    if sockets:
        expected = prefix / ".darlingserver.sock"
        for value in sockets:
            candidate = Path(os.fsdecode(value))
            if not candidate.is_absolute() or candidate.resolve() != expected:
                return False
        return True
    return _selected_server_ancestor(process, prefix)


def rootless_process_in_prefix(pid, prefix, noroot_lib):
    """Whether a native mldr process is proven to use this rootless prefix.

    Enable only when the caller has configured NOROOT_LIB. Unknown, oversized,
    unreadable, mixed-prefix, reused, or ordinary host processes are rejected.
    Mapping proof also covers reparented orphan daemons; ancestry alone never
    identifies orphans and a shared mount namespace never identifies guests.
    """
    if not noroot_lib or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        selected = Path(prefix).resolve()
        if not selected.is_absolute() or selected == Path("/"):
            return False
        process = Path("/proc") / str(pid)
        before = _identity(process)
        if before is None:
            return False
        executable = Path(os.readlink(process / "exe")).resolve()
        if executable.name != "mldr":
            return False
        prefixes, executable_prefixes = _runtime_prefixes(_read_bounded(process / "maps", _MAPS_LIMIT))
        if prefixes is None or prefixes - {selected}:
            return False
        proven = selected in executable_prefixes
        if not proven and not prefixes:
            proven = _bootstrap_provenance(process, selected)
        return bool(proven and _identity(process) == before
                    and Path(os.readlink(process / "exe")).resolve() == executable)
    except (OSError, ValueError, IndexError, RuntimeError):
        return False
