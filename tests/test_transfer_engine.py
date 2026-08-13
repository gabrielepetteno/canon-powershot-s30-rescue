"""Tests for :mod:`retrocam.transfer` — the engine, not the delete gate.

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests

Stdlib only: no pytest, no camera, no network, no Pillow. Almost every test here
drives the **real** :class:`~retrocam.backends.massstorage.MassStorageBackend`
against a **real** DCIM tree on disk, because the properties under test — bytes
arriving intact, two folders sharing a base name not overwriting each other, a
cancelled run leaving no debris — only mean something end to end. A handful of
cases a real card cannot produce (a driver that raises raw, a driver that writes
the wrong bytes, a camera addressed by USB port) use the small purpose-built
backends at the top of this file.

The asymmetry that governs every assertion is the same one :mod:`retrocam.verify`
states: a file this engine calls ``ok`` may be erased from a 20-year-old card, so
the load-bearing assertions are the negative ones — what did **not** end up in
``report.deletable``, and what was **not** written before a refusal.

Why the card is not built from ``helpers.tiny_jpeg``
----------------------------------------------------
``TransferEngine`` always verifies with ``deep=True``. ``tiny_jpeg`` is
structurally valid but has no scan segment, so on a machine with Pillow
installed every deep check fails with "cannot identify image file" and every
happy-path assertion here would collapse — while passing on a machine without
Pillow. A test suite whose verdict depends on an optional dependency is not a
test suite, so :func:`photo` below wraps a genuinely decodable baseline JPEG
instead. Everything else comes from ``helpers``.
"""

from __future__ import annotations

import base64
import collections
import dataclasses
import os
import shutil
import struct
import sys
import unittest
from typing import Callable, Dict, List, Optional, Sequence

# The package lives in src/ and is not installed while the suite runs from a
# checkout. Derived from this file's location, so discovery works from anywhere.
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.join(os.path.dirname(_TESTS_DIR), "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from helpers import (  # noqa: E402
    RecordingProgress,
    TempDirCase,
    make_read_only,
    mass_storage_camera,
    not_a_jpeg,
    sha,
)
from retrocam import i18n  # noqa: E402
from retrocam.backends.base import CameraBackend, noop_progress  # noqa: E402
from retrocam.backends.massstorage import MassStorageBackend  # noqa: E402
from retrocam.model import (  # noqa: E402
    BackendKind,
    CameraError,
    CameraFile,
    CameraInfo,
    CancelToken,
    DeleteOutcome,
    DownloadOutcome,
    Progress,
    VerifyResult,
)
from retrocam.transfer import TransferEngine, TransferReport  # noqa: E402


# --------------------------------------------------------------------------- #
# A photograph the deep check accepts on every machine
# --------------------------------------------------------------------------- #

#: A real 8x8 baseline JPEG produced by Pillow once and frozen here. It is a
#: complete image — quantisation tables, Huffman tables, a scan — so Pillow
#: decodes it and :mod:`retrocam.verify` returns ``checked_decode=True``. Without
#: Pillow the same bytes pass the structural check. Either way: ``ok=True``.
_BASE_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABsSFBcUERsXFhceHBsgKEIrKCUlKFE6PTBCYFVlZF9V"
    "XVtqeJmBanGQc1tdhbWGkJ6jq62rZ4C8ybqmx5moq6T/wAALCAAIAAgBAREA/8QAHwAAAQUBAQEB"
    "AQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1Fh"
    "ByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZ"
    "WmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXG"
    "x8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/9oACAEBAAA/ACv/2Q=="
)


def photo(payload: int = 1000, marker: int = 0x11) -> bytes:
    """A decodable JPEG of a predictable size and distinguishable content.

    The filler rides in a COM segment inserted straight after the SOI marker,
    which is legal anywhere in a JPEG header and which both Pillow and the
    structural check step straight over. ``payload`` therefore controls the exact
    byte count and ``marker`` controls the content, so two photographs can be
    made to share a size while remaining different pictures — the case that makes
    the resume and cancel logic interesting.
    """
    body = bytes([marker]) * max(0, payload)
    com = b"\xff\xfe" + struct.pack(">H", len(body) + 2) + body
    return _BASE_JPEG[:2] + com + _BASE_JPEG[2:]


#: Mirrors ``helpers.DEFAULT_CARD``: the same base name in two folders, which is
#: what a Canon produces once its frame counter rolls over.
CARD: Dict[str, Dict[str, bytes]] = {
    "118CANON": {
        "IMG_0001.JPG": photo(1000, 0x11),
        "IMG_0002.JPG": photo(2000, 0x22),
    },
    "119CANON": {
        "IMG_0001.JPG": photo(3000, 0x33),
    },
}

#: As above, but the two ``IMG_0001.JPG`` frames are byte-for-byte the same
#: *length* while being different photographs. Name and size together are then
#: not enough to tell them apart, which is exactly what the "one copy is evidence
#: for exactly one photo" rule in the cancellation path defends against.
TWIN_CARD: Dict[str, Dict[str, bytes]] = {
    "118CANON": {
        "IMG_0001.JPG": photo(1000, 0x11),
        "IMG_0002.JPG": photo(2000, 0x22),
    },
    "119CANON": {
        "IMG_0001.JPG": photo(1000, 0x99),
    },
}


def payloads_of(spec: Dict[str, Dict[str, bytes]]) -> Dict[str, bytes]:
    """``{device_path: bytes}`` for a card spec, without touching the disk."""
    return {
        "/DCIM/%s/%s" % (folder, name): data
        for folder, files in spec.items()
        for name, data in files.items()
    }


# --------------------------------------------------------------------------- #
# Backends for the cases a real card cannot produce
# --------------------------------------------------------------------------- #


class _StubBackend(CameraBackend):
    """Concrete but inert. Subclasses override only the method they exercise.

    ``delete`` raises: nothing in this file may reach it. The delete gate is
    another test module's subject, and a download test that quietly erased a card
    would be the worst possible way to find that out.
    """

    kind = BackendKind.MASS_STORAGE
    display_name = "Stub"

    @classmethod
    def is_available(cls):  # type: ignore[override]
        return (True, "")

    def detect(self, progress=noop_progress) -> List[CameraInfo]:
        return []

    def list_files(
        self, camera, progress=noop_progress, cancel=None
    ) -> List[CameraFile]:
        return []

    def download(
        self,
        camera,
        files,
        dest_dir,
        progress=noop_progress,
        cancel=None,
        skip_existing=True,
    ) -> List[DownloadOutcome]:
        return []

    def delete(
        self, camera, files, progress=noop_progress, cancel=None
    ) -> List[DeleteOutcome]:
        raise AssertionError("no test in this module may reach backend.delete()")


