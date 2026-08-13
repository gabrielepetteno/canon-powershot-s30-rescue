"""Mass-storage backend: memory cards and cameras that mount as a filesystem.

This is the backend that actually rescues most archives, and it is the one that
must never surprise anyone. It needs no drivers, no daemons and no third-party
packages: the card is already mounted by the operating system, so the whole job
is ``os.walk`` plus a careful copy loop.

It covers two situations that look different to the user but are identical to
the code:

* a CompactFlash / SD card pulled out of the camera and put into a reader —
  the only way to reach a pre-PTP body such as the Canon PowerShot S30, whose
  USB protocol is proprietary Canon and understood by nothing on a modern OS;
* any camera from roughly 2003 onwards that presents itself as USB Mass
  Storage when you plug it in.

Design rules that are not negotiable here
-----------------------------------------
* **Read-only except in :meth:`delete`.** :meth:`detect`, :meth:`list_files`
  and :meth:`download` never create, truncate or rename anything on the card.
  The single exception is :meth:`supports_delete`, which must find out whether
  the card accepts writes *before* the user commits to erasing anything; it
  creates one empty probe file and removes it immediately, and only when the
  cheap non-writing checks came back inconclusive.
* **Copy, fsync, then rename.** Bytes land in a ``.part`` file inside the
  destination directory and only reach their final name through
  :func:`os.replace` after :func:`os.fsync`. An interrupted run therefore
  leaves obvious debris, never a plausible-looking truncated photo.
* **One bad file must not end the rescue.** A 20-year-old card usually has a
  few unreadable sectors. Per-file OS errors become failed outcomes and the
  loop continues; only failures that doom the whole operation (unwritable
  destination, card unplugged) are raised as :class:`CameraError`.
* **Delete file-by-file, inside the card only.** Never ``rmdir``, never
  ``shutil.rmtree``, never a glob. Every path is re-checked to be inside the
  mount point before :func:`os.remove` touches it, so a malformed listing
  cannot reach into the user's home directory.

Messages raised from here are shown to the user verbatim, so they are written
in plain language and say what to try next. They are deliberately not routed
through :mod:`retrocam.i18n`: backend errors carry OS-level detail that the GUI
wraps in its own translated framing, and importing the translation layer into a
backend would drag GUI state into a worker thread.
"""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import string
import sys
import uuid
from typing import Dict, List, Optional, Sequence, Tuple

from ..model import (
    BackendKind,
    CameraError,
    CameraFile,
    CameraInfo,
    CameraNotFound,
    CancelToken,
    DeleteOutcome,
    DownloadOutcome,
    Progress,
    ProgressCallback,
    TransferAborted,
)
from .base import Availability, CameraBackend, noop_progress

__all__ = ["MassStorageBackend"]


# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #

#: Copy chunk. 1 MiB keeps a USB 2.0 reader saturated while still giving the
#: cancel token roughly ten checks per second on the slowest media we target.
COPY_BUFFER = 1024 * 1024

#: Emit an intra-file progress tick at least this often, so the bar moves even
#: on a 300 MB movie clip instead of freezing for a minute.
PROGRESS_EVERY_BYTES = 4 * 1024 * 1024

#: Cancel/progress check cadence while listing, in files.
LIST_TICK_EVERY = 200

#: Junk that every desktop OS sprinkles onto removable media. These are not
#: photographs and copying them only confuses the user's destination folder.
_JUNK_NAMES = frozenset(
    {
        "thumbs.db",
        "thumbs.db:encryptable",
        "desktop.ini",
        ".ds_store",
        "autorun.inf",
        "ehthumbs.db",
        "picasa.ini",
        "zbthumbnail.info",  # Canon's own thumbnail cache
    }
)

#: Directories that belong to the host OS, not to the camera. Present on cards
#: because Windows/macOS wrote them at mount time; never worth copying.
_JUNK_DIRS = frozenset(
    {
        "system volume information",
        "$recycle.bin",
        "recycler",
        ".spotlight-v100",
        ".trashes",
        ".fseventsd",
        ".temporaryitems",
        ".documentrevisions-v100",
        "lost.dir",
        "found.000",
    }
)

#: DCIM sub-folder naming is a de-facto standard: three digits followed by a
#: five-character vendor tag (``118CANON``, ``100MSDCF``, ``100OLYMP``). Mapping
#: the tag back to a brand lets the device list say "Canon card" instead of
#: "NO NAME", which is the difference between recognising your camera and not.
#: Order matters — the first substring that matches wins, so specific tags come
#: before ones that could appear inside another.
_DCIM_VENDOR_HINTS: Tuple[Tuple[str, str], ...] = (
    ("CANON", "Canon"),
    ("EOS", "Canon"),
    ("MSDCF", "Sony"),
    ("NIKON", "Nikon"),
    ("NCD", "Nikon"),
    ("NC_D", "Nikon"),
    ("OLYMP", "Olympus"),
    ("_FUJI", "Fujifilm"),
    ("FUJI", "Fujifilm"),
    ("PENTX", "Pentax"),
    ("_PANA", "Panasonic"),
    ("LUMIX", "Panasonic"),
    ("CASIO", "Casio"),
    ("RICOH", "Ricoh"),
    ("SIGMA", "Sigma"),
    ("LEICA", "Leica"),
    ("KODAK", "Kodak"),
    ("MINLT", "Minolta"),
    ("SSCAM", "Samsung"),
    ("GOPRO", "GoPro"),
    ("APPLE", "Apple"),
    ("ANDRO", "Android"),
)

#: Canon writes a management database next to DCIM. On the reference S30 card
#: this is the strongest brand signal available, and it is never deleted.
_CANON_MARKER_DIRS = frozenset({"canonmsc", "canon_a"})

# Windows GetDriveType return values (winbase.h).
_DRIVE_UNKNOWN = 0
_DRIVE_NO_ROOT_DIR = 1
_DRIVE_REMOVABLE = 2
_DRIVE_FIXED = 3
_DRIVE_REMOTE = 4
_DRIVE_CDROM = 5
_DRIVE_RAMDISK = 6

_SEM_FAILCRITICALERRORS = 0x0001
_FILE_READ_ONLY_VOLUME = 0x00080000

