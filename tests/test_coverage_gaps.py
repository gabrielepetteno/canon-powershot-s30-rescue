"""Tests for code the rest of the suite never reaches.

Every test in this file was written to kill a specific surviving mutant, or to
cover a module that had *no* executed line at all. It is not a grab-bag: each
class below corresponds to one hole that was measured rather than guessed.

The four holes, and why each one matters:

* **The deep-decode branch of** :mod:`retrocam.verify`. Pillow is an optional
  dependency and is deliberately absent from this suite's environment, so
  ``verify._deep_decode`` — the code that decides whether a JPEG the decoder
  refuses may be erased from the card — never ran. Turning
  ``ImageFile.LOAD_TRUNCATED_IMAGES`` from ``False`` to ``True`` there left the
  whole suite green, and that single character is the difference between
  "half a photo is corrupt" and "half a photo decodes fine, erase the original".
  A stub ``PIL`` lets the real branch run on a machine with no Pillow.

* **"Unknown" collapsing into "empty"** in the abort-recovery path. When the
  destination cannot be listed before a run, :meth:`TransferEngine._snapshot`
  answers ``None`` meaning *unknown*, and unknown must credit nothing. Making it
  answer an empty set instead — which reads as "everything here is new" — also
  left the suite green.

* **The delete button's own guard** in :mod:`retrocam.app`. The module had zero
  executed lines. Two of its properties are load-bearing rather than cosmetic:
  the button-level gate, and the fact that picking another camera throws the old
  report away. Nothing else binds a :class:`TransferReport` to the camera that
  produced it, so that second one is the only thing standing between camera A's
  evidence and a delete aimed at camera B.

* **The headless entry point** in :mod:`retrocam.__main__`, also at zero. Its
  docstring makes a safety claim — "``--cli`` never downloads and never deletes
  ... there is no headless path to a destructive operation" — that nothing
  checked.

No Tk window is ever created here: the GUI tests build a
:class:`~retrocam.app.RetroCamApp` without running ``__init__`` and set only the
state the method under test reads. That is white-box on purpose. The alternative
is a real ``Tk()`` root, which needs a display and would make these tests
unrunnable on the very CI runners that build the release binaries.

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests

Stdlib only, no Pillow, no camera, no network, no display.
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import textwrap
import types
import unittest
from typing import Any, Dict, Iterator, List, Optional, Sequence

# Discovery puts ``tests/`` on sys.path, but be explicit so the file also runs
# when pointed at directly from another directory.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from helpers import (  # noqa: E402
    TempDirCase,
    mass_storage_camera,
    riff_avi,
    tiny_jpeg,
)

from retrocam import deps, registry, verify  # noqa: E402
from retrocam import __main__ as main_module  # noqa: E402
from retrocam.backends.base import (  # noqa: E402
    Availability,
    CameraBackend,
    noop_progress,
)
from retrocam.backends.massstorage import MassStorageBackend  # noqa: E402
from retrocam.model import (  # noqa: E402
    BackendKind,
    CameraFile,
    CameraInfo,
    CancelToken,
    DeleteOutcome,
    DownloadOutcome,
    VerifyResult,
)
from retrocam.transfer import TransferEngine, TransferReport  # noqa: E402

# app.py imports tkinter at module scope. A Python built without Tk is a state
# this project supports and explains rather than crashes on (see
# ``__main__._run_gui``), so the GUI tests skip there instead of erroring the
# whole file out of discovery.
try:  # pragma: no cover - depends on the interpreter, not on the tests
    from retrocam import app as app_module
except Exception as exc:  # pragma: no cover - Python without Tk
    app_module = None  # type: ignore[assignment]
    _APP_IMPORT_ERROR = "%s: %s" % (type(exc).__name__, exc)
else:
    _APP_IMPORT_ERROR = ""


# --------------------------------------------------------------------------- #
# A stub Pillow
# --------------------------------------------------------------------------- #


class _StubImage:
    """What ``Image.open()`` returns: a context manager that can be decoded."""

    def __init__(self, state: Dict[str, Any], decode_error: Optional[Exception]):
        self._state = state
        self._decode_error = decode_error

    def __enter__(self) -> "_StubImage":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def _decode(self, step: str) -> None:
        self._state["steps"].append(step)
        if self._decode_error is not None:
            raise self._decode_error

    def verify(self) -> None:
        self._decode("verify")

    def load(self) -> None:
        self._decode("load")


@contextlib.contextmanager
def stub_pillow(
    decode_error: Optional[Exception] = None, host_truncated_setting: bool = False
) -> Iterator[Dict[str, Any]]:
    """Install a minimal fake ``PIL`` so ``verify._deep_decode`` genuinely runs.

    ``decode_error`` makes both ``verify()`` and ``load()`` raise it, which is
    what a real Pillow does for a damaged file *and* for a format it has no
    codec for — the two cases ``_deep_decode`` must tell apart by extension.

    ``host_truncated_setting`` is the value of ``ImageFile.LOAD_TRUNCATED_IMAGES``
    the process is already carrying. Any library in the process can set it, and
    the point of several tests below is what RetroCam does with it.

    The yielded dict records ``flag_during_decode`` — the value of that global
    observed at the moment each image was opened — and ``steps``, the decode
    calls that actually happened.
    """
    imagefile_mod = types.ModuleType("PIL.ImageFile")
    imagefile_mod.LOAD_TRUNCATED_IMAGES = host_truncated_setting  # type: ignore[attr-defined]

    state: Dict[str, Any] = {"opened": [], "flag_during_decode": [], "steps": []}

    def _open(path: str) -> _StubImage:
        state["opened"].append(path)
        # Sampled here, inside the decode, because that is the only moment at
        # which the setting can affect what Pillow accepts.
        state["flag_during_decode"].append(imagefile_mod.LOAD_TRUNCATED_IMAGES)
        return _StubImage(state, decode_error)

    image_mod = types.ModuleType("PIL.Image")
    image_mod.open = _open  # type: ignore[attr-defined]

    pil_mod = types.ModuleType("PIL")
    pil_mod.Image = image_mod  # type: ignore[attr-defined]
    pil_mod.ImageFile = imagefile_mod  # type: ignore[attr-defined]

    stubs = {"PIL": pil_mod, "PIL.Image": image_mod, "PIL.ImageFile": imagefile_mod}
    sentinel = object()
    saved = {name: sys.modules.get(name, sentinel) for name in stubs}
    sys.modules.update(stubs)
    try:
        yield state
    finally:
        for name, previous in saved.items():
            if previous is sentinel:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous  # type: ignore[assignment]


class DeepDecodeTests(TempDirCase):
    """``verify._deep_decode``: the branch a machine without Pillow never runs."""

    def jpeg(self, name: str = "IMG_0001.JPG", payload: int = 300) -> str:
        path = self.path(name)
        with open(path, "wb") as handle:
            handle.write(tiny_jpeg(payload))
        return path

    def test_a_hosts_tolerate_truncated_setting_is_forced_off_during_the_decode(
        self,
    ) -> None:
        """``LOAD_TRUNCATED_IMAGES`` must be False while our decode runs.

        It is a Pillow-wide global that any library in the process can set, and
        several do — it is the correct setting for a photo *viewer*, which would
        rather show most of a broken image than nothing. For us it is
        catastrophic: with it on, Pillow decodes a half-written JPEG without
        complaint, the file passes verification, and the delete gate then erases
        the only complete copy from the card.

        So the test starts from the hostile state — the flag already True, as if
        a viewer library had been imported first — and asserts that the value in
        force *at the moment the image is opened* is False regardless.
        """
        path = self.jpeg()
        with stub_pillow(host_truncated_setting=True) as pillow:
            result = verify.verify_download(path, expected_size=os.path.getsize(path))

        self.assertTrue(pillow["opened"], "the decode never ran; the stub was not used")
        self.assertEqual(
            pillow["flag_during_decode"],
            [False] * len(pillow["opened"]),
            "Pillow was allowed to tolerate a truncated image while verifying",
        )
        # The file really is fine, so the run must also end in a green verdict
        # with the stronger guarantee recorded — otherwise this test would pass
        # just as well against a verifier that refuses everything.
        self.assertTrue(result.ok, result.reason)
        self.assertTrue(result.checked_decode)

    def test_the_hosts_tolerate_truncated_setting_is_restored_afterwards(self) -> None:
        """We borrow a process-wide global; we must give it back.

        Leaving it forced to False would silently change how every other part of
        the host process decodes images. The restore is in a ``finally``, and
        this pins it for both outcomes: a clean decode and a failed one.
        """
        path = self.jpeg()
        for label, error in (
            ("a clean decode", None),
            ("a failed decode", OSError("x")),
        ):
            with self.subTest(case=label):
                with stub_pillow(
                    decode_error=error, host_truncated_setting=True
                ) as pillow:
                    verify.verify_download(path, expected_size=os.path.getsize(path))
                    restored = sys.modules["PIL.ImageFile"].LOAD_TRUNCATED_IMAGES
                self.assertTrue(pillow["opened"], "the decode never ran")
                self.assertTrue(
                    restored,
                    "the host's LOAD_TRUNCATED_IMAGES setting was not put back",
                )

    def test_a_jpeg_the_decoder_rejects_is_condemned_but_a_movie_is_not(self) -> None:
        """A decode failure is proof of damage only for formats Pillow must know.

        Pillow raises the same ``UnidentifiedImageError`` for "these bytes are
        corrupt" and for "I have no codec for this", so the exception cannot tell
        the two apart and the extension decides instead. Getting this backwards
        in either direction is expensive: condemn every AVI and CRW and the user
        can never clear their card, and the pressure is then to delete with no
        verification at all; wave a damaged JPEG through and the card loses the
        only intact copy.

        All three cases run against the *same* decoder failure, so the difference
        can only come from the policy under test.
        """
        broken = ValueError("cannot identify image file")

        jpeg = self.jpeg()
        with stub_pillow(decode_error=broken):
            verdict = verify.verify_download(jpeg, expected_size=os.path.getsize(jpeg))
        self.assertFalse(verdict.ok, "a JPEG the decoder rejects must not pass")
        self.assertIn("decode", verdict.reason.lower())
        self.assertFalse(verdict.checked_decode)

        movie = self.path("MVI_0001.AVI")
        with open(movie, "wb") as handle:
            handle.write(riff_avi(256))
        with stub_pillow(decode_error=broken):
            verdict = verify.verify_download(
                movie, expected_size=os.path.getsize(movie)
            )
        self.assertTrue(
            verdict.ok,
            "a movie Pillow cannot read is unverified, not corrupt: %s"
            % verdict.reason,
        )
        self.assertFalse(
            verdict.checked_decode,
            "no pixels were decoded, so the stronger guarantee must not be claimed",
        )

        # Control: with a decoder that works, the same JPEG passes *and* reports
        # the stronger guarantee. Without this the two assertions above would
        # also hold for a verifier that had simply stopped calling Pillow.
        with stub_pillow():
            verdict = verify.verify_download(jpeg, expected_size=os.path.getsize(jpeg))
        self.assertTrue(verdict.ok, verdict.reason)
        self.assertTrue(verdict.checked_decode)


# --------------------------------------------------------------------------- #
# "Unknown" is not "empty"
# --------------------------------------------------------------------------- #


class AbortRecoverySnapshotTests(TempDirCase):
    """After an abort, an unlistable destination must credit nothing."""

    def test_an_unlistable_destination_credits_nothing_after_an_abort(self) -> None:
        """``_snapshot`` answers None for "unknown", and None credits nothing.

        The snapshot taken before a transfer is what separates "this run wrote
        that file" from "that file was already lying there". When the directory
        cannot be listed the honest answer is *unknown*, and the recovery path
        must then credit nothing at all.

        The tempting shortcut — answering an empty set — inverts the meaning into
        "nothing was here before, so everything here now is mine", which is
        exactly how a stranger's file with a matching name and size gets reported
        as a rescued, verified, safe-to-erase photograph.
        """
        dest = self.path("rescued")
        os.makedirs(dest)
        data = tiny_jpeg(400)
        with open(os.path.join(dest, "IMG_0001.JPG"), "wb") as handle:
            handle.write(data)
        camera_file = CameraFile(
            folder="/DCIM/118CANON", name="IMG_0001.JPG", size=len(data)
        )

        # 1. An unlistable directory is "unknown", never "empty".
        self.assertIsNone(
            TransferEngine._snapshot(self.path("no-such-directory")),
            "a destination that cannot be listed must answer 'unknown'",
        )

        # 2. "Unknown" credits nothing, even though the file is sitting right
        #    there and matches on both name and byte count.
        self.assertIsNone(
            TransferEngine._find_recovered_dest(camera_file, dest, None, set()),
            "a file was credited to a photograph on an unknown snapshot",
        )

        # 3. Control. With a genuine snapshot of an empty directory the very
        #    same call *does* credit the copy — so the refusal above is the
        #    snapshot being unknown, not this helper never crediting anything.
        self.assertEqual(
            TransferEngine._find_recovered_dest(camera_file, dest, set(), set()),
            os.path.join(dest, "IMG_0001.JPG"),
        )


# --------------------------------------------------------------------------- #
# The GUI's own guards
# --------------------------------------------------------------------------- #


class _Var:
    """Stand-in for a ``tk.StringVar`` that needs no Tk root."""

    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value


@unittest.skipIf(
    app_module is None, "tkinter is not importable: %s" % _APP_IMPORT_ERROR
)
class DeleteButtonGateTests(TempDirCase):
    """:mod:`retrocam.app`'s half of the delete gate, without a window."""

    def setUp(self) -> None:
        super().setUp()
        self.card_root, _contents = self.make_card()
        self.camera = mass_storage_camera(self.card_root, model="Canon PowerShot S30")
        self.engine = TransferEngine(MassStorageBackend(), self.camera)

    # -- fixtures ---------------------------------------------------------- #

    def report(self, all_ok: bool = True) -> TransferReport:
        """A real report whose files really exist at their destination."""
        dest = self.path("rescued")
        os.makedirs(dest, exist_ok=True)
        outcomes: List[DownloadOutcome] = []
        for index, ok in enumerate((True, all_ok)):
            data = tiny_jpeg(200 + index)
            name = "IMG_000%d.JPG" % (index + 1)
            path = os.path.join(dest, name)
            with open(path, "wb") as handle:
                handle.write(data)
            outcomes.append(
                DownloadOutcome(
                    file=CameraFile(
                        folder="/DCIM/118CANON", name=name, size=len(data), mtime=1.0
                    ),
                    dest_path=path,
                    ok=ok,
                    verify=VerifyResult(ok=ok, reason="" if ok else "truncated"),
                    error="" if ok else "truncated",
                )
            )
        return TransferReport(outcomes=outcomes, dest_dir=dest)

    def app(self, **state: Any) -> Any:
        """A ``RetroCamApp`` carrying state but owning no widgets.

        ``__init__`` builds the entire window, so it is deliberately not run:
        the methods under test read plain attributes and never touch a widget,
        and requiring a display here would make the GUI's safety guards
        untestable on exactly the headless machines that build the releases.
        """
        instance = object.__new__(app_module.RetroCamApp)
        instance._busy = False
        instance._engine = self.engine
        instance._report = None
        instance._delete_supported = True
        for name, value in state.items():
            setattr(instance, "_" + name, value)
        return instance

    # -- tests ------------------------------------------------------------- #

    def test_the_delete_button_stays_disabled_until_every_condition_holds(self) -> None:
        """Four independent conditions, each of which alone disables the button.

        This is the GUI's own guard, not the engine's: ``delete_verified``
        re-derives the verified set from the evidence whatever the button did.
        It still matters, because it is what stops the user from ever being
        *offered* an erase that the engine would then have to refuse — including
        the case that has no engine-side equivalent, a transport that cannot
        delete at all.

        ``_delete_supported`` is three-valued on purpose: ``None`` means the
        backend has not been asked yet (asking touches the card, so it happens on
        the worker thread), and "not asked" must read as "no".
        """
        good = self.report()
        cases = [
            ("everything holds", {}, True),
            ("an operation is already running", {"busy": True}, False),
            ("no camera is selected", {"engine": None}, False),
            ("nothing has been downloaded yet", {"report": None}, False),
            ("the transport was never asked", {"delete_supported": None}, False),
            ("the transport cannot erase", {"delete_supported": False}, False),
            (
                "a file failed verification",
                {"report": self.report(all_ok=False)},
                False,
            ),
        ]
        for label, overrides, expected in cases:
            with self.subTest(state=label):
                state: Dict[str, Any] = {"report": good}
                state.update(overrides)
                self.assertIs(self.app(**state)._can_delete(), expected)

    def test_choosing_another_camera_discards_the_previous_report(self) -> None:
        """A report proves things about the camera it came from, and only that.

        Nothing binds a :class:`TransferReport` to a camera:
        ``delete_verified`` will happily apply one camera's report to another
        camera's engine, and the device paths inside it — ``/DCIM/118CANON/
        IMG_0001.JPG`` — are the paths a second Canon body very plausibly also
        uses. Dropping the report here is therefore not housekeeping; it is the
        only thing between camera A's evidence and an erase aimed at camera B.

        Everything downstream goes with it: the file list, and the cached answer
        to "can this transport erase".
        """
        other_root, _ = self.make_card(name="card2")
        other = mass_storage_camera(other_root, model="Canon PowerShot A40")
        backend = MassStorageBackend()

        instance = self.app(report=self.report())
        instance._devices = [(backend, self.camera), (backend, other)]
        instance._files = [CameraFile(folder="/DCIM/118CANON", name="IMG_0001.JPG")]
        instance._summary_var = _Var()
        # Stubbed because they drive widgets or start a worker thread; none of
        # them participates in the invariant under test.
        instance._suggest_dest = lambda model: None
        instance._update_camera_info = lambda: None
        instance._start_listing = lambda: None

        self.assertTrue(instance._can_delete(), "premise: the erase was on offer")

        instance._select_device(1)

        self.assertIs(instance._report, None, "the previous report survived a swap")
        self.assertIs(instance._files, None, "a stale file list survived a swap")
        self.assertIs(
            instance._delete_supported,
            None,
            "the previous transport's erase capability survived a swap",
        )
        self.assertIs(instance._engine.camera, other, "the new camera was not bound")
        self.assertFalse(
            instance._can_delete(),
            "the delete button stayed lit after the camera was changed",
        )


