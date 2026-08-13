"""Tests for :mod:`retrocam.registry` and :mod:`retrocam.deps`.

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests

These two modules run before the user has done anything at all: the registry
probes every transport and the deps panel probes every optional tool while the
GUI is still painting its first window. Two promises therefore have to hold
absolutely, and both are asserted here rather than assumed:

* **Nothing may raise out of a probe.** A backend with a bad import, a tool that
  answers with garbage, a ``HOME`` that does not exist — each of these must
  degrade to "unavailable, here is why", never to a traceback in front of
  someone whose only copy of a photograph is on a 20-year-old card.
* **Nothing may touch the network before an explicit Install.** The README says
  so, so :class:`_SocketTripwire` watches every in-process socket call during
  :func:`retrocam.deps.check_all` and the process spy asserts the only child
  processes started are version queries.

The registry tests drive throwaway :class:`~retrocam.backends.base.CameraBackend`
subclasses (see :func:`_backend`) rather than the real three: what is under test
is the registry's own contract — order, isolation between backends, and the
de-duplication heuristic — and using scripted backends is the only way to make
"gphoto2 exploded but the card reader still worked" reproducible on a machine
with no camera attached. The real three are still exercised directly wherever
the assertion is about them (order, and that their real ``is_available`` probes
answer without raising on this platform).

Nothing here spawns an installer. Every :func:`retrocam.deps.install` call in
this file is either an unknown key or is pinned to a platform branch that
returns instructions, and a recorder asserts no child process was started.
"""

from __future__ import annotations

import ast
import dataclasses
import os
import socket
import subprocess
import sys
import time
import unittest
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple, Type
from unittest import mock

# The package lives in src/ and is not installed while the suite runs from a
# checkout. Derived from this file's location rather than the cwd, so discovery
# works from anywhere. The tests directory itself goes on the path too, so
# ``helpers`` imports whether or not the runner already put it there.
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.join(os.path.dirname(_TESTS_DIR), "src")
for _entry in (_SRC_DIR, _TESTS_DIR):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from helpers import RecordingProgress, TempDirCase  # noqa: E402

from retrocam import deps, registry  # noqa: E402
from retrocam.backends.base import CameraBackend, noop_progress  # noqa: E402
from retrocam.backends.gphoto2_backend import GPhoto2Backend  # noqa: E402
from retrocam.backends.massstorage import MassStorageBackend  # noqa: E402
from retrocam.backends.wia import WiaBackend  # noqa: E402
from retrocam.model import (  # noqa: E402
    BackendKind,
    CameraInfo,
    CameraNotFound,
    Progress,
    TransferAborted,
)


# --------------------------------------------------------------------------- #
# Scripted backends
# --------------------------------------------------------------------------- #


class _FakeBackend(CameraBackend):
    """Base for the throwaway backends the registry tests drive.

    Only what the registry actually calls is implemented for real
    (``is_available`` and ``detect``); the transfer methods exist because the
    ABC demands them and would fail instantiation otherwise, which is itself
    part of the contract being tested — the registry instantiates backends.
    """

    kind = BackendKind.MASS_STORAGE
    display_name = "Fake backend"

    # Class-level scripting, filled in by :func:`_backend`.
    _cameras: Tuple[CameraInfo, ...] = ()
    _available: Any = (True, "")
    _available_raises: Optional[BaseException] = None
    _detect_raises: Optional[BaseException] = None
    instances: List["_FakeBackend"] = []

    def __init__(self) -> None:
        type(self).instances.append(self)

    @classmethod
    def is_available(cls) -> Any:
        if cls._available_raises is not None:
            raise cls._available_raises
        return cls._available

    def detect(self, progress: Any = noop_progress) -> List[CameraInfo]:
        # Real backends report progress while they scan. Doing the same here is
        # what gives "a progress sink that raises cannot kill detection" teeth.
        progress(Progress(phase="detect", message="%s scanning" % self.display_name))
        if type(self)._detect_raises is not None:
            raise type(self)._detect_raises
        return list(type(self)._cameras)

    def list_files(self, camera, progress=noop_progress, cancel=None):  # type: ignore[no-untyped-def]
        return []

    def download(  # type: ignore[no-untyped-def]
        self,
        camera,
        files,
        dest_dir,
        progress=noop_progress,
        cancel=None,
        skip_existing=True,
    ):
        return []

    def delete(self, camera, files, progress=noop_progress, cancel=None):  # type: ignore[no-untyped-def]
        return []


def _backend(
    name: str,
    kind: BackendKind = BackendKind.MASS_STORAGE,
    cameras: Sequence[CameraInfo] = (),
    available: Any = (True, ""),
    detect_raises: Optional[BaseException] = None,
    available_raises: Optional[BaseException] = None,
) -> Type[_FakeBackend]:
    """Build a fresh scripted backend class.

    A new class per call, so the ``instances`` list and the scripted answers
    cannot leak between tests.
    """
    attrs: Dict[str, Any] = {
        "kind": kind,
        "display_name": name,
        "_cameras": tuple(cameras),
        "_available": available,
        "_available_raises": available_raises,
        "_detect_raises": detect_raises,
        "instances": [],
    }
    return type(name.replace(" ", "_"), (_FakeBackend,), attrs)


def _camera(model: str, kind: BackendKind, port: str = "port0") -> CameraInfo:
    return CameraInfo(model=model, port=port, kind=kind)


# --------------------------------------------------------------------------- #
# registry: order and availability
# --------------------------------------------------------------------------- #


class RegistryOrderTests(unittest.TestCase):
    """The order of ``ALL_BACKENDS`` is a safety decision, not a style one."""

    def test_mass_storage_is_tried_before_the_driver_dependent_transports(self) -> None:
        """Mass storage first, then gphoto2, then WIA — exactly.

        Mass storage needs no driver, no daemon and no elevation: a card in a
        reader is read with plain ``open()``. It must therefore be attempted
        first, and it must also win the de-duplication (see
        :class:`DetectAllDeduplicationTests`), which only works if it is
        genuinely first in this list.
        """
        self.assertEqual(
            registry.ALL_BACKENDS,
            [MassStorageBackend, GPhoto2Backend, WiaBackend],
        )

    def test_every_registered_backend_implements_the_backend_contract(self) -> None:
        """A registry entry the GUI cannot instantiate is a startup crash."""
        for backend_cls in registry.ALL_BACKENDS:
            with self.subTest(backend=backend_cls.__name__):
                self.assertTrue(issubclass(backend_cls, CameraBackend))
                self.assertIsInstance(backend_cls.kind, BackendKind)
                self.assertTrue(backend_cls.display_name.strip())

    def test_each_backend_declares_a_distinct_kind(self) -> None:
        """``CameraInfo.kind`` routes a camera back to its backend.

        Two backends sharing a kind would make that routing ambiguous, and
        would also silently disable de-duplication between them (entries of the
        same kind are never merged).
        """
        kinds = [backend_cls.kind for backend_cls in registry.ALL_BACKENDS]
        self.assertEqual(len(kinds), len(set(kinds)))