class _RawExceptionBackend(_StubBackend):
    """A driver that breaks rule 1 of the backend contract and raises raw.

    Real ones do: pywin32 raises ``com_error``, a subprocess wrapper raises
    ``CalledProcessError``. The engine has to turn those into something a
    photographer can read.
    """

    MESSAGE = "libgphoto2 PTP layer: I/O error at 0x7f3a1c"

    def list_files(self, camera, progress=noop_progress, cancel=None):
        raise RuntimeError(self.MESSAGE)

    def download(
        self,
        camera,
        files,
        dest_dir,
        progress=noop_progress,
        cancel=None,
        skip_existing=True,
    ):
        raise RuntimeError(self.MESSAGE)


class _UsbPortBackend(_StubBackend):
    """A camera reached over USB rather than as a mounted volume.

    Its ``port`` is a gphoto2 address such as ``usb:001,004`` and it has no
    ``mount``/``dcim`` in ``raw``, so the engine has no filesystem root belonging
    to the device and the "destination is on the card" check has nothing to
    refuse.
    """

    kind = BackendKind.GPHOTO2

    def __init__(self, payloads: Dict[str, bytes]) -> None:
        self.payloads = dict(payloads)

    def list_files(self, camera, progress=noop_progress, cancel=None):
        out = []
        for path in sorted(self.payloads):
            folder, _, name = path.rpartition("/")
            out.append(
                CameraFile(folder=folder, name=name, size=len(self.payloads[path]))
            )
        return out

    def download(
        self,
        camera,
        files,
        dest_dir,
        progress=noop_progress,
        cancel=None,
        skip_existing=True,
    ):
        outcomes = []
        for camera_file in files:
            dest = CameraBackend.safe_dest_path(dest_dir, camera_file)
            with open(dest, "wb") as handle:
                handle.write(self.payloads[camera_file.path])
            outcomes.append(DownloadOutcome(file=camera_file, dest_path=dest, ok=True))
        return outcomes


class _MisreportingBackend(_StubBackend):
    """A driver whose outcome list does not describe the files it was handed.

    Real drivers do this. A gphoto2 build that lowercases ``IMG_0001.JPG`` on
    the way out reports a path nobody asked for; a WIA provider that loses an
    item mid-enumeration reports one fewer. The bytes may even be perfectly
    correct — what is lost is the mapping from *copy on disk* to *photograph on
    the card*, and every later step (the summary, the delete gate, the
    last-moment re-stat) is derived from that mapping.

    ``extra`` adds an outcome for a path that was never requested, ``drop``
    omits the outcome for one that was (its bytes still land, so the failure is
    purely in the report), and ``reverse`` returns the correct set in the wrong
    order — the harmless case the guard must *not* refuse.
    """

    def __init__(
        self,
        payloads: Dict[str, bytes],
        extra: Optional[str] = None,
        drop: Optional[str] = None,
        reverse: bool = False,
    ) -> None:
        self.payloads = dict(payloads)
        self.extra = extra
        self.drop = drop
        self.reverse = reverse

    def list_files(self, camera, progress=noop_progress, cancel=None):
        out = []
        for path in sorted(self.payloads):
            folder, _, name = path.rpartition("/")
            out.append(
                CameraFile(folder=folder, name=name, size=len(self.payloads[path]))
            )
        return out

    def download(
        self,
        camera,
        files,
        dest_dir,
        progress=noop_progress,
        cancel=None,
        skip_existing=True,
    ):
        outcomes = []
        for camera_file in files:
            dest = CameraBackend.safe_dest_path(dest_dir, camera_file)
            with open(dest, "wb") as handle:
                handle.write(self.payloads[camera_file.path])
            if camera_file.path == self.drop:
                # The copy lands; only its entry in the report goes missing.
                continue
            outcomes.append(DownloadOutcome(file=camera_file, dest_path=dest, ok=True))

        if self.extra is not None:
            folder, _, name = self.extra.rpartition("/")
            outcomes.append(
                DownloadOutcome(
                    file=CameraFile(folder=folder, name=name, size=0),
                    dest_path=None,
                    ok=True,
                )
            )
        if self.reverse:
            outcomes.reverse()
        return outcomes


class _SkipWithErrorBackend(_StubBackend):
    """Reports a file as skipped while also recording that something went wrong.

    A backend lands here when it finds a copy of the right size at the
    destination but cannot finish the comparison it wanted to make: the camera
    dropped the link while re-reading the source, or the timestamp could not be
    fetched. It is not claiming the copy is good — it is saying it does not
    know, and an outcome that says "skipped" while also carrying an error must
    be read as the error.
    """

    MESSAGE = "the camera dropped the connection while re-reading the source"

    def __init__(self, dest_paths: Dict[str, str]) -> None:
        self.dest_paths = dict(dest_paths)

    def download(
        self,
        camera,
        files,
        dest_dir,
        progress=noop_progress,
        cancel=None,
        skip_existing=True,
    ):
        return [
            DownloadOutcome(
                file=f,
                dest_path=self.dest_paths[f.path],
                ok=False,
                skipped=True,
                error=self.MESSAGE,
            )
            for f in files
        ]


class _CorruptingBackend(MassStorageBackend):
    """Copies the card correctly, then overwrites the copy and still claims ok.

    The only lie is the content: everything else — the temp-file-and-rename, the
    one-outcome-per-file contract, the destination names — is the genuine
    mass-storage code path. This is the failure verification exists to catch, and
    it is not hypothetical: a flaky USB 1.1 link on a 2003 body drops bytes and
    the driver reports a clean transfer.
    """

    def __init__(
        self,
        mangle: Callable[[CameraFile], bytes],
        only: Optional[Sequence[str]] = None,
    ) -> None:
        super().__init__()
        self._mangle = mangle
        self._only = None if only is None else set(only)

    def download(
        self,
        camera,
        files,
        dest_dir,
        progress=noop_progress,
        cancel=None,
        skip_existing=True,
    ):
        outcomes = super().download(
            camera, files, dest_dir, progress, cancel, skip_existing
        )
        rewritten = []
        for outcome in outcomes:
            targeted = self._only is None or outcome.file.name in self._only
            if outcome.dest_path and targeted:
                with open(outcome.dest_path, "wb") as handle:
                    handle.write(self._mangle(outcome.file))
                rewritten.append(dataclasses.replace(outcome, ok=True, skipped=False))
            else:
                rewritten.append(outcome)
        return rewritten


