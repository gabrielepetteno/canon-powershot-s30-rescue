"""The list of backends, and the one place that asks all of them at once.

The GUI never imports a backend directly. It asks this module which transports
exist (:func:`backend_status`), which ones can run here
(:func:`available_backends`), and what is plugged in (:func:`detect_all`). Adding
a fourth transport means adding one import and one list entry.

Ordering is a safety decision, not an aesthetic one. Mass storage comes first
because it is the only path that needs no driver, no daemon and no elevated
anything: a CompactFlash card in a reader is read with plain ``open()``. The
gphoto2 CLI comes next — it can reach the camera body itself, including
pre-PTP Canons, but only where it is installed. WIA comes last: on Windows it
sees only cameras Windows already has a driver for, which a 2001 PowerShot is
not.

Nothing here is cached. The user is expected to plug the camera in, install a
missing tool, and press Refresh — a stale "no camera found" is the most
frustrating answer this app could give.
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence, Set, Tuple, Type

from .backends.base import Availability, CameraBackend, noop_progress
from .backends.gphoto2_backend import GPhoto2Backend
from .backends.massstorage import MassStorageBackend
from .backends.wia import WiaBackend
from .model import (
    CameraError,
    CameraInfo,
    Progress,
    ProgressCallback,
    TransferAborted,
)

__all__ = [
    "ALL_BACKENDS",
    "available_backends",
    "backend_status",
    "detect_all",
]


#: Every transport RetroCam knows about, in the order they are tried.
#: Safest and least demanding first — see the module docstring.
ALL_BACKENDS: List[Type[CameraBackend]] = [
    MassStorageBackend,
    GPhoto2Backend,
    WiaBackend,
]


# --------------------------------------------------------------------------- #
# Availability
# --------------------------------------------------------------------------- #


def _safe_availability(backend_cls: Type[CameraBackend]) -> Availability:
    """Ask a backend whether it can run, defensively.

    ``is_available()`` is contractually forbidden from raising, but this is the
    startup path for the whole application: one backend with a bad import or a
    typo in a ctypes signature must not stop the other two from rescuing
    photos. A backend that misbehaves here is simply reported as unavailable,
    with the reason visible in the environment panel.
    """
    try:
        result = backend_cls.is_available()
    except Exception as exc:
        return False, "%s could not be checked: %s" % (_name_of(backend_cls), exc)

    try:
        ok, hint = result
        return bool(ok), str(hint or "")
    except Exception:
        # A backend that returned something other than (bool, str).
        return bool(result), ""


def _name_of(backend_cls: Type[CameraBackend]) -> str:
    """Display name of a backend class, falling back to the class name."""
    name = getattr(backend_cls, "display_name", "") or ""
    return str(name) if name else backend_cls.__name__


def backend_status() -> List[Tuple[Type[CameraBackend], bool, str]]:
    """``(class, available, hint)`` for every backend, in registry order.

    This is what the environment panel renders: the unavailable ones matter as
    much as the available ones, because their hints are the instructions for
    making them work.
    """
    return [(cls,) + _safe_availability(cls) for cls in ALL_BACKENDS]


def available_backends() -> List[Type[CameraBackend]]:
    """The backends that can actually run on this machine right now."""
    return [cls for cls, ok, _hint in backend_status() if ok]


# --------------------------------------------------------------------------- #
# De-duplication
# --------------------------------------------------------------------------- #

#: Words that appear in device descriptions without identifying anything.
#: Dropping them keeps 'Removable drive E:' from matching 'USB PTP Camera'.
_NOISE_WORDS = frozenset(
    [
        "camera",
        "cameras",
        "card",
        "device",
        "digital",
        "disk",
        "drive",
        "external",
        "generic",
        "mass",
        "media",
        "mtp",
        "portable",
        "ptp",
        "reader",
        "removable",
        "storage",
        "unknown",
        "usb",
        "volume",
    ]
)


def _model_tokens(model: str) -> Set[str]:
    """Distinctive lowercase tokens of a model string, noise removed."""
    tokens = re.findall(r"[a-z0-9]+", (model or "").lower())
    return set(t for t in tokens if len(t) >= 2 and t not in _NOISE_WORDS)


def _looks_like_same_device(first: CameraInfo, second: CameraInfo) -> bool:
    """Best-effort guess that two listings are one physical camera.

    Honest description of the heuristic, because it is a guess and nothing more:
    there is **no** reliable cross-transport device identity. gphoto2 reports
    ``usb:001,004``, WIA reports a driver GUID, and a mounted card reports a
    mount point; none of these can be compared. All we have is the model text,
    which each transport invents differently.

    So we compare distinctive word sets and demand real overlap — identical
    token sets, or one being a subset of the other with at least two tokens in
    common. 'Canon PowerShot S30' matches 'Canon PowerShot S30'; it does not
    match 'Removable drive E:'.

    Two entries from the *same* backend are never merged: a backend that reports
    two devices is reporting two devices (two card readers, two ports), and
    hiding one would be a rescue failure.

    The bias is deliberate: showing the same camera twice is a cosmetic
    annoyance, hiding a device someone needs is data loss. When in doubt, this
    returns False.
    """
    if first.kind == second.kind:
        return False

    first_tokens = _model_tokens(first.model)
    second_tokens = _model_tokens(second.model)
    if not first_tokens or not second_tokens:
        return False
    if first_tokens == second_tokens:
        return True

    smaller, larger = (
        (first_tokens, second_tokens)
        if len(first_tokens) <= len(second_tokens)
        else (second_tokens, first_tokens)
    )
    # One shared word is not evidence — half the world's cameras say 'Canon'.
    return len(smaller) >= 2 and smaller.issubset(larger)


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #


def _guard(progress: ProgressCallback) -> ProgressCallback:
    """Wrap a progress sink so a failure inside it cannot escape.

    The far end of this callback is the GUI's queue. A hiccup there — a full
    queue, a Tk teardown race, a bug in the log pane — must never be able to
    make a camera vanish from the list, so the sink handed to backends
    swallows its own errors instead of aborting their ``detect()``.
    """

    def _sink(item: Progress) -> None:
        try:
            progress(item)
        except Exception:
            pass

    return _sink


def _emit(progress: ProgressCallback, item: Progress) -> None:
    """Push one progress tick. A logging failure must not abort detection."""
    _guard(progress)(item)


def detect_all(
    progress: ProgressCallback = noop_progress,
) -> List[Tuple[CameraBackend, CameraInfo]]:
    """Ask every usable backend what it can see, and return everything found.

    Each result pairs a camera with the *live backend instance* that found it —
    that instance is the only object that knows how to list, download from and
    delete on that device, so the caller must keep it.

    One backend failing never hides another's results. A
    :class:`~retrocam.model.CameraError` is reported through ``progress.message``
    and detection continues with the next transport; the user sees "gphoto2
    could not claim the camera" *and* the card that was found in the reader.

    Cancellation is the one exception: a
    :class:`~retrocam.model.TransferAborted` is a user instruction, not a
    failure, and is re-raised so the GUI stops instead of quietly carrying on.
    """
    statuses = backend_status()
    usable = [(cls, hint) for cls, ok, hint in statuses if ok]
    total = len(usable)

    # Say why a transport was not tried. Silence here reads as "my camera is
    # broken" when the truth is usually "a tool is missing".
    for backend_cls, ok, hint in statuses:
        if not ok:
            _emit(
                progress,
                Progress(
                    phase="detect",
                    total=total,
                    name=_name_of(backend_cls),
                    message="%s: not available%s"
                    % (_name_of(backend_cls), (" — " + hint) if hint else ""),
                ),
            )

    results: List[Tuple[CameraBackend, CameraInfo]] = []

    for index, (backend_cls, _hint) in enumerate(usable):
        name = _name_of(backend_cls)
        _emit(
            progress,
            Progress(
                phase="detect",
                index=index,
                total=total,
                name=name,
                message="Looking for cameras via %s…" % name,
            ),
        )

        try:
            backend = backend_cls()
            # The backend's own progress ticks are forwarded untouched: it knows
            # what it is doing ("scanning E:\"), we only know that it is its
            # turn. The sink is guarded so a backend that dutifully reports
            # progress cannot be killed by a failure in the GUI's log.
            found = backend.detect(_guard(progress))
        except TransferAborted:
            raise
        except CameraError as exc:
            _emit(
                progress,
                Progress(
                    phase="detect",
                    index=index,
                    total=total,
                    name=name,
                    message="%s: %s" % (name, exc),
                ),
            )
            continue
        except Exception as exc:
            # A backend bug (bad ctypes call, COM error) is not a reason to lose
            # the results of the transports that did work.
            _emit(
                progress,
                Progress(
                    phase="detect",
                    index=index,
                    total=total,
                    name=name,
                    message="%s: unexpected failure — %s" % (name, exc),
                ),
            )
            continue

        for camera in found or []:
            duplicate = _first_match(results, camera)
            if duplicate is not None:
                # Never drop one silently: the log must show what was merged, so
                # a wrong guess is visible rather than mysterious.
                _emit(
                    progress,
                    Progress(
                        phase="detect",
                        index=index,
                        total=total,
                        name=name,
                        message="%s: '%s' looks like the '%s' already found via "
                        "%s — keeping the first one."
                        % (name, camera.label, duplicate.label, duplicate.kind.value),
                    ),
                )
                continue
            results.append((backend, camera))
            _emit(
                progress,
                Progress(
                    phase="detect",
                    index=index,
                    total=total,
                    name=name,
                    message="Found: %s" % camera.label,
                ),
            )

    _emit(
        progress,
        Progress(
            phase="detect",
            index=total,
            total=total,
            message="Detection finished — %d device(s) found." % len(results),
        ),
    )
    return results


def _first_match(
    results: Sequence[Tuple[CameraBackend, CameraInfo]], camera: CameraInfo
) -> Optional[CameraInfo]:
    """The already-collected camera that ``camera`` duplicates, if any.

    Earlier entries win, which is exactly why the registry order matters: a card
    read directly through the filesystem is always preferable to the same photos
    fetched over a 20-year-old USB 1.1 protocol.
    """
    for _backend, existing in results:
        if _looks_like_same_device(existing, camera):
            return existing
    return None
