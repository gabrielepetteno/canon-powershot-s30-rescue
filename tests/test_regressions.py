"""Permanent guards for the defects an adversarial safety review found.

Every finding below was a way to reach the same end state — a photograph that
exists nowhere but on the card is reported as rescued, and the card copy is
then erased. Each was fixed, and each fix is a few lines that look optional to
anyone reading the code without this file next to it: a ``claimed`` set passed
into a helper, an extra ``and``, a comparison by identity rather than by path.
This module exists so that removing one of those lines turns red instead of
turning quiet.

Every test therefore reproduces the **original failing scenario** and asserts
the corrected behaviour. Each docstring names its finding (F1 … F7) and states
in one line what used to go wrong; the test method's name states the property
that must hold forever.

    F1  a cancelled run credited one copy on disk to two photographs
    F2  gphoto2 resume matched a file against the copy another file just wrote,
        and skipped on name+size alone when the camera reported no timestamp
    F3  WIA resume matched on size alone, with no per-batch claim tracking
    F4  a card swapped in the reader was deleted by path, without re-checking
        that the file at that path is still the one that was listed
    F5  the delete gate selected by ``CameraFile.path``, which names a *file
        name*, not a photograph
    F6  a destination folder on the card itself: every step correct, total loss
    F7  ``progress=None`` crashed with a message blaming the camera

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests

Stdlib only: no pytest, no camera, no network, no Pillow, no admin rights.

Why the card holds AVI clips and not the JPEGs of ``helpers.DEFAULT_CARD``
--------------------------------------------------------------------------
:class:`~retrocam.transfer.TransferEngine` always verifies with ``deep=True``,
and ``helpers.tiny_jpeg`` is structurally valid but deliberately not decodable.
On a machine that happens to have Pillow installed, every JPEG here would fail
the decode step, no outcome would ever be verified, and the tests that must
reach the delete gate could not reach it — they would pass or fail depending on
an optional dependency. ``.avi`` is not in ``verify.py``'s strict-decode set, so
these clips verify identically with and without Pillow. The layout still mirrors
``DEFAULT_CARD`` where it matters: the same base name in two folders, which is
what a Canon writes once its frame counter rolls over, and which is the case
that broke three backends during review. A PowerShot of that era really does
write ``MVI_xxxx.AVI`` next to its stills, so this is a card, not a contrivance.

The two backend-level tests (F2, F4) never run verification, so their card
content is irrelevant to the assertion; they use the same clips purely so the
whole file speaks one language.
"""

from __future__ import annotations

import hashlib
import os
import sys
import unittest
from typing import Dict, List, Optional, Sequence

# Discovery puts ``tests/`` on sys.path; being explicit lets this file also be
# run when pointed at directly from another directory.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from helpers import (  # noqa: E402
    CardSpec,
    FakeGphoto2,
    FakeWiaDevice,
    FakeWiaFile,
    FakeWiaFolder,
    RecordingProgress,
    TempDirCase,
    fake_wia,
    mass_storage_camera,
    riff_avi,
    sha,
)

from retrocam import i18n  # noqa: E402
from retrocam.backends import wia as wia_mod  # noqa: E402
from retrocam.backends.base import (  # noqa: E402
    Availability,
    CameraBackend,
    noop_progress,
)
from retrocam.backends.gphoto2_backend import GPhoto2Backend  # noqa: E402
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
    ProgressCallback,
    VerifyResult,
)
from retrocam.transfer import TransferEngine, TransferReport  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def clip(payload: int = 1000, marker: int = 0x11) -> bytes:
    """A RIFF/AVI clip of an exact size whose body says which clip it is.

    ``helpers.riff_avi`` fills its body with NULs, so two clips of equal length
    are byte-identical and no test could tell which of them landed on disk —
    which is precisely the distinction every finding here turns on. Overwriting
    the body leaves the 12-byte RIFF header (and therefore the size and the
    structural check) exactly as the helper built it, while making each clip
    recognisable.
    """
    data = bytearray(riff_avi(payload))
    data[12:] = bytes([marker]) * (len(data) - 12)
    return bytes(data)