class _CancelWhenNamed:
    """Progress sink that cancels the run when a named file reaches a phase.

    Cancelling on a progress tick rather than from a timer keeps the test
    deterministic: the engine and the backend are single-threaded here, so the
    cancellation always lands at exactly the same instruction.
    """

    def __init__(self, token: CancelToken, name: str, phase: str = "download") -> None:
        self.token = token
        self.name = name
        self.phase = phase
        self.ticks: List[Progress] = []

    def __call__(self, tick: Progress) -> None:
        self.ticks.append(tick)
        if tick.phase == self.phase and tick.name == self.name:
            self.token.cancel()


_Usage = collections.namedtuple("_Usage", "total used free")


# --------------------------------------------------------------------------- #
# Shared fixture
# --------------------------------------------------------------------------- #


class _EngineCase(TempDirCase):
    """A card on disk, an engine driving it, and a pinned language."""

    def setUp(self) -> None:
        super().setUp()
        # Several assertions read user-facing text. Pin English so the suite gives
        # the same verdict on an Italian machine and after another test module has
        # called set_language().
        previous = i18n.current_language()
        i18n.set_language("en")
        self.addCleanup(i18n.set_language, previous)

    # -- building blocks --------------------------------------------------- #

    def card(self, spec=None, name: str = "card"):
        """``(card_root, {device_path: bytes})`` for a fresh DCIM tree."""
        return self.make_card(CARD if spec is None else spec, name=name)

    def engine_for(self, card_root: str, backend=None) -> TransferEngine:
        return TransferEngine(
            backend if backend is not None else MassStorageBackend(),
            mass_storage_camera(card_root),
        )

    @staticmethod
    def source(card_root: str, device_path: str) -> str:
        """Real path on the card for '/DCIM/118CANON/IMG_0001.JPG'."""
        return os.path.join(card_root, *device_path.strip("/").split("/"))

    @staticmethod
    def read(path: str) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()

    def snapshot(self, root: str) -> Dict[str, bytes]:
        """Every file under ``root`` as ``{device_path: bytes}``.

        Used to prove a refusal touched nothing: a stray file anywhere on the
        card shows up as an extra key and fails the comparison.
        """
        found: Dict[str, bytes] = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                found["/" + rel] = self.read(full)
        return found

    def patch_disk_usage(self, free: int) -> List[tuple]:
        """Make ``shutil.disk_usage`` report ``free`` bytes; return its call log.

        Each entry is ``(path, contents_of_path_at_call_time)``, which is how the
        tests prove the check ran *before* anything was copied rather than after.
        """
        original = shutil.disk_usage

        calls: List[tuple] = []

        def fake(path):
            listing = sorted(os.listdir(path)) if os.path.isdir(path) else None
            calls.append((path, listing))
            return _Usage(total=free * 4, used=free * 3, free=free)

        shutil.disk_usage = fake  # type: ignore[assignment]
        self.addCleanup(setattr, shutil, "disk_usage", original)
        return calls

    # -- assertions --------------------------------------------------------- #

    def assertNoLitter(self, dest_dir: str) -> None:
        """No half-written file may survive in the destination, ever.

        The backend writes to ``.rcr-<hex>.part`` and renames into place, so
        debris here means an interrupted copy is sitting next to the good ones —
        and a ``.part`` left behind is one refactor away from being mistaken for
        a photo.
        """
        leftovers = sorted(
            name
            for name in os.listdir(dest_dir)
            if name.endswith(".part") or name.startswith(".rcr-")
        )
        self.assertEqual(leftovers, [], "temporary files left in the destination")

    def assertEachPhotoIntact(self, report, card_root, contents) -> None:
        """Every outcome holds its *own* copy, byte-identical to the card."""
        seen = set()
        for outcome in report.outcomes:
            self.assertTrue(outcome.ok, outcome.error)
            self.assertIsNotNone(outcome.dest_path)
            self.assertNotIn(
                outcome.dest_path, seen, "two photographs share one destination file"
            )
            seen.add(outcome.dest_path)
            self.assertEqual(
                sha(outcome.dest_path),
                sha(self.source(card_root, outcome.file.path)),
                "%s is not byte-identical to the card" % outcome.file.path,
            )
            self.assertEqual(self.read(outcome.dest_path), contents[outcome.file.path])


# --------------------------------------------------------------------------- #
# construction
# --------------------------------------------------------------------------- #


class EnginePairingTests(_EngineCase):
    """A camera may only be driven by the backend that found it.

    ``CameraInfo.port`` has no meaning of its own: it is a mount point to mass
    storage, a ``usb:001,004`` address to gphoto2, a driver GUID to WIA. Pairing
    a camera with the wrong backend is therefore not a type error that surfaces
    as a clean failure — it is an address handed to a transport that will
    interpret it as something else entirely, and the operation waiting at the
    end of that mistake is ``delete()``. The engine refuses the pairing at
    construction, before any device has been touched.
    """

    def test_a_camera_found_by_another_backend_is_refused_at_construction(self) -> None:
        root, _ = self.card()
        camera = mass_storage_camera(root)  # kind = MASS_STORAGE
        self.assertEqual(camera.kind, BackendKind.MASS_STORAGE)

        with self.assertRaises(ValueError) as caught:
            TransferEngine(_UsbPortBackend({}), camera)  # kind = GPHOTO2

        # Both halves of the mismatch are named: "wrong backend" alone does not
        # tell a bug reporter which pairing went wrong.
        message = str(caught.exception)
        self.assertIn(BackendKind.MASS_STORAGE.value, message)
        self.assertIn(BackendKind.GPHOTO2.value, message)

    def test_the_matching_backend_is_accepted(self) -> None:
        # The control: the guard must refuse the mismatch, not every pairing.
        root, _ = self.card()
        engine = TransferEngine(MassStorageBackend(), mass_storage_camera(root))
        self.assertEqual(len(engine.list_files()), 3)


# --------------------------------------------------------------------------- #
# list_files
# --------------------------------------------------------------------------- #


