"""Tests for the delete gate: the one property the whole program rests on.

    A file is erased from the camera **only** if its bytes were written to disk
    and byte-verified during this run.

Everything here is an attack on that sentence. The gate lives in
:meth:`retrocam.transfer.TransferEngine.delete_verified` and in
:attr:`retrocam.transfer.TransferReport.deletable`, and the interesting
assertions are all negative ones: what must *not* reach ``backend.delete()``.

Two kinds of fixture are used, and the difference is deliberate:

* **A real card, a real backend, a real report.** Wherever the state can be
  reached honestly, it is: :class:`~retrocam.backends.massstorage.MassStorageBackend`
  walks a real DCIM tree on disk, copies real bytes, and the engine re-reads
  every destination file. A gate tested only against hand-built objects would
  prove that the assertions match the assertions.
* **Hand-built outcomes.** A few states cannot be produced by an honest run at
  all — an outcome forged with ``ok=True`` and no evidence, or two different
  photographs reporting the same device path — and those are exactly the states
  a future refactor could introduce. They are built by hand and pushed at the
  gate directly.

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests

Stdlib only, no Pillow, no camera, no network.
"""

from __future__ import annotations

import os
import sys
import unittest
from typing import List, Optional, Sequence, Tuple

# Discovery puts ``tests/`` on sys.path, but be explicit so the file also runs
# when pointed at directly from another directory.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from helpers import (  # noqa: E402
    CardSpec,
    RecordingProgress,
    TempDirCase,
    mass_storage_camera,
    not_a_jpeg,
    riff_avi,
    sha,
    tiny_jpeg,
    truncated_jpeg,
)

from retrocam.backends.base import (  # noqa: E402
    Availability,
    CameraBackend,
    noop_progress,
)
from retrocam.backends.massstorage import MassStorageBackend  # noqa: E402
from retrocam.model import (  # noqa: E402
    BackendKind,
    CameraError,
    CameraFile,
    CameraInfo,
    CancelToken,
    DeleteOutcome,
    DownloadOutcome,
    ProgressCallback,
    VerifyResult,
)
from retrocam.transfer import TransferEngine, TransferReport  # noqa: E402


# --------------------------------------------------------------------------- #
# Card fixtures
# --------------------------------------------------------------------------- #

# WHY these are AVI clips and not the JPEGs of ``helpers.DEFAULT_CARD``:
# the engine always verifies with ``deep=True``, and ``helpers.tiny_jpeg`` is
# structurally valid but deliberately *not decodable*. On a machine that happens
# to have Pillow installed, every JPEG on the card would therefore fail the
# decode step, no outcome would ever be verified, and not one test in this file
# could reach the gate at all — the suite would silently stop testing the thing
# it exists to test. ``.avi`` is not in verify.py's strict-decode set, so these
# files verify identically with and without Pillow.
#
# The layout still mirrors DEFAULT_CARD in the way that matters: the same base
# name lives in two different folders, which is the case that broke three
# backends during review. A Canon PowerShot of this era really does write
# MVI_xxxx.AVI next to its stills, so this is a card layout, not a contrivance.
_GOOD_CARD: CardSpec = {
    "118CANON": {
        "MVI_0001.AVI": riff_avi(1000),
        "MVI_0002.AVI": riff_avi(2000),
    },
    "119CANON": {
        # Same base name as above, different clip, different bytes.
        "MVI_0001.AVI": riff_avi(3000),
    },
}

#: One file that transfers and verifies, one that transfers perfectly and then
#: fails verification. The truncated JPEG is copied byte-for-byte off the card —
#: the *transfer* succeeds — and is rejected only because the file itself is
#: missing its FFD9 end marker. That is the partial-report case, and it fails
#: structurally, so it fails the same way with and without Pillow.
_MIXED_CARD: CardSpec = {
    "118CANON": {
        "MVI_0001.AVI": riff_avi(1000),
        "IMG_0002.JPG": truncated_jpeg(2000),
    },
}

#: Single file, for the resume/skip case.
_ONE_FILE_CARD: CardSpec = {"118CANON": {"MVI_0001.AVI": riff_avi(1000)}}


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


