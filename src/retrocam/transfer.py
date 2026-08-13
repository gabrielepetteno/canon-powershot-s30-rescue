"""Download orchestration and the delete gate.

This module sits between the GUI and the backends. It owns two things nobody
else is allowed to own:

1. **Verification of what actually landed on disk.** A backend reports whether
   the *transfer* worked; only this module decides whether the *file* is good,
   by re-reading every destination path through :func:`retrocam.verify.verify_download`.
2. **The delete gate.** :meth:`TransferEngine.delete_verified` is the single
   place in the program that may call ``backend.delete()``.

The safety property the whole project rests on, stated once so a reviewer can
check it in one pass:

    A :class:`~retrocam.model.CameraFile` reaches ``backend.delete()`` only if
    it appears in :attr:`TransferReport.deletable`, and a file appears there
    only if its outcome carries a destination path **and** a
    :class:`~retrocam.model.VerifyResult` with ``ok=True`` produced during this
    run, **and** that destination file is re-``stat``-ed immediately before the
    deletion call.

Every branch that could widen that set raises instead of widening. A skipped
file (already present at the destination from an earlier run) is no exception:
it is re-verified on disk like everything else, because "a file with the right
name already exists" is not evidence that the photo survived.

Translation keys used here (all have English fallbacks baked in, so a missing
key degrades to readable English rather than to a raw key):

    transfer.err.*        — user-facing failure messages
    transfer.msg.*        — log/progress lines
    transfer.summary.*    — :meth:`TransferReport.summary_lines` output
"""

from __future__ import annotations

import dataclasses
import os
import shutil
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .backends.base import CameraBackend, noop_progress
from .model import (
    CameraError,
    CameraFile,
    CameraInfo,
    CancelToken,
    DeleteOutcome,
    DownloadOutcome,
    Progress,
    ProgressCallback,
    TransferAborted,
    VerifyResult,
)
from .verify import verify_download

__all__ = ["TransferReport", "TransferEngine"]


# --------------------------------------------------------------------------- #
# Localisation helper
# --------------------------------------------------------------------------- #

# i18n is imported defensively: this module must remain importable (and the
# rescue must remain possible) even if the translation table is broken or being
# edited. A photo rescue tool that refuses to start because a string is missing
# would be a bad trade.
try:  # pragma: no cover - trivial import guard
    from .i18n import t as _translate  # type: ignore[attr-defined]
except Exception:  # pragma: no cover - i18n absent or broken
    _translate = None  # type: ignore[assignment]


def _msg(key: str, fallback: str, **kw: object) -> str:
    """Translate ``key``, falling back to the English ``fallback`` template.

    Returns the fallback when i18n is unavailable, when the key is unknown
    (``t()`` conventionally echoes the key back), or when interpolation fails.
    """
    if _translate is not None:
        try:
            text = _translate(key, **kw)  # type: ignore[misc]
        except Exception:
            text = ""
        if text and text != key:
            return text
    try:
        return fallback.format(**kw)
    except Exception:  # pragma: no cover - defensive only
        return fallback


def _as_progress(progress: Optional[ProgressCallback]) -> ProgressCallback:
    """Normalise a progress callback, accepting ``None`` as "report nothing".

    The keyword defaults to :func:`noop_progress`, but an explicit ``None`` is
    the idiomatic way to say "no callback" and it bypasses the default. Without
    this, ``None`` surfaced as ``TypeError: 'NoneType' object is not callable``
    deep inside a backend, which the engine then wrapped into a message blaming
    the *camera* for what is really a caller mistake — the single most
    misleading way this code could fail.
    """
    return progress if callable(progress) else noop_progress


def _real(path: str) -> str:
    """Comparison key for a directory: absolute, symlinks resolved, case-folded.

    ``realpath`` matters more than it looks: macOS puts firmlinks inside
    ``/Volumes`` and a card can be reachable through more than one path, so a
    plain string comparison would miss "this is the same place".
    """
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _overlaps(first: str, second: str) -> bool:
    """True when two directories are the same, or one contains the other.

    Containment is tested in both directions on purpose: writing *into* the card
    and writing a folder that *holds* the card are both ways of putting the
    rescue on the media it is rescuing.
    """
    if not first or not second:
        return False
    try:
        a, b = _real(first), _real(second)
    except OSError:  # pragma: no cover - realpath on a vanished mount
        return False
    if a == b:
        return True
    return a.startswith(b.rstrip(os.sep) + os.sep) or b.startswith(
        a.rstrip(os.sep) + os.sep
    )


def _human_bytes(n: int) -> str:
    """Format a byte count for humans, e.g. ``'214.3 MB'``."""
    if n < 0:
        return "?"
    step = 1024.0
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < step or unit == "TB":
            if unit == "B":
                return "%d B" % int(value)
            return "%.1f %s" % (value, unit)
        value /= step
    return "%.1f TB" % value  # pragma: no cover - unreachable


#: How many per-file failure lines :meth:`TransferReport.summary_lines` prints
#: before collapsing the rest into a counter. A card with 500 unreadable files
#: must not produce a 500-line dialog.
_MAX_DETAIL_LINES = 20