class ListFilesTests(_EngineCase):
    """The listing is the input to everything else, including the delete gate."""

    def test_files_come_back_ordered_by_folder_then_name(self) -> None:
        # Folders and names are laid down in reverse order on purpose: a listing
        # that merely echoes os.walk / os.listdir cannot pass this by accident.
        root, _ = self.card(
            {
                "200OLYMP": {
                    "ZZ_0002.JPG": photo(30, 0x71),
                    "AA_0001.JPG": photo(20, 0x72),
                },
                "100CANON": {
                    "IMG_0009.JPG": photo(40, 0x73),
                    "IMG_0001.JPG": photo(50, 0x74),
                },
            }
        )
        engine = self.engine_for(root)

        keys = [(f.folder, f.name) for f in engine.list_files()]

        self.assertEqual(len(keys), 4)
        self.assertEqual(keys, sorted(keys))
        # Reproducible: the progress bar and any resume depend on two listings of
        # the same card agreeing.
        self.assertEqual(keys, [(f.folder, f.name) for f in engine.list_files()])

    def test_every_listed_file_reports_an_exact_size(self) -> None:
        # The size is the baseline verification checks against, and therefore the
        # number the delete gate ultimately rests on. A -1 here silently disables
        # the strongest truncation defence the program has.
        root, contents = self.card()
        for camera_file in self.engine_for(root).list_files():
            self.assertTrue(camera_file.size_known, camera_file.path)
            self.assertEqual(camera_file.size, len(contents[camera_file.path]))

    def test_an_empty_card_lists_nothing_instead_of_failing(self) -> None:
        # "Nothing to rescue" is a normal answer. Raising here would tell a user
        # with an already-emptied card that their camera is broken.
        root, _ = self.card({"118CANON": {}})
        self.assertEqual(self.engine_for(root).list_files(), [])

    def test_a_driver_raising_a_raw_exception_becomes_a_camera_error(self) -> None:
        root, _ = self.card()
        engine = self.engine_for(root, _RawExceptionBackend())

        with self.assertRaises(CameraError) as caught:
            engine.list_files()

        message = str(caught.exception)
        # The GUI shows this verbatim, so it must name the device and carry no
        # trace of the plumbing that failed.
        self.assertNotIsInstance(caught.exception, RuntimeError)
        self.assertIn(engine.camera.label, message)
        self.assertNotIn("Traceback", message)
        self.assertNotIn('File "', message)
        # The original is still chained, so a bug report can recover the detail.
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)

    def test_progress_none_is_accepted_by_list_files(self) -> None:
        # Regression: `progress=None` bypassed the keyword default and reached the
        # backend, where it surfaced as "'NoneType' object is not callable" — which
        # the engine then wrapped into a message blaming the *camera* for what was
        # a caller mistake. The most misleading way this code could fail.
        root, _ = self.card()
        files = self.engine_for(root).list_files(progress=None)
        self.assertEqual(len(files), 3)


# --------------------------------------------------------------------------- #
# download: the happy path
# --------------------------------------------------------------------------- #


class DownloadTests(_EngineCase):
    """What a successful rescue must look like on disk."""

    def test_every_photo_reaches_its_own_file_byte_identical_to_the_card(self) -> None:
        # 118CANON/IMG_0001.JPG and 119CANON/IMG_0001.JPG are different pictures
        # wearing one name. Landing them on top of each other would report three
        # rescued photos while holding two, and then offer all three for deletion.
        root, contents = self.card()
        engine = self.engine_for(root)
        dest = self.path("rescue")

        report = engine.download(engine.list_files(), dest)

        self.assertEqual(report.ok_count, 3)
        self.assertEqual(report.failed_count, 0)
        self.assertEqual(report.skipped_count, 0)
        self.assertTrue(report.all_verified)
        self.assertEqual(len(report.deletable), 3)
        self.assertEachPhotoIntact(report, root, contents)
        # Verification evidence is attached to each outcome, not merely implied.
        for outcome in report.outcomes:
            self.assertIsInstance(outcome.verify, VerifyResult)
            self.assertTrue(outcome.verify.ok)

    def test_a_finished_run_leaves_no_temporary_files_behind(self) -> None:
        root, _ = self.card()
        engine = self.engine_for(root)
        dest = self.path("rescue")

        engine.download(engine.list_files(), dest)

        self.assertNoLitter(dest)
        self.assertEqual(len(os.listdir(dest)), 3)

    def test_progress_none_is_accepted_by_download(self) -> None:
        root, _ = self.card()
        engine = self.engine_for(root)
        report = engine.download(
            engine.list_files(progress=None), self.path("rescue"), progress=None
        )
        self.assertEqual(report.ok_count, 3)

    def test_a_driver_raising_mid_download_becomes_a_camera_error(self) -> None:
        root, _ = self.card()
        engine = self.engine_for(root, _RawExceptionBackend())
        files = [CameraFile(folder="/DCIM/118CANON", name="IMG_0001.JPG", size=1335)]

        with self.assertRaises(CameraError) as caught:
            engine.download(files, self.path("rescue"))

        message = str(caught.exception)
        self.assertNotIn("Traceback", message)
        # The single fact the user needs before deciding what to do next.
        self.assertIn("Nothing was erased", message)


# --------------------------------------------------------------------------- #
# download: one outcome per requested file, or no report at all
# --------------------------------------------------------------------------- #


