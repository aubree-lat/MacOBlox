"""Host GPU inventory and per-game PRIME selection (no GTK dependency)."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess

GPU_ENVIRONMENT = (
    "DRI_PRIME", "__NV_PRIME_RENDER_OFFLOAD", "__NV_PRIME_RENDER_OFFLOAD_PROVIDER",
    "__GLX_VENDOR_LIBRARY_NAME", "__VK_LAYER_NV_optimus", "MESA_VK_DEVICE_SELECT",
    "MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE", "__EGL_VENDOR_LIBRARY_FILENAMES",
    "VK_DRIVER_FILES", "VK_ICD_FILENAMES", "VK_ADD_DRIVER_FILES",
    "VK_LOADER_DRIVERS_SELECT", "VK_LOADER_DRIVERS_DISABLE",
)
_PCI = re.compile(r"([0-9a-fA-F]{4,8}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-7])")


def pci_address(value):
    match = _PCI.fullmatch(value.strip()) if isinstance(value, str) else None
    if not match or int(match[1], 16) > 0xffff:
        return None
    return f"{int(match[1], 16):04x}:{match[2].lower()}:{match[3].lower()}.{match[4]}"


def _read(path, limit=4096):
    try:
        with Path(path).open() as source:
            return source.read(limit).strip()
    except (OSError, UnicodeError):
        return ""


def _names():
    """Optional friendly names; inventory/selection work without pciutils."""
    program = shutil.which("lspci")
    if not program:
        return {}
    try:
        result = subprocess.run([program, "-D", "-mm"], capture_output=True,
                                text=True, timeout=1)
        if result.returncode:
            return {}
        names = {}
        for line in result.stdout[:256 * 1024].splitlines():
            fields = shlex.split(line)
            address = pci_address(fields[0]) if len(fields) >= 4 else None
            if address:
                names[address] = f"{fields[2]} {fields[3]}"
        return names
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def enumerate_gpus():
    """Unique DRM adapters identified by PCI address, never card index."""
    adapters = {}
    try:
        nodes = sorted(Path("/sys/class/drm").iterdir())
    except OSError:
        return []
    for node in nodes:
        if not re.fullmatch(r"card\d+|renderD\d+", node.name):
            continue
        device = node / "device"
        properties = dict(line.split("=", 1) for line in _read(device / "uevent").splitlines()
                          if "=" in line)
        address = pci_address(properties.get("PCI_SLOT_NAME", ""))
        if not address:
            continue
        try:
            vendor = int(_read(device / "vendor"), 16)
            product = int(_read(device / "device"), 16)
            if int(_read(device / "class"), 16) >> 16 != 3:
                continue
            driver = (device / "driver").resolve(strict=True).name
        except (OSError, ValueError):
            continue
        adapter = adapters.setdefault(address, {
            "id": "pci:" + address, "pci": address, "vendor": vendor,
            "product": product, "driver": driver, "device": str(device.resolve()),
            "render_node": None,
        })
        if node.name.startswith("renderD"):
            adapter["render_node"] = "/dev/dri/" + node.name
    names = _names() if adapters else {}
    nvidia_count = sum(adapter["vendor"] == 0x10de and adapter["driver"] == "nvidia"
                       for adapter in adapters.values())
    for address, adapter in adapters.items():
        model = _read(Path("/proc/driver/nvidia/gpus") / address / "information")
        nvidia_name = next((line.partition(":")[2].strip() for line in model.splitlines()
                            if line.startswith("Model:")), "")
        vendor = {0x10de: "NVIDIA", 0x1002: "AMD", 0x8086: "Intel"}.get(
            adapter["vendor"], "GPU")
        name = nvidia_name or names.get(address) or f"{vendor} {adapter['product']:04x}"
        adapter["name"] = name
        adapter["label"] = f"{name} ({address})"
        adapter["nvidia_count"] = nvidia_count
    return [adapters[address] for address in sorted(adapters)]


def selected_gpu(selection, adapters=None):
    if selection == "auto":
        return None
    if not isinstance(selection, str) or not selection.startswith("pci:"):
        raise RuntimeError("The saved graphics-card choice is invalid. Select Automatic in Settings.")
    address = pci_address(selection[4:])
    if address is None:
        raise RuntimeError("The saved graphics-card choice is invalid. Select Automatic in Settings.")
    adapters = enumerate_gpus() if adapters is None else adapters
    for adapter in adapters:
        if adapter["pci"] == address:
            return adapter
    raise RuntimeError("The selected graphics card is unavailable. Reconnect it or select Automatic in Settings.")


def _nvidia_egl_manifest():
    roots = [Path(root) for root in ("/usr/share", "/usr/local/share", "/etc", "/app/share")]
    roots.extend(path / "share" for path in sorted(Path("/usr/lib/x86_64-linux-gnu/GL").glob("*")))
    for root in roots:
        for manifest in sorted((root / "glvnd/egl_vendor.d").glob("*.json")):
            try:
                data = json.loads(_read(manifest, 64 * 1024))
                library = data["ICD"]["library_path"]
                if isinstance(library, str) and "nvidia" in Path(library).name.lower():
                    return str(manifest)
            except (ValueError, KeyError, TypeError):
                continue
    return None


def gpu_environment(adapter, renderer):
    """Explicit choices override inherited GPU selectors, not ICD filters.

    Automatic keeps terminal/desktop selections and forwards them to the guest.
    Mesa's PCI selector applies to OpenGL and Vulkan; ! restricts Vulkan's
    physical-device list. NVIDIA EGL uses PRIME rather than Mesa's selector.
    """
    inherited = {name: os.environ[name] for name in GPU_ENVIRONMENT if name in os.environ}
    if adapter is None:
        if renderer == "vulkan":
            inherited.pop("__EGL_VENDOR_LIBRARY_FILENAMES", None)
        return inherited
    address = adapter["pci"]
    prime = "pci-" + address.replace(":", "_").replace(".", "_")
    nvidia = adapter["vendor"] == 0x10de and adapter["driver"] == "nvidia"
    # Vendor/product IDs are shared by identical cards. Use the PCI selector
    # alone, without a competing Mesa selector or inherited NVIDIA provider.
    inherited.pop("MESA_VK_DEVICE_SELECT", None)
    inherited.pop("__NV_PRIME_RENDER_OFFLOAD_PROVIDER", None)
    inherited.update({
        "DRI_PRIME": prime + ("!" if renderer == "vulkan" else ""),
        "__NV_PRIME_RENDER_OFFLOAD": "1" if nvidia else "0",
        "__GLX_VENDOR_LIBRARY_NAME": "nvidia" if nvidia else "mesa",
        "__VK_LAYER_NV_optimus": "NVIDIA_only" if nvidia else "non_NVIDIA_only",
        "MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE": "1",
    })
    if nvidia:
        # NVIDIA EGL selects RandR providers, not PCI IDs. Never claim a PCI
        # choice selected one particular GPU when several NVIDIA screens exist.
        # Automatic remains available for an explicit desktop/terminal provider.
        if renderer == "opengl" and adapter.get("nvidia_count", 1) > 1:
            raise RuntimeError("Selecting between multiple NVIDIA GPUs with OpenGL needs an X11 provider. Select Automatic and use __NV_PRIME_RENDER_OFFLOAD_PROVIDER for that driver setup, or select Vulkan.")
    if renderer == "opengl":
        if nvidia:
            manifest = _nvidia_egl_manifest()
            if not manifest:
                raise RuntimeError("The selected NVIDIA card needs its EGL driver. Install the NVIDIA graphics driver or select Automatic.")
        else:
            from .graphics import mesa_egl_manifest
            manifest = mesa_egl_manifest()
            if not manifest:
                raise RuntimeError("The selected graphics card needs Mesa EGL. Install Mesa or select Automatic.")
        inherited["__EGL_VENDOR_LIBRARY_FILENAMES"] = str(manifest)
    else:
        # Zink must keep the Mesa EGL vendor set by renderer_environment.
        inherited.pop("__EGL_VENDOR_LIBRARY_FILENAMES", None)
    return inherited


def host_graphics_paths():
    """Linux layers see host paths even when Darwin rewrites HOME to /Users."""
    configuration = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return {"XDG_CONFIG_HOME": str(configuration.absolute())}