#: Safety margin applied on top of the sum of known file sizes when checking
#: free space. Covers filesystem block rounding and the temporary file each
#: backend writes next to its final name.
_SPACE_MARGIN = 1.10


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TransferReport:
    """The complete, immutable record of one download run.

    This object is what the GUI shows and what the delete gate reads. It is
    frozen on purpose: once a run is over, nobody gets to promote a failed file
    to "verified" by assignment.
    """

    outcomes: List[DownloadOutcome]
    """One entry per requested file, in the order they were requested."""

    dest_dir: str
    """Absolute directory the files were written into."""

    aborted: bool = False
    """True when the user cancelled mid-run and this report is partial.

    Additive field with a default, so ``TransferReport(outcomes, dest_dir)``
    keeps working. It exists because "45 of 82, cancelled" and "45 of 82, 37
    unreadable" are very different messages for the user.
    """

    def __post_init__(self) -> None:
        # Copy the sequence we were handed: a frozen dataclass protects the
        # attribute binding, not the list behind it, and a caller that keeps
        # mutating its own list must not be able to edit a finished report.
        object.__setattr__(self, "outcomes", list(self.outcomes))

    # -- counters ---------------------------------------------------------- #

    @property
    def ok_count(self) -> int:
        """Files written *and* verified. ``ok_count + failed_count == len(outcomes)``."""
        return sum(1 for o in self.outcomes if o.ok)

    @property
    def failed_count(self) -> int:
        """Files that are not usable, for any reason (transfer or verification)."""
        return sum(1 for o in self.outcomes if not o.ok)

    @property
    def skipped_count(self) -> int:
        """Files the backend did not re-transfer because a copy already existed.

        Overlaps :attr:`ok_count`: a skipped file that passed re-verification is
        both skipped and ok; one that failed re-verification is both skipped and
        failed.
        """
        return sum(1 for o in self.outcomes if o.skipped)

    # -- the gate's source of truth ---------------------------------------- #

    @property
    def deletable(self) -> List[CameraFile]:
        """The only files that may ever be passed to ``backend.delete()``.

        Membership is re-derived from the *evidence* attached to each outcome
        rather than trusted from ``outcome.ok`` alone. All three conditions must
        hold:

        * ``ok`` is True — the flag the model documents as the delete gate's
          input;
        * ``dest_path`` is set — bytes have a home on disk;
        * a :class:`~retrocam.model.VerifyResult` with ``ok=True`` is attached —
          the file was read back and checked **during this run**.

        The third condition is what makes skipped files safe: a pre-existing
        destination file carries no VerifyResult of its own, so it can only get
        here after :meth:`TransferEngine.download` re-verified it. Deriving the
        set this way also means that a hand-built or future-refactored
        ``DownloadOutcome`` that sets ``ok=True`` without verification evidence
        silently drops out of the deletion set instead of quietly joining it.
        """
        return [o.file for o in self.verified_outcomes()]

    def verified_outcomes(self) -> List[DownloadOutcome]:
        """The outcomes behind :attr:`deletable`, evidence still attached.

        Same membership test, one definition, so the two can never drift apart.
        The gate uses *this* one because a :class:`~retrocam.model.CameraFile` is
        the identity of a path, not of a photograph, and the two are not always
        the same thing: a WIA driver that flattens the camera's tree reports
        ``/IMG_0001.JPG`` for two different pictures taken a year apart. Selecting
        by path would then let the failed one ride into the deletion set on the
        verified one's name. Carrying the outcome keeps every later step attached
        to the evidence that justified it.
        """
        out: List[DownloadOutcome] = []
        for o in self.outcomes:
            if not o.ok:
                continue
            if not o.dest_path:
                continue
            if o.verify is None or not o.verify.ok:
                continue
            out.append(o)
        return out

    @property
    def all_verified(self) -> bool:
        """True iff at least one file was requested and every one of them is ok.

        An empty run is *not* "all verified": nothing was proven, so no delete
        button may light up because of it.
        """
        return bool(self.outcomes) and all(o.ok for o in self.outcomes)

    # -- presentation ------------------------------------------------------ #

    def summary_lines(self) -> List[str]:
        """Human-readable recap for the log pane and the final dialog."""
        total = len(self.outcomes)
        lines: List[str] = [
            _msg("transfer.summary.dest", "Destination: {dest}", dest=self.dest_dir),
            _msg(
                "transfer.summary.recovered",
                "Recovered and verified: {ok} of {total}",
                ok=self.ok_count,
                total=total,
            ),
        ]

        if self.aborted:
            lines.append(
                _msg(
                    "transfer.summary.aborted",
                    "Run interrupted by the user - the remaining files were not transferred.",
                )
            )

        if self.skipped_count:
            lines.append(
                _msg(
                    "transfer.summary.skipped",
                    "Already present and re-checked on disk: {n}",
                    n=self.skipped_count,
                )
            )

        if self.ok_count:
            # Tell the user how strong the guarantee is: a structural check
            # catches truncation, a full decode also catches bit rot.
            deep = sum(
                1
                for o in self.outcomes
                if o.ok and o.verify is not None and o.verify.checked_decode
            )
            lines.append(
                _msg(
                    "transfer.summary.deep",
                    "Fully decoded during the check: {deep} of {ok} (the rest passed the structural check)",
                    deep=deep,
                    ok=self.ok_count,
                )
            )

        if self.failed_count:
            lines.append(
                _msg(
                    "transfer.summary.failed", "Not recovered: {n}", n=self.failed_count
                )
            )
            shown = 0
            for o in self.outcomes:
                if o.ok:
                    continue
                if shown >= _MAX_DETAIL_LINES:
                    lines.append(
                        _msg(
                            "transfer.summary.more",
                            "  ... and {n} more",
                            n=self.failed_count - shown,
                        )
                    )
                    break
                reason = o.error or (o.verify.reason if o.verify is not None else "")
                if not reason:
                    reason = _msg("transfer.summary.unknown_reason", "unknown reason")
                lines.append("  %s - %s" % (o.file.name, reason))
                shown += 1

        lines.append(
            _msg(
                "transfer.summary.deletable",
                "Safe to erase from the camera: {n}",
                n=len(self.deletable),
            )
        )
        return lines


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #


class TransferEngine:
    """Drives one backend against one camera for the duration of a session.

    The engine is deliberately thin: backends move bytes, :mod:`retrocam.verify`
    judges files, and this class only sequences them and refuses to let an
    unverified file anywhere near a delete call.
    """

    def __init__(self, backend: CameraBackend, camera: CameraInfo) -> None:
        if backend is None or camera is None:  # pragma: no cover - programming error
            raise ValueError("TransferEngine requires both a backend and a camera.")

        # A CameraInfo produced by one backend addresses a device the others
        # cannot reach: its `port` means a mount point to one and a USB address
        # to another. Pairing them wrongly would, in the worst case, aim a
        # delete at the wrong device, so we refuse the pairing up front.
        backend_kind = getattr(backend, "kind", None)
        if backend_kind is not None and camera.kind != backend_kind:
            raise ValueError(
                "Camera %r was found by the %s backend and cannot be driven by %s."
                % (
                    camera.label,
                    getattr(camera.kind, "value", camera.kind),
                    getattr(backend_kind, "value", backend_kind),
                )
            )

        self.backend = backend
        self.camera = camera

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<TransferEngine %s via %s>" % (
            self.camera.label,
            type(self.backend).__name__,
        )

    # -- listing ----------------------------------------------------------- #

    def list_files(
        self,
        progress: ProgressCallback = noop_progress,
        cancel: Optional[CancelToken] = None,
    ) -> List[CameraFile]:
        """List everything on the device. Strictly read-only.

        Raises :class:`~retrocam.model.CameraError` if the listing itself fails.
        An empty list is a normal answer (empty card), not an error.
        """
        progress = _as_progress(progress)
        if cancel is not None:
            cancel.raise_if_cancelled()

        try:
            files = list(self.backend.list_files(self.camera, progress, cancel))
        except CameraError:
            raise  # already user-facing (includes TransferAborted)
        except Exception as exc:  # a backend broke rule 1; do not leak a traceback
            raise CameraError(
                _msg(
                    "transfer.err.list_failed",
                    "Could not read the file list from {camera}: {err}",
                    camera=self.camera.label,
                    err=exc,
                )
            ) from exc

        known = sum(f.size for f in files if f.size_known)
        progress(
            Progress(
                phase="list",
                index=len(files),
                total=len(files),
                message=_msg(
                    "transfer.msg.listed",
                    "{n} file(s) found on the camera ({size}).",
                    n=len(files),
                    size=_human_bytes(known) if known else "?",
                ),
            )
        )
        return files

    # -- download ---------------------------------------------------------- #

    def download(
        self,
        files: Sequence[CameraFile],
        dest_dir: str,
        progress: ProgressCallback = noop_progress,
        cancel: Optional[CancelToken] = None,
        skip_existing: bool = True,
    ) -> TransferReport:
        """Copy ``files`` into ``dest_dir`` and verify every byte that landed.

        Sequence: prepare and probe the destination, refuse early if the disk is
        too small, hand the bytes to the backend, then re-read **every**
        destination file (skipped ones included) and rebuild each outcome with
        ``ok = transfer ok AND verification ok``.

        Cancellation returns a *partial* report rather than raising: the files
        that did make it are the whole point of the exercise and must not be
        thrown away with the exception. The caller inspects
        :attr:`TransferReport.aborted` to phrase the message.

        Raises :class:`~retrocam.model.CameraError` when the destination is
        unusable, when there is not enough free space, or when the backend
        returns outcomes that do not match the requested files.
        """
        progress = _as_progress(progress)
        requested = list(files)
        dest_dir = os.path.abspath(os.path.expanduser(dest_dir))

        if not requested:
            # Nothing requested is not an error, but it is also not a success:
            # `all_verified` stays False, so no delete can be justified by it.
            return TransferReport(outcomes=[], dest_dir=dest_dir)

        if cancel is not None:
            cancel.raise_if_cancelled()

        # Must come before _prepare_dest: that method creates the folder and
        # writes a probe file into it, which on a card would already be a write
        # to the device during an operation the contract calls read-only.
        self._reject_dest_on_device(dest_dir)
        self._prepare_dest(dest_dir)
        self._check_free_space(requested, dest_dir, skip_existing, progress)

        # Snapshot the destination *before* the transfer. Used only by the
        # cancellation path, to tell files this run created from files that were
        # already lying there (see _recover_after_abort).
        pre_existing = self._snapshot(dest_dir)

        try:
            raw = list(
                self.backend.download(
                    self.camera,
                    requested,
                    dest_dir,
                    progress,
                    cancel,
                    skip_existing,
                )
            )
        except TransferAborted:
            progress(
                Progress(
                    phase="download",
                    message=_msg(
                        "transfer.msg.cancelled",
                        "Cancelled - checking which files completed before stopping.",
                    ),
                )
            )
            outcomes = self._recover_after_abort(
                requested, dest_dir, pre_existing, progress
            )
            return TransferReport(outcomes=outcomes, dest_dir=dest_dir, aborted=True)
        except CameraError:
            raise
        except Exception as exc:  # a backend broke rule 1
            raise CameraError(
                _msg(
                    "transfer.err.download_failed",
                    "The transfer stopped unexpectedly: {err}. Nothing was erased from the camera.",
                    err=exc,
                )
            ) from exc

        aligned = self._align_outcomes(requested, raw)
        verified, aborted = self._verify_all(aligned, progress, cancel)
        return TransferReport(outcomes=verified, dest_dir=dest_dir, aborted=aborted)

    # -- delete ------------------------------------------------------------ #

    def delete_verified(
        self,
        report: TransferReport,
        progress: ProgressCallback = noop_progress,
        cancel: Optional[CancelToken] = None,
    ) -> List[DeleteOutcome]:
        """Erase from the camera **only** the files this report proved good.

        This is the gate. Everything it does is a narrowing step; there is no
        code path here that adds a file to the deletion set.

        On ``report.failed_count > 0``: deletion still proceeds, and this is
        deliberate but narrow. The gate is applied *per file*, not per run, and
        the list handed to the backend is exactly :attr:`TransferReport.deletable`
        — the verified subset — so failures cannot smuggle themselves in. What
        the engine must never do is delete anything derived from the report as a
        whole (e.g. "all files listed on the camera") when some of them failed;
        that path does not exist, and the subset assertion below exists to make
        sure a future refactor cannot create it. A warning line is emitted so
        the log records that a partial erase happened.

        A cancelled deletion propagates :class:`~retrocam.model.TransferAborted`:
        unlike a download, a half-finished erase cannot be summarised from disk,
        and the honest answer is "re-list the camera to see what is left".

        Raises:
            ValueError: if the report has nothing verified to delete.
            CameraError: if the backend cannot delete, or reports having deleted
                something that was not in the verified set.
        """
        progress = _as_progress(progress)
        # A wrong object here would be catastrophic rather than merely wrong:
        # anything exposing a permissive `deletable` would sail straight through
        # the gate. Only the real report type is accepted.
        if not isinstance(report, TransferReport):
            raise TypeError(
                "delete_verified() requires a TransferReport, got %r"
                % type(report).__name__
            )

        if not self.backend.supports_delete():
            raise CameraError(
                _msg(
                    "transfer.err.delete_unsupported",
                    "This connection cannot erase files from {camera}. "
                    "The card may be write-protected, or the driver may be read-only.",
                    camera=self.camera.label,
                )
            )

        # The allow-list, captured once from the report's own definition of
        # "verified". Every later step may only remove entries from it.
        #
        # It is a list of *outcomes*, and it is walked by object identity rather
        # than matched by device path, because a path is not a unique key. Two
        # entries in one listing can wear the same ``/DCIM/100X/IMG_0001.JPG``
        # (a WIA driver that flattens the camera's folders does this routinely),
        # and a path-keyed allow-list would then let the entry that FAILED
        # verification through on the strength of its verified namesake.
        verified_outcomes = report.verified_outcomes()
        verified = [o.file for o in verified_outcomes]
        if not verified:
            raise ValueError(
                "Refusing to delete: no file in this report was downloaded and verified."
            )

        verified_ids = frozenset(id(o.file) for o in verified_outcomes)

        if cancel is not None:
            cancel.raise_if_cancelled()

        if report.failed_count:
            progress(
                Progress(
                    phase="delete",
                    message=_msg(
                        "transfer.msg.partial_delete",
                        "{bad} file(s) were not recovered and will be left on the camera.",
                        bad=report.failed_count,
                    ),
                )
            )

        # Last-moment re-check on disk. Between verification and this call the
        # user may have moved, emptied or unmounted the destination folder; the
        # camera copy is worthless the moment the local one stops existing.
        to_delete: List[CameraFile] = []
        refused: List[DeleteOutcome] = []
        for outcome in verified_outcomes:
            intact, why = self._dest_still_intact(outcome)
            if intact:
                to_delete.append(outcome.file)
            else:
                refused.append(DeleteOutcome(file=outcome.file, ok=False, error=why))
                progress(
                    Progress(
                        phase="delete",
                        name=outcome.file.name,
                        message=_msg(
                            "transfer.msg.delete_refused",
                            "Keeping {name} on the camera: {why}",
                            name=outcome.file.name,
                            why=why,
                        ),
                    )
                )

        # ------------------------------------------------------------------ #
        # THE SUBSET ASSERTION
        # ------------------------------------------------------------------ #
        # `to_delete` is built above out of `verified_outcomes` themselves, so
        # this check cannot fail today. It is written out anyway, as executable
        # code rather than an `assert` statement (which `python -O` strips),
        # because it is the invariant the entire program is built on: nothing may
        # be erased that was not byte-verified on disk. If a later refactor
        # changes how `to_delete` is assembled, this must crash loudly instead of
        # silently widening the deletion set. Identity, not equality: two
        # distinct photographs can compare equal when a driver reports the same
        # name, size and timestamp for both.
        stray = [f.path for f in to_delete if id(f) not in verified_ids]
        if stray:
            raise RuntimeError(
                "SAFETY VIOLATION: delete_verified() was about to erase %d unverified "
                "file(s) from the camera: %s. Nothing was deleted. This is a bug in "
                "RetroCam Rescue - please report it."
                % (len(stray), ", ".join(stray[:5]))
            )
        if len(to_delete) > len(verified):  # pragma: no cover - duplicate smuggling
            raise RuntimeError(
                "SAFETY VIOLATION: the deletion set (%d) is larger than the verified "
                "set (%d). Nothing was deleted." % (len(to_delete), len(verified))
            )

        if not to_delete:
            # Everything that was verified has since gone missing locally.
            # Refusing entirely is the only safe answer.
            progress(
                Progress(
                    phase="delete",
                    message=_msg(
                        "transfer.msg.delete_all_refused",
                        "Nothing was erased: the verified copies are no longer on disk.",
                    ),
                )
            )
            return refused

        progress(
            Progress(
                phase="delete",
                index=0,
                total=len(to_delete),
                message=_msg(
                    "transfer.msg.deleting",
                    "Erasing {n} verified file(s) from {camera}.",
                    n=len(to_delete),
                    camera=self.camera.label,
                ),
            )
        )

        try:
            results = list(
                self.backend.delete(self.camera, to_delete, progress, cancel)
            )
        except CameraError:
            raise  # includes TransferAborted, deliberately propagated
        except Exception as exc:  # a backend broke rule 1
            raise CameraError(
                _msg(
                    "transfer.err.delete_failed",
                    "Erasing stopped unexpectedly: {err}. "
                    "Re-scan the camera to see which files are still there.",
                    err=exc,
                )
            ) from exc

        # Post-condition: the backend must not have touched anything outside the
        # set we handed it. We cannot undo a deletion, but we can make sure the
        # user is told loudly instead of seeing a green tick.
        #
        # Identity first, because that is what every backend in this repo hands
        # back — the very objects it was given. Equality is accepted as a
        # fallback so that a backend which rebuilds an identical CameraFile
        # raises no false alarm; the alarm below tells the user to stop using the
        # card, and it must mean something when it fires.
        handed_ids = frozenset(id(f) for f in to_delete)
        handed_values = frozenset(to_delete)
        outside = [
            r.file.path
            for r in results
            if id(r.file) not in handed_ids and r.file not in handed_values
        ]
        if outside:
            raise CameraError(
                _msg(
                    "transfer.err.delete_outside",
                    "The {backend} backend reported erasing {n} file(s) that were never "
                    "verified: {paths}. Stop using this card and copy anything left on it.",
                    backend=type(self.backend).__name__,
                    n=len(outside),
                    paths=", ".join(outside[:5]),
                )
            )

        return list(results) + refused

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _device_roots(self) -> List[str]:
        """Filesystem paths that belong to the camera itself, if it has any.

        Only a mounted card exposes one. gphoto2 addresses a camera as
        ``usb:001,004`` and WIA as a driver GUID; neither is a directory, so for
        those transports this returns ``[]`` and the containment check below is
        a no-op rather than a guess.
        """
        roots: List[str] = []
        candidates = [
            str(self.camera.raw.get("mount") or ""),
            str(self.camera.raw.get("dcim") or ""),
            self.camera.port or "",
        ]
        for candidate in candidates:
            # Absolute *and* a real directory: a port string such as
            # 'usb:001,004' must never be mistaken for a path.
            if not candidate or not os.path.isabs(candidate):
                continue
            try:
                if os.path.isdir(candidate):
                    roots.append(candidate)
            except OSError:
                continue
        return roots

    def _reject_dest_on_device(self, dest_dir: str) -> None:
        """Refuse to write the rescue onto the card it is rescuing.

        A user browsing for a destination can reach the card in two clicks, and
        picking it is unrecoverable rather than merely wrong. With the
        destination inside the card's own DCIM folder, the backend's resume
        check finds each source file already sitting at its destination path —
        same name, same size, same timestamp — and reports it as skipped;
        verification then re-reads *the original* and passes it; the delete gate
        re-stats *the original* and finds it intact; and the erase removes every
        photo while no copy was ever made anywhere else.

        Every one of those steps is individually correct. The only place to
        break the chain is here, before a single byte is written, which is why
        this refuses rather than trying to compensate downstream.
        """
        for root in self._device_roots():
            if _overlaps(dest_dir, root):
                raise CameraError(
                    _msg(
                        "transfer.err.dest_on_camera",
                        "The destination {dest} is on the camera's own memory card "
                        "({root}). Copying the photos onto the card they came from "
                        "would leave no second copy, so nothing was transferred. "
                        "Choose a folder on this computer, such as your Downloads "
                        "folder.",
                        dest=dest_dir,
                        root=root,
                    )
                )

    def _prepare_dest(self, dest_dir: str) -> None:
        """Create the destination directory and prove we can write into it.

        ``os.access`` lies on Windows (and on network shares everywhere), so we
        actually create and remove a probe file. Discovering that the folder is
        read-only now is worth a millisecond; discovering it at file 70 of 82
        over USB 1.1 is not.
        """
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as exc:
            raise CameraError(
                _msg(
                    "transfer.err.dest_create",
                    "Cannot create the destination folder {dest}: {err}. "
                    "Choose another folder.",
                    dest=dest_dir,
                    err=exc.strerror or exc,
                )
            ) from exc

        if not os.path.isdir(dest_dir):
            raise CameraError(
                _msg(
                    "transfer.err.dest_not_dir",
                    "The destination {dest} is not a folder. Choose another folder.",
                    dest=dest_dir,
                )
            )

        probe = os.path.join(dest_dir, ".retrocam-write-test-%d.tmp" % os.getpid())
        try:
            fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except OSError as exc:
            raise CameraError(
                _msg(
                    "transfer.err.dest_readonly",
                    "Cannot write into {dest}: {err}. "
                    "Pick a folder you own, such as your Downloads folder.",
                    dest=dest_dir,
                    err=exc.strerror or exc,
                )
            ) from exc
        finally:
            try:
                os.remove(probe)
            except OSError:
                pass  # the probe is disposable; a leftover must not stop a rescue

    def _check_free_space(
        self,
        files: Sequence[CameraFile],
        dest_dir: str,
        skip_existing: bool,
        progress: ProgressCallback,
    ) -> None:
        """Refuse up front if the disk obviously cannot hold the card.

        Only files whose size the backend reported are counted; unknown sizes
        are reported to the log so the user knows the estimate is partial. This
        is an early-warning check, not an accounting system: a filesystem that
        cannot be measured never blocks the transfer.
        """
        known = [f for f in files if f.size_known]
        unknown = len(files) - len(known)
        needed = sum(f.size for f in known)

        if skip_existing:
            # Files already sitting at the destination with exactly the right
            # size will be skipped, so they do not need room. Matching by flat
            # base name mirrors what safe_dest_path tries first; getting this
            # slightly wrong only makes the estimate more conservative.
            for f in known:
                flat = os.path.join(
                    dest_dir, os.path.basename(f.name.replace("\\", "/"))
                )
                try:
                    if os.path.isfile(flat) and os.path.getsize(flat) == f.size:
                        needed -= f.size
                except OSError:
                    pass

        if needed <= 0:
            if unknown:
                progress(
                    Progress(
                        phase="download",
                        message=_msg(
                            "transfer.msg.space_unknown",
                            "The camera did not report file sizes; free space could not be checked.",
                        ),
                    )
                )
            return

        try:
            free = shutil.disk_usage(dest_dir).free
        except OSError:
            # Unmeasurable filesystem (some network mounts). Proceed: a failed
            # measurement is not a reason to refuse a rescue.
            progress(
                Progress(
                    phase="download",
                    message=_msg(
                        "transfer.msg.space_uncheckable",
                        "Free space on {dest} could not be measured; continuing.",
                        dest=dest_dir,
                    ),
                )
            )
            return

        required = int(needed * _SPACE_MARGIN)
        if required > free:
            raise CameraError(
                _msg(
                    "transfer.err.no_space",
                    "Not enough free space in {dest}: about {need} is required "
                    "(including a 10% margin) but only {free} is available. "
                    "Free some space or choose another folder.",
                    dest=dest_dir,
                    need=_human_bytes(required),
                    free=_human_bytes(free),
                )
            )

        progress(
            Progress(
                phase="download",
                message=_msg(
                    "transfer.msg.space_ok",
                    "{need} to copy, {free} free in {dest}.",
                    need=_human_bytes(needed),
                    free=_human_bytes(free),
                    dest=dest_dir,
                )
                + (
                    ""
                    if not unknown
                    else " "
                    + _msg(
                        "transfer.msg.space_partial",
                        "({n} file(s) of unreported size are not included.)",
                        n=unknown,
                    )
                ),
            )
        )

    @staticmethod
    def _snapshot(dest_dir: str) -> Optional[Set[str]]:
        """Names present in ``dest_dir`` before the transfer starts.

        Returns ``None`` when the directory cannot be listed. ``None`` means
        "unknown", and the cancellation path treats unknown as "credit nothing"
        — an empty set would mean the opposite ("everything here is new"), which
        is precisely the mistake that would mark a stranger's file as verified.
        """
        try:
            return set(os.listdir(dest_dir))
        except OSError:
            return None

    @staticmethod
    def _align_outcomes(
        files: Sequence[CameraFile], raw: Sequence[DownloadOutcome]
    ) -> List[DownloadOutcome]:
        """Pair each requested file with its outcome, or fail loudly.

        The backend contract promises one outcome per input file in input order.
        We re-align by device path rather than trusting the order (harmless
        forgiveness), but we refuse anything that is not a one-to-one mapping:
        if the outcomes do not describe exactly the files we asked for, we do
        not know which local file corresponds to which photo on the card, and
        that confusion is one step away from erasing the wrong picture.
        """
        slots: List[Optional[DownloadOutcome]] = [None] * len(files)
        index_by_path: Dict[str, List[int]] = {}
        for i, f in enumerate(files):
            index_by_path.setdefault(f.path, []).append(i)

        for outcome in raw:
            candidates = index_by_path.get(outcome.file.path)
            if not candidates:
                raise CameraError(
                    _msg(
                        "transfer.err.outcome_unknown",
                        "The camera driver reported a file that was not requested ({path}). "
                        "The downloaded files are safe in the destination folder and "
                        "nothing was erased from the camera.",
                        path=outcome.file.path,
                    )
                )
            slots[candidates.pop(0)] = outcome

        missing = [files[i].path for i, slot in enumerate(slots) if slot is None]
        if missing:
            raise CameraError(
                _msg(
                    "transfer.err.outcome_missing",
                    "The camera driver did not report back on {n} file(s), starting with "
                    "{path}. The downloaded files are safe in the destination folder and "
                    "nothing was erased from the camera.",
                    n=len(missing),
                    path=missing[0],
                )
            )
        return [slot for slot in slots if slot is not None]

    def _verify_all(
        self,
        outcomes: Sequence[DownloadOutcome],
        progress: ProgressCallback,
        cancel: Optional[CancelToken],
    ) -> Tuple[List[DownloadOutcome], bool]:
        """Re-read every destination file and rebuild the outcomes.

        Returns ``(outcomes, aborted)``. The final ``ok`` is always
        ``transfer ok AND verification ok`` — verification can only ever demote
        an outcome, never promote one.

        Skipped files are verified like any other: a copy that was already at
        the destination has never been checked by this program, and "the name
        and size look right" is exactly the state a half-finished 2003 transfer
        leaves behind.
        """
        total = len(outcomes)
        final: List[DownloadOutcome] = []
        aborted = False

        for i, o in enumerate(outcomes):
            if not aborted and cancel is not None and cancel.cancelled():
                aborted = True

            if aborted:
                # Unverified means not ok, which means not deletable. Cheap and
                # correct: a re-run will skip and verify these properly.
                final.append(
                    dataclasses.replace(
                        o,
                        ok=False,
                        verify=None,
                        error=o.error
                        or _msg(
                            "transfer.err.not_verified",
                            "cancelled before this file could be checked",
                        ),
                    )
                )
                continue

            progress(
                Progress(
                    phase="verify",
                    index=i,
                    total=total,
                    name=o.file.name,
                    message="",
                )
            )

            if not o.dest_path:
                # Nothing on disk to check; keep the backend's own explanation.
                final.append(
                    dataclasses.replace(
                        o,
                        ok=False,
                        verify=None,
                        error=o.error
                        or _msg("transfer.err.no_bytes", "no file was written"),
                    )
                )
                continue

            try:
                result = verify_download(
                    o.dest_path, expected_size=o.file.size, deep=True
                )
            except Exception as exc:
                # A crash inside verification must not lose the other 81 files,
                # and must never be read as "verified".
                result = VerifyResult(
                    ok=False,
                    reason=_msg(
                        "transfer.err.verify_crashed",
                        "the integrity check could not run: {err}",
                        err=exc,
                    ),
                )

            # A skipped file has no transfer to succeed or fail: the copy on
            # disk is the evidence, and we have just re-read it. Any error the
            # backend attached still counts against it.
            transfer_ok = bool(o.ok) or (o.skipped and not o.error)
            ok = bool(transfer_ok and result.ok)

            error = o.error
            if not ok and not error:
                error = result.reason
            final.append(dataclasses.replace(o, ok=ok, verify=result, error=error))

        progress(
            Progress(
                phase="verify",
                index=total,
                total=total,
                message=_msg(
                    "transfer.msg.verified",
                    "Checked {n} file(s) on disk.",
                    n=total,
                ),
            )
        )
        return final, aborted

    def _recover_after_abort(
        self,
        files: Sequence[CameraFile],
        dest_dir: str,
        pre_existing: Optional[Set[str]],
        progress: ProgressCallback,
    ) -> List[DownloadOutcome]:
        """Rebuild outcomes from disk after the backend raised TransferAborted.

        The backend raised instead of returning, so its per-file outcomes are
        gone; the files it managed to write are not. Rather than discard that
        work, each requested file is credited only when *all* of the following
        hold:

        * its size was known before the run (an unknown size cannot be matched
          against anything, so unknown-size files are never credited);
        * a destination file exists under one of the deterministic names
          ``safe_dest_path`` produces, and that name was **not** present before
          this run started — so a same-named file from an unrelated shoot can
          never be mistaken for the photo we were fetching;
        * no earlier file in this same run has already been credited with that
          copy — two Canon folders hold the same base name once the frame
          counter rolls over, and one copy on disk is evidence for exactly one
          photograph;
        * its size matches exactly and it passes full verification.

        Everything else is reported as not transferred. Being wrong in this
        direction costs a re-run; being wrong in the other direction costs a
        photograph.
        """
        outcomes: List[DownloadOutcome] = []
        recovered = 0
        credited: Set[str] = set()

        for f in files:
            dest = self._find_recovered_dest(f, dest_dir, pre_existing, credited)
            if dest is not None:
                credited.add(os.path.normcase(os.path.abspath(dest)))
            if dest is None:
                outcomes.append(
                    DownloadOutcome(
                        file=f,
                        dest_path=None,
                        ok=False,
                        error=_msg(
                            "transfer.err.cancelled_before",
                            "cancelled before this file was transferred",
                        ),
                    )
                )
                continue

            try:
                result = verify_download(dest, expected_size=f.size, deep=True)
            except Exception as exc:
                result = VerifyResult(
                    ok=False,
                    reason=_msg(
                        "transfer.err.verify_crashed",
                        "the integrity check could not run: {err}",
                        err=exc,
                    ),
                )
            if result.ok:
                recovered += 1
            outcomes.append(
                DownloadOutcome(
                    file=f,
                    dest_path=dest,
                    ok=result.ok,
                    verify=result,
                    error="" if result.ok else result.reason,
                )
            )

        progress(
            Progress(
                phase="verify",
                index=len(files),
                total=len(files),
                message=_msg(
                    "transfer.msg.recovered_after_cancel",
                    "{n} file(s) completed and verified before the run was stopped.",
                    n=recovered,
                ),
            )
        )
        return outcomes

    @staticmethod
    def _find_recovered_dest(
        camera_file: CameraFile,
        dest_dir: str,
        pre_existing: Optional[Set[str]],
        credited: Optional[Set[str]] = None,
    ) -> Optional[str]:
        """Locate a file this run wrote for ``camera_file``, or None.

        Mirrors the first two names ``CameraBackend.safe_dest_path`` would hand
        out. Later fallbacks (``name_2.jpg``) are ambiguous across different
        source files, so they are deliberately not searched: an unrecognised
        file stays uncredited rather than being credited to the wrong photo.

        ``credited`` holds the copies already awarded to earlier files in this
        same run and they are never awarded twice. Without that, two photographs
        sharing a base name and a byte count — ``118CANON/IMG_0001.JPG`` and
        ``119CANON/IMG_0001.JPG`` after the counter rolls over — would both be
        credited with the single copy the first one wrote, and the second would
        be reported as rescued, verified, and safe to erase from the card while
        its bytes existed nowhere.
        """
        if pre_existing is None:
            return None  # cannot tell new files from old ones: credit nothing
        if not camera_file.size_known:
            return None  # no size to match against: credit nothing

        base = os.path.basename(camera_file.name.replace("\\", "/")).strip()
        if not base or base in (".", ".."):
            return None

        folder_tag = camera_file.folder.rstrip("/").split("/")[-1] or "DCIM"
        for name in (base, "%s_%s" % (folder_tag, base)):
            if name in pre_existing:
                continue  # already there before we started: not ours to claim
            candidate = os.path.join(dest_dir, name)
            if credited is not None and (
                os.path.normcase(os.path.abspath(candidate)) in credited
            ):
                continue  # an earlier file in this run already owns that copy
            try:
                if (
                    os.path.isfile(candidate)
                    and os.path.getsize(candidate) == camera_file.size
                ):
                    return candidate
            except OSError:
                continue
        return None

    @staticmethod
    def _dest_still_intact(outcome: DownloadOutcome) -> Tuple[bool, str]:
        """Re-``stat`` a verified local copy just before erasing the original.

        Verification happened minutes ago; the user may have moved the folder,
        emptied it, or unplugged the external drive since. Erasing the camera's
        copy of a file whose local copy has vanished is the one mistake this
        program can never take back.
        """
        dest = outcome.dest_path
        if not dest:
            return False, _msg(
                "transfer.err.gate_no_dest", "no local copy was recorded"
            )

        try:
            size = os.path.getsize(dest) if os.path.isfile(dest) else None
        except OSError as exc:
            return False, _msg(
                "transfer.err.gate_unreadable",
                "the local copy is no longer readable ({err})",
                err=exc.strerror or exc,
            )

        if size is None:
            return False, _msg(
                "transfer.err.gate_missing",
                "the local copy is missing from {dest}",
                dest=os.path.dirname(dest) or dest,
            )
        if size == 0:
            return False, _msg("transfer.err.gate_empty", "the local copy is empty")
        if outcome.file.size_known and size != outcome.file.size:
            return False, _msg(
                "transfer.err.gate_changed",
                "the local copy changed since it was checked ({now} bytes, expected {want})",
                now=size,
                want=outcome.file.size,
            )
        return True, ""
