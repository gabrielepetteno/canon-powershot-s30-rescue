"""Tests for :mod:`retrocam.backends.massstorage` — the backend that does the rescue.

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests

Nothing is faked here except the *volume enumerator*: every test builds a real
DCIM tree in a temporary directory and lets the real backend walk it, copy from
it and erase inside it. That is the point — this backend's whole job is
``os.walk`` plus a careful copy loop, so a test that stubs the filesystem tests
nothing. Only :func:`massstorage._candidate_mounts` is monkeypatched, because
the alternative is scanning the developer's real ``/Volumes``.

The assertions that matter are the ones that stand between a photograph and its
deletion:

* a copy is byte-identical, not merely same-sized;
* two files that share a base name across two Canon folders reach two distinct
  destinations, and each destination holds the *right* photograph;
* nothing is ever skipped on the strength of a file this same batch just wrote;
* a cancelled transfer leaves no debris that could be mistaken for a photo;
* delete removes exactly what it was handed, and refuses when the file under
  the path is no longer the file that was listed.
"""

from __future__ import annotations

import os
import sys
import unittest
from typing import List, Optional
from unittest import mock

# The package lives in src/ and is not installed while the suite runs from a
# checkout. Derived from this file's location, so discovery works from anywhere.
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
_SRC_DIR = os.path.join(os.path.dirname(_TESTS_DIR), "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from helpers import (  # noqa: E402
    RecordingProgress,
    TempDirCase,
    make_read_only,
    mass_storage_camera,
    sha,
    tiny_jpeg,
)
from retrocam.backends import massstorage  # noqa: E402
from retrocam.backends.massstorage import MassStorageBackend  # noqa: E402
from retrocam.model import (  # noqa: E402
    BackendKind,
    CameraFile,
    CameraNotFound,
    CancelToken,
    TransferAborted,
)


# --------------------------------------------------------------------------- #
# Local fixtures
# --------------------------------------------------------------------------- #


class _CancelOnNthPoll(CancelToken):
    """A token that trips itself the ``n``-th time the copy loop asks.

    The copy loop polls :meth:`cancelled` once per chunk, so this fires *inside*
    a file instead of tidily between two of them. That is the only moment that
    can leave a half-written photograph on disk, which is exactly what the
    ``.part``-then-rename design exists to prevent.
    """

    def __init__(self, n: int) -> None:
        super().__init__()
        self.n = n
        self.polls = 0

    def cancelled(self) -> bool:
        self.polls += 1
        if self.polls >= self.n:
            self.cancel()
        return super().cancelled()


def _debris(dest_dir: str) -> List[str]:
    """Every partial-transfer artefact left in a destination directory."""
    if not os.path.isdir(dest_dir):
        return []
    return sorted(
        name
        for name in os.listdir(dest_dir)
        if name.startswith(".rcr-") or name.endswith(".part")
    )


class _CardCase(TempDirCase):
    """Base case: a backend, a card on disk, and a destination folder."""

    def setUp(self) -> None:
        super().setUp()
        self.backend = MassStorageBackend()
        self.dest = self.path("dest")

    def card(self, spec=None, name: str = "NO NAME"):
        """``(root, {device_path: bytes}, CameraInfo)`` for one card on disk."""
        root, contents = self.make_card(spec, name=name)
        return root, contents, mass_storage_camera(root)

    def mounts(self, *entries: str):
        """Patch volume enumeration so detection never touches real /Volumes."""
        return mock.patch.object(
            massstorage,
            "_candidate_mounts",
            lambda: [(path, "Mounted volume") for path in entries],
        )

    def assertNoDebris(self, dest_dir: Optional[str] = None) -> None:
        left = _debris(self.dest if dest_dir is None else dest_dir)
        self.assertEqual(left, [], "partial-transfer files were left behind: %r" % left)


# --------------------------------------------------------------------------- #
# Availability
# --------------------------------------------------------------------------- #


class AvailabilityTests(unittest.TestCase):
    """This backend is the fallback that must never be unavailable."""

    def test_is_available_is_unconditionally_true(self) -> None:
        # Every other backend can be missing a tool or a driver. This one only
        # uses the filesystem, and the registry relies on that: it is the last
        # transport standing when everything clever has failed.
        self.assertEqual(MassStorageBackend.is_available(), (True, ""))

    def test_install_hint_is_empty_because_nothing_can_be_installed(self) -> None:
        self.assertEqual(MassStorageBackend.install_hint(), "")


# --------------------------------------------------------------------------- #
# detect()
# --------------------------------------------------------------------------- #


