"""Windows Image Acquisition (WIA) backend — the native Windows camera path.

What this backend can and cannot do (read this before filing a bug)
-------------------------------------------------------------------
WIA is the only way to reach a camera *body* on Windows without installing a
driver. Since Windows Vista, Microsoft's own documentation says WIA no longer
supports cameras directly; what actually happens on Windows 10/11 is that the
in-box **WPD/MTP driver** exposes PTP cameras and phones back to WIA
applications through a compatibility shim. The practical consequence:

* **PTP / MTP cameras (roughly 2002 onwards) work.** They show up in
  ``WIA.DeviceManager.DeviceInfos`` with device type ``Camera`` and can be
  listed, transferred and — sometimes — erased.
* **Pre-PTP cameras do not work, and cannot be made to work here.** The
  reference device for this project, the Canon PowerShot S30 (USB 04A9:3057),
  speaks Canon's proprietary protocol. Windows has no driver that binds it, so
  it never reaches WIA at all. For that camera the answer on Windows is the
  memory-card reader (the mass-storage backend) or the usbipd-win + WSL2 +
  gphoto2 bridge. :meth:`detect` says so in plain language when it finds
  nothing.

This backend is still worth shipping: it covers the long tail of *slightly*
less old cameras a user is likely to plug in next, with zero dependencies
beyond ``pywin32``.

Cross-platform import safety
----------------------------
``registry.py`` imports every backend module on every operating system, so this
module must import cleanly on macOS and Linux. **Every** ``win32com`` /
``pythoncom`` import therefore lives inside a function, behind ``try/except
ImportError``. Nothing at module scope touches Windows.

Threading
---------
COM is apartment-threaded. Each thread that touches WIA must call
``CoInitialize`` once on entry and ``CoUninitialize`` on exit, and a COM object
obtained on one thread must never be used on another. This backend therefore
initialises the apartment inside every public method and never stores a live
COM object on ``self`` or in a dataclass: ``CameraFile.raw`` carries only the
item's *string* ``ItemID``, which is re-resolved against a freshly connected
device on each call. That is also what the base-class contract demands, since
the camera may be unplugged between two GUI actions.
"""

from __future__ import annotations

import contextlib
import os
import sys
import uuid
from datetime import datetime
from typing import Any, Iterator, List, Optional, Sequence, Tuple

from ..model import (
    BackendKind,
    BackendUnavailable,
    CameraError,
    CameraFile,
    CameraInfo,
    CancelToken,
    DeleteOutcome,
    DownloadOutcome,
    Progress,
    ProgressCallback,
)
from .base import Availability, CameraBackend, noop_progress

__all__ = ["WiaBackend"]


# --------------------------------------------------------------------------- #
# WIA constants
#
# Numeric ids computed from mingw-w64's ``wiadef.h``:
#   WIA_RESERVED_FOR_NEW_PROPS = 1024, WIA_DIP_FIRST = 2
#   -> WIA_DPA_FIRST = 1026 -> WIA_DPC_FIRST = 2050 -> WIA_DPS_FIRST = 3074
#   -> WIA_IPA_FIRST = 4098 -> WIA_IPC_FIRST = 5122
# We address properties by numeric id rather than by their English display name
# because the name strings are localised on non-English Windows installs.
# --------------------------------------------------------------------------- #

# Device-info properties (WIA_DIP_*).
WIA_DIP_DEV_ID = 2
WIA_DIP_VEND_DESC = 3
WIA_DIP_DEV_DESC = 4
WIA_DIP_DEV_TYPE = 5
WIA_DIP_PORT_NAME = 6
WIA_DIP_DEV_NAME = 7

# Item properties (WIA_IPA_*).
WIA_IPA_ITEM_NAME = 4098
WIA_IPA_FULL_ITEM_NAME = 4099
WIA_IPA_ITEM_TIME = 4100
WIA_IPA_ITEM_FLAGS = 4101
WIA_IPA_ACCESS_RIGHTS = 4102
WIA_IPA_FORMAT = 4106
WIA_IPA_ITEM_SIZE = 4116
WIA_IPA_FILENAME_EXTENSION = 4123

#: The documented English display name of each property above, copied from the
#: ``*_STR`` constants that sit beside the ids in ``wiadef.h``. Used only as a
#: *fallback* when the numeric-id form returns nothing.
#:
#: The two forms are not interchangeable in Microsoft's own samples: an
#: **ImageFile**'s properties are indexed by id-as-string
#: (``Img.Properties("40091")``), while a **device item**'s properties are
#: indexed by name (``itm.Properties("Item Name")``, ``"Item Flags"``,
#: ``"Item Time Stamp"``). No published sample indexes a camera item's
#: properties by numeric id, so the id-only form this backend prefers is an
#: unverified assumption — and a catastrophic one to get wrong, because *every*
#: property read would fail at once, every item would look untransferable, and
#: the listing would come back empty. To the user that reads as "the memory card
#: is blank", which is the worst possible way to be wrong.
#:
#: Id first (it survives a localised Windows), documented name second.
_PROP_NAMES = {
    WIA_DIP_DEV_ID: "Unique Device ID",
    WIA_DIP_VEND_DESC: "Manufacturer",
    WIA_DIP_DEV_DESC: "Description",
    WIA_DIP_DEV_TYPE: "Type",
    WIA_DIP_PORT_NAME: "Port",
    WIA_DIP_DEV_NAME: "Name",
    WIA_IPA_ITEM_NAME: "Item Name",
    WIA_IPA_FULL_ITEM_NAME: "Full Item Name",
    WIA_IPA_ITEM_TIME: "Item Time Stamp",
    WIA_IPA_ITEM_FLAGS: "Item Flags",
    WIA_IPA_ACCESS_RIGHTS: "Access Rights",
    WIA_IPA_FORMAT: "Format",
    WIA_IPA_ITEM_SIZE: "Item Size",
    WIA_IPA_FILENAME_EXTENSION: "Filename extension",
}

# Item flags (WiaItemType*). Only the ones we act on are listed.
WIA_ITEM_TYPE_IMAGE = 0x00000001
WIA_ITEM_TYPE_FILE = 0x00000002
WIA_ITEM_TYPE_FOLDER = 0x00000004
WIA_ITEM_TYPE_DELETED = 0x00000080
WIA_ITEM_TYPE_DISCONNECTED = 0x00000100
WIA_ITEM_TYPE_STORAGE = 0x00001000
WIA_ITEM_TYPE_TRANSFER = 0x00002000
WIA_ITEM_TYPE_VIDEO = 0x00010000
#: ``WiaItemTypeRemoved``. The driver kept the node but the data behind it is
#: gone. It arrives as a negative ``VT_I4``; Python's arbitrary-precision ints
#: make ``flags & WIA_ITEM_TYPE_REMOVED`` come out right either way.
WIA_ITEM_TYPE_REMOVED = 0x80000000

#: Flags that mean "there is nothing here to rescue". Listing one of these would
#: inflate the count the user is shown and hand the transfer engine an item that
#: yields no bytes — which then looks like a rescued photo that may be erased.
WIA_ITEM_TYPE_GONE = (
    WIA_ITEM_TYPE_DELETED | WIA_ITEM_TYPE_DISCONNECTED | WIA_ITEM_TYPE_REMOVED
)

# Access rights (WIA_IPA_ACCESS_RIGHTS). Verified against the Windows 10 SDK's
# wiadef.h (10.0.14393.0), lines 1725-1729:
#   #define WIA_ITEM_CAN_BE_DELETED  0x80
#   #define WIA_ITEM_READ            WIA_PROP_READ    (0x01)
#   #define WIA_ITEM_WRITE           WIA_PROP_WRITE   (0x02)
# The deletability bit is 0x80, *not* 0x04 — 0x04 is easy to mis-remember and
# would silently enable a delete button on a read-only device. Note that 0x80 is
# also WiaItemTypeDeleted in the *item flags* property, which is where the
# confusion comes from; they are different properties.
WIA_ITEM_READ = 0x01
WIA_ITEM_WRITE = 0x02
WIA_ITEM_CAN_BE_DELETED = 0x80

# WiaDeviceType enumeration: Unspecified=0, Scanner=1, Camera=2, Video=3.
WIA_DEVICE_TYPE_CAMERA = 2

_DEVICE_MANAGER_PROGID = "WIA.DeviceManager"

