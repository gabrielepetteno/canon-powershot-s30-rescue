"""Integrity checks for files that have just been copied off a camera.

This module answers one question — *is the copy on disk complete and readable?*
— and the answer is what the delete gate reads before erasing anything from a
20-year-old card. That makes the failure modes wildly asymmetric:

* A **false negative** (a good file reported as suspect) costs the user a little
  card space: the file stays on the camera and is simply not offered for
  deletion. Annoying, recoverable.
* A **false positive** (a corrupt file reported as fine) destroys an
  irreplaceable photo, because the original is then erased from the card.

Every judgement call in here is therefore resolved toward ``ok=False``. When we
cannot prove a file is intact, we say so; we never assume.

The checks run cheapest-first, so a truncated 800 KB JPEG is rejected after two
``stat`` fields rather than after a full decode:

1. the path exists and is a regular file;
2. the file is not empty;
3. the byte count matches what the camera reported (when it reported one);
4. the container structure matches the extension (JPEG SOI/EOI, TIFF/CIFF
   magic, RIFF form);
5. optionally, Pillow fully decodes the pixels.

Pillow is an **optional** enhancement. It is imported lazily inside the
functions that use it so the application starts, detects cameras and downloads
photos on a bare Python install. Without it we still run steps 1-4 and report
``checked_decode=False``, which the GUI shows so the user knows how strong the
guarantee behind a green tick actually is.

Known limits of step 5 (measured, not assumed)
----------------------------------------------
* ``Image.verify()`` is a real check for PNG but a no-op for JPEG, so step 5
  always follows it with a fresh open and a full ``load()``.
* libjpeg treats an ``FFD9`` end marker as a clean end of image. A JPEG that
  was cut short and then *re-terminated* with ``FFD9`` therefore decodes
  without error, with the missing rows silently filled in. **No decoder can
  catch that**, which is why the exact byte count from the camera listing
  (step 3) is the primary truncation defence and step 5 is complementary
  rather than a replacement for it.
* A decode failure only counts as evidence of damage for formats Pillow is
  guaranteed to handle (see ``_STRICT_DECODE_EXTS``). For a Canon CRW, a CR2
  whose main image uses lossless JPEG, or an AVI clip, "Pillow cannot read
  this" means the codec is missing, not that the bytes are bad — failing those
  would make every raw and every movie on the card permanently undeletable,
  which pushes users toward deleting with no verification at all.

Note on ``VerifyResult.reason``: it is normally empty when ``ok`` is True, but
this module also uses it to carry *advisory notes* about files that passed with
a caveat — trailing padding after a JPEG's end marker, or an extension we have
no structural knowledge of. Callers must key their logic on ``ok`` alone and
treat ``reason`` as display text.

Reason strings are intentionally plain English rather than ``i18n.t()`` keys:
they are diagnostic detail that ends up in the log pane and in bug reports, and
they must stay stable and greppable across languages.
"""

from __future__ import annotations

import os
import stat as stat_module
import warnings
from typing import FrozenSet, Optional, Tuple

from .model import VerifyResult

__all__ = ["verify_download", "pillow_available"]


# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #

#: Bytes read from the front of the file. Every magic number we test lives in
#: the first 14 bytes; 64 leaves room without a second syscall.
_HEAD_BYTES = 64

#: How far back from EOF we hunt for a JPEG's end-of-image marker. Some cameras
#: (and some card readers that pad to a sector boundary) leave a run of NULs
#: after the real end of the image, so checking only the final two bytes would
#: reject perfectly good photos. 64 KiB is far more slack than any real camera
#: needs while staying a single cheap read.
_JPEG_TAIL_WINDOW = 64 * 1024

#: Bytes tolerated between a JPEG's FFD9 marker and EOF. NUL is sector padding;
#: FF is the JPEG fill byte. Anything else means we are not looking at the end
#: of the image we think we are, so the file is rejected.
_JPEG_PAD_BYTES: FrozenSet[int] = frozenset((0x00, 0xFF))

# Extension groups. ``.thm`` is the JPEG thumbnail Canon writes beside a movie
# clip; it is a plain baseline JPEG despite the extension.
_JPEG_EXTS: FrozenSet[str] = frozenset((".jpg", ".jpeg", ".thm"))
_TIFF_EXTS: FrozenSet[str] = frozenset((".tif", ".tiff", ".cr2"))