class DetectTests(_CardCase):
    """Detection must find cards, and must find *only* cards."""

    def test_detect_finds_a_volume_that_holds_a_dcim_folder(self) -> None:
        root, _contents, _camera = self.card()
        progress = RecordingProgress()

        with self.mounts(root):
            found = self.backend.detect(progress)

        self.assertEqual(len(found), 1)
        info = found[0]
        self.assertEqual(info.kind, BackendKind.MASS_STORAGE)
        self.assertEqual(info.port, os.path.abspath(root))
        self.assertEqual(info.raw["mount"], os.path.abspath(root))
        self.assertEqual(info.raw["dcim"], os.path.join(os.path.abspath(root), "DCIM"))
        self.assertTrue(os.path.isdir(info.raw["dcim"]))
        # The GUI shows this verbatim, so the mount kind has to survive.
        self.assertIn("Mounted volume", info.detail)
        self.assertTrue(progress.saw_phase("detect"))
        self.assertIn("Found 1 card(s) with photos.", progress.messages())

    def test_detect_ignores_a_volume_without_dcim(self) -> None:
        # An external backup disk is a mounted volume too. Offering it as a
        # camera would invite the user to point the delete gate at their archive.
        plain = self.path("Backup")
        os.makedirs(os.path.join(plain, "Documents"))
        with open(os.path.join(plain, "Documents", "notes.txt"), "w") as handle:
            handle.write("not a camera")

        with self.mounts(plain):
            self.assertEqual(self.backend.detect(), [])

    def test_detect_accepts_a_lowercase_dcim_folder(self) -> None:
        # A card re-written by a Linux tool can end up with 'dcim'. Missing it
        # would tell the user their photos are gone.
        root = self.path("LINUXCARD")
        lower = os.path.join(root, "dcim", "100ABCDE")
        os.makedirs(lower)
        with open(os.path.join(lower, "A.JPG"), "wb") as handle:
            handle.write(tiny_jpeg(64))

        with self.mounts(root):
            found = self.backend.detect()

        self.assertEqual(len(found), 1)
        dcim = found[0].raw["dcim"]
        self.assertTrue(os.path.isdir(dcim))
        self.assertEqual(os.path.basename(dcim).upper(), "DCIM")
        # samefile rather than string equality: on a case-insensitive volume the
        # backend legitimately reports the 'DCIM' spelling for a 'dcim' folder.
        self.assertTrue(os.path.samefile(dcim, lower.rsplit(os.sep, 1)[0]))

    def test_detect_names_a_canon_card_from_its_dcim_folder(self) -> None:
        # '118CANON' is the strongest brand signal a FAT card carries; the volume
        # label is very often literally 'NO NAME'. Saying "Canon card" is the
        # difference between recognising your camera and not.
        root, _contents, _camera = self.card(name="NO NAME")

        with self.mounts(root):
            found = self.backend.detect()

        self.assertEqual(found[0].raw["vendor"], "Canon")
        self.assertEqual(found[0].model, "Canon card (NO NAME)")

    def test_detect_falls_back_to_the_volume_label_for_an_unknown_vendor(self) -> None:
        spec = {"100ABCDE": {"A.JPG": tiny_jpeg(64)}}
        root, _contents, _camera = self.card(spec, name="UNTITLED")

        with self.mounts(root):
            found = self.backend.detect()

        self.assertEqual(found[0].raw["vendor"], "")
        self.assertEqual(found[0].model, "UNTITLED")

    def test_detect_never_offers_the_startup_disk(self) -> None:
        # Belt and braces: the boot volume is refused by _is_boot_volume, and
        # would in any case fail the DCIM requirement. Asserting it here means a
        # future change to either check still cannot put the startup disk — and
        # therefore every file on it — in front of the delete gate.
        root, _contents, _camera = self.card()

        with self.mounts(os.sep, root):
            found = self.backend.detect()

        self.assertEqual([info.raw["mount"] for info in found], [os.path.abspath(root)])

    def test_the_boot_volume_guard_sees_through_the_macos_firmlink(self) -> None:
        # /Volumes/Macintosh HD is not a separate volume, it is a firmlink to
        # '/'. Comparing path strings would miss it and detection would walk the
        # entire startup disk, so the guard has to resolve the link.
        firmlink = self.path("Macintosh HD")
        os.symlink(os.sep, firmlink)

        self.assertTrue(massstorage._is_boot_volume(os.sep))
        self.assertTrue(massstorage._is_boot_volume(firmlink))
        self.assertFalse(massstorage._is_boot_volume(self.card()[0]))

    def test_detect_reports_one_card_reachable_through_two_mount_paths_once(
        self,
    ) -> None:
        # Not hypothetical: the Linux sweep descends one level under /media to
        # find udisks2's /media/<user>/<label>, so a card mounted at /media/CARD
        # has its own /media/CARD/DCIM enumerated as a candidate mount too. Both
        # candidates resolve to the same DCIM folder, and _find_dcim accepts a
        # root that *is* a DCIM folder — so without the seen_dcim guard the same
        # card would appear twice in the device list and be downloaded twice.
        root, _contents, _camera = self.card()

        with self.mounts(root, os.path.join(root, "DCIM")):
            found = self.backend.detect()

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].raw["mount"], os.path.abspath(root))


# --------------------------------------------------------------------------- #
# list_files()
# --------------------------------------------------------------------------- #


