"""Tests for :mod:`retrocam.backends.wia`, the native Windows camera path.

This file carries unusual weight. The WIA backend has never run on real Windows
hardware and will not before release, so the fake COM layer in
:func:`helpers.fake_wia` is the only evidence anyone has that this code is
correct. Treat it as the substitute for hardware testing that it is.

What is genuinely exercised here is the backend's own logic: property reads by
numeric id, the depth-first item walk and its guards, COM apartment discipline,
the ``.part``-then-rename write, the size-mismatch refusal that stops a silent
re-encode from overwriting a photo, the batch-claim rule that stops one photo
being credited to another's copy, and the re-resolve-by-``ItemID`` delete.

What is *not* covered — and cannot be from macOS — is whether a real WIA driver
behaves the way the fake does. Every such place is marked ``TODO(hardware)``
with the exact question to answer on a real Windows machine.

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import os
import sys
import types
import unittest
from typing import Any, Dict, Iterator, List, Optional, Sequence

from helpers import (  # noqa: E402
    FakeWiaDevice,
    FakeWiaFile,
    FakeWiaFolder,
    RecordingProgress,
    TempDirCase,
    fake_wia,
    make_read_only,
    riff_avi,
    sha,
    tiny_jpeg,
)

# Private fixture types. They are part of the shared fake — the shapes the real
# automation API has — and are imported rather than re-declared so there is only
# one definition of each shape in the suite.
from helpers import (  # noqa: E402
    _ComError,
    _FakeCollection,
    _FakeImageFile,
    _FakeProperties,
)

from retrocam.backends import wia  # noqa: E402
from retrocam.model import (  # noqa: E402
    BackendKind,
    BackendUnavailable,
    CameraError,
    CameraFile,
    CameraInfo,
    CancelToken,
    TransferAborted,
)


# --------------------------------------------------------------------------- #
# Fixtures shared by the tests below
# --------------------------------------------------------------------------- #

#: Three photographs. A and C deliberately share both a name and a byte count
#: while holding different pictures: that is the Canon frame-counter rollover
#: that broke three backends during review, and every "wrong file" bug in this
#: module needs exactly that pair to show itself.
PHOTO_A = tiny_jpeg(1000, 0x11)  # /DCIM/118CANON/IMG_0001.JPG
PHOTO_B = tiny_jpeg(2000, 0x22)  # /DCIM/118CANON/IMG_0002.JPG
PHOTO_C = tiny_jpeg(1000, 0x33)  # /DCIM/119CANON/IMG_0001.JPG

#: A WIA_IPA_ITEM_TIME value in its documented shape: eight SYSTEMTIME words,
#: (year, month, day-of-week, day, hour, minute, second, milliseconds).
#: 13 August 2003, 19:22:00 — day-of-week (index 2) must be ignored.
SYSTEMTIME_WORDS = (2003, 8, 3, 13, 19, 22, 0, 0)

#: A plausible WIA format GUID, used to check the transfer asks for the stored
#: format rather than letting Windows choose (and re-encode).
JPEG_FORMAT_GUID = "{B96B3CAE-0728-11D3-9D7B-0000F81EF32E}"

#: wiaFormatBMP. The format Windows converts *to* when it decides not to hand
#: over the stored file, which is the failure the format guard exists to catch.
BMP_FORMAT_GUID = "{B96B3CAB-0728-11D3-9D7B-0000F81EF32E}"


def digest(data: bytes) -> str:
    """Content digest of a byte string, comparable with :func:`helpers.sha`."""
    return hashlib.sha256(data).hexdigest()[:16]


def canon_device(**kwargs: Any) -> FakeWiaDevice:
    """A device shaped the way a Canon card looks through WIA.

    ``Full Item Name`` on a real device is ``0000\\Root\\DCIM\\118CANON\\...``:
    a numeric device index and a synthetic ``Root`` that exist only in WIA's
    bookkeeping, wrapped around the folders that really are on the card. The
    fake derives full names from the tree, so the two bookkeeping levels are
    modelled as folders to reproduce the strings the parser must cope with.
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
                                        [
                                            FakeWiaFile("IMG_0001.JPG", PHOTO_A),
                                            FakeWiaFile("IMG_0002.JPG", PHOTO_B),
                                        ],
                                    ),
                                    FakeWiaFolder(
                                        "119CANON",
                                        [FakeWiaFile("IMG_0001.JPG", PHOTO_C)],
                                    ),
                                ],
                            )
                        ],
                    )
                ],
            )
        ],
        **kwargs,
    )


def flat_device(*files: FakeWiaFile, **kwargs: Any) -> FakeWiaDevice:
    """Every file hanging off the root, which is what the WPD shim often shows."""
    return FakeWiaDevice(items=list(files), **kwargs)