class _RecordingBackend(CameraBackend):
    """A backend that records what the gate hands it and erases nothing.

    Deleting nothing is the point: these tests are about the *set of files that
    reached the backend*, which is precisely and only what the gate controls.
    Recording ``list(files)`` keeps the element identities intact, so assertions
    can be made with ``is`` rather than ``==`` — two different photographs can
    compare equal (see :class:`DuplicateDevicePathTest`), and an equality-based
    assertion would pass while the wrong picture was being erased.
    """

    kind = BackendKind.MASS_STORAGE
    display_name = "recording test backend"

    def __init__(
        self, can_delete: bool = True, extra: Optional[CameraFile] = None
    ) -> None:
        #: One entry per ``delete()`` call: the files it was handed, in order.
        self.batches: List[List[CameraFile]] = []
        self._can_delete = can_delete
        #: A file this backend will claim to have erased although it was never
        #: given it — the post-condition the engine must catch.
        self._extra = extra

    # -- CameraBackend contract ------------------------------------------- #

    @classmethod
    def is_available(cls) -> Availability:
        return (True, "")

    def detect(self, progress: ProgressCallback = noop_progress) -> List[CameraInfo]:
        return []

    def list_files(
        self,
        camera: CameraInfo,
        progress: ProgressCallback = noop_progress,
        cancel: Optional[CancelToken] = None,
    ) -> List[CameraFile]:
        return []

    def download(
        self,
        camera: CameraInfo,
        files: Sequence[CameraFile],
        dest_dir: str,
        progress: ProgressCallback = noop_progress,
        cancel: Optional[CancelToken] = None,
        skip_existing: bool = True,
    ) -> List[DownloadOutcome]:
        raise AssertionError(
            "the delete-gate tests download through the real backend, never this one"
        )

    def delete(
        self,
        camera: CameraInfo,
        files: Sequence[CameraFile],
        progress: ProgressCallback = noop_progress,
        cancel: Optional[CancelToken] = None,
    ) -> List[DeleteOutcome]:
        self.batches.append(list(files))
        outcomes = [DeleteOutcome(file=f, ok=True) for f in files]
        if self._extra is not None:
            outcomes.append(DeleteOutcome(file=self._extra, ok=True))
        return outcomes

    def supports_delete(self) -> bool:
        return self._can_delete

    # -- assertions helper ------------------------------------------------- #

    @property
    def handed(self) -> List[CameraFile]:
        """The single batch handed to ``delete()``; raises if there was not one."""
        if len(self.batches) != 1:
            raise AssertionError(
                "expected exactly one delete() call, got %d" % len(self.batches)
            )
        return self.batches[0]


class _ImpostorReport:
    """An object that merely *looks* like a report, with a wide-open gate.

    It exposes exactly the two members ``delete_verified`` reads, and both say
    "erase everything". If the gate trusted duck typing, this object would empty
    the card; the ``isinstance`` check is the only thing between the two, which
    is why it gets a test of its own.
    """

    aborted = False
    dest_dir = "/nowhere"
    outcomes: List[DownloadOutcome] = []
    failed_count = 0

    def __init__(self, files: Sequence[CameraFile]) -> None:
        self._files = list(files)

    @property
    def deletable(self) -> List[CameraFile]:
        return list(self._files)

    def verified_outcomes(self) -> List[DownloadOutcome]:
        return [
            DownloadOutcome(
                file=f,
                dest_path="/nowhere/%s" % f.name,
                ok=True,
                verify=VerifyResult(ok=True),
            )
            for f in self._files
        ]


# --------------------------------------------------------------------------- #
# Shared base case
# --------------------------------------------------------------------------- #