def digest(data: bytes) -> str:
    """Content digest of a byte string, comparable with :func:`helpers.sha`."""
    return hashlib.sha256(data).hexdigest()[:16]


#: Two different recordings that share a base name **and a byte count**. Name
#: plus size is then not enough to tell them apart, and every resume/recovery
#: shortcut in the program has to survive that.
CLIP_A = clip(1000, 0x11)  # /DCIM/118CANON/MVI_0001.AVI
CLIP_B = clip(1000, 0x99)  # /DCIM/119CANON/MVI_0001.AVI

#: The Canon frame-counter rollover, as a card on disk.
TWIN_CARD: CardSpec = {
    "118CANON": {"MVI_0001.AVI": CLIP_A},
    "119CANON": {"MVI_0001.AVI": CLIP_B},
}

#: A clip that is **not** on the card: it is already sitting in the destination
#: folder when the run starts, left there by an unrelated earlier session. Same
#: length as ``CLIP_B``, different content — so name and size alone cannot tell
#: it apart from the photograph the run was about to fetch.
CLIP_STRANGER = clip(1000, 0x55)

#: Two clips with **different** base names, so nothing about this card is
#: ambiguous on its own. The trap is entirely in the destination folder.
STRANGER_CARD: CardSpec = {
    "118CANON": {"MVI_0001.AVI": CLIP_A},
    "119CANON": {"MVI_0002.AVI": CLIP_B},
}


class _CancelAfterFirstFile:
    """Progress sink that cancels the run once the first file is on disk.

    Cancelling from a progress tick rather than from a timer keeps the test
    deterministic: engine and backend are single-threaded here, so the token is
    always set at the same instruction — after file 0 has been renamed into
    place and before file 1 is read.
    """

    def __init__(self, token: CancelToken) -> None:
        self.token = token
        self.ticks: List[Progress] = []

    def __call__(self, tick: Progress) -> None:
        self.ticks.append(tick)
        # The post-copy tick of the first file: bytes_done only becomes non-zero
        # after os.replace has given the copy its final name.
        if tick.phase == "download" and tick.index == 0 and tick.bytes_done > 0:
            self.token.cancel()


class _RecordingDeleteBackend(CameraBackend):
    """Records what the delete gate hands it, and erases nothing.

    Erasing nothing is deliberate: F5 is about *which photograph* reached
    ``delete()``, which is exactly and only what the gate controls. The files
    are recorded as the very objects they were handed, because two different
    photographs compare equal when a driver reports the same name and size for
    both — an assertion written with ``==`` would pass while the wrong picture
    was being erased.
    """

    kind = BackendKind.WIA
    display_name = "recording test backend"

    def __init__(self) -> None:
        self.batches: List[List[CameraFile]] = []

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
        raise AssertionError("this backend is only ever driven through the gate")

    def delete(
        self,
        camera: CameraInfo,
        files: Sequence[CameraFile],
        progress: ProgressCallback = noop_progress,
        cancel: Optional[CancelToken] = None,
    ) -> List[DeleteOutcome]:
        self.batches.append(list(files))
        return [DeleteOutcome(file=f, ok=True) for f in files]


class RegressionCase(TempDirCase):
    """Temp directory, a pinned language, and a few shared assertions."""

    def setUp(self) -> None:
        super().setUp()
        # Several assertions read a user-facing message. Pinning the language
        # keeps them from depending on the developer's system locale.
        previous = i18n.current_language()
        i18n.set_language("en")
        self.addCleanup(i18n.set_language, previous)

    def dest(self, name: str = "out") -> str:
        """An empty destination directory outside the card."""
        path = self.path(name)
        os.makedirs(path, exist_ok=True)
        return path

    def snapshot(self, root: str) -> Dict[str, str]:
        """``{relative path: content digest}`` for a whole tree.

        Used to prove that a refusal happened *before* anything was written:
        comparing digests catches a new file, a removed file and a rewritten
        one, which "the folder still has three entries" would not.
        """
        out: Dict[str, str] = {}
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                full = os.path.join(dirpath, name)
                out[os.path.relpath(full, root)] = sha(full)
        return out


