"""Tests for :mod:`retrocam.verify`, the module the delete gate trusts.

Run with::

    python3 -m unittest discover -s tests

Stdlib only: no pytest, and no Pillow required. The structural cases below use
hand-built byte sequences and ``deep=False`` so they assert exactly one thing —
the container check — on every machine, with or without Pillow installed. The
decode cases are gated on :func:`retrocam.verify.pillow_available` and build
their fixtures with Pillow itself, so the "valid" file really is decodable
rather than merely plausible.

The asymmetry that matters is asserted throughout: a file this module calls
``ok`` may be deleted from a 20-year-old card, so the interesting assertions
are the *negative* ones.
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import unittest
from typing import Tuple

# The package lives in src/ and is not installed while the suite runs from a
# checkout, so put it on the path before importing. Derived from this file's
# location rather than the cwd, so discovery works from anywhere.
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.join(os.path.dirname(_TESTS_DIR), "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from retrocam.model import VerifyResult  # noqa: E402
from retrocam.verify import pillow_available, verify_download  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixture builders
# --------------------------------------------------------------------------- #

#: A structurally valid baseline JPEG skeleton: SOI, a JFIF APP0 segment, some
#: filler standing in for the entropy-coded scan, and EOI. It satisfies every
#: container rule without being decodable, which is why it is only ever used
#: with deep=False.
_JPEG_SOI = b"\xff\xd8"
_JPEG_APP0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
_JPEG_EOI = b"\xff\xd9"


def _jpeg_bytes(payload: bytes = b"\x00" * 256, eoi: bool = True) -> bytes:
    """Build a JPEG-shaped blob, optionally without its end marker."""
    data = _JPEG_SOI + _JPEG_APP0 + payload
    return data + _JPEG_EOI if eoi else data


def _riff_bytes(form: bytes, payload: bytes) -> bytes:
    """Build a RIFF container whose declared length matches its real length."""
    body = form + payload
    return b"RIFF" + struct.pack("<I", len(body)) + body


class _TempTree(unittest.TestCase):
    """Base class giving each test an isolated, self-cleaning directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="retrocam-verify-")
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = self._tmp.name

    def write(self, name: str, data: bytes) -> str:
        """Write ``data`` to ``name`` inside the temp dir, return the path."""
        path = os.path.join(self.tmpdir, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def read(self, path: str) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()

    # Short aliases used by the Pillow-gated tests, which build a genuinely
    # decodable photo and then damage it in a specific way.
    _read = read

    def _real_jpeg(self, name: str, size: Tuple[int, int] = (240, 180)) -> str:
        """Write a real, decodable JPEG. Requires Pillow (callers are gated)."""
        from PIL import Image

        path = os.path.join(self.tmpdir, name)
        Image.new("RGB", size, (10, 120, 200)).save(path, "JPEG")
        return path

    def assertNotOk(self, result: VerifyResult, contains: str = "") -> None:
        """A rejection must always explain itself, or the GUI shows a blank."""
        self.assertFalse(
            result.ok, "expected rejection, got ok with %r" % (result.reason,)
        )
        self.assertTrue(result.reason.strip(), "a rejection must carry a reason")
        if contains:
            self.assertIn(contains.lower(), result.reason.lower())


# --------------------------------------------------------------------------- #
# Steps 1-3: existence, emptiness, size
# --------------------------------------------------------------------------- #


class TestFileLevelChecks(_TempTree):
    def test_missing_file_is_rejected(self) -> None:
        missing = os.path.join(self.tmpdir, "IMG_1870.JPG")
        self.assertNotOk(verify_download(missing), "not found")

    def test_empty_path_is_rejected(self) -> None:
        # DownloadOutcome.dest_path is None when a transfer failed; whatever the
        # caller passes through must never verify as good.
        self.assertNotOk(verify_download(""))

    def test_zero_byte_file_is_rejected(self) -> None:
        path = self.write("IMG_1871.JPG", b"")
        self.assertNotOk(verify_download(path, deep=False), "empty")

    def test_directory_is_not_a_regular_file(self) -> None:
        sub = os.path.join(self.tmpdir, "118CANON")
        os.mkdir(sub)
        self.assertNotOk(verify_download(sub), "regular file")

    def test_size_mismatch_reports_both_numbers(self) -> None:
        data = _jpeg_bytes(b"\x11" * 512)
        path = self.write("IMG_1872.JPG", data)
        expected = len(data) + 4096

        result = verify_download(path, expected_size=expected, deep=False)

        self.assertNotOk(result, "truncated")
        self.assertIn(str(len(data)), result.reason)
        self.assertIn(str(expected), result.reason)

    def test_larger_than_expected_is_rejected(self) -> None:
        # Extra bytes mean we appended to something, or the listing does not
        # describe the file we just read. Either way it is not a clean copy.
        data = _jpeg_bytes(b"\x22" * 512)
        path = self.write("IMG_1873.JPG", data)

        result = verify_download(path, expected_size=len(data) - 100, deep=False)

        self.assertNotOk(result)
        self.assertIn(str(len(data)), result.reason)

    def test_exact_expected_size_passes(self) -> None:
        data = _jpeg_bytes()
        path = self.write("IMG_1874.JPG", data)

        result = verify_download(path, expected_size=len(data), deep=False)

        self.assertTrue(result.ok, result.reason)

    def test_unknown_expected_size_is_not_checked(self) -> None:
        data = _jpeg_bytes()
        path = self.write("IMG_1875.JPG", data)

        # -1 means "the backend could not report a size"; that must not be
        # compared against anything.
        self.assertTrue(verify_download(path, expected_size=-1, deep=False).ok)


# --------------------------------------------------------------------------- #
# Step 4: JPEG structure
# --------------------------------------------------------------------------- #


class TestJpegStructure(_TempTree):
    def test_valid_jpeg_passes(self) -> None:
        path = self.write("IMG_1880.JPG", _jpeg_bytes())

        result = verify_download(path, deep=False)

        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.reason, "")
        self.assertFalse(result.checked_decode)

    def test_jpeg_extension_case_is_ignored(self) -> None:
        # Cameras write upper-case names; the check is on the format, not the
        # spelling.
        path = self.write("IMG_1881.jpeg", _jpeg_bytes())
        self.assertTrue(verify_download(path, deep=False).ok)

    def test_thm_thumbnail_is_treated_as_jpeg(self) -> None:
        path = self.write("MVI_1882.THM", _jpeg_bytes())
        self.assertTrue(verify_download(path, deep=False).ok)
        self.assertNotOk(
            verify_download(
                self.write("MVI_1883.THM", _jpeg_bytes(eoi=False)), deep=False
            ),
            "ffd9",
        )

    def test_truncated_jpeg_without_end_marker_is_rejected(self) -> None:
        path = self.write("IMG_1884.JPG", _jpeg_bytes(b"\x33" * 4096, eoi=False))
        self.assertNotOk(verify_download(path, deep=False), "ffd9")

    def test_jpeg_without_start_marker_is_rejected(self) -> None:
        path = self.write("IMG_1885.JPG", b"\x00\x00" + _jpeg_bytes()[2:])
        self.assertNotOk(verify_download(path, deep=False), "ffd8")

    def test_trailing_null_padding_is_accepted_but_reported(self) -> None:
        path = self.write("IMG_1886.JPG", _jpeg_bytes() + b"\x00" * 40)

        result = verify_download(path, deep=False)

        self.assertTrue(result.ok, result.reason)
        self.assertTrue(result.reason.strip(), "padding must be reported to the user")
        self.assertIn("padding", result.reason.lower())

    def test_real_data_after_end_marker_is_rejected(self) -> None:
        # This is the trap an over-lenient FFD9 search falls into: a truncated
        # photo whose embedded EXIF thumbnail supplies an end marker, followed
        # by the surviving head of the real scan data. Accepting it would erase
        # a half-copied photo from the card.
        thumbnail = _jpeg_bytes(b"\x44" * 64)
        path = self.write("IMG_1887.JPG", thumbnail + b"\x55" * 2048)

        self.assertNotOk(verify_download(path, deep=False))

    def test_end_marker_beyond_the_tail_window_is_rejected(self) -> None:
        # Padding larger than the scan window means we cannot prove where the
        # image ends, so the answer is "no" rather than a guess.
        path = self.write("IMG_1888.JPG", _jpeg_bytes() + b"\x00" * (128 * 1024))
        self.assertNotOk(verify_download(path, deep=False))