class _GateCase(TempDirCase):
    """A real card on disk, plus the plumbing to run a real rescue against it."""

    #: Card layout built in ``setUp``. Subclasses override.
    CARD: CardSpec = _GOOD_CARD

    def setUp(self) -> None:
        super().setUp()
        self.card_root, self.contents = self.make_card(self.CARD)
        self.camera = mass_storage_camera(self.card_root)
        self.dest = self.path("rescued")
        self.progress = RecordingProgress()

    # -- real work --------------------------------------------------------- #

    def rescue(self, skip_existing: bool = True) -> TransferReport:
        """List, download and verify the fixture card for real. No stubs.

        The report returned here is the same object a genuine rescue produces,
        which is what makes the gate assertions worth anything.
        """
        engine = TransferEngine(MassStorageBackend(), self.camera)
        files = engine.list_files(self.progress)
        self.assertTrue(files, "the fixture card produced no files to download")
        return engine.download(
            files, self.dest, self.progress, skip_existing=skip_existing
        )

    def gate(self, backend: CameraBackend) -> TransferEngine:
        """An engine wired to ``backend``, for calling ``delete_verified``."""
        return TransferEngine(backend, self.camera)

    # -- lookups ----------------------------------------------------------- #

    def card_path(self, device_path: str) -> str:
        """Real filesystem path on the fixture card for a device path."""
        return os.path.join(self.card_root, *device_path.strip("/").split("/"))

    def outcome_for(self, report: TransferReport, device_path: str) -> DownloadOutcome:
        matches = [o for o in report.outcomes if o.file.path == device_path]
        self.assertEqual(
            len(matches), 1, "expected exactly one outcome for %s" % device_path
        )
        return matches[0]

    def write_dest(self, name: str, data: bytes) -> str:
        """Write a file into the destination folder and return its path."""
        os.makedirs(self.dest, exist_ok=True)
        path = os.path.join(self.dest, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def local_copy(
        self, folder: str = "/DCIM/118CANON", name: str = "IMG_9001.JPG"
    ) -> Tuple[CameraFile, str]:
        """``(camera_file, dest_path)`` for a hand-built outcome.

        The local copy really exists on disk and really has the size recorded on
        the :class:`CameraFile`, so the gate's last-moment ``stat`` accepts it.
        Only the state a test is actually about gets faked; everything the gate
        checks around it is genuine, or the test would pass for the wrong reason.
        """
        data = tiny_jpeg(300)
        dest_path = self.write_dest(name, data)
        camera_file = CameraFile(folder=folder, name=name, size=len(data), mtime=1.0)
        return camera_file, dest_path


# --------------------------------------------------------------------------- #
# 1. `deletable` is derived from evidence, never from a flag
# --------------------------------------------------------------------------- #


class DeletableDerivationTest(_GateCase):
    """``TransferReport.deletable`` re-derives membership from the evidence."""

    CARD = _MIXED_CARD

    def test_a_file_that_failed_verification_is_not_deletable(self) -> None:
        """A perfect transfer of a corrupt file must not become deletable.

        The truncated JPEG is copied off the card byte-for-byte — the backend's
        transfer genuinely succeeds — and is rejected only by the engine's
        re-read of the destination. This is the everyday case the gate exists
        for, and it is run end to end through the real backend.
        """
        report = self.rescue()

        good = self.outcome_for(report, "/DCIM/118CANON/MVI_0001.AVI")
        bad = self.outcome_for(report, "/DCIM/118CANON/IMG_0002.JPG")

        # The bad file did land on disk: this is a verification failure, not a
        # transfer failure, and the gate must catch it all the same.
        self.assertTrue(bad.dest_path and os.path.isfile(bad.dest_path))
        self.assertIsNotNone(bad.verify)
        self.assertFalse(bad.verify.ok)
        self.assertFalse(bad.ok)

        self.assertEqual([f.path for f in report.deletable], [good.file.path])
        self.assertFalse(
            report.all_verified,
            "all_verified must be False while any file in the run failed",
        )
        self.assertEqual(report.failed_count, 1)

    def test_an_outcome_forged_ok_without_evidence_is_not_deletable(self) -> None:
        """``ok=True`` alone buys nothing: the gate wants the evidence itself.

        Both forgeries below are states no honest run produces, and both are one
        careless refactor away. ``deletable`` re-derives membership instead of
        trusting the flag, so both drop out silently rather than joining the
        deletion set.
        """
        camera_file, dest_path = self.local_copy()

        no_verify_result = DownloadOutcome(
            file=camera_file, dest_path=dest_path, ok=True, verify=None
        )
        no_local_copy = DownloadOutcome(
            file=camera_file, dest_path=None, ok=True, verify=VerifyResult(ok=True)
        )

        for forged in (no_verify_result, no_local_copy):
            report = TransferReport(outcomes=[forged], dest_dir=self.dest)
            self.assertEqual(
                report.deletable,
                [],
                "an outcome with ok=True but no %s must not be deletable"
                % ("VerifyResult" if forged.verify is None else "dest_path"),
            )
            self.assertEqual(report.verified_outcomes(), [])

    def test_an_outcome_whose_verify_result_says_not_ok_is_not_deletable(self) -> None:
        """``ok=True`` next to a failed VerifyResult is a contradiction; the
        gate resolves it in the only safe direction."""
        camera_file, dest_path = self.local_copy()
        contradictory = DownloadOutcome(
            file=camera_file,
            dest_path=dest_path,
            ok=True,
            verify=VerifyResult(ok=False, reason="truncated: 12 of 1012 bytes"),
        )

        report = TransferReport(outcomes=[contradictory], dest_dir=self.dest)

        self.assertEqual(report.deletable, [])
        self.assertEqual(report.verified_outcomes(), [])


# --------------------------------------------------------------------------- #
# 2. What the gate refuses outright
# --------------------------------------------------------------------------- #


class GateRefusalTest(_GateCase):
    """``delete_verified`` refuses before it ever reaches the backend."""

    def test_an_empty_report_is_refused_and_no_backend_call_is_made(self) -> None:
        """Nothing requested proves nothing. An empty run must not authorise an
        erase, which is also why ``all_verified`` is False for it."""
        backend = _RecordingBackend()
        report = TransferReport(outcomes=[], dest_dir=self.dest)

        self.assertFalse(report.all_verified)
        with self.assertRaises(ValueError):
            self.gate(backend).delete_verified(report, self.progress)

        self.assertEqual(backend.batches, [], "the backend must not have been called")

    def test_a_report_where_nothing_verified_is_refused(self) -> None:
        """A run in which every file failed is refused as a whole, rather than
        being allowed through with an empty deletion set."""
        backend = _RecordingBackend()
        camera_file, dest_path = self.local_copy()
        failed = DownloadOutcome(
            file=camera_file,
            dest_path=dest_path,
            ok=False,
            verify=VerifyResult(ok=False, reason="JPEG end marker FFD9 missing"),
            error="JPEG end marker FFD9 missing",
        )
        report = TransferReport(outcomes=[failed], dest_dir=self.dest)

        with self.assertRaises(ValueError):
            self.gate(backend).delete_verified(report, self.progress)

        self.assertEqual(backend.batches, [])

    def test_a_duck_typed_report_impostor_is_refused_by_type(self) -> None:
        """The gate accepts the real report type and nothing else.

        Recorded as TypeError because that is what the code raises today; if the
        product ever changes it to CameraError this test must be updated
        deliberately, not by accident.
        """
        backend = _RecordingBackend()
        impostor = _ImpostorReport([self.local_copy()[0]])
        # The impostor really would authorise a deletion if it were trusted.
        self.assertEqual(len(impostor.deletable), 1)

        engine = self.gate(backend)
        with self.assertRaises(TypeError) as caught:
            engine.delete_verified(impostor, self.progress)  # type: ignore[arg-type]

        self.assertIn("TransferReport", str(caught.exception))
        self.assertEqual(backend.batches, [])

    def test_a_backend_that_cannot_delete_raises_instead_of_trying(self) -> None:
        """A write-protected card or a read-only driver stops the erase here,
        with a message, rather than half-way through the card."""
        backend = _RecordingBackend(can_delete=False)
        report = self.rescue()
        self.assertTrue(report.deletable, "premise: this report authorises an erase")

        with self.assertRaises(CameraError):
            self.gate(backend).delete_verified(report, self.progress)

        self.assertEqual(backend.batches, [])


# --------------------------------------------------------------------------- #
# 3. What actually reaches the backend
# --------------------------------------------------------------------------- #


class HandoverTest(_GateCase):
    """Exactly ``report.deletable`` reaches ``backend.delete()``. No more."""

    def test_the_backend_receives_exactly_the_deletable_files(self) -> None:
        """Object identity, not equality: on a card where two folders hold the
        same base name, equality cannot tell the two photographs apart."""
        backend = _RecordingBackend()
        report = self.rescue()
        self.assertEqual(len(report.deletable), 3, "premise: all three verified")

        results = self.gate(backend).delete_verified(report, self.progress)

        self.assertEqual(
            [id(f) for f in backend.handed],
            [id(f) for f in report.deletable],
            "the gate must hand over the verified CameraFile objects themselves",
        )
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r.ok for r in results))

    def test_the_gate_hands_over_individual_files_never_folders(self) -> None:
        """Deletion is per file. A folder must never be handed to the backend,
        even when every file inside it was verified — an unexpected listing
        mismatch must not be able to escalate into total loss."""
        backend = _RecordingBackend()
        report = self.rescue()

        self.gate(backend).delete_verified(report, self.progress)

        handed = backend.handed
        self.assertEqual(len(handed), len(report.deletable))
        for entry in handed:
            self.assertIsInstance(entry, CameraFile)
            # Every entry names a real file on the card, not a directory.
            self.assertTrue(
                os.path.isfile(self.card_path(entry.path)),
                "%s is not a file on the card" % entry.path,
            )

    def test_a_backend_reporting_an_unrequested_erase_raises(self) -> None:
        """The post-condition fires loudly instead of widening.

        A deletion cannot be undone, so the only thing left is to tell the user
        in the strongest terms. The offending path must appear in the message —
        it is interpolated, so this assertion survives translation.
        """
        stray = CameraFile(folder="/DCIM/999OTHER", name="IMG_9999.JPG", size=4242)
        backend = _RecordingBackend(extra=stray)
        report = self.rescue()

        with self.assertRaises(CameraError) as caught:
            self.gate(backend).delete_verified(report, self.progress)

        self.assertIn(stray.path, str(caught.exception))

    def test_a_verified_copy_that_vanished_is_kept_on_the_camera(self) -> None:
        """Verification happened minutes ago; the gate re-stats the local copy
        immediately before erasing. A copy the user moved or deleted meanwhile
        must drop out of the deletion set, not ride in on its old verdict."""
        backend = _RecordingBackend()
        report = self.rescue()
        vanished = self.outcome_for(report, "/DCIM/119CANON/MVI_0001.AVI")
        os.remove(vanished.dest_path)

        results = self.gate(backend).delete_verified(report, self.progress)

        handed = backend.handed
        self.assertEqual(len(handed), 2)
        self.assertNotIn(
            vanished.file.path,
            [f.path for f in handed],
            "a file with no local copy must never be handed to delete()",
        )
        refused = [r for r in results if r.file.path == vanished.file.path]
        self.assertEqual(len(refused), 1)
        self.assertFalse(refused[0].ok)
        self.assertTrue(refused[0].error, "the refusal must carry a reason")

    def test_a_verified_copy_truncated_to_zero_bytes_is_kept_on_the_camera(
        self,
    ) -> None:
        """A local copy that still exists but is now empty must not authorise an
        erase.

        This is not a contrived state: a destination that filled up, a cloud-sync
        client that replaced the file with a placeholder, or a crash during a
        later copy all leave a zero-byte file with the right name sitting exactly
        where the verified one was. ``os.path.isfile`` still says yes, so only an
        explicit size check separates "my photo is here" from "my photo is gone".
        """
        backend = _RecordingBackend()
        report = self.rescue()
        emptied = self.outcome_for(report, "/DCIM/119CANON/MVI_0001.AVI")
        with open(emptied.dest_path, "wb"):
            pass  # truncate in place: the name survives, the bytes do not
        self.assertEqual(os.path.getsize(emptied.dest_path), 0)

        results = self.gate(backend).delete_verified(report, self.progress)

        self.assertNotIn(
            emptied.file.path,
            [f.path for f in backend.handed],
            "an empty local copy must never authorise erasing the original",
        )
        refused = [r for r in results if r.file.path == emptied.file.path]
        self.assertEqual(len(refused), 1)
        self.assertFalse(refused[0].ok)
        self.assertTrue(refused[0].error, "the refusal must carry a reason")
        # The photograph is still on the card, which is the point of refusing.
        self.assertTrue(os.path.isfile(self.card_path(emptied.file.path)))

    def test_an_empty_local_copy_of_unknown_size_is_kept_on_the_camera(self) -> None:
        """The zero-byte branch of the re-stat, isolated from the size check.

        When the camera never reported a size — ``size == -1``, which is exactly
        what a WIA driver that exposes no length produces — there is nothing to
        compare the local file against, and an empty copy passes every other
        check in the gate: it exists, it is readable, and it carries a verified
        verdict from minutes ago. Only the explicit zero-byte test refuses it.

        The state is reachable in the field: the copy verified fine, and a
        cloud-sync client then replaced it with a zero-byte placeholder before
        the user pressed Delete. Erasing the camera's copy at that point loses
        the photograph for good.

        The outcome is hand-built because a real run cannot produce it — the
        point is precisely that verification is already behind us and the world
        changed afterwards.
        """
        backend = _RecordingBackend()
        camera_file, dest_path = self.local_copy()
        unknown = CameraFile(
            folder=camera_file.folder, name=camera_file.name, size=-1, mtime=1.0
        )
        self.assertFalse(unknown.size_known, "premise: the camera reported no size")
        report = TransferReport(
            outcomes=[
                DownloadOutcome(
                    file=unknown,
                    dest_path=dest_path,
                    ok=True,
                    verify=VerifyResult(ok=True),
                )
            ],
            dest_dir=self.dest,
        )
        self.assertEqual(
            len(report.deletable), 1, "premise: this report authorises an erase"
        )

        with open(dest_path, "wb"):
            pass  # the placeholder replaces the photo, keeping its name

        results = self.gate(backend).delete_verified(report, self.progress)

        # Nothing survived the re-stat, so delete() is never reached at all.
        self.assertEqual(
            backend.batches, [], "an empty local copy authorised erasing the original"
        )
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertTrue(results[0].error, "the refusal must carry a reason")

    def test_a_verified_copy_that_changed_size_is_kept_on_the_camera(self) -> None:
        """The copy was verified minutes ago and has since been rewritten.

        Non-empty and readable, so the two cheaper checks pass; only comparing
        against the size the camera reported catches it. Something overwrote the
        rescue between the check and the erase, and whatever is there now is not
        the photograph that was verified — so the camera's copy is the only one
        left and must stay.
        """
        backend = _RecordingBackend()
        report = self.rescue()
        changed = self.outcome_for(report, "/DCIM/119CANON/MVI_0001.AVI")
        self.assertTrue(changed.file.size_known, "premise: the size is known")
        with open(changed.dest_path, "wb") as handle:
            handle.write(tiny_jpeg(changed.file.size + 128))
        self.assertNotEqual(os.path.getsize(changed.dest_path), changed.file.size)

        results = self.gate(backend).delete_verified(report, self.progress)

        self.assertNotIn(
            changed.file.path,
            [f.path for f in backend.handed],
            "a local copy that changed since verification must not authorise an erase",
        )
        refused = [r for r in results if r.file.path == changed.file.path]
        self.assertEqual(len(refused), 1)
        self.assertFalse(refused[0].ok)
        self.assertTrue(refused[0].error, "the refusal must carry a reason")
        self.assertTrue(os.path.isfile(self.card_path(changed.file.path)))

    def test_a_vanished_local_copy_of_unknown_size_is_kept_on_the_camera(self) -> None:
        """The *missing-file* branch of the re-stat, isolated from the size check.

        ``test_a_verified_copy_that_vanished_is_kept_on_the_camera`` above covers
        the same user-visible outcome, but it cannot reach this branch: its files
        come off a real card, so their sizes are known, and a vanished copy is
        already refused one check later by ``size != outcome.file.size`` (``None``
        never equals a real byte count). Delete the "the local copy is missing"
        branch and that test still passes. This one does not.

        The branch is load-bearing exactly when the camera reported no size —
        ``size == -1``, which is what a WIA driver that exposes no length
        produces, and what ``FakeWiaFile(report_size=-1)`` exists to reproduce.
        With the size unknown there is nothing left to compare against, so this
        is the only check standing between a folder the user moved after
        verification and an erase that leaves the photograph nowhere at all.

        Reachable in the field, and unremarkable when it happens: the rescue
        verified, the user dragged the destination folder to an external drive
        (or a sync client moved it) while the confirmation dialog was still open,
        and then pressed Delete.
        """
        backend = _RecordingBackend()
        camera_file, dest_path = self.local_copy()
        unknown = CameraFile(
            folder=camera_file.folder, name=camera_file.name, size=-1, mtime=1.0
        )
        self.assertFalse(unknown.size_known, "premise: the camera reported no size")
        report = TransferReport(
            outcomes=[
                DownloadOutcome(
                    file=unknown,
                    dest_path=dest_path,
                    ok=True,
                    verify=VerifyResult(ok=True),
                )
            ],
            dest_dir=self.dest,
        )
        self.assertEqual(
            len(report.deletable), 1, "premise: this report authorises an erase"
        )

        os.remove(dest_path)  # the whole folder went somewhere else
        self.assertFalse(os.path.exists(dest_path))

        results = self.gate(backend).delete_verified(report, self.progress)

        # Nothing survived the re-stat, so delete() is never reached at all.
        self.assertEqual(
            backend.batches,
            [],
            "a local copy that no longer exists authorised erasing the original",
        )
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertTrue(results[0].error, "the refusal must carry a reason")