# --------------------------------------------------------------------------- #
# F1 - one copy on disk is evidence for exactly one photograph
# --------------------------------------------------------------------------- #


class AbortRecoveryRegressionTest(RegressionCase):
    """:meth:`TransferEngine._recover_after_abort` / ``_find_recovered_dest``."""

    def test_a_cancelled_run_credits_one_copy_to_exactly_one_photograph(self) -> None:
        """F1 - rebuilding outcomes from disk after a cancellation used to credit
        the same copy to both photographs that share a base name, so a photo
        whose bytes were never written became verified and deletable.

        The card holds ``/DCIM/118CANON/MVI_0001.AVI`` and
        ``/DCIM/119CANON/MVI_0001.AVI`` with **equal byte counts**. The run is
        cancelled after the first file, so exactly one copy exists on disk under
        the flat name ``MVI_0001.AVI``. Without the per-run ``credited`` set,
        the second file finds that copy, matches it on name and size, verifies
        it (it is a complete clip of exactly the right length) and is reported
        as rescued — while its own bytes exist nowhere but on the card.
        """
        card_root, payloads = self.make_card(TWIN_CARD)
        camera = mass_storage_camera(card_root)
        backend = MassStorageBackend()
        engine = TransferEngine(backend, camera)
        dest = self.dest()

        files = engine.list_files()
        self.assertEqual(
            [f.path for f in files],
            ["/DCIM/118CANON/MVI_0001.AVI", "/DCIM/119CANON/MVI_0001.AVI"],
        )
        first, second = files
        self.assertEqual(first.size, second.size, "the fixture must arm the trap")

        token = CancelToken()
        report = engine.download(files, dest, _CancelAfterFirstFile(token), token)

        self.assertTrue(report.aborted)

        # Exactly one copy reached the disk, and it is the first photograph's.
        self.assertEqual(sorted(os.listdir(dest)), ["MVI_0001.AVI"])
        written = os.path.join(dest, "MVI_0001.AVI")
        self.assertEqual(sha(written), digest(payloads[first.path]))

        # The second photograph must be reported as not transferred...
        self.assertEqual([o.ok for o in report.outcomes], [True, False])
        self.assertIsNone(report.outcomes[1].dest_path)
        self.assertFalse(report.all_verified, "a partial run is not a verified run")

        # ...and, above all, must not be deletable.
        self.assertEqual(len(report.deletable), 1)
        self.assertIs(report.deletable[0], first)
        self.assertNotIn(id(second), {id(f) for f in report.deletable})

        # The property behind the assertion above, stated directly: nothing on
        # disk holds the second photograph's bytes, so nothing could justify
        # erasing it from the card.
        on_disk = {sha(os.path.join(dest, name)) for name in os.listdir(dest)}
        self.assertNotIn(digest(payloads[second.path]), on_disk)

    def test_a_file_already_in_the_destination_is_never_credited_to_a_photo(
        self,
    ) -> None:
        """F1b - the other half of ``_find_recovered_dest``'s guard: a name that
        was in the destination *before* the run started is not evidence that
        this run rescued anything.

        The destination already holds ``MVI_0002.AVI`` from an unrelated
        session, the same length as the card's ``/DCIM/119CANON/MVI_0002.AVI``
        but a different recording. The run is cancelled after the first file, so
        the second is never transferred. Rebuilding outcomes from disk, the
        second file finds a file at exactly the name ``safe_dest_path`` would
        have given it, of exactly the right size, which passes verification —
        because it is a perfectly valid clip. It is simply somebody else's.

        Without the ``pre_existing`` snapshot the engine credits it, reports the
        photograph as rescued and verified, and offers it to the delete gate,
        which re-stats the stranger's file, finds it intact, and erases the only
        copy of the real one from the card.
        """
        card_root, payloads = self.make_card(STRANGER_CARD)
        camera = mass_storage_camera(card_root)
        engine = TransferEngine(MassStorageBackend(), camera)
        dest = self.dest()

        # The debris from the earlier session, in place before anything runs.
        stranger = os.path.join(dest, "MVI_0002.AVI")
        with open(stranger, "wb") as handle:
            handle.write(CLIP_STRANGER)

        files = engine.list_files()
        self.assertEqual(
            [f.path for f in files],
            ["/DCIM/118CANON/MVI_0001.AVI", "/DCIM/119CANON/MVI_0002.AVI"],
        )
        first, second = files
        self.assertEqual(
            len(CLIP_STRANGER), second.size, "the fixture must arm the trap"
        )

        token = CancelToken()
        report = engine.download(files, dest, _CancelAfterFirstFile(token), token)

        self.assertTrue(report.aborted)

        # The first photograph was genuinely transferred and may be erased.
        self.assertTrue(report.outcomes[0].ok)
        self.assertEqual(len(report.deletable), 1)
        self.assertIs(report.deletable[0], first)

        # The second was not. The stranger's file must not stand in for it.
        self.assertFalse(
            report.outcomes[1].ok,
            "a file that was already in the destination was credited to a photo "
            "this run never transferred",
        )
        self.assertIsNone(report.outcomes[1].dest_path)
        self.assertNotIn(id(second), {id(f) for f in report.deletable})

        # The stranger's file is also left exactly as it was found: the run must
        # neither claim it nor overwrite it.
        self.assertEqual(sha(stranger), digest(CLIP_STRANGER))

        # And the property behind all of the above: the second photograph's
        # bytes are nowhere on this computer, so nothing could justify erasing
        # it from the card.
        on_disk = {sha(os.path.join(dest, name)) for name in os.listdir(dest)}
        self.assertNotIn(digest(payloads[second.path]), on_disk)