class OutcomeAlignmentTests(_EngineCase):
    """The driver's outcomes must describe exactly the files it was given.

    ``_align_outcomes`` re-pairs what came back with what was asked for. When
    that pairing is not one-to-one the program cannot say which copy on disk
    belongs to which photograph on the card — and the delete gate, the re-stat
    and the "78 of 82 recovered" line are all derived from it. There is no safe
    way to guess, so the whole run is refused with the copies left in place.
    """

    def test_an_outcome_for_a_file_that_was_never_requested_is_refused(self) -> None:
        # A driver that invents a path proves its bookkeeping disagrees with
        # ours. Accepting the outcomes we *can* map and ignoring the stray one
        # would be the tempting fix, and it is wrong: the disagreement is
        # evidence that the names we are about to hand to delete() may not mean
        # what we think they mean.
        root, contents = self.card()
        backend = _MisreportingBackend(
            payloads_of(CARD), extra="/DCIM/120CANON/IMG_9999.JPG"
        )
        engine = self.engine_for(root, backend)
        dest = self.path("rescue")

        with self.assertRaises(CameraError) as caught:
            engine.download(engine.list_files(), dest)

        message = str(caught.exception)
        self.assertIn("/DCIM/120CANON/IMG_9999.JPG", message)
        self.assertIn("nothing was erased from the camera", message.lower())
        self.assertNotIn("Traceback", message)
        # The refusal is a refusal to *reason*, not to copy: the bytes that did
        # land stay where they are, and the card is untouched.
        self.assertEqual(self.snapshot(root), contents)

    def test_a_file_the_driver_never_reported_back_on_is_refused(self) -> None:
        # The copy for IMG_0002.JPG is on disk and is perfectly good. It is still
        # refused, because a driver that loses one file from its report may have
        # lost track of which file it was — and the cost of guessing wrong here
        # is a photograph erased from the card.
        root, contents = self.card()
        backend = _MisreportingBackend(
            payloads_of(CARD), drop="/DCIM/118CANON/IMG_0002.JPG"
        )
        engine = self.engine_for(root, backend)
        dest = self.path("rescue")

        with self.assertRaises(CameraError) as caught:
            engine.download(engine.list_files(), dest)

        message = str(caught.exception)
        self.assertIn("/DCIM/118CANON/IMG_0002.JPG", message)
        self.assertIn("nothing was erased from the camera", message.lower())
        self.assertEqual(self.snapshot(root), contents)
        # The dropped file really did reach the disk: the refusal is about the
        # missing evidence, not about a missing copy.
        self.assertIn("IMG_0002.JPG", os.listdir(dest))

    def test_outcomes_returned_out_of_order_are_realigned_not_refused(self) -> None:
        # The control that keeps the two tests above honest. Order is the one
        # deviation the engine forgives, because it can re-derive the pairing
        # from the device paths with no ambiguity. Without this test, the guard
        # could tighten into "any deviation raises" and the suite would applaud.
        root, contents = self.card()
        backend = _MisreportingBackend(payloads_of(CARD), reverse=True)
        engine = self.engine_for(root, backend)
        dest = self.path("rescue")

        requested = engine.list_files()
        report = engine.download(requested, dest)

        self.assertEqual(report.ok_count, 3)
        self.assertEqual(report.failed_count, 0)
        # Realigned into the order the caller asked for, not the order it got
        # back: "file 3 of 3" in the UI must name the third requested file.
        self.assertEqual(
            [o.file.path for o in report.outcomes], [f.path for f in requested]
        )
        self.assertEachPhotoIntact(report, root, contents)


# --------------------------------------------------------------------------- #
# download: verification is genuinely applied
# --------------------------------------------------------------------------- #


class VerificationTests(_EngineCase):
    """A backend's ``ok`` is a claim about the transfer, never about the file."""

    def test_bytes_of_the_right_length_but_the_wrong_content_are_rejected(self) -> None:
        # The sharp case: the byte count matches exactly, so only a real
        # structural check can catch it. If verification ever degrades to a size
        # comparison, this is the test that notices.
        root, _ = self.card()
        backend = _CorruptingBackend(mangle=lambda f: not_a_jpeg(f.size))
        engine = self.engine_for(root, backend)
        dest = self.path("rescue")

        report = engine.download(engine.list_files(), dest)

        self.assertEqual(report.ok_count, 0)
        self.assertEqual(report.failed_count, 3)
        self.assertFalse(report.all_verified)
        self.assertEqual(report.deletable, [])
        for outcome in report.outcomes:
            self.assertFalse(outcome.ok)
            self.assertIsInstance(outcome.verify, VerifyResult)
            self.assertFalse(outcome.verify.ok)
            self.assertTrue(outcome.error, "a rejected file must say why")

    def test_a_short_file_is_rejected_even_though_the_driver_reported_success(
        self,
    ) -> None:
        # A genuine JPEG missing its last bytes: the classic interrupted transfer
        # over a flaky 20-year-old USB link, and the reason the exact size from
        # the listing is carried all the way through to verification.
        root, contents = self.card()
        backend = _CorruptingBackend(mangle=lambda f: contents[f.path][:-10])
        engine = self.engine_for(root, backend)

        report = engine.download(engine.list_files(), self.path("rescue"))

        self.assertEqual(report.ok_count, 0)
        self.assertFalse(report.all_verified)
        self.assertEqual(report.deletable, [])
        self.assertIn("truncated", report.outcomes[0].verify.reason)

    def test_one_damaged_file_does_not_condemn_the_others(self) -> None:
        # A card from 2003 usually has a few bad sectors. Losing the other 81
        # photos because of them would defeat the entire point of the program.
        root, contents = self.card()
        backend = _CorruptingBackend(
            mangle=lambda f: not_a_jpeg(f.size), only=["IMG_0002.JPG"]
        )
        engine = self.engine_for(root, backend)

        report = engine.download(engine.list_files(), self.path("rescue"))

        self.assertEqual(report.ok_count, 2)
        self.assertEqual(report.failed_count, 1)
        self.assertFalse(report.all_verified)
        self.assertEqual(
            sorted(f.name for f in report.deletable),
            ["IMG_0001.JPG", "IMG_0001.JPG"],
        )
        bad = [o for o in report.outcomes if not o.ok]
        self.assertEqual([o.file.name for o in bad], ["IMG_0002.JPG"])


# --------------------------------------------------------------------------- #
# download: resume
# --------------------------------------------------------------------------- #