# --------------------------------------------------------------------------- #
# 4. Partial runs: the documented behaviour, pinned
# --------------------------------------------------------------------------- #


class PartialReportTest(_GateCase):
    """``failed_count > 0`` does not block the erase; it narrows it."""

    CARD = _MIXED_CARD

    def test_only_the_verified_subset_leaves_the_card(self) -> None:
        """Documented behaviour, pinned: the gate is applied per file, not per
        run, so a partial rescue still erases what it proved — and the file that
        failed is still on the card afterwards, which is the whole point.

        This runs against the real MassStorageBackend, so the assertion about
        what is left on the card is an assertion about the filesystem.
        """
        report = self.rescue()
        self.assertEqual(report.failed_count, 1, "premise: a partial report")
        self.assertEqual(len(report.deletable), 1)

        good_path = "/DCIM/118CANON/MVI_0001.AVI"
        bad_path = "/DCIM/118CANON/IMG_0002.JPG"

        results = self.gate(MassStorageBackend()).delete_verified(report, self.progress)

        self.assertEqual([r.file.path for r in results], [good_path])
        self.assertTrue(results[0].ok)
        self.assertFalse(
            os.path.exists(self.card_path(good_path)),
            "the verified file should have been erased from the card",
        )
        self.assertTrue(
            os.path.isfile(self.card_path(bad_path)),
            "the file that failed verification MUST still be on the card",
        )