class BackendStatusTests(unittest.TestCase):
    """``backend_status`` feeds the environment panel and must never raise."""

    def test_status_reports_one_triple_per_backend_in_registry_order(self) -> None:
        """Real probes, real platform: shape and order are as documented."""
        statuses = registry.backend_status()
        self.assertEqual(len(statuses), len(registry.ALL_BACKENDS))
        for (backend_cls, ok, hint), expected in zip(statuses, registry.ALL_BACKENDS):
            with self.subTest(backend=expected.__name__):
                self.assertIs(backend_cls, expected)
                self.assertIsInstance(ok, bool)
                self.assertIsInstance(hint, str)

    def test_an_unavailable_backend_still_explains_itself(self) -> None:
        """The hints are the instructions for fixing the environment.

        An unavailable transport with an empty hint teaches the user that the
        panel is noise. Whichever backends are unavailable on this machine, each
        one has to say why.
        """
        for backend_cls, ok, hint in registry.backend_status():
            if not ok:
                with self.subTest(backend=backend_cls.__name__):
                    self.assertTrue(hint.strip(), "no hint for an unavailable backend")

    def test_a_probe_that_raises_is_reported_as_unavailable_not_propagated(
        self,
    ) -> None:
        """One backend with a bad import must not stop the other two.

        ``is_available`` is contractually forbidden from raising, but this runs
        at startup for the whole application, so the registry defends against it
        anyway. The failure has to survive as a *visible* hint naming the
        backend — a silent False would hide a broken install forever.
        """
        exploding = _backend("Exploding", available_raises=RuntimeError("bad ctypes"))
        healthy = _backend("Healthy", kind=BackendKind.GPHOTO2)

        with mock.patch.object(registry, "ALL_BACKENDS", [exploding, healthy]):
            statuses = registry.backend_status()
            available = registry.available_backends()

        self.assertEqual([cls for cls, _ok, _hint in statuses], [exploding, healthy])
        self.assertFalse(statuses[0][1])
        self.assertIn("Exploding", statuses[0][2])
        self.assertIn("bad ctypes", statuses[0][2])
        self.assertEqual(available, [healthy])

    def test_a_probe_that_ignores_the_tuple_contract_is_coerced(self) -> None:
        """A backend returning a bare bool is normalised, not crashed on."""
        sloppy = _backend("Sloppy", available=True)
        with mock.patch.object(registry, "ALL_BACKENDS", [sloppy]):
            statuses = registry.backend_status()

        self.assertEqual(len(statuses), 1)
        self.assertIs(statuses[0][1], True)
        self.assertEqual(statuses[0][2], "")

    def test_available_backends_is_an_ordered_subset_of_all_backends(self) -> None:
        """Real probes: availability filters the registry, it never reorders it."""
        available = registry.available_backends()
        self.assertLessEqual(set(available), set(registry.ALL_BACKENDS))

        positions = [registry.ALL_BACKENDS.index(cls) for cls in available]
        self.assertEqual(positions, sorted(positions))

        # Mass storage has nothing to probe and nothing to install, so it is
        # available on every platform this project supports. If this ever fails,
        # the app has lost the one transport that always works.
        self.assertIn(MassStorageBackend, available)

    def test_available_backends_agrees_with_backend_status(self) -> None:
        """The two public views must not be able to disagree."""
        with mock.patch.object(
            registry,
            "ALL_BACKENDS",
            [
                _backend("Yes", available=(True, "")),
                _backend("No", kind=BackendKind.WIA, available=(False, "missing tool")),
            ],
        ):
            statuses = registry.backend_status()
            available = registry.available_backends()

        self.assertEqual([cls for cls, ok, _hint in statuses if ok], available)

    def test_status_never_raises_when_the_platform_looks_like_windows(self) -> None:
        """ "Whatever the platform" includes one this machine is not running.

        ``WiaBackend.is_available`` reads ``sys.platform`` at call time and then
        tries to import pywin32; forcing the Windows branch on macOS exercises
        the import-failure path that a real Windows box without pywin32 would
        take. It must answer, not raise.
        """
        for platform in ("win32", "linux", "darwin"):
            with self.subTest(platform=platform):
                with mock.patch.object(sys, "platform", platform):
                    statuses = registry.backend_status()
                self.assertEqual(len(statuses), len(registry.ALL_BACKENDS))
                for _cls, ok, hint in statuses:
                    self.assertIsInstance(ok, bool)
                    self.assertIsInstance(hint, str)


# --------------------------------------------------------------------------- #
# registry: detection
# --------------------------------------------------------------------------- #