#: Extensions for which a Pillow failure is treated as proof of damage.
#: Deliberately only JPEG: it is what a PowerShot writes, every Pillow build
#: decodes it, and so a refusal to decode one can only mean the bytes are bad.
#: For every other format a failure is ambiguous (see the module docstring) and
#: is downgraded to "not verified" instead of "corrupt".
_STRICT_DECODE_EXTS: FrozenSet[str] = _JPEG_EXTS

#: TIFF byte-order mark plus the magic number 42, in both endiannesses.
_TIFF_MAGICS = (b"II\x2a\x00", b"MM\x00\x2a")

#: Upper bound on a decoder message quoted into ``VerifyResult.reason``. The
#: reason is shown in a table cell next to the file name; a decoder that dumps a
#: paragraph must not blow up the layout.
_MAX_REASON_CHARS = 160

#: Canon CIFF (the ``.crw`` raw format of the PowerShot era) signature: byte
#: order mark, 4-byte heap offset, then this literal at offset 6.
_CIFF_SIGNATURE = b"HEAPCCDR"


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def pillow_available() -> bool:
    """True when Pillow can actually decode images in this interpreter.

    Imports ``PIL.Image`` rather than the ``PIL`` package alone: a Pillow whose
    compiled ``_imaging`` extension is missing or mismatched imports as a
    package and then fails on the first real call, which would turn an optional
    enhancement into a crash mid-transfer.

    The result is deliberately **not** cached. The dependency panel can install
    Pillow while the app is running, and a cached ``False`` would keep deep
    verification switched off until the user restarted — exactly when they most
    want the stronger check.
    """
    try:
        from PIL import Image  # noqa: F401  (probe import, not used here)
    except Exception:
        # ImportError for "not installed", but a broken build can raise almost
        # anything at import time; either way Pillow is unusable.
        return False
    return True


def verify_download(
    local_path: str, expected_size: int = -1, deep: bool = True
) -> VerifyResult:
    """Decide whether the file at ``local_path`` is a complete, sound copy.

    :param local_path: Absolute path of the file just written to disk.
    :param expected_size: Byte count the camera reported for the source file,
        or ``-1`` when the transport could not report one (see
        :attr:`~retrocam.model.CameraFile.size`). A non-negative value is
        checked for an exact match — a short read is the single most common way
        a flaky 20-year-old USB link corrupts a transfer.
    :param deep: Attempt a full pixel decode when Pillow is importable. Set
        False for a fast structural pass.
    :returns: :class:`~retrocam.model.VerifyResult`. ``ok`` is True only when
        every applicable check passed.

    Never raises. Any error — unreadable file, vanished file, decoder blowing
    up — is converted into ``ok=False`` with an explanatory reason, because a
    verification step that throws would abort the transfer loop and lose the
    outcomes of files that were fine.
    """
    try:
        return _verify(local_path, expected_size, deep)
    except OSError as exc:
        return VerifyResult(False, "read error: {0}".format(_describe(exc)))
    except Exception as exc:  # noqa: BLE001 - deliberate last line of defence
        # Nothing below should raise anything else, but "cannot verify" must
        # always degrade to "not ok" rather than to a stack trace in the GUI.
        return VerifyResult(False, "verification failed: {0}".format(_describe(exc)))


# --------------------------------------------------------------------------- #
# Steps 1-3: existence, emptiness, exact size
# --------------------------------------------------------------------------- #


def _verify(local_path: str, expected_size: int, deep: bool) -> VerifyResult:
    """Body of :func:`verify_download`, wrapped by its exception guard."""
    if not local_path:
        # A DownloadOutcome with dest_path=None must never look verifiable.
        return VerifyResult(False, "no destination path was recorded")

    # ``expected_size`` is typed int, but a backend that forgot to fill in a
    # size would hand us None; treat that as "unknown" instead of crashing.
    if expected_size is None:
        expected_size = -1

    try:
        st = os.stat(local_path)
    except FileNotFoundError:
        return VerifyResult(
            False,
            "file not found: {0}".format(os.path.basename(local_path) or local_path),
        )

    if not stat_module.S_ISREG(st.st_mode):
        # Directory, FIFO, device node: whatever it is, it is not our photo.
        return VerifyResult(False, "not a regular file")

    size = st.st_size
    if size <= 0:
        return VerifyResult(False, "file is empty (0 bytes)")

    if expected_size >= 0 and size != expected_size:
        if size < expected_size:
            return VerifyResult(
                False, "truncated: {0} of {1} bytes".format(size, expected_size)
            )
        # Larger than announced is just as wrong: it means we appended to an
        # existing file, or the listing does not describe the file we read.
        return VerifyResult(
            False, "larger than expected: {0} of {1} bytes".format(size, expected_size)
        )

    # --- step 4: structure ------------------------------------------------- #
    head, tail = _read_edges(local_path, size)
    ext = os.path.splitext(local_path)[1].lower()
    struct_ok, note = _check_structure(ext, head, tail, size)
    if not struct_ok:
        return VerifyResult(False, note)

    # --- step 5: full decode ----------------------------------------------- #
    checked_decode = False
    if deep:
        decode_ok, decode_reason, checked_decode = _deep_decode(
            local_path, strict=ext in _STRICT_DECODE_EXTS
        )
        if not decode_ok:
            return VerifyResult(False, decode_reason)

    return VerifyResult(True, note, checked_decode=checked_decode)