class SkippedFileTest(_GateCase):
    """A file the backend skipped is re-read from disk like every other one."""

    CARD = _ONE_FILE_CARD

    def test_a_same_sized_impostor_at_the_destination_is_not_deletable(self) -> None:
        """A file already sitting at the destination is not evidence of anything.

        A previous run left a file of exactly the right name, size and timestamp,
        so the backend legitimately skips the transfer — but its bytes are
        rubbish. The engine re-verifies skipped files like any other, so the
        photograph stays on the card and the gate refuses the run outright.
        Half-finished 2003 transfers leave exactly this state behind.
        """
        device_path = "/DCIM/118CANON/MVI_0001.AVI"
        source = self.card_path(device_path)
        real_bytes = self.contents[device_path]
        # Same name, same size, same mtime — everything a resume check looks at —
        # but not the same file.
        impostor = self.write_dest("MVI_0001.AVI", not_a_jpeg(len(real_bytes)))
        source_stat = os.stat(source)
        os.utime(impostor, (source_stat.st_mtime, source_stat.st_mtime))

        report = self.rescue()

        self.assertEqual(report.skipped_count, 1, "premise: the backend skipped it")
        self.assertEqual(report.deletable, [])
        with self.assertRaises(ValueError):
            self.gate(_RecordingBackend()).delete_verified(report, self.progress)
        self.assertTrue(
            os.path.isfile(source), "the original must still be on the card"
        )