#: Mount-point roots to sweep on Linux. ``/media/<label>`` is the classic
#: layout, ``/media/<user>/<label>`` is what udisks2 does on Ubuntu, and
#: ``/run/media/<user>/<label>`` is the Fedora/Arch equivalent — hence the
#: two-level sweep. ``/mnt`` is only checked one level deep: it is where people
#: mount things by hand, and under WSL it holds the whole Windows filesystem,
#: which is far too expensive to walk.
_LINUX_MOUNT_ROOTS: Tuple[Tuple[str, bool], ...] = (
    ("/media", True),
    ("/run/media", True),
    ("/mnt", False),
)

#: Upper bound on children examined per mount root, so a pathological directory
#: cannot turn device detection into a multi-second freeze.
_MAX_CHILDREN_PER_ROOT = 128


# --------------------------------------------------------------------------- #
# Error translation
# --------------------------------------------------------------------------- #

#: errno -> the half-sentence that tells a non-technical user what went wrong
#: and what to do about it. Anything not listed falls back to ``strerror``.
_ERRNO_HINTS: Dict[int, str] = {
    errno.EACCES: (
        "permission denied. On macOS, give your terminal (or the app) access to "
        "removable volumes in System Settings > Privacy & Security > Files and "
        "Folders; on Linux, check that your user owns the mount point"
    ),
    errno.EPERM: (
        "the operating system refused the operation. Try running the app as the "
        "user who mounted the card"
    ),
    errno.EROFS: (
        "the card is write-protected. Slide the lock switch on the SD adapter to "
        "the unlocked position and re-insert it"
    ),
    errno.EIO: (
        "the card reported a physical read error (a bad sector). This file may be "
        "damaged; try a different card reader before giving up on it"
    ),
    errno.ENOENT: "the file is no longer there. Was the card removed or edited meanwhile?",
    errno.ENOSPC: "the destination disk is full. Free some space and try again",
    errno.EDQUOT: "you have reached your disk quota on the destination",
    errno.ENODEV: "the card was disconnected. Plug it back in and press Detect again",
    errno.ENXIO: "the card was disconnected. Plug it back in and press Detect again",
    errno.ESTALE: "the card was unmounted while it was being read. Re-insert it and retry",
    errno.EBUSY: "the file is locked by another program. Close any photo viewer and retry",
    errno.ENOTDIR: "the path on the card is not a folder any more (corrupt filesystem?)",
    errno.EISDIR: "that entry is a folder, not a file",
    errno.ENAMETOOLONG: "the file name is too long for the destination filesystem",
    errno.EMFILE: "too many files are open. Close other applications and retry",
    errno.ENFILE: "the system ran out of file handles. Close other applications and retry",
}


def _explain(exc: OSError, action: str, path: str = "") -> str:
    """Turn an OSError into one sentence a photographer can act on.

    ``action`` is a verb phrase in context, e.g. ``"read IMG_1870.JPG"``. The
    resulting string is what the GUI shows, so it never contains a traceback or
    a bare errno.
    """
    hint = _ERRNO_HINTS.get(getattr(exc, "errno", None) or -1)
    if hint is None:
        hint = (exc.strerror or str(exc) or "unknown error").strip()
        # Keep OS wording, just make it read as a clause rather than a shout.
        hint = hint[0].lower() + hint[1:] if hint else "unknown error"
    where = " (%s)" % path if path else ""
    return "Could not %s%s: %s." % (action, where, hint)


# --------------------------------------------------------------------------- #
# Filesystem helpers (module level: they are useful to tests and to the GUI)
# --------------------------------------------------------------------------- #


def _norm(path: str) -> str:
    """Comparison key for a path: absolute, and case-folded where the OS is."""
    return os.path.normcase(os.path.abspath(path))


def _is_within(root: str, path: str) -> bool:
    """True when ``path`` really lives inside ``root``.

    Symlinks are resolved first, which is the point: this is the guard that
    stops a corrupt or hostile listing from making :meth:`delete` reach a file
    outside the card.
    """
    try:
        root_r = _norm(os.path.realpath(root))
        path_r = _norm(os.path.realpath(path))
    except OSError:
        return False
    return path_r == root_r or path_r.startswith(root_r.rstrip(os.sep) + os.sep)


def _is_boot_volume(path: str) -> bool:
    """True when the candidate mount point is really the running system's root.

    macOS puts a firmlink to ``/`` inside ``/Volumes`` (usually
    ``/Volumes/Macintosh HD``), and it must never be offered as a camera card:
    listing it would walk the whole startup disk.
    """
    try:
        if os.path.realpath(path) == os.sep:
            return True
        return os.path.samefile(path, os.sep)
    except OSError:
        return False


def _safe_scandir_names(path: str, limit: int = 0) -> List[Tuple[str, bool]]:
    """``[(name, is_dir), ...]`` for one directory, or ``[]`` when unreadable.

    Detection sweeps directories the user may have no permission for (other
    users' mount points, empty card slots). Those are normal conditions, not
    errors, so they must not raise.
    """
    out = []  # type: List[Tuple[str, bool]]
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    is_dir = False
                out.append((entry.name, is_dir))
                if limit and len(out) >= limit:
                    break
    except OSError:
        return []
    return out


def _find_dcim(root: str) -> Optional[str]:
    """Absolute path of the DCIM folder inside ``root``, or None.

    The lookup is case-insensitive because a card formatted in-camera writes
    ``DCIM`` but an ext4/exFAT image written by a Linux tool may hold ``dcim``,
    and Linux will not find one when asked for the other. The literal spellings
    are tried first so the common case costs a single ``stat``.

    A volume that *is* a DCIM folder (the user pointed us straight at it, or an
    archive was restored one level down) is accepted as-is.
    """
    try:
        if os.path.basename(root.rstrip(os.sep)).upper() == "DCIM" and os.path.isdir(
            root
        ):
            return os.path.abspath(root)
    except OSError:
        return None

    for spelling in ("DCIM", "dcim", "Dcim"):
        candidate = os.path.join(root, spelling)
        try:
            if os.path.isdir(candidate):
                return os.path.abspath(candidate)
        except OSError:
            return None

    # Fall back to a listing for exotic casings such as 'DCim'.
    for name, is_dir in _safe_scandir_names(root):
        if is_dir and name.upper() == "DCIM":
            return os.path.abspath(os.path.join(root, name))
    return None