class ListFilesTests(_CardCase):
    """The listing is the baseline verification and deletion both trust."""

    def test_list_files_walks_every_dcim_subfolder(self) -> None:
        # A non-recursive listing silently loses most of a Canon archive: the
        # images live in 118CANON, 119CANON and so on, never in DCIM itself.
        root, contents, camera = self.card()

        files = self.backend.list_files(camera)

        self.assertEqual(sorted(f.path for f in files), sorted(contents))

    def test_list_files_reports_the_exact_size_and_mtime_from_the_filesystem(
        self,
    ) -> None:
        # Verification compares against this size and the delete gate compares
        # against this mtime. A rounded value here would make both meaningless.
        root, contents, camera = self.card()

        for camera_file in self.backend.list_files(camera):
            st = os.stat(camera_file.raw["src"])
            self.assertEqual(camera_file.size, st.st_size, camera_file.path)
            self.assertEqual(camera_file.mtime, st.st_mtime, camera_file.path)
            self.assertEqual(camera_file.size, len(contents[camera_file.path]))

    def test_list_files_returns_a_stable_folder_then_name_order(self) -> None:
        root, _contents, camera = self.card()

        first = [f.path for f in self.backend.list_files(camera)]
        second = [f.path for f in self.backend.list_files(camera)]

        self.assertEqual(first, second)
        self.assertEqual(first, sorted(first))

    def test_list_files_skips_os_junk_and_hidden_entries(self) -> None:
        # Copying these into the user's photo folder is not merely untidy: an
        # AppleDouble sidecar is a resource fork, it will fail verification, and
        # a failed file is one the user is told they must not delete.
        root, contents, camera = self.card()
        folder = os.path.join(root, "DCIM", "118CANON")
        junk = {
            ".hidden.JPG": b"hidden",
            "._IMG_0001.JPG": b"appledouble resource fork",
            "Thumbs.db": b"windows thumbnail cache",
            "desktop.ini": b"[.ShellClassInfo]",
            ".DS_Store": b"finder metadata",
        }
        for name, data in junk.items():
            with open(os.path.join(folder, name), "wb") as handle:
                handle.write(data)
        # A whole junk directory, to prove the walk is pruned and not merely
        # filtered file-by-file.
        svi = os.path.join(root, "DCIM", "System Volume Information")
        os.makedirs(svi)
        with open(os.path.join(svi, "IndexerVolumeGuid"), "wb") as handle:
            handle.write(b"{...}")

        files = self.backend.list_files(camera)

        # The junk is gone and every real photograph survived: a filter that
        # threw away IMG_0001.JPG along with Thumbs.db would pass a
        # "no junk listed" assertion on its own.
        self.assertEqual({f.name for f in files} & set(junk), set())
        self.assertEqual(sorted(f.path for f in files), sorted(contents))

    def test_list_files_on_an_empty_dcim_returns_an_empty_list(self) -> None:
        # A freshly formatted card is not an error condition; the GUI must show
        # "0 files", not a failure the user tries to fix by re-inserting it.
        root = self.path("EMPTY")
        os.makedirs(os.path.join(root, "DCIM"))
        camera = mass_storage_camera(root)
        progress = RecordingProgress()

        self.assertEqual(self.backend.list_files(camera, progress), [])
        self.assertIn("Found 0 file(s).", progress.messages())

    def test_list_files_raises_when_the_card_has_been_unplugged(self) -> None:
        # Nothing from CameraInfo.raw is trusted without a fresh isdir, because
        # the reader may have been pulled since detect() ran. The message is
        # asserted too: it is shown verbatim, and "plug it back in" is a very
        # different instruction from "this is the wrong card".
        root, _contents, camera = self.card()
        import shutil

        shutil.rmtree(root)

        with self.assertRaises(CameraNotFound) as caught:
            self.backend.list_files(camera)
        self.assertIn("not connected any more", str(caught.exception))


# --------------------------------------------------------------------------- #
# download()
# --------------------------------------------------------------------------- #