class DetectAllTests(unittest.TestCase):
    """One transport failing must never cost the user another transport's card."""

    def test_a_camera_error_in_one_backend_does_not_hide_another_s_cameras(
        self,
    ) -> None:
        """The headline promise of ``detect_all``.

        The failing backend is deliberately *first*: this asserts both that its
        error is survivable and that detection carries on to the backends after
        it. The failure has to reach the log as well — a camera that silently
        never appears is the most frustrating answer this app could give.
        """
        card = _camera("Removable drive", BackendKind.MASS_STORAGE, "/Volumes/CARD")
        broken = _backend(
            "gphoto2",
            kind=BackendKind.GPHOTO2,
            detect_raises=CameraNotFound("could not claim the camera"),
        )
        working = _backend("Card reader", cameras=[card])

        progress = RecordingProgress()
        with mock.patch.object(registry, "ALL_BACKENDS", [broken, working]):
            results = registry.detect_all(progress)

        self.assertEqual([info for _backend_obj, info in results], [card])
        self.assertTrue(
            any(
                "gphoto2" in message and "could not claim the camera" in message
                for message in progress.messages()
            ),
            "the failure was never reported: %r" % (progress.messages(),),
        )

    def test_an_unexpected_exception_in_one_backend_is_contained(self) -> None:
        """A backend *bug* (bad ctypes call, COM error) is not a CameraError.

        It must be caught just as thoroughly, because the results of the
        transports that did work are the whole point of the rescue.
        """
        card = _camera("Canon PowerShot S30", BackendKind.MASS_STORAGE)
        buggy = _backend(
            "WIA", kind=BackendKind.WIA, detect_raises=AttributeError("no such member")
        )
        working = _backend("Card reader", cameras=[card])

        progress = RecordingProgress()
        with mock.patch.object(registry, "ALL_BACKENDS", [buggy, working]):
            results = registry.detect_all(progress)

        self.assertEqual([info for _backend_obj, info in results], [card])
        self.assertTrue(
            any("unexpected failure" in message for message in progress.messages()),
            "an unexpected backend failure was swallowed silently",
        )

    def test_cancellation_is_re_raised_rather_than_reported(self) -> None:
        """``TransferAborted`` is a user instruction, not a backend failure.

        Swallowing it like any other :class:`CameraError` would make Cancel look
        broken: the GUI would keep probing the remaining transports after the
        user asked it to stop.
        """
        cancelled = _backend(
            "Cancelling", detect_raises=TransferAborted("cancelled by the user")
        )
        never_reached = _backend(
            "Never reached",
            kind=BackendKind.GPHOTO2,
            cameras=[_camera("Canon PowerShot S30", BackendKind.GPHOTO2)],
        )

        with mock.patch.object(registry, "ALL_BACKENDS", [cancelled, never_reached]):
            with self.assertRaises(TransferAborted):
                registry.detect_all(RecordingProgress())

        self.assertEqual(never_reached.instances, [])

    def test_one_progress_tick_is_emitted_for_every_backend_probed(self) -> None:
        """Silence reads as "my camera is broken" when a tool is merely missing.

        Every backend must be accounted for: the ones that were tried, by name,
        and the ones that were skipped, with the reason.
        """
        first = _backend("First", cameras=[_camera("Alpha", BackendKind.MASS_STORAGE)])
        second = _backend("Second", kind=BackendKind.GPHOTO2)
        skipped = _backend(
            "Skipped",
            kind=BackendKind.WIA,
            available=(False, "pywin32 is not installed"),
        )

        progress = RecordingProgress()
        with mock.patch.object(registry, "ALL_BACKENDS", [first, second, skipped]):
            registry.detect_all(progress)

        self.assertEqual(set(progress.phases()), {"detect"})
        messages = progress.messages()
        for name in ("First", "Second", "Skipped"):
            with self.subTest(backend=name):
                self.assertTrue(
                    any(name in message for message in messages),
                    "no progress mentioned %s: %r" % (name, messages),
                )
        self.assertTrue(
            any(
                "Skipped" in message
                and "not available" in message
                and "pywin32 is not installed" in message
                for message in messages
            ),
            "the skipped backend did not explain itself: %r" % (messages,),
        )
        # The unavailable backend must not have been constructed or probed.
        self.assertEqual(skipped.instances, [])

    def test_detection_ends_with_a_count_the_user_can_read(self) -> None:
        """The last tick reports the total, so an empty result is explained."""
        empty = _backend("Empty")
        progress = RecordingProgress()
        with mock.patch.object(registry, "ALL_BACKENDS", [empty]):
            results = registry.detect_all(progress)

        self.assertEqual(results, [])
        self.assertIn(
            "Detection finished — 0 device(s) found.",
            progress.messages(),
        )

    def test_each_camera_comes_back_with_the_instance_that_found_it(self) -> None:
        """The live backend instance is the only object that can service it.

        ``CameraInfo`` alone is not enough to download or delete: the pairing is
        the API, so an instance that is merely of the right *class* is not good
        enough — it has to be the one whose ``detect`` produced the camera.
        """
        one = _camera("Alpha", BackendKind.MASS_STORAGE)
        two = _camera("Beta", BackendKind.GPHOTO2)
        first = _backend("First", cameras=[one])
        second = _backend("Second", kind=BackendKind.GPHOTO2, cameras=[two])

        with mock.patch.object(registry, "ALL_BACKENDS", [first, second]):
            results = registry.detect_all()

        self.assertEqual([info for _b, info in results], [one, two])
        self.assertIs(results[0][0], first.instances[0])
        self.assertIs(results[1][0], second.instances[0])

    def test_a_progress_sink_that_raises_cannot_make_a_camera_vanish(self) -> None:
        """The far end of the callback is the GUI's queue.

        A full queue or a Tk teardown race must not be able to abort detection —
        the sink handed to backends is guarded, and so is every tick the registry
        emits itself.
        """

        def hostile(_item: Progress) -> None:
            raise RuntimeError("the log pane exploded")

        card = _camera("Canon PowerShot S30", BackendKind.MASS_STORAGE)
        with mock.patch.object(
            registry, "ALL_BACKENDS", [_backend("Card reader", cameras=[card])]
        ):
            results = registry.detect_all(hostile)

        self.assertEqual([info for _b, info in results], [card])

    def test_a_backend_returning_none_is_tolerated(self) -> None:
        """``detect`` should return a list; ``None`` must not crash the scan.

        A third-party backend that forgets the return statement is a plausible
        contribution, and losing the other transports over it is not acceptable.
        """
        sloppy = _backend("Sloppy")
        sloppy.detect = lambda self, progress=noop_progress: None  # type: ignore[assignment]
        card = _camera("Canon PowerShot S30", BackendKind.GPHOTO2)
        working = _backend("Working", kind=BackendKind.GPHOTO2, cameras=[card])

        with mock.patch.object(registry, "ALL_BACKENDS", [sloppy, working]):
            results = registry.detect_all()

        self.assertEqual([info for _b, info in results], [card])