# --------------------------------------------------------------------------- #
# F2 - the gphoto2 resume check
# --------------------------------------------------------------------------- #


class Gphoto2ResumeSkipRegressionTest(RegressionCase):
    """:meth:`GPhoto2Backend._maybe_skip`.

    F2 covers two independent defects in one method, so it gets two tests: they
    need different state and either one could be reintroduced without the other.
    """

    #: What ``--parsable -L`` reports for a card whose clock was reset, so every
    #: file carries the same timestamp. ``helpers.FakeGphoto2`` prints exactly
    #: this value for every file.
    CAMERA_MTIME = 1700000000.0

    def camera(self, gp: FakeGphoto2) -> CameraInfo:
        """The :class:`CameraInfo` ``detect`` would have produced for ``gp``."""
        return CameraInfo(
            model=gp.model,
            port=gp.port,
            kind=BackendKind.GPHOTO2,
            detail=gp.port,
            raw={"backend": "gphoto2"},
        )

    def listed(self, folder: str, name: str, data: bytes) -> CameraFile:
        """A :class:`CameraFile` shaped exactly as the parsable listing yields.

        Built by hand rather than by calling ``list_files``: the shared
        ``FakeGphoto2`` prints its ``--parsable`` output as one ``KEY=value``
        line per field, which is not the single-line format the shipped parser
        accepts, so the backend would silently fall through to the human
        listing and report ``size=-1`` — and an unknown size disables the resume
        check being tested here before it can be reached.
        """
        return CameraFile(
            folder=folder,
            name=name,
            size=len(data),
            mtime=self.CAMERA_MTIME,
            raw={"source": "parsable"},
        )

    def test_two_photos_in_one_batch_never_share_a_resumed_copy(self) -> None:
        """F2 - with no per-batch ``claimed`` set, the second file matched the
        copy the first file had just been credited with: same name, same size,
        same camera timestamp, entirely different picture.

        The run resumes an interrupted rescue, which is the state that makes
        this reachable: ``MVI_0001.AVI`` is already at the destination from an
        earlier run, carrying the camera's timestamp exactly as gphoto2 stamps
        it. The first file legitimately skips onto that copy. The second file —
        a different clip in ``119CANON``, of the same length and the same
        timestamp — must **not**, or the engine would verify the first clip's
        bytes and green-light erasing the second from the camera.
        """
        dest = self.dest()
        # The copy an earlier, interrupted run left behind.
        resumed = os.path.join(dest, "MVI_0001.AVI")
        with open(resumed, "wb") as handle:
            handle.write(CLIP_A)
        os.utime(resumed, (self.CAMERA_MTIME, self.CAMERA_MTIME))

        first = self.listed("/DCIM/118CANON", "MVI_0001.AVI", CLIP_A)
        second = self.listed("/DCIM/119CANON", "MVI_0001.AVI", CLIP_B)
        self.assertEqual(first.size, second.size, "the fixture must arm the trap")

        with FakeGphoto2(card=TWIN_CARD) as gp:
            backend = GPhoto2Backend()
            outcomes = backend.download(
                self.camera(gp), [first, second], dest, RecordingProgress()
            )
            # Read inside the with-block: FakeGphoto2 deletes its call log on
            # exit and calls() would then answer [] to any later assertion.
            fetches = [c for c in gp.calls() if "--get-file" in c]

        # The first file resumes onto the copy that was already there.
        self.assertTrue(outcomes[0].skipped)
        self.assertEqual(outcomes[0].dest_path, resumed)

        # The second file does not: it is fetched from the camera and gets a
        # destination of its own.
        self.assertFalse(outcomes[1].skipped, "the second clip must be transferred")
        self.assertTrue(outcomes[1].ok, outcomes[1].error)
        self.assertNotEqual(outcomes[1].dest_path, outcomes[0].dest_path)
        self.assertEqual(
            os.path.basename(str(outcomes[1].dest_path)), "119CANON_MVI_0001.AVI"
        )

        # Each copy holds its own recording; the resumed one was not touched.
        self.assertEqual(sha(resumed), digest(CLIP_A))
        self.assertEqual(sha(str(outcomes[1].dest_path)), digest(CLIP_B))

        # And the second clip really did come off the camera.
        self.assertEqual(len(fetches), 1, fetches)
        self.assertIn("/DCIM/119CANON", fetches[0])

    def test_nothing_is_skipped_when_the_camera_reports_no_timestamp(self) -> None:
        """F2 - the timestamp check was skipped when the camera reported no
        mtime, leaving name and size as the only evidence behind an irreversible
        delete.

        Both calls below are made against the same file on disk, with the same
        name and the same exact byte count. The only difference is whether the
        listing carried a timestamp, and that alone must decide: two facts are
        not enough to justify erasing the original.
        """
        dest = self.dest()
        candidate = os.path.join(dest, "MVI_0001.AVI")
        with open(candidate, "wb") as handle:
            handle.write(CLIP_A)
        os.utime(candidate, (self.CAMERA_MTIME, self.CAMERA_MTIME))

        backend = GPhoto2Backend()
        common = {
            "folder": "/DCIM/118CANON",
            "name": "MVI_0001.AVI",
            "size": len(CLIP_A),
        }

        without_time = CameraFile(mtime=None, **common)
        self.assertIsNone(
            backend._maybe_skip(dest, without_time, True, set()),
            "a name and a size are not evidence that this photo was downloaded",
        )

        # The same file, the same copy on disk: only the timestamp was missing
        # above. Without this second assertion the one above could pass for the
        # wrong reason (a fixture that matches nothing proves nothing).
        with_time = CameraFile(mtime=self.CAMERA_MTIME, **common)
        self.assertEqual(
            getattr(
                backend._maybe_skip(dest, with_time, True, set()), "dest_path", None
            ),
            candidate,
        )