class SkipExistingTests(_EngineCase):
    """A second run must add nothing and must trust nothing it did not check."""

    def test_a_second_identical_run_skips_everything_without_duplicating(self) -> None:
        root, contents = self.card()
        engine = self.engine_for(root)
        dest = self.path("rescue")

        first = engine.download(engine.list_files(), dest)
        after_first = sorted(os.listdir(dest))
        digests = {name: sha(os.path.join(dest, name)) for name in after_first}

        second = engine.download(engine.list_files(), dest)

        self.assertEqual(first.ok_count, 3)
        self.assertEqual(second.skipped_count, 3)
        # Skipped, but still re-read from disk this run: "a file with the right
        # name already exists" is not evidence that the photo survived.
        self.assertEqual(second.ok_count, 3)
        self.assertEqual(len(second.deletable), 3)
        self.assertEqual(sorted(os.listdir(dest)), after_first)
        self.assertEqual(
            {name: sha(os.path.join(dest, name)) for name in after_first}, digests
        )
        self.assertEachPhotoIntact(second, root, contents)

    def test_a_destination_of_the_right_name_but_the_wrong_size_is_re_fetched(
        self,
    ) -> None:
        # A half-finished copy from an earlier session wears the right name. Only
        # the size stops it being adopted as a completed download — and adopting
        # it would mark it verified and then erase the original from the card.
        root, contents = self.card()
        engine = self.engine_for(root)
        dest = self.path("rescue")
        engine.download(engine.list_files(), dest)

        stale = os.path.join(dest, "IMG_0002.JPG")
        with open(stale, "wb") as handle:
            handle.write(contents["/DCIM/118CANON/IMG_0002.JPG"][:200])

        report = engine.download(engine.list_files(), dest)

        refetched = [o for o in report.outcomes if o.file.name == "IMG_0002.JPG"]
        self.assertEqual(len(refetched), 1)
        outcome = refetched[0]
        self.assertFalse(outcome.skipped, "a short file was trusted as a finished copy")
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(
            self.read(outcome.dest_path), contents["/DCIM/118CANON/IMG_0002.JPG"]
        )
        # The backend never overwrites what it finds, so the fresh copy takes a
        # free name; what matters is that the truncated one is not the evidence.
        self.assertNotEqual(os.path.getsize(outcome.dest_path), 200)
        self.assertEqual(report.ok_count, 3)

    def test_a_skipped_file_carrying_a_backend_error_is_not_promoted_to_ok(
        self,
    ) -> None:
        """ "Skipped" only excuses a missing transfer when nothing went wrong.

        The engine treats a skipped file as transferred, because the copy on
        disk is the evidence and it re-reads it anyway. That shortcut must not
        extend to a backend that skipped *and* reported a problem: the copies
        here are byte-perfect and verification passes on every one of them, so
        the backend's error is the only thing standing between them and the
        delete gate. If the ``and not o.error`` clause ever goes, this test is
        what notices — the files would be marked ok on the strength of a check
        that was never in doubt, and erased from the card.
        """
        root, _ = self.card()
        dest = self.path("rescue")

        # A real first run, so the copies on disk are genuinely good.
        real = self.engine_for(root)
        first = real.download(real.list_files(), dest)
        self.assertEqual(first.ok_count, 3, "premise: three perfect copies on disk")
        requested = [o.file for o in first.outcomes]
        dest_paths = {o.file.path: o.dest_path for o in first.outcomes}

        backend = _SkipWithErrorBackend(dest_paths)
        report = self.engine_for(root, backend).download(requested, dest)

        self.assertEqual(report.skipped_count, 3)
        self.assertEqual(report.ok_count, 0)
        self.assertEqual(report.deletable, [])
        self.assertFalse(report.all_verified)
        for outcome in report.outcomes:
            self.assertFalse(outcome.ok, "a skipped file with an error was called ok")
            # The check itself passed. The demotion comes from the error alone,
            # which is what makes this test about the clause and not the check.
            self.assertIsNotNone(outcome.verify)
            self.assertTrue(outcome.verify.ok)
            self.assertIn(_SkipWithErrorBackend.MESSAGE, outcome.error)


# --------------------------------------------------------------------------- #
# download: free space
# --------------------------------------------------------------------------- #


class FreeSpaceTests(_EngineCase):
    """Discovering the disk is full at file 70 of 82 over USB 1.1 is not a plan."""

    def test_free_space_is_measured_before_the_first_byte_is_copied(self) -> None:
        calls = self.patch_disk_usage(free=1024**3)
        root, contents = self.card()
        engine = self.engine_for(root)
        dest = self.path("rescue")

        report = engine.download(engine.list_files(), dest)

        self.assertEqual(report.ok_count, 3)
        self.assertEqual(len(calls), 1)
        path, listing_at_call_time = calls[0]
        self.assertEqual(os.path.realpath(path), os.path.realpath(dest))
        # The destination was still empty when the question was asked.
        self.assertEqual(listing_at_call_time, [])
        self.assertEachPhotoIntact(report, root, contents)

    def test_a_card_larger_than_the_free_space_is_refused_before_copying(self) -> None:
        self.patch_disk_usage(free=1024)  # the card needs ~7 KB
        root, contents = self.card()
        engine = self.engine_for(root)
        dest = self.path("rescue")

        with self.assertRaises(CameraError) as caught:
            engine.download(engine.list_files(), dest)

        message = str(caught.exception)
        self.assertIn("space", message.lower())
        self.assertIn(dest, message)
        # Refused early: the destination exists (it was probed) but holds nothing,
        # and the card was never touched.
        self.assertEqual(os.listdir(dest), [])
        self.assertEqual(self.snapshot(root), contents)


# --------------------------------------------------------------------------- #
# download: which destinations are allowed
# --------------------------------------------------------------------------- #


