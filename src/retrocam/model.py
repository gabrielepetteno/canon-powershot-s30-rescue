"""Core data types shared by every backend and by the GUI.

This module is the single source of truth for the shapes that cross module
boundaries. It deliberately has **no dependencies outside the standard library**
and must stay import-cheap: the GUI imports it at startup, before any backend
has been probed.

Nothing here performs I/O. Backends produce these objects, the transfer engine
consumes them, and the GUI renders them.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional

__all__ = [
    "BackendKind",
    "CameraInfo",
    "CameraFile",
    "VerifyResult",
    "DownloadOutcome",
    "DeleteOutcome",
    "Progress",
    "ProgressCallback",
    "CancelToken",
    "CameraError",
    "CameraNotFound",
    "BackendUnavailable",
    "TransferAborted",
]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class CameraError(Exception):
    """Base class for every recoverable error raised by a backend.

    Backends must raise this (or a subclass) instead of leaking subprocess,
    COM or OSError exceptions, so the GUI can show one clear message instead of
    a stack trace. The message is user-facing: write it in plain language and,
    where possible, say what to try next.
    """


class CameraNotFound(CameraError):
    """No camera is currently reachable through this backend."""


class BackendUnavailable(CameraError):
    """The backend cannot run at all here (missing tool, wrong OS, no permission)."""


class TransferAborted(CameraError):
    """The user cancelled the operation through the cancel token."""


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #


class BackendKind(str, Enum):
    """Which transport is used to talk to the camera.

    Order matters for auto-selection: MASS_STORAGE is tried first because it
    needs no drivers and cannot fail in the ways the others can.
    """

    MASS_STORAGE = "massstorage"
    GPHOTO2 = "gphoto2"
    WIA = "wia"


@dataclass(frozen=True)
class CameraInfo:
    """A camera (or card) that a backend has found and can talk to."""

    model: str
    """Human-readable model, e.g. 'Canon PowerShot S30'."""

    port: str
    """Backend-specific address. gphoto2: 'usb:000,005'. Mass storage: mount
    point. WIA: the device id. Passed back verbatim on later calls."""

    kind: BackendKind
    """Which backend produced (and must service) this camera."""

    detail: str = ""
    """One-line extra context for the UI, e.g. 'Removable drive E:\\' or
    'battery OK'. May be empty."""

    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    """Backend's private bookkeeping. Opaque to the GUI. Never displayed."""

    @property
    def label(self) -> str:
        """The string the GUI shows in the device list."""
        return f"{self.model} ({self.port})" if self.port else self.model


@dataclass(frozen=True)
class CameraFile:
    """One file that exists on the camera, as reported by a listing.

    ``size`` is authoritative for verification, so backends must report the
    exact byte count whenever the transport exposes it. Use ``-1`` (not 0) when
    the size is genuinely unknown: 0 is a legitimate size for a corrupt file and
    must not be confused with "unreported".
    """

    folder: str
    """Folder on the device, POSIX-style, e.g. '/DCIM/118CANON'."""

    name: str
    """Base name including extension, e.g. 'IMG_1870.JPG'."""

    size: int = -1
    """Exact size in bytes, or -1 if the backend cannot report it."""

    mtime: Optional[float] = None
    """POSIX timestamp of the file on the device, if known."""

    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    """Backend's private handle (WIA item, absolute source path, ...)."""

    @property
    def path(self) -> str:
        """Full device path, e.g. '/DCIM/118CANON/IMG_1870.JPG'.

        Used as the stable identity of a file across download and delete, so it
        must be reproducible between two listings of the same card.
        """
        return f"{self.folder.rstrip('/')}/{self.name}"

    @property
    def size_known(self) -> bool:
        return self.size >= 0


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VerifyResult:
    """Verdict of the integrity check performed on a downloaded file."""

    ok: bool
    reason: str = ""
    """Empty when ok. Otherwise a short human-readable cause, e.g.
    'truncated: 512000 of 871424 bytes' or 'JPEG end marker missing'."""

    checked_decode: bool = False
    """True if the image was fully decoded (Pillow present), False if only the
    structural check ran. Surfaced in the UI so the user knows how strong the
    guarantee is."""


@dataclass(frozen=True)
class DownloadOutcome:
    """What happened to a single file during download."""

    file: CameraFile
    dest_path: Optional[str]
    """Absolute path written on disk, or None if the download failed."""

    ok: bool
    """True only if the bytes were written AND verification passed. This is the
    single flag the delete gate reads — never widen its meaning."""

    verify: Optional[VerifyResult] = None
    error: str = ""
    skipped: bool = False
    """True when an identical, already-verified copy was found at the
    destination and the transfer was skipped (resume support)."""


@dataclass(frozen=True)
class DeleteOutcome:
    """What happened to a single file during deletion from the camera."""

    file: CameraFile
    ok: bool
    error: str = ""


# --------------------------------------------------------------------------- #
# Progress and cancellation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Progress:
    """A progress tick pushed from the worker thread to the GUI.

    Instances are put on a queue and drained by the Tk main loop; they must stay
    immutable and cheap to build, because a large transfer emits thousands.
    """

    phase: str
    """One of: 'detect', 'list', 'download', 'verify', 'delete', 'deps'."""

    index: int = 0
    """0-based index of the item being processed."""

    total: int = 0
    """Total items in this phase, 0 if unknown."""

    name: str = ""
    """Item being processed, e.g. 'IMG_1870.JPG'."""

    bytes_done: int = 0
    bytes_total: int = 0
    message: str = ""
    """Free-form line appended to the log pane. May be empty."""

    @property
    def fraction(self) -> float:
        """Overall completion in [0.0, 1.0]; 0.0 when the total is unknown."""
        if self.total <= 0:
            return 0.0
        return min(1.0, max(0.0, self.index / self.total))


ProgressCallback = Callable[[Progress], None]
"""Called from the worker thread. Implementations must be non-blocking and
thread-safe — the GUI's implementation only enqueues."""


class CancelToken:
    """Cooperative cancellation shared between the GUI and the worker thread.

    Backends must check :meth:`cancelled` between files (and, for long files,
    between chunks) and raise :class:`TransferAborted` promptly when set. A
    cancelled download leaves already-completed files in place — it never rolls
    back — and must never leave a partially written file at its final name.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def reset(self) -> None:
        self._event.clear()

    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise TransferAborted("Operation cancelled by the user.")

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self._event.is_set()