class DownloadTests(_CardCase):
    """The copy itself: byte-identity, distinct names, no debris."""

    def test_download_writes_byte_identical_copies(self) -> None:
        root, contents, camera = self.card()
        files = self.backend.list_files(camera)

        outcomes = self.backend.download(camera, files, self.dest)

        self.assertEqual(len(outcomes), len(files))
        for outcome in outcomes:
            self.assertTrue(outcome.ok, outcome.error)
            self.assertIsNotNone(outcome.dest_path)
            with open(outcome.dest_path, "rb") as handle:
                self.assertEqual(handle.read(), contents[outcome.file.path])
        self.assertNoDebris()

    def test_download_gives_two_folders_sharing_a_name_two_destinations(self) -> None:
        # 118CANON/IMG_0001.JPG and 119CANON/IMG_0001.JPG are different
        # photographs; a Canon reuses base names once its counter rolls over.
        # Writing both to 'IMG_0001.JPG' destroys one of them, and the delete
        # gate would then happily erase the original of the one that was lost.
        root, contents, camera = self.card()
        files = self.backend.list_files(camera)
        by_path = {f.path: f for f in files}
        pair = ["/DCIM/118CANON/IMG_0001.JPG", "/DCIM/119CANON/IMG_0001.JPG"]

        outcomes = self.backend.download(camera, [by_path[p] for p in pair], self.dest)

        dest_paths = [o.dest_path for o in outcomes]
        self.assertEqual(len(set(dest_paths)), 2, "both photos went to one file")
        self.assertEqual(
            [os.path.basename(p) for p in dest_paths],
            ["IMG_0001.JPG", "119CANON_IMG_0001.JPG"],
        )
        # Names being distinct is not enough — assert each destination holds the
        # photograph it is supposed to hold.
        for device_path, outcome in zip(pair, outcomes):
            with open(outcome.dest_path, "rb") as handle:
                self.assertEqual(handle.read(), contents[device_path], device_path)
        self.assertNotEqual(sha(dest_paths[0]), sha(dest_paths[1]))

    def test_download_preserves_the_cameras_timestamp(self) -> None:
        # On a 20-year-old archive the mtime is often the only surviving record
        # of when the photo was taken. The card is deliberately back-dated to
        # 2003 first: with a freshly written card the source mtime is "now" and
        # a copy that kept no timestamp at all would look correct.
        root, contents, camera = self.card()
        shot_at = 1060792920.0  # 13 August 2003, the S30's era
        for device_path in contents:
            os.utime(os.path.join(root, device_path.lstrip("/")), (shot_at, shot_at))
        files = self.backend.list_files(camera)

        outcomes = self.backend.download(camera, files, self.dest)

        for outcome in outcomes:
            self.assertEqual(outcome.file.mtime, shot_at, outcome.file.path)
            self.assertAlmostEqual(
                os.stat(outcome.dest_path).st_mtime,
                shot_at,
                delta=0.01,
                msg=outcome.file.path,
            )

    def test_download_creates_the_destination_folder_and_leaves_no_part_files(
        self,
    ) -> None:
        root, contents, camera = self.card()
        self.assertFalse(os.path.exists(self.dest))

        self.backend.download(camera, self.backend.list_files(camera), self.dest)

        self.assertTrue(os.path.isdir(self.dest))
        self.assertEqual(len(os.listdir(self.dest)), len(contents))
        self.assertNoDebris()

    def test_download_ticks_progress_within_a_file_that_spans_chunks(self) -> None:
        # The tunables are lowered rather than writing a 20 MB fixture: the loop
        # under test is identical, and the test stays fast and deterministic.
        big = tiny_jpeg(40000, 0x7E)
        root, _contents, camera = self.card({"118CANON": {"BIG.JPG": big}})
        files = self.backend.list_files(camera)
        progress = RecordingProgress()

        with (
            mock.patch.object(massstorage, "COPY_BUFFER", 4096),
            mock.patch.object(massstorage, "PROGRESS_EVERY_BYTES", 8192),
        ):
            self.backend.download(camera, files, self.dest, progress)

        ticks = [t for t in progress.of_phase("download") if t.name == "BIG.JPG"]
        self.assertTrue(all(t.bytes_total == len(big) for t in ticks))
        partial = [t for t in ticks if 0 < t.bytes_done < len(big)]
        self.assertGreaterEqual(len(partial), 2, "the bar would freeze on a big file")
        counts = [t.bytes_done for t in ticks]
        self.assertEqual(counts, sorted(counts), "progress went backwards")
        self.assertEqual(counts[-1], len(big))

    def test_a_copy_that_stops_short_is_never_reported_as_ok(self) -> None:
        # The card stops delivering bytes partway through the read. What lands on
        # disk is a truncated photograph; ok=True here would let the engine
        # certify it and the delete gate erase the only complete copy, which is
        # still on the card.
        #
        # The card is shrunk from inside the progress callback that announces the
        # file, which the backend emits *before* it opens the source — so the
        # copy loop genuinely reads fewer bytes than the size it was promised.
        # (Truncating once the file is already open is not usable here: macOS
        # keeps serving the cached pages past the new end of file.)
        big = tiny_jpeg(40000, 0x3C)
        root, _contents, camera = self.card({"118CANON": {"BIG.JPG": big}})
        files = self.backend.list_files(camera)
        source = files[0].raw["src"]
        short = 8192
        state = {"cut": False}

        def cut_the_card_off(tick):
            if tick.bytes_total and tick.bytes_done == 0 and not state["cut"]:
                state["cut"] = True
                os.truncate(source, short)

        outcomes = self.backend.download(camera, files, self.dest, cut_the_card_off)

        self.assertTrue(state["cut"], "the fixture never interrupted the copy")
        self.assertFalse(outcomes[0].ok)
        self.assertIn("Copied %d of %d bytes" % (short, len(big)), outcomes[0].error)
        self.assertIn("the card stopped responding", outcomes[0].error)
        self.assertEqual(os.path.getsize(outcomes[0].dest_path), short)
        self.assertNoDebris()

    def test_a_file_that_changed_size_since_the_listing_is_never_reported_as_ok(
        self,
    ) -> None:
        # The download-side sibling of the delete gate's F4 check: the listing's
        # size is the number verification will trust. If the card disagrees now,
        # a different photograph is sitting at that path — a swapped card is the
        # dangerous case — and this copy must not be certified.
        root, _contents, camera = self.card()
        files = self.backend.list_files(camera)
        victim = files[0]
        with open(victim.raw["src"], "wb") as handle:
            handle.write(tiny_jpeg(5000, 0xDD))  # another photo, another size

        outcomes = self.backend.download(camera, [victim], self.dest)

        self.assertFalse(outcomes[0].ok)
        self.assertIn("changed size on the card", outcomes[0].error)
        self.assertIn("Re-scan the card before deleting anything", outcomes[0].error)

    def test_download_reports_a_failure_per_file_without_ending_the_run(self) -> None:
        # A 20-year-old card usually has a few unreadable sectors. One bad file
        # must not cost the user the other eighty.
        root, contents, camera = self.card()
        files = self.backend.list_files(camera)
        os.remove(files[0].raw["src"])  # the card lost this sector

        outcomes = self.backend.download(camera, files, self.dest)

        self.assertEqual(len(outcomes), len(files))
        self.assertFalse(outcomes[0].ok)
        self.assertIn("Could not read", outcomes[0].error)
        self.assertTrue(all(o.ok for o in outcomes[1:]))
        self.assertNoDebris()