class DestinationTests(_EngineCase):
    """The refusal that stops the program erasing a card while copying nothing.

    With the destination on the card, every downstream step stays individually
    correct and the outcome is total loss: the resume check finds each source
    file already sitting at its destination path and reports it skipped,
    verification re-reads *the original* and passes it, the gate re-stats *the
    original* and finds it intact, and the erase then removes every photo while
    no copy was ever made anywhere else. The only place to break that chain is
    before the first byte.
    """

    def test_a_destination_on_the_card_is_refused_before_anything_is_written(
        self,
    ) -> None:
        root, contents = self.card()
        engine = self.engine_for(root)
        files = engine.list_files()

        cases = [
            ("the card itself", root),
            ("the card's DCIM folder", os.path.join(root, "DCIM")),
            ("a new folder on the card", os.path.join(root, "RESCUE")),
            ("a sub-folder of DCIM", os.path.join(root, "DCIM", "118CANON")),
        ]
        link = self.path("card-link")
        try:
            os.symlink(root, link)
        except (OSError, NotImplementedError, AttributeError):  # pragma: no cover
            pass  # no symlink support here; the other four still carry the property
        else:
            # A symlink is how a user reaches the card in two clicks without the
            # path ever looking like the card.
            cases.append(("a symlink to the card", link))

        for label, dest in cases:
            with self.subTest(destination=label):
                with self.assertRaises(CameraError) as caught:
                    engine.download(files, dest)
                self.assertIn(root, str(caught.exception))
                # Nothing written, nothing erased, no new folder created.
                self.assertEqual(self.snapshot(root), contents)
                self.assertFalse(os.path.exists(os.path.join(root, "RESCUE")))

    def test_a_destination_that_contains_the_card_is_refused(self) -> None:
        # Writing the rescue into the folder that *holds* the card is the same
        # mistake seen from the other side, and containment is therefore tested
        # in both directions.
        root, contents = self.card()
        engine = self.engine_for(root)

        with self.assertRaises(CameraError):
            engine.download(engine.list_files(), self.tmp)

        self.assertEqual(self.snapshot(root), contents)

    def test_a_destination_beside_the_card_is_accepted(self) -> None:
        # The refusal must be narrow. A sibling folder under the same parent is
        # an ordinary, correct choice, and refusing it would push users toward
        # copying by hand with no verification at all.
        root, contents = self.card()
        engine = self.engine_for(root)

        report = engine.download(engine.list_files(), self.path("rescue"))

        self.assertEqual(report.ok_count, 3)
        self.assertEachPhotoIntact(report, root, contents)

    def test_a_camera_addressed_by_usb_port_refuses_no_destination(self) -> None:
        # Paired with test_a_destination_that_contains_the_card_is_refused: the
        # same destination, refused for a mounted card, must be allowed here.
        # 'usb:001,004' is not a directory, and treating it as one — or guessing
        # a root for a transport that has none — would block legitimate folders.
        root, _ = self.card()
        payloads = payloads_of(CARD)
        backend = _UsbPortBackend(payloads)
        camera = CameraInfo(
            model="Canon PowerShot S30",
            port="usb:001,004",
            kind=BackendKind.GPHOTO2,
            detail="usb",
        )
        engine = TransferEngine(backend, camera)
        self.assertEqual(engine._device_roots(), [])

        report = engine.download(engine.list_files(), self.tmp)

        self.assertEqual(report.ok_count, 3)
        self.assertTrue(report.all_verified)
        self.assertEqual(
            {self.read(o.dest_path) for o in report.outcomes},
            set(payloads.values()),
        )
        # The card that happens to live inside that destination is untouched.
        self.assertEqual(self.snapshot(root), payloads_of(CARD))

    def test_a_missing_destination_directory_is_created(self) -> None:
        root, contents = self.card()
        engine = self.engine_for(root)
        dest = self.path("rescue", "2003", "canon")
        self.assertFalse(os.path.exists(dest))

        report = engine.download(engine.list_files(), dest)

        self.assertTrue(os.path.isdir(dest))
        self.assertEqual(report.ok_count, 3)
        self.assertEachPhotoIntact(report, root, contents)

    def test_a_read_only_destination_is_refused_with_an_actionable_message(
        self,
    ) -> None:
        # os.access lies on Windows and on network shares, so the engine writes a
        # probe file. Finding out now costs a millisecond; finding out at file 70
        # of 82 over USB 1.1 costs the user their evening.
        root, contents = self.card()
        engine = self.engine_for(root)
        dest = self.path("rescue")
        os.makedirs(dest)
        make_read_only(dest)

        with self.assertRaises(CameraError) as caught:
            engine.download(engine.list_files(), dest)

        message = str(caught.exception)
        self.assertIn(dest, message)
        self.assertIn("folder", message.lower())  # says what to do next
        self.assertEqual(os.listdir(dest), [])
        self.assertEqual(self.snapshot(root), contents)


# --------------------------------------------------------------------------- #
# download: cancellation
# --------------------------------------------------------------------------- #


class CancellationTests(_EngineCase):
    """A cancelled run keeps what it finished and claims nothing more."""

    def test_cancelling_mid_run_yields_a_partial_report_of_what_completed(self) -> None:
        root, contents = self.card()
        engine = self.engine_for(root)
        dest = self.path("rescue")
        token = CancelToken()
        # Cancel the instant the second file starts: the first is complete on
        # disk, the second has not written a byte, the third was never reached.
        hook = _CancelWhenNamed(token, "IMG_0002.JPG")

        report = engine.download(engine.list_files(), dest, hook, token)

        self.assertTrue(report.aborted)
        self.assertEqual(len(report.outcomes), 3, "the report must cover every request")
        self.assertEqual(report.ok_count, 1)
        # Partial means partial: nothing about a cancelled run is "all verified".
        self.assertFalse(report.all_verified)

        done = report.outcomes[0]
        self.assertTrue(done.ok)
        self.assertEqual(done.file.path, "/DCIM/118CANON/IMG_0001.JPG")
        self.assertEqual(self.read(done.dest_path), contents[done.file.path])
        self.assertEqual(report.deletable, [done.file])

        for outcome in report.outcomes[1:]:
            self.assertFalse(outcome.ok)
            self.assertIsNone(outcome.dest_path)
            self.assertTrue(outcome.error)

        self.assertNoLitter(dest)
        self.assertEqual(os.listdir(dest), ["IMG_0001.JPG"])
        self.assertEqual(self.snapshot(root), contents)

    def test_one_copy_on_disk_is_credited_to_exactly_one_photograph(self) -> None:
        # TWIN_CARD holds two different pictures both called IMG_0001.JPG with the
        # same byte count. After a cancellation the engine rebuilds outcomes from
        # disk, and name-plus-size cannot tell these two apart: only the rule that
        # a copy already claimed by an earlier file is never claimed again keeps
        # 119CANON/IMG_0001.JPG from being reported as rescued, verified and safe
        # to erase while its bytes exist nowhere on the computer.
        root, contents = self.card(TWIN_CARD)
        engine = self.engine_for(root)
        dest = self.path("rescue")
        token = CancelToken()
        hook = _CancelWhenNamed(token, "IMG_0002.JPG")

        report = engine.download(engine.list_files(), dest, hook, token)

        self.assertTrue(report.aborted)
        self.assertEqual(report.ok_count, 1)
        self.assertEqual(len(report.deletable), 1)

        by_path = {o.file.path: o for o in report.outcomes}
        rescued = by_path["/DCIM/118CANON/IMG_0001.JPG"]
        twin = by_path["/DCIM/119CANON/IMG_0001.JPG"]
        self.assertTrue(rescued.ok)
        self.assertEqual(
            sha(rescued.dest_path), sha(self.source(root, rescued.file.path))
        )
        self.assertFalse(twin.ok, "a photo with no copy on disk was reported rescued")
        self.assertIsNone(twin.dest_path)
        self.assertNotIn(twin.file, report.deletable)

    def test_cancelling_during_verification_leaves_unchecked_files_not_ok(self) -> None:
        # The other cancellation path: the transfer finished, the check did not.
        # The files are on disk and a re-run will verify them properly, but until
        # something has actually read them back nothing may be called verified.
        root, contents = self.card()
        engine = self.engine_for(root)
        dest = self.path("rescue")
        token = CancelToken()
        hook = _CancelWhenNamed(token, "IMG_0001.JPG", phase="verify")

        report = engine.download(engine.list_files(), dest, hook, token)

        self.assertTrue(report.aborted)
        self.assertEqual(report.ok_count, 1)
        self.assertEqual(len(report.deletable), 1)
        self.assertFalse(report.all_verified)
        for outcome in report.outcomes[1:]:
            self.assertFalse(outcome.ok)
            self.assertIsNone(outcome.verify, "an unchecked file kept a verdict")
            self.assertTrue(outcome.error)
        # All three copies really are on disk; only the evidence is missing.
        self.assertEqual(len(os.listdir(dest)), 3)
        self.assertNoLitter(dest)
        self.assertEqual(self.snapshot(root), contents)