class DetectAllDeduplicationTests(unittest.TestCase):
    """One physical camera seen twice is shown once — the earlier one wins."""

    def test_the_same_model_seen_by_two_transports_is_returned_once(self) -> None:
        """Mass storage wins, because reading a card beats USB 1.1 PTP.

        This only holds because mass storage is first in ``ALL_BACKENDS``:
        ``detect_all`` keeps the entry it already collected.
        """
        card = _camera("Canon PowerShot S30", BackendKind.MASS_STORAGE, "/Volumes/CARD")
        body = _camera("Canon PowerShot S30", BackendKind.GPHOTO2, "usb:001,004")
        reader = _backend("Card reader", cameras=[card])
        gphoto = _backend("gphoto2", kind=BackendKind.GPHOTO2, cameras=[body])

        progress = RecordingProgress()
        with mock.patch.object(registry, "ALL_BACKENDS", [reader, gphoto]):
            results = registry.detect_all(progress)

        self.assertEqual([info for _b, info in results], [card])
        self.assertIs(results[0][1].kind, BackendKind.MASS_STORAGE)
        # A wrong guess must be visible in the log rather than mysterious.
        self.assertTrue(
            any(
                "looks like" in message and card.label in message
                for message in progress.messages()
            ),
            "the merge was never explained: %r" % (progress.messages(),),
        )

    def test_two_devices_from_one_backend_are_never_merged(self) -> None:
        """Two card readers holding two identical cards are two devices.

        A backend that reports two devices is reporting two devices. Hiding one
        is data loss, which is why the heuristic refuses outright when the kinds
        match — even when the model strings are identical.
        """
        first = _camera("Canon PowerShot S30", BackendKind.MASS_STORAGE, "/Volumes/A")
        second = _camera("Canon PowerShot S30", BackendKind.MASS_STORAGE, "/Volumes/B")
        reader = _backend("Card reader", cameras=[first, second])

        with mock.patch.object(registry, "ALL_BACKENDS", [reader]):
            results = registry.detect_all()

        self.assertEqual([info for _b, info in results], [first, second])

    def test_two_generic_descriptions_are_never_taken_for_one_device(self) -> None:
        """'Removable drive E:' is not evidence of anything.

        Every distinctive token here is a noise word, so the token sets come out
        empty and the heuristic must decline. Merging these would hide a real
        second device behind a word every USB gadget uses.
        """
        drive = _camera("Removable drive E:", BackendKind.MASS_STORAGE)
        ptp = _camera("USB PTP Camera", BackendKind.WIA)
        reader = _backend("Card reader", cameras=[drive])
        wia = _backend("WIA", kind=BackendKind.WIA, cameras=[ptp])

        with mock.patch.object(registry, "ALL_BACKENDS", [reader, wia]):
            results = registry.detect_all()

        self.assertEqual([info for _b, info in results], [drive, ptp])

    def test_same_device_heuristic_demands_real_token_overlap(self) -> None:
        """The exact rule the code implements, stated case by case.

        There is no reliable cross-transport device identity — gphoto2 says
        ``usb:001,004``, WIA says a driver GUID, a card says a mount point — so
        all this has is the model text. The rule is: different transports, and
        either identical distinctive token sets or one being a subset of the
        other with at least two tokens in common. Everything else is a guess and
        must come back False, because showing a camera twice is cosmetic while
        hiding one is data loss.
        """
        ms, gp, wia = (
            BackendKind.MASS_STORAGE,
            BackendKind.GPHOTO2,
            BackendKind.WIA,
        )
        cases = [
            # (first model, first kind, second model, second kind, same device?)
            ("Canon PowerShot S30", ms, "Canon PowerShot S30", gp, True),
            # Subset with two distinctive tokens: 'Canon' is dropped as a vendor
            # word by neither side, but {powershot, s30} ⊂ {canon, powershot, s30}.
            ("Canon PowerShot S30", ms, "PowerShot S30", gp, True),
            # One shared word is not evidence — half the world's cameras say Canon.
            ("Canon PowerShot S30", ms, "Canon", gp, False),
            # Two real cameras from the same vendor must both survive.
            ("Canon PowerShot S30", ms, "Canon EOS 300D", gp, False),
            # Nothing but noise words on both sides.
            ("Removable drive", ms, "USB Mass Storage Device", wia, False),
            # Same transport: never merged, whatever the text says.
            ("Canon PowerShot S30", ms, "Canon PowerShot S30", ms, False),
            # An empty model cannot match anything.
            ("", ms, "Canon PowerShot S30", gp, False),
            # Case and punctuation are not identity.
            ("canon powershot-s30", ms, "Canon PowerShot S30", wia, True),
        ]
        for first_model, first_kind, second_model, second_kind, expected in cases:
            with self.subTest(first=first_model, second=second_model):
                self.assertEqual(
                    registry._looks_like_same_device(
                        _camera(first_model, first_kind),
                        _camera(second_model, second_kind),
                    ),
                    expected,
                )

    def test_the_heuristic_is_symmetric(self) -> None:
        """Registry order must decide which entry wins, not which is compared.

        If the answer depended on argument order, the surviving device would
        depend on the order two backends happened to answer in.
        """
        pairs = [
            ("Canon PowerShot S30", "PowerShot S30"),
            ("Canon PowerShot S30", "Canon"),
            ("Nikon Coolpix 995", "Canon PowerShot S30"),
        ]
        for left, right in pairs:
            with self.subTest(left=left, right=right):
                first = _camera(left, BackendKind.MASS_STORAGE)
                second = _camera(right, BackendKind.GPHOTO2)
                self.assertEqual(
                    registry._looks_like_same_device(first, second),
                    registry._looks_like_same_device(second, first),
                )


# --------------------------------------------------------------------------- #
# deps: tripwires
# --------------------------------------------------------------------------- #


class _SocketTripwire:
    """Records every in-process attempt to open a socket.

    It *records* rather than only raising, and the test asserts the record is
    empty afterwards. That distinction is the whole point: every probe in
    ``deps.py`` is wrapped in a bare ``except Exception``, so a tripwire that
    merely raised would be caught by the code under test and the test would pass
    while the network call happened.

    Limitation, stated honestly: this sees only sockets opened in *this*
    process. A child process could open its own, which is why
    :class:`_ProcessSpy` asserts that the only children started are version
    queries.
    """

    def __init__(self) -> None:
        self.attempts: List[str] = []
        self._patches: List[Any] = []

    def _record(self, what: str) -> Any:
        def _blocked(*_args: Any, **_kwargs: Any) -> Any:
            self.attempts.append(what)
            raise OSError("network access is forbidden during check_all()")

        return _blocked

    def __enter__(self) -> "_SocketTripwire":
        for name in ("socket", "create_connection", "getaddrinfo", "gethostbyname"):
            patch = mock.patch.object(socket, name, self._record(name))
            patch.start()
            self._patches.append(patch)
        return self

    def __exit__(self, *_exc: Any) -> None:
        for patch in reversed(self._patches):
            patch.stop()
        self._patches = []


class _ProcessSpy:
    """Records every child process started, tagged by the API that started it.

    Both entry points are watched, and the tag matters: ``subprocess.run``
    delegates to ``subprocess.Popen`` internally, so a single probe shows up
    twice — once as the time-boxed ``run`` call the module made, and once as the
    raw ``Popen`` the standard library made on its behalf. Comparing the two
    lists is what proves no probe starts a process *outside* the runner that
    carries the deadline.
    """

    def __init__(self) -> None:
        self.calls: List[Tuple[str, List[str], Dict[str, Any]]] = []
        self._patches: List[Any] = []

    def __enter__(self) -> "_ProcessSpy":
        original_run = subprocess.run
        original_popen = subprocess.Popen

        def run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
            self.calls.append(("run", list(cmd), dict(kwargs)))
            return original_run(cmd, *args, **kwargs)

        def popen(cmd: Any, *args: Any, **kwargs: Any) -> Any:
            self.calls.append(("Popen", list(cmd), dict(kwargs)))
            return original_popen(cmd, *args, **kwargs)

        for name, replacement in (("run", run), ("Popen", popen)):
            patch = mock.patch.object(subprocess, name, replacement)
            patch.start()
            self._patches.append(patch)
        return self

    def __exit__(self, *_exc: Any) -> None:
        for patch in reversed(self._patches):
            patch.stop()
        self._patches = []

    def argvs(self, api: str = "Popen") -> List[List[str]]:
        """Every argv started through ``api``. ``Popen`` sees them all, once."""
        return [cmd for name, cmd, _kwargs in self.calls if name == api]