# --------------------------------------------------------------------------- #
# download(skip_existing=True)
# --------------------------------------------------------------------------- #


class SkipExistingTests(_CardCase):
    """Resume support — the one place where a wrong answer costs a photograph."""

    def test_an_identical_existing_copy_is_skipped(self) -> None:
        root, _contents, camera = self.card()
        files = self.backend.list_files(camera)
        first = self.backend.download(camera, files, self.dest)
        before = {o.dest_path: sha(o.dest_path) for o in first}

        second = self.backend.download(camera, files, self.dest, skip_existing=True)

        for outcome in second:
            self.assertTrue(outcome.skipped, outcome.file.path)
            # A skipped file moved no bytes, so this backend cannot certify it.
            # The engine re-verifies on disk; erring this way means a resumed run
            # may re-check a file, never that an unchecked file becomes deletable.
            self.assertFalse(outcome.ok)
            self.assertIn(outcome.dest_path, before)
        self.assertEqual({p: sha(p) for p in before}, before)
        self.assertEqual(len(os.listdir(self.dest)), len(files))

    def test_a_same_name_file_of_a_different_size_is_not_skipped(self) -> None:
        # Someone else's IMG_0002.JPG already sitting in the download folder must
        # not be mistaken for this card's photo, and must not be overwritten.
        root, contents, camera = self.card()
        files = self.backend.list_files(camera)
        target = next(f for f in files if f.name == "IMG_0002.JPG")
        decoy = os.path.join(self.dest, "IMG_0002.JPG")
        os.makedirs(self.dest)
        with open(decoy, "wb") as handle:
            handle.write(b"a completely different photograph")
        decoy_digest = sha(decoy)

        outcomes = self.backend.download(camera, [target], self.dest)

        self.assertFalse(outcomes[0].skipped)
        self.assertTrue(outcomes[0].ok, outcomes[0].error)
        self.assertNotEqual(outcomes[0].dest_path, decoy)
        self.assertEqual(sha(decoy), decoy_digest, "the stranger's file was clobbered")
        with open(outcomes[0].dest_path, "rb") as handle:
            self.assertEqual(handle.read(), contents[target.path])

    def test_a_file_is_never_credited_to_a_copy_the_same_batch_just_wrote(self) -> None:
        # Review finding — the 'claimed' set in _existing_copy.
        #
        # Two Canon folders hold IMG_0001.JPG. Here they are the same size and
        # carry the same timestamp (a card formatted and shot in one session does
        # exactly this), but they are different photographs. Without the claimed
        # set, file B matches the flat 'IMG_0001.JPG' that file A wrote moments
        # earlier on size and mtime, comes back skipped=True with A's dest_path,
        # and B is then never copied at all — while the engine verifies A's file
        # a second time and clears B for deletion from the card.
        same_size_a = tiny_jpeg(1500, 0xAA)
        same_size_b = tiny_jpeg(1500, 0xBB)
        self.assertEqual(len(same_size_a), len(same_size_b))
        self.assertNotEqual(same_size_a, same_size_b)
        root, contents, camera = self.card(
            {
                "118CANON": {"IMG_0001.JPG": same_size_a},
                "119CANON": {"IMG_0001.JPG": same_size_b},
            }
        )
        stamp = 1060000000.0
        for device_path in contents:
            os.utime(os.path.join(root, device_path.lstrip("/")), (stamp, stamp))
        files = self.backend.list_files(camera)
        self.assertEqual([f.mtime for f in files], [stamp, stamp])
        self.assertEqual(files[0].size, files[1].size)

        outcomes = self.backend.download(camera, files, self.dest, skip_existing=True)

        self.assertFalse(outcomes[1].skipped, "file B was credited to file A's copy")
        self.assertTrue(outcomes[1].ok, outcomes[1].error)
        self.assertNotEqual(outcomes[0].dest_path, outcomes[1].dest_path)
        for outcome in outcomes:
            with open(outcome.dest_path, "rb") as handle:
                self.assertEqual(handle.read(), contents[outcome.file.path])

    def test_skip_existing_false_copies_again_rather_than_skipping(self) -> None:
        root, _contents, camera = self.card()
        files = self.backend.list_files(camera)
        self.backend.download(camera, files, self.dest)

        outcomes = self.backend.download(camera, files, self.dest, skip_existing=False)

        self.assertTrue(all(not o.skipped and o.ok for o in outcomes))
        self.assertEqual(len(set(o.dest_path for o in outcomes)), len(files))
        self.assertNoDebris()