def _read_edges(path: str, size: int) -> Tuple[bytes, bytes]:
    """Return ``(first bytes, last bytes)`` of the file in one open.

    Reading only the two ends keeps verification O(1) on a 4 GB card full of
    AVI clips; the expensive whole-file pass is Pillow's job and is opt-in.
    """
    window = min(size, _JPEG_TAIL_WINDOW)
    with open(path, "rb") as handle:
        head = handle.read(_HEAD_BYTES)
        # size > 0 and window <= size, so this seek is always in range.
        handle.seek(-window, os.SEEK_END)
        tail = handle.read(window)
    return head, tail


# --------------------------------------------------------------------------- #
# Step 4: format-aware structural checks
# --------------------------------------------------------------------------- #


def _check_structure(ext: str, head: bytes, tail: bytes, size: int) -> Tuple[bool, str]:
    """Dispatch on extension. Returns ``(ok, reason_or_note)``.

    The note is non-empty on success only when the caller deserves a caveat
    (padding found, or no structural knowledge for this extension).
    """
    if ext in _JPEG_EXTS:
        return _check_jpeg(head, tail)
    if ext in _TIFF_EXTS:
        return _check_tiff(ext, head)
    if ext == ".crw":
        return _check_crw(head)
    if ext == ".avi":
        return _check_riff(head, size, b"AVI ", "AVI")
    if ext == ".wav":
        return _check_riff(head, size, b"WAVE", "WAV")
    return (
        True,
        "unknown file type '{0}' - size checked, no structural check available".format(
            ext or "(no extension)"
        ),
    )


def _check_jpeg(head: bytes, tail: bytes) -> Tuple[bool, str]:
    """Verify the JPEG container: FFD8 at the start, FFD9 at (or near) the end.

    The end marker is located with ``rfind`` over the tail window rather than
    by looking at the final two bytes, because cameras and readers pad. To stop
    that leniency from accepting a truncated file whose *embedded EXIF
    thumbnail* happens to supply an FFD9, everything after the marker must be
    padding: real image data following an "end" marker means the marker was not
    the end of the file we are checking.
    """
    if not head.startswith(b"\xff\xd8"):
        return False, "not a JPEG: start marker FFD8 missing"

    marker = tail.rfind(b"\xff\xd9")
    if marker < 0:
        return False, "JPEG end marker FFD9 missing - the file is truncated"

    trailing = tail[marker + 2 :]
    if not trailing:
        return True, ""
    if set(trailing) <= _JPEG_PAD_BYTES:
        return (
            True,
            "JPEG complete, with {0} padding byte(s) after the FFD9 end marker".format(
                len(trailing)
            ),
        )
    return (
        False,
        "{0} unexpected byte(s) after the JPEG end marker - the file looks damaged".format(
            len(trailing)
        ),
    )


def _check_tiff(ext: str, head: bytes) -> Tuple[bool, str]:
    """Verify TIFF magic, plus the ``CR`` signature that marks a Canon CR2."""
    if not head.startswith(_TIFF_MAGICS):
        return False, "not a TIFF/CR2 file: TIFF magic missing at offset 0"
    if ext == ".cr2" and head[8:10] != b"CR":
        # Every real CR2 carries this at offset 8; without it we are looking at
        # a plain TIFF wearing the wrong extension, or at damaged bytes.
        return False, "not a Canon CR2 file: 'CR' signature missing at offset 8"
    return True, ""


def _check_crw(head: bytes) -> Tuple[bool, str]:
    """Verify a Canon CIFF ``.crw`` header (the PowerShot-era raw container).

    A plain TIFF header is also accepted: a handful of tools rewrite raws in
    place and keep the ``.crw`` name, and rejecting those would block deletion
    of files that are in fact intact.
    """
    if head[0:2] in (b"II", b"MM") and head[6:14] == _CIFF_SIGNATURE:
        return True, ""
    if head.startswith(_TIFF_MAGICS):
        return True, "CRW carries a TIFF header rather than the CIFF signature"
    return False, "not a Canon CRW file: CIFF signature missing"