# --------------------------------------------------------------------------- #
# progress
# --------------------------------------------------------------------------- #


class ProgressTests(_EngineCase):
    """The only window the user has into a transfer that takes twenty minutes."""

    def test_download_and_verify_are_both_reported_file_by_file(self) -> None:
        root, _ = self.card()
        engine = self.engine_for(root)
        # fail_on_thread turns the sink into a tripwire: the engine must run on
        # the caller's thread, because the GUI's real callback is only safe to
        # call from the worker thread that owns the queue.
        ticks = RecordingProgress(fail_on_thread=True)

        report = engine.download(engine.list_files(ticks), self.path("rescue"), ticks)

        self.assertEqual(report.ok_count, 3)
        self.assertTrue(ticks.saw_phase("download"))
        self.assertTrue(ticks.saw_phase("verify"))
        # One named verify tick per requested file, in the requested order — the
        # two IMG_0001.JPG entries are different photographs and both must appear.
        self.assertEqual(
            [t.name for t in ticks.of_phase("verify") if t.name],
            ["IMG_0001.JPG", "IMG_0002.JPG", "IMG_0001.JPG"],
        )
        self.assertEqual(
            {t.name for t in ticks.of_phase("download") if t.name},
            {"IMG_0001.JPG", "IMG_0002.JPG"},
        )
        self.assertTrue(ticks.messages(), "the log pane would stay empty")

    def test_no_tick_claims_more_items_than_its_total(self) -> None:
        root, _ = self.card()
        engine = self.engine_for(root)
        ticks = RecordingProgress()

        engine.download(engine.list_files(ticks), self.path("rescue"), ticks)

        self.assertTrue(ticks.ticks)
        for tick in ticks.ticks:
            # Progress.fraction clamps into [0, 1], so it would happily hide an
            # index of 5 out of 3 and a progress bar that jumps backwards. The raw
            # index is what can actually catch that; the clamp is checked too so
            # the GUI's contract is stated in one place.
            if tick.total > 0:
                self.assertLessEqual(
                    tick.index, tick.total, "%r overruns its total" % (tick,)
                )
            self.assertGreaterEqual(tick.fraction, 0.0)
            self.assertLessEqual(tick.fraction, 1.0)


# --------------------------------------------------------------------------- #
# TransferReport
# --------------------------------------------------------------------------- #


class TransferReportTests(_EngineCase):
    """The object the GUI renders and the delete gate reads."""

    def test_summary_lines_are_readable_with_nothing_left_unformatted(self) -> None:
        root, _ = self.card()
        backend = _CorruptingBackend(
            mangle=lambda f: not_a_jpeg(f.size), only=["IMG_0002.JPG"]
        )
        engine = self.engine_for(root, backend)
        dest = self.path("rescue")

        report = engine.download(engine.list_files(), dest)
        lines = report.summary_lines()

        self.assertTrue(lines)
        for line in lines:
            self.assertIsInstance(line, str)
            self.assertTrue(line.strip(), "a blank line in the summary")
            # A leaked '{dest}' means the i18n fallback failed; a line that is
            # still a key means the translation table was consulted and lost.
            self.assertNotIn("{", line)
            self.assertFalse(line.startswith("transfer."))
        joined = "\n".join(lines)
        self.assertIn(dest, joined)
        self.assertIn("2 of 3", joined)
        self.assertIn("IMG_0002.JPG", joined)  # the failure is named, not just counted

    def test_an_empty_request_is_never_all_verified(self) -> None:
        # Nothing was proven, so no delete button may light up because of it — and
        # nothing is created on disk either.
        root, _ = self.card()
        engine = self.engine_for(root)
        dest = self.path("rescue")

        report = engine.download([], dest)

        self.assertEqual(report.outcomes, [])
        self.assertFalse(report.all_verified)
        self.assertEqual(report.deletable, [])
        self.assertFalse(os.path.exists(dest))

    def test_a_finished_report_ignores_later_edits_to_the_caller_s_list(self) -> None:
        # Frozen protects the attribute binding, not the list behind it. Without
        # the defensive copy a caller could append a hand-built ok=True outcome to
        # its own list and watch it appear in `deletable`.
        good = CameraFile(folder="/DCIM/118CANON", name="IMG_0001.JPG", size=10)
        smuggled = CameraFile(folder="/DCIM/118CANON", name="IMG_0002.JPG", size=10)
        outcomes = [
            DownloadOutcome(
                file=good,
                dest_path="/somewhere/IMG_0001.JPG",
                ok=True,
                verify=VerifyResult(True),
            )
        ]
        report = TransferReport(outcomes=outcomes, dest_dir="/somewhere")

        outcomes.append(
            DownloadOutcome(
                file=smuggled,
                dest_path="/somewhere/IMG_0002.JPG",
                ok=True,
                verify=VerifyResult(True),
            )
        )

        self.assertEqual(len(report.outcomes), 1)
        self.assertEqual(report.deletable, [good])

    def test_an_outcome_without_verification_evidence_is_not_deletable(self) -> None:
        # `deletable` is re-derived from the evidence rather than trusted from the
        # ok flag, so a future refactor that sets ok=True without checking the
        # bytes drops out of the deletion set instead of quietly joining it.
        camera_file = CameraFile(folder="/DCIM/118CANON", name="IMG_0001.JPG", size=10)
        report = TransferReport(
            outcomes=[
                DownloadOutcome(
                    file=camera_file, dest_path="/somewhere/IMG_0001.JPG", ok=True
                )
            ],
            dest_dir="/somewhere",
        )

        self.assertEqual(report.ok_count, 1)
        self.assertTrue(report.all_verified)
        self.assertEqual(report.deletable, [], "an unverified file reached the gate")


if __name__ == "__main__":  # pragma: no cover - convenience runner
    unittest.main()