#: Depth cap for the item-tree walk. Real cameras nest two or three levels
#: (root / DCIM / 118CANON); anything deeper is a driver bug or a loop, and we
#: would rather truncate the listing than hang the GUI forever.
_MAX_TREE_DEPTH = 12

#: HRESULT returned by ``CoInitialize`` when the thread already lives in a
#: different apartment model. It means "someone else set this thread up" — we
#: proceed and must *not* call ``CoUninitialize``, or we would tear down an
#: apartment we do not own.
_RPC_E_CHANGED_MODE = -2147417850  # 0x80010106

#: ``REGDB_E_CLASSNOTREG``. On a machine that plainly has WIA, this almost
#: always means the Python interpreter and ``wiaaut.dll`` are different
#: bitnesses rather than that anything is missing.
_REGDB_E_CLASSNOTREG = -2147221164  # 0x80040154

#: Shown next to every WIA camera in the GUI. The Windows path of this program
#: has never been run against real hardware; the person about to erase a
#: twenty-year-old memory card is entitled to know that before they do.
_UNVERIFIED_NOTICE = "never tested on real hardware"


# --------------------------------------------------------------------------- #
# Lazy COM plumbing
# --------------------------------------------------------------------------- #


def _import_com() -> Tuple[Any, Any]:
    """Import ``pythoncom`` and ``win32com.client`` on demand.

    Kept out of module scope so this file imports on macOS and Linux, where
    ``registry.py`` still pulls it in to build the environment panel.

    Raises:
        BackendUnavailable: when pywin32 is not installed. The message is shown
            to the user verbatim, so it names the exact pip command.
    """
    try:
        import pythoncom  # type: ignore[import-not-found]
        import win32com.client  # type: ignore[import-not-found]
    except ImportError:
        raise BackendUnavailable(
            "The Windows camera support (pywin32) is not installed. "
            "Run: pip install pywin32"
        )
    return pythoncom, win32com.client


@contextlib.contextmanager
def _com_apartment() -> Iterator[Any]:
    """Initialise the COM apartment for the calling thread, and tear it down.

    Every WIA call must happen between ``CoInitialize`` and ``CoUninitialize``
    *on the same thread*. Tkinter apps do this work on a worker thread, which
    starts life with no apartment at all, so skipping this yields the famously
    unhelpful "CoInitialize has not been called" error.

    Yields the ``pythoncom`` module, so a caller that wants to catch
    ``pythoncom.com_error`` specifically can reach it without a second import.
    Most callers here catch ``Exception`` instead and funnel it through
    :func:`_friendly`, because a WIA driver can fail with plain ``TypeError``
    from the marshalling layer just as easily as with a COM error.
    """
    pythoncom, _ = _import_com()

    owns_apartment = True
    try:
        pythoncom.CoInitialize()
    except Exception as exc:  # pragma: no cover - Windows-only path
        # RPC_E_CHANGED_MODE means the thread is already in a (different)
        # apartment. WIA automation is happy either way; we just must not
        # uninitialise an apartment that someone else created.
        if _hresult_of(exc) != _RPC_E_CHANGED_MODE:
            raise BackendUnavailable(
                "Windows could not start the imaging service (COM initialisation "
                "failed): %s" % _friendly(exc)
            )
        owns_apartment = False

    try:
        yield pythoncom
    finally:
        if owns_apartment:
            with contextlib.suppress(Exception):
                pythoncom.CoUninitialize()


def _hresult_of(exc: BaseException) -> Optional[int]:
    """Best-effort extraction of the HRESULT from a ``pythoncom.com_error``."""
    args = getattr(exc, "args", None) or ()
    if args and isinstance(args[0], int):
        return args[0]
    return None


def _friendly(exc: BaseException) -> str:
    """Turn a COM exception into one sentence a non-technical user can read.

    ``pythoncom.com_error`` args are ``(hresult, strerror, excepinfo, argerr)``
    where ``excepinfo`` — when present — carries the driver's own description at
    index 2. That description is by far the most useful part, so we prefer it.
    """
    args = getattr(exc, "args", None) or ()

    description = ""
    if len(args) >= 3 and isinstance(args[2], (tuple, list)) and len(args[2]) >= 3:
        candidate = args[2][2]
        if isinstance(candidate, str):
            description = candidate.strip()

    if not description and len(args) >= 2 and isinstance(args[1], str):
        description = args[1].strip()

    hresult = _hresult_of(exc)
    if description and hresult is not None:
        return "%s (0x%08X)" % (description, hresult & 0xFFFFFFFF)
    if description:
        return description
    if hresult is not None:
        return "Windows error 0x%08X" % (hresult & 0xFFFFFFFF)
    return str(exc) or exc.__class__.__name__


# --------------------------------------------------------------------------- #
# Property access
# --------------------------------------------------------------------------- #


def _prop(props: Any, pid: int, default: Any = None) -> Any:
    """Read one WIA property, returning ``default`` when it cannot be read.

    ``Properties.Item`` is documented as retrieving a property "either by
    position or name" and takes a VARIANT, so three lookup keys are plausible
    and only one of them is safe to try: the numeric id **rendered as a string**
    (never as a number, which would be read as a *position* and could silently
    return an unrelated property). That form is tried first because it survives
    a localised Windows install; the documented English display name is tried
    second, because Microsoft's own item-level samples use only that form.

    Never raises. A driver that does not implement a property is the norm, not
    an error, and the whole listing must not die because one camera omits a
    timestamp.

    TODO(hardware): confirm on real Windows hardware which key form a PTP camera
    accepts for *item* properties. Log both ``itm.Properties("4098").Value`` and
    ``itm.Properties("Item Name").Value`` for one item and report which work. If
    the id form works everywhere, the name fallback is dead weight and can go;
    if it never works, this fallback is the only thing keeping the backend from
    reporting every camera as empty.
    """
    keys = [str(int(pid))]
    name = _PROP_NAMES.get(int(pid))
    if name:
        keys.append(name)

    for key in keys:
        exists = None
        try:
            exists = bool(props.Exists(key))
        except Exception:
            # Some drivers raise instead of answering Exists(). An unanswered
            # question is not a "no", so fall through to the direct lookup.
            exists = None
        if exists is False:
            continue
        try:
            return props.Item(key).Value
        except Exception:
            continue
    return default


def _prop_int(props: Any, pid: int, default: int = 0) -> int:
    """Read a property expected to be numeric, coercing safely."""
    value = _prop(props, pid, None)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _prop_str(props: Any, pid: int, default: str = "") -> str:
    """Read a property expected to be text, coercing safely."""
    value = _prop(props, pid, None)
    if value is None:
        return default
    try:
        text = str(value).strip()
    except Exception:
        return default
    return text or default


def _item_mtime(props: Any) -> Optional[float]:
    """POSIX timestamp for ``WIA_IPA_ITEM_TIME``, or None when unavailable.

    The property is a ``VT_UI2|VT_VECTOR`` of eight words in ``SYSTEMTIME``
    order (year, month, day-of-week, day, hour, minute, second, milliseconds).
    The automation layer's ``Vector`` object also exposes a ``.Date`` shortcut,
    which is what Microsoft's own samples use; we try that first and fall back
    to decoding the raw words.

    Camera timestamps are wall-clock local time with no zone information, so the
    naive datetime is interpreted in the machine's local zone. That can be off
    by the zone difference if the photos were taken abroad — acceptable, since
    ``mtime`` is cosmetic here and never feeds verification.

    TODO(hardware): confirm on real Windows hardware what pywin32 returns for
    ``Vector.Date`` (expected: a ``pywintypes.datetime``, which subclasses
    ``datetime.datetime``). Both paths below are guarded, so a surprise here
    degrades to ``None`` rather than breaking the listing.
    """
    raw = _prop(props, WIA_IPA_ITEM_TIME, None)
    if raw is None:
        return None

    # Preferred path: the Vector's own Date conversion.
    try:
        as_date = raw.Date
    except Exception:
        as_date = None
    if as_date is not None:
        try:
            return float(as_date.timestamp())
        except Exception:
            try:
                return float(
                    datetime(
                        as_date.year,
                        as_date.month,
                        as_date.day,
                        as_date.hour,
                        as_date.minute,
                        as_date.second,
                    ).timestamp()
                )
            except Exception:
                return None

    # Fallback: raw SYSTEMTIME words. Index 2 is wDayOfWeek and is skipped.
    #
    # Microsoft's own sample gates on ``If v.Count = 8`` before touching the
    # vector, and so do we: *exactly* eight values, none of them text. Accepting
    # anything looser lets a 16-character PTP date string ("2003081319220000")
    # be iterated character by character into eight plausible-looking integers,
    # which decodes to a confident, silently wrong date. A missing timestamp is
    # honest; an invented one is not.
    if isinstance(raw, (str, bytes, bytearray)):
        return None
    try:
        values = list(raw)
    except Exception:
        return None
    if len(values) != 8:
        return None
    if any(isinstance(v, (str, bytes, bytearray)) for v in values):
        return None
    try:
        words = [int(v) for v in values]
    except (TypeError, ValueError):
        return None
    try:
        return float(
            datetime(
                words[0], words[1], words[3], words[4], words[5], words[6]
            ).timestamp()
        )
    except Exception:
        return None


