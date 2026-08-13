"""The contract every backend implements.

A *backend* is one way of talking to a camera: a mounted card, the gphoto2 CLI,
or the Windows Image Acquisition service. The GUI never learns which one it got;
it only ever sees this interface, which is what lets the same button work for a
20-year-old Canon on macOS and a CompactFlash reader on Windows.

Implementing a new backend means subclassing :class:`CameraBackend`, filling in
the five abstract methods, and adding it to ``registry.py``. Nothing else in the
project should need to change.

Rules every implementation must honour
--------------------------------------
1. **Never raise a raw exception.** Wrap subprocess/COM/OS failures in a
   :class:`~retrocam.model.CameraError` whose message a non-technical user can
   act on.
2. **Never write to the camera except in :meth:`delete`.** Listing and
   downloading are strictly read-only. A card from 2003 may be one bad write
   away from unreadable.
3. **Never partially overwrite a destination file.** Download to a temporary
   name in the destination directory and rename into place only once the bytes
   are complete, so an interrupted run can never be mistaken for a good file.
4. **Check the cancel token** between files and raise
   :class:`~retrocam.model.TransferAborted` promptly.
5. **Report exact byte sizes** in :meth:`list_files` whenever the transport
   exposes them; verification depends on it.
"""

from __future__ import annotations

import abc
import os
from typing import List, Optional, Sequence, Tuple

from ..model import (
    BackendKind,
    CameraFile,
    CameraInfo,
    CancelToken,
    DeleteOutcome,
    DownloadOutcome,
    Progress,
    ProgressCallback,
)

__all__ = ["CameraBackend", "Availability", "noop_progress"]


#: ``(available, hint)``. When ``available`` is False, ``hint`` explains what is
#: missing in one sentence the GUI can show verbatim, e.g.
#: ``"gphoto2 is not installed — press Install to add it via Homebrew."``
Availability = Tuple[bool, str]


def noop_progress(_: Progress) -> None:
    """Default progress sink, so callers may omit the callback entirely."""