# --------------------------------------------------------------------------- #
# 5. Duplicate device paths (review finding F5)
# --------------------------------------------------------------------------- #


class DuplicateDevicePathTest(_GateCase):
    """Two photographs, one device path. The failed one must not ride along.

    A WIA driver that flattens the camera's tree reports ``/IMG_0001.JPG`` for
    two pictures taken a year apart, so a :class:`CameraFile` is the identity of
    a *path*, not of a photograph. Selecting the deletion set by path — or
    asserting membership with ``==`` — would let the picture that failed
    verification be erased on the strength of its verified namesake's evidence.
    """

    def test_a_failed_twin_is_not_erased_on_its_namesakes_evidence(self) -> None:
        folder = "/DCIM/100WIA"
        name = "IMG_0001.JPG"
        data = tiny_jpeg(400)
        dest_path = self.write_dest(name, data)

        # Deliberately indistinguishable by value: same folder, name, size and
        # timestamp. Only ``raw`` differs, and CameraFile excludes it from
        # comparison, so these two are ``==`` and must still never be confused.
        verified_file = CameraFile(
            folder=folder, name=name, size=len(data), mtime=1.0, raw={"which": "good"}
        )
        failed_file = CameraFile(
            folder=folder, name=name, size=len(data), mtime=1.0, raw={"which": "bad"}
        )
        self.assertEqual(verified_file, failed_file, "premise: equal by value")
        self.assertIsNot(verified_file, failed_file)
        self.assertEqual(verified_file.path, failed_file.path)

        # The failed twin's bytes DID land on disk, under the second name
        # ``safe_dest_path`` hands out on a collision, and the local copy is
        # exactly the right length. That matters: the gate's last-moment re-stat
        # would accept this file happily, so it cannot be what saves the
        # photograph here. The only thing keeping it on the camera is the failed
        # VerifyResult — which is precisely the property under test, isolated
        # from the second line of defence.
        failed_dest = self.write_dest("100WIA_" + name, not_a_jpeg(len(data)))
        self.assertEqual(os.path.getsize(failed_dest), len(data))

        # The failed one comes first, so a path-keyed or order-dependent
        # implementation would pick the wrong object.
        failed = DownloadOutcome(
            file=failed_file,
            dest_path=failed_dest,
            ok=False,
            verify=VerifyResult(ok=False, reason="not a JPEG: start marker missing"),
            error="not a JPEG: start marker missing",
        )
        verified = DownloadOutcome(
            file=verified_file,
            dest_path=dest_path,
            ok=True,
            verify=VerifyResult(ok=True),
        )
        report = TransferReport(outcomes=[failed, verified], dest_dir=self.dest)

        self.assertEqual(len(report.deletable), 1)
        self.assertIs(report.deletable[0], verified_file)

        backend = _RecordingBackend()
        self.gate(backend).delete_verified(report, self.progress)

        handed = backend.handed
        self.assertEqual(len(handed), 1, "one copy on disk justifies one deletion")
        self.assertIs(
            handed[0],
            verified_file,
            "the gate must hand over the verified object itself, not an equal one",
        )
        self.assertFalse(any(f is failed_file for f in handed))