# --------------------------------------------------------------------------- #
# Step 4: raw, TIFF and RIFF containers
# --------------------------------------------------------------------------- #


class TestOtherContainers(_TempTree):
    def test_valid_avi_passes(self) -> None:
        path = self.write("MVI_1890.AVI", _riff_bytes(b"AVI ", b"LIST" + b"\x00" * 512))

        result = verify_download(path, deep=False)

        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.reason, "")

    def test_avi_with_wrong_form_is_rejected(self) -> None:
        path = self.write("MVI_1891.AVI", _riff_bytes(b"WAVE", b"\x00" * 64))
        self.assertNotOk(verify_download(path, deep=False), "avi")

    def test_avi_shorter_than_its_riff_header_is_rejected(self) -> None:
        # RIFF is the one container that detects its own truncation, even when
        # the camera never told us the expected size.
        data = _riff_bytes(b"AVI ", b"\x00" * 1024)
        path = self.write("MVI_1892.AVI", data[:-256])
        self.assertNotOk(verify_download(path, deep=False), "truncated")

    def test_valid_wav_passes(self) -> None:
        path = self.write("SND_1893.WAV", _riff_bytes(b"WAVE", b"fmt " + b"\x00" * 32))
        self.assertTrue(verify_download(path, deep=False).ok)

    def test_non_riff_file_with_avi_extension_is_rejected(self) -> None:
        path = self.write("MVI_1894.AVI", b"\x00" * 4096)
        self.assertNotOk(verify_download(path, deep=False), "riff")

    def test_valid_crw_passes(self) -> None:
        # Canon CIFF header: byte order, heap offset, then HEAPCCDR.
        data = b"II" + struct.pack("<I", 0x1A) + b"HEAPCCDR" + b"\x00" * 1024
        path = self.write("CRW_1895.CRW", data)
        self.assertTrue(verify_download(path, deep=False).ok)

    def test_crw_without_ciff_signature_is_rejected(self) -> None:
        path = self.write("CRW_1896.CRW", b"II" + b"\x00" * 1024)
        self.assertNotOk(verify_download(path, deep=False), "ciff")

    def test_valid_cr2_passes(self) -> None:
        data = b"II\x2a\x00" + struct.pack("<I", 0x10) + b"CR\x02\x00" + b"\x00" * 512
        path = self.write("IMG_1897.CR2", data)
        self.assertTrue(verify_download(path, deep=False).ok)

    def test_cr2_without_canon_signature_is_rejected(self) -> None:
        data = b"II\x2a\x00" + struct.pack("<I", 0x10) + b"\x00" * 512
        path = self.write("IMG_1898.CR2", data)
        self.assertNotOk(verify_download(path, deep=False), "cr2")

    def test_tiff_magic_is_checked(self) -> None:
        good = b"MM\x00\x2a" + b"\x00" * 512
        self.assertTrue(verify_download(self.write("a.tif", good), deep=False).ok)
        self.assertNotOk(
            verify_download(self.write("b.tif", b"XX" + b"\x00" * 512), deep=False),
            "tiff",
        )