class CameraBackend(abc.ABC):
    """One transport for reaching photos on a device.

    Instances are cheap and stateless-ish: the GUI may construct a backend,
    probe it, and throw it away. Any handle that must survive between calls
    belongs in ``CameraInfo.raw`` / ``CameraFile.raw``, not on ``self``, because
    the device may be unplugged and re-plugged between two GUI actions.
    """

    #: Which transport this is. Must match the class's entry in the registry.
    kind: BackendKind

    #: Short name shown in the UI, e.g. "Memory card / reader".
    display_name: str = "Camera"

    #: One-line explanation of when this backend is the right choice.
    description: str = ""

    # -- capability probing ------------------------------------------------ #

    @classmethod
    @abc.abstractmethod
    def is_available(cls) -> Availability:
        """Report whether this backend can run on this machine *right now*.

        Must be fast (no device enumeration, no network) and must never raise:
        it is called for every backend at startup to build the environment
        panel. Return ``(False, hint)`` rather than raising when a tool is
        missing — a missing dependency is a normal, fixable state, not an error.
        """

    @classmethod
    def install_hint(cls) -> str:
        """Command or step that would make this backend available.

        Shown next to the Install button. Empty when nothing can be automated.
        """
        return ""

    # -- discovery --------------------------------------------------------- #

    @abc.abstractmethod
    def detect(self, progress: ProgressCallback = noop_progress) -> List[CameraInfo]:
        """Return every camera this backend can currently reach.

        An empty list is a valid, non-exceptional answer meaning "nothing
        plugged in". Only raise :class:`~retrocam.model.CameraError` when the
        probe itself failed (tool crashed, permission denied) — a state the user
        must fix, as opposed to simply having no camera attached.
        """

    @abc.abstractmethod
    def list_files(
        self,
        camera: CameraInfo,
        progress: ProgressCallback = noop_progress,
        cancel: Optional[CancelToken] = None,
    ) -> List[CameraFile]:
        """List every downloadable file on the device, recursively.

        Must descend into sub-folders: on a Canon card the images live in
        ``/DCIM/118CANON``, ``/DCIM/119CANON`` and so on, and a non-recursive
        listing silently loses most of the archive.

        Returns files in a stable order (folder, then name) so that the
        progress bar advances predictably and re-runs are reproducible.
        """

    # -- the two operations that matter ------------------------------------ #

    @abc.abstractmethod
    def download(
        self,
        camera: CameraInfo,
        files: Sequence[CameraFile],
        dest_dir: str,
        progress: ProgressCallback = noop_progress,
        cancel: Optional[CancelToken] = None,
        skip_existing: bool = True,
    ) -> List[DownloadOutcome]:
        """Copy ``files`` from the device into ``dest_dir``.

        Returns one :class:`~retrocam.model.DownloadOutcome` per input file, in
        the same order, whether it succeeded or not — the caller counts on the
        1:1 mapping to report "78 of 82 recovered" accurately.

        The backend writes the bytes and fills in ``dest_path``; it must leave
        ``ok`` reflecting the *transfer* only. The transfer engine runs
        verification afterwards and produces the final outcome. Implementations
        must therefore never set ``ok=True`` on the strength of a zero-length or
        short read.

        When ``skip_existing`` is set and the destination already holds a file
        of exactly the expected size, the backend may return an outcome with
        ``skipped=True`` instead of re-reading it over a slow USB 1.1 link.
        """

    @abc.abstractmethod
    def delete(
        self,
        camera: CameraInfo,
        files: Sequence[CameraFile],
        progress: ProgressCallback = noop_progress,
        cancel: Optional[CancelToken] = None,
    ) -> List[DeleteOutcome]:
        """Erase ``files`` from the device. Irreversible.

        Callers must only ever pass files that were downloaded *and* verified;
        enforcing that is the transfer engine's job, but a backend must not
        second-guess it by deleting anything outside ``files`` — in particular
        it must never format the card or delete a whole folder, even when every
        file in that folder was requested. Deleting file-by-file keeps an
        unexpected listing mismatch from escalating into total loss.
        """

    def supports_delete(self) -> bool:
        """Whether :meth:`delete` actually works here.

        Some transports are read-only (a write-protected card, a WIA device
        that refuses erase). Backends override this to gray out the button
        rather than failing at the last moment.
        """
        return True

    # -- shared helpers ---------------------------------------------------- #

    @staticmethod
    def safe_dest_path(dest_dir: str, camera_file: CameraFile) -> str:
        """Absolute destination path for a file, collision-free and contained.

        Two Canon folders can legitimately hold the same base name (``IMG_0001``
        appears again once the counter rolls over), so when the flat name is
        already taken by a *different* device path we disambiguate with the
        device folder rather than silently overwriting.

        Also strips any directory component from the device-supplied name: a
        malformed listing must not be able to write outside ``dest_dir``.
        """
        safe_name = os.path.basename(camera_file.name.replace("\\", "/")).strip()
        if not safe_name or safe_name in (".", ".."):
            safe_name = "unnamed.bin"

        candidate = os.path.join(dest_dir, safe_name)
        if not os.path.exists(candidate):
            return candidate

        # Prefix with the device folder, e.g. '118CANON_IMG_0001.JPG'.
        folder_tag = camera_file.folder.rstrip("/").split("/")[-1] or "DCIM"
        candidate = os.path.join(dest_dir, f"{folder_tag}_{safe_name}")
        if not os.path.exists(candidate):
            return candidate

        stem, ext = os.path.splitext(safe_name)
        for n in range(2, 1000):
            candidate = os.path.join(dest_dir, f"{stem}_{n}{ext}")
            if not os.path.exists(candidate):
                return candidate
        raise RuntimeError(f"Cannot find a free destination name for {safe_name!r}")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} kind={self.kind.value}>"