def _camera_hint(mount: str, dcim: str) -> str:
    """Brand guessed from the card's own folder names, or ``""``.

    ``/DCIM/118CANON`` on the reference S30 card is enough to label the device
    "Canon card", which is far more recognisable than the FAT volume label
    (often literally ``NO NAME``).
    """
    for name, is_dir in _safe_scandir_names(mount, limit=_MAX_CHILDREN_PER_ROOT):
        if is_dir and name.lower() in _CANON_MARKER_DIRS:
            return "Canon"

    for name, is_dir in _safe_scandir_names(dcim, limit=_MAX_CHILDREN_PER_ROOT):
        if not is_dir:
            continue
        upper = name.upper()
        for tag, vendor in _DCIM_VENDOR_HINTS:
            if tag in upper:
                return vendor
    return ""


def _volume_is_read_only(mount: str) -> Optional[bool]:
    """Whether the volume is mounted read-only, without writing to it.

    Returns None when the platform cannot tell us — the caller then falls back
    to an actual (empty) write probe. Deliberately cheap and side-effect free so
    it can run during detection.
    """
    if os.name == "nt":
        info = _windows_volume_information(mount)
        if info is None:
            return None
        return info[2]

    statvfs = getattr(os, "statvfs", None)
    if statvfs is None:  # pragma: no cover - non-POSIX, non-Windows
        return None
    try:
        flags = statvfs(mount).f_flag
    except (OSError, AttributeError, ValueError):
        return None
    read_only_bit = getattr(os, "ST_RDONLY", 1)
    return bool(flags & read_only_bit)


# --------------------------------------------------------------------------- #
# Windows plumbing (ctypes only — never a hard dependency)
# --------------------------------------------------------------------------- #


def _kernel32():
    """Private handle on kernel32, or None when we are not on Windows.

    A private :class:`ctypes.WinDLL` is used rather than the cached
    ``ctypes.windll.kernel32`` so that setting ``argtypes`` cannot disturb any
    other library in the process. Every caller treats ``None`` as "fall back to
    plain filesystem checks": drive enumeration must degrade, never explode.
    """
    if os.name != "nt":
        return None
    try:
        return ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):  # pragma: no cover - Windows only
        return None


def _windows_volume_information(root: str) -> Optional[Tuple[str, str, bool]]:
    """``(label, filesystem, read_only)`` for a drive root, or None.

    None means "no media in the slot" as often as it means "call failed", and
    both are handled identically by the caller.
    """
    k32 = _kernel32()
    if k32 is None:
        return None
    try:  # pragma: no cover - Windows only
        from ctypes import wintypes

        fn = k32.GetVolumeInformationW
        fn.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPWSTR,
            wintypes.DWORD,
        ]
        fn.restype = wintypes.BOOL

        label = ctypes.create_unicode_buffer(261)
        fsname = ctypes.create_unicode_buffer(261)
        serial = wintypes.DWORD()
        maxlen = wintypes.DWORD()
        flags = wintypes.DWORD()
        ok = fn(
            root,
            label,
            261,
            ctypes.byref(serial),
            ctypes.byref(maxlen),
            ctypes.byref(flags),
            fsname,
            261,
        )
        if not ok:
            return None
        return (label.value, fsname.value, bool(flags.value & _FILE_READ_ONLY_VOLUME))
    except Exception:
        # ctypes marshalling problems must never break device detection.
        return None


class _WindowsQuietErrors(object):
    """Suppress the "There is no disk in the drive" modal while probing.

    Without this, touching an empty card slot pops a system dialog *behind* the
    Tk window and the app simply looks hung. ``SetThreadErrorMode`` is scoped to
    the calling thread, so the rest of the process keeps its normal behaviour.
    """

    def __init__(self) -> None:
        self._k32 = _kernel32()
        self._previous = None

    def __enter__(self) -> "_WindowsQuietErrors":
        if self._k32 is None:
            return self
        try:  # pragma: no cover - Windows only
            from ctypes import wintypes

            previous = wintypes.DWORD()
            fn = self._k32.SetThreadErrorMode
            fn.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
            fn.restype = wintypes.BOOL
            if fn(_SEM_FAILCRITICALERRORS, ctypes.byref(previous)):
                self._previous = previous.value
        except Exception:
            self._previous = None
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._k32 is not None and self._previous is not None:
            try:  # pragma: no cover - Windows only
                self._k32.SetThreadErrorMode(self._previous, None)
            except Exception:
                pass
        return False


def _windows_drive_roots() -> List[Tuple[str, str, int]]:
    """``(root, kind_label, priority)`` for every drive worth probing.

    The task brief says to keep ``DRIVE_REMOVABLE``; real hardware forces one
    addition. Many built-in and USB 3 card readers present their media as
    ``DRIVE_FIXED``, so filtering strictly on ``DRIVE_REMOVABLE`` silently loses
    exactly the readers people buy for CompactFlash. Removable drives are
    therefore returned first, fixed non-system drives after them, and network
    shares / optical drives never — and since a volume is only accepted once a
    DCIM folder is found on it, admitting fixed drives cannot produce a bogus
    "camera" for the system disk.

    Falls back to plain ``isdir`` on every letter when ctypes is unavailable, so
    a hardened or exotic Python still detects cards.
    """
    roots = []  # type: List[Tuple[str, str, int]]
    system_root = _norm(os.environ.get("SystemDrive", "C:") + os.sep)
    k32 = _kernel32()

    with _WindowsQuietErrors():
        letters = list(string.ascii_uppercase)
        if k32 is not None:
            try:  # pragma: no cover - Windows only
                from ctypes import wintypes

                k32.GetLogicalDrives.restype = wintypes.DWORD
                mask = int(k32.GetLogicalDrives())
                if mask:
                    letters = [
                        ch
                        for i, ch in enumerate(string.ascii_uppercase)
                        if mask & (1 << i)
                    ]
            except Exception:
                pass

        for ch in letters:
            root = "%s:%s" % (ch, os.sep)
            drive_type = None
            if k32 is not None:
                try:  # pragma: no cover - Windows only
                    from ctypes import wintypes

                    k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
                    k32.GetDriveTypeW.restype = wintypes.UINT
                    drive_type = int(k32.GetDriveTypeW(root))
                except Exception:
                    drive_type = None

            if drive_type is None:
                # ctypes unavailable: existence is the only signal we have.
                try:
                    if os.path.isdir(root):
                        roots.append((root, "Drive %s:" % ch, 1))
                except OSError:
                    pass
                continue

            if drive_type == _DRIVE_REMOVABLE:
                roots.append((root, "Removable drive %s:" % ch, 0))
            elif drive_type == _DRIVE_FIXED and _norm(root) != system_root:
                roots.append((root, "Drive %s:" % ch, 1))
            elif drive_type == _DRIVE_RAMDISK and _norm(root) != system_root:
                roots.append((root, "RAM drive %s:" % ch, 2))
            # DRIVE_REMOTE, DRIVE_CDROM, DRIVE_NO_ROOT_DIR and DRIVE_UNKNOWN are
            # never cameras and probing them is slow or noisy.

    roots.sort(key=lambda item: (item[2], item[0]))
    return roots