def _dest_key(path: str) -> str:
    """Comparison key for a destination path, case-folded where the OS is."""
    return os.path.normcase(os.path.abspath(path))


def _folder_from_full_name(full_item_name: str) -> str:
    """Convert a WIA ``Full Item Name`` into the POSIX folder the contract wants.

    WIA hands back a backslash tree path that is prefixed with the device's
    index and a synthetic root, e.g.::

        0000\\Root\\DCIM\\118CANON\\IMG_1870   ->   /DCIM/118CANON
        0000\\Root\\IMG_1870                   ->   /

    Both leading segments are WIA bookkeeping rather than anything that exists
    on the camera's card, so they are stripped: ``CameraFile.path`` is the
    identity a file keeps across download and delete, and it should read like
    the path the user would see on the card.

    The device index is stripped only when it is entirely digits, so a real
    top-level folder never gets eaten by accident.

    TODO(hardware): confirm the real shape of ``WIA_IPA_FULL_ITEM_NAME`` for a
    PTP camera reached through the WPD compatibility layer — print it for one
    file and compare against the two forms above. Getting this wrong costs no
    data: ``folder`` only decides the disambiguating prefix used when two
    folders hold the same base name. It does decide whether the second copy is
    called ``118CANON_IMG_0001.JPG`` or something meaningless, and whether the
    listing the user reads looks like their memory card.
    """
    normalised = full_item_name.replace("\\", "/")
    segments = [seg for seg in normalised.split("/") if seg]

    # Drop the file's own name: only the containing folder is wanted.
    if segments:
        segments = segments[:-1]

    if segments and segments[0].isdigit():
        segments = segments[1:]
    if segments and segments[0].lower() == "root":
        segments = segments[1:]

    return "/" + "/".join(segments) if segments else "/"


def _coerce_bytes(data: Any) -> bytes:
    """Normalise whatever pywin32 hands back for a ``VT_ARRAY|VT_UI1`` VARIANT.

    ``Vector.BinaryData`` is documented as "the Vector of bytes as an array of
    bytes" — a "Variant array of unsigned one-byte characters" — but pywin32's
    marshalling of that VARIANT is not something this project has been able to
    verify on hardware. It may arrive as ``bytes``, as a ``memoryview``/buffer,
    or as a tuple of small ints. Handling all three costs one function and
    removes an entire class of "works on my machine" bug.

    The rejected shapes matter more than the accepted ones. ``bytearray(7)``
    returns *seven zero bytes*, so a bare ``bytes(bytearray(data))`` would turn
    a driver that answered with a plain integer into a silently fabricated file
    full of zeroes carrying a photograph's name — the exact failure this program
    exists to prevent. ``int``, ``bool`` and ``str`` are therefore refused by
    name rather than left to a constructor that would "helpfully" accept them.

    TODO(hardware): confirm the actual type on real Windows hardware
    (``type(item.Transfer().FileData.BinaryData)``) and, if it is always
    ``bytes``, delete the other branches rather than leave them untested.
    """
    if isinstance(data, bytes):
        return data
    if isinstance(data, (bytearray, memoryview)):
        return bytes(data)
    # int/bool would zero-fill; str would either raise or be encoded arbitrarily.
    # None means the driver returned nothing at all.
    if data is None or isinstance(data, (int, float, bool, str)):
        raise CameraError(
            "Windows returned the photo as %s instead of as image data, so "
            "nothing was saved. Please report this along with your camera "
            "model." % type(data).__name__
        )
    try:
        return bytes(bytearray(data))
    except Exception:
        raise CameraError(
            "Windows returned the photo in a format this program does not "
            "understand. Please report this along with your camera model."
        )


def _guid_key(value: Any) -> str:
    """Comparison key for a WIA FormatID, tolerant of braces and case.

    Format ids travel as BSTRs shaped ``{B96B3CAE-0728-11D3-9D7B-0000F81EF32E}``
    but nothing guarantees a driver echoes the braces or the case back, so both
    are normalised away before two ids are compared. Returns "" for anything
    that is not usable as an id, which callers must read as "unknown", never as
    "different".

    ``None`` in particular must come back as "" rather than as the string
    ``"None"``: a driver that leaves ``FormatID`` unset would otherwise look
    like a driver that returned a *different* format, and every transfer would
    be refused as a conversion.
    """
    if value is None:
        return ""
    try:
        text = str(value).strip()
    except Exception:
        return ""
    key = text.strip("{}").upper()
    # A GUID of all zeroes is wiaFormatUndefined - "unspecified format", not a
    # format that differs from the one we asked for.
    if not key or set(key) <= {"0", "-"}:
        return ""
    return key


# --------------------------------------------------------------------------- #
# The backend
# --------------------------------------------------------------------------- #