# --------------------------------------------------------------------------- #
# deps: check_all
# --------------------------------------------------------------------------- #


class DependencyReportTests(unittest.TestCase):
    """``check_all`` paints the environment panel and must be inert and quick."""

    def test_dependency_fields_keep_their_documented_order(self) -> None:
        """The field order is public: callers build and unpack these positionally.

        Reordering ``present`` and ``version`` would silently turn a version
        string into a truthy "present" flag, which the delete-gate-adjacent UI
        would then report as "Pillow installed, downloads are fully decoded".
        """
        names = tuple(field.name for field in dataclasses.fields(deps.Dependency))
        self.assertEqual(
            names, ("key", "label", "present", "version", "hint", "can_autoinstall")
        )
        # Only key and label are required; everything else has a safe default.
        minimal = deps.Dependency("gphoto2", "gphoto2")
        self.assertIs(minimal.present, False)
        self.assertEqual((minimal.version, minimal.hint), ("", ""))
        self.assertIs(minimal.can_autoinstall, False)

    def test_check_all_returns_usable_rows_for_this_machine(self) -> None:
        """Every row is renderable, and every missing thing says how to get it."""
        report = deps.check_all()

        self.assertTrue(report, "check_all() reported nothing at all")
        for dep in report:
            with self.subTest(key=dep.key):
                self.assertIsInstance(dep, deps.Dependency)
                self.assertTrue(dep.key.strip(), "a dependency has no key")
                self.assertTrue(dep.label.strip(), "a dependency has no label")
                self.assertIsInstance(dep.present, bool)
                self.assertIsInstance(dep.version, str)
                if not dep.present:
                    self.assertTrue(
                        dep.hint.strip(),
                        "%s is absent but does not say how to get it" % dep.key,
                    )

    def test_check_all_reports_only_keys_the_gui_can_translate(self) -> None:
        """``key`` is the stable seam the GUI looks translations up against."""
        known = {
            deps.KEY_GPHOTO2,
            deps.KEY_HOMEBREW,
            deps.KEY_PILLOW,
            deps.KEY_PYWIN32,
            deps.KEY_WSL,
            deps.KEY_USBIPD,
        }
        for dep in deps.check_all():
            with self.subTest(key=dep.key):
                self.assertIn(dep.key, known)

    def test_a_probe_that_raises_does_not_empty_the_panel(self) -> None:
        """One broken probe must cost one row, not all of them.

        The replacement row keeps the real key so the GUI can still label it,
        and carries the reason as its hint.
        """

        def _probe_pillow() -> deps.Dependency:  # name drives the recovered key
            raise RuntimeError("importlib went sideways")

        with mock.patch.object(deps, "_probe_pillow", _probe_pillow):
            report = deps.check_all()

        keys = [dep.key for dep in report]
        self.assertIn(deps.KEY_PILLOW, keys)
        broken = next(dep for dep in report if dep.key == deps.KEY_PILLOW)
        self.assertFalse(broken.present)
        self.assertIn("importlib went sideways", broken.hint)
        # The other probes still produced rows.
        self.assertGreater(len(report), 1)

    def test_check_all_makes_no_network_call(self) -> None:
        """The README promises nothing before Install touches the network.

        Enforced here for this process, and by
        :meth:`test_check_all_only_asks_tools_to_identify_themselves` for the
        children.
        """
        with _SocketTripwire() as tripwire:
            report = deps.check_all()

        self.assertEqual(tripwire.attempts, [], "check_all() opened a socket")
        self.assertTrue(report)

    @staticmethod
    def _spy_on_check_all() -> _ProcessSpy:
        """Run ``check_all`` with every tool lookup pointed at this interpreter.

        Without this, the process assertions below would quietly pass on a
        machine that has none of the tools installed — no probe would spawn
        anything, and there would be nothing to assert about. Pointing
        :func:`retrocam.deps.which` at ``sys.executable`` guarantees the probes
        really do spawn something everywhere, and ``python --version`` is the
        most harmless stand-in there is: it prints one line and exits.
        """
        with _ProcessSpy() as spy:
            with mock.patch.object(
                deps, "which", lambda name, extra_dirs=(): sys.executable
            ):
                deps.check_all()
        return spy

    def test_check_all_only_asks_tools_to_identify_themselves(self) -> None:
        """No child process started by a probe may change the machine.

        A probe is read-only by contract. Every command it runs has to be a
        pure query — ``--version``, or WSL's ``--list --quiet`` — so that
        painting the environment panel cannot mutate the system.
        """
        queries = {"--version", "--list", "--quiet"}
        spy = self._spy_on_check_all()

        self.assertTrue(spy.argvs(), "no probe started a process to assert about")
        for argv in spy.argvs():
            with self.subTest(argv=argv):
                self.assertTrue(
                    set(argv[1:]) <= queries,
                    "a probe ran something other than a version query",
                )

    def test_every_probe_process_is_time_boxed_and_cannot_block_on_stdin(
        self,
    ) -> None:
        """A tool that hangs must be killed, not waited on by the GUI thread.

        Two properties: no probe starts a process outside ``_run`` (the only
        runner that carries a deadline), and every process gets a closed stdin
        so a tool that decides to ask a question dies instead of wedging the
        GUI.
        """
        spy = self._spy_on_check_all()

        self.assertTrue(spy.argvs(), "no probe started a process to assert about")
        self.assertEqual(
            spy.argvs("Popen"),
            spy.argvs("run"),
            "a probe started a process outside the time-boxed runner",
        )
        for api, argv, kwargs in spy.calls:
            with self.subTest(api=api, argv=argv):
                self.assertIs(kwargs.get("stdin"), subprocess.DEVNULL)
                if api == "run":
                    timeout = kwargs.get("timeout")
                    self.assertIsNotNone(timeout, "a probe ran without a timeout")
                    self.assertLessEqual(float(timeout), 15.0)

    def test_check_all_returns_promptly(self) -> None:
        """The environment panel paints on the GUI thread.

        The bound is deliberately loose — each probe is individually capped at
        15 s and this machine finishes in milliseconds — because what is being
        caught is an *unbounded* wait, not a slow one.
        """
        started = time.time()
        deps.check_all()
        self.assertLess(time.time() - started, 20.0)

    def test_homebrew_is_told_not_to_auto_update_while_probing(self) -> None:
        """``brew --version`` must not become a network operation.

        Homebrew is free to decide that ``--version`` is a good moment to
        auto-update; stating the opt-out costs nothing and keeps the "nothing
        before Install touches the network" promise true regardless. Driven with
        a stubbed runner so the assertion holds on machines without Homebrew.
        """
        recorded: Dict[str, Any] = {}

        def fake_run(cmd, timeout=10.0, extra_env=None):  # type: ignore[no-untyped-def]
            recorded["cmd"] = list(cmd)
            recorded["extra_env"] = dict(extra_env or {})
            return 0, "Homebrew 4.2.0", ""

        with mock.patch.object(
            deps, "which", lambda name, extra_dirs=(): "/fake/bin/brew"
        ):
            with mock.patch.object(deps, "_run", fake_run):
                dep = deps._probe_homebrew()

        self.assertTrue(dep.present)
        self.assertEqual(dep.version, "4.2.0")
        self.assertEqual(recorded["cmd"], ["/fake/bin/brew", "--version"])
        self.assertEqual(recorded["extra_env"].get("HOMEBREW_NO_AUTO_UPDATE"), "1")
        self.assertEqual(recorded["extra_env"].get("HOMEBREW_NO_ANALYTICS"), "1")