# --------------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------------- #


class CancellationTests(_CardCase):
    """A cancelled transfer must leave nothing that looks like a photograph."""

    def test_cancelling_mid_file_raises_and_leaves_no_part_file(self) -> None:
        big = tiny_jpeg(40000, 0x5A)
        root, _contents, camera = self.card({"118CANON": {"BIG.JPG": big}})
        files = self.backend.list_files(camera)
        cancel = _CancelOnNthPoll(3)  # two chunks in, well short of the end

        with mock.patch.object(massstorage, "COPY_BUFFER", 4096):
            with self.assertRaises(TransferAborted) as caught:
                self.backend.download(camera, files, self.dest, cancel=cancel)

        self.assertGreaterEqual(cancel.polls, 3)
        self.assertNoDebris()
        # And no plausible-looking truncated photo wearing the final name.
        self.assertFalse(os.path.exists(os.path.join(self.dest, "BIG.JPG")))
        self.assertEqual(os.listdir(self.dest), [])
        # What was already rescued travels on the exception so the caller can
        # still report it.
        self.assertIsInstance(getattr(caught.exception, "outcomes", None), list)

    def test_cancelling_between_files_keeps_the_files_already_copied(self) -> None:
        # Cancellation never rolls back: a user who stops a long rescue keeps
        # everything that was already written.
        root, contents, camera = self.card()
        files = self.backend.list_files(camera)
        cancel = CancelToken()
        original = MassStorageBackend._download_one

        def cancel_after_first(self_, *args, **kwargs):
            outcome = original(self_, *args, **kwargs)
            cancel.cancel()
            return outcome

        with mock.patch.object(MassStorageBackend, "_download_one", cancel_after_first):
            with self.assertRaises(TransferAborted) as caught:
                self.backend.download(camera, files, self.dest, cancel=cancel)

        rescued = getattr(caught.exception, "outcomes", [])
        self.assertEqual(len(rescued), 1)
        self.assertTrue(rescued[0].ok)
        with open(rescued[0].dest_path, "rb") as handle:
            self.assertEqual(handle.read(), contents[rescued[0].file.path])
        self.assertNoDebris()


# --------------------------------------------------------------------------- #
# delete()
# --------------------------------------------------------------------------- #


class DeleteTests(_CardCase):
    """Irreversible, so the assertions are about what is *not* removed."""

    def test_delete_removes_exactly_the_listed_files_one_at_a_time(self) -> None:
        root, contents, camera = self.card()
        files = self.backend.list_files(camera)
        doomed = [f for f in files if f.name == "IMG_0001.JPG"]
        spared = [f for f in files if f.name != "IMG_0001.JPG"]
        self.assertTrue(doomed and spared)
        real_remove = os.remove
        removed: List[str] = []

        def spy(path, *args, **kwargs):
            removed.append(os.fspath(path))
            return real_remove(path, *args, **kwargs)

        with mock.patch.object(os, "remove", spy):
            outcomes = self.backend.delete(camera, doomed)

        self.assertTrue(all(o.ok for o in outcomes))
        # One os.remove per file: never a glob, never rmtree, never a rmdir.
        self.assertEqual(sorted(removed), sorted(f.raw["src"] for f in doomed))
        for camera_file in doomed:
            self.assertFalse(os.path.exists(camera_file.raw["src"]))
        for camera_file in spared:
            self.assertTrue(os.path.exists(camera_file.raw["src"]))
            with open(camera_file.raw["src"], "rb") as handle:
                self.assertEqual(handle.read(), contents[camera_file.path])

    def test_delete_leaves_the_card_folder_structure_standing(self) -> None:
        # The camera expects DCIM and its numbered folders to exist; a card that
        # comes back with them missing may refuse to shoot until reformatted.
        root, _contents, camera = self.card()
        files = self.backend.list_files(camera)

        self.backend.delete(camera, files)

        self.assertTrue(os.path.isdir(os.path.join(root, "DCIM")))
        for folder in ("118CANON", "119CANON"):
            self.assertTrue(os.path.isdir(os.path.join(root, "DCIM", folder)), folder)

    def test_delete_on_a_write_protected_card_reports_an_outcome_not_an_exception(
        self,
    ) -> None:
        # The user is staring at a progress bar. A traceback here reads as "your
        # photos are gone"; a per-file message reads as "the lock switch is on".
        root, contents, camera = self.card()
        files = self.backend.list_files(camera)
        make_read_only(os.path.join(root, "DCIM", "118CANON"))

        outcomes = self.backend.delete(camera, files)

        blocked = [o for o in outcomes if o.file.folder == "/DCIM/118CANON"]
        self.assertTrue(blocked)
        for outcome in blocked:
            self.assertFalse(outcome.ok)
            self.assertIn("Could not delete", outcome.error)
            self.assertIn("permission denied", outcome.error.lower())
            # Refused, not half-done: the photograph is still on the card.
            self.assertTrue(os.path.exists(outcome.file.raw["src"]))
            with open(outcome.file.raw["src"], "rb") as handle:
                self.assertEqual(handle.read(), contents[outcome.file.path])

    def test_delete_refuses_a_path_that_does_not_resolve_inside_the_card(self) -> None:
        # The containment check is what keeps a corrupt listing from turning
        # delete into an arbitrary-file remover.
        root, _contents, camera = self.card()
        outside = self.path("precious.JPG")
        with open(outside, "wb") as handle:
            handle.write(tiny_jpeg(128))
        forged = CameraFile(
            folder="/DCIM/118CANON",
            name="precious.JPG",
            size=os.path.getsize(outside),
            mtime=os.stat(outside).st_mtime,
            raw={"src": outside, "mount": root},
        )

        outcomes = self.backend.delete(camera, [forged])

        self.assertFalse(outcomes[0].ok)
        self.assertIn("does not point inside the card", outcomes[0].error)
        self.assertTrue(os.path.exists(outside))