# --------------------------------------------------------------------------- #
# F3 - the WIA resume check
# --------------------------------------------------------------------------- #


class WiaResumeSkipRegressionTest(RegressionCase):
    """:meth:`retrocam.backends.wia.WiaBackend._existing_copy`."""

    @staticmethod
    def device() -> FakeWiaDevice:
        """A camera whose two folders hold one base name, as WIA reports it.

        ``Full Item Name`` on a real device is prefixed with a numeric device
        index and a synthetic ``Root``; both are WIA bookkeeping and are
        modelled here as folders so the parser meets the strings it really gets.
        """
        return FakeWiaDevice(
            items=[
                FakeWiaFolder(
                    "0000",
                    [
                        FakeWiaFolder(
                            "Root",
                            [
                                FakeWiaFolder(
                                    "DCIM",
                                    [
                                        FakeWiaFolder(
                                            "118CANON",
                                            [FakeWiaFile("MVI_0001.AVI", CLIP_A)],
                                        ),
                                        FakeWiaFolder(
                                            "119CANON",
                                            [FakeWiaFile("MVI_0001.AVI", CLIP_B)],
                                        ),
                                    ],
                                )
                            ],
                        )
                    ],
                )
            ]
        )

    def test_equal_sized_namesakes_each_keep_their_own_transferred_copy(self) -> None:
        """F3 - ``_existing_copy`` matched on size alone with no claim tracking,
        so the second photo "resumed" onto the copy the first had just written.

        This transport has no timestamps to fall back on — WIA hands over bytes,
        not file metadata — so an unclaimed name plus a size is all the evidence
        there is. Duplicate names reach it by two routes: the frame-counter
        rollover modelled below, and the flat tree the WPD compatibility shim
        reports, where every image hangs off the root. With no per-batch claim
        tracking the second file is reported as already-downloaded, the engine
        verifies the *first* clip's bytes on its behalf, and the second becomes
        deletable from the camera having never been transferred at all.
        """
        dest = self.dest()
        with fake_wia([self.device()]) as state:
            backend = wia_mod.WiaBackend()
            camera = backend.detect()[0]
            files = backend.list_files(camera)
            engine = TransferEngine(backend, camera)
            report = engine.download(files, dest, RecordingProgress())
            transferred = list(state["transfers"])

        self.assertEqual(
            [f.path for f in files],
            ["/DCIM/118CANON/MVI_0001.AVI", "/DCIM/119CANON/MVI_0001.AVI"],
        )
        self.assertEqual(files[0].size, files[1].size, "the fixture must arm the trap")

        # Neither file was skipped: both were genuinely pulled off the camera.
        self.assertFalse(any(o.skipped for o in report.outcomes))
        self.assertEqual(len(transferred), 2, transferred)
        self.assertEqual(len(set(transferred)), 2, "two distinct items, not one twice")

        # Each photograph has a destination of its own, holding its own bytes.
        paths = [str(o.dest_path) for o in report.outcomes]
        self.assertEqual(len(set(paths)), 2, paths)
        self.assertEqual(
            sorted(os.path.basename(p) for p in paths),
            ["119CANON_MVI_0001.AVI", "MVI_0001.AVI"],
        )
        self.assertEqual(sha(paths[0]), digest(CLIP_A))
        self.assertEqual(sha(paths[1]), digest(CLIP_B))

        # Both are deletable, and each on the strength of its own bytes.
        self.assertTrue(report.all_verified, [o.error for o in report.outcomes])
        self.assertEqual([id(f) for f in report.deletable], [id(f) for f in files])