def _posix_mount_roots() -> List[Tuple[str, str, int]]:
    """``(mount, kind_label, priority)`` for macOS and Linux mount points."""
    roots = []  # type: List[Tuple[str, str, int]]
    seen = set()  # type: set

    def add(path: str, label: str, priority: int) -> None:
        key = _norm(path)
        if key in seen:
            return
        seen.add(key)
        roots.append((path, label, priority))

    if sys.platform == "darwin":
        for name, is_dir in _safe_scandir_names(
            "/Volumes", limit=_MAX_CHILDREN_PER_ROOT
        ):
            if is_dir:
                add(os.path.join("/Volumes", name), "Mounted volume", 0)

    for root, deep in _LINUX_MOUNT_ROOTS:
        for name, is_dir in _safe_scandir_names(root, limit=_MAX_CHILDREN_PER_ROOT):
            if not is_dir:
                continue
            child = os.path.join(root, name)
            add(child, "Mounted volume", 0)
            if not deep:
                continue
            # udisks2 nests one level under the user name: /media/<user>/<label>.
            for sub, sub_is_dir in _safe_scandir_names(
                child, limit=_MAX_CHILDREN_PER_ROOT
            ):
                if sub_is_dir:
                    add(os.path.join(child, sub), "Mounted volume", 1)

    roots.sort(key=lambda item: (item[2], item[0]))
    return roots


def _candidate_mounts() -> List[Tuple[str, str]]:
    """Every mount point worth checking for a DCIM folder, best first."""
    raw = _windows_drive_roots() if os.name == "nt" else _posix_mount_roots()
    return [(path, label) for path, label, _priority in raw]


# --------------------------------------------------------------------------- #
# The backend
# --------------------------------------------------------------------------- #