class StillTheListedFileTests(_CardCase):
    """F4 regression: the card in the reader may not be the card that was read."""

    def test_delete_refuses_a_file_whose_size_changed_since_the_listing(self) -> None:
        # Eject one CompactFlash, insert another, and the OS re-mounts it at the
        # very same path — where a second Canon body has very plausibly written
        # its own /DCIM/118CANON/IMG_0001.JPG. Deleting by path alone would erase
        # a photograph that was never downloaded.
        root, _contents, camera = self.card()
        files = self.backend.list_files(camera)
        victim = files[0]
        swapped = tiny_jpeg(9000, 0xEE)  # a different photo, different size
        with open(victim.raw["src"], "wb") as handle:
            handle.write(swapped)

        outcomes = self.backend.delete(camera, [victim])

        self.assertFalse(outcomes[0].ok)
        self.assertIn("not the file that was downloaded", outcomes[0].error)
        self.assertIn("Nothing was erased", outcomes[0].error)
        self.assertTrue(os.path.exists(victim.raw["src"]))
        with open(victim.raw["src"], "rb") as handle:
            self.assertEqual(handle.read(), swapped)

    def test_delete_refuses_a_file_whose_mtime_changed_since_the_listing(self) -> None:
        # The swapped card can coincidentally match on size; two photographs
        # matching on size *and* timestamp is not credible.
        root, contents, camera = self.card()
        files = self.backend.list_files(camera)
        victim = files[0]
        replacement = tiny_jpeg(1000, 0xCC)  # same size as DEFAULT_CARD's photo
        self.assertEqual(len(replacement), victim.size)
        with open(victim.raw["src"], "wb") as handle:
            handle.write(replacement)
        os.utime(victim.raw["src"], (victim.mtime + 600, victim.mtime + 600))

        outcomes = self.backend.delete(camera, [victim])

        self.assertFalse(outcomes[0].ok)
        self.assertIn("changed on the card", outcomes[0].error)
        self.assertTrue(os.path.exists(victim.raw["src"]))

    def test_a_timestamp_within_fats_two_second_granularity_still_deletes(self) -> None:
        # The guard must refuse swapped cards without refusing every card: FAT
        # stores mtimes at 2-second resolution, so a 1-second drift is normal.
        root, _contents, camera = self.card()
        files = self.backend.list_files(camera)
        victim = files[0]
        os.utime(victim.raw["src"], (victim.mtime + 1, victim.mtime + 1))

        outcomes = self.backend.delete(camera, [victim])

        self.assertTrue(outcomes[0].ok, outcomes[0].error)
        self.assertFalse(os.path.exists(victim.raw["src"]))


# --------------------------------------------------------------------------- #
# supports_delete()
# --------------------------------------------------------------------------- #