class WiaBackend(CameraBackend):
    """Talk to a PTP camera through the Windows Image Acquisition service.

    Deliberate limitations, all forced by the transport rather than chosen:

    * **No progress inside a file.** ``Item.Transfer`` is a single blocking call
      that returns a finished in-memory image; there is no chunk callback. The
      progress bar therefore moves once per file.
    * **Cancellation only between files.** :class:`~retrocam.model.CancelToken`
      is checked before each transfer and each deletion. A transfer already in
      flight cannot be interrupted.
    * **Each file is buffered fully in RAM.** Fine for the JPEG/AVI sizes these
      cameras produce (a few MB); it would not be fine for large raw files.

    Verification status
    -------------------
    Every constant and call signature in this module has been checked against
    Microsoft's published documentation and against ``wiadef.h`` from the
    Windows 10 SDK. Nothing in it has been run against a real camera, or against
    a real WIA driver, or on Windows at all. Those are different things, and
    only the first is done.

    Every remaining gap carries a ``TODO(hardware)`` naming the exact question
    and how to answer it. The rule they are all written to: **if an assumption
    turns out to be wrong, this backend must fail where the user can see it. It
    must never report a photo as rescued that it did not rescue**, because the
    next thing that happens to a rescued photo is that its original is erased.
    """

    kind = BackendKind.WIA
    display_name = "Windows camera (WIA)"
    description = (
        "Connects directly to a camera through Windows, with no driver install. "
        "Works with cameras that speak PTP; cameras older than that are "
        "invisible to Windows and need the card reader instead."
    )

    def __init__(self) -> None:
        # Cached answer for supports_delete(). None means "we have not looked at
        # a device yet", which the getter treats as "no" — see supports_delete.
        # This is a plain bool, not a COM handle, so caching it on self does not
        # violate the "no live handles on self" rule.
        self._delete_capable: Optional[bool] = None

    # -- capability probing ------------------------------------------------ #

    @classmethod
    def is_available(cls) -> Availability:
        """Fast, device-free probe: right OS, and is pywin32 importable?

        Deliberately does *not* create the ``WIA.DeviceManager`` object.
        Enumerating ``DeviceInfos`` is known to hang on machines with no imaging
        device or with the "Windows Image Acquisition" (stisvc) service stopped,
        and this method runs at startup on the GUI thread.
        """
        if sys.platform != "win32":
            return (False, "Windows camera support (WIA) only exists on Windows.")
        try:
            import win32com.client  # noqa: F401
        except ImportError:
            return (
                False,
                "pywin32 is not installed — run: pip install pywin32",
            )
        return (True, "")

    @classmethod
    def install_hint(cls) -> str:
        return "pip install pywin32"

    # -- discovery --------------------------------------------------------- #

    def detect(self, progress: ProgressCallback = noop_progress) -> List[CameraInfo]:
        """Enumerate WIA devices and keep the ones that are cameras.

        Returns an empty list when nothing is attached — that is a normal state,
        not an error. Scanners (device type 1) and webcams (3) are filtered out.

        Note for callers: run this on a worker thread. ``DeviceInfos`` can block
        for a long time when the imaging service is unhealthy, and there is no
        way to time out a COM call from Python.

        TODO(hardware): confirm whether a pre-PTP camera such as the PowerShot
        S30 shows up here at all (expected: no — it appears in Device Manager as
        an unknown device, or not at all). The user-facing copy in
        :meth:`list_files` and in the GUI depends on that answer.
        """
        self._require_windows()
        progress(Progress(phase="detect", message="Asking Windows for cameras..."))

        found: List[CameraInfo] = []
        with _com_apartment():
            manager = self._device_manager()
            try:
                infos = manager.DeviceInfos
                count = int(infos.Count)
            except Exception as exc:
                raise CameraError(
                    "Windows could not list imaging devices: %s\n"
                    "Check that the 'Windows Image Acquisition (WIA)' service is "
                    "running." % _friendly(exc)
                )

            # DeviceInfos is 1-based.
            for index in range(1, count + 1):
                try:
                    info = infos.Item(index)
                    device_type = _prop_int(info.Properties, WIA_DIP_DEV_TYPE, -1)
                    # Item.Type is the documented accessor; the property is the
                    # fallback for drivers that do not surface it there.
                    with contextlib.suppress(Exception):
                        device_type = int(info.Type)
                    if device_type != WIA_DEVICE_TYPE_CAMERA:
                        continue

                    props = info.Properties
                    device_id = str(info.DeviceID)
                    model = (
                        _prop_str(props, WIA_DIP_DEV_NAME)
                        or _prop_str(props, WIA_DIP_DEV_DESC)
                        or "Camera"
                    )
                    vendor = _prop_str(props, WIA_DIP_VEND_DESC)
                    port = _prop_str(props, WIA_DIP_PORT_NAME)
                except Exception as exc:
                    # One misbehaving device must not hide the others.
                    progress(
                        Progress(
                            phase="detect",
                            message="Skipped an imaging device Windows could not "
                            "describe: %s" % _friendly(exc),
                        )
                    )
                    continue

                # Connect once here so a camera that enumerates but refuses to
                # open is reported honestly instead of failing later, mid-rescue.
                reachable = True
                connect_error = ""
                try:
                    self._connect(manager, device_id)
                except CameraError as exc:
                    reachable = False
                    connect_error = str(exc)

                label = ("%s %s" % (vendor, model)).strip() if vendor else model
                detail = "WIA"
                if port:
                    detail = "WIA / %s" % port
                if not reachable:
                    detail = "%s - not responding" % detail
                # The GUI prints `detail` directly under the camera's name, on
                # the same screen as the Delete button. This is the one place a
                # caveat reaches the user at the moment it matters.
                detail = "%s - %s" % (detail, _UNVERIFIED_NOTICE)

                found.append(
                    CameraInfo(
                        model=label,
                        port=device_id,
                        kind=BackendKind.WIA,
                        detail=detail,
                        raw={
                            "device_id": device_id,
                            "reachable": reachable,
                            "connect_error": connect_error,
                        },
                    )
                )

        progress(
            Progress(
                phase="detect",
                total=len(found),
                message="Windows reported %d camera(s)." % len(found),
            )
        )
        if found:
            progress(
                Progress(
                    phase="detect",
                    message="Note: this Windows camera path has never been "
                    "tested against a real camera. Open your rescued photos and "
                    "check they are right before erasing anything.",
                )
            )
        return found

    def list_files(
        self,
        camera: CameraInfo,
        progress: ProgressCallback = noop_progress,
        cancel: Optional[CancelToken] = None,
    ) -> List[CameraFile]:
        """Walk the device's item tree and return every downloadable file.

        The tree is walked depth-first; folder items are descended into, deleted
        items are skipped, and anything that is not an image/file/transferable
        item is ignored. Under the WPD compatibility layer the tree is often
        *flat* (every image hanging off the root) rather than mirroring
        ``/DCIM/118CANON``, so both shapes are handled.

        ``CameraFile.raw`` stores only strings — the item's ``ItemID`` and its
        parent's — never the live COM object, because that object dies with the
        COM apartment at the end of this call and cannot legally cross threads.
        If the camera is unplugged and re-plugged, the ids may change: re-run
        detection and re-list before downloading.

        TODO(hardware): confirm which tree shape a PTP camera actually presents
        — flat (every image hanging off the root) or nested (``/DCIM/118CANON``)
        — because deletion takes a different route for each. A flat tree removes
        from ``Device.Items``, which is the collection Microsoft's own sample
        demonstrates; a nested one removes from a sub-folder's ``Items``, which
        no published sample demonstrates. See :meth:`delete`.
        """
        self._require_windows()
        progress(Progress(phase="list", message="Reading the camera index..."))

        collected: List[CameraFile] = []
        delete_bits_seen = False
        #: Counters filled in by the walk, used only to tell three very
        #: different situations apart in the message the user is shown.
        stats = {"visited": 0, "undescribed": 0}

        with _com_apartment():
            manager = self._device_manager()
            device = self._connect(manager, camera.port)
            try:
                root_items = device.Items
            except Exception as exc:
                raise CameraError(
                    "The camera connected but Windows could not read its file "
                    "list: %s" % _friendly(exc)
                )

            delete_bits_seen = self._walk_items(
                items=root_items,
                out=collected,
                progress=progress,
                cancel=cancel,
                parent_item_id="",
                depth=0,
                seen_ids=set(),
                stats=stats,
            )

        # Stable order: folder first, then name. Reproducible between runs, which
        # is what makes "resume" and the progress bar behave.
        collected.sort(key=lambda cf: (cf.folder.lower(), cf.name.lower()))

        # Cache the delete capability for supports_delete(); see its docstring
        # for why "we saw no bit at all" is treated as "no".
        self._delete_capable = bool(delete_bits_seen)

        if not collected and stats["undescribed"]:
            # The difference that matters: Windows *did* hand over items, and
            # not one of them would say what it was. That is this program
            # failing to read the camera, not an empty card — and telling the
            # user their card is blank when it is full of photographs is the
            # most damaging way this backend could be wrong. The most likely
            # cause is that the driver rejects the numeric property ids used
            # throughout this module; see the TODO(hardware) on :func:`_prop`.
            progress(
                Progress(
                    phase="list",
                    message="Windows listed %d item(s) on this camera but would "
                    "not describe any of them, so none could be read. This is a "
                    "fault in this program's Windows support, not an empty card "
                    "- please report it with your camera model. Your photos have "
                    "not been touched." % stats["undescribed"],
                )
            )
        elif not collected:
            progress(
                Progress(
                    phase="list",
                    message="The camera reported no photos. If you expected some, "
                    "check that the memory card is inserted.",
                )
            )
        else:
            progress(
                Progress(
                    phase="list",
                    total=len(collected),
                    message="Found %d file(s) on the camera." % len(collected),
                )
            )
            sizeless = sum(1 for cf in collected if not cf.size_known)
            if sizeless:
                # Not a defect — WIA documents a size of zero as "the driver has
                # no information", and notes it is common for compressed data,
                # which is every JPEG on the card. But it disables the strongest
                # integrity check this transport has, so the user gets told
                # rather than quietly given a weaker guarantee.
                progress(
                    Progress(
                        phase="list",
                        message="The camera did not report a size for %d of %d "
                        "file(s). Those copies can only be checked for a sound "
                        "structure, not for an exact length - look at them before "
                        "erasing anything." % (sizeless, len(collected)),
                    )
                )
        return collected

    # -- the two operations that matter ------------------------------------ #

    def download(
        self,
        camera: CameraInfo,
        files: Sequence[CameraFile],
        dest_dir: str,
        progress: ProgressCallback = noop_progress,
        cancel: Optional[CancelToken] = None,
        skip_existing: bool = True,
    ) -> List[DownloadOutcome]:
        """Transfer each file, writing to a ``.part`` name and renaming into place.

        One outcome per input file, in the same order, success or failure.

        The bytes are pulled into memory via ``ImageFile.FileData.BinaryData``
        and written by us, rather than using ``ImageFile.SaveFile``. That is
        deliberate: ``SaveFile`` gives no fsync and no atomic rename, and going
        through the byte array lets us compare the transferred length against
        the size the camera reported *before* the file ever gets its final name.
        On this transport that comparison is the strongest integrity signal
        available, and it is also what catches the transfer silently handing
        back a re-encoded BMP instead of the stored JPEG.

        ``ok`` here reflects the *transfer only* — the transfer engine runs
        verification afterwards and produces the final verdict.
        """
        self._require_windows()

        outcomes: List[DownloadOutcome] = []
        total = len(files)
        # Destination paths already spoken for by this batch, so that two files
        # sharing a base name cannot resolve to the same copy. See
        # :meth:`_existing_copy` for why that would be a photo-losing bug.
        claimed: set = set()

        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as exc:
            raise CameraError(
                "Cannot write to the destination folder %s: %s" % (dest_dir, exc)
            )

        with _com_apartment():
            manager = self._device_manager()
            device = self._connect(manager, camera.port)

            for index, camera_file in enumerate(files):
                # WIA cannot interrupt a transfer in flight, so this is the only
                # place cancellation can be honoured.
                if cancel is not None:
                    cancel.raise_if_cancelled()

                progress(
                    Progress(
                        phase="download",
                        index=index,
                        total=total,
                        name=camera_file.name,
                        bytes_total=max(camera_file.size, 0),
                        bytes_done=0,
                    )
                )

                if skip_existing:
                    existing = self._existing_copy(dest_dir, camera_file, claimed)
                    if existing is not None:
                        claimed.add(_dest_key(existing))
                        outcomes.append(
                            DownloadOutcome(
                                file=camera_file,
                                dest_path=existing,
                                ok=True,
                                skipped=True,
                            )
                        )
                        progress(
                            Progress(
                                phase="download",
                                index=index,
                                total=total,
                                name=camera_file.name,
                                bytes_total=max(camera_file.size, 0),
                                bytes_done=max(camera_file.size, 0),
                                message="Already downloaded, skipped.",
                            )
                        )
                        continue

                outcomes.append(
                    self._download_one(
                        device=device,
                        camera_file=camera_file,
                        dest_dir=dest_dir,
                    )
                )
                if outcomes[-1].dest_path:
                    claimed.add(_dest_key(str(outcomes[-1].dest_path)))

                last = outcomes[-1]
                progress(
                    Progress(
                        phase="download",
                        index=index,
                        total=total,
                        name=camera_file.name,
                        bytes_total=max(camera_file.size, 0),
                        bytes_done=max(camera_file.size, 0) if last.ok else 0,
                        message="" if last.ok else last.error,
                    )
                )

        return outcomes

    def delete(
        self,
        camera: CameraInfo,
        files: Sequence[CameraFile],
        progress: ProgressCallback = noop_progress,
        cancel: Optional[CancelToken] = None,
    ) -> List[DeleteOutcome]:
        """Erase the given files from the camera, one at a time. Irreversible.

        The automation API for erasing is ``Items.Remove(Index)`` on the
        collection that *contains* the item. Two rules follow from that, and
        breaking either one deletes the wrong photo:

        1. **Re-resolve the index by ``ItemID`` immediately before every single
           removal.** Indices in a live collection shift as items disappear, so
           an index captured during listing is stale the moment the first file
           is erased.
        2. **Nested items live in their parent folder's collection**, not in
           ``device.Items``. The parent id recorded during the walk is used to
           re-open the right collection.

        Nothing is ever bulk-deleted: the ``WIA_CMD_DELETE_ALL_ITEMS`` command
        exists and is never issued by this program.

        TODO(hardware): Microsoft's own sample only demonstrates removal from
        the root ``Device.Items`` collection. Removal from a *sub-folder's*
        collection is the logical extension but is undemonstrated — verify on
        real hardware with a camera that reports a nested tree before trusting
        it with the only copy of anything.
        """
        self._require_windows()

        outcomes: List[DeleteOutcome] = []
        total = len(files)

        with _com_apartment():
            manager = self._device_manager()
            device = self._connect(manager, camera.port)

            for index, camera_file in enumerate(files):
                if cancel is not None:
                    cancel.raise_if_cancelled()

                progress(
                    Progress(
                        phase="delete",
                        index=index,
                        total=total,
                        name=camera_file.name,
                    )
                )

                # A file the camera explicitly marked as non-deletable is refused
                # here rather than attempted; an unknown answer is attempted,
                # because by this point the caller has already gated on
                # supports_delete() and on a verified download.
                if camera_file.raw.get("can_delete") is False:
                    outcomes.append(
                        DeleteOutcome(
                            file=camera_file,
                            ok=False,
                            error="The camera reports this file cannot be erased "
                            "over USB. Erase it from the camera's own menu.",
                        )
                    )
                    continue

                outcomes.append(
                    self._delete_one(device=device, camera_file=camera_file)
                )

        return outcomes

    def supports_delete(self) -> bool:
        """Whether erasing over WIA is offered at all for the last-listed device.

        Conservative on purpose. This returns True only when at least one item in
        the most recent :meth:`list_files` explicitly advertised
        ``WIA_ITEM_CAN_BE_DELETED`` in its access rights. Before any listing has
        run — and on drivers that omit the access-rights property entirely — the
        answer is False, so the GUI greys the button out.

        The cost of a false negative is a greyed button and a manual erase from
        the camera menu. The cost of a false positive is an erase that fails
        halfway through a batch, on a device holding twenty-year-old photos.
        Those are not symmetric.

        TODO(hardware): confirm how widely the WPD-to-WIA compatibility layer
        populates ``WIA_IPA_ACCESS_RIGHTS`` for PTP cameras. If it turns out to
        be routinely absent on devices that *can* erase, this heuristic should
        gain an explicit user override ("my camera supports this, enable it")
        rather than being loosened by default.
        """
        return self._delete_capable is True

    # -- internals: connection --------------------------------------------- #

    @staticmethod
    def _require_windows() -> None:
        """Fail fast, and in the user's language, when this cannot possibly work."""
        available, hint = WiaBackend.is_available()
        if not available:
            raise BackendUnavailable(hint)

    @staticmethod
    def _device_manager() -> Any:
        """Create the WIA device manager for the current COM apartment.

        Late binding (``Dispatch``) is used on purpose instead of
        ``gencache.EnsureDispatch``: the makepy cache breaks under PyInstaller
        and in read-only installs, which is exactly how this app will ship.

        TODO(hardware): confirm that ``WIA.DeviceManager`` — the WIA Automation
        Layer, ``wiaaut.dll`` — is registered for the *same bitness* as the
        Python that runs this app on Windows 10 and 11. Nothing this program
        does works without it, and a 32/64-bit registration mismatch is the
        classic way to get REGDB_E_CLASSNOTREG (0x80040154) from an object that
        is plainly installed. One line in PowerShell answers it:
        ``New-Object -ComObject WIA.DeviceManager``.
        """
        _, win32com_client = _import_com()
        try:
            return win32com_client.Dispatch(_DEVICE_MANAGER_PROGID)
        except Exception as exc:
            hresult = _hresult_of(exc)
            detail = ""
            if hresult == _REGDB_E_CLASSNOTREG:
                # Naming the actual cause matters: "class not registered" sends
                # people to reinstall Windows components they already have.
                detail = (
                    "\nWindows reports that the imaging component is not "
                    "registered. This usually means Python and the Windows "
                    "Image Acquisition component are different bitnesses "
                    "(32-bit vs 64-bit)."
                )
            raise BackendUnavailable(
                "Windows Image Acquisition is not available on this PC: %s\n"
                "Check that the 'Windows Image Acquisition (WIA)' service is "
                "running.%s" % (_friendly(exc), detail)
            )

    @staticmethod
    def _connect(manager: Any, device_id: str) -> Any:
        """Open a connection to a device by its WIA DeviceID.

        The automation layer accepts the DeviceID string as the index into the
        ``DeviceInfos`` collection — ``DevMan.DeviceInfos(DevID).Connect`` is
        the form Microsoft's own sample uses to reconnect later. Some drivers
        reject it, so we fall back to scanning the collection for a matching
        ``DeviceID``.

        TODO(hardware): confirm that a camera's ``DeviceID`` is stable across an
        unplug/re-plug cycle. Every later call in this backend takes the id from
        ``CameraInfo.port``, so if Windows mints a new one per connection the
        user's second action after re-plugging fails with "that camera is no
        longer connected" until they press Search again. That is the safe
        direction to be wrong in, but it should be known rather than assumed.
        """
        if not device_id:
            raise CameraError("No camera was selected.")

        try:
            return manager.DeviceInfos(device_id).Connect()
        except Exception:
            pass  # Fall through to the explicit scan below.

        try:
            infos = manager.DeviceInfos
            for index in range(1, int(infos.Count) + 1):
                info = infos.Item(index)
                if str(info.DeviceID) == str(device_id):
                    return info.Connect()
        except Exception as exc:
            raise CameraError(
                "Windows could not open the camera: %s\n"
                "Try unplugging it, switching it on, and plugging it back in."
                % _friendly(exc)
            )

        raise CameraError(
            "That camera is no longer connected. Plug it back in and press "
            "Search again."
        )

    # -- internals: listing ------------------------------------------------- #

    def _walk_items(
        self,
        items: Any,
        out: List[CameraFile],
        progress: ProgressCallback,
        cancel: Optional[CancelToken],
        parent_item_id: str,
        depth: int,
        seen_ids: set,
        stats: dict,
    ) -> bool:
        """Depth-first walk of one WIA item collection, appending to ``out``.

        Returns True when at least one item advertised the "can be deleted"
        access-rights bit, which is what :meth:`supports_delete` keys off.

        ``stats`` accumulates two counters across the whole walk: ``visited``,
        every node reached, and ``undescribed``, every node whose flags could
        not be read as anything at all. The second is what lets
        :meth:`list_files` tell "this card is empty" apart from "this program
        could not read a single property", which look identical from here and
        could not be more different to the person holding the camera.
        """
        if depth > _MAX_TREE_DEPTH:
            return False

        delete_bit_seen = False

        try:
            count = int(items.Count)
        except Exception:
            return False

        # Collections are 1-based.
        for index in range(1, count + 1):
            if cancel is not None:
                cancel.raise_if_cancelled()

            try:
                item = items.Item(index)
                props = item.Properties
                item_id = str(item.ItemID)
            except Exception as exc:
                progress(
                    Progress(
                        phase="list",
                        message="Skipped an item the camera would not describe: %s"
                        % _friendly(exc),
                    )
                )
                continue

            # Some drivers expose the same node twice (root aliases, storage
            # shortcuts). Without this guard those become an infinite descent.
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            stats["visited"] = stats.get("visited", 0) + 1

            flags = _prop_int(props, WIA_IPA_ITEM_FLAGS, 0)
            if flags == 0:
                # No flags at all. Either a genuine property-bag node with no
                # data behind it, or - far more likely if it happens to every
                # item - this program failing to read properties on this driver.
                stats["undescribed"] = stats.get("undescribed", 0) + 1

            # Deleted / disconnected / removed items are tombstones: the node
            # survives but the data behind it does not. Listing one inflates the
            # count the user is shown and hands the transfer engine an item that
            # yields nothing.
            if flags & WIA_ITEM_TYPE_GONE:
                continue

            if flags & (WIA_ITEM_TYPE_FOLDER | WIA_ITEM_TYPE_STORAGE):
                try:
                    children = item.Items
                except Exception:
                    continue
                if self._walk_items(
                    items=children,
                    out=out,
                    progress=progress,
                    cancel=cancel,
                    parent_item_id=item_id,
                    depth=depth + 1,
                    seen_ids=seen_ids,
                    stats=stats,
                ):
                    delete_bit_seen = True
                continue

            downloadable = (
                WIA_ITEM_TYPE_FILE
                | WIA_ITEM_TYPE_TRANSFER
                | WIA_ITEM_TYPE_IMAGE
                | WIA_ITEM_TYPE_VIDEO
            )
            if not flags & downloadable:
                continue  # Not something we can pull bytes out of.

            camera_file, has_delete_bit = self._build_camera_file(
                item=item, props=props, item_id=item_id, parent_item_id=parent_item_id
            )
            if has_delete_bit:
                delete_bit_seen = True
            out.append(camera_file)

            progress(
                Progress(
                    phase="list",
                    index=len(out),
                    name=camera_file.name,
                )
            )

        return delete_bit_seen

    @staticmethod
    def _item_display_name(props: Any, item_id: str) -> str:
        """The file name for one item, from its name and extension properties.

        Shared by the listing and by the pre-delete identity check, so that the
        two can be compared at all: deriving the name twice by two slightly
        different routes would make every comparison a coin toss.
        """
        name = _prop_str(props, WIA_IPA_ITEM_NAME) or item_id.rsplit("\\", 1)[-1]
        extension = _prop_str(props, WIA_IPA_FILENAME_EXTENSION)
        # WIA_IPA_ITEM_NAME is usually the bare stem ("IMG_1870"); the extension
        # is a separate property with no leading dot.
        if extension and not name.lower().endswith("." + extension.lower()):
            name = "%s.%s" % (name, extension)
        return name or "unnamed.bin"

    @staticmethod
    def _item_size(props: Any) -> int:
        """Exact byte count for one item, or ``-1`` when the driver won't say.

        ``WIA_IPA_ITEM_SIZE`` is documented as zero when "the WIA minidriver has
        no information about the exact size of the data", a situation Microsoft
        describes as "common for compressed data". Zero therefore means unknown,
        not empty, and maps to -1 so downstream verification knows not to trust
        a length comparison. Inventing a size here — or back-filling it from the
        bytes that arrive — would make the transfer check itself against itself.
        """
        raw_size = _prop(props, WIA_IPA_ITEM_SIZE, None)
        try:
            size = int(raw_size)
        except (TypeError, ValueError):
            return -1
        return size if size > 0 else -1

    @staticmethod
    def _build_camera_file(
        item: Any, props: Any, item_id: str, parent_item_id: str
    ) -> Tuple[CameraFile, bool]:
        """Turn one WIA item into a :class:`CameraFile`. Returns (file, deletable)."""
        name = WiaBackend._item_display_name(props, item_id)
        folder = _folder_from_full_name(_prop_str(props, WIA_IPA_FULL_ITEM_NAME))
        size = WiaBackend._item_size(props)

        access_rights = _prop(props, WIA_IPA_ACCESS_RIGHTS, None)
        can_delete: Optional[bool]
        if access_rights is None:
            can_delete = None  # Property absent: unknown, not "no".
        else:
            try:
                can_delete = bool(int(access_rights) & WIA_ITEM_CAN_BE_DELETED)
            except (TypeError, ValueError):
                can_delete = None

        camera_file = CameraFile(
            folder=folder,
            name=name,
            size=size,
            mtime=_item_mtime(props),
            raw={
                # Strings only. The live COM object dies with this apartment.
                "item_id": item_id,
                "parent_item_id": parent_item_id,
                "format": _prop_str(props, WIA_IPA_FORMAT),
                "flags": _prop_int(props, WIA_IPA_ITEM_FLAGS, 0),
                "can_delete": can_delete,
            },
        )
        return camera_file, can_delete is True

    @staticmethod
    def _resolve_item(device: Any, item_id: str) -> Any:
        """Re-open an item from its stored ``ItemID`` on a fresh connection.

        ``Device.GetItem`` is the direct route; when a driver does not implement
        it we fall back to walking the tree looking for a matching id. Either
        way the identity check is on the id string, never on a remembered index.
        """
        if not item_id:
            raise CameraError("Internal error: this file has no camera item id.")

        try:
            item = device.GetItem(item_id)
            if item is not None:
                return item
        except Exception:
            pass  # Fall through to the scan.

        # Reading ``device.Items`` is itself a COM call and can fail — the
        # camera may have been unplugged between the listing and this moment.
        # Everything that calls this method catches CameraError and nothing
        # else, so a raw com_error escaping here would abort the whole batch
        # with a traceback instead of failing one file.
        try:
            root_items = device.Items
        except Exception as exc:
            raise CameraError(
                "Windows could not re-read the camera's file list: %s\n"
                "Try unplugging the camera, switching it on, and plugging it "
                "back in." % _friendly(exc)
            )

        found = WiaBackend._find_item(root_items, item_id, 0, set())
        if found is None:
            raise CameraError(
                "This file is no longer on the camera (or the camera was "
                "unplugged). Search for the camera again and re-read the list."
            )
        return found

    @staticmethod
    def _find_item(items: Any, item_id: str, depth: int, seen_ids: set) -> Any:
        """Recursive search of an item tree for one ``ItemID``. None when absent."""
        if depth > _MAX_TREE_DEPTH:
            return None
        try:
            count = int(items.Count)
        except Exception:
            return None

        for index in range(1, count + 1):
            try:
                item = items.Item(index)
                current_id = str(item.ItemID)
            except Exception:
                continue
            if current_id in seen_ids:
                continue
            seen_ids.add(current_id)
            if current_id == item_id:
                return item
            # A node that will not describe itself is skipped, not fatal: this
            # runs inside download and delete, whose callers catch CameraError
            # and nothing else, so an unguarded COM failure here would end the
            # whole batch rather than one file.
            try:
                flags = _prop_int(item.Properties, WIA_IPA_ITEM_FLAGS, 0)
            except Exception:
                continue
            if flags & (WIA_ITEM_TYPE_FOLDER | WIA_ITEM_TYPE_STORAGE):
                try:
                    children = item.Items
                except Exception:
                    continue
                found = WiaBackend._find_item(children, item_id, depth + 1, seen_ids)
                if found is not None:
                    return found
        return None

    # -- internals: download ------------------------------------------------ #

    def _download_one(
        self, device: Any, camera_file: CameraFile, dest_dir: str
    ) -> DownloadOutcome:
        """Transfer one file. Never raises for a per-file problem.

        Only cancellation and truly fatal conditions propagate; anything else
        becomes a failed outcome so the remaining files still get their chance.
        """
        try:
            item = self._resolve_item(device, str(camera_file.raw.get("item_id", "")))
        except CameraError as exc:
            return DownloadOutcome(
                file=camera_file, dest_path=None, ok=False, error=str(exc)
            )

        # Ask for the item's own stored format so Windows performs a straight
        # file transfer instead of decoding and re-encoding. A re-encode would
        # both change the bytes and destroy the EXIF metadata.
        #
        # Asking is not the same as getting. Item.Transfer is documented to
        # return the requested format "if the device supports that format;
        # otherwise this method uses the preferred format for this imaging
        # device" — a silent substitution with no error and no flag. That single
        # sentence is the largest correctness risk in this backend, so the
        # answer is checked below rather than assumed.
        wanted_format = str(camera_file.raw.get("format") or "")
        try:
            image = item.Transfer(wanted_format) if wanted_format else item.Transfer()
        except Exception as exc:
            # Some drivers reject an explicit format argument outright. Retrying
            # bare recovers the photo; not retrying loses it for a formality.
            # The bare call is the one most likely to hand back a conversion, so
            # the format check below is not skipped for it.
            try:
                image = item.Transfer()
            except Exception:
                return DownloadOutcome(
                    file=camera_file,
                    dest_path=None,
                    ok=False,
                    error="Windows could not read this photo from the camera: %s"
                    % _friendly(exc),
                )

        conversion = self._detect_conversion(image, camera_file, wanted_format)
        if conversion:
            return DownloadOutcome(
                file=camera_file, dest_path=None, ok=False, error=conversion
            )

        try:
            data = _coerce_bytes(image.FileData.BinaryData)
        except CameraError as exc:
            return DownloadOutcome(
                file=camera_file, dest_path=None, ok=False, error=str(exc)
            )
        except Exception as exc:
            return DownloadOutcome(
                file=camera_file,
                dest_path=None,
                ok=False,
                error="Windows transferred this photo but returned no usable "
                "data: %s" % _friendly(exc),
            )

        if not data:
            return DownloadOutcome(
                file=camera_file,
                dest_path=None,
                ok=False,
                error="The camera returned an empty file. Nothing was written.",
            )

        # The strongest integrity check this transport allows, applied *before*
        # the file can ever carry its final name. A mismatch usually means
        # Windows silently converted the image (typically to BMP) rather than
        # handing over the stored bytes — writing that would quietly replace the
        # user's original JPEG with a lossy re-encode.
        if camera_file.size_known and len(data) != camera_file.size:
            return DownloadOutcome(
                file=camera_file,
                dest_path=None,
                ok=False,
                error=(
                    "Windows returned %d bytes but the camera says the file is "
                    "%d bytes. It may have converted the image instead of "
                    "copying it, so nothing was saved." % (len(data), camera_file.size)
                ),
            )

        temp_path = os.path.join(dest_dir, ".rcr-%s.part" % uuid.uuid4().hex)
        try:
            with open(temp_path, "wb") as handle:
                handle.write(data)
                handle.flush()
                # fsync before the rename: without it a power cut can leave a
                # correctly-named file full of zeroes, which is worse than no
                # file at all because the user would delete the original.
                os.fsync(handle.fileno())

            # Resolve the final name only now, immediately before the rename, so
            # the collision check reflects the directory as it is at this moment.
            final_path = self.safe_dest_path(dest_dir, camera_file)
            os.replace(temp_path, final_path)
        except OSError as exc:
            with contextlib.suppress(OSError):
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            return DownloadOutcome(
                file=camera_file,
                dest_path=None,
                ok=False,
                error="Could not save the photo to disk: %s" % exc,
            )

        # ok reflects the transfer only. The transfer engine verifies the file
        # afterwards and produces the outcome the delete gate actually reads.
        return DownloadOutcome(file=camera_file, dest_path=final_path, ok=True)

    #: Formats WIA converts *to* when it decides not to hand over the stored
    #: bytes. Seeing one of these come back for a file the camera called .JPG or
    #: .AVI is proof of a conversion, not a suspicion.
    _CONVERSION_EXTENSIONS = frozenset({"bmp", "png", "gif", "tif", "tiff"})

    @staticmethod
    def _detect_conversion(
        image: Any, camera_file: CameraFile, wanted_format: str
    ) -> str:
        """Reason to refuse this transfer as a re-encode, or ``""`` to accept it.

        ``Item.Transfer(FormatID)`` is documented to fall back to the device's
        *preferred* format when the requested one is unsupported, silently and
        with no error. The transferred bytes would then be a decoded, re-encoded
        picture rather than the file on the card: different bytes, no EXIF, and
        — if it lands as a JPEG — something that passes every structural check
        this program can make. The user's original would then be erased in
        favour of a lossy copy.

        The length comparison in :meth:`_download_one` catches this whenever the
        camera reported a size. It very often does not: ``WIA_IPA_ITEM_SIZE`` is
        documented to be zero when "the WIA minidriver has no information about
        the exact size of the data", and Microsoft notes that "this situation is
        common for compressed data" — which is to say, common for JPEG, which is
        every file this program exists to rescue. So the size guard cannot be
        the only guard.

        Two independent signals are read back off the returned ``ImageFile``:

        1. ``FormatID`` — the format actually delivered. A definite mismatch
           against the format that was asked for is a definite conversion.
        2. ``FileExtension`` — checked only against the handful of formats WIA
           converts *to*. Narrow on purpose: a false refusal costs the user a
           photograph they can no longer rescue with this program, so this must
           not fire on a driver that simply words an extension differently.

        Either signal being unreadable means *unknown*, never *different*: a
        driver that does not implement these properties must not lose the run.

        TODO(hardware): confirm on real Windows hardware that a PTP camera's
        stored JPEG comes back with ``ImageFile.FormatID`` equal to the item's
        ``WIA_IPA_FORMAT`` and ``FileExtension == "jpg"``, and that an AVI clip
        comes back as itself rather than as a converted still. If this guard
        turns out to fire on healthy transfers, fix the comparison — do not
        delete the guard, because it is the only protection a size-less driver
        has.
        """
        requested = _guid_key(wanted_format)
        try:
            delivered = _guid_key(image.FormatID)
        except Exception:
            delivered = ""

        if requested and delivered and requested != delivered:
            return (
                "Windows converted this photo instead of copying it (the camera "
                "stores it as %s but Windows returned %s), so nothing was saved. "
                "Please report this along with your camera model."
                % (wanted_format, delivered)
            )

        try:
            delivered_ext = str(image.FileExtension or "").strip().lstrip(".").lower()
        except Exception:
            delivered_ext = ""

        camera_ext = os.path.splitext(camera_file.name)[1].lstrip(".").lower()
        if (
            delivered_ext in WiaBackend._CONVERSION_EXTENSIONS
            and camera_ext
            and camera_ext != delivered_ext
        ):
            return (
                "Windows returned this photo as a .%s image rather than the .%s "
                "file stored on the camera, which means it converted it. Nothing "
                "was saved." % (delivered_ext, camera_ext)
            )

        return ""

    @staticmethod
    def _existing_copy(
        dest_dir: str, camera_file: CameraFile, claimed: Optional[set] = None
    ) -> Optional[str]:
        """Path of an already-downloaded copy of this file, or None.

        Resume support, and deliberately narrow: only an exact size match on one
        of the two names :meth:`CameraBackend.safe_dest_path` would have chosen
        counts. When the camera did not report a size we always re-download,
        because "a file with the right name exists" is not evidence that it
        holds the right bytes — and the whole delete gate hangs off that.

        ``claimed`` holds the destination paths earlier files in this same batch
        already own, and skipping them is not an optimisation but a safety
        requirement. Two cameras folders can hold the same base name (the frame
        counter rolls over), and under the WPD compatibility shim the tree is
        often flat, so duplicates are *more* likely here than elsewhere. Without
        this check the second file matches the copy the first one just wrote —
        same name, same size, different picture — and the transfer engine would
        then verify photo A's bytes and clear photo B for deletion from the
        camera. Trying the names in the same order ``safe_dest_path`` does keeps
        a resumed run agreeing with the run it is resuming.

        Note that no timestamp comparison is possible on this transport: WIA
        hands over bytes, not file metadata, so a downloaded copy carries the
        time it was written rather than the time the photo was taken. Size plus
        an unclaimed name is all the evidence there is.
        """
        if not camera_file.size_known:
            return None

        safe_name = os.path.basename(camera_file.name.replace("\\", "/")).strip()
        if not safe_name or safe_name in (".", ".."):
            return None

        folder_tag = camera_file.folder.rstrip("/").split("/")[-1] or "DCIM"
        candidates = [
            os.path.join(dest_dir, safe_name),
            os.path.join(dest_dir, "%s_%s" % (folder_tag, safe_name)),
        ]
        for candidate in candidates:
            if claimed is not None and _dest_key(candidate) in claimed:
                continue  # another file in this batch owns that copy
            try:
                if os.path.isfile(candidate) and (
                    os.path.getsize(candidate) == camera_file.size
                ):
                    return candidate
            except OSError:
                continue
        return None

    # -- internals: delete -------------------------------------------------- #

    def _delete_one(self, device: Any, camera_file: CameraFile) -> DeleteOutcome:
        """Erase one item, re-resolving its index in its parent collection first."""
        item_id = str(camera_file.raw.get("item_id", ""))
        if not item_id:
            return DeleteOutcome(
                file=camera_file,
                ok=False,
                error="Internal error: this file has no camera item id.",
            )

        try:
            parent_items = self._parent_collection(
                device, str(camera_file.raw.get("parent_item_id", ""))
            )
        except CameraError as exc:
            return DeleteOutcome(file=camera_file, ok=False, error=str(exc))

        # Re-resolve the index *now*: every successful removal shifts the ones
        # after it, so an index from listing time points at a different photo.
        # This is the pattern Microsoft's own automation sample uses -
        #
        #     For i = 1 to Dev.Items.Count
        #         If Dev.Items(i).ItemID = itm.ItemID Then Dev.Items.Remove i
        #
        # - and it is only as trustworthy as ``ItemID`` itself.
        index = None
        target = None
        try:
            count = int(parent_items.Count)
            for candidate in range(1, count + 1):
                entry = parent_items.Item(candidate)
                if str(entry.ItemID) == item_id:
                    index = candidate
                    target = entry
                    break
        except Exception as exc:
            return DeleteOutcome(
                file=camera_file,
                ok=False,
                error="Could not re-check the file on the camera before erasing "
                "it, so nothing was erased: %s" % _friendly(exc),
            )

        if index is None or target is None:
            # Already gone. Not an error worth alarming the user about, but not
            # a success either: report it so the count stays honest.
            return DeleteOutcome(
                file=camera_file,
                ok=False,
                error="This file is no longer on the camera.",
            )

        mismatch = self._identity_mismatch(target, camera_file)
        if mismatch:
            return DeleteOutcome(file=camera_file, ok=False, error=mismatch)

        try:
            parent_items.Remove(index)
        except Exception as exc:
            return DeleteOutcome(
                file=camera_file,
                ok=False,
                error="The camera refused to erase this file: %s" % _friendly(exc),
            )

        return DeleteOutcome(file=camera_file, ok=True)

    @staticmethod
    def _identity_mismatch(item: Any, camera_file: CameraFile) -> str:
        """Reason this item is not the file we listed, or ``""`` when it is.

        The last thing standing between a wrong assumption and a destroyed
        photograph. Matching on ``ItemID`` alone assumes two things Microsoft
        documents nowhere: that an ``ItemID`` identifies the same file after the
        camera has been unplugged and re-plugged, and that a driver never
        recycles one. If either is false, the id found in the collection belongs
        to a *different* picture and ``Remove`` would erase it — a picture the
        user never selected and this program never rescued.

        So the item is asked who it is, immediately before it is destroyed, and
        the answer must agree with the listing on:

        * **kind** — anything flagged folder or storage is refused outright.
          ``Remove`` on a folder would take its whole contents with it, which is
          one step from formatting the card. Nothing in this program ever
          selects a folder, so reaching here at all means something is wrong.
        * **name** — the identity the user actually sees and chose.
        * **size** — checked only when both sides know it, since
          ``WIA_IPA_ITEM_SIZE`` is legitimately absent on many drivers.

        A property that cannot be read is *not* treated as a mismatch: a driver
        that answers nothing must not make every deletion fail. It does mean
        this guard degrades to the ItemID match alone on such a device, which is
        exactly the situation the TODO below asks a tester to characterise.

        TODO(hardware): confirm on real Windows hardware that ``ItemID`` values
        survive a disconnect/reconnect cycle, and that they are never reused for
        a different file after a deletion. Procedure: list a card, note the ids,
        unplug and re-plug the camera, list again and compare; then erase one
        file, re-list, and check no surviving file has taken the erased one's id.
        Until that is done this check — not the id — is what makes deletion safe.
        """
        try:
            props = item.Properties
        except Exception:
            # Cannot interrogate it at all. The id matched; nothing contradicts
            # the listing; refusing every deletion on such a driver would be its
            # own kind of wrong. Proceed on the id alone.
            return ""

        flags = _prop_int(props, WIA_IPA_ITEM_FLAGS, 0)
        if flags & (WIA_ITEM_TYPE_FOLDER | WIA_ITEM_TYPE_STORAGE):
            return (
                "Refusing to erase %s: the camera now reports it as a folder, "
                "not a photo. Nothing was erased. Please report this."
                % camera_file.name
            )

        found_name = _prop_str(props, WIA_IPA_ITEM_NAME)
        if found_name:
            expected = camera_file.name
            actual = WiaBackend._item_display_name(props, "")
            if actual.lower() != expected.lower():
                return (
                    "Refusing to erase %s: the camera now calls that file %s "
                    "instead. The list is out of date, so nothing was erased — "
                    "search for the camera again and re-read the list."
                    % (expected, actual)
                )

        found_size = WiaBackend._item_size(props)
        if (
            camera_file.size_known
            and found_size >= 0
            and found_size != camera_file.size
        ):
            return (
                "Refusing to erase %s: the camera now reports it as %d bytes "
                "instead of %d. The list is out of date, so nothing was erased — "
                "search for the camera again and re-read the list."
                % (camera_file.name, found_size, camera_file.size)
            )

        return ""

    @staticmethod
    def _parent_collection(device: Any, parent_item_id: str) -> Any:
        """The ``Items`` collection that directly contains a file.

        Root-level files live in ``Device.Items``; nested ones live in their
        folder's own ``Items``. Removing by index from the wrong collection is
        precisely how a delete-the-wrong-photo bug happens, so the parent is
        re-opened by id rather than assumed.
        """
        if not parent_item_id:
            try:
                return device.Items
            except Exception as exc:
                raise CameraError(
                    "Windows could not re-read the camera's file list: %s"
                    % _friendly(exc)
                )

        parent = WiaBackend._resolve_item(device, parent_item_id)
        try:
            return parent.Items
        except Exception as exc:
            raise CameraError(
                "Windows could not open the folder holding this file: %s"
                % _friendly(exc)
            )