# --------------------------------------------------------------------------- #
# The headless entry point
# --------------------------------------------------------------------------- #


class _ProbeBackend(CameraBackend):
    """A backend that answers read-only questions and fails destructive ones."""

    kind = BackendKind.MASS_STORAGE
    display_name = "Probe transport"

    def __init__(self, files: Sequence[CameraFile]) -> None:
        self.files = list(files)
        self.listed = 0
        self.destructive_calls: List[str] = []

    @classmethod
    def is_available(cls) -> Availability:
        return True, ""

    def detect(self, progress: Any = noop_progress) -> List[CameraInfo]:
        return []

    def list_files(
        self,
        camera: CameraInfo,
        progress: Any = noop_progress,
        cancel: Optional[CancelToken] = None,
    ) -> List[CameraFile]:
        self.listed += 1
        return list(self.files)

    def download(
        self,
        camera: CameraInfo,
        files: Sequence[CameraFile],
        dest_dir: str,
        progress: Any = noop_progress,
        cancel: Optional[CancelToken] = None,
        skip_existing: bool = True,
    ) -> List[DownloadOutcome]:
        self.destructive_calls.append("download")
        raise AssertionError("the headless listing wrote files to disk")

    def delete(
        self,
        camera: CameraInfo,
        files: Sequence[CameraFile],
        progress: Any = noop_progress,
        cancel: Optional[CancelToken] = None,
    ) -> List[DeleteOutcome]:
        self.destructive_calls.append("delete")
        raise AssertionError("the headless listing erased files from the camera")