class SupportsDeleteTests(_CardCase):
    """The Delete button must be greyed out *before* the user commits."""

    def test_supports_delete_is_true_on_a_writable_card_and_leaves_no_probe(
        self,
    ) -> None:
        root, _contents, camera = self.card()
        self.backend.list_files(camera)  # this is what records the current card

        self.assertTrue(self.backend.supports_delete())

        # The probe is the only write this backend performs outside delete(); it
        # must not survive on a 20-year-old card.
        leftovers = [
            name
            for name in os.listdir(os.path.join(root, "DCIM"))
            if name.startswith(".rcr-")
        ]
        self.assertEqual(leftovers, [])

    def test_supports_delete_is_false_on_a_write_protected_card(self) -> None:
        root, _contents, camera = self.card()
        self.backend.list_files(camera)
        make_read_only(os.path.join(root, "DCIM"))

        self.assertFalse(self.backend.supports_delete())

    def test_supports_delete_is_true_before_any_card_has_been_touched(self) -> None:
        # Nothing is selected in that state, so refusing would only confuse.
        self.assertTrue(MassStorageBackend().supports_delete())


# --------------------------------------------------------------------------- #
# Symlinks
# --------------------------------------------------------------------------- #


class SymlinkContainmentTests(_CardCase):
    """A symlink on a camera card is either impossible or malicious."""

    def setUp(self) -> None:
        super().setUp()
        self.root, self.contents, self.camera = self.card()
        self.outside = self.path("elsewhere")
        os.makedirs(self.outside)
        self.secret = os.path.join(self.outside, "secret.JPG")
        self.secret_bytes = tiny_jpeg(777, 0x99)
        with open(self.secret, "wb") as handle:
            handle.write(self.secret_bytes)
        # A link to a file outside the card...
        self.link = os.path.join(self.root, "DCIM", "118CANON", "LINKED.JPG")
        os.symlink(self.secret, self.link)
        # ...and a link to a whole directory outside the card.
        os.symlink(self.outside, os.path.join(self.root, "DCIM", "ELSEWHERE"))

    def test_the_walk_never_descends_through_a_directory_symlink(self) -> None:
        listed = {f.path for f in self.backend.list_files(self.camera)}

        self.assertNotIn("/DCIM/ELSEWHERE/secret.JPG", listed)
        self.assertFalse(any(p.startswith("/DCIM/ELSEWHERE/") for p in listed))

    def test_no_byte_from_outside_the_card_ever_reaches_the_destination(self) -> None:
        # The end-to-end property: whatever the listing contains, downloading all
        # of it cannot copy a file that lives outside the mount point.
        files = self.backend.list_files(self.camera)

        self.backend.download(self.camera, files, self.dest)

        for name in os.listdir(self.dest):
            with open(os.path.join(self.dest, name), "rb") as handle:
                self.assertNotEqual(handle.read(), self.secret_bytes, name)
        self.assertFalse(os.path.exists(os.path.join(self.dest, "LINKED.JPG")))
        self.assertNoDebris()

    def test_downloading_a_symlink_is_refused_by_the_containment_check(self) -> None:
        linked = CameraFile(
            folder="/DCIM/118CANON",
            name="LINKED.JPG",
            size=len(self.secret_bytes),
            mtime=os.stat(self.link).st_mtime,
            raw={"src": self.link, "mount": self.root},
        )

        outcome = self.backend.download(self.camera, [linked], self.dest)[0]

        self.assertFalse(outcome.ok)
        self.assertIsNone(outcome.dest_path)
        self.assertIn("does not point inside the card", outcome.error)

    def test_deleting_a_link_out_of_the_card_is_refused_and_its_target_survives(
        self,
    ) -> None:
        # os.remove on this link would only unlink the link, but the file it
        # points at is outside the card and has never been downloaded, so the
        # containment check refuses before the shortcut check is even reached.
        linked = CameraFile(
            folder="/DCIM/118CANON",
            name="LINKED.JPG",
            size=len(self.secret_bytes),
            mtime=os.stat(self.link).st_mtime,
            raw={"src": self.link, "mount": self.root},
        )

        outcome = self.backend.delete(self.camera, [linked])[0]

        self.assertFalse(outcome.ok)
        self.assertIn("does not point inside the card", outcome.error)
        self.assertTrue(os.path.exists(self.secret))
        self.assertTrue(os.path.islink(self.link))

    def test_deleting_a_link_that_stays_inside_the_card_is_refused_as_a_shortcut(
        self,
    ) -> None:
        # A link whose target is on the card passes containment, so this is the
        # case the islink guard exists for. Erasing it would consume the user's
        # one delete pass on something that is not a photograph, while the real
        # image it points at stays behind looking like it was never downloaded.
        inside = os.path.join(self.root, "DCIM", "118CANON", "SHORTCUT.JPG")
        real_photo = os.path.join(self.root, "DCIM", "118CANON", "IMG_0002.JPG")
        os.symlink(real_photo, inside)
        linked = CameraFile(
            folder="/DCIM/118CANON",
            name="SHORTCUT.JPG",
            size=os.path.getsize(real_photo),
            mtime=os.stat(real_photo).st_mtime,
            raw={"src": inside, "mount": self.root},
        )

        outcome = self.backend.delete(self.camera, [linked])[0]

        self.assertFalse(outcome.ok)
        self.assertIn("shortcut", outcome.error)
        self.assertTrue(os.path.islink(inside))
        self.assertTrue(os.path.exists(real_photo))


if __name__ == "__main__":  # pragma: no cover - convenience runner
    unittest.main()
