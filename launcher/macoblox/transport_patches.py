"""Verified RakNet compatibility patches for known client executables.

Offsets and instructions were inspected in the x86_64 7411056 and 7421053
releases downloaded from Roblox's deployment CDN.
Normalize only the listed patches before checking the complete executable's
SHA-256. A client update or an unexpected modification is unsupported; no
heuristic search or partial application is allowed.
"""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import tempfile


@dataclass(frozen=True)
class Site:
    offset: int
    original: bytes
    patched: bytes
    name: str


@dataclass(frozen=True)
class Release:
    size: int
    sha256: str
    sites: tuple[Site, ...]
    compatible_changes: tuple[Site, ...] = ()


RELEASES = (Release(
    123526704,
    "be95441279431786043ca06a918b1c3ef172f54848b716df8ecf2e440921ebff",
    (
        Site(0x4A38D78, bytes.fromhex("554889e5"), bytes.fromhex("31c0c390"), "transport selection"),
        Site(0x536EC65, b"\x01", b"\xff", "fallback connection"),
        Site(0x5370858, b"\x75", b"\xeb", "dummy connection first caller"),
        Site(0x5370A1E, b"\x75", b"\xeb", "dummy connection second caller"),
    ),
    (Site(0x1153F70, b"\x89\xf3", b"\x31\xdb", "startup throttle"),),
), Release(
    # 0.742.0.7421053, official upload version-f9247f9560044102.
    # Each relocated site was inspected with its branch target and flag;
    # the size/hash below describe the pristine executable in that archive.
    125285200,
    "ac9ad59b3aa3805bf2d1c89fb6ff9c7941bf2e57af17a81cbc26abe94a022c47",
    (
        Site(0x4B5D12B, bytes.fromhex("554889e5"), bytes.fromhex("31c0c390"), "transport selection"),
        Site(0x54B2F8D, b"\x01", b"\xff", "fallback connection"),
        Site(0x54B4B80, b"\x75", b"\xeb", "dummy connection first caller"),
        Site(0x54B4D46, b"\x75", b"\xeb", "dummy connection second caller"),
    ),
    (Site(0x11A21C8, b"\x89\xf3", b"\x31\xdb", "startup throttle"),),
))


def plan_transport_patch(data, releases=RELEASES):
    """Return (status, complete replacement), without mutating anything."""
    for release in releases:
        if len(data) != release.size:
            continue
        normalized = bytearray(data)
        sites = release.sites + release.compatible_changes
        if any(data[s.offset:s.offset + len(s.original)] not in (s.original, s.patched)
               or len(s.original) != len(s.patched) for s in sites):
            continue
        for site in sites:
            normalized[site.offset:site.offset + len(site.original)] = site.original
        if hashlib.sha256(normalized).hexdigest() != release.sha256:
            continue
        replacement = bytearray(data)
        for site in release.sites:
            replacement[site.offset:site.offset + len(site.patched)] = site.patched
        if replacement == data:
            return "already patched", None
        return "patched", replacement
    return "unsupported client; transport binary left unchanged", None


def apply_transport_patch(binary):
    binary = Path(binary)
    data = binary.read_bytes()
    status, replacement = plan_transport_patch(data)
    if replacement is None:
        return status
    file_stat = binary.stat()
    fd, temporary = tempfile.mkstemp(prefix=".RobloxPlayer-transport-", dir=binary.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), stat.S_IMODE(file_stat.st_mode))
            output.write(replacement)
            output.flush()
            os.fsync(output.fileno())
        # An updater or another patcher may have touched the client while the
        # replacement was prepared. Refuse to overwrite its changes.
        if binary.read_bytes() != data:
            raise RuntimeError("The client changed while transport patches were being prepared")
        os.replace(temporary, binary)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return status