def import_private_copy() -> Any:
    """Execute ``wia.py`` again under a private name, leaving ``sys.modules``
    alone so no other test's view of the module changes."""
    spec = importlib.util.spec_from_file_location(
        "retrocam.backends._wia_import_probe", wia.__file__
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@contextlib.contextmanager
def no_pywin32(platform: str = "win32") -> Iterator[None]:
    """Windows with pywin32 absent, deterministically.

    Setting a ``sys.modules`` entry to ``None`` is the documented way to make an
    import fail; it is used instead of simply trusting that this machine has no
    pywin32, so the test asserts the same thing on a developer's Windows box.
    """
    names = ("pythoncom", "win32com", "win32com.client")
    sentinel = object()
    saved: Dict[str, Any] = {n: sys.modules.get(n, sentinel) for n in names}
    old_platform = sys.platform
    for name in names:
        sys.modules[name] = None  # type: ignore[assignment]
    sys.platform = platform  # type: ignore[assignment]
    try:
        yield
    finally:
        sys.platform = old_platform  # type: ignore[assignment]
        for name, previous in saved.items():
            if previous is sentinel:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


# --------------------------------------------------------------------------- #
# Small tools for bending the fake COM graph
# --------------------------------------------------------------------------- #


def connected_device(index: int = 1) -> Any:
    """The live fake device object, for tests that must bend the graph.

    Only valid inside a :func:`fake_wia` block. It goes through the backend's
    own ``_device_manager`` so the test reaches exactly the objects the code
    under test will reach.
    """
    manager = wia.WiaBackend._device_manager()
    return manager.DeviceInfos.Item(index).Connect()


def device_infos() -> Any:
    """The live ``DeviceInfos`` collection. Only valid inside :func:`fake_wia`."""
    return wia.WiaBackend._device_manager().DeviceInfos


def descend(root: Any, *indexes: int) -> Any:
    """The fake item reached by following 1-based collection indexes."""
    node = root
    for index in indexes:
        node = node.Items.Item(index)
    return node


def all_item_ids(items: Any) -> List[str]:
    """Every ``ItemID`` still present in a fake tree, folders included.

    Deletion assertions read this rather than a re-listing, because a listing
    only ever returns files: "the folder was not removed" is invisible there.
    """
    found: List[str] = []
    for index in range(1, items.Count + 1):
        entry = items.Item(index)
        found.append(entry.ItemID)
        found.extend(all_item_ids(entry.Items))
    return found


class _BentProperties:
    """A property collection with one driver quirk bolted on.

    Real drivers differ in which properties they implement and how they answer
    ``Exists``; this wrapper lets a single test bend a single behaviour without
    a bespoke class each time. ``hidden`` removes a property (the driver does
    not implement it), ``overrides`` changes one value, and ``exists_raises``
    reproduces the drivers that raise from ``Exists`` instead of answering it.
    """

    def __init__(
        self,
        inner: Any,
        overrides: Optional[Dict[int, Any]] = None,
        hidden: Sequence[int] = (),
        exists_raises: bool = False,
    ) -> None:
        self._inner = inner
        self._overrides = {str(int(k)): v for k, v in (overrides or {}).items()}
        self._hidden = {str(int(k)) for k in hidden}
        self._exists_raises = exists_raises

    def Exists(self, key: str) -> bool:
        if self._exists_raises:
            raise _ComError(text="this driver does not implement Exists")
        key = str(key)
        if key in self._hidden:
            return False
        return key in self._overrides or bool(self._inner.Exists(key))

    def Item(self, key: str) -> Any:
        key = str(key)
        if key in self._hidden:
            raise KeyError(key)
        if key in self._overrides:
            return types.SimpleNamespace(Value=self._overrides[key])
        return self._inner.Item(key)


def bend(item: Any, **kwargs: Any) -> Any:
    """Wrap one fake item's properties with :class:`_BentProperties`."""
    item.Properties = _BentProperties(item.Properties, **kwargs)
    return item


class _NameOnlyProperties:
    """A driver that rejects numeric property ids and answers only to names.

    Microsoft's automation samples index an *ImageFile*'s properties by the
    property id rendered as a string (``Img.Properties("40091")``) but index a
    *device item*'s properties by the English display name
    (``itm.Properties("Item Name")``). No published sample does the former on a
    camera item, so a driver shaped like this one is entirely plausible — and on
    it, an id-only backend reads nothing at all and reports every camera as
    empty. This is the fixture that proves the fallback works.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def _as_id(self, key: Any) -> Optional[str]:
        """The numeric key for a display name, or None for anything else."""
        for pid, name in wia._PROP_NAMES.items():
            if str(key) == name:
                return str(pid)
        return None

    def Exists(self, key: Any) -> bool:
        real = self._as_id(key)
        return False if real is None else bool(self._inner.Exists(real))

    def Item(self, key: Any) -> Any:
        real = self._as_id(key)
        if real is None:
            raise _ComError(text="unknown property")
        return self._inner.Item(real)


class _MuteProperties:
    """A driver that enumerates items and then describes none of them.

    Every lookup says no. This is what a wholly wrong property-access strategy
    would look like from the backend's side, and the point of the fixture is
    that it must not be mistaken for an empty memory card.
    """

    def Exists(self, key: Any) -> bool:
        return False

    def Item(self, key: Any) -> Any:
        raise _ComError(text="the driver does not implement this property")


class _RecordingProperties:
    """Passes everything through, remembering which keys were asked for."""

    def __init__(self, inner: Any, log: List[str]) -> None:
        self._inner = inner
        self._log = log

    def Exists(self, key: Any) -> bool:
        self._log.append(str(key))
        return bool(self._inner.Exists(key))

    def Item(self, key: Any) -> Any:
        self._log.append(str(key))
        return self._inner.Item(key)


class _TypedImageFile:
    """An ``ImageFile`` that says what format it actually holds.

    The real one has read-only ``FormatID`` and ``FileExtension`` properties.
    The shared fake omits them, which models a driver that leaves them unset;
    this one models a driver that fills them in — including the case that
    matters, where they say Windows converted the picture. ``format_id=None``
    and ``extension=None`` mean the property is absent, so reading it raises,
    exactly as it would on a driver that does not implement it.
    """

    def __init__(
        self,
        data: bytes,
        format_id: Optional[str] = None,
        extension: Optional[str] = None,
    ) -> None:
        self.FileData = types.SimpleNamespace(BinaryData=data)
        if format_id is not None:
            self.FormatID = format_id
        if extension is not None:
            self.FileExtension = extension


class _BrokenDeviceInfo:
    """A device Windows enumerates but cannot describe: reading its properties
    raises. One of these must not hide the healthy cameras behind it."""

    DeviceID = "{broken-device}"

    @property
    def Properties(self) -> Any:
        raise _ComError(text="The device is not ready")

    def Connect(self) -> Any:
        raise _ComError(text="The device is not ready")


class _UnreachableDeviceInfo:
    """A camera that enumerates but refuses to open — flat battery, asleep, or
    already claimed by another program. Detection must say so rather than
    pretending it is fine and failing later, mid-rescue."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.DeviceID = inner.DeviceID

    @property
    def Properties(self) -> Any:
        return self._inner.Properties

    def Connect(self) -> Any:
        raise _ComError(text="The device is in use")


class _EndlessFile:
    """A transferable item invented by :class:`_EndlessFolder`."""

    def __init__(self, depth: int) -> None:
        self.ItemID = "\\endless\\%d\\PHOTO.JPG" % depth
        self.Items = _FakeCollection([])
        self.Properties = _FakeProperties(
            {
                wia.WIA_IPA_ITEM_NAME: "PHOTO%d.JPG" % depth,
                wia.WIA_IPA_FULL_ITEM_NAME: self.ItemID,
                wia.WIA_IPA_ITEM_FLAGS: wia.WIA_ITEM_TYPE_IMAGE,
                wia.WIA_IPA_ITEM_SIZE: 10,
            }
        )


class _EndlessFolder:
    """A driver that invents a fresh sub-folder every time it is asked.

    Each level gets a new ``ItemID``, so the seen-ids guard cannot stop the
    descent and only the depth cap can. A buggy driver really can do this, and
    the GUI thread must not disappear into it. ``budget`` is a tripwire that
    turns a broken depth guard into a fast, readable failure instead of a hang.
    """

    TRIPWIRE = 200

    def __init__(self, depth: int, budget: List[int]) -> None:
        self.depth = depth
        self._budget = budget
        self.ItemID = "\\endless\\%d" % depth
        self.Properties = _FakeProperties(
            {
                wia.WIA_IPA_ITEM_NAME: "endless%d" % depth,
                wia.WIA_IPA_FULL_ITEM_NAME: self.ItemID,
                wia.WIA_IPA_ITEM_FLAGS: wia.WIA_ITEM_TYPE_FOLDER,
            }
        )

    @property
    def Items(self) -> Any:
        self._budget[0] += 1
        if self._budget[0] > self.TRIPWIRE:
            raise RuntimeError("depth guard failed: the walk never terminated")
        return _FakeCollection(
            [_EndlessFolder(self.depth + 1, self._budget), _EndlessFile(self.depth)]
        )


class WiaCase(TempDirCase):
    """Assertions shared by the transfer-facing tests."""

    def assertFileHolds(self, path: str, data: bytes) -> None:
        """The file exists and is byte-identical to the photograph expected."""
        self.assertTrue(os.path.isfile(path), "missing file: %s" % path)
        self.assertEqual(
            sha(path), digest(data), "wrong bytes at %s" % os.path.basename(path)
        )

    def assertDirHolds(self, dest: str, *names: str) -> None:
        """The destination holds exactly these names — no leftovers, no .part."""
        self.assertEqual(sorted(os.listdir(dest)), sorted(names))

    def listed(self, backend: Any, camera: Any) -> Dict[str, CameraFile]:
        """Listing keyed by device path, so tests name photos, not indexes."""
        return {cf.path: cf for cf in backend.list_files(camera)}


# --------------------------------------------------------------------------- #
# Import safety and availability
# --------------------------------------------------------------------------- #


class ImportSafetyTests(unittest.TestCase):
    """``registry.py`` imports every backend on every OS, so this module must
    import — and answer ``is_available`` — on a Mac with no pywin32 at all."""

    def test_module_imports_with_pywin32_missing(self) -> None:
        """A stray top-level ``import pythoncom`` would break the whole app on
        macOS, because ``registry.py`` imports this file at startup on every OS.
        Executing the file again with those imports blocked is the only way to
        prove one has not crept in: on the Windows machine where such an import
        would be added it works perfectly, and nothing else in the suite would
        notice until a Mac user opened the app.

        A private copy is imported rather than ``importlib.reload``: reloading
        would replace ``WiaBackend`` with a new class object while the registry
        still holds the old one, and the resulting identity failures would
        surface in someone else's test file, in test order.
        """
        with no_pywin32(platform=sys.platform):
            fresh = import_private_copy()

        self.assertIsNot(fresh, wia)
        self.assertFalse(fresh.WiaBackend.is_available()[0])

    def test_import_com_names_the_exact_pip_command(self) -> None:
        """The message is shown to the user verbatim, so it must be actionable."""
        with no_pywin32():
            with self.assertRaises(BackendUnavailable) as caught:
                wia._import_com()
        self.assertIn("pip install pywin32", str(caught.exception))

    def test_is_available_is_false_off_windows(self) -> None:
        """No exception, no import attempt: just a plain answer. This runs at
        startup on the GUI thread for every backend."""
        available, hint = wia.WiaBackend.is_available()
        self.assertFalse(available)
        self.assertIn("Windows", hint)

    def test_is_available_is_false_on_windows_without_pywin32(self) -> None:
        with no_pywin32():
            available, hint = wia.WiaBackend.is_available()
        self.assertFalse(available)
        self.assertIn("pywin32", hint)
        self.assertIn("pip install pywin32", hint)

    def test_is_available_is_true_when_windows_and_pywin32_are_present(self) -> None:
        with fake_wia([canon_device()]):
            self.assertEqual(wia.WiaBackend.is_available(), (True, ""))

    def test_is_available_does_not_create_the_device_manager(self) -> None:
        """Enumerating WIA devices is known to hang when the imaging service is
        unhealthy, and this runs on the GUI thread at startup. Probing must stay
        device-free — asserting no Dispatch happened is how that stays true."""
        with fake_wia([canon_device()]) as state:
            wia.WiaBackend.is_available()
            self.assertEqual(state.get("dispatched", []), [])

    def test_install_hint_is_the_pip_command(self) -> None:
        self.assertEqual(wia.WiaBackend.install_hint(), "pip install pywin32")

    def test_every_operation_refuses_politely_off_windows(self) -> None:
        """No method may explode with an ImportError or AttributeError when the
        GUI calls it on the wrong platform; each must fail its own gate."""
        backend = wia.WiaBackend()
        camera = CameraInfo(model="Camera", port="{id}", kind=BackendKind.WIA)
        for call in (
            lambda: backend.detect(),
            lambda: backend.list_files(camera),
            lambda: backend.download(camera, [], "/nowhere/this-must-not-be-created"),
            lambda: backend.delete(camera, []),
        ):
            with self.assertRaises(BackendUnavailable):
                call()

    def test_supports_delete_is_false_before_anything_has_been_listed(self) -> None:
        """A fresh backend has seen no access rights, so the GUI must grey the
        erase button out rather than offer it optimistically."""
        self.assertFalse(wia.WiaBackend().supports_delete())


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #


class DetectTests(unittest.TestCase):
    def test_detect_reads_model_vendor_and_port_from_the_right_properties(self) -> None:
        """Properties are addressed by numeric id because their English names are
        localised; a wrong id shows the user another device's name."""
        device = canon_device(
            name="PowerShot S30", vendor="Canon", port="\\\\.\\Usbscan0"
        )
        with fake_wia([device]):
            found = wia.WiaBackend().detect()

        self.assertEqual(len(found), 1)
        camera = found[0]
        self.assertEqual(camera.model, "Canon PowerShot S30")
        self.assertEqual(camera.kind, BackendKind.WIA)
        self.assertIn("\\\\.\\Usbscan0", camera.detail)
        self.assertTrue(camera.raw["reachable"])

    def test_detect_uses_the_device_id_as_the_port(self) -> None:
        """``port`` is passed back verbatim on every later call, so it must be
        the id that reconnects — not the human-readable port name, which is not
        unique and cannot be used to reopen the device."""
        device = canon_device(device_id="{DEVICE-ID-42}", port="\\\\.\\Usbscan0")
        with fake_wia([device]):
            camera = wia.WiaBackend().detect()[0]

        self.assertEqual(camera.port, "{DEVICE-ID-42}")
        self.assertEqual(camera.raw["device_id"], "{DEVICE-ID-42}")

    def test_detect_keeps_cameras_and_drops_scanners_and_webcams(self) -> None:
        """A flatbed scanner is a WIA device too. Offering to rescue photos from
        one, or worse to erase it, is not something to leave to chance."""
        camera = canon_device(device_id="{cam}", name="PowerShot S30")
        scanner = FakeWiaDevice(device_id="{scanner}", name="LiDE 60", device_type=1)
        webcam = FakeWiaDevice(device_id="{webcam}", name="HD Webcam", device_type=3)
        unspecified = FakeWiaDevice(device_id="{other}", name="Thing", device_type=0)

        with fake_wia([scanner, camera, webcam, unspecified]):
            found = wia.WiaBackend().detect()

        self.assertEqual([c.port for c in found], ["{cam}"])

    def test_detect_returns_an_empty_list_when_nothing_is_attached(self) -> None:
        """ "No camera" is a normal state, not an error: the GUI shows advice,
        not a stack trace."""
        progress = RecordingProgress()
        with fake_wia([]):
            found = wia.WiaBackend().detect(progress)

        self.assertEqual(found, [])
        self.assertTrue(progress.saw_phase("detect"))

    def test_detect_skips_a_device_it_cannot_describe(self) -> None:
        """One misbehaving driver must not hide the camera next to it."""
        progress = RecordingProgress()
        with fake_wia([canon_device(device_id="{good}")]):
            device_infos()._raw().insert(0, _BrokenDeviceInfo())
            found = wia.WiaBackend().detect(progress)

        self.assertEqual([c.port for c in found], ["{good}"])
        self.assertTrue(
            any("Skipped an imaging device" in m for m in progress.messages()),
            "the skip must be reported, not swallowed silently",
        )

    def test_detect_reports_a_camera_that_will_not_open(self) -> None:
        """Connecting during detection is deliberate: a camera that enumerates
        but refuses to open is reported honestly now, rather than failing later
        in the middle of a rescue the user has already committed to."""
        with fake_wia([canon_device(device_id="{asleep}")]):
            infos = device_infos()
            infos._raw()[0] = _UnreachableDeviceInfo(infos._raw()[0])
            found = wia.WiaBackend().detect()

        self.assertEqual(len(found), 1)
        self.assertFalse(found[0].raw["reachable"])
        self.assertIn("not responding", found[0].detail)
        self.assertIn("The device is in use", found[0].raw["connect_error"])

    def test_detect_raises_a_friendly_error_when_com_dispatch_fails(self) -> None:
        """No WIA service, no imaging stack: a CameraError the GUI can print,
        never a raw COM traceback."""
        failure = _ComError(-2147024891, "Access is denied")
        with fake_wia([canon_device()], dispatch_raises=failure):
            with self.assertRaises(CameraError) as caught:
                wia.WiaBackend().detect()

        message = str(caught.exception)
        self.assertIn("Access is denied", message)
        self.assertIn("0x80070005", message)
        self.assertIn("Windows Image Acquisition", message)


# --------------------------------------------------------------------------- #
# COM apartment discipline
# --------------------------------------------------------------------------- #


class ApartmentTests(unittest.TestCase):
    """COM is apartment-threaded: every thread touching WIA must call
    ``CoInitialize`` on entry and ``CoUninitialize`` on exit. Leaking an
    apartment per operation eventually wedges the process, and the Tk worker
    thread starts life with no apartment at all."""

    def test_each_operation_opens_and_closes_exactly_one_apartment(self) -> None:
        with fake_wia([canon_device()]) as state:
            backend = wia.WiaBackend()

            backend.detect()
            self.assertEqual((state["co_init"], state["co_uninit"]), (1, 1))

            camera = backend.detect()[0]
            before = state["co_init"]
            backend.list_files(camera)
            self.assertEqual(state["co_init"], before + 1)
            self.assertEqual(state["co_uninit"], state["co_init"])

    def test_the_apartment_is_closed_even_when_the_body_raises(self) -> None:
        """The failure path is the one that leaks in practice, because it is the
        one nobody runs twice."""
        with fake_wia([canon_device()], dispatch_raises=_ComError()) as state:
            with self.assertRaises(CameraError):
                wia.WiaBackend().detect()

            self.assertEqual(state["co_init"], 1)
            self.assertEqual(state["co_uninit"], 1)

    def test_the_apartment_is_closed_when_the_user_cancels(self) -> None:
        cancel = CancelToken()
        cancel.cancel()
        with fake_wia([canon_device()]) as state:
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            before = state["co_uninit"]
            with self.assertRaises(TransferAborted):
                backend.list_files(camera, cancel=cancel)

            self.assertEqual(state["co_uninit"], before + 1)
            self.assertEqual(state["co_uninit"], state["co_init"])


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


class ListFilesTests(unittest.TestCase):
    def test_the_walk_descends_into_nested_folders(self) -> None:
        """A non-recursive listing silently loses most of a Canon archive, since
        every photo lives in /DCIM/<nnn>CANON and none at the root."""
        with fake_wia([canon_device()]):
            backend = wia.WiaBackend()
            files = backend.list_files(backend.detect()[0])

        self.assertEqual(
            [cf.path for cf in files],
            [
                "/DCIM/118CANON/IMG_0001.JPG",
                "/DCIM/118CANON/IMG_0002.JPG",
                "/DCIM/119CANON/IMG_0001.JPG",
            ],
        )

    def test_folders_are_never_returned_as_files(self) -> None:
        """Handing a folder to the transfer engine would produce a zero-byte
        'photo' that then looks deletable."""
        with fake_wia([canon_device()]):
            backend = wia.WiaBackend()
            files = backend.list_files(backend.detect()[0])

        names = {cf.name for cf in files}
        self.assertEqual(names, {"IMG_0001.JPG", "IMG_0002.JPG"})
        for bookkeeping in ("DCIM", "118CANON", "119CANON", "Root", "0000"):
            self.assertNotIn(bookkeeping, names)

    def test_the_listing_is_sorted_by_folder_then_name(self) -> None:
        """Reproducible order is what makes resume and the progress bar honest;
        WIA hands items back in whatever order the driver feels like."""
        device = FakeWiaDevice(
            items=[
                FakeWiaFolder(
                    "DCIM",
                    [
                        FakeWiaFolder(
                            "119CANON", [FakeWiaFile("IMG_0001.JPG", PHOTO_C)]
                        ),
                        FakeWiaFolder(
                            "118CANON",
                            [
                                FakeWiaFile("IMG_0002.JPG", PHOTO_B),
                                FakeWiaFile("IMG_0001.JPG", PHOTO_A),
                            ],
                        ),
                    ],
                )
            ]
        )
        with fake_wia([device]):
            backend = wia.WiaBackend()
            files = backend.list_files(backend.detect()[0])

        self.assertEqual(
            [cf.path for cf in files],
            [
                "/DCIM/118CANON/IMG_0001.JPG",
                "/DCIM/118CANON/IMG_0002.JPG",
                "/DCIM/119CANON/IMG_0001.JPG",
            ],
        )

    def test_the_size_is_the_exact_byte_count_from_the_item_size_property(self) -> None:
        """Verification compares this number against the bytes on disk, so a
        rounded or estimated size would make the delete gate meaningless."""
        with fake_wia([canon_device()]):
            backend = wia.WiaBackend()
            files = self.listed_by_path(backend)

        self.assertEqual(files["/DCIM/118CANON/IMG_0001.JPG"].size, len(PHOTO_A))
        self.assertEqual(files["/DCIM/118CANON/IMG_0002.JPG"].size, len(PHOTO_B))
        self.assertEqual(files["/DCIM/119CANON/IMG_0001.JPG"].size, len(PHOTO_C))

    def test_an_unreported_size_stays_minus_one_and_is_never_invented(self) -> None:
        """-1 means "the driver does not know". It must never become 0 (a real
        size, for a corrupt file) and must never be back-filled from the bytes
        that arrive, or the transfer would be checking itself against itself."""
        device = flat_device(
            FakeWiaFile("SILENT.JPG", PHOTO_A, report_size=-1),
            FakeWiaFile("ZERO.JPG", PHOTO_B, report_size=0),
        )
        with fake_wia([device]):
            backend = wia.WiaBackend()
            files = self.listed_by_path(backend)

        for path in ("/SILENT.JPG", "/ZERO.JPG"):
            self.assertEqual(files[path].size, -1, path)
            self.assertFalse(files[path].size_known, path)

    def test_the_item_timestamp_is_decoded_from_its_systemtime_words(self) -> None:
        """Index 2 is day-of-week and must be skipped; reading it as the day
        would date every photo to the first week of the month."""
        import datetime

        device = flat_device(FakeWiaFile("IMG.JPG", PHOTO_A, mtime=SYSTEMTIME_WORDS))
        with fake_wia([device]):
            backend = wia.WiaBackend()
            files = backend.list_files(backend.detect()[0])

        expected = datetime.datetime(2003, 8, 13, 19, 22, 0).timestamp()
        self.assertEqual(files[0].mtime, expected)

    def test_an_unrecognised_timestamp_degrades_to_none(self) -> None:
        """mtime is cosmetic and never feeds verification, so a shape the
        decoder does not know must cost a timestamp, not the whole listing.

        TODO(hardware): confirm what ``WIA_IPA_ITEM_TIME`` actually is under the
        WPD-to-WIA shim. The fake's default is the 16-digit string form some
        PTP drivers use, which lands here (mtime None); the documented form is
        the vector of words covered by the test above.
        """
        device = flat_device(FakeWiaFile("IMG.JPG", PHOTO_A, mtime="2003081319220000"))
        with fake_wia([device]):
            backend = wia.WiaBackend()
            files = backend.list_files(backend.detect()[0])

        self.assertEqual(len(files), 1)
        self.assertIsNone(files[0].mtime)
        self.assertEqual(files[0].size, len(PHOTO_A))

    def test_a_driver_that_raises_from_exists_still_yields_properties(self) -> None:
        """``Properties.Exists`` is not universally implemented. Falling back to
        a direct lookup keeps the whole listing from vanishing on such a driver
        — which would read to the user as an empty memory card."""
        with fake_wia([canon_device()]):
            device = connected_device()
            folder = descend(device, 1, 1, 1, 1)  # 0000/Root/DCIM/118CANON
            for index in (1, 2):
                bend(folder.Items.Item(index), exists_raises=True)

            backend = wia.WiaBackend()
            files = self.listed_by_path(backend)

        first = files["/DCIM/118CANON/IMG_0001.JPG"]
        self.assertEqual(first.size, len(PHOTO_A))
        self.assertEqual(first.name, "IMG_0001.JPG")
        self.assertIs(first.raw["can_delete"], True)

    def test_a_bare_item_name_gains_the_extension_from_its_own_property(self) -> None:
        """``WIA_IPA_ITEM_NAME`` is usually the bare stem (``IMG_1870``) with the
        extension in a property of its own and no leading dot. Dropping it writes
        files neither Windows nor macOS will open, and no amount of verification
        would notice. The other shape — a name that already carries the
        extension — must not become ``IMG_0001.JPG.JPG``.

        TODO(hardware): confirm which shape a PTP camera reports through the WPD
        shim. Both are pinned here because the answer is genuinely unknown.
        """
        with fake_wia([canon_device()]):
            device = connected_device()
            folder = descend(device, 1, 1, 1, 1)  # 0000/Root/DCIM/118CANON
            bend(folder.Items.Item(1), overrides={wia.WIA_IPA_ITEM_NAME: "IMG_0001"})

            backend = wia.WiaBackend()
            files = backend.list_files(backend.detect()[0])

        self.assertEqual(
            [cf.path for cf in files],
            [
                "/DCIM/118CANON/IMG_0001.JPG",
                "/DCIM/118CANON/IMG_0002.JPG",
                "/DCIM/119CANON/IMG_0001.JPG",
            ],
        )

    def test_an_item_reported_twice_is_listed_once(self) -> None:
        """Some drivers alias the same node (a storage shortcut beside the root).
        Without the seen-ids guard those become an infinite descent.

        TODO(hardware): the guard keys on ``ItemID``, so a driver that gave two
        genuinely different photos the same id would drop one from the listing.
        Confirm on hardware that ids are unique per item — including when the
        tree is flattened and two folders' IMG_0001.JPG land side by side.
        """
        photo = FakeWiaFile("IMG_0001.JPG", PHOTO_A)
        device = FakeWiaDevice(
            items=[FakeWiaFolder("STORE", [photo]), FakeWiaFolder("STORE", [photo])]
        )
        with fake_wia([device]):
            backend = wia.WiaBackend()
            files = backend.list_files(backend.detect()[0])

        self.assertEqual([cf.path for cf in files], ["/STORE/IMG_0001.JPG"])

    def test_a_tree_that_never_ends_is_truncated_rather_than_followed(self) -> None:
        """Every level here has a fresh ItemID, so only the depth cap can stop
        the walk. Truncating a listing is bad; hanging the GUI forever is worse.

        The count is arithmetic, not a magic number: the root is depth 0 and
        holds only the folder, each of depths 1..12 contributes one file, and
        depth 13 returns immediately (``depth > _MAX_TREE_DEPTH``).
        """
        budget = [0]
        with fake_wia([FakeWiaDevice(items=[])]):
            device = connected_device()
            device.Items._raw().append(_EndlessFolder(0, budget))
            backend = wia.WiaBackend()
            files = backend.list_files(backend.detect()[0])

        self.assertEqual(len(files), wia._MAX_TREE_DEPTH)
        self.assertLess(budget[0], _EndlessFolder.TRIPWIRE)

    def test_deleted_items_are_not_offered_for_rescue(self) -> None:
        """An item flagged deleted is a tombstone; transferring it yields
        nothing and listing it inflates the count the user is shown."""
        with fake_wia([canon_device()]):
            device = connected_device()
            folder = descend(device, 1, 1, 1, 1)
            bend(
                folder.Items.Item(1),
                overrides={
                    wia.WIA_IPA_ITEM_FLAGS: (
                        wia.WIA_ITEM_TYPE_IMAGE | wia.WIA_ITEM_TYPE_DELETED
                    )
                },
            )
            backend = wia.WiaBackend()
            files = backend.list_files(backend.detect()[0])

        self.assertNotIn("/DCIM/118CANON/IMG_0001.JPG", [cf.path for cf in files])
        self.assertEqual(len(files), 2)

    def test_items_that_cannot_be_transferred_are_ignored(self) -> None:
        """Property-bag nodes with no data behind them exist on some devices."""
        with fake_wia([canon_device()]):
            device = connected_device()
            folder = descend(device, 1, 1, 1, 1)
            bend(folder.Items.Item(1), overrides={wia.WIA_IPA_ITEM_FLAGS: 0})
            backend = wia.WiaBackend()
            files = backend.list_files(backend.detect()[0])

        self.assertEqual(
            [cf.path for cf in files],
            ["/DCIM/118CANON/IMG_0002.JPG", "/DCIM/119CANON/IMG_0001.JPG"],
        )

    def test_the_camera_item_id_is_stored_as_a_string_not_a_com_object(self) -> None:
        """A live COM object dies with the apartment and cannot legally cross
        threads; storing one in ``CameraFile.raw`` would work in a unit test and
        crash the GUI. Only strings and plain values may survive the call."""
        with fake_wia([canon_device()]):
            backend = wia.WiaBackend()
            files = backend.list_files(backend.detect()[0])

        for camera_file in files:
            for key, value in camera_file.raw.items():
                self.assertIsInstance(
                    value,
                    (str, int, bool, type(None)),
                    "CameraFile.raw[%r] must not hold a live handle" % key,
                )
            self.assertTrue(camera_file.raw["item_id"])

    def test_an_empty_camera_says_so_instead_of_looking_broken(self) -> None:
        progress = RecordingProgress()
        with fake_wia([FakeWiaDevice(items=[])]):
            backend = wia.WiaBackend()
            files = backend.list_files(backend.detect()[0], progress)

        self.assertEqual(files, [])
        self.assertTrue(
            any("no photos" in m for m in progress.messages()), progress.messages()
        )

    def test_listing_can_be_cancelled(self) -> None:
        cancel = CancelToken()
        cancel.cancel()
        with fake_wia([canon_device()]):
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            with self.assertRaises(TransferAborted):
                backend.list_files(camera, cancel=cancel)

    # -- helper ------------------------------------------------------------- #

    def listed_by_path(self, backend: Any) -> Dict[str, CameraFile]:
        camera = backend.detect()[0]
        return {cf.path: cf for cf in backend.list_files(camera)}


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #


class DownloadTests(WiaCase):
    def test_photos_arrive_byte_identical_and_in_the_right_place(self) -> None:
        """Two folders hold an IMG_0001.JPG; both photographs must survive, each
        under its own name. Same name plus same size is exactly the pair that
        turns a naming bug into a lost picture."""
        dest = self.path("out")
        progress = RecordingProgress(fail_on_thread=True)
        with fake_wia([canon_device()]):
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            files = backend.list_files(camera)
            outcomes = backend.download(camera, files, dest, progress)

        self.assertTrue(all(o.ok for o in outcomes), [o.error for o in outcomes])
        self.assertEqual(len(outcomes), 3)
        self.assertDirHolds(
            dest, "IMG_0001.JPG", "IMG_0002.JPG", "119CANON_IMG_0001.JPG"
        )
        self.assertFileHolds(os.path.join(dest, "IMG_0001.JPG"), PHOTO_A)
        self.assertFileHolds(os.path.join(dest, "IMG_0002.JPG"), PHOTO_B)
        self.assertFileHolds(os.path.join(dest, "119CANON_IMG_0001.JPG"), PHOTO_C)

    def test_outcomes_are_one_per_input_file_in_the_same_order(self) -> None:
        """The caller counts on the 1:1 mapping to report "78 of 82 recovered"."""
        dest = self.path("out")
        with fake_wia([canon_device()]):
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            files = backend.list_files(camera)
            outcomes = backend.download(camera, files, dest)

        self.assertEqual([o.file.path for o in outcomes], [f.path for f in files])

    def test_no_part_file_survives_a_successful_transfer(self) -> None:
        """The temporary name exists only between write and rename; a leftover
        would look to the user like a second, broken copy."""
        dest = self.path("out")
        with fake_wia([canon_device()]):
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            backend.download(camera, backend.list_files(camera), dest)

        self.assertEqual([n for n in os.listdir(dest) if n.endswith(".part")], [])
        self.assertEqual([n for n in os.listdir(dest) if n.startswith(".rcr-")], [])

    def test_binary_data_delivered_as_a_tuple_of_ints_is_accepted(self) -> None:
        """pywin32's marshalling of ``VT_ARRAY|VT_UI1`` has never been confirmed
        on hardware for this project; it may be bytes, a memoryview, or a tuple
        of small ints. The tuple path is the one that would otherwise write a
        file full of ASCII digits.

        TODO(hardware): confirm the real type and, if it is always ``bytes``,
        simplify ``_coerce_bytes`` rather than leaving three untested branches.
        """
        dest = self.path("out")
        device = flat_device(FakeWiaFile("IMG_0001.JPG", PHOTO_A, returns_tuple=True))
        with fake_wia([device]):
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            outcomes = backend.download(camera, backend.list_files(camera), dest)

        self.assertTrue(outcomes[0].ok, outcomes[0].error)
        self.assertFileHolds(os.path.join(dest, "IMG_0001.JPG"), PHOTO_A)

    def test_a_size_mismatch_is_refused_and_nothing_is_written(self) -> None:
        """The "Windows silently re-encoded it to BMP" guard, and the strongest
        integrity signal this transport offers. If the delivered length does not
        match the size the camera reported, the bytes are not the photograph —
        writing them under the final name would replace the user's original with
        a lossy re-encode that verification would then happily bless."""
        dest = self.path("out")
        device = flat_device(
            FakeWiaFile("IMG_0001.JPG", PHOTO_A, report_size=len(PHOTO_A) + 500)
        )
        with fake_wia([device]):
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            outcomes = backend.download(camera, backend.list_files(camera), dest)

        self.assertFalse(outcomes[0].ok)
        self.assertIsNone(outcomes[0].dest_path)
        self.assertIn(str(len(PHOTO_A)), outcomes[0].error)
        self.assertIn(str(len(PHOTO_A) + 500), outcomes[0].error)
        # Not under its final name, and not under the temporary one either.
        self.assertDirHolds(dest)

    def test_an_empty_transfer_is_refused(self) -> None:
        """A zero-byte file that carries a photo's name is the worst possible
        outcome: it looks like a successful rescue right up until the original
        has been erased."""
        dest = self.path("out")
        with fake_wia([flat_device(FakeWiaFile("IMG_0001.JPG", b""))]):
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            outcomes = backend.download(camera, backend.list_files(camera), dest)

        self.assertFalse(outcomes[0].ok)
        self.assertIsNone(outcomes[0].dest_path)
        self.assertIn("empty", outcomes[0].error.lower())
        self.assertDirHolds(dest)

    def test_one_failed_transfer_does_not_end_the_batch(self) -> None:
        """One unreadable photo on a twenty-year-old card must not cost the user
        the other eighty-one."""
        dest = self.path("out")
        device = flat_device(
            FakeWiaFile("IMG_0001.JPG", PHOTO_A),
            FakeWiaFile("IMG_0002.JPG", PHOTO_B, transfer_raises=True),
            FakeWiaFile("IMG_0003.JPG", PHOTO_C),
        )
        with fake_wia([device]):
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            outcomes = backend.download(camera, backend.list_files(camera), dest)

        self.assertEqual([o.ok for o in outcomes], [True, False, True])
        self.assertIn("transfer failed", outcomes[1].error)
        self.assertIsNone(outcomes[1].dest_path)
        self.assertDirHolds(dest, "IMG_0001.JPG", "IMG_0003.JPG")
        self.assertFileHolds(os.path.join(dest, "IMG_0003.JPG"), PHOTO_C)

    def test_the_stored_format_is_requested_so_windows_does_not_re_encode(self) -> None:
        """Asking for the item's own format is what makes this a file copy
        rather than a decode-and-re-encode, which would change every byte and
        destroy the EXIF the photo is half the value of."""
        dest = self.path("out")
        requested: List[Any] = []
        with fake_wia([flat_device(FakeWiaFile("IMG_0001.JPG", PHOTO_A))]):
            item = descend(connected_device(), 1)
            bend(item, overrides={wia.WIA_IPA_FORMAT: JPEG_FORMAT_GUID})

            def transfer(fmt: Any = None) -> Any:
                requested.append(fmt)
                return _FakeImageFile(PHOTO_A)

            item.Transfer = transfer

            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            outcomes = backend.download(camera, backend.list_files(camera), dest)

        self.assertTrue(outcomes[0].ok, outcomes[0].error)
        self.assertEqual(requested, [JPEG_FORMAT_GUID])
        self.assertFileHolds(os.path.join(dest, "IMG_0001.JPG"), PHOTO_A)

    def test_a_driver_that_rejects_the_stored_format_gets_one_plain_retry(self) -> None:
        """Some drivers refuse an explicit format argument outright. Retrying
        bare recovers the photo; not retrying loses it for a formality.

        TODO(hardware): a bare ``Transfer()`` is the call most likely to hand
        back a converted image, so confirm on hardware that the size check below
        is what catches that — this retry is deliberately not a free pass.
        """
        dest = self.path("out")
        requested: List[Any] = []
        with fake_wia([flat_device(FakeWiaFile("IMG_0001.JPG", PHOTO_A))]):
            item = descend(connected_device(), 1)
            bend(item, overrides={wia.WIA_IPA_FORMAT: JPEG_FORMAT_GUID})

            def picky_transfer(fmt: Any = None) -> Any:
                requested.append(fmt)
                if fmt:
                    raise _ComError(text="the format is not supported")
                return _FakeImageFile(PHOTO_A)

            item.Transfer = picky_transfer

            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            outcomes = backend.download(camera, backend.list_files(camera), dest)

        self.assertEqual(requested, [JPEG_FORMAT_GUID, None])
        self.assertTrue(outcomes[0].ok, outcomes[0].error)
        self.assertFileHolds(os.path.join(dest, "IMG_0001.JPG"), PHOTO_A)

    def test_a_file_that_vanished_between_listing_and_download_is_reported(
        self,
    ) -> None:
        """The camera can be unplugged, or the user can erase from its menu,
        between two GUI actions. That is a failed file, not a crash."""
        dest = self.path("out")
        with fake_wia([canon_device()]):
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            files = backend.list_files(camera)
            ghost = CameraFile(
                folder="/DCIM/118CANON",
                name="GONE.JPG",
                size=10,
                raw={"item_id": "\\0000\\Root\\DCIM\\118CANON\\GONE.JPG"},
            )
            outcomes = backend.download(camera, [ghost, files[0]], dest)

        self.assertFalse(outcomes[0].ok)
        self.assertIn("no longer on the camera", outcomes[0].error)
        self.assertTrue(outcomes[1].ok)
        self.assertDirHolds(dest, "IMG_0001.JPG")

    def test_the_destination_directory_is_created_when_missing(self) -> None:
        dest = self.path("out", "rescued-photos")
        with fake_wia([flat_device(FakeWiaFile("IMG_0001.JPG", PHOTO_A))]):
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            backend.download(camera, backend.list_files(camera), dest)

        self.assertFileHolds(os.path.join(dest, "IMG_0001.JPG"), PHOTO_A)

    def test_an_unwritable_destination_fails_before_any_transfer(self) -> None:
        """Finding out after twenty minutes of USB 1.1 that nothing could be
        saved is not acceptable; the check happens before the first byte."""
        parent = self.path("locked")
        os.makedirs(parent)
        make_read_only(parent)
        with fake_wia([canon_device()]) as state:
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            files = backend.list_files(camera)
            with self.assertRaises(CameraError) as caught:
                backend.download(camera, files, os.path.join(parent, "out"))

        self.assertIn("Cannot write to the destination folder", str(caught.exception))
        self.assertEqual(state["transfers"], [], "nothing may be pulled off the camera")

    def test_progress_reports_one_tick_per_file_with_the_expected_totals(self) -> None:
        """The bar moves once per file on this transport — ``Item.Transfer`` is
        a single blocking call with no chunk callback — so the totals are all
        the GUI has to work with."""
        dest = self.path("out")
        progress = RecordingProgress()
        with fake_wia([canon_device()]):
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            files = backend.list_files(camera)
            backend.download(camera, files, dest, progress)

        ticks = progress.of_phase("download")
        self.assertEqual([t.index for t in ticks], [0, 0, 1, 1, 2, 2])
        self.assertTrue(all(t.total == 3 for t in ticks))
        self.assertEqual(ticks[0].name, files[0].name)
        self.assertEqual(ticks[1].bytes_done, len(PHOTO_A))

    def test_cancelling_between_files_aborts_and_leaves_no_litter(self) -> None:
        """WIA cannot interrupt a transfer in flight, so between files is the
        only honest cancellation point. What matters is that the photos already
        rescued stay, and that no half-written file is left behind."""
        dest = self.path("out")
        cancel = CancelToken()
        seen: List[str] = []

        def cancel_after_first(tick: Any) -> None:
            if tick.phase == "download":
                seen.append(tick.name)
                cancel.cancel()

        with fake_wia([canon_device()]) as state:
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            files = backend.list_files(camera)
            with self.assertRaises(TransferAborted):
                backend.download(camera, files, dest, cancel_after_first, cancel)

        # The first file was already in flight and is kept; nothing after it ran.
        self.assertEqual(len(state["transfers"]), 1)
        self.assertDirHolds(dest, "IMG_0001.JPG")
        self.assertFileHolds(os.path.join(dest, "IMG_0001.JPG"), PHOTO_A)
        self.assertEqual(set(seen), {"IMG_0001.JPG"})


# --------------------------------------------------------------------------- #
# Resume / skip_existing
# --------------------------------------------------------------------------- #


class SkipExistingTests(WiaCase):
    """Resume support is where a "harmless optimisation" turns into data loss:
    a file wrongly declared already-downloaded is verified against someone
    else's bytes and then cleared for erasure from the camera."""

    def test_a_second_photo_is_never_credited_to_the_first_photos_copy(self) -> None:
        """Review finding F3, in the flattened tree the WPD shim produces.

        Two different photographs, both called IMG_0001.JPG, both 1026 bytes.
        The first resolves to IMG_0001.JPG. Without the batch 'claimed' set the
        second one matches that same file — right name, right size, wrong
        picture — comes back ``skipped=True, ok=True``, and the transfer engine
        then verifies photo A's bytes and clears photo B for deletion from the
        camera. The photograph is gone and every check passed.
        """
        dest = self.path("out")
        device = flat_device(
            FakeWiaFile("IMG_0001.JPG", PHOTO_A),
            FakeWiaFile("IMG_0001.JPG", PHOTO_C),
        )
        with fake_wia([device]):
            # The fake derives ItemID from the path, so two same-named files at
            # one level would otherwise collapse into one item. Give the second
            # the distinct id a real driver must give it.
            connected_device().Items._raw()[1].ItemID = "\\IMG_0001.JPG(2)"

            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            files = backend.list_files(camera)
            self.assertEqual([cf.name for cf in files], ["IMG_0001.JPG"] * 2)
            self.assertEqual(files[0].size, files[1].size)

            first_run = backend.download(camera, files, dest)
            # A resumed run over an already-complete destination: the same two
            # files, the same order, nothing left to do.
            second_run = backend.download(camera, files, dest, skip_existing=True)

        self.assertEqual([o.skipped for o in first_run], [False, False])
        self.assertEqual([o.skipped for o in second_run], [True, True])

        names = [os.path.basename(o.dest_path or "") for o in second_run]
        self.assertEqual(names, ["IMG_0001.JPG", "DCIM_IMG_0001.JPG"])
        self.assertNotEqual(
            second_run[0].dest_path,
            second_run[1].dest_path,
            "two photos were credited to the same copy",
        )
        self.assertFileHolds(os.path.join(dest, "IMG_0001.JPG"), PHOTO_A)
        self.assertFileHolds(os.path.join(dest, "DCIM_IMG_0001.JPG"), PHOTO_C)

    def test_the_claim_also_holds_across_folders(self) -> None:
        """The same failure with a nested tree: /DCIM/118CANON/IMG_0001.JPG and
        /DCIM/119CANON/IMG_0001.JPG are different photographs of the same size."""
        dest = self.path("out")
        with fake_wia([canon_device()]):
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            files = backend.list_files(camera)
            backend.download(camera, files, dest)
            resumed = backend.download(camera, files, dest)

        self.assertTrue(all(o.skipped for o in resumed))
        self.assertEqual(
            [os.path.basename(o.dest_path or "") for o in resumed],
            ["IMG_0001.JPG", "IMG_0002.JPG", "119CANON_IMG_0001.JPG"],
        )
        self.assertFileHolds(os.path.join(dest, "IMG_0001.JPG"), PHOTO_A)
        self.assertFileHolds(os.path.join(dest, "119CANON_IMG_0001.JPG"), PHOTO_C)

    def test_a_copy_of_the_wrong_size_is_re_downloaded(self) -> None:
        """A truncated leftover from an interrupted run has the right name and
        the wrong length. Trusting the name alone would keep the truncation and
        then authorise erasing the good original."""
        dest = self.path("out")
        os.makedirs(dest)
        with open(os.path.join(dest, "IMG_0001.JPG"), "wb") as handle:
            handle.write(PHOTO_A[:200])

        with fake_wia([flat_device(FakeWiaFile("IMG_0001.JPG", PHOTO_A))]):
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            outcomes = backend.download(camera, backend.list_files(camera), dest)

        self.assertFalse(outcomes[0].skipped)
        self.assertTrue(outcomes[0].ok)
        # The stub is left alone and the good copy lands beside it, rather than
        # being overwritten in place where a failure would destroy both.
        self.assertEqual(os.path.getsize(os.path.join(dest, "IMG_0001.JPG")), 200)
        self.assertFileHolds(os.path.join(dest, "DCIM_IMG_0001.JPG"), PHOTO_A)

    def test_a_file_of_unknown_size_is_never_skipped(self) -> None:
        """With no size from the camera there is no evidence the existing file
        holds the right bytes, and "a file with that name exists" is not
        evidence. Re-downloading costs time; the alternative costs photographs."""
        dest = self.path("out")
        os.makedirs(dest)
        with open(os.path.join(dest, "IMG_0001.JPG"), "wb") as handle:
            handle.write(PHOTO_A)

        device = flat_device(FakeWiaFile("IMG_0001.JPG", PHOTO_A, report_size=-1))
        with fake_wia([device]) as state:
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            outcomes = backend.download(camera, backend.list_files(camera), dest)

        self.assertFalse(outcomes[0].skipped)
        self.assertEqual(len(state["transfers"]), 1)

    def test_skip_existing_false_transfers_everything_again(self) -> None:
        dest = self.path("out")
        with fake_wia([canon_device()]) as state:
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            files = backend.list_files(camera)
            backend.download(camera, files, dest)
            outcomes = backend.download(camera, files, dest, skip_existing=False)

        self.assertEqual([o.skipped for o in outcomes], [False, False, False])
        self.assertEqual(len(state["transfers"]), 6)


# --------------------------------------------------------------------------- #
# Delete
# --------------------------------------------------------------------------- #


class DeleteTests(WiaCase):
    """Erasing is irreversible and happens on a device holding the only copy of
    twenty-year-old photographs. Every test here is about *not* touching
    something that was not asked for."""

    def test_delete_removes_exactly_the_requested_items(self) -> None:
        with fake_wia([canon_device()]) as state:
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            files = self.listed(backend, camera)
            target = files["/DCIM/118CANON/IMG_0002.JPG"]

            outcomes = backend.delete(camera, [target])
            remaining = [cf.path for cf in backend.list_files(camera)]

        self.assertEqual([o.ok for o in outcomes], [True])
        self.assertEqual(state["deleted"], [target.raw["item_id"]])
        self.assertEqual(
            remaining,
            ["/DCIM/118CANON/IMG_0001.JPG", "/DCIM/119CANON/IMG_0001.JPG"],
        )

    def test_deleting_one_of_two_identically_named_photos_spares_the_other(
        self,
    ) -> None:
        """The single worst bug this program could have. Both files are called
        IMG_0001.JPG and both are 1026 bytes; only the one in 119CANON was
        asked for, and the one in 118CANON must still be on the camera."""
        with fake_wia([canon_device()]) as state:
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            files = self.listed(backend, camera)
            target = files["/DCIM/119CANON/IMG_0001.JPG"]

            outcomes = backend.delete(camera, [target])
            survivors = [cf.path for cf in backend.list_files(camera)]

        self.assertTrue(outcomes[0].ok, outcomes[0].error)
        self.assertEqual(state["deleted"], [target.raw["item_id"]])
        self.assertIn("/DCIM/118CANON/IMG_0001.JPG", survivors)
        self.assertNotIn("/DCIM/119CANON/IMG_0001.JPG", survivors)

    def test_the_index_is_re_resolved_before_every_removal(self) -> None:
        """``Items.Remove`` takes an index, and every successful removal shifts
        the ones after it. Deleting the first and third files of a folder with
        indexes captured at listing time erases the first and then whatever has
        slid into third place — a photo the user never selected."""
        photos = [
            FakeWiaFile("IMG_0001.JPG", PHOTO_A),
            FakeWiaFile("IMG_0002.JPG", PHOTO_B),
            FakeWiaFile("IMG_0003.JPG", PHOTO_C),
        ]
        with fake_wia([FakeWiaDevice(items=[FakeWiaFolder("DCIM", photos)])]) as state:
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            files = self.listed(backend, camera)
            first = files["/DCIM/IMG_0001.JPG"]
            third = files["/DCIM/IMG_0003.JPG"]

            outcomes = backend.delete(camera, [first, third])
            survivors = [cf.path for cf in backend.list_files(camera)]

        self.assertEqual([o.ok for o in outcomes], [True, True])
        self.assertEqual(state["deleted"], [first.raw["item_id"], third.raw["item_id"]])
        self.assertEqual(survivors, ["/DCIM/IMG_0002.JPG"])

    def test_delete_never_removes_a_folder(self) -> None:
        """``WIA_CMD_DELETE_ALL_ITEMS`` exists and is never issued. Emptying a
        folder must leave the folder — anything else is one step from formatting
        the card."""
        with fake_wia([canon_device()]):
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            files = self.listed(backend, camera)
            backend.delete(
                camera,
                [
                    files["/DCIM/118CANON/IMG_0001.JPG"],
                    files["/DCIM/118CANON/IMG_0002.JPG"],
                ],
            )
            survivors = all_item_ids(connected_device().Items)

        self.assertIn("\\0000\\Root\\DCIM\\118CANON", survivors)
        self.assertIn("\\0000\\Root\\DCIM", survivors)
        self.assertIn("\\0000\\Root", survivors)
        self.assertNotIn("\\0000\\Root\\DCIM\\118CANON\\IMG_0001.JPG", survivors)

    def test_a_file_the_camera_marks_read_only_is_refused_not_attempted(self) -> None:
        """Protected files exist (the in-camera lock flag). Refusing before
        touching the collection keeps a doomed removal from disturbing it."""
        device = flat_device(FakeWiaFile("IMG_0001.JPG", PHOTO_A, can_delete=False))
        with fake_wia([device]) as state:
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            files = backend.list_files(camera)
            self.assertIs(files[0].raw["can_delete"], False)

            outcomes = backend.delete(camera, files)
            survivors = all_item_ids(connected_device().Items)

        self.assertFalse(outcomes[0].ok)
        self.assertIn("cannot be erased", outcomes[0].error)
        self.assertEqual(state["deleted"], [], "no removal may even be attempted")
        self.assertIn("\\IMG_0001.JPG", survivors)

    def test_a_camera_that_refuses_at_the_last_moment_is_reported(self) -> None:
        """Unknown deletability means *attempt* — the gate upstream has already
        decided this file is safe to erase. What must not happen is an exception
        escaping into the GUI, or the batch stopping.

        TODO(hardware): confirm which HRESULT a real PTP camera returns when it
        refuses an erase, so the message can name the cause rather than quoting
        a hex code at a non-technical user.
        """
        device = flat_device(
            FakeWiaFile("IMG_0001.JPG", PHOTO_A, can_delete=False),
            FakeWiaFile("IMG_0002.JPG", PHOTO_B),
        )
        with fake_wia([device]):
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            files = backend.list_files(camera)
            # The driver advertises nothing about deletability, then refuses:
            # the access-rights property is not a promise.
            files[0].raw["can_delete"] = None

            outcomes = backend.delete(camera, files)
            survivors = all_item_ids(connected_device().Items)

        self.assertEqual([o.ok for o in outcomes], [False, True])
        self.assertIn("refused to erase", outcomes[0].error)
        self.assertIn("item is read-only", outcomes[0].error)
        self.assertIn("\\IMG_0001.JPG", survivors)
        self.assertNotIn("\\IMG_0002.JPG", survivors)

    def test_a_file_that_is_already_gone_is_reported_not_counted_as_erased(
        self,
    ) -> None:
        """The count the user is shown has to stay honest, and "already gone"
        might also mean "the ids moved under us", which is not a success."""
        with fake_wia([canon_device()]):
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            files = self.listed(backend, camera)
            target = files["/DCIM/118CANON/IMG_0001.JPG"]

            self.assertTrue(backend.delete(camera, [target])[0].ok)
            second = backend.delete(camera, [target])

        self.assertFalse(second[0].ok)
        self.assertIn("no longer on the camera", second[0].error)

    def test_a_file_with_no_item_id_is_refused(self) -> None:
        """Defence in depth: a CameraFile from another backend, or a corrupted
        one, must not reach ``Items.Remove`` with an empty id."""
        with fake_wia([canon_device()]) as state:
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            stray = CameraFile(folder="/DCIM", name="IMG_0001.JPG", size=10, raw={})
            outcomes = backend.delete(camera, [stray])

        self.assertFalse(outcomes[0].ok)
        self.assertEqual(state["deleted"], [])

    def test_cancelling_stops_the_next_erasure(self) -> None:
        """Between files is the only cancellation point, and it must actually
        stop the batch rather than merely reporting it did."""
        cancel = CancelToken()

        def cancel_at_once(tick: Any) -> None:
            if tick.phase == "delete":
                cancel.cancel()

        with fake_wia([canon_device()]) as state:
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            files = backend.list_files(camera)
            with self.assertRaises(TransferAborted):
                backend.delete(camera, files, cancel_at_once, cancel)

            survivors = [cf.path for cf in backend.list_files(camera)]

        self.assertEqual(len(state["deleted"]), 1)
        self.assertEqual(len(survivors), 2)


# --------------------------------------------------------------------------- #
# supports_delete
# --------------------------------------------------------------------------- #


class SupportsDeleteTests(unittest.TestCase):
    """Conservative by design. A false negative costs a greyed-out button and a
    manual erase from the camera menu; a false positive costs an erase that
    fails halfway through a batch on a device holding the only copies."""

    def test_true_only_after_an_item_advertised_the_delete_bit(self) -> None:
        with fake_wia([canon_device()]):
            backend = wia.WiaBackend()
            self.assertFalse(backend.supports_delete())
            camera = backend.detect()[0]
            self.assertFalse(
                backend.supports_delete(), "detection alone proves nothing"
            )
            backend.list_files(camera)
            self.assertTrue(backend.supports_delete())

    def test_false_when_no_item_carries_the_delete_bit(self) -> None:
        device = flat_device(FakeWiaFile("IMG_0001.JPG", PHOTO_A, can_delete=False))
        with fake_wia([device]):
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            files = backend.list_files(camera)

        self.assertIs(files[0].raw["can_delete"], False)
        self.assertFalse(backend.supports_delete())

    def test_false_when_the_driver_omits_the_access_rights_property(self) -> None:
        """Absent is unknown, not "yes". The per-file answer stays ``None`` so
        an explicit refusal can still be told apart from silence.

        TODO(hardware): confirm how widely the WPD-to-WIA shim populates
        ``WIA_IPA_ACCESS_RIGHTS`` for PTP cameras. If devices that *can* erase
        routinely omit it, this needs an explicit user override rather than a
        looser default.
        """
        with fake_wia([canon_device()]):
            backend = wia.WiaBackend()
            device = connected_device()
            folder = descend(device, 1, 1, 1, 1)
            for index in (1, 2):
                bend(folder.Items.Item(index), hidden=[wia.WIA_IPA_ACCESS_RIGHTS])
            folder_119 = descend(device, 1, 1, 1, 2)
            bend(folder_119.Items.Item(1), hidden=[wia.WIA_IPA_ACCESS_RIGHTS])

            camera = backend.detect()[0]
            files = backend.list_files(camera)

        self.assertTrue(files)
        for camera_file in files:
            self.assertIsNone(camera_file.raw["can_delete"])
        self.assertFalse(backend.supports_delete())

    def test_the_deletable_bit_is_0x80_and_not_0x04(self) -> None:
        """0x04 is the value that is easy to mis-remember. Reading it instead
        would light the erase button up on a read-only device — this test is the
        only thing standing between that typo and a released binary."""
        self.assertEqual(wia.WIA_ITEM_CAN_BE_DELETED, 0x80)

        with fake_wia([flat_device(FakeWiaFile("IMG_0001.JPG", PHOTO_A))]):
            item = descend(connected_device(), 1)
            bend(
                item,
                overrides={
                    wia.WIA_IPA_ACCESS_RIGHTS: (
                        wia.WIA_ITEM_READ | wia.WIA_ITEM_WRITE | 0x04
                    )
                },
            )
            backend = wia.WiaBackend()
            files = backend.list_files(backend.detect()[0])

        self.assertIs(files[0].raw["can_delete"], False)
        self.assertFalse(backend.supports_delete())

    def test_a_later_listing_replaces_the_earlier_answer(self) -> None:
        """Cameras get swapped. The cached answer must describe the device that
        was listed last, not the most permissive one ever seen."""
        deletable = flat_device(FakeWiaFile("IMG_0001.JPG", PHOTO_A))
        protected = flat_device(
            FakeWiaFile("IMG_0001.JPG", PHOTO_A, can_delete=False),
            device_id="{second}",
        )
        backend = wia.WiaBackend()
        with fake_wia([deletable]):
            backend.list_files(backend.detect()[0])
            self.assertTrue(backend.supports_delete())
        with fake_wia([protected]):
            backend.list_files(backend.detect()[0])
            self.assertFalse(backend.supports_delete())


# --------------------------------------------------------------------------- #
# The silent re-encode guard
# --------------------------------------------------------------------------- #


class TransferFormatTests(WiaCase):
    """``Item.Transfer(FormatID)`` is documented to return the requested format
    "if the device supports that format; **otherwise this method uses the
    preferred format for this imaging device**" — a substitution made silently,
    with no error and no flag. What comes back is then a decoded, re-encoded
    picture instead of the file on the card: different bytes, no EXIF, and, if
    it lands as a JPEG, something that passes every structural check this
    program can make before the original is erased.

    The length comparison catches this only when the camera reported a size, and
    ``WIA_IPA_ITEM_SIZE`` is documented as zero — meaning "no information" —
    whenever the driver does not know, a case Microsoft calls "common for
    compressed data". That is every JPEG on the card. So the size guard cannot
    be the only guard, and these tests are about the one that works without it.
    """

    def transfer_returning(self, image: Any, **file_kwargs: Any) -> Any:
        """A one-file device whose transfer hands back ``image``."""
        device = flat_device(FakeWiaFile("IMG_0001.JPG", PHOTO_A, **file_kwargs))
        return device

    def run_one(self, device: Any, image: Any, fmt: Optional[str]) -> Any:
        """Download the device's single file, with ``item.Transfer`` scripted."""
        dest = self.path("out")
        with fake_wia([device]):
            item = descend(connected_device(), 1)
            if fmt is not None:
                bend(item, overrides={wia.WIA_IPA_FORMAT: fmt})
            item.Transfer = lambda _fmt=None: image

            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            outcomes = backend.download(camera, backend.list_files(camera), dest)
        return outcomes[0], dest

    def test_a_photo_returned_in_another_format_is_refused(self) -> None:
        """The camera stores JPEG; Windows hands back BMP. The delivered length
        is deliberately identical to the size the camera reported, so the size
        guard cannot fire and only the format check stands between a lossy
        re-encode and the user's original being cleared for deletion."""
        image = _TypedImageFile(PHOTO_A, format_id=BMP_FORMAT_GUID)
        outcome, dest = self.run_one(
            self.transfer_returning(image), image, JPEG_FORMAT_GUID
        )

        self.assertFalse(outcome.ok)
        self.assertIsNone(outcome.dest_path)
        self.assertIn("converted", outcome.error)
        self.assertDirHolds(dest)

    def test_the_same_format_written_differently_is_still_accepted(self) -> None:
        """The control the refusal above needs. Nothing guarantees a driver
        echoes a GUID back with the same braces or the same case, and a guard
        that rejected ``b96b3cae-...`` as "different from ``{B96B3CAE-...}``"
        would refuse every healthy transfer on such a device — losing photos to
        protect them."""
        image = _TypedImageFile(
            PHOTO_A, format_id="b96b3cae-0728-11d3-9d7b-0000f81ef32e"
        )
        outcome, dest = self.run_one(
            self.transfer_returning(image), image, JPEG_FORMAT_GUID
        )

        self.assertTrue(outcome.ok, outcome.error)
        self.assertFileHolds(os.path.join(dest, "IMG_0001.JPG"), PHOTO_A)

    def test_a_bitmap_is_refused_even_when_the_camera_reported_no_size(self) -> None:
        """The case the size guard cannot reach, and the reason the extension is
        read at all: the driver reports no size (documented as "common for
        compressed data") *and* no FormatID, so the only thing left saying this
        is not the stored photograph is that Windows called it a .bmp."""
        image = _TypedImageFile(PHOTO_A, extension="bmp")
        device = self.transfer_returning(image, report_size=-1)
        outcome, dest = self.run_one(device, image, JPEG_FORMAT_GUID)

        self.assertFalse(outcome.ok)
        self.assertIn(".bmp", outcome.error)
        self.assertDirHolds(dest)

    def test_a_jpeg_of_unknown_size_reported_as_a_jpeg_is_kept(self) -> None:
        """Control: the same size-less path must still rescue a healthy photo."""
        image = _TypedImageFile(PHOTO_A, extension="jpg")
        device = self.transfer_returning(image, report_size=-1)
        outcome, dest = self.run_one(device, image, JPEG_FORMAT_GUID)

        self.assertTrue(outcome.ok, outcome.error)
        self.assertFileHolds(os.path.join(dest, "IMG_0001.JPG"), PHOTO_A)

    def test_a_video_clip_is_not_mistaken_for_a_conversion(self) -> None:
        """The other half of the control. These cameras record AVI as well as
        JPEG, and an extension check that fired on anything unfamiliar would
        quietly make every clip unrescuable — a guard that loses files is not a
        safety feature."""
        clip = riff_avi(400)
        image = _TypedImageFile(clip, extension="avi")
        dest = self.path("out")
        device = flat_device(FakeWiaFile("MVI_0001.AVI", clip, report_size=-1))
        with fake_wia([device]):
            item = descend(connected_device(), 1)
            item.Transfer = lambda _fmt=None: image
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            outcomes = backend.download(camera, backend.list_files(camera), dest)

        self.assertTrue(outcomes[0].ok, outcomes[0].error)
        self.assertFileHolds(os.path.join(dest, "MVI_0001.AVI"), clip)

    def test_a_driver_that_reports_nothing_about_the_format_is_trusted(self) -> None:
        """A driver that implements neither FormatID nor FileExtension must not
        have every transfer refused: unreadable means *unknown*, never
        *different*. This is the shape of the shared fake, and therefore the
        shape every other test in this file depends on."""
        image = _TypedImageFile(PHOTO_A)
        outcome, dest = self.run_one(
            self.transfer_returning(image), image, JPEG_FORMAT_GUID
        )

        self.assertTrue(outcome.ok, outcome.error)
        self.assertFileHolds(os.path.join(dest, "IMG_0001.JPG"), PHOTO_A)


# --------------------------------------------------------------------------- #
# How properties are addressed
# --------------------------------------------------------------------------- #


class PropertyAccessTests(unittest.TestCase):
    """Every fact this backend knows about a file arrives through
    ``Properties``. Address them the wrong way and nothing raises — every read
    returns the default, every item looks untransferable, and the listing comes
    back empty. The user is then told their memory card is blank."""

    def test_a_driver_that_only_answers_to_display_names_still_lists(self) -> None:
        """Microsoft's item-level samples index properties by display name and
        only their *ImageFile* samples index by id-as-string, so a driver that
        rejects the numeric form is entirely plausible. On one, the id-only
        lookup this backend prefers reads nothing at all."""
        device = flat_device(
            FakeWiaFile("IMG_0001.JPG", PHOTO_A),
            FakeWiaFile("IMG_0002.JPG", PHOTO_B),
        )
        with fake_wia([device]):
            root = connected_device().Items
            for index in (1, 2):
                item = root.Item(index)
                item.Properties = _NameOnlyProperties(item.Properties)

            backend = wia.WiaBackend()
            files = backend.list_files(backend.detect()[0])

        self.assertEqual([cf.name for cf in files], ["IMG_0001.JPG", "IMG_0002.JPG"])
        self.assertEqual(files[0].size, len(PHOTO_A))
        self.assertIs(files[0].raw["can_delete"], True)

    def test_the_numeric_id_is_tried_first_and_the_name_is_never_needed(
        self,
    ) -> None:
        """The control. Numeric ids survive a localised Windows and display
        names may not, so the name must stay a fallback: on a driver that
        honours ids, no lookup by name may happen at all."""
        asked: List[str] = []
        with fake_wia([flat_device(FakeWiaFile("IMG_0001.JPG", PHOTO_A))]):
            item = descend(connected_device(), 1)
            item.Properties = _RecordingProperties(item.Properties, asked)
            backend = wia.WiaBackend()
            files = backend.list_files(backend.detect()[0])

        self.assertEqual(len(files), 1)
        # Every property this driver implements must have been read by its
        # numeric id, with the display name never reached. (The name form *is*
        # legitimately tried for a property the driver does not implement —
        # "Format" here — which is the fallback doing its job, not a regression.)
        for pid in (
            wia.WIA_IPA_ITEM_NAME,
            wia.WIA_IPA_ITEM_FLAGS,
            wia.WIA_IPA_ITEM_SIZE,
            wia.WIA_IPA_ACCESS_RIGHTS,
        ):
            self.assertIn(str(pid), asked, "property %d was never read by id" % pid)
            self.assertNotIn(
                wia._PROP_NAMES[pid],
                asked,
                "fell back to the display name for a property read fine by id",
            )


# --------------------------------------------------------------------------- #
# What the listing tells the user
# --------------------------------------------------------------------------- #


class ListingHonestyTests(unittest.TestCase):
    def test_a_camera_whose_items_cannot_be_described_is_not_called_empty(
        self,
    ) -> None:
        """The most damaging way this backend could be wrong. If the property
        lookups are addressed in a way this driver does not accept, every item
        reads as nothing, the listing is empty, and the user is told a card full
        of twenty-year-old photographs holds none — at which point they may well
        reformat it. Windows handed us items; saying so is the difference
        between a reportable bug and a destroyed archive."""
        progress = RecordingProgress()
        device = flat_device(
            FakeWiaFile("IMG_0001.JPG", PHOTO_A),
            FakeWiaFile("IMG_0002.JPG", PHOTO_B),
        )
        with fake_wia([device]):
            root = connected_device().Items
            for index in (1, 2):
                root.Item(index).Properties = _MuteProperties()

            backend = wia.WiaBackend()
            files = backend.list_files(backend.detect()[0], progress)

        self.assertEqual(files, [])
        messages = " ".join(progress.messages())
        self.assertIn("would not describe", messages)
        self.assertIn("not an empty card", messages)
        self.assertNotIn("The camera reported no photos", messages)

    def test_a_genuinely_empty_camera_still_says_it_is_empty(self) -> None:
        """The control: the diagnostic above must not fire when there is simply
        nothing on the card, or it becomes noise that hides the real thing."""
        progress = RecordingProgress()
        with fake_wia([FakeWiaDevice(items=[])]):
            backend = wia.WiaBackend()
            backend.list_files(backend.detect()[0], progress)

        messages = " ".join(progress.messages())
        self.assertIn("no photos", messages)
        self.assertNotIn("would not describe", messages)

    def test_files_of_unknown_size_are_named_in_the_listing(self) -> None:
        """A size of zero is documented to mean "the driver has no information",
        and Microsoft calls that "common for compressed data" — every JPEG on
        the card. It disables the exact-length check, which is the strongest
        integrity signal this transport has, so the user is told plainly rather
        than quietly handed a weaker guarantee before pressing Delete."""
        progress = RecordingProgress()
        device = flat_device(
            FakeWiaFile("IMG_0001.JPG", PHOTO_A, report_size=-1),
            FakeWiaFile("IMG_0002.JPG", PHOTO_B),
        )
        with fake_wia([device]):
            backend = wia.WiaBackend()
            backend.list_files(backend.detect()[0], progress)

        messages = " ".join(progress.messages())
        self.assertIn("did not report a size for 1 of 2", messages)

    def test_no_size_warning_when_every_file_reported_one(self) -> None:
        """Control: the warning must be absent when it does not apply."""
        progress = RecordingProgress()
        with fake_wia([canon_device()]):
            backend = wia.WiaBackend()
            backend.list_files(backend.detect()[0], progress)

        self.assertNotIn("did not report a size", " ".join(progress.messages()))

    def test_an_item_flagged_removed_is_not_offered_for_rescue(self) -> None:
        """``WiaItemTypeRemoved`` (0x80000000) is a tombstone like
        ``WiaItemTypeDeleted``: the node survives, the data does not. Listing it
        inflates the count and hands the transfer engine an item that yields
        nothing — which then looks like a rescued photo that may be erased.

        The value arrives as a negative VT_I4, so this also pins that the mask
        still works once the sign bit is set.
        """
        with fake_wia([canon_device()]):
            device = connected_device()
            folder = descend(device, 1, 1, 1, 1)  # 0000/Root/DCIM/118CANON
            bend(
                folder.Items.Item(1),
                overrides={
                    wia.WIA_IPA_ITEM_FLAGS: (
                        wia.WIA_ITEM_TYPE_IMAGE | wia.WIA_ITEM_TYPE_REMOVED
                    )
                },
            )
            backend = wia.WiaBackend()
            files = backend.list_files(backend.detect()[0])

        self.assertNotIn("/DCIM/118CANON/IMG_0001.JPG", [cf.path for cf in files])
        self.assertEqual(len(files), 2)

    def test_detection_says_out_loud_that_this_path_is_untested(self) -> None:
        """The Windows backend ships without ever having run on Windows. The
        person deciding whether to erase a memory card is entitled to know that
        on the screen where they decide, not only in the README."""
        progress = RecordingProgress()
        with fake_wia([canon_device()]):
            found = wia.WiaBackend().detect(progress)

        self.assertIn(wia._UNVERIFIED_NOTICE, found[0].detail)
        self.assertIn("never been tested", " ".join(progress.messages()))


# --------------------------------------------------------------------------- #
# Delete: is this still the file we listed?
# --------------------------------------------------------------------------- #


class DeleteIdentityTests(WiaCase):
    """Matching on ``ItemID`` assumes two things Microsoft documents nowhere:
    that an id means the same file after a re-plug, and that a driver never
    reuses one. If either is false, ``Items.Remove`` erases a photograph the
    user never selected and this program never rescued. The item is therefore
    asked who it is in the instant before it is destroyed."""

    def target_and_delete(self, **overrides: Any) -> Any:
        """List, bend the item the delete will land on, then delete it."""
        with fake_wia([canon_device()]) as state:
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            files = self.listed(backend, camera)
            target = files["/DCIM/118CANON/IMG_0001.JPG"]

            folder = descend(connected_device(), 1, 1, 1, 1)
            bend(folder.Items.Item(1), **overrides)

            outcomes = backend.delete(camera, [target])
            survivors = all_item_ids(connected_device().Items)
        return outcomes[0], state, survivors

    def test_an_id_that_now_names_a_different_photo_is_not_erased(self) -> None:
        """The recycled-ItemID case. The id still matches, so the removal would
        go ahead on that evidence alone — and take a photograph the user never
        chose. The name disagreeing with the listing is the only warning there
        is, and it must stop the erase, not be logged after it."""
        outcome, state, survivors = self.target_and_delete(
            overrides={wia.WIA_IPA_ITEM_NAME: "IMG_9999"}
        )

        self.assertFalse(outcome.ok)
        self.assertIn("IMG_0001.JPG", outcome.error)
        self.assertIn("IMG_9999.JPG", outcome.error)
        self.assertEqual(state["deleted"], [], "no removal may even be attempted")
        self.assertIn("\\0000\\Root\\DCIM\\118CANON\\IMG_0001.JPG", survivors)

    def test_an_id_that_now_names_a_file_of_another_size_is_not_erased(self) -> None:
        """Same failure, caught by the other half of the identity. A camera that
        reuses names across folders makes the name alone weak evidence, so the
        byte count is checked too whenever both sides know it."""
        outcome, state, survivors = self.target_and_delete(
            overrides={wia.WIA_IPA_ITEM_SIZE: len(PHOTO_A) + 7}
        )

        self.assertFalse(outcome.ok)
        self.assertIn(str(len(PHOTO_A)), outcome.error)
        self.assertEqual(state["deleted"], [])
        self.assertIn("\\0000\\Root\\DCIM\\118CANON\\IMG_0001.JPG", survivors)

    def test_an_item_that_now_reports_itself_a_folder_is_never_removed(self) -> None:
        """``Items.Remove`` on a folder takes its contents with it, which is one
        step from formatting the card. Nothing in this program ever selects a
        folder, so arriving here means an assumption has already failed — and
        the response to that must be to stop, not to continue carefully."""
        outcome, state, survivors = self.target_and_delete(
            overrides={wia.WIA_IPA_ITEM_FLAGS: wia.WIA_ITEM_TYPE_FOLDER}
        )

        self.assertFalse(outcome.ok)
        self.assertIn("folder", outcome.error)
        self.assertEqual(state["deleted"], [])
        self.assertIn("\\0000\\Root\\DCIM\\118CANON\\IMG_0001.JPG", survivors)

    def test_a_driver_that_describes_nothing_still_erases_on_the_id_alone(
        self,
    ) -> None:
        """The control, and a deliberate limit. A driver that answers no
        property question cannot contradict the listing, and refusing every
        deletion on such a device would be its own kind of wrong — the user
        would erase from the camera's menu instead, with no verification at all.
        Unreadable means *unknown*, never *different*; what it costs is that on
        such a driver the ItemID match is the only guard left."""
        outcome, state, _ = self.target_and_delete(
            hidden=[
                wia.WIA_IPA_ITEM_NAME,
                wia.WIA_IPA_ITEM_SIZE,
                wia.WIA_IPA_ITEM_FLAGS,
                wia.WIA_IPA_FILENAME_EXTENSION,
            ]
        )

        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(len(state["deleted"]), 1)

    def test_an_item_whose_properties_raise_is_still_erased_on_the_id_alone(
        self,
    ) -> None:
        """The same limit, one step harsher: reading ``Properties`` at all
        throws. Erasing must still proceed, for the same reason — a driver that
        cannot answer has not contradicted anything, and a program that refused
        every deletion on it would push the user to the camera's own menu, where
        nothing is verified at all. This is the branch that decides which way an
        unanswerable question falls, so it is pinned deliberately rather than
        left to whichever exception happens to be caught."""

        class _AngryItem:
            """An item that resolves by id and then refuses to describe itself."""

            def __init__(self, inner: Any) -> None:
                self._inner = inner
                self.ItemID = inner.ItemID
                self.Items = inner.Items

            @property
            def Properties(self) -> Any:
                raise _ComError(text="the device is busy")

        with fake_wia([canon_device()]):
            backend = wia.WiaBackend()
            camera = backend.detect()[0]
            target = self.listed(backend, camera)["/DCIM/118CANON/IMG_0001.JPG"]

            folder = descend(connected_device(), 1, 1, 1, 1)
            entries = folder.Items._raw()
            entries[0] = _AngryItem(entries[0])

            outcomes = backend.delete(camera, [target])
            # Read the tree rather than the fake's `deleted` log: the wrapper
            # above is not one of the fake's own items, so it never reaches that
            # log. What was asked was whether the file left the camera.
            survivors = all_item_ids(connected_device().Items)

        self.assertTrue(outcomes[0].ok, outcomes[0].error)
        self.assertNotIn("\\0000\\Root\\DCIM\\118CANON\\IMG_0001.JPG", survivors)
        self.assertIn("\\0000\\Root\\DCIM\\118CANON\\IMG_0002.JPG", survivors)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


class PathAndFormatTests(unittest.TestCase):
    """The small pure functions. They are cheap to test and each one has a way
    of failing that only shows up as a wrong file name or an unreadable error."""

    def test_wia_bookkeeping_is_stripped_from_the_folder(self) -> None:
        """``CameraFile.path`` is a file's identity across download and delete,
        and it should read like the path on the card, not WIA's internals."""
        cases = {
            "0000\\Root\\DCIM\\118CANON\\IMG_1870": "/DCIM/118CANON",
            "0000\\Root\\IMG_1870": "/",
            "0000\\Root\\DCIM\\IMG_1870": "/DCIM",
            "\\IMG_1870": "/",
        }
        for full_name, expected in cases.items():
            self.assertEqual(wia._folder_from_full_name(full_name), expected, full_name)

    def test_a_real_top_level_folder_of_digits_is_not_eaten(self) -> None:
        """The device index is stripped only when the *whole* first segment is
        digits and is followed by Root — a card really can hold a folder called
        100MEDIA, and losing it would change the file's identity."""
        self.assertEqual(
            wia._folder_from_full_name("0000\\Root\\100MEDIA\\IMG_0001.JPG"),
            "/100MEDIA",
        )

    def test_binary_data_is_accepted_in_every_shape_pywin32_may_use(self) -> None:
        payload = b"\xff\xd8\x00\x42\xff\xd9"
        self.assertEqual(wia._coerce_bytes(payload), payload)
        self.assertEqual(wia._coerce_bytes(bytearray(payload)), payload)
        self.assertEqual(wia._coerce_bytes(memoryview(payload)), payload)
        self.assertEqual(wia._coerce_bytes(tuple(payload)), payload)
        self.assertEqual(wia._coerce_bytes(list(payload)), payload)

    def test_unusable_binary_data_raises_a_reportable_error(self) -> None:
        """Better a named failure the user can report than a file full of the
        repr of a COM object."""
        with self.assertRaises(CameraError) as caught:
            wia._coerce_bytes(object())
        self.assertIn("report this", str(caught.exception))

    def test_a_number_is_refused_rather_than_expanded_into_zero_bytes(self) -> None:
        """``bytearray(7)`` is seven zero bytes, not the number seven. A
        permissive coercion would turn a driver that answered with a plain
        integer into a fabricated file of zeroes carrying a photograph's name —
        the precise outcome this program exists to prevent, and one that a
        camera reporting no size would not catch downstream either.
        """
        for value in (7, True, 0.5, "not bytes", None):
            with self.assertRaises(CameraError, msg=repr(value)):
                wia._coerce_bytes(value)

    def test_a_format_id_is_compared_ignoring_braces_and_case(self) -> None:
        """Nothing guarantees a driver echoes a GUID back the way it was given,
        so the conversion guard normalises before it accuses."""
        self.assertEqual(
            wia._guid_key("{B96B3CAE-0728-11D3-9D7B-0000F81EF32E}"),
            wia._guid_key("b96b3cae-0728-11d3-9d7b-0000f81ef32e"),
        )

    def test_an_absent_format_id_reads_as_unknown_not_as_different(self) -> None:
        """The direction this must fail in. An unset FormatID that compared as
        "different" would refuse every healthy transfer on a driver that simply
        does not implement the property — losing photographs in the name of
        protecting them. The all-zero GUID is wiaFormatUndefined and means the
        same thing."""
        for absent in (None, "", "   ", "{00000000-0000-0000-0000-000000000000}"):
            self.assertEqual(wia._guid_key(absent), "", repr(absent))

    def test_a_timestamp_of_the_wrong_length_is_refused_not_decoded(self) -> None:
        """Microsoft's own sample gates on ``If v.Count = 8`` before reading the
        vector, and so does this. A longer sequence decoded by taking the first
        few values produces a confident, silently wrong date; a missing
        timestamp is honest, an invented one is not.
        """
        eight = (2003, 8, 3, 13, 19, 22, 0, 0)
        self.assertIsNotNone(wia._item_mtime(_FakeProperties({4100: eight})))
        wrong_shapes = (
            eight + (0, 0),  # a longer vector
            eight[:6],  # a shorter one
            "2003081319220000",  # the 16-digit PTP date string
            "20030813",  # ...and an 8-character one, which is worse
            # Eight raw bytes. This is the shape that made the guard necessary:
            # bytes iterate as integers, so without it these eight decode
            # cleanly to the year 100 and return a confident timestamp of
            # -58997168396.0 rather than admitting the date is unknown.
            b"\x64\x06\x00\x0f\x0a\x1e\x00\x00",
        )
        for wrong in wrong_shapes:
            self.assertIsNone(
                wia._item_mtime(_FakeProperties({4100: wrong})), repr(wrong)
            )

    def test_the_drivers_own_description_is_preferred_in_error_text(self) -> None:
        """``com_error`` args are (hresult, strerror, excepinfo, argerr) and the
        driver's own words live at excepinfo[2]. They beat "Unspecified error"
        every time, and this is the text the user is asked to report."""
        exc = Exception(
            -2147024891,
            "Unspecified error",
            (0, "WIA", "The camera is switched off", None, 0, 0),
            0,
        )
        message = wia._friendly(exc)
        self.assertIn("The camera is switched off", message)
        self.assertIn("0x80070005", message)
        self.assertNotIn("Unspecified error", message)

    def test_a_com_error_without_a_description_still_names_the_code(self) -> None:
        self.assertEqual(
            wia._friendly(Exception(-2147024891)), "Windows error 0x80070005"
        )

    def test_a_plain_exception_survives_the_friendly_conversion(self) -> None:
        """A WIA driver can fail with a TypeError out of the marshalling layer
        just as easily as with a COM error."""
        self.assertEqual(wia._friendly(TypeError("bad variant")), "bad variant")
        self.assertEqual(wia._friendly(RuntimeError()), "RuntimeError")


if __name__ == "__main__":  # pragma: no cover - convenience only
    unittest.main()