class TestUnknownExtensions(_TempTree):
    def test_unknown_extension_passes_with_a_caveat(self) -> None:
        path = self.write("CANON.MRK", b"\x01\x02\x03\x04" * 64)

        result = verify_download(path, deep=False)

        self.assertTrue(result.ok, result.reason)
        self.assertTrue(
            result.reason.strip(),
            "the user must be told no structural check was possible",
        )
        self.assertIn("unknown", result.reason.lower())

    def test_file_without_extension_is_still_size_checked(self) -> None:
        path = self.write("NOEXT", b"\x00" * 32)

        self.assertTrue(verify_download(path, deep=False).ok)
        self.assertNotOk(
            verify_download(path, expected_size=999, deep=False), "truncated"
        )


# --------------------------------------------------------------------------- #
# Step 5: optional deep decode
# --------------------------------------------------------------------------- #


class TestDeepDecode(_TempTree):
    def test_pillow_available_returns_a_bool(self) -> None:
        self.assertIsInstance(pillow_available(), bool)

    @unittest.skipIf(pillow_available(), "Pillow is installed")
    def test_without_pillow_deep_falls_back_to_structure(self) -> None:
        # The whole app must work on a bare Python: deep=True degrades to the
        # structural verdict instead of failing or raising.
        path = self.write("IMG_1900.JPG", _jpeg_bytes())

        result = verify_download(path, deep=True)

        self.assertTrue(result.ok, result.reason)
        self.assertFalse(result.checked_decode)

    @unittest.skipUnless(pillow_available(), "Pillow is not installed")
    def test_with_pillow_a_real_photo_is_fully_decoded(self) -> None:
        from PIL import Image

        path = os.path.join(self.tmpdir, "IMG_1901.JPG")
        Image.new("RGB", (32, 24), (200, 40, 40)).save(path, "JPEG")

        result = verify_download(path, deep=True)

        self.assertTrue(result.ok, result.reason)
        self.assertTrue(result.checked_decode)

    @unittest.skipUnless(pillow_available(), "Pillow is not installed")
    def test_with_pillow_a_truncated_photo_is_rejected(self) -> None:
        # What a short read over a flaky USB 1.1 link actually leaves on disk,
        # asserted against a genuine JPEG rather than a synthetic one. The
        # missing FFD9 means the structural layer rejects it before the decoder
        # is reached — deep=True must not rescue it on the way past.
        path = self._real_jpeg("IMG_1902.JPG")
        data = self._read(path)
        self.write("IMG_1902.JPG", data[: len(data) // 2])

        result = verify_download(path, deep=True)

        self.assertNotOk(result)
        self.assertFalse(result.checked_decode)

    @unittest.skipUnless(pillow_available(), "Pillow is not installed")
    def test_corrupt_jpeg_that_passes_the_structural_check_is_rejected(self) -> None:
        # The case that justifies the whole deep layer: SOI and EOI are intact
        # and the byte count is whatever we claim it is, so steps 1-4 see a
        # healthy file. Only a decoder notices the header is shredded.
        #
        # Pillow reports this as UnidentifiedImageError — the very same
        # exception it raises for a format it has no codec for. An earlier
        # revision of verify.py treated that exception as "unsupported format"
        # and returned ok=True, i.e. it offered a wrecked photo up for
        # deletion from the card. This test exists to keep that from returning.
        path = self._real_jpeg("IMG_1906.JPG")
        data = self._read(path)
        damaged = data[:2] + b"\x5a" * 38 + data[40:]
        self.write("IMG_1906.JPG", damaged)

        self.assertTrue(
            damaged.startswith(b"\xff\xd8") and damaged.endswith(b"\xff\xd9")
        )
        self.assertTrue(
            verify_download(path, deep=False).ok,
            "precondition: the structural check alone must not catch this",
        )
        self.assertNotOk(verify_download(path, deep=True))

    @unittest.skipUnless(pillow_available(), "Pillow is not installed")
    def test_size_check_catches_the_truncation_the_decoder_cannot(self) -> None:
        # Documented limit of step 5: libjpeg treats FFD9 as a clean end of
        # image, so a file cut in half and re-terminated with FFD9 decodes
        # without error and the missing rows are silently filled in. No decoder
        # catches that, which is why the exact size from the camera listing is
        # the primary truncation defence and deep decode is complementary.
        path = self._real_jpeg("IMG_1907.JPG")
        data = self._read(path)
        self.write("IMG_1907.JPG", data[: len(data) // 2] + b"\xff\xd9")

        self.assertNotOk(
            verify_download(path, expected_size=len(data), deep=True), "truncated"
        )

    @unittest.skipUnless(pillow_available(), "Pillow is not installed")
    def test_undecodable_tiff_is_not_treated_as_damaged(self) -> None:
        # A CR2 whose main image uses Canon's lossless JPEG, or any TIFF
        # compression Pillow lacks, must not be branded corrupt: it would make
        # every raw on the card undeletable and push the user toward erasing
        # with no verification at all.
        path = self.write("IMG_1908.TIF", b"MM\x00\x2a" + b"\x00" * 512)

        result = verify_download(path, deep=True)

        self.assertTrue(result.ok, result.reason)
        self.assertFalse(result.checked_decode)

    @unittest.skipUnless(pillow_available(), "Pillow is not installed")
    def test_formats_pillow_cannot_read_are_not_failed(self) -> None:
        # Pillow has no CRW or AVI codec. "I cannot read this" is not evidence
        # of damage, and treating it as such would make every movie clip on the
        # card permanently undeletable.
        crw = b"II" + struct.pack("<I", 0x1A) + b"HEAPCCDR" + b"\x00" * 1024
        crw_result = verify_download(self.write("CRW_1903.CRW", crw), deep=True)
        avi_result = verify_download(
            self.write("MVI_1904.AVI", _riff_bytes(b"AVI ", b"\x00" * 256)), deep=True
        )

        self.assertTrue(crw_result.ok, crw_result.reason)
        self.assertFalse(crw_result.checked_decode)
        self.assertTrue(avi_result.ok, avi_result.reason)
        self.assertFalse(avi_result.checked_decode)

    def test_deep_false_never_sets_checked_decode(self) -> None:
        path = self.write("IMG_1905.JPG", _jpeg_bytes())
        self.assertFalse(verify_download(path, deep=False).checked_decode)


# --------------------------------------------------------------------------- #
# Contract-level guarantees
# --------------------------------------------------------------------------- #


class TestNeverRaises(_TempTree):
    def test_every_hostile_input_returns_a_result(self) -> None:
        # verify_download() is called inside the transfer loop; if it ever
        # raised, the outcomes of files that were fine would be lost with it.
        candidates = [
            "",
            os.path.join(self.tmpdir, "does-not-exist.JPG"),
            self.tmpdir,
            self.write("weirdé.JPG", b"\xff\xd8"),
            self.write("one-byte.JPG", b"\xff"),
            self.write("no-ext-empty", b""),
        ]
        for path in candidates:
            with self.subTest(path=path):
                for deep in (False, True):
                    result = verify_download(path, expected_size=-1, deep=deep)
                    self.assertIsInstance(result, VerifyResult)
                    if not result.ok:
                        self.assertTrue(result.reason.strip())

    def test_result_is_the_shared_dataclass(self) -> None:
        # Other modules pass this straight into DownloadOutcome.verify, so the
        # identity of the type matters, not just its shape.
        path = self.write("IMG_1910.JPG", _jpeg_bytes())
        self.assertIsInstance(verify_download(path, deep=False), VerifyResult)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