# --------------------------------------------------------------------------- #
# F4 - the card in the reader may not be the card that was downloaded
# --------------------------------------------------------------------------- #


class SwappedCardRegressionTest(RegressionCase):
    """:meth:`MassStorageBackend.delete` / ``_still_the_listed_file``."""

    def test_delete_refuses_a_file_that_no_longer_matches_the_listing(self) -> None:
        """F4 - a card swapped in the reader remounts at the identical path, and
        deletion by path alone erased a same-named photo that had never been
        downloaded.

        The delete gate upstream proves that *a* file of this device path was
        copied and verified; only the card can prove that the file still at that
        path is the one that was copied. Both facts recorded during listing are
        checked here: the size, and the modification time. Each is disturbed on
        its own so that neither check can be removed without a failure — a
        second card really can hold a same-named clip of the same length.
        """
        original: CardSpec = {
            "118CANON": {
                "MVI_0001.AVI": clip(1000, 0x11),
                "MVI_0002.AVI": clip(2000, 0x22),
            }
        }
        card_root, _payloads = self.make_card(original)
        camera = mass_storage_camera(card_root)
        backend = MassStorageBackend()

        listed = backend.list_files(camera)
        self.assertEqual([f.name for f in listed], ["MVI_0001.AVI", "MVI_0002.AVI"])
        by_size, by_time = listed

        # The swap: a different card, mounted at the same path, whose 118CANON
        # folder holds its own MVI_0001/MVI_0002. One differs from the listing
        # in size only, the other in timestamp only.
        other_size = clip(1500, 0x55)
        same_size = clip(2000, 0x77)
        self.assertEqual(len(same_size), by_time.size)

        first = str(by_size.raw["src"])
        second = str(by_time.raw["src"])
        with open(first, "wb") as handle:
            handle.write(other_size)
        os.utime(first, (by_size.mtime, by_size.mtime))
        with open(second, "wb") as handle:
            handle.write(same_size)
        os.utime(second, (by_time.mtime + 3600.0, by_time.mtime + 3600.0))

        outcomes = backend.delete(camera, listed, RecordingProgress())

        self.assertEqual([o.ok for o in outcomes], [False, False])
        for outcome in outcomes:
            self.assertIn("re-scan", outcome.error.lower(), outcome.error)

        # Nothing was erased: both files are still there, still holding the
        # bytes of the card that is actually in the reader.
        self.assertEqual(sha(first), digest(other_size))
        self.assertEqual(sha(second), digest(same_size))