# --------------------------------------------------------------------------- #
# deps: install safety
# --------------------------------------------------------------------------- #


class _InstallSpy:
    """Replaces the two process runners so no installer can actually run."""

    def __init__(self) -> None:
        self.ran: List[List[str]] = []
        self._patches: List[Any] = []

    def __enter__(self) -> "_InstallSpy":
        def fake_run(cmd, timeout=10.0, extra_env=None):  # type: ignore[no-untyped-def]
            self.ran.append(list(cmd))
            return 0, "", ""

        def fake_stream(cmd, key, progress, timeout, extra_env=None):  # type: ignore[no-untyped-def]
            self.ran.append(list(cmd))
            return 0, [], False

        for name, replacement in (("_run", fake_run), ("_stream", fake_stream)):
            patch = mock.patch.object(deps, name, replacement)
            patch.start()
            self._patches.append(patch)
        return self

    def __exit__(self, *_exc: Any) -> None:
        for patch in reversed(self._patches):
            patch.stop()
        self._patches = []


class InstallRefusalTests(unittest.TestCase):
    """``install`` returns instructions instead of raising or escalating."""

    def test_an_unknown_key_is_refused_with_a_message_not_an_exception(self) -> None:
        """The GUI shows this string verbatim; a traceback would not do."""
        for key in ("nonsense", "", "   ", "gphoto3"):
            with self.subTest(key=key):
                with _InstallSpy() as spy:
                    ok, message = deps.install(key)
                self.assertIs(ok, False)
                self.assertTrue(message.strip())
                self.assertEqual(spy.ran, [], "an unknown key started a process")
                # The message has to be actionable: it lists what *can* be done.
                for installable in deps.INSTALLABLE_KEYS:
                    self.assertIn(installable, message)

    def test_a_none_key_is_refused_rather_than_crashing(self) -> None:
        """``(key or "")`` is load-bearing: the GUI can pass a cleared selection."""
        with _InstallSpy() as spy:
            ok, message = deps.install(None)  # type: ignore[arg-type]
        self.assertIs(ok, False)
        self.assertTrue(message.strip())
        self.assertEqual(spy.ran, [])

    def test_the_two_manual_dependencies_hand_back_instructions(self) -> None:
        """Homebrew and WSL2 are refused *by design*, with the real command.

        Installing Homebrew means piping a downloaded script into a shell, and
        WSL2 needs elevation and a reboot. Both must be a message, never an
        action.
        """
        with _InstallSpy() as spy:
            brew_ok, brew_message = deps.install(deps.KEY_HOMEBREW)
            wsl_ok, wsl_message = deps.install(deps.KEY_WSL)

        self.assertEqual(spy.ran, [])
        self.assertIs(brew_ok, False)
        self.assertIn("brew.sh", brew_message)
        self.assertIs(wsl_ok, False)
        self.assertIn("wsl --install", wsl_message)

    def test_the_key_is_normalised_before_it_is_matched(self) -> None:
        """A stray space or capital from the GUI must not read as 'unknown'.

        Driven through the pywin32 handler with Windows forced off, so the
        assertion is about routing and never reaches a package manager on any
        platform.
        """
        with mock.patch.object(deps, "_IS_WINDOWS", False):
            with _InstallSpy() as spy:
                ok, message = deps.install("  PyWin32  ")

        self.assertIs(ok, False)
        self.assertIn("Windows", message)
        self.assertEqual(spy.ran, [], "the platform check ran an installer anyway")
        # It routed to the handler, not to the unknown-key branch.
        self.assertNotIn("Nothing can be installed", message)

    def test_a_handler_that_raises_becomes_a_message(self) -> None:
        """An install must not crash the GUI, whatever the installer does."""

        def _install_pillow(_progress: Any) -> Tuple[bool, str]:
            raise OSError("disk full")

        with mock.patch.object(deps, "_install_pillow", _install_pillow):
            ok, message = deps.install(deps.KEY_PILLOW)

        self.assertIs(ok, False)
        self.assertIn("disk full", message)

    def test_linux_gphoto2_is_delegated_to_the_user_not_run(self) -> None:
        """RetroCam does not install system packages, so it does not try.

        The distro command it prints contains ``sudo`` — that is the *user's*
        command to type, and the point of this test is that it stays a string.
        """
        with mock.patch.object(deps, "_IS_WINDOWS", False):
            with mock.patch.object(deps, "_IS_MACOS", False):
                with _InstallSpy() as spy:
                    ok, message = deps.install(deps.KEY_GPHOTO2)

        self.assertIs(ok, False)
        self.assertIn("yourself", message)
        self.assertEqual(spy.ran, [], "RetroCam tried to install a system package")

    def test_the_pip_argv_never_escalates(self) -> None:
        """pip runs as this interpreter, user-scoped, never through a shell.

        ``--user`` is what keeps the install out of system directories and
        therefore out of sudo; inside a virtualenv the flag is rejected outright
        by pip, and the venv *is* the user's private site, so dropping it there
        preserves the guarantee rather than breaking the command.
        """
        # Not a virtualenv: base_prefix == prefix.
        with mock.patch.object(sys, "base_prefix", sys.prefix):
            outside = deps._pip_command("Pillow")
        # A virtualenv: base_prefix points at the interpreter it was made from.
        with mock.patch.object(sys, "base_prefix", os.path.join(sys.prefix, "base")):
            inside = deps._pip_command("Pillow")

        self.assertEqual(outside[:4], [sys.executable, "-m", "pip", "install"])
        self.assertIn("--user", outside)
        self.assertNotIn("--user", inside)
        for argv in (outside, inside):
            with self.subTest(argv=argv):
                self.assertEqual(argv[-1], "Pillow")
                self.assertNotIn("sudo", argv)
                self.assertEqual(argv[0], sys.executable)