# --------------------------------------------------------------------------- #
# 6. Blast radius: what deletion must leave alone
# --------------------------------------------------------------------------- #


class BlastRadiusTest(_GateCase):
    """Erasing files erases files: not folders, and never the local copies."""

    def test_the_cards_folder_structure_survives_the_erase(self) -> None:
        """The camera expects its DCIM folders to exist; the contract forbids
        removing them, even when every file inside was erased."""
        report = self.rescue()
        engine = self.gate(MassStorageBackend())

        results = engine.delete_verified(report, self.progress)

        self.assertEqual(len(results), 3)
        self.assertTrue(all(r.ok for r in results))
        for device_path in self.contents:
            self.assertFalse(
                os.path.exists(self.card_path(device_path)),
                "%s should have been erased" % device_path,
            )
        for folder in ("DCIM", "DCIM/118CANON", "DCIM/119CANON"):
            self.assertTrue(
                os.path.isdir(os.path.join(self.card_root, *folder.split("/"))),
                "%s must still exist on the card" % folder,
            )

    def test_the_local_copies_are_untouched_by_the_erase(self) -> None:
        """Deleting from the camera must not disturb the rescue itself — and
        each local copy must still hold the bytes of the photograph it came
        from, which is the duplicate-name case again, checked on disk."""
        report = self.rescue()
        before = {o.file.path: (o.dest_path, sha(o.dest_path)) for o in report.outcomes}

        self.gate(MassStorageBackend()).delete_verified(report, self.progress)

        for device_path, (dest_path, digest) in before.items():
            self.assertTrue(os.path.isfile(dest_path), "%s vanished" % dest_path)
            self.assertEqual(digest, sha(dest_path), "%s changed" % dest_path)
            with open(dest_path, "rb") as handle:
                self.assertEqual(
                    handle.read(),
                    self.contents[device_path],
                    "%s no longer holds the bytes of the photograph it came from"
                    % dest_path,
                )


if __name__ == "__main__":  # pragma: no cover - convenience for a single file
    unittest.main()