# --------------------------------------------------------------------------- #
# F5 - a path names a file name, not a photograph
# --------------------------------------------------------------------------- #


class DeleteGateIdentityRegressionTest(RegressionCase):
    """:meth:`TransferEngine.delete_verified` / ``TransferReport.verified_outcomes``."""

    def test_a_failed_outcome_cannot_ride_on_a_verified_namesakes_path(self) -> None:
        """F5 - the gate filtered by ``CameraFile.path``, so a failed outcome
        that shared a verified file's path was handed to ``backend.delete()``.

        A driver that flattens the camera's tree reports ``/MVI_0001.AVI`` for
        two clips recorded a year apart, and the two ``CameraFile`` objects then
        compare *equal* as well: same folder, same name, same size (``raw``,
        which holds the driver's own handle, is excluded from equality). Only
        object identity separates them, which is why the gate walks outcomes
        rather than matching paths. Membership is tested here with ``is``,
        because an assertion written with ``==`` would pass while the wrong
        picture was erased.
        """
        dest = self.dest()
        verified_copy = os.path.join(dest, "MVI_0001.AVI")
        with open(verified_copy, "wb") as handle:
            handle.write(CLIP_A)

        rescued = CameraFile(
            folder="/",
            name="MVI_0001.AVI",
            size=len(CLIP_A),
            raw={"item_id": r"0000\Root\118CANON\MVI_0001"},
        )
        lost = CameraFile(
            folder="/",
            name="MVI_0001.AVI",
            size=len(CLIP_B),
            raw={"item_id": r"0000\Root\119CANON\MVI_0001"},
        )
        self.assertEqual(rescued.path, lost.path, "the fixture must arm the trap")
        self.assertEqual(rescued, lost, "and they must be indistinguishable by value")

        report = TransferReport(
            outcomes=[
                # The failed one first: order must not be what saves the photo.
                DownloadOutcome(
                    file=lost,
                    dest_path=None,
                    ok=False,
                    error="the camera returned an empty file",
                ),
                DownloadOutcome(
                    file=rescued,
                    dest_path=verified_copy,
                    ok=True,
                    verify=VerifyResult(ok=True),
                ),
            ],
            dest_dir=dest,
        )

        # The report's own view of "verified" is already by outcome, not by path.
        self.assertEqual(len(report.verified_outcomes()), 1)
        self.assertIs(report.verified_outcomes()[0].file, rescued)
        self.assertEqual(len(report.deletable), 1)

        backend = _RecordingDeleteBackend()
        camera = CameraInfo(
            model="Canon PowerShot S30", port="{device-id}", kind=BackendKind.WIA
        )
        results = TransferEngine(backend, camera).delete_verified(report)

        self.assertEqual(len(backend.batches), 1)
        handed = backend.batches[0]
        self.assertEqual(len(handed), 1, [f.path for f in handed])
        self.assertIs(handed[0], rescued)
        self.assertNotIn(id(lost), {id(f) for f in handed})
        self.assertEqual([r.ok for r in results], [True])
        self.assertIs(results[0].file, rescued)


