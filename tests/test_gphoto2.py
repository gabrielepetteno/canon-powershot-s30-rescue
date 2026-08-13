"""Tests for :mod:`retrocam.backends.gphoto2_backend`.

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests

This is the only transport that can reach a pre-2003 Canon compact, and it is
also the one that talks to the camera through a *process*. So nothing here mocks
``subprocess``: :class:`helpers.FakeGphoto2` installs a real executable named
``gphoto2`` first on ``PATH``, the backend genuinely spawns it, and both halves
of the risky surface — the argv we construct and the stdout we parse — are
exercised for real. ``gp.calls()`` then lets a test assert what the camera was
actually asked to do, which is the only way to prove that a delete named exactly
one file.

Two things are deliberately kept out of the real world:

* **killall.** ``_release_device`` shells out to ``killall ptpcamerad`` on
  macOS. A test suite must not kill the developer's daemons, so every test runs
  with a shim in place of the module's ``subprocess`` that intercepts ``killall``
  and passes everything else — including the gphoto2 spawn — straight through.
* **Platform flags.** ``_IS_MACOS`` / ``_IS_WINDOWS`` are pinned per test rather
  than inherited from the machine, so the same assertions hold on any runner.

The interesting assertions are the negative ones. A file this backend reports as
``ok`` may be erased from a twenty-year-old card, so the tests below care much
more about what it *refuses* to claim than about the happy path.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import os
import subprocess
import sys
import time
import unittest
from typing import Any, Callable, List, Sequence, Tuple

# The package lives in src/ and is not installed while the suite runs from a
# checkout. helpers does the same thing; doing it here too keeps this module
# importable on its own.
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.join(os.path.dirname(_TESTS_DIR), "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from helpers import (  # noqa: E402
    DEFAULT_CARD,
    FakeGphoto2,
    RecordingProgress,
    TempDirCase,
    tiny_jpeg,
)

from retrocam import i18n  # noqa: E402
from retrocam.backends import gphoto2_backend as gp2  # noqa: E402
from retrocam.backends.gphoto2_backend import GPhoto2Backend  # noqa: E402
from retrocam.model import (  # noqa: E402
    BackendKind,
    CameraError,
    CameraFile,
    CameraInfo,
    CameraNotFound,
    CancelToken,
    TransferAborted,
)


# --------------------------------------------------------------------------- #
# Cards
# --------------------------------------------------------------------------- #


#: Two folders, one base name, *identical sizes*, different photographs. This is
#: the shape that makes a size-only resume check dangerous: on the card these are
#: two unrelated pictures that a naive skip would collapse into one.
CARD_WITH_DUPLICATE_NAMES = {
    "118CANON": {"IMG_0001.JPG": tiny_jpeg(1000, 0x11)},
    "119CANON": {"IMG_0001.JPG": tiny_jpeg(1000, 0x99)},
}


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


# --------------------------------------------------------------------------- #
# Fixture extensions
# --------------------------------------------------------------------------- #


class _ScriptedGphoto2(FakeGphoto2):
    """:class:`FakeGphoto2` with single-statement edits applied to the stub.

    Each entry in ``REPLACEMENTS`` swaps one whole statement for another, so the
    stub's indentation is preserved and the result stays valid Python. A
    replacement whose target has disappeared raises rather than silently doing
    nothing: a stub edit that quietly stops applying would downgrade a test to
    passing for the wrong reason, which is worse than a red build.
    """

    REPLACEMENTS: Tuple[Tuple[str, str], ...] = ()

    def _script_source(self) -> str:
        source = super()._script_source()
        for old, new in self.REPLACEMENTS:
            if old not in source:
                raise AssertionError(
                    "helpers.FakeGphoto2 no longer contains the statement %r, "
                    "so the stub edit in %s cannot be applied. Re-check it "
                    "against the fixture." % (old, type(self).__name__)
                )
            source = source.replace(old, new)
        return source


#: One line of genuine ``gphoto2 --parsable -L`` output, i.e. the format that
#: ``gphoto2_backend._PARSABLE_RE`` was written against and that the shipped
#: parser accepts.
_REAL_PARSABLE_LINE = (
    "print(\"FILENAME='%s/%s' PERMS=rd FILESIZE=%d FILETYPE=%s "
    'FILEMTIME=%d" % (folder.rstrip("/"), name, size, "image/jpeg" '
    'if name.lower().endswith((".jpg", ".jpeg")) '
    'else "application/octet-stream", 1700000000))'
)


class ParsableGphoto2(_ScriptedGphoto2):
    """A gphoto2 whose ``--parsable -L`` output the backend can actually read.

    ``helpers.FakeGphoto2`` prints ``--parsable`` output as one ``KEY=value``
    line per field, which no parser in the product accepts: against it, the
    backend silently falls through its attempt chain to the *human* listing every
    single time. That would leave the primary path — the one that supplies the
    exact byte counts verification depends on — untested, and would make
    :meth:`GPhoto2Backend.delete`'s re-listing confirm nothing while appearing to
    succeed. This subclass emits the real one-line format instead.

    If the shared fixture is fixed to emit that format itself, this class becomes
    a transparent pass-through rather than breaking.
    """

    REPLACEMENTS = (
        ('print("FILENAME=%s" % name)', _REAL_PARSABLE_LINE),
        ('print("FOLDER=%s" % folder)', "pass"),
        ('print("FILESIZE=%d" % size)', "pass"),
        ('print("FILEMTIME=%d" % 1700000000)', "pass"),
    )

    def _script_source(self) -> str:
        source = FakeGphoto2._script_source(self)
        if "PERMS=" in source:
            return source  # the shared fixture already emits the real format
        return super()._script_source()


class LyingDeleteGphoto2(ParsableGphoto2):
    """A camera that exits 0 from ``--delete-file`` and keeps the file anyway.

    Not a contrived fixture: it is the Canon behaviour the backend's re-listing
    was written for. A PowerShot that refuses an erase — a protected image, a
    write-locked card, a body that simply will not — can still report success,
    and only reading the folder back afterwards tells the difference.

    Only the ``os.remove`` inside the stub's delete branch is neutralised, so
    everything else about the invocation stays real: the argv the backend builds,
    the exit code it reads, and the listing it then parses.
    """

    def _script_source(self) -> str:
        source = super()._script_source()
        if "os.remove(src)" not in source:
            raise AssertionError(
                "the delete branch of helpers.FakeGphoto2 no longer calls "
                "'os.remove(src)', so this camera is not actually lying and the "
                "test using it would pass for the wrong reason."
            )
        return source.replace("os.remove(src)", "pass")


class HumanOnlyGphoto2(_ScriptedGphoto2):
    """An older gphoto2 that does not know ``--parsable``.

    Such builds exist, which is why the backend keeps a human-listing parser as a
    fallback. Failing the flag outright (rather than relying on the shared stub
    happening to print an unparsable format) is what makes the fallback path
    deterministic here.
    """

    REPLACEMENTS = (
        (
            'folder_filter = opt("--folder")',
            "sys.stderr.write(\"gphoto2: unrecognized option '--parsable'\\n\"); "
            "sys.exit(2)",
        ),
    )


class TwoCameraGphoto2(_ScriptedGphoto2):
    """``--auto-detect`` reporting two bodies, both with spaces in the model."""

    SECOND = ("Nikon COOLPIX 990", "usb:001,007")

    REPLACEMENTS = (
        (
            'print("%-30s %s" % (MODEL, PORT))',
            'print("%-30s %s" % (MODEL, PORT)); '
            'print("%-30s %s" % ("Nikon COOLPIX 990", "usb:001,007"))',
        ),
    )


class _SubprocessShim:
    """Stands in for the ``subprocess`` module *inside the backend only*.

    Exactly one command is intercepted: ``killall``. Everything else — the
    gphoto2 spawn, ``Popen``, ``DEVNULL``, ``TimeoutExpired`` — is proxied to the
    real module, so process groups, pipe draining and exit codes stay genuine.

    ``witness`` is sampled at each interception, which is how a test proves the
    release happened *before* gphoto2 was spawned rather than merely somewhere in
    the same call.
    """

    def __init__(self) -> None:
        self.killed: List[Tuple[List[str], Any]] = []
        self.witness: Callable[[], Any] = lambda: None

    def __getattr__(self, name: str) -> Any:
        return getattr(subprocess, name)

    def run(self, argv: Sequence[str], **kwargs: Any) -> Any:
        argv = list(argv)
        if argv and os.path.basename(argv[0]) == "killall":
            self.killed.append((argv, self.witness()))
            return subprocess.CompletedProcess(argv, 0)
        return subprocess.run(argv, **kwargs)

    def killed_names(self) -> List[str]:
        return [argv[1] for argv, _ in self.killed if len(argv) > 1]


class _ProcessStartClock:
    """A ``time`` stand-in whose ``monotonic()`` counts from process start.

    Python guarantees nothing about ``time.monotonic()``'s reference point --
    "the reference point of the returned value is undefined, so that only the
    difference between the results of two calls is valid" -- and real builds
    disagree. Apple's Python 3.9, this project's declared floor, counts from
    process start and so returns values near zero; the Homebrew 3.14 build
    counts from boot and returns six-figure values.

    Any code that uses ``0.0`` as a "this never happened" sentinel therefore
    works on one interpreter and silently misbehaves on the other. This shim
    pins the hostile case so the suite gives the same verdict everywhere instead
    of inheriting whichever clock the developer happens to run.

    Everything except ``monotonic`` proxies to the real module, and the clock is
    frozen: a test advances it explicitly with :meth:`advance`, so throttle
    behaviour is asserted rather than raced against.
    """

    def __init__(self, start: float = 0.01) -> None:
        self._now = start

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def sleep(self, _seconds: float) -> None:
        """The release path sleeps to let launchd notice; tests must not wait."""


# --------------------------------------------------------------------------- #
# Base case
# --------------------------------------------------------------------------- #


class GPhoto2TestCase(TempDirCase):
    """Deterministic environment for every gphoto2 test."""

    def setUp(self) -> None:
        super().setUp()

        # Message assertions are about the product's words, not the developer's
        # locale: i18n picks Italian on an Italian machine.
        previous_language = i18n.current_language()
        i18n.set_language("en")
        self.addCleanup(i18n.set_language, previous_language)

        # killall must never actually run.
        self.shim = _SubprocessShim()
        self.patch_module("subprocess", self.shim)

        # Pin the platform so the same assertions hold on macOS, Linux and CI.
        # Tests about macOS behaviour flip _IS_MACOS back on explicitly.
        self.patch_module("_IS_MACOS", False)
        self.patch_module("_IS_WINDOWS", False)

    # -- helpers ----------------------------------------------------------- #

    def patch_module(self, name: str, value: Any) -> None:
        """Set a module global on the backend for the duration of one test."""
        original = getattr(gp2, name)
        setattr(gp2, name, value)
        self.addCleanup(setattr, gp2, name, original)

    def patch_env(self, name: str, value: str) -> None:
        original = os.environ.get(name)
        os.environ[name] = value
        if original is None:
            self.addCleanup(os.environ.pop, name, None)
        else:
            self.addCleanup(os.environ.__setitem__, name, original)

    def camera(self, gp: FakeGphoto2) -> CameraInfo:
        """The :class:`CameraInfo` ``detect`` would have produced for ``gp``."""
        return CameraInfo(
            model=gp.model,
            port=gp.port,
            kind=BackendKind.GPHOTO2,
            detail=gp.port,
            raw={"backend": "gphoto2"},
        )

    def dest(self, name: str = "out") -> str:
        path = self.path(name)
        os.makedirs(path, exist_ok=True)
        return path

    # -- views over the recorded argv -------------------------------------- #

    @staticmethod
    def recorded(gp: FakeGphoto2) -> List[List[str]]:
        """Every argv the backend passed, read while the fixture is still live.

        :class:`FakeGphoto2` deletes its call log on exit and ``calls()`` then
        answers ``[]``, so an assertion written after the ``with`` block silently
        becomes one that cannot fail. Going through here turns that mistake into
        an error instead.
        """
        if not os.path.isdir(gp.store):
            raise AssertionError(
                "FakeGphoto2 has already been torn down; read the recorded "
                "calls inside the with-block or the assertion cannot fail."
            )
        return gp.calls()

    def calls_with(self, gp: FakeGphoto2, flag: str) -> List[List[str]]:
        return [call for call in self.recorded(gp) if flag in call]

    @staticmethod
    def value_after(call: Sequence[str], flag: str) -> str:
        """The argument following ``flag`` in a recorded argv."""
        index = list(call).index(flag)
        return call[index + 1]

    def assert_camera_pinned(self, gp: FakeGphoto2, call: Sequence[str]) -> None:
        """Every call that touches a camera must name *which* camera.

        gphoto2 remembers the last model/port it used in a settings file and
        silently reuses them when the flags are omitted; with two bodies on the
        bus an unpinned invocation talks to whichever enumerated first. Since
        this program erases photographs, that is the worst failure it could have.
        """
        self.assertIn("--port", call, "call does not pin the port: %r" % (call,))
        self.assertEqual(self.value_after(call, "--port"), gp.port)
        self.assertIn("--camera", call, "call does not pin the model: %r" % (call,))
        self.assertEqual(self.value_after(call, "--camera"), gp.model)


# --------------------------------------------------------------------------- #
# is_available
# --------------------------------------------------------------------------- #


class IsAvailableTests(GPhoto2TestCase):
    """The startup probe: fast, device-free, and never raising."""

    def test_reports_available_when_a_working_gphoto2_is_on_path(self) -> None:
        self.patch_module("_IS_MACOS", True)
        with FakeGphoto2() as gp:
            available, hint = GPhoto2Backend.is_available()
            calls = self.recorded(gp)

        self.assertTrue(available)
        self.assertEqual(hint, "")
        # The probe must be device-free: --version and nothing else. Enumerating
        # the USB bus at startup would stall the GUI before it is even drawn.
        self.assertEqual(calls, [["--version"]])

    def test_missing_binary_reports_unavailable_with_an_actionable_hint(self) -> None:
        # A missing dependency is a normal, fixable state — not an error. The
        # hint is shown verbatim in the environment panel, so it has to tell a
        # non-technical user what to do, not merely what is wrong.
        self.patch_module("_IS_MACOS", True)
        self.patch_env("PATH", self.dest("no-tools-here"))

        available, hint = GPhoto2Backend.is_available()

        self.assertFalse(available)
        self.assertTrue(hint.strip(), "an unavailable backend must explain itself")
        self.assertIn("gphoto2", hint)
        self.assertIn("install", hint.lower())
        self.assertTrue(hint.strip().endswith("."), "the hint must read as a sentence")
        # And the Install button needs something to run.
        self.assertIn("brew", GPhoto2Backend.install_hint())

    def test_installed_but_broken_binary_is_reported_unavailable(self) -> None:
        # The common case is a dylib that went missing across an OS upgrade.
        # Finding that out at startup is recoverable; finding it out halfway
        # through a rescue is not.
        self.patch_module("_IS_MACOS", True)
        bin_dir = self.dest("broken-bin")
        broken = os.path.join(bin_dir, "gphoto2")
        _write(broken, b"#!/bin/sh\nexit 1\n")
        os.chmod(broken, 0o755)
        self.patch_env("PATH", bin_dir)

        available, hint = GPhoto2Backend.is_available()

        self.assertFalse(available)
        self.assertIn(broken, hint)

    def test_windows_is_unavailable_even_with_a_binary_on_path(self) -> None:
        # No supported libgphoto2 build can claim USB on native Windows. Saying
        # so up front is what makes the registry fall through to WIA instead of
        # failing later with a cryptic I/O error.
        self.patch_module("_IS_WINDOWS", True)
        with FakeGphoto2() as gp:
            available, hint = GPhoto2Backend.is_available()
            calls = self.recorded(gp)

        self.assertFalse(available)
        self.assertTrue(hint.strip())
        self.assertEqual(calls, [], "Windows must not even spawn the binary")


# --------------------------------------------------------------------------- #
# detect
# --------------------------------------------------------------------------- #


class DetectTests(GPhoto2TestCase):
    """Parsing ``--auto-detect``, whose header is two lines and whose rows are
    a padded model followed by a port."""

    def test_model_containing_spaces_is_split_from_the_port(self) -> None:
        # 'Canon PowerShot S30' + 'usb:000,005'. A plain split() would produce
        # model='Canon' and port='PowerShot', and every later call would then
        # pin a camera that does not exist.
        progress = RecordingProgress()
        with FakeGphoto2(model="Canon PowerShot S30", port="usb:000,005") as gp:
            cameras = GPhoto2Backend().detect(progress)
            calls = self.recorded(gp)

        self.assertEqual(len(cameras), 1)
        self.assertEqual(cameras[0].model, "Canon PowerShot S30")
        self.assertEqual(cameras[0].port, "usb:000,005")
        self.assertEqual(cameras[0].kind, BackendKind.GPHOTO2)
        self.assertEqual(calls, [["--auto-detect"]])
        self.assertTrue(progress.saw_phase("detect"))

    def test_every_attached_camera_is_returned(self) -> None:
        # Two bodies on the bus is precisely when pinning --port starts to
        # matter, so losing one here would hide the case that needs it most.
        with TwoCameraGphoto2(model="Canon PowerShot S30", port="usb:000,005"):
            cameras = GPhoto2Backend().detect()

        self.assertEqual(
            [(c.model, c.port) for c in cameras],
            [("Canon PowerShot S30", "usb:000,005"), TwoCameraGphoto2.SECOND],
        )

    def test_nothing_attached_returns_an_empty_list_without_raising(self) -> None:
        # --auto-detect exits 0 and still prints its header when the bus is
        # empty, so the exit code says nothing. "No camera" is an answer, not a
        # failure: raising here would show an error dialog to someone who simply
        # has not plugged the camera in yet.
        progress = RecordingProgress()
        with FakeGphoto2(detect_empty=True):
            cameras = GPhoto2Backend().detect(progress)

        self.assertEqual(cameras, [])
        self.assertTrue(
            any("no camera" in m.lower() for m in progress.messages()),
            "the user must be told the bus looked empty: %r" % (progress.messages(),),
        )

    def test_a_failing_probe_raises_instead_of_reporting_no_camera(self) -> None:
        # A probe that could not run is a state the user must fix. Reporting it
        # as "no camera attached" would send them looking for a cable problem
        # they do not have.
        with FakeGphoto2(fail_with="No camera found"):
            with self.assertRaises(CameraNotFound) as caught:
                GPhoto2Backend().detect()

        message = str(caught.exception)
        self.assertIn("No camera is connected", message)
        self.assertNotIn("Traceback", message)


# --------------------------------------------------------------------------- #
# list_files
# --------------------------------------------------------------------------- #


class ListFilesTests(GPhoto2TestCase):
    """Listing must recurse, must be stably ordered, and must never hand
    verification a size it invented."""

    def test_parsable_listing_recurses_and_reports_exact_byte_sizes(self) -> None:
        progress = RecordingProgress()
        with ParsableGphoto2() as gp:
            files = GPhoto2Backend().list_files(self.camera(gp), progress)
            listing_calls = self.calls_with(gp, "-L")

        # Both Canon folders, sorted by (folder, name) so re-runs are
        # reproducible and the progress bar only ever moves forwards.
        self.assertEqual(
            [f.path for f in files],
            [
                "/DCIM/118CANON/IMG_0001.JPG",
                "/DCIM/118CANON/IMG_0002.JPG",
                "/DCIM/119CANON/IMG_0001.JPG",
            ],
        )

        # Exact byte counts, not approximations: this number is what the
        # download's completeness check and the resume check compare against.
        expected = [
            len(DEFAULT_CARD["118CANON"]["IMG_0001.JPG"]),
            len(DEFAULT_CARD["118CANON"]["IMG_0002.JPG"]),
            len(DEFAULT_CARD["119CANON"]["IMG_0001.JPG"]),
        ]
        self.assertEqual([f.size for f in files], expected)
        self.assertTrue(all(f.size_known for f in files))
        self.assertTrue(all(f.raw["source"] == "parsable" for f in files))
        self.assertTrue(all(f.mtime for f in files))

        self.assertEqual(len(listing_calls), 1, "one listing was enough")
        self.assert_camera_pinned(gp, listing_calls[0])
        self.assertIn("--parsable", listing_calls[0])
        self.assertTrue(progress.saw_phase("list"))

    def test_rounded_kb_listing_reports_size_unknown_never_the_rounded_value(
        self,
    ) -> None:
        # The human listing's KB column is a rounded display value. Letting it
        # reach verification would be worse than having no size at all: a
        # 1026-byte file listed as "1 KB" would either fail every check or, with
        # the wrong comparison, produce a *false match* on a different photo of
        # the same rounded size and green-light erasing it.
        with HumanOnlyGphoto2() as gp:
            files = GPhoto2Backend().list_files(self.camera(gp))
            parsable_attempts = self.calls_with(gp, "--parsable")
            listing_attempts = self.calls_with(gp, "-L")

        self.assertEqual(len(files), 3)
        real_sizes = {
            "/DCIM/118CANON/IMG_0001.JPG": len(
                DEFAULT_CARD["118CANON"]["IMG_0001.JPG"]
            ),
            "/DCIM/118CANON/IMG_0002.JPG": len(
                DEFAULT_CARD["118CANON"]["IMG_0002.JPG"]
            ),
            "/DCIM/119CANON/IMG_0001.JPG": len(
                DEFAULT_CARD["119CANON"]["IMG_0001.JPG"]
            ),
        }
        for cam_file in files:
            self.assertEqual(cam_file.size, -1)
            self.assertFalse(cam_file.size_known)
            # The rounded figure survives for display only.
            kb = cam_file.raw["kb"]
            self.assertGreater(kb, 0)
            self.assertEqual(kb, round(real_sizes[cam_file.path] / 1024))
            self.assertNotEqual(cam_file.size, kb)
            self.assertNotEqual(cam_file.size, kb * 1024)
            self.assertEqual(cam_file.raw["source"], "human")

        # The fallback chain really was walked: --parsable first, then -R, then
        # the human listing.
        self.assertEqual(len(parsable_attempts), 2)
        self.assertEqual(len(listing_attempts), 3)

    def test_an_unknown_size_still_downloads_rather_than_failing_the_check(
        self,
    ) -> None:
        # The other half of the rule above: because the rounded KB never becomes
        # a size, the completeness check has nothing to compare and the file is
        # rescued. If the rounded value ever leaked into CameraFile.size this
        # download would be rejected as "incomplete transfer".
        dest = self.dest()
        with HumanOnlyGphoto2() as gp:
            backend = GPhoto2Backend()
            files = backend.list_files(self.camera(gp))
            outcomes = backend.download(self.camera(gp), files[:1], dest)

        self.assertTrue(outcomes[0].ok, outcomes[0].error)
        self.assertEqual(
            _read(outcomes[0].dest_path), DEFAULT_CARD["118CANON"]["IMG_0001.JPG"]
        )


# --------------------------------------------------------------------------- #
# download
# --------------------------------------------------------------------------- #


class DownloadTests(GPhoto2TestCase):
    """One process per file, one outcome per file, no half-written results."""

    def test_files_are_fetched_per_file_through_a_part_name_with_exact_bytes(
        self,
    ) -> None:
        dest = self.dest()
        progress = RecordingProgress()
        with ParsableGphoto2() as gp:
            backend = GPhoto2Backend()
            camera = self.camera(gp)
            files = backend.list_files(camera)
            outcomes = backend.download(camera, files, dest, progress)
            get_calls = self.calls_with(gp, "--get-file")

        self.assertEqual(len(outcomes), len(files))
        self.assertTrue(all(o.ok for o in outcomes), [o.error for o in outcomes])

        # The same base name in two folders must not collapse into one file, and
        # each destination must hold *its own* photograph. Comparing bytes (not
        # just sizes) is what makes this assertion mean anything.
        by_device_path = {o.file.path: o for o in outcomes}
        for device_path, expected in (
            ("/DCIM/118CANON/IMG_0001.JPG", DEFAULT_CARD["118CANON"]["IMG_0001.JPG"]),
            ("/DCIM/118CANON/IMG_0002.JPG", DEFAULT_CARD["118CANON"]["IMG_0002.JPG"]),
            ("/DCIM/119CANON/IMG_0001.JPG", DEFAULT_CARD["119CANON"]["IMG_0001.JPG"]),
        ):
            outcome = by_device_path[device_path]
            self.assertEqual(_read(outcome.dest_path), expected, device_path)
        self.assertEqual(
            len({o.dest_path for o in outcomes}), 3, "one destination per photo"
        )
        self.assertEqual(
            os.path.basename(by_device_path["/DCIM/119CANON/IMG_0001.JPG"].dest_path),
            "119CANON_IMG_0001.JPG",
        )

        self.assertEqual(len(get_calls), 3, "one gphoto2 invocation per file")
        for call in get_calls:
            self.assert_camera_pinned(gp, call)
            self.assertIn("--folder", call)
            # The bytes land on a private hidden name inside the destination
            # directory and are renamed into place only once complete, so an
            # interrupted run can never be mistaken for a good file.
            filename = self.value_after(call, "--filename")
            self.assertEqual(os.path.dirname(filename), dest)
            base = os.path.basename(filename)
            self.assertTrue(base.startswith(".retrocam-"), base)
            self.assertTrue(base.endswith(".part"), base)

        # And nothing temporary survived the run: no .part file is left for a
        # later pass to mistake for a rescued photo.
        self.assertEqual(
            sorted(os.listdir(dest)),
            sorted(os.path.basename(o.dest_path) for o in outcomes),
        )
        self.assertTrue(progress.saw_phase("download"))

    def test_one_unreadable_file_does_not_abort_the_batch(self) -> None:
        # On a dying card the whole point is to rescue the photos that can still
        # be read. Stopping at the first unreadable one is the failure mode this
        # program exists to avoid.
        dest = self.dest()
        with ParsableGphoto2() as gp:
            backend = GPhoto2Backend()
            camera = self.camera(gp)
            files = backend.list_files(camera)
            ghost = CameraFile(
                folder="/DCIM/118CANON",
                name="IMG_9999.JPG",  # listed once, gone by the time we ask
                size=4096,
                mtime=1700000000.0,
            )
            batch = [files[0], ghost, files[2]]
            outcomes = backend.download(camera, batch, dest)

        # One outcome per input, in the same order: the "78 of 82 recovered"
        # report is built by zipping these against the request.
        self.assertEqual(len(outcomes), 3)
        self.assertEqual([o.file.path for o in outcomes], [f.path for f in batch])

        self.assertTrue(outcomes[0].ok)
        self.assertTrue(outcomes[2].ok)

        failed = outcomes[1]
        self.assertFalse(failed.ok)
        self.assertIsNone(failed.dest_path)
        self.assertTrue(failed.error.strip(), "a failure must say something")
        self.assertNotIn("Traceback", failed.error)

        # The failed attempt left no debris that a later run could mistake for
        # a real photo.
        self.assertEqual(
            sorted(os.listdir(dest)),
            sorted(os.path.basename(o.dest_path) for o in outcomes if o.ok),
        )

    def test_a_short_read_is_rejected_instead_of_reported_as_rescued(self) -> None:
        # The listing's exact byte count is the only defence against a transfer
        # that stopped early but exited 0. ok=True here would be handed straight
        # to the delete gate.
        dest = self.dest()
        with ParsableGphoto2() as gp:
            backend = GPhoto2Backend()
            camera = self.camera(gp)
            files = backend.list_files(camera)
            lying = dataclasses.replace(files[0], size=files[0].size + 5000)
            outcomes = backend.download(camera, [lying], dest)

        self.assertFalse(outcomes[0].ok)
        self.assertIsNone(outcomes[0].dest_path)
        self.assertIn("incomplete", outcomes[0].error.lower())
        self.assertEqual(os.listdir(dest), [], "the short file must not survive")

    def test_cancelling_keeps_what_was_rescued_and_leaves_no_partial_file(self) -> None:
        dest = self.dest()
        cancel = CancelToken()

        class _CancelAfterFirst(RecordingProgress):
            def __call__(self, tick: Any) -> None:
                super().__call__(tick)
                if tick.phase == "download" and tick.index >= 1:
                    cancel.cancel()

        progress = _CancelAfterFirst()
        with ParsableGphoto2() as gp:
            backend = GPhoto2Backend()
            camera = self.camera(gp)
            files = backend.list_files(camera)
            with self.assertRaises(TransferAborted) as caught:
                backend.download(camera, files, dest, progress, cancel)

        # A cancelled run never rolls back what it already rescued, and it can
        # still report it.
        partial = getattr(caught.exception, "outcomes", None)
        self.assertIsNotNone(partial, "cancellation must carry the partial results")
        self.assertEqual(len(partial), 1)
        self.assertTrue(partial[0].ok)
        self.assertEqual(
            _read(partial[0].dest_path), DEFAULT_CARD["118CANON"]["IMG_0001.JPG"]
        )
        self.assertEqual(os.listdir(dest), [os.path.basename(partial[0].dest_path)])


class SkipExistingTests(GPhoto2TestCase):
    """Resume. The one place where a wrong answer erases a photograph."""

    def test_a_second_folders_file_is_never_credited_to_the_first_ones_copy(
        self,
    ) -> None:
        # Review finding F2. 118CANON/IMG_0001.JPG and 119CANON/IMG_0001.JPG are
        # different photographs of identical size. Once the first has been
        # written as the flat 'IMG_0001.JPG', a resume check that only looks at
        # name + size + mtime matches the second one against it, reports it as
        # already rescued, and the delete gate then erases a photo that was
        # never downloaded. Only the batch's 'claimed' bookkeeping prevents it.
        dest = self.dest()
        first_bytes = CARD_WITH_DUPLICATE_NAMES["118CANON"]["IMG_0001.JPG"]
        second_bytes = CARD_WITH_DUPLICATE_NAMES["119CANON"]["IMG_0001.JPG"]
        self.assertEqual(len(first_bytes), len(second_bytes))
        self.assertNotEqual(first_bytes, second_bytes)

        with ParsableGphoto2(card=CARD_WITH_DUPLICATE_NAMES) as gp:
            backend = GPhoto2Backend()
            camera = self.camera(gp)
            files = backend.list_files(camera)
            self.assertEqual(
                [f.path for f in files],
                ["/DCIM/118CANON/IMG_0001.JPG", "/DCIM/119CANON/IMG_0001.JPG"],
            )

            # An interrupted earlier run already fetched the first photo, with
            # the camera's timestamp on it, exactly as gphoto2 leaves it.
            already_there = os.path.join(dest, "IMG_0001.JPG")
            _write(already_there, first_bytes)
            os.utime(already_there, (files[0].mtime, files[0].mtime))

            outcomes = backend.download(camera, files, dest, skip_existing=True)
            get_calls = self.calls_with(gp, "--get-file")

        first, second = outcomes

        # The genuine resume is honoured: no second trip over USB 1.1 for it.
        self.assertTrue(first.skipped)
        self.assertTrue(first.ok)
        self.assertEqual(first.dest_path, already_there)

        # The impostor is not.
        self.assertFalse(
            second.skipped,
            "119CANON's photo was credited to 118CANON's copy — F2 has regressed",
        )
        self.assertTrue(second.ok, second.error)
        self.assertNotEqual(second.dest_path, already_there)
        self.assertEqual(_read(second.dest_path), second_bytes)
        self.assertEqual(_read(already_there), first_bytes, "the first copy is intact")

        # And the proof from the wire: gphoto2 was asked for the second folder
        # and only the second folder.
        self.assertEqual(len(get_calls), 1)
        self.assertEqual(self.value_after(get_calls[0], "--folder"), "/DCIM/119CANON")

    def test_no_skip_when_the_camera_reports_no_timestamp(self) -> None:
        # A matching size on its own is one fact, and one fact is not enough to
        # justify reporting a file as rescued when that flag authorises an
        # irreversible delete. Re-reading costs seconds; being wrong costs the
        # photograph.
        dest = self.dest()
        with ParsableGphoto2() as gp:
            backend = GPhoto2Backend()
            camera = self.camera(gp)
            listed = backend.list_files(camera)[0]
            undated = dataclasses.replace(listed, mtime=None)

            # A byte-perfect copy is already on disk, with a plausible mtime.
            decoy = os.path.join(dest, "IMG_0001.JPG")
            _write(decoy, DEFAULT_CARD["118CANON"]["IMG_0001.JPG"])

            outcomes = backend.download(camera, [undated], dest, skip_existing=True)
            get_calls = self.calls_with(gp, "--get-file")

        self.assertFalse(outcomes[0].skipped, "an undated file must be re-read")
        self.assertTrue(outcomes[0].ok, outcomes[0].error)
        self.assertEqual(len(get_calls), 1, "the camera really was asked for it")
        self.assertNotEqual(outcomes[0].dest_path, decoy)
        self.assertEqual(
            os.path.basename(outcomes[0].dest_path), "118CANON_IMG_0001.JPG"
        )


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #


class DeleteTests(GPhoto2TestCase):
    """The irreversible half. Everything here is about blast radius."""

    def test_deletion_is_one_pinned_call_per_file_and_erases_only_those(
        self,
    ) -> None:
        with ParsableGphoto2() as gp:
            backend = GPhoto2Backend()
            camera = self.camera(gp)
            files = backend.list_files(camera)
            # Erase one file out of a folder that holds two: a backend that
            # deleted the folder, or that recursed, would take the other with it.
            outcomes = backend.delete(camera, [files[0]])
            remaining = backend.list_files(camera)
            delete_calls = self.calls_with(gp, "--delete-file")

        self.assertEqual(len(outcomes), 1)
        self.assertTrue(outcomes[0].ok, outcomes[0].error)

        self.assertEqual(len(delete_calls), 1)
        call = delete_calls[0]
        self.assert_camera_pinned(gp, call)
        self.assertEqual(self.value_after(call, "--folder"), "/DCIM/118CANON")
        self.assertEqual(self.value_after(call, "--delete-file"), "IMG_0001.JPG")

        # Reality, not the exit code: the other two photographs are still there.
        self.assertEqual(
            [f.path for f in remaining],
            [f.path for f in files if f.path != "/DCIM/118CANON/IMG_0001.JPG"],
        )

    def test_deletion_never_uses_a_bulk_or_recursive_flag(self) -> None:
        # A listing mismatch must stay a one-file problem. --delete-all-files or
        # a recursive delete turns it into total loss, so these flags must not
        # appear anywhere near a delete — however many files were requested, and
        # however many folders they span.
        forbidden = {"--delete-all-files", "-R", "--recurse", "-r"}
        with ParsableGphoto2() as gp:
            backend = GPhoto2Backend()
            camera = self.camera(gp)
            files = backend.list_files(camera)
            backend.delete(camera, files)  # every file, across both folders
            all_calls = gp.calls()

        delete_calls = [c for c in all_calls if "--delete-file" in c]
        self.assertEqual(len(delete_calls), 3, "one invocation per file, never a batch")
        for call in delete_calls:
            self.assertEqual(call.count("--delete-file"), 1)
            self.assertIn("--folder", call)
            self.assertFalse(
                forbidden.intersection(call), "bulk flag in a delete: %r" % (call,)
            )

        # Belt and braces: the bulk flag never appears in *any* call this
        # backend made, not just in the ones we recognised as deletes.
        for call in all_calls:
            self.assertNotIn("--delete-all-files", call)

    def test_a_refused_delete_is_reported_per_file_and_never_raised(self) -> None:
        # A write-protected card or an in-camera protected photo makes an
        # individual delete fail. That is a per-file outcome, not a reason to
        # abandon the batch or to show a stack trace.
        with FakeGphoto2(
            fail_with="*** Error: Could not claim the USB device ***"
        ) as gp:
            backend = GPhoto2Backend()
            camera = self.camera(gp)
            targets = [
                CameraFile(folder="/DCIM/118CANON", name="IMG_0001.JPG", size=1026),
                CameraFile(folder="/DCIM/118CANON", name="IMG_0002.JPG", size=2026),
            ]
            outcomes = backend.delete(camera, targets)

        self.assertEqual(len(outcomes), 2)
        for outcome in outcomes:
            self.assertFalse(outcome.ok)
            self.assertIn("holding the camera", outcome.error)
            self.assertNotIn("Traceback", outcome.error)

    def test_an_unconfirmable_deletion_is_flagged_rather_than_asserted(self) -> None:
        # gphoto2 exited 0, but the card could not be re-read to prove it. The
        # backend keeps the verdict and says so: "probably deleted" must never
        # be reported as deleted without qualification.
        with HumanOnlyGphoto2() as gp:
            backend = GPhoto2Backend()
            camera = self.camera(gp)
            target = CameraFile(folder="/DCIM/118CANON", name="IMG_0001.JPG", size=1026)
            outcomes = backend.delete(camera, [target])

        self.assertTrue(outcomes[0].ok)
        self.assertTrue(
            outcomes[0].error.strip(),
            "an unconfirmed deletion must carry a caveat",
        )
        self.assertIn("confirm", outcomes[0].error.lower())

    def test_a_file_absent_from_the_card_is_reported_as_deleted(self) -> None:
        # NOTE: gphoto2 exits non-zero here ("File not found"), yet the outcome
        # is ok=True. That is deliberate and documented: the re-listing is
        # treated as the authority, and absence from the card is the only real
        # proof a file is gone. It also disambiguates the Canon driver's habit
        # of reporting a refused delete and an already-deleted file with the
        # same -108 code.
        #
        # The dangerous direction is the other one — claiming success while the
        # file is still on the card — and that is what the re-listing exists to
        # catch. Asserting the documented behaviour here so that any change to
        # it is a deliberate act.
        with ParsableGphoto2() as gp:
            backend = GPhoto2Backend()
            camera = self.camera(gp)
            ghost = CameraFile(folder="/DCIM/118CANON", name="IMG_4242.JPG", size=1024)
            outcomes = backend.delete(camera, [ghost])
            delete_calls = self.calls_with(gp, "--delete-file")

        self.assertEqual(len(delete_calls), 1)
        self.assertTrue(
            outcomes[0].ok,
            "absence from the re-listing is what decides the verdict",
        )

    def test_a_file_still_on_the_camera_is_never_reported_as_erased(self) -> None:
        """The dangerous direction of the test above, and the reason it exists.

        ``delete()`` promises that "a file gphoto2 claimed to delete but that is
        still on the card is reported as *not* deleted". That promise is the
        entire point of the re-listing: on the Canon driver a *refused* delete
        can surface as ``-108 File not found``, the same code as "already gone",
        so the exit code cannot tell an erase from a refusal.

        Until now only the harmless direction was pinned — a file that really is
        gone being reported as deleted. Sabotaging the ``still_there`` check so
        that every file is declared erased left the whole suite green, so nothing
        was protecting the direction that actually costs something.

        It costs something because the caller believes the verdict: ``ok=True``
        is what the GUI counts into "42 photo(s) erased", and someone who has
        been told the card was cleared has no reason to check it before
        reformatting.
        """
        with LyingDeleteGphoto2() as gp:
            backend = GPhoto2Backend()
            camera = self.camera(gp)
            files = backend.list_files(camera)
            target = files[0]
            outcomes = backend.delete(camera, [target])
            # Reality, read back off the camera with a second listing.
            remaining = backend.list_files(camera)
            delete_calls = self.calls_with(gp, "--delete-file")

        self.assertEqual(len(delete_calls), 1, "the delete was never attempted")
        self.assertIn(
            target.path,
            [f.path for f in remaining],
            "premise: this camera keeps the file while exiting 0",
        )

        self.assertEqual(len(outcomes), 1)
        self.assertFalse(
            outcomes[0].ok,
            "a file still on the camera was reported as erased on the strength "
            "of the exit code",
        )
        self.assertIn("still on the camera", outcomes[0].error)


# --------------------------------------------------------------------------- #
# macOS device release
# --------------------------------------------------------------------------- #


class DeviceReleaseTests(GPhoto2TestCase):
    """macOS auto-launches a PTP daemon that holds the USB interface."""

    def test_macos_releases_the_ptp_daemon_before_touching_the_camera(self) -> None:
        # If ptpcamerad still owns the interface, libgphoto2 fails with
        # -53 and the rescue never starts. Both daemon names are tried because
        # the name changed in macOS 13.
        self.patch_module("_IS_MACOS", True)
        progress = RecordingProgress()
        with FakeGphoto2() as gp:
            # Sampled at each killall: how many times gphoto2 had been spawned.
            self.shim.witness = lambda: len(gp.calls())
            GPhoto2Backend().detect(progress)

        self.assertEqual(self.shim.killed_names(), ["ptpcamerad", "PTPCamera"])
        self.assertEqual(
            [seen for _, seen in self.shim.killed],
            [0, 0],
            "the device must be released before gphoto2 is spawned, not after",
        )
        self.assertTrue(
            any("release" in m.lower() for m in progress.messages()),
            "the user is told, because the daemon coming back is a real cause "
            "of repeat failures: %r" % (progress.messages(),),
        )

    def test_the_first_release_is_never_suppressed_by_the_throttle(self) -> None:
        """The throttle must not swallow the release that actually matters.

        ``_release_device`` rate-limits itself so a 300-photo batch does not
        respawn ``killall`` per file. The "when did we last release" sentinel
        used to be ``0.0``, which assumes ``time.monotonic()`` starts far from
        zero. On Apple's Python 3.9 -- the floor this project declares -- it
        starts at process start instead, so ``now - 0.0`` was under the five
        second throttle for the whole first five seconds of the program: the
        release was skipped exactly when the user clicks Detect, ``ptpcamerad``
        kept the USB interface, and libgphoto2 failed with "-53 Could not claim
        the USB device" -- the precise failure this code exists to prevent.

        The clock is faked rather than left to the interpreter so that this is
        caught on every build, not only on the one where the bug happens to
        reproduce.
        """
        self.patch_module("_IS_MACOS", True)
        self.patch_module("time", _ProcessStartClock(0.01))

        with FakeGphoto2():
            GPhoto2Backend().detect()

        self.assertEqual(
            self.shim.killed_names(),
            ["ptpcamerad", "PTPCamera"],
            "the first release was suppressed by the throttle",
        )

    def test_the_throttle_still_suppresses_a_rapid_second_release(self) -> None:
        """The control for the test above: the fix must not disable throttling.

        Two detections a fraction of a second apart share one release; the same
        pair five seconds apart do not. Without this, "never suppress" could be
        satisfied by removing the throttle altogether and respawning ``killall``
        for every file of a 300-photo batch.
        """
        self.patch_module("_IS_MACOS", True)
        clock = _ProcessStartClock(0.01)
        self.patch_module("time", clock)
        backend = GPhoto2Backend()

        with FakeGphoto2():
            backend.detect()
            clock.advance(0.5)  # well inside the throttle window
            backend.detect()
            self.assertEqual(
                self.shim.killed_names(),
                ["ptpcamerad", "PTPCamera"],
                "the throttle stopped working",
            )

            clock.advance(gp2._RELEASE_THROTTLE_SECONDS + 0.1)
            backend.detect()

        self.assertEqual(self.shim.killed_names(), ["ptpcamerad", "PTPCamera"] * 2)

    def test_no_release_is_attempted_off_macos(self) -> None:
        # Nothing to release, and killall may not even exist.
        self.patch_module("_IS_MACOS", False)
        with FakeGphoto2():
            GPhoto2Backend().detect()

        self.assertEqual(self.shim.killed, [])

    def test_a_claim_conflict_releases_the_device_and_retries_once(self) -> None:
        # The race the retry exists for: launchd restarts ptpcamerad between our
        # release and our claim. One kill-and-retry turns a user-visible failure
        # into a hiccup — but it must be *one*, not a loop.
        self.patch_module("_IS_MACOS", True)
        with FakeGphoto2(
            fail_with="*** Error: Could not claim the USB device ***"
        ) as gp:
            with self.assertRaises(CameraError) as caught:
                GPhoto2Backend().detect()
            attempts = self.calls_with(gp, "--auto-detect")

        self.assertEqual(len(attempts), 2, "exactly one retry")
        self.assertEqual(self.shim.killed_names(), ["ptpcamerad", "PTPCamera"] * 2)

        # The message the user sees names the programs to close, not an errno.
        message = str(caught.exception)
        self.assertIn("Another program is holding the camera", message)
        self.assertIn("Image Capture", message)
        self.assertNotIn("Traceback", message)


# --------------------------------------------------------------------------- #
# Static guarantees about how processes are started
# --------------------------------------------------------------------------- #


class ProcessSafetyTests(unittest.TestCase):
    """Camera-supplied file names end up in these arguments.

    A name like ``; rm -rf ~`` is a perfectly legal thing for a twenty-year-old
    camera to have on its card, so no invocation may reach a shell and no argv
    may be built by string concatenation. Asserted on the source itself, because
    that is the only way to cover every call site including the ones a given test
    run never takes.
    """

    _SPAWNERS = {"run", "Popen", "call", "check_call", "check_output"}

    def _calls(self) -> List[ast.Call]:
        tree = ast.parse(inspect.getsource(gp2))
        return [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in self._SPAWNERS
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
        ]

    def test_every_subprocess_call_passes_an_argv_list_and_no_shell(self) -> None:
        calls = self._calls()
        # If this drops to zero the test has stopped testing anything.
        self.assertGreaterEqual(len(calls), 3, "no subprocess call sites found")

        for call in calls:
            where = "line %d" % call.lineno
            for keyword in call.keywords:
                self.assertNotEqual(keyword.arg, "shell", "shell= passed at %s" % where)
            self.assertTrue(call.args, "no positional argv at %s" % where)
            argv = call.args[0]
            self.assertNotIsInstance(
                argv, (ast.JoinedStr, ast.BinOp), "argv built as a string at %s" % where
            )
            if isinstance(argv, ast.Constant):
                self.fail("argv is a bare string constant at %s" % where)

    def test_the_backend_never_reaches_a_shell_by_another_route(self) -> None:
        # subprocess is not the only way to hand a camera-supplied name to
        # /bin/sh. Matched on the syntax tree rather than on the text, because
        # this module discusses ``shell=True`` in its own docstrings.
        forbidden = {("os", "system"), ("os", "popen"), ("commands", "getoutput")}
        tree = ast.parse(inspect.getsource(gp2))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and (func.value.id, func.attr) in forbidden
            ):
                self.fail(
                    "%s.%s used at line %d" % (func.value.id, func.attr, node.lineno)
                )


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
