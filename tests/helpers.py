"""Shared fixtures for the test suite. No third-party dependencies.

Everything here exists so the tests can exercise the *real* modules rather than
stubs. Three transports need faking to run on a machine with no camera attached:

* **mass storage** — needs nothing faked; :func:`make_card` builds a real DCIM
  tree on disk and the backend walks it exactly as it would walk a card.
* **gphoto2** — :class:`FakeGphoto2` puts a real executable named ``gphoto2``
  first on ``PATH``, so the backend's argv construction, output parsing and exit
  handling are all genuinely exercised.
* **WIA** — :func:`fake_wia` replaces the single COM seam
  (``wia._import_com``) with an in-memory object graph shaped like the real
  automation API. Combined with a patched ``sys.platform`` this runs the whole
  Windows backend on macOS.

The fakes are deliberately *unhelpful*: collections are 1-based like the real
COM ones, properties raise instead of returning ``False`` from ``Exists`` when
asked to, and the gphoto2 stub reproduces the real tool's exact output format.
A fake that is nicer than reality tests nothing.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
import struct
import sys
import tempfile
import textwrap
import types
import unittest
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

# Make ``src/`` importable no matter where the runner is invoked from.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from retrocam.model import (  # noqa: E402
    BackendKind,
    CameraInfo,
    Progress,
)

__all__ = [
    "tiny_jpeg",
    "truncated_jpeg",
    "not_a_jpeg",
    "riff_avi",
    "sha",
    "make_card",
    "mass_storage_camera",
    "RecordingProgress",
    "FakeGphoto2",
    "fake_wia",
    "FakeWiaFile",
    "FakeWiaFolder",
    "FakeWiaDevice",
    "TempDirCase",
]


# --------------------------------------------------------------------------- #
# Byte-level fixtures
# --------------------------------------------------------------------------- #


def tiny_jpeg(payload: int = 256, marker_byte: int = 0x42) -> bytes:
    """A structurally valid JPEG of a predictable size.

    Real enough for the structural checks in ``verify.py``: SOI, an APP0/JFIF
    segment, a comment segment carrying the filler, then EOI. It is *not* a
    decodable image — tests that need Pillow to succeed must build a real one,
    and there is a helper for that in the verify tests.
    """
    soi = b"\xff\xd8"
    jfif = (
        b"\xff\xe0"
        + struct.pack(">H", 16)
        + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    )
    # COM segment: length covers the 2 length bytes plus the payload.
    body = bytes([marker_byte]) * max(0, payload)
    com = b"\xff\xfe" + struct.pack(">H", len(body) + 2) + body
    return soi + jfif + com + b"\xff\xd9"


def truncated_jpeg(payload: int = 256) -> bytes:
    """A JPEG whose EOI marker is missing — the classic interrupted transfer."""
    return tiny_jpeg(payload)[:-2]


def not_a_jpeg(size: int = 512) -> bytes:
    """Bytes that are emphatically not a JPEG, but deterministic across runs."""
    return bytes((i * 37 + 11) % 251 for i in range(size))


def riff_avi(payload: int = 128) -> bytes:
    """A minimal RIFF/AVI container, for the non-JPEG verification path."""
    body = b"\x00" * payload
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"AVI " + body


def sha(path: str) -> str:
    """Short content digest, for asserting a copy is byte-identical."""
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# A card on disk
# --------------------------------------------------------------------------- #


#: A card layout: ``{"118CANON": {"IMG_0001.JPG": b"...", ...}, ...}``.
CardSpec = Dict[str, Dict[str, bytes]]


DEFAULT_CARD: CardSpec = {
    # Two folders holding the SAME base name is the case that broke three
    # backends during review: a Canon rolls its counter over and reuses names.
    "118CANON": {
        "IMG_0001.JPG": tiny_jpeg(1000, 0x11),
        "IMG_0002.JPG": tiny_jpeg(2000, 0x22),
    },
    "119CANON": {
        "IMG_0001.JPG": tiny_jpeg(3000, 0x33),
    },
}


def make_card(root: str, spec: Optional[CardSpec] = None) -> Dict[str, bytes]:
    """Build a DCIM tree under ``root`` and return ``{device_path: bytes}``.

    The returned mapping is keyed by the POSIX device path the backend will
    report (``/DCIM/118CANON/IMG_0001.JPG``), so a test can assert that the
    right *photograph* reached the right destination — which is precisely the
    property that duplicate file names put at risk.
    """
    spec = DEFAULT_CARD if spec is None else spec
    contents: Dict[str, bytes] = {}
    for folder, files in spec.items():
        target = os.path.join(root, "DCIM", folder)
        os.makedirs(target, exist_ok=True)
        for name, data in files.items():
            with open(os.path.join(target, name), "wb") as fh:
                fh.write(data)
            contents["/DCIM/%s/%s" % (folder, name)] = data
    return contents


def mass_storage_camera(card_root: str, model: str = "Test Card") -> CameraInfo:
    """A :class:`CameraInfo` pointing at a card built by :func:`make_card`.

    Mirrors exactly what ``MassStorageBackend.detect`` produces, including the
    ``mount``/``dcim`` keys that ``transfer._reject_dest_on_device`` reads to
    refuse a destination that sits on the card itself.
    """
    return CameraInfo(
        model=model,
        port=card_root,
        kind=BackendKind.MASS_STORAGE,
        detail="fake card",
        raw={"mount": card_root, "dcim": os.path.join(card_root, "DCIM")},
    )


def make_read_only(path: str) -> None:
    """Drop write permission on a directory, skipping the test when running as root."""
    if os.geteuid() == 0:  # pragma: no cover - CI safety valve
        raise unittest.SkipTest("cannot test read-only paths as root")
    os.chmod(path, stat.S_IRUSR | stat.S_IXUSR)


# --------------------------------------------------------------------------- #
# Progress capture
# --------------------------------------------------------------------------- #


class RecordingProgress:
    """A progress callback that remembers everything, for assertions.

    Also usable as a tripwire: pass ``fail_on_thread`` to assert that no tick
    arrives from a thread other than the one that built it.
    """

    def __init__(self, fail_on_thread: bool = False) -> None:
        self.ticks: List[Progress] = []
        self._fail_on_thread = fail_on_thread
        self._home = __import__("threading").current_thread().ident

    def __call__(self, progress: Progress) -> None:
        if self._fail_on_thread:
            here = __import__("threading").current_thread().ident
            assert here == self._home, "progress arrived from another thread"
        self.ticks.append(progress)

    # -- convenience views ------------------------------------------------- #

    def phases(self) -> List[str]:
        return [t.phase for t in self.ticks]

    def messages(self) -> List[str]:
        return [t.message for t in self.ticks if t.message]

    def of_phase(self, phase: str) -> List[Progress]:
        return [t for t in self.ticks if t.phase == phase]

    def saw_phase(self, phase: str) -> bool:
        return any(t.phase == phase for t in self.ticks)


# --------------------------------------------------------------------------- #
# A fake gphoto2 binary
# --------------------------------------------------------------------------- #


class FakeGphoto2:
    """Puts a scripted ``gphoto2`` executable first on ``PATH``.

    The stub is a real Python script, so the backend genuinely spawns a process,
    passes its real argv and parses real stdout. ``calls()`` returns every argv
    the backend used, which is how the tests assert that ``--port`` is always
    pinned and that deletion never uses a bulk flag.

    Use as a context manager::

        with FakeGphoto2(card=spec) as gp:
            ...
            self.assertIn("--port", gp.calls()[0])
    """

    def __init__(
        self,
        card: Optional[CardSpec] = None,
        model: str = "Canon PowerShot S30",
        port: str = "usb:000,005",
        detect_empty: bool = False,
        fail_with: Optional[str] = None,
        exit_code: int = 0,
        version: str = "gphoto2 2.5.32",
    ) -> None:
        self.card = DEFAULT_CARD if card is None else card
        self.model = model
        self.port = port
        self.detect_empty = detect_empty
        self.fail_with = fail_with
        self.exit_code = exit_code
        self.version = version
        self._tmp: Optional[str] = None
        self._old_path: Optional[str] = None

    # -- lifecycle --------------------------------------------------------- #

    def __enter__(self) -> "FakeGphoto2":
        self._tmp = tempfile.mkdtemp(prefix="fakegp-")
        self.log_path = os.path.join(self._tmp, "calls.log")
        self.store = os.path.join(self._tmp, "store")
        os.makedirs(self.store, exist_ok=True)
        # Materialise the card so --get-file can serve real bytes.
        for folder, files in self.card.items():
            d = os.path.join(self.store, "DCIM", folder)
            os.makedirs(d, exist_ok=True)
            for name, data in files.items():
                with open(os.path.join(d, name), "wb") as fh:
                    fh.write(data)

        script = os.path.join(self._tmp, "gphoto2")
        with open(script, "w") as fh:
            fh.write(self._script_source())
        os.chmod(script, 0o755)

        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = self._tmp + os.pathsep + self._old_path
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._old_path is not None:
            os.environ["PATH"] = self._old_path
        if self._tmp:
            import shutil

            shutil.rmtree(self._tmp, ignore_errors=True)

    # -- inspection -------------------------------------------------------- #

    def calls(self) -> List[List[str]]:
        """Every argv the backend passed, oldest first."""
        if not os.path.exists(self.log_path):
            return []
        out: List[List[str]] = []
        with open(self.log_path) as fh:
            for line in fh:
                line = line.rstrip("\n")
                if line:
                    out.append(line.split("\x1f"))
        return out

    def flat_calls(self) -> List[str]:
        return [" ".join(c) for c in self.calls()]

    # -- the stub ---------------------------------------------------------- #

    def _script_source(self) -> str:
        return textwrap.dedent(
            '''\
            #!/usr/bin/env python3
            """Scripted stand-in for the gphoto2 CLI (test fixture)."""
            import os, sys

            LOG = %(log)r
            STORE = %(store)r
            MODEL = %(model)r
            PORT = %(port)r
            DETECT_EMPTY = %(detect_empty)r
            FAIL_WITH = %(fail_with)r
            EXIT_CODE = %(exit_code)r
            VERSION = %(version)r

            argv = sys.argv[1:]
            with open(LOG, "a") as fh:
                fh.write("\\x1f".join(argv) + "\\n")

            def opt(name, default=None):
                if name in argv:
                    i = argv.index(name)
                    if i + 1 < len(argv):
                        return argv[i + 1]
                return default

            if "--version" in argv:
                print(VERSION)
                sys.exit(0)

            if FAIL_WITH:
                sys.stderr.write(FAIL_WITH + "\\n")
                sys.exit(EXIT_CODE or 1)

            if "--auto-detect" in argv:
                print("Model                          Port")
                print("-" * 58)
                if not DETECT_EMPTY:
                    print("%%-30s %%s" %% (MODEL, PORT))
                sys.exit(0)

            if "--summary" in argv:
                print("Detected a '%%s'." %% MODEL)
                print("Camera summary:")
                print("Model: %%s" %% MODEL)
                sys.exit(0)

            def walk():
                """Yield (folder, name, size) for the whole store, sorted."""
                dcim = os.path.join(STORE, "DCIM")
                for root, dirs, files in os.walk(dcim):
                    dirs.sort()
                    rel = "/DCIM" + root[len(dcim):].replace(os.sep, "/")
                    for n in sorted(files):
                        yield rel, n, os.path.getsize(os.path.join(root, n))

            # Machine-readable listing: the backend prefers this for exact sizes.
            if "--parsable" in argv or "--show-info" in argv:
                folder_filter = opt("--folder")
                for folder, name, size in walk():
                    if folder_filter and folder != folder_filter:
                        continue
                    print("FILENAME=%%s" %% name)
                    print("FOLDER=%%s" %% folder)
                    print("FILESIZE=%%d" %% size)
                    print("FILEMTIME=%%d" %% 1700000000)
                sys.exit(0)

            if "-L" in argv or "--list-files" in argv:
                by_folder = {}
                for folder, name, size in walk():
                    by_folder.setdefault(folder, []).append((name, size))
                counter = 0
                print("Detected a '%%s'." %% MODEL)
                for folder in sorted(by_folder):
                    entries = by_folder[folder]
                    print("There are %%d files in folder '%%s'." %% (len(entries), folder))
                    for name, size in entries:
                        counter += 1
                        kb = (size + 512) // 1024
                        print("#%%-5d %%-26s rd %%5d KB image/jpeg 1700000000"
                              %% (counter, name, kb))
                sys.exit(0)

            if "--get-file" in argv:
                folder = opt("--folder", "/DCIM")
                name = opt("--get-file")
                target = opt("--filename")
                src = os.path.join(STORE, folder.lstrip("/").replace("/", os.sep), name)
                if not os.path.exists(src):
                    sys.stderr.write("*** Error: File not found ***\\n")
                    sys.exit(1)
                data = open(src, "rb").read()
                if target:
                    with open(target, "wb") as fh:
                        fh.write(data)
                    print("Saving file as %%s" %% target)
                else:
                    with open(name, "wb") as fh:
                        fh.write(data)
                    print("Saving file as %%s" %% name)
                sys.exit(0)

            if "--delete-file" in argv:
                folder = opt("--folder", "/DCIM")
                name = opt("--delete-file")
                src = os.path.join(STORE, folder.lstrip("/").replace("/", os.sep), name)
                if not os.path.exists(src):
                    sys.stderr.write("*** Error: File not found ***\\n")
                    sys.exit(1)
                os.remove(src)
                sys.exit(0)

            sys.exit(0)
            '''
            % {
                "log": self.log_path,
                "store": self.store,
                "model": self.model,
                "port": self.port,
                "detect_empty": self.detect_empty,
                "fail_with": self.fail_with,
                "exit_code": self.exit_code,
                "version": self.version,
            }
        )


# --------------------------------------------------------------------------- #
# A fake WIA / COM layer
# --------------------------------------------------------------------------- #


class _FakeProperty:
    def __init__(self, value: Any) -> None:
        self.Value = value


class _FakeProperties:
    """1-based property collection keyed by the property id rendered as a string.

    ``raise_on_exists`` reproduces drivers that raise from ``Exists`` instead of
    answering it — ``_prop`` has a fallback for exactly that, and it needs
    covering.
    """

    def __init__(self, values: Dict[int, Any], raise_on_exists: bool = False) -> None:
        self._values = {str(int(k)): v for k, v in values.items()}
        self._raise_on_exists = raise_on_exists

    def Exists(self, key: str) -> bool:
        if self._raise_on_exists:
            raise RuntimeError("driver does not implement Exists")
        return str(key) in self._values

    def Item(self, key: str) -> _FakeProperty:
        return _FakeProperty(self._values[str(key)])


class _FakeCollection:
    """1-based COM-style collection, like ``Items`` and ``DeviceInfos``."""

    def __init__(self, entries: Sequence[Any]) -> None:
        self._entries = list(entries)

    @property
    def Count(self) -> int:
        return len(self._entries)

    def Item(self, index: Any) -> Any:
        if isinstance(index, int):
            return self._entries[index - 1]  # 1-based, like the real thing
        for entry in self._entries:
            if getattr(entry, "DeviceID", None) == index:
                return entry
        raise KeyError(index)

    def __call__(self, index: Any) -> Any:
        # DeviceInfos(id) is callable in the automation API.
        return self.Item(index)

    def Remove(self, index: int) -> None:
        del self._entries[index - 1]

    def _raw(self) -> List[Any]:
        return self._entries


class FakeWiaFile:
    """One transferable image item."""

    def __init__(
        self,
        name: str,
        data: bytes,
        mtime: str = "2003081319220000",
        can_delete: bool = True,
        report_size: Optional[int] = None,
        transfer_raises: bool = False,
        returns_tuple: bool = False,
    ) -> None:
        self.name = name
        self.data = data
        self.mtime = mtime
        self.can_delete = can_delete
        #: ``None`` means "report the true size"; ``-1`` means "report nothing".
        self.report_size = report_size
        self.transfer_raises = transfer_raises
        self.returns_tuple = returns_tuple
        self.transfers = 0
        self.parent: Optional["FakeWiaFolder"] = None


class FakeWiaFolder:
    def __init__(self, name: str, children: Sequence[Any]) -> None:
        self.name = name
        self.children = list(children)


class FakeWiaDevice:
    def __init__(
        self,
        device_id: str = "{6BDD1FC6-810F-11D0-BEC7-08002BE2092F}\\\\0001",
        name: str = "Canon PowerShot S30",
        vendor: str = "Canon",
        port: str = "\\\\.\\Usbscan0",
        device_type: int = 2,
        items: Optional[Sequence[Any]] = None,
    ) -> None:
        self.device_id = device_id
        self.name = name
        self.vendor = vendor
        self.port = port
        self.device_type = device_type
        self.items = list(items or [])


@contextlib.contextmanager
def fake_wia(
    devices: Sequence[FakeWiaDevice],
    platform: str = "win32",
    dispatch_raises: Optional[BaseException] = None,
) -> Iterator[Dict[str, Any]]:
    """Run the real ``WiaBackend`` against an in-memory COM graph.

    Patches the module's single COM seam plus ``sys.platform``, so
    ``is_available``, ``detect``, ``list_files``, ``download`` and ``delete``
    all execute their genuine code paths. Yields a dict with ``devices`` and a
    ``deleted`` list recording every item the backend removed, so a test can
    assert that deletion touched exactly the intended files and nothing else.
    """
    from retrocam.backends import wia as wia_mod

    state: Dict[str, Any] = {"devices": list(devices), "deleted": [], "transfers": []}

    # -- build the object graph ------------------------------------------- #

    def build_item(node: Any, parent_path: str) -> Any:
        if isinstance(node, FakeWiaFolder):
            full = parent_path + "\\" + node.name
            children = [build_item(c, full) for c in node.children]
            props = _FakeProperties(
                {
                    wia_mod.WIA_IPA_ITEM_NAME: node.name,
                    wia_mod.WIA_IPA_FULL_ITEM_NAME: full,
                    wia_mod.WIA_IPA_ITEM_FLAGS: wia_mod.WIA_ITEM_TYPE_FOLDER,
                }
            )
            item = _FakeItem(full, props, _FakeCollection(children), None, state)
            return item

        assert isinstance(node, FakeWiaFile)
        full = parent_path + "\\" + node.name
        values: Dict[int, Any] = {
            wia_mod.WIA_IPA_ITEM_NAME: node.name,
            wia_mod.WIA_IPA_FULL_ITEM_NAME: full,
            wia_mod.WIA_IPA_ITEM_FLAGS: (
                wia_mod.WIA_ITEM_TYPE_IMAGE | wia_mod.WIA_ITEM_TYPE_FILE
            ),
            wia_mod.WIA_IPA_ACCESS_RIGHTS: (
                wia_mod.WIA_ITEM_READ
                | (wia_mod.WIA_ITEM_CAN_BE_DELETED if node.can_delete else 0)
            ),
            wia_mod.WIA_IPA_ITEM_TIME: node.mtime,
            wia_mod.WIA_IPA_FILENAME_EXTENSION: node.name.rsplit(".", 1)[-1],
        }
        size = len(node.data) if node.report_size is None else node.report_size
        if size >= 0:
            values[wia_mod.WIA_IPA_ITEM_SIZE] = size
        return _FakeItem(
            full, _FakeProperties(values), _FakeCollection([]), node, state
        )

    device_objs = []
    for dev in devices:
        root_items = _FakeCollection([build_item(i, "") for i in dev.items])
        device_objs.append(_FakeDevice(dev, root_items, state))

    infos = _FakeCollection([_FakeDeviceInfo(d, state) for d in device_objs])
    manager = _FakeManager(infos)

    class _FakePythoncom:
        com_error = _ComError

        @staticmethod
        def CoInitialize() -> None:
            state.setdefault("co_init", 0)
            state["co_init"] += 1

        @staticmethod
        def CoUninitialize() -> None:
            state.setdefault("co_uninit", 0)
            state["co_uninit"] += 1

    class _FakeWin32ComClient:
        @staticmethod
        def Dispatch(progid: str) -> Any:
            state.setdefault("dispatched", []).append(progid)
            if dispatch_raises is not None:
                raise dispatch_raises
            return manager

    # ``_import_com`` is the seam every *operation* goes through, but
    # ``is_available()`` deliberately does a bare ``import win32com.client``
    # instead — it must answer without creating any COM object. Patching the
    # seam alone therefore leaves every public method failing its own
    # availability gate on a machine with no pywin32, so the module stubs below
    # are part of the fake, not a convenience.
    stub_pythoncom = types.ModuleType("pythoncom")
    stub_pythoncom.com_error = _ComError  # type: ignore[attr-defined]
    stub_pythoncom.CoInitialize = _FakePythoncom.CoInitialize  # type: ignore[attr-defined]
    stub_pythoncom.CoUninitialize = _FakePythoncom.CoUninitialize  # type: ignore[attr-defined]
    stub_client = types.ModuleType("win32com.client")
    stub_client.Dispatch = _FakeWin32ComClient.Dispatch  # type: ignore[attr-defined]
    stub_win32com = types.ModuleType("win32com")
    stub_win32com.client = stub_client  # type: ignore[attr-defined]
    stubs = {
        "pythoncom": stub_pythoncom,
        "win32com": stub_win32com,
        "win32com.client": stub_client,
    }
    sentinel = object()
    saved = {name: sys.modules.get(name, sentinel) for name in stubs}

    old_import = wia_mod._import_com
    old_platform = sys.platform
    wia_mod._import_com = lambda: (_FakePythoncom, _FakeWin32ComClient)  # type: ignore[assignment]
    sys.modules.update(stubs)
    if platform:
        sys.platform = platform  # type: ignore[assignment]
    try:
        yield state
    finally:
        wia_mod._import_com = old_import  # type: ignore[assignment]
        sys.platform = old_platform  # type: ignore[assignment]
        for name, previous in saved.items():
            if previous is sentinel:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


class _ComError(Exception):
    """Stand-in for ``pythoncom.com_error``; carries an HRESULT-shaped tuple."""

    def __init__(
        self, hresult: int = -2147024891, text: str = "Access is denied"
    ) -> None:
        super().__init__(hresult, text, None, None)
        self.hresult = hresult


class _FakeImageFile:
    def __init__(self, data: bytes, as_tuple: bool = False) -> None:
        self.FileData = _FakeBinary(data, as_tuple)


class _FakeBinary:
    def __init__(self, data: bytes, as_tuple: bool) -> None:
        #: Exercises ``_coerce_bytes``: pywin32 may hand back a tuple of ints.
        self.BinaryData = tuple(data) if as_tuple else data


class _FakeItem:
    def __init__(
        self,
        item_id: str,
        props: _FakeProperties,
        items: _FakeCollection,
        node: Optional[FakeWiaFile],
        state: Dict[str, Any],
    ) -> None:
        self.ItemID = item_id
        self.Properties = props
        self.Items = items
        self._node = node
        self._state = state

    def Transfer(self, fmt: Any = None) -> Any:
        if self._node is None:
            raise RuntimeError("folders cannot be transferred")
        if self._node.transfer_raises:
            raise _ComError(text="transfer failed")
        self._node.transfers += 1
        self._state["transfers"].append(self.ItemID)
        return _FakeImageFile(self._node.data, self._node.returns_tuple)


class _FakeDevice:
    def __init__(
        self, spec: FakeWiaDevice, items: _FakeCollection, state: Dict[str, Any]
    ) -> None:
        self.DeviceID = spec.device_id
        self.Items = items
        self._spec = spec
        self._state = state

    @property
    def Properties(self) -> _FakeProperties:
        from retrocam.backends import wia as wia_mod

        return _FakeProperties(
            {
                wia_mod.WIA_DIP_DEV_ID: self._spec.device_id,
                wia_mod.WIA_DIP_DEV_NAME: self._spec.name,
                wia_mod.WIA_DIP_DEV_DESC: self._spec.name,
                wia_mod.WIA_DIP_VEND_DESC: self._spec.vendor,
                wia_mod.WIA_DIP_PORT_NAME: self._spec.port,
                wia_mod.WIA_DIP_DEV_TYPE: self._spec.device_type,
            }
        )


class _FakeDeviceInfo:
    def __init__(self, device: _FakeDevice, state: Dict[str, Any]) -> None:
        self._device = device
        self.DeviceID = device.DeviceID
        self._state = state

    @property
    def Properties(self) -> _FakeProperties:
        return self._device.Properties

    def Connect(self) -> _FakeDevice:
        self._state.setdefault("connects", []).append(self.DeviceID)
        return self._device


class _FakeManager:
    def __init__(self, infos: _FakeCollection) -> None:
        self.DeviceInfos = infos


# Deletion has to mutate the tree, so ``Items.Remove`` records what it removed.
_orig_remove = _FakeCollection.Remove


def _recording_remove(self: _FakeCollection, index: int) -> None:
    entry = self._entries[index - 1]
    node = getattr(entry, "_node", None)
    state = getattr(entry, "_state", None)
    if state is not None:
        state["deleted"].append(getattr(entry, "ItemID", "?"))
    if node is not None and not node.can_delete:
        raise _ComError(text="item is read-only")
    _orig_remove(self, index)


_FakeCollection.Remove = _recording_remove  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Base test case
# --------------------------------------------------------------------------- #


class TempDirCase(unittest.TestCase):
    """Gives each test a private temp directory, cleaned up afterwards."""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="retrocam-test-")
        self.tmp = self._tmp.name
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        # A test may have dropped write permission to check a read-only path;
        # restore it or the cleanup fails on the directory itself.
        for root, dirs, _ in os.walk(self.tmp):
            for d in dirs:
                with contextlib.suppress(OSError):
                    os.chmod(os.path.join(root, d), 0o755)
        self._tmp.cleanup()

    def path(self, *parts: str) -> str:
        return os.path.join(self.tmp, *parts)

    def make_card(
        self, spec: Optional[CardSpec] = None, name: str = "card"
    ) -> Tuple[str, Dict[str, bytes]]:
        root = self.path(name)
        os.makedirs(root, exist_ok=True)
        return root, make_card(root, spec)