# --------------------------------------------------------------------------- #
# F6 - the rescue may not be written onto the card it is rescuing
# --------------------------------------------------------------------------- #


class DestinationOnCardRegressionTest(RegressionCase):
    """:meth:`TransferEngine._reject_dest_on_device`."""

    def test_a_destination_on_the_card_is_refused_before_anything_is_written(
        self,
    ) -> None:
        """F6 - with the destination on the card, every step was individually
        correct and the result was total loss.

        Each source file already sits at its own destination path, so the resume
        check reports it as skipped; verification then re-reads *the original*
        and passes; the gate re-stats *the original* and finds it intact; and
        the erase removes every photo while no copy was ever made anywhere else.
        The only place to break that chain is before the first byte is written,
        so the refusal must come out of ``download`` itself — and it must not
        even create the folder or leave its write probe behind.
        """
        card_root, _payloads = self.make_card(TWIN_CARD)
        camera = mass_storage_camera(card_root)
        engine = TransferEngine(MassStorageBackend(), camera)
        files = engine.list_files()
        before = self.snapshot(card_root)
        self.assertTrue(before, "the card must actually hold something")

        destinations = [
            card_root,  # the card itself
            os.path.join(card_root, "DCIM"),  # the DCIM folder
            os.path.join(card_root, "DCIM", "118CANON"),  # beside the photos
            os.path.join(card_root, "rescued"),  # a new folder on the card
            self.tmp,  # a folder that *contains* the card
        ]
        for dest in destinations:
            with self.subTest(dest=dest):
                with self.assertRaises(CameraError) as caught:
                    engine.download(files, dest, RecordingProgress())
                # The message has to name the card, or the user cannot act on it.
                self.assertIn(card_root, str(caught.exception))

                self.assertEqual(self.snapshot(card_root), before)

        # A destination that did not exist must not have been created either:
        # the refusal precedes _prepare_dest, which is what creates folders.
        self.assertFalse(os.path.exists(os.path.join(card_root, "rescued")))

        # And the destination the user should have picked still works, which is
        # what keeps the check above from being satisfiable by refusing always.
        report = engine.download(files, self.dest(), RecordingProgress())
        self.assertTrue(report.all_verified, [o.error for o in report.outcomes])


# --------------------------------------------------------------------------- #
# F7 - "no progress callback" is a caller's choice, not a camera failure
# --------------------------------------------------------------------------- #


class NoProgressCallbackRegressionTest(RegressionCase):
    """:func:`retrocam.transfer._as_progress`."""

    def test_every_engine_entry_point_accepts_progress_none(self) -> None:
        """F7 - passing ``progress=None`` raised ``TypeError: 'NoneType' object
        is not callable`` deep inside a backend, which the engine then wrapped
        into a message blaming the *camera* for what is a caller's choice.

        ``None`` is the idiomatic way to say "report nothing" and it bypasses
        the keyword default, so all three entry points have to normalise it.
        This runs a whole rescue — list, download, erase — without a single
        callback; any of them still assuming a callable would fail here, and a
        misdirected error message is the most expensive kind: it sends the user
        to check the camera and the cable while the code is at fault.
        """
        card_root, payloads = self.make_card(TWIN_CARD)
        camera = mass_storage_camera(card_root)
        backend = MassStorageBackend()
        engine = TransferEngine(backend, camera)
        dest = self.dest()

        files = engine.list_files(None)
        self.assertEqual(len(files), len(payloads))

        report = engine.download(files, dest, None)
        self.assertTrue(report.all_verified, [o.error for o in report.outcomes])
        self.assertEqual(len(report.deletable), len(files))

        results = engine.delete_verified(report, None)
        self.assertEqual([r.ok for r in results], [True] * len(files))
        for camera_file in files:
            self.assertFalse(os.path.exists(str(camera_file.raw["src"])))


if __name__ == "__main__":  # pragma: no cover - convenience runner
    unittest.main()