def _check_riff(head: bytes, size: int, form: bytes, label: str) -> Tuple[bool, str]:
    """Verify a RIFF container and cross-check its declared length.

    The 32-bit size field at offset 4 counts every byte after itself, so a
    complete file is exactly ``declared + 8`` bytes. That makes RIFF the one
    format here that detects truncation on its own, without the camera having
    told us the expected size.
    """
    if len(head) < 12 or head[0:4] != b"RIFF" or head[8:12] != form:
        return False, "not a valid {0} file: RIFF/{1} header missing".format(
            label, form.decode("ascii", "replace").strip()
        )

    declared = int.from_bytes(head[4:8], "little")
    if declared <= 0:
        # Some capture tools leave the field at zero and never patch it up.
        # Absence of a length is not evidence of damage, so pass with a caveat.
        return True, "{0} header declares no length - size not cross-checked".format(
            label
        )

    total = declared + 8
    if size < total:
        return False, "{0} truncated: header declares {1} bytes, file has {2}".format(
            label, total, size
        )
    if size > total:
        return (
            True,
            "{0} complete, with {1} extra byte(s) after the RIFF chunk".format(
                label, size - total
            ),
        )
    return True, ""


# --------------------------------------------------------------------------- #
# Step 5: optional full decode through Pillow
# --------------------------------------------------------------------------- #


def _deep_decode(path: str, strict: bool) -> Tuple[bool, str, bool]:
    """Force a complete decode of the image. Returns ``(ok, reason, decoded)``.

    :param strict: True when the extension names a format Pillow is guaranteed
        to support, which makes any failure evidence that the file is damaged.
        When False a failure only means "could not confirm": ``ok`` is left
        alone and ``decoded`` stays False.

    ``decoded`` reports whether a real pixel decode actually happened, so the
    UI can distinguish "verified to the last pixel" from "structure looks
    right".

    Note that a corrupt *header* makes Pillow raise ``UnidentifiedImageError``
    — the same exception it raises for a format it simply has no codec for.
    The two are indistinguishable from the exception alone, which is precisely
    why the caller decides by extension instead of by exception type: treating
    every ``UnidentifiedImageError`` as "unsupported format" would wave a
    header-corrupted JPEG straight through the delete gate.
    """
    try:
        from PIL import Image, ImageFile
    except Exception:
        return True, "", False

    # WHY: any module in the process (or a plugin) can flip this global to True
    # to make Pillow tolerate truncated files. That is the correct setting for
    # a viewer and a catastrophic one for us — it would decode a half-written
    # photo without complaint and green-light its deletion from the card.
    previous_truncated = ImageFile.LOAD_TRUNCATED_IMAGES
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    try:
        with warnings.catch_warnings():
            # Pillow warns about oversized images and odd EXIF; a warnings
            # filter set to "error" elsewhere would otherwise turn a healthy
            # photo into a verification failure.
            warnings.simplefilter("ignore")
            try:
                # verify() checks the container cheaply but leaves the file
                # unusable afterwards, so load() needs a fresh handle. Running
                # both catches damage that either one alone would miss.
                with Image.open(path) as image:
                    image.verify()
                with Image.open(path) as image:
                    image.load()
            except Exception as exc:  # noqa: BLE001 - decoders raise many types
                if not strict:
                    return True, "", False
                return (
                    False,
                    "image decode failed: {0}".format(_describe(exc, path)),
                    False,
                )
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = previous_truncated

    return True, "", True


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _describe(exc: BaseException, path: str = "") -> str:
    """One short human-readable line for an exception, never empty.

    Reasons are rendered next to the file name in a table cell, so ``path`` (when
    given) is collapsed to its base name: Pillow embeds the full absolute path in
    its messages, and repeating a 90-character temp path beside the file it
    refers to pushes the actual cause out of view.
    """
    strerror: Optional[str] = getattr(exc, "strerror", None)
    message = str(strerror) if strerror else str(exc).strip()
    if not message:
        message = type(exc).__name__
    if path:
        message = message.replace(path, os.path.basename(path))
    if len(message) > _MAX_REASON_CHARS:
        # ASCII on purpose: reasons are also written to a plain log file, which
        # on a Windows console may not be UTF-8.
        message = message[: _MAX_REASON_CHARS - 3] + "..."
    return message