# --------------------------------------------------------------------------- #
# deps: source-level safety
# --------------------------------------------------------------------------- #

_DEPS_PATH = os.path.join(_SRC_DIR, "retrocam", "deps.py")
with open(_DEPS_PATH, "r", encoding="utf-8") as _fh:
    _DEPS_SOURCE = _fh.read()
_DEPS_TREE = ast.parse(_DEPS_SOURCE, filename=_DEPS_PATH)

#: Anything that would run a command as another (or elevated) user.
_ESCALATORS = frozenset(
    ["sudo", "doas", "pkexec", "runas", "gsudo", "su", "elevate", "sudoedit"]
)

#: Anything that would hand a string to an interpreter instead of exec'ing an
#: argument list, plus the downloaders that make "curl | bash" possible.
_SHELLS = frozenset(
    [
        "bash",
        "sh",
        "zsh",
        "csh",
        "ksh",
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "osascript",
        "curl",
        "wget",
    ]
)


def _first_word(value: str) -> str:
    """Leading token of a string, as a command name would appear."""
    stripped = value.strip()
    if not stripped:
        return ""
    return os.path.basename(stripped.split()[0].replace("\\", "/")).lower()


class DepsSourceSafetyTests(unittest.TestCase):
    """The two hard rules of ``deps.py``, checked against the source itself.

    Runtime tests cannot cover this: the dangerous paths are the ones that would
    really install software, and running them is exactly what must not happen in
    a test suite. Parsing the module is how the promise stays enforced.
    """

    def test_no_child_process_is_ever_started_through_a_shell(self) -> None:
        """``shell=True`` turns every argument into an injection point."""
        offenders = []
        for node in ast.walk(_DEPS_TREE):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "shell":
                    continue
                value = keyword.value
                is_false = isinstance(value, ast.Constant) and value.value is False
                if not is_false:
                    offenders.append(getattr(node, "lineno", "?"))
        self.assertEqual(offenders, [], "shell= used at deps.py lines %r" % offenders)

    def test_no_command_string_starts_with_an_escalation_tool(self) -> None:
        """``sudo`` may be printed for the user to type, never executed.

        The only place allowed to produce such a string is
        ``_linux_install_command``, whose whole job is to tell the user what to
        run themselves; its result is never passed to a process.
        """
        allowed = self._line_range("_linux_install_command")
        offenders = []
        for node in ast.walk(_DEPS_TREE):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if _first_word(node.value) not in _ESCALATORS:
                continue
            if allowed[0] <= node.lineno <= allowed[1]:
                continue
            offenders.append((node.lineno, node.value[:60]))
        self.assertEqual(offenders, [], "escalation outside the hint builder")

    def test_no_argument_list_invokes_a_shell_or_a_downloader(self) -> None:
        """Every argv in this module is a literal list; none may be a shell.

        The Homebrew install script (``/bin/bash -c "$(curl ...)"``) exists in
        this module as a *string to display*. This test is what keeps it from
        ever becoming an argv.
        """
        offenders = []
        for node in ast.walk(_DEPS_TREE):
            if not isinstance(node, (ast.List, ast.Tuple)):
                continue
            for element in node.elts:
                if not isinstance(element, ast.Constant):
                    continue
                if not isinstance(element.value, str):
                    continue
                word = _first_word(element.value)
                if word in _SHELLS or word in _ESCALATORS:
                    offenders.append((element.lineno, element.value[:60]))
        self.assertEqual(offenders, [], "a shell appears in an argument list")

    def test_every_child_process_is_started_with_stdin_closed(self) -> None:
        """No installer may block forever waiting for an answer we cannot give.

        stdin is the difference between "winget asked which package you meant"
        and a GUI that is wedged with no visible cause.
        """
        calls = self._subprocess_calls()
        self.assertTrue(calls, "no subprocess call found — has deps.py moved?")
        for name, node in calls:
            with self.subTest(call=name, line=node.lineno):
                stdin = self._keyword(node, "stdin")
                self.assertIsNotNone(stdin, "%s starts without closing stdin" % name)
                self.assertEqual(ast.unparse(stdin), "subprocess.DEVNULL")

    def test_the_probe_runner_is_always_given_a_deadline(self) -> None:
        """``subprocess.run`` blocks the caller, so it must carry a timeout.

        ``Popen`` is exempt: ``_stream`` supervises it with a watchdog timer
        instead, which is checked by the presence of that timer rather than a
        kwarg.
        """
        for name, node in self._subprocess_calls():
            if name != "subprocess.run":
                continue
            with self.subTest(line=node.lineno):
                self.assertIsNotNone(
                    self._keyword(node, "timeout"),
                    "subprocess.run without a timeout at line %d" % node.lineno,
                )

    # -- helpers ----------------------------------------------------------- #

    def _line_range(self, function_name: str) -> Tuple[int, int]:
        for node in ast.walk(_DEPS_TREE):
            if isinstance(node, ast.FunctionDef) and node.name == function_name:
                return (node.lineno, node.end_lineno or node.lineno)
        self.fail("deps.py no longer defines %s()" % function_name)
        raise AssertionError  # pragma: no cover - self.fail always raises

    def _subprocess_calls(self) -> List[Tuple[str, ast.Call]]:
        found: List[Tuple[str, ast.Call]] = []
        for node in ast.walk(_DEPS_TREE):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if not isinstance(func.value, ast.Name) or func.value.id != "subprocess":
                continue
            if func.attr in ("run", "Popen"):
                found.append(("subprocess." + func.attr, node))
        return found

    @staticmethod
    def _keyword(node: ast.Call, name: str) -> Optional[ast.expr]:
        for keyword in node.keywords:
            if keyword.arg == name:
                return keyword.value
        return None


# --------------------------------------------------------------------------- #
# deps: destination folders
# --------------------------------------------------------------------------- #