class HeadlessEntryPointTests(unittest.TestCase):
    """``python -m retrocam``: the paths that must work with no display."""

    def patch(self, module: Any, name: str, value: Any) -> None:
        original = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, original)

    def test_the_cli_never_downloads_and_never_deletes(self) -> None:
        """``--cli`` is the one headless path, and it is strictly read-only.

        The module's docstring states it outright — "there is no headless path to
        a destructive operation in this program, deliberately: erasing a card is
        a decision that belongs in front of a confirmation dialog" — and until
        now nothing checked it. A ``--download`` or ``--delete`` bolted on later
        for convenience would be a bypass of every confirmation, of the delete
        gate's GUI-side guard, and of the two-step confirmation for a whole card.

        The backend fails the test from inside if either operation is ever
        reached, so this cannot rot into an assertion about a mock.
        """
        backend = _ProbeBackend(
            [
                CameraFile(folder="/DCIM/118CANON", name="IMG_0001.JPG", size=1024),
                CameraFile(folder="/DCIM/119CANON", name="IMG_0001.JPG", size=2048),
            ]
        )
        camera = CameraInfo(
            model="Canon PowerShot S30", port="usb:001,004", kind=BackendKind.GPHOTO2
        )

        self.patch(
            deps,
            "check_all",
            lambda: [deps.Dependency(key="pillow", label="Pillow", present=False)],
        )
        self.patch(registry, "backend_status", lambda: [(_ProbeBackend, True, "")])
        self.patch(registry, "detect_all", lambda progress=None: [(backend, camera)])

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main_module._run_cli()

        self.assertEqual(code, 0, "a camera was found, so the exit code must be 0")
        self.assertEqual(
            backend.destructive_calls,
            [],
            "the read-only listing performed a destructive operation",
        )
        self.assertEqual(backend.listed, 1, "the listing never happened")
        printed = out.getvalue()
        self.assertIn("Canon PowerShot S30", printed)
        self.assertIn("IMG_0001.JPG", printed)

        # And the documented "nothing found" exit code, so a script can branch.
        self.patch(registry, "detect_all", lambda progress=None: [])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main_module._run_cli(), 1)

    def test_version_is_answered_without_ever_loading_tk(self) -> None:
        """``--version`` must not import tkinter, let alone open a root window.

        This is what makes the release pipeline's smoke test meaningful: the
        build runners are headless, and they prove a frozen binary starts by
        running it with ``--version``. Resolve the version by importing
        ``app.py`` — which imports tkinter at module scope — and that check turns
        into a test of the *runner's* display rather than of the binary.

        A subprocess, because the assertion is about what ``sys.modules`` holds
        afterwards and this test process has already imported both modules.
        """
        script = textwrap.dedent(
            """
            import sys
            sys.path.insert(0, %r)
            from retrocam.__main__ import main
            code = main(["--version"])
            print("EXIT=%%d" %% code)
            print("TKINTER=%%s" %% ("tkinter" in sys.modules))
            print("APP=%%s" %% ("retrocam.app" in sys.modules))
            """
            % _SRC
        )
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("EXIT=0", proc.stdout)
        self.assertIn(
            "TKINTER=False", proc.stdout, "--version pulled tkinter into the process"
        )
        self.assertIn("APP=False", proc.stdout, "--version imported the GUI module")
        # The point of the flag is the number, so prove one was actually printed.
        self.assertRegex(proc.stdout, r"\d+\.\d+")


if __name__ == "__main__":  # pragma: no cover - convenience runner
    unittest.main(verbosity=2)