class MassStorageBackend(CameraBackend):
    """Reads photos from any card or camera the OS has already mounted.

    Stateless by design: everything needed to service a camera travels in
    :attr:`CameraInfo.raw` (``mount`` and ``dcim``), because the user may unplug
    the reader between two GUI actions. The only instance state is a small cache
    used to answer :meth:`supports_delete`, which the base class defines without
    a camera argument.
    """

    kind = BackendKind.MASS_STORAGE
    display_name = "Memory card or USB drive"
    description = (
        "Reads a memory card in a reader, or a camera that appears as a USB "
        "drive. Needs no drivers and works on every operating system."
    )

    def __init__(self, scan_whole_volume: bool = False) -> None:
        """
        ``scan_whole_volume`` widens :meth:`list_files` from the DCIM tree to
        the entire volume. Off by default because DCIM is where cameras write;
        available because cards that were partly reorganised on a computer keep
        photos elsewhere, and a rescue tool that cannot see them has failed.
        """
        self.scan_whole_volume = scan_whole_volume
        # UI-hint cache only. The authoritative handle is always CameraInfo.raw;
        # this exists solely because supports_delete() takes no camera argument.
        self._last_mount = ""  # type: str
        self._last_dcim = ""  # type: str
        self._writable_cache = {}  # type: Dict[str, bool]

    # -- capability probing ------------------------------------------------ #

    @classmethod
    def is_available(cls) -> Availability:
        """Always available: this backend only uses the filesystem.

        There is nothing to install and nothing to probe, which is exactly why
        this backend is first in the registry — it is the fallback that works
        when every clever transport has failed.
        """
        return (True, "")

    @classmethod
    def install_hint(cls) -> str:
        """Nothing to install; the operating system already provides this."""
        return ""

    # -- discovery --------------------------------------------------------- #

    def detect(self, progress: ProgressCallback = noop_progress) -> List[CameraInfo]:
        """Find every mounted volume that holds a DCIM folder.

        Returning an empty list is the normal answer when nothing is plugged in.
        Unreadable mount points (someone else's card, an empty slot) are skipped
        silently rather than raised: they are not the user's problem to fix.
        """
        # A fresh scan is the moment a swapped card becomes visible, and a
        # different card can appear at the same mount path, so no writability
        # verdict from before this point may be reused.
        self._writable_cache.clear()

        try:
            candidates = _candidate_mounts()
        except Exception as exc:  # defensive: enumeration must never kill detect
            raise CameraError(
                "Could not list the drives on this computer (%s). Try unplugging "
                "and re-plugging the card reader." % exc
            )

        found = []  # type: List[CameraInfo]
        seen_dcim = set()  # type: set
        total = len(candidates)

        for index, (mount, kind_label) in enumerate(candidates):
            progress(
                Progress(
                    phase="detect",
                    index=index,
                    total=total,
                    name=os.path.basename(mount.rstrip(os.sep)) or mount,
                )
            )

            if _is_boot_volume(mount):
                continue  # the startup disk is never a camera card
            try:
                if not os.path.isdir(mount):
                    continue
            except OSError:
                continue

            dcim = _find_dcim(mount)
            if dcim is None:
                continue
            key = _norm(dcim)
            if key in seen_dcim:
                continue  # same volume reachable through two mount paths
            seen_dcim.add(key)

            found.append(self._describe(mount, dcim, kind_label))

        progress(
            Progress(
                phase="detect",
                index=total,
                total=total,
                message="Found %d card(s) with photos." % len(found),
            )
        )
        if len(found) == 1:
            self._remember(found[0].raw.get("mount", ""), found[0].raw.get("dcim", ""))
        return found

    def _describe(self, mount: str, dcim: str, kind_label: str) -> CameraInfo:
        """Build the CameraInfo the GUI shows for one volume."""
        label = self._volume_label(mount)
        vendor = _camera_hint(mount, dcim)

        if vendor and label:
            model = "%s card (%s)" % (vendor, label)
        elif vendor:
            model = "%s card" % vendor
        elif label:
            model = label
        else:
            model = "Memory card"

        # statvfs / GetVolumeInformationW only read metadata, so asking here
        # costs nothing and never writes to the card.
        read_only = _volume_is_read_only(mount)

        bits = [kind_label]
        if read_only is True:
            bits.append("write-protected")
        detail = " - ".join(bits)

        return CameraInfo(
            model=model,
            port=os.path.abspath(mount),
            kind=self.kind,
            detail=detail,
            raw={
                "mount": os.path.abspath(mount),
                "dcim": dcim,
                "label": label,
                "vendor": vendor,
                "read_only": read_only,
            },
        )

    @staticmethod
    def _volume_label(mount: str) -> str:
        """Human name of the volume.

        On macOS and Linux the mount point's last component *is* the label. On
        Windows the root is ``E:\\``, which has no basename, so we ask the OS —
        and accept an empty answer, since an unlabelled FAT card is normal.
        """
        if os.name == "nt":
            info = _windows_volume_information(mount)
            if info and info[0]:
                return info[0]
            return ""
        return os.path.basename(mount.rstrip(os.sep))

    # -- listing ----------------------------------------------------------- #

    def list_files(
        self,
        camera: CameraInfo,
        progress: ProgressCallback = noop_progress,
        cancel: Optional[CancelToken] = None,
    ) -> List[CameraFile]:
        """Walk the DCIM tree and return every real file on the card.

        Sizes and timestamps come straight from ``os.stat``, so they are exact —
        which matters, because the transfer engine uses the size as the
        verification baseline and therefore as the delete gate.

        A directory that cannot be read (bad sector, permission) is reported
        through ``progress`` and skipped; the rest of the card is still
        rescued. Only a card that has vanished entirely raises.
        """
        mount, dcim = self._resolve(camera)
        self._remember(mount, dcim)

        walk_root = mount if self.scan_whole_volume else dcim
        # Device paths are reported relative to the volume, so a file in
        # <mount>/DCIM/118CANON becomes '/DCIM/118CANON'. Using the DCIM parent
        # as the base keeps that true even when the volume *is* the DCIM folder.
        base = os.path.dirname(dcim.rstrip(os.sep)) or dcim
        if _norm(base) == _norm(dcim):  # dcim is a filesystem root
            base = dcim

        files = []  # type: List[CameraFile]
        problems = []  # type: List[str]

        def on_error(exc: OSError) -> None:
            # os.walk swallows errors by default; a rescue tool must not.
            problems.append(
                _explain(exc, "read a folder", getattr(exc, "filename", "") or "")
            )

        try:
            for dirpath, dirnames, filenames in os.walk(walk_root, onerror=on_error):
                if cancel is not None:
                    cancel.raise_if_cancelled()

                # Prune in place so os.walk never descends into OS junk.
                dirnames[:] = sorted(
                    d for d in dirnames if not self._is_junk_dir(dirpath, d)
                )

                folder = self._device_folder(base, dirpath)
                for name in sorted(filenames):
                    if self._is_junk_file(dirpath, name):
                        continue
                    src = os.path.join(dirpath, name)
                    try:
                        st = os.stat(src)
                    except OSError as exc:
                        problems.append(_explain(exc, "read %s" % name, src))
                        continue
                    if not stat.S_ISREG(st.st_mode):
                        continue  # devices, fifos and stray symlinks are not photos

                    files.append(
                        CameraFile(
                            folder=folder,
                            name=name,
                            size=int(st.st_size),
                            mtime=float(st.st_mtime),
                            raw={"src": src, "mount": mount},
                        )
                    )

                    if len(files) % LIST_TICK_EVERY == 0:
                        if cancel is not None:
                            cancel.raise_if_cancelled()
                        progress(
                            Progress(
                                phase="list",
                                index=len(files),
                                total=0,
                                name=name,
                                message="Found %d files so far..." % len(files),
                            )
                        )
        except TransferAborted:
            raise
        except OSError as exc:
            raise CameraError(_explain(exc, "read the card", walk_root))

        files.sort(key=lambda cf: (cf.folder, cf.name))

        message = "Found %d file(s)." % len(files)
        if problems:
            # Surfaced, not hidden: the user must know part of the card is
            # unreadable before they decide to erase anything.
            message += " %d folder(s) or file(s) could not be read." % len(problems)
        progress(
            Progress(phase="list", index=len(files), total=len(files), message=message)
        )
        for problem in problems[:20]:
            progress(Progress(phase="list", message=problem))
        return files

    @staticmethod
    def _device_folder(base: str, dirpath: str) -> str:
        """POSIX-style device folder for a real directory, e.g. '/DCIM/118CANON'."""
        try:
            rel = os.path.relpath(dirpath, base)
        except ValueError:  # different drives on Windows
            return "/"
        if rel in (".", os.curdir, ""):
            return "/"
        rel = rel.replace(os.sep, "/")
        if os.altsep:
            rel = rel.replace(os.altsep, "/")
        return "/" + rel.strip("/")

    @staticmethod
    def _is_junk_dir(parent: str, name: str) -> bool:
        """Directories written by the host OS, not by the camera."""
        lowered = name.lower()
        if lowered in _JUNK_DIRS:
            return True
        if name.startswith("."):
            return True
        return MassStorageBackend._has_hidden_attribute(os.path.join(parent, name))

    @staticmethod
    def _is_junk_file(parent: str, name: str) -> bool:
        """Files that are not photographs: hidden, AppleDouble or OS metadata."""
        if name.startswith("."):
            # Covers both '.DS_Store' and macOS AppleDouble sidecars '._IMG_1870.JPG',
            # which carry resource forks, not image data.
            return True
        if name.lower() in _JUNK_NAMES:
            return True
        if name.startswith(".rcr-") or name.endswith(".part"):
            return True  # our own temp files, if a destination was ever on the card
        return MassStorageBackend._has_hidden_attribute(os.path.join(parent, name))

    @staticmethod
    def _has_hidden_attribute(path: str) -> bool:
        """True for a Windows hidden/system entry. Always False elsewhere."""
        if os.name != "nt":
            return False
        try:  # pragma: no cover - Windows only
            attrs = os.stat(path).st_file_attributes  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            return False
        hidden = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2)
        system = getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 0x4)
        return bool(attrs & (hidden | system))

    # -- download ---------------------------------------------------------- #

    def download(
        self,
        camera: CameraInfo,
        files: Sequence[CameraFile],
        dest_dir: str,
        progress: ProgressCallback = noop_progress,
        cancel: Optional[CancelToken] = None,
        skip_existing: bool = True,
    ) -> List[DownloadOutcome]:
        """Copy files off the card, one temp-file-and-rename at a time.

        Returns exactly one outcome per input file, in the input order, so the
        caller's "78 of 82 recovered" count is trustworthy.

        ``ok`` reports the *transfer* only — bytes written equals bytes on the
        card. The transfer engine verifies the result afterwards and produces
        the final outcome that the delete gate reads.

        A skipped file (``skip_existing`` and an identical-size copy already at
        the destination) comes back with ``skipped=True`` and ``ok=False``: no
        bytes moved, so this backend cannot claim the copy is good. The engine
        can verify the file on disk and upgrade the outcome. Erring this way
        means a resumed run may re-verify, never that an unchecked file becomes
        deletable.
        """
        mount, _dcim = self._resolve(camera)
        dest_dir = os.path.abspath(dest_dir)

        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as exc:
            raise CameraError(_explain(exc, "create the destination folder", dest_dir))
        if not os.path.isdir(dest_dir):
            raise CameraError(
                "The destination folder %s does not exist and could not be created. "
                "Pick another folder." % dest_dir
            )

        outcomes = []  # type: List[DownloadOutcome]
        total = len(files)
        # Destination paths already spoken for by this batch, so that two cards
        # folders holding the same base name cannot resolve to the same file.
        claimed = set()  # type: set

        for index, camera_file in enumerate(files):
            if cancel is not None:
                try:
                    cancel.raise_if_cancelled()
                except TransferAborted as abort:
                    # Best effort: hand back what was already rescued so a
                    # cancelling caller can still report it.
                    setattr(abort, "outcomes", outcomes)
                    raise
            outcome = self._download_one(
                mount,
                camera_file,
                dest_dir,
                index,
                total,
                progress,
                cancel,
                skip_existing,
                outcomes,
                claimed,
            )
            if outcome.dest_path:
                claimed.add(_norm(outcome.dest_path))
            outcomes.append(outcome)

        progress(
            Progress(
                phase="download",
                index=total,
                total=total,
                message="Copied %d of %d file(s)."
                % (sum(1 for o in outcomes if o.ok), total),
            )
        )
        return outcomes

    def _download_one(
        self,
        mount: str,
        camera_file: CameraFile,
        dest_dir: str,
        index: int,
        total: int,
        progress: ProgressCallback,
        cancel: Optional[CancelToken],
        skip_existing: bool,
        done_so_far: List[DownloadOutcome],
        claimed: set,
    ) -> DownloadOutcome:
        """Copy one file. Never raises except for cancellation."""
        try:
            src = self._source_path(mount, camera_file)
        except CameraError as exc:
            return DownloadOutcome(
                file=camera_file, dest_path=None, ok=False, error=str(exc)
            )

        try:
            st = os.stat(src)
        except OSError as exc:
            return DownloadOutcome(
                file=camera_file,
                dest_path=None,
                ok=False,
                error=_explain(exc, "read %s" % camera_file.name, src),
            )
        if not stat.S_ISREG(st.st_mode):
            return DownloadOutcome(
                file=camera_file,
                dest_path=None,
                ok=False,
                error="%s is not a regular file on the card." % camera_file.name,
            )

        source_size = int(st.st_size)
        # The listing's size is the number verification will trust later. If the
        # card disagrees now, something changed underneath us (a swapped card is
        # the dangerous case) and the copy must not be certified.
        listing_mismatch = camera_file.size_known and camera_file.size != source_size

        if skip_existing:
            existing = self._existing_copy(dest_dir, camera_file, source_size, claimed)
            if existing is not None:
                progress(
                    Progress(
                        phase="download",
                        index=index,
                        total=total,
                        name=camera_file.name,
                        bytes_done=source_size,
                        bytes_total=source_size,
                        message="Already downloaded, skipped: %s" % camera_file.name,
                    )
                )
                return DownloadOutcome(
                    file=camera_file, dest_path=existing, ok=False, skipped=True
                )

        progress(
            Progress(
                phase="download",
                index=index,
                total=total,
                name=camera_file.name,
                bytes_done=0,
                bytes_total=source_size,
            )
        )

        temp_path = os.path.join(dest_dir, ".rcr-%s.part" % uuid.uuid4().hex)
        written = 0
        try:
            with open(src, "rb") as source:
                with open(temp_path, "wb") as target:
                    next_tick = PROGRESS_EVERY_BYTES
                    while True:
                        if cancel is not None and cancel.cancelled():
                            raise TransferAborted("Download cancelled by the user.")
                        chunk = source.read(COPY_BUFFER)
                        if not chunk:
                            break
                        target.write(chunk)
                        written += len(chunk)
                        if written >= next_tick:
                            next_tick = written + PROGRESS_EVERY_BYTES
                            progress(
                                Progress(
                                    phase="download",
                                    index=index,
                                    total=total,
                                    name=camera_file.name,
                                    bytes_done=written,
                                    bytes_total=source_size,
                                )
                            )
                    # Force the bytes to the platter before the file is allowed
                    # to take its final name: a power cut mid-rename must not
                    # leave an empty file wearing a good photo's name.
                    target.flush()
                    os.fsync(target.fileno())

            dest_path = CameraBackend.safe_dest_path(dest_dir, camera_file)
            os.replace(temp_path, dest_path)
        except TransferAborted as abort:
            self._discard(temp_path)
            setattr(abort, "outcomes", list(done_so_far))
            raise
        except OSError as exc:
            self._discard(temp_path)
            return DownloadOutcome(
                file=camera_file,
                dest_path=None,
                ok=False,
                error=_explain(exc, "copy %s" % camera_file.name, src),
            )
        except Exception as exc:  # pragma: no cover - defensive
            self._discard(temp_path)
            return DownloadOutcome(
                file=camera_file,
                dest_path=None,
                ok=False,
                error="Could not copy %s: %s." % (camera_file.name, exc),
            )

        # Keep the camera's timestamp: for a 20-year-old archive the mtime is
        # often the only surviving record of when the photo was taken.
        mtime = camera_file.mtime if camera_file.mtime is not None else st.st_mtime
        try:
            os.utime(dest_path, (mtime, mtime))
        except OSError:
            pass  # cosmetic only; never fail a good copy over a timestamp

        progress(
            Progress(
                phase="download",
                index=index,
                total=total,
                name=camera_file.name,
                bytes_done=written,
                bytes_total=source_size,
            )
        )

        if written != source_size:
            return DownloadOutcome(
                file=camera_file,
                dest_path=dest_path,
                ok=False,
                error="Copied %d of %d bytes - the card stopped responding."
                % (written, source_size),
            )
        if listing_mismatch:
            return DownloadOutcome(
                file=camera_file,
                dest_path=dest_path,
                ok=False,
                error=(
                    "%s changed size on the card (listed %d bytes, found %d). "
                    "Re-scan the card before deleting anything."
                    % (camera_file.name, camera_file.size, source_size)
                ),
            )
        return DownloadOutcome(file=camera_file, dest_path=dest_path, ok=True)

    @staticmethod
    def _existing_copy(
        dest_dir: str,
        camera_file: CameraFile,
        expected_size: int,
        claimed: Optional[set] = None,
    ) -> Optional[str]:
        """Path of a copy of *this exact file* already at the destination, or None.

        Resume support is the one place where a wrong answer costs a photograph:
        claiming a file is already downloaded lets the engine verify the file on
        disk, mark it good, and ultimately erase the original from the card. A
        match therefore has to be convincing on three counts.

        1. **Name.** Both names :meth:`CameraBackend.safe_dest_path` can produce
           are tried, so last run's ``119CANON_IMG_1870.JPG`` is recognised.
        2. **Not already claimed in this batch.** Two Canon folders legitimately
           hold the same base name once the frame counter rolls over. Without
           this check, ``119CANON/IMG_1870.JPG`` would match the flat
           ``IMG_1870.JPG`` that ``118CANON``'s *different* photo wrote — same
           name, possibly same size, entirely different image. Skipping names an
           earlier file in this batch already took mirrors exactly what
           ``safe_dest_path`` does when writing, so resume and fresh runs agree.
        3. **Size, and timestamp when both are known.** Every file this backend
           writes gets the card's mtime stamped onto it, so a genuine earlier
           copy always matches within the 2-second granularity FAT allows. When
           it does not match we simply copy again: a duplicate is an annoyance,
           a wrongly skipped file is a lost photograph.
        """
        flat = os.path.basename(camera_file.name.replace("\\", "/")).strip()
        if not flat or flat in (".", ".."):
            return None

        folder_tag = camera_file.folder.rstrip("/").split("/")[-1] or "DCIM"
        for name in (flat, "%s_%s" % (folder_tag, flat)):
            candidate = os.path.join(dest_dir, name)
            if claimed is not None and _norm(candidate) in claimed:
                continue
            try:
                if not os.path.isfile(candidate):
                    continue
                st = os.stat(candidate)
            except OSError:
                continue
            if st.st_size != expected_size:
                continue
            if camera_file.mtime is not None:
                if abs(st.st_mtime - camera_file.mtime) > 2.0:
                    continue
            return candidate
        return None

    @staticmethod
    def _discard(temp_path: str) -> None:
        """Remove a partial download. Failure here is never worth reporting."""
        try:
            os.remove(temp_path)
        except OSError:
            pass

    # -- delete ------------------------------------------------------------ #

    def delete(
        self,
        camera: CameraInfo,
        files: Sequence[CameraFile],
        progress: ProgressCallback = noop_progress,
        cancel: Optional[CancelToken] = None,
    ) -> List[DeleteOutcome]:
        """Erase the given files from the card. Irreversible.

        Exactly the files passed in, one :func:`os.remove` each. No directory is
        ever removed, no pattern is ever expanded, and nothing outside the mount
        point can be touched even if a listing is corrupt. Emptied Canon folders
        and the ``CANONMSC`` management database are deliberately left in place:
        the camera expects them, and the contract forbids folder deletion.

        Every file is also re-``stat``-ed and matched against the listing it came
        from immediately before it is removed — see :meth:`_still_the_listed_file`.
        The caller has proved that *a* file was downloaded and verified; only the
        card itself can prove that the file still sitting at that path is the one
        that was downloaded, and a card swapped in the reader between the
        download and the erase presents exactly the same paths.

        Note for the caller's UI: FAT writes may sit in the OS cache, so the
        user should still eject the card properly before pulling it out.
        """
        mount, _dcim = self._resolve(camera)
        outcomes = []  # type: List[DeleteOutcome]
        total = len(files)

        for index, camera_file in enumerate(files):
            if cancel is not None:
                cancel.raise_if_cancelled()

            progress(
                Progress(
                    phase="delete", index=index, total=total, name=camera_file.name
                )
            )

            try:
                src = self._source_path(mount, camera_file)
            except CameraError as exc:
                outcomes.append(
                    DeleteOutcome(file=camera_file, ok=False, error=str(exc))
                )
                continue

            # A symlink on a camera card is either impossible or malicious;
            # removing one could unlink a target outside the card.
            try:
                if os.path.islink(src):
                    outcomes.append(
                        DeleteOutcome(
                            file=camera_file,
                            ok=False,
                            error="%s is a shortcut, not a photo - left untouched "
                            "for safety." % camera_file.name,
                        )
                    )
                    continue
                if os.path.isdir(src):
                    outcomes.append(
                        DeleteOutcome(
                            file=camera_file,
                            ok=False,
                            error="%s is a folder. This app never deletes folders."
                            % camera_file.name,
                        )
                    )
                    continue
            except OSError as exc:
                outcomes.append(
                    DeleteOutcome(
                        file=camera_file,
                        ok=False,
                        error=_explain(exc, "check %s" % camera_file.name, src),
                    )
                )
                continue

            same, why = self._still_the_listed_file(src, camera_file)
            if not same:
                outcomes.append(DeleteOutcome(file=camera_file, ok=False, error=why))
                continue

            try:
                os.remove(src)
            except FileNotFoundError:
                outcomes.append(
                    DeleteOutcome(
                        file=camera_file,
                        ok=False,
                        error="%s is no longer on the card." % camera_file.name,
                    )
                )
            except OSError as exc:
                outcomes.append(
                    DeleteOutcome(
                        file=camera_file,
                        ok=False,
                        error=_explain(exc, "delete %s" % camera_file.name, src),
                    )
                )
            else:
                outcomes.append(DeleteOutcome(file=camera_file, ok=True))

        deleted = sum(1 for o in outcomes if o.ok)
        # Deleting frees space, so any cached writability verdict is stale.
        self._writable_cache.pop(_norm(mount), None)
        progress(
            Progress(
                phase="delete",
                index=total,
                total=total,
                message="Deleted %d of %d file(s) from the card." % (deleted, total),
            )
        )
        return outcomes

    @staticmethod
    def _still_the_listed_file(src: str, camera_file: CameraFile) -> Tuple[bool, str]:
        """Is the file at ``src`` still the one the listing described?

        Returns ``(ok, reason_when_not)``.

        The delete gate upstream proves that a file of this device path was
        copied and byte-verified. It cannot prove that the card in the reader is
        still the same card: eject one CompactFlash and insert another and the
        operating system re-mounts it at the very same path — ``/Volumes/NO
        NAME`` on macOS, ``E:\\`` on Windows — where a second Canon body has
        very plausibly written its own ``/DCIM/118CANON/IMG_1870.JPG``. Deleting
        by path alone would erase a photograph that was never downloaded.

        Size and modification time both come from :func:`os.stat` during
        listing, so on the card we actually read they match to the bit and this
        check never fires. Two different photographs matching on both is not
        credible. The timestamp is compared with the 2-second tolerance FAT's
        granularity forces; a card whose size or time disagrees is refused and
        left completely alone.
        """
        try:
            st = os.stat(src)
        except OSError as exc:
            return False, _explain(exc, "re-check %s" % camera_file.name, src)

        if camera_file.size_known and int(st.st_size) != camera_file.size:
            return False, (
                "%s is not the file that was downloaded (it is now %d bytes, the "
                "copy that was verified was %d). Nothing was erased - re-scan the "
                "card." % (camera_file.name, int(st.st_size), camera_file.size)
            )

        if camera_file.mtime is not None:
            if abs(float(st.st_mtime) - camera_file.mtime) > 2.0:
                return False, (
                    "%s changed on the card since it was read, so it may not be "
                    "the photo that was downloaded. Nothing was erased - re-scan "
                    "the card." % camera_file.name
                )

        return True, ""

    def supports_delete(self) -> bool:
        """Whether this card accepts writes at all.

        Answered in three escalating steps, stopping at the first definite one:

        1. the mount flags (``statvfs`` on POSIX, ``GetVolumeInformationW`` on
           Windows) — pure metadata, no write;
        2. ``os.access`` — cheap, and enough to catch permission problems;
        3. one empty probe file created and immediately removed inside DCIM.

        Step 3 is the only write this backend performs outside :meth:`delete`,
        and it exists so a locked card greys the Delete button out *before* the
        user commits, instead of failing halfway through an erase. The result is
        cached per mount point: probing on every UI repaint would pointlessly
        exercise a fragile card.

        Returns True when no card has been touched yet — nothing is selected in
        that state, and per-file errors would still explain any refusal.
        """
        mount, dcim = self._last_mount, self._last_dcim
        if not mount or not os.path.isdir(mount):
            return True

        key = _norm(mount)
        if key in self._writable_cache:
            return self._writable_cache[key]

        writable = self._probe_writable(mount, dcim)
        self._writable_cache[key] = writable
        return writable

    @staticmethod
    def _probe_writable(mount: str, dcim: str) -> bool:
        """Do the actual three-step writability test. Never raises."""
        if _volume_is_read_only(mount) is True:
            return False

        target = dcim if dcim and os.path.isdir(dcim) else mount
        if not os.access(target, os.W_OK):
            return False

        # Zero bytes, exclusive create, removed in the same breath. On FAT this
        # only touches a directory entry; no data cluster is allocated.
        probe = os.path.join(target, ".rcr-writetest-%s.tmp" % uuid.uuid4().hex[:8])
        try:
            handle = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError:
            return False
        else:
            try:
                os.close(handle)
            except OSError:
                pass
            return True
        finally:
            try:
                os.remove(probe)
            except OSError:
                pass

    # -- shared internals -------------------------------------------------- #

    def _remember(self, mount: str, dcim: str) -> None:
        """Record the last card touched, for :meth:`supports_delete`.

        Switching to a different mount point drops every cached writability
        verdict: the cheap way to be wrong here is to remember "writable" for a
        card that has since been swapped for a locked one.
        """
        if mount and self._last_mount and _norm(mount) != _norm(self._last_mount):
            self._writable_cache.clear()
        self._last_mount = mount
        self._last_dcim = dcim

    def _resolve(self, camera: CameraInfo) -> Tuple[str, str]:
        """``(mount, dcim)`` for a camera, re-checked against the live filesystem.

        The card may have been ejected since :meth:`detect` ran, so nothing from
        ``raw`` is trusted without a fresh ``isdir``.
        """
        mount = str(camera.raw.get("mount") or camera.port or "")
        if not mount:
            raise CameraNotFound(
                "This card has no mount point recorded. Press Detect again."
            )
        mount = os.path.abspath(mount)
        try:
            mounted = os.path.isdir(mount)
        except OSError:
            mounted = False
        if not mounted:
            raise CameraNotFound(
                "The card at %s is not connected any more. Plug it back in and "
                "press Detect again." % mount
            )

        dcim = str(camera.raw.get("dcim") or "")
        try:
            fresh = bool(dcim) and os.path.isdir(dcim)
        except OSError:
            fresh = False
        if not fresh:
            found = _find_dcim(mount)
            if found is None:
                raise CameraNotFound(
                    "No DCIM folder found on %s. Is this the right card, or was "
                    "it re-inserted while the app was running?" % mount
                )
            dcim = found

        self._remember(mount, dcim)
        return mount, dcim

    @staticmethod
    def _source_path(mount: str, camera_file: CameraFile) -> str:
        """Absolute path on the card for one listed file.

        Prefers the exact path captured during listing, falls back to rebuilding
        it from the device path, and in both cases refuses anything that does
        not resolve to somewhere inside the mount point. That containment check
        is what keeps a corrupt listing from turning :meth:`delete` into an
        arbitrary-file remover.
        """
        src = str(camera_file.raw.get("src") or "")
        if not src:
            # Device paths are '/DCIM/...' relative to the volume, so rebuild
            # from the mount point itself. '..' components are dropped rather
            # than resolved: no listing has any business climbing out of DCIM.
            parts = [
                part
                for part in camera_file.path.strip("/").split("/")
                if part not in ("", ".", "..")
            ]
            if not parts:
                raise CameraError(
                    "This file has no usable path on the card. Re-scan the card."
                )
            src = os.path.join(mount, *parts)

        src = os.path.abspath(src)
        if not _is_within(mount, src):
            raise CameraError(
                "%s does not point inside the card (%s). Refusing to touch it - "
                "re-scan the card." % (camera_file.path, mount)
            )
        return src