class DefaultDownloadDirTests(TempDirCase):
    """Picking a folder must never fail and must never have side effects."""

    def test_the_real_machine_gets_an_absolute_path(self) -> None:
        result = deps.default_download_dir()
        self.assertTrue(os.path.isabs(result), result)

    def test_the_system_downloads_folder_is_preferred_when_it_exists(self) -> None:
        home = self.path("home")
        downloads = os.path.join(home, "Downloads")
        os.makedirs(downloads)
        with mock.patch("os.path.expanduser", lambda _p: home):
            self.assertEqual(deps.default_download_dir(), downloads)

    def test_a_missing_downloads_folder_falls_back_to_home_without_creating_it(
        self,
    ) -> None:
        """Choosing a destination is not permission to create one.

        The GUI shows this path before the user has agreed to anything; creating
        ``~/Downloads`` as a side effect of painting a window would be a change
        to the machine nobody asked for.
        """
        home = self.path("home")
        os.makedirs(home)
        with mock.patch("os.path.expanduser", lambda _p: home):
            result = deps.default_download_dir()

        self.assertEqual(result, home)
        self.assertFalse(os.path.exists(os.path.join(home, "Downloads")))

    def test_a_home_that_does_not_exist_falls_back_to_the_current_directory(
        self,
    ) -> None:
        """A broken ``HOME`` is common on kiosk and live-USB systems."""
        missing = self.path("no", "such", "home")
        with mock.patch("os.path.expanduser", lambda _p: missing):
            result = deps.default_download_dir()

        self.assertEqual(result, os.path.abspath(os.getcwd()))
        self.assertFalse(os.path.exists(missing))

    def test_an_exploding_expanduser_still_yields_a_path(self) -> None:
        """Never raises: the GUI has nowhere to put an exception here."""

        def boom(_path: str) -> str:
            raise RuntimeError("no password database")

        with mock.patch("os.path.expanduser", boom):
            result = deps.default_download_dir()

        self.assertTrue(os.path.isabs(result), result)

    def test_the_windows_branch_honours_userprofile(self) -> None:
        """Exercised from macOS: the ctypes lookup fails and the env wins.

        ``SHGetKnownFolderPath`` is unavailable here, so this covers the
        documented fallback chain rather than the happy path — which is the half
        that has to work when a machine has a relocated or missing Downloads.
        """
        profile = self.path("profile")
        downloads = os.path.join(profile, "Downloads")
        os.makedirs(downloads)
        home = self.path("home")
        os.makedirs(home)

        with (
            mock.patch.object(deps, "_IS_WINDOWS", True),
            mock.patch.object(deps, "_IS_MACOS", False),
            mock.patch.dict(os.environ, {"USERPROFILE": profile}),
            mock.patch("os.path.expanduser", lambda _p: home),
        ):
            self.assertEqual(deps.default_download_dir(), downloads)


class SuggestedDestTests(TempDirCase):
    """The proposed folder name: safe on three filesystems, and only a proposal."""

    # Every date assertion below samples the date before *and* after the call
    # and accepts either, so a run that straddles midnight cannot flake.

    def test_the_documented_example_is_produced_exactly(self) -> None:
        """The README's worked example: a PowerShot S30 rescued today."""
        before = datetime.now().strftime("%Y-%m-%d")
        result = deps.suggested_dest("Canon PowerShot S30", base=self.tmp)
        after = datetime.now().strftime("%Y-%m-%d")

        self.assertEqual(os.path.dirname(result), self.tmp)
        self.assertIn(
            os.path.basename(result),
            {"PowerShot_S30_" + before, "PowerShot_S30_" + after},
        )

    def test_awkward_model_names_become_safe_folder_names(self) -> None:
        """Restricted to ``[A-Za-z0-9._-]``, and always inside ``base``.

        A model string arrives from a camera's own firmware or from a WIA
        driver: spaces, slashes, backslashes and non-ASCII are all normal, and
        none of them may reach the filesystem or escape the chosen parent.
        """
        cases = [
            ("Canon PowerShot S30", "PowerShot_S30"),
            # A vendor word is only dropped while the rest still identifies the
            # camera: 'Kodak 4800' must not become '4800'.
            ("Kodak 4800", "Kodak_4800"),
            ("Canon", "Canon"),
            ("Canon\\PowerShot", "PowerShot"),
            # Non-ASCII collapses to a separator rather than reaching the disk.
            ("Nikon Coolpix é 995", "Coolpix_995"),
            # A model with nothing usable left still yields a usable folder.
            ("日本語", "Camera"),
            ("", "Camera"),
            ("   ", "Camera"),
            # Traversal cannot survive sanitisation.
            ("../../etc/passwd", "etc_passwd"),
        ]
        for model, expected_stem in cases:
            with self.subTest(model=model):
                before = datetime.now().strftime("%Y-%m-%d")
                result = deps.suggested_dest(model, base=self.tmp)
                after = datetime.now().strftime("%Y-%m-%d")
                name = os.path.basename(result)

                self.assertEqual(
                    os.path.dirname(result),
                    self.tmp,
                    "%r escaped the chosen parent" % model,
                )
                self.assertIn(
                    name, {"%s_%s" % (expected_stem, d) for d in (before, after)}
                )
                self.assertRegex(name, r"^[A-Za-z0-9._-]+$")

    def test_a_very_long_model_is_truncated(self) -> None:
        """Some drivers report a whole marketing sentence as the model name."""
        result = deps.suggested_dest("Nikon " + "x" * 300, base=self.tmp)
        stem = os.path.basename(result).rsplit("_", 1)[0]
        self.assertLessEqual(len(stem), 60)

    def test_the_path_is_proposed_and_never_created(self) -> None:
        """The transfer engine creates the folder, and only once bytes flow.

        Creating it here would scatter empty dated folders every time someone
        merely plugged a camera in and changed their mind.
        """
        base = self.path("not", "there", "yet")
        result = deps.suggested_dest("Canon PowerShot S30", base=base)

        self.assertFalse(os.path.exists(result))
        self.assertFalse(os.path.exists(base))

    def test_the_same_camera_on_the_same_day_lands_in_the_same_folder(self) -> None:
        """Deliberately not unique: this is what makes a resume possible.

        A fresh folder per attempt would scatter half-copies of an interrupted
        rescue across four directories and defeat ``skip_existing``.
        """
        first = deps.suggested_dest("Canon PowerShot S30", base=self.tmp)
        second = deps.suggested_dest("Canon PowerShot S30", base=self.tmp)
        self.assertEqual(first, second)

    def test_a_relative_or_tilde_base_is_resolved_to_an_absolute_path(self) -> None:
        """The GUI may hand back whatever the user typed into the field."""
        home = self.path("home")
        os.makedirs(home)
        with mock.patch("os.path.expanduser", lambda _p: home):
            result = deps.suggested_dest("Canon PowerShot S30", base="~")

        self.assertTrue(os.path.isabs(result), result)
        self.assertEqual(os.path.dirname(result), home)

    def test_no_base_falls_back_to_the_download_folder(self) -> None:
        result = deps.suggested_dest("Canon PowerShot S30")
        self.assertTrue(os.path.isabs(result), result)
        self.assertEqual(os.path.dirname(result), deps.default_download_dir())
        self.assertFalse(os.path.exists(result), "the proposal was created on disk")

    def test_a_hostile_base_never_raises(self) -> None:
        """Whatever comes out of the folder picker, a path comes back."""
        for base in ("\x00broken", "~nosuchuser~/x", "%NOPE%/dir"):
            with self.subTest(base=base):
                result = deps.suggested_dest("Canon PowerShot S30", base=base)
                self.assertTrue(result)
                self.assertTrue(os.path.isabs(result), result)


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    unittest.main()
