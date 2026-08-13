"""Backend that drives the ``gphoto2`` command-line tool.

This is the only transport that can reach a pre-2003 Canon compact over USB.
Cameras like the PowerShot S30 predate both PTP and USB Mass Storage: they speak
Canon's proprietary protocol, which is implemented by libgphoto2's ``canon``
camlib and by nothing else that ships on a modern desktop. If a user has one of
these bodies and no CompactFlash reader, this backend is the whole product.

Design notes (all of them are consequences of gphoto2's actual behaviour, which
was measured on macOS with gphoto2 2.5.32 / libgphoto2 2.5.34):

*Every* invocation passes both ``--camera`` and ``--port``. gphoto2 persists the
last used model/port into a settings file (``~/.gphoto/settings``, or
``~/Library/Application Support/gphoto/settings`` on macOS) and silently reuses
them when the flags are omitted — a bare ``gphoto2 -L`` will happily talk to
whatever camera was addressed last. With two cameras plugged in, an omitted
``--port`` means gphoto2 quietly picks the first one it enumerates. Since this
program deletes photos, addressing the wrong device is the worst bug we could
ship, so the flags are never optional here.

Everything runs with ``LC_ALL=C``: gphoto2's human-readable output — including
its error text and the ``--auto-detect`` header — is gettext-localized, so a
user with an Italian system would otherwise defeat every parser below. Only the
integer libgphoto2 error code is stable across locales, and that is what we map.

Sizes come from ``--parsable -L``, never from plain ``-L``: the human listing
rounds up to whole KB, which cannot be compared against a byte count. A rounded
size is worse than no size at all, because verification would reject every good
file, so the human parser (kept as a fallback for old gphoto2 builds) reports
``size=-1`` and stashes the rounded KB in ``raw['kb']`` for display only.

Translation keys used by this module (they must exist in ``i18n.py``):
``gphoto2.missing_macos``, ``gphoto2.missing_linux``, ``gphoto2.missing_generic``,
``gphoto2.unavailable_windows``, ``gphoto2.install_hint_windows``,
``gphoto2.broken_binary``, ``gphoto2.released_ptp``, ``gphoto2.detecting``,
``gphoto2.detected_none``, ``gphoto2.detected_one``, ``gphoto2.listing``,
``gphoto2.listed``, ``gphoto2.downloading``, ``gphoto2.skipped_existing``,
``gphoto2.deleting``, ``gphoto2.delete_confirming``, ``gphoto2.timeout``,
``gphoto2.cancelled``, ``gphoto2.spawn_failed``, ``gphoto2.dest_unwritable``,
``gphoto2.no_output``, ``gphoto2.empty_file``, ``gphoto2.size_mismatch``,
``gphoto2.replace_failed``, ``gphoto2.still_present``, ``gphoto2.err_claim``,
``gphoto2.err_unplugged``, ``gphoto2.err_no_camera``, ``gphoto2.err_io``,
``gphoto2.err_port_timeout``, ``gphoto2.err_os``, ``gphoto2.err_camera_op``,
``gphoto2.err_file_not_found``, ``gphoto2.err_dir_not_found``,
``gphoto2.err_no_space``, ``gphoto2.err_busy``, ``gphoto2.err_corrupt``,
``gphoto2.err_bad_params``, ``gphoto2.err_unknown_port``,
``gphoto2.err_unsupported``, ``gphoto2.err_generic``.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..model import (
    BackendKind,
    CameraError,
    CameraFile,
    CameraInfo,
    CameraNotFound,
    CancelToken,
    DeleteOutcome,
    DownloadOutcome,
    Progress,
    ProgressCallback,
    TransferAborted,
)
from .base import Availability, CameraBackend, noop_progress

try:  # pragma: no cover - exercised implicitly by every real run
    from ..i18n import t
except Exception:  # pragma: no cover - safety net, see below

    def t(key: str, **kw: Any) -> str:
        """Emergency fallback so a broken i18n import cannot kill the backend.

        Losing translations is annoying; losing the only transport that can read
        a 20-year-old Canon is not acceptable. The key plus its parameters is
        still readable enough to act on.
        """
        detail = " ".join(str(v) for v in kw.values() if str(v))
        return "{0}: {1}".format(key, detail) if detail else key


try:  # POSIX only; used for the stronger macOS flush-to-platter fsync.
    import fcntl
except Exception:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # select-based pipe polling is POSIX only (see _run_streaming).
    import selectors
except Exception:  # pragma: no cover - should never happen on a supported build
    selectors = None  # type: ignore[assignment]

__all__ = ["GPhoto2Backend"]


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_IS_POSIX = os.name == "posix"
_IS_MACOS = sys.platform == "darwin"
_IS_WINDOWS = sys.platform.startswith("win")

#: Seconds allowed for the trivial, device-free probes.
_VERSION_TIMEOUT = 8.0
#: ``--auto-detect`` walks the USB bus and can take a while with hubs attached.
_DETECT_TIMEOUT = 60.0
#: A full recursive listing of a 1 GB card over USB 1.1.
_LIST_TIMEOUT = 240.0
#: One ``--delete-file`` round trip.
_DELETE_TIMEOUT = 90.0
#: Fixed part of the per-file download budget (process spawn + USB handshake).
_DOWNLOAD_TIMEOUT_BASE = 45.0
#: An S30 sustains roughly 0.5 MB/s; 50 kB/s gives a 10x safety margin.
_DOWNLOAD_BYTES_PER_SECOND = 50_000.0
#: Never wait longer than this for a single file, however large.
_DOWNLOAD_TIMEOUT_CAP = 1800.0
#: Don't respawn ``killall`` for every file in a 300-photo batch.
_RELEASE_THROTTLE_SECONDS = 5.0
#: Grace period between SIGINT and SIGKILL when cancelling or timing out.
_KILL_GRACE_SECONDS = 2.0

#: Ports look like ``usb:001,004``. Anchoring on the ``scheme:`` prefix at the
#: end of the line is what lets us split a model name that contains spaces.
_PORT_RE = re.compile(
    r"(?P<port>(?:usb|usbscsi|usbdiskdirect|ptpip|ip|serial|disk):\S*)\s*$"
)

#: ``--parsable -L`` output. The four trailing fields are whitespace-free, so a
#: greedy path capture is unambiguous even for names containing a quote.
_PARSABLE_RE = re.compile(
    r"^FILENAME='(?P<path>.*)' "
    r"PERMS=(?P<perms>\S*) "
    r"FILESIZE=(?P<size>\d+) "
    r"FILETYPE=(?P<mime>\S*) "
    r"FILEMTIME=(?P<mtime>-?\d+)\s*$"
)

#: Human ``-L`` header: "There are 31 files in folder '/DCIM/118CANON'." and
#: "There is no file in folder '/DCIM'." Only parsed under LC_ALL=C.
_HUMAN_FOLDER_RE = re.compile(r"\bin folder '(?P<folder>.*)'\.?\s*$")

#: Human ``-L`` entry: "#1     IMG_1870.JPG      rd   851 KB image/jpeg 1786084632"
_HUMAN_ENTRY_RE = re.compile(
    r"^#(?P<num>\d+)\s+"
    r"(?P<name>\S.*?)\s+"
    r"(?P<perms>[r-][d-])\s+"
    r"(?P<kb>\d+)\s+KB\s+"
    r"(?P<mime>\S+)"
    r"(?:\s+(?P<mtime>-?\d+))?\s*$"
)

#: A pure decoration line such as "*** Error ***" (locale-independent: the
#: informative variant always carries the "(code: 'text')" parentheses).
_BANNER_DECORATION_RE = re.compile(r"^\*{2,}[^()]*\*{2,}$")

#: Final line of gphoto2's error banner: "*** Error (-53: 'Could not ...') ***".
#: The integer is the only locale-stable part of the whole message.
_GP_CODE_RE = re.compile(r"\*\*\*[^(]*\((-?\d+)\s*:")

#: gphoto2's progress bar: "... | 45.6% 12s\r". Percent only, advisory only.
_PCT_RE = re.compile(rb"(\d{1,3}\.\d)\s*%")

#: libgphoto2 error code -> (translation key, exception class to raise).
#: Codes are stable across versions and locales; the message text is not.
_ERROR_MAP = {
    -53: ("gphoto2.err_claim", CameraError),
    -52: ("gphoto2.err_unplugged", CameraNotFound),
    -105: ("gphoto2.err_no_camera", CameraNotFound),
    -7: ("gphoto2.err_io", CameraError),
    -10: ("gphoto2.err_port_timeout", CameraError),
    -114: ("gphoto2.err_os", CameraError),
    -113: ("gphoto2.err_camera_op", CameraError),
    -108: ("gphoto2.err_file_not_found", CameraError),
    -107: ("gphoto2.err_dir_not_found", CameraError),
    -115: ("gphoto2.err_no_space", CameraError),
    -110: ("gphoto2.err_busy", CameraError),
    -102: ("gphoto2.err_corrupt", CameraError),
    -2: ("gphoto2.err_bad_params", CameraError),
    -5: ("gphoto2.err_unknown_port", CameraError),
    -6: ("gphoto2.err_unsupported", CameraError),
    -1: ("gphoto2.err_generic", CameraError),
}

#: Emitted verbatim by libgphoto2_port when another process owns the device.
_CLAIM_MARKERS = ("could not claim", "resource busy")
#: Emitted when the named model is not on the bus at all.
_NO_CAMERA_MARKERS = ("no camera found", "could not detect any camera")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _decode(raw: bytes) -> str:
    """Decode subprocess output without ever raising on odd bytes."""
    return raw.decode("utf-8", errors="replace")


def _tail(text: str, max_lines: int = 3, max_chars: int = 400) -> str:
    """Last few meaningful lines of stderr, condensed for a one-line UI slot.

    gphoto2 frames its errors with a decoration line ("*** Error ***") that
    carries no information; dropping it leaves room for the lines that do. The
    final "*** Error (-53: '...') ***" line is kept, because the code in it is
    the one piece of the banner that survives translation.
    """
    lines = [ln.strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln and not _BANNER_DECORATION_RE.match(ln)]
    picked = " / ".join(lines[-max_lines:])
    return picked[:max_chars]


def _gp_error_code(stderr: str) -> Optional[int]:
    """Extract the libgphoto2 error code from an error banner, if present.

    The last match wins: the banner prints zero or more detail blocks and then
    one final ``(code: 'text')`` line, which is the code that actually caused
    the non-zero exit.
    """
    code = None
    for match in _GP_CODE_RE.finditer(stderr or ""):
        try:
            code = int(match.group(1))
        except ValueError:  # pragma: no cover - regex guarantees digits
            continue
    return code


def _looks_like_claim_conflict(stderr: str) -> bool:
    """True when the failure is 'something else already owns the USB device'."""
    if _gp_error_code(stderr) == -53:
        return True
    low = (stderr or "").lower()
    return any(marker in low for marker in _CLAIM_MARKERS)


def _normalise_device_path(path: str) -> Tuple[str, str]:
    """Split an absolute camera path into (folder, name), collapsing slashes.

    gphoto2 emits a doubled separator when a folder argument carried a trailing
    slash (``/DCIM/118CANON//IMG_0001.JPG``); left alone that would produce a
    ``CameraFile.path`` that no longer matches the one used for deletion.
    """
    cleaned = re.sub(r"/{2,}", "/", (path or "").strip())
    if not cleaned.startswith("/"):
        cleaned = "/" + cleaned
    folder, _, name = cleaned.rpartition("/")
    return (folder or "/"), name


def _download_timeout(size: int) -> float:
    """Per-file timeout. gphoto2 has no ``--timeout``; a dying camera hangs."""
    budget = _DOWNLOAD_TIMEOUT_BASE
    if size > 0:
        budget += size / _DOWNLOAD_BYTES_PER_SECOND
    return min(budget, _DOWNLOAD_TIMEOUT_CAP)


def _fsync_path(path: str) -> None:
    """Flush a finished download to stable storage before renaming it.

    Best effort by design: on a filesystem that refuses fsync we would rather
    keep the (verified) file than fail the transfer.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        # F_FULLFSYNC is the only call on macOS that reaches the platter;
        # plain fsync() there merely hands the data to the drive cache.
        if fcntl is not None and hasattr(fcntl, "F_FULLFSYNC"):
            try:
                fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
                return
            except OSError:
                pass
        os.fsync(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _fsync_dir(path: str) -> None:
    """Persist a rename in the directory entry itself (POSIX only)."""
    if not _IS_POSIX:
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _unlink_quietly(path: str) -> None:
    """Remove a temporary file, ignoring the fact that it may not exist."""
    try:
        os.unlink(path)
    except OSError:
        pass


class _PercentParser:
    """Turns gphoto2's ASCII progress bar into percentage callbacks.

    gphoto2 buffers an entire file in memory and flushes it only once the
    transfer has finished, so neither the growing destination file nor the byte
    count on a ``--stdout`` pipe reveals anything about progress. Its own
    redrawn progress bar is the single available signal, and it is advisory:
    correctness always comes from the exit code plus the size check.
    """

    def __init__(self, emit: Callable[[float], None]) -> None:
        self._buf = b""
        self._last_whole = -1
        self._emit = emit

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        # Bar updates terminate with \r and never \n, so segment on both.
        parts = re.split(rb"[\r\n]", self._buf)
        self._buf = parts.pop()
        if len(self._buf) > 4096:
            # A stream with no separators must not grow the buffer forever.
            self._buf = self._buf[-4096:]
        for part in parts:
            last = None
            for last in _PCT_RE.finditer(part):
                pass
            if last is None:
                continue
            try:
                pct = float(last.group(1))
            except ValueError:  # pragma: no cover - regex guarantees a float
                continue
            pct = min(100.0, max(0.0, pct))
            whole = int(pct)
            # One tick per whole percent: a large transfer would otherwise put
            # thousands of Progress objects on the GUI queue.
            if whole != self._last_whole:
                self._last_whole = whole
                self._emit(pct)


# --------------------------------------------------------------------------- #
# The backend
# --------------------------------------------------------------------------- #


class GPhoto2Backend(CameraBackend):
    """Talks to a camera through the ``gphoto2`` CLI.

    Chosen automatically for cameras that are neither a mounted card nor a WIA
    device: on a Canon PowerShot S30 this is the only code path that works at
    all. Reading is done with one subprocess per file, which costs about 30 ms
    of spawn time against several seconds of USB 1.1 transfer — a price worth
    paying, because it is what makes per-file progress, per-file outcomes and
    "one bad photo does not abort the rescue" possible.
    """

    kind = BackendKind.GPHOTO2
    display_name = "gphoto2 (vintage / proprietary protocol)"
    description = (
        "Reads cameras that are neither a USB drive nor PTP, such as pre-2003 "
        "Canon compacts. Requires the gphoto2 command-line tool."
    )

    #: Name looked up on PATH. A class attribute so the classmethod probe does
    #: not have to instantiate the backend just to learn what to look for.
    EXECUTABLE = "gphoto2"

    def __init__(self, executable: str = "") -> None:
        self._executable = executable or self.EXECUTABLE
        # Purely a throttle for _release_device; no device state lives on self,
        # because the camera may be unplugged between two GUI actions.
        #
        # None means "never released", not 0.0: time.monotonic()'s reference
        # point is explicitly undefined, and on some builds (Apple's Python 3.9,
        # which is this project's declared floor) it counts from process start
        # rather than from boot. A 0.0 sentinel then reads as "released a moment
        # ago" for the first five seconds of the program's life -- exactly when
        # the user clicks Detect -- and suppressed the one release that matters.
        self._last_release: Optional[float] = None

    # -- capability probing ------------------------------------------------ #

    @classmethod
    def is_available(cls) -> Availability:
        """Report whether the gphoto2 CLI can be used on this machine.

        Never raises and never touches a camera: it is called for every backend
        at startup, before anything is plugged in.
        """
        if _IS_WINDOWS:
            # There is no supported gphoto2/libgphoto2 build for native Windows:
            # the MSYS2/Cygwin ports cannot claim USB devices. Reporting False
            # here (even if some binary happens to be on PATH) makes the registry
            # fall through instead of failing later with a cryptic I/O error.
            return (False, t("gphoto2.unavailable_windows"))

        try:
            path = shutil.which(cls.EXECUTABLE)
        except Exception:  # pragma: no cover - PATH lookups do not normally fail
            path = None

        if not path:
            if _IS_MACOS:
                return (False, t("gphoto2.missing_macos"))
            if sys.platform.startswith("linux"):
                return (False, t("gphoto2.missing_linux"))
            return (False, t("gphoto2.missing_generic"))

        # An installed-but-broken binary (missing dylib after an OS upgrade is
        # the common case) must be reported as unavailable, not discovered
        # halfway through a transfer. --version is device-free and instant.
        try:
            proc = subprocess.run(
                [path, "--version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_base_env(),
                timeout=_VERSION_TIMEOUT,
                **_platform_popen_kwargs(),
            )
        except Exception:
            return (False, t("gphoto2.broken_binary", path=path))

        if proc.returncode != 0:
            return (False, t("gphoto2.broken_binary", path=path))
        return (True, "")

    @classmethod
    def install_hint(cls) -> str:
        """Command (or guidance) that would make this backend usable."""
        if _IS_MACOS:
            return "brew install gphoto2"
        if sys.platform.startswith("linux"):
            return "sudo apt install gphoto2   |   sudo dnf install gphoto2"
        if _IS_WINDOWS:
            return t("gphoto2.install_hint_windows")
        return "gphoto2"

    def executable_name(self) -> str:
        """Name or path of the CLI, so tests can point at a stub."""
        return self._executable

    def supports_delete(self) -> bool:
        """gphoto2 can delete on the S30 — but per-file failures are normal.

        The Canon driver reports "Delete selected files on camera: yes" for this
        body, so the button stays enabled. That is a *capability* claim, not a
        guarantee: a write-protected card, a photo protected in-camera, or a
        stuck lock switch makes an individual ``--delete-file`` fail. Those are
        reported as ``DeleteOutcome(ok=False, error=...)`` for that one file and
        never raised — the rest of the batch still runs.
        """
        return True

    # -- process plumbing -------------------------------------------------- #

    def _common_argv(self, camera: Optional[CameraInfo]) -> List[str]:
        """Base argv with the camera pinned, so no other device can be hit.

        Both flags are always passed together (see the module docstring): with
        either one missing gphoto2 falls back to its persisted settings or to
        "whatever enumerated first", both of which can silently address the
        wrong camera.
        """
        argv = [self.executable_name()]
        if camera is not None:
            if camera.model:
                argv += ["--camera", camera.model]
            if camera.port:
                argv += ["--port", camera.port]
        return argv

    def _run(
        self,
        argv: List[str],
        timeout: float,
        cancel: Optional[CancelToken] = None,
        on_stdout: Optional[Callable[[bytes], None]] = None,
        cwd: Optional[str] = None,
    ) -> Tuple[int, str, str]:
        """Run gphoto2 and return ``(returncode, stdout, stderr)``.

        Always argv-as-a-list (never ``shell=True``): camera-supplied file names
        end up in these arguments and must not be able to reach a shell. stdin
        is closed so an unexpected "Overwrite? [y|n]" prompt dies instead of
        hanging the GUI forever, and the child gets its own process group so
        cancelling kills gphoto2 without touching us.
        """
        if not _IS_POSIX or selectors is None:
            return self._run_blocking(argv, timeout, cwd)
        return self._run_streaming(argv, timeout, cancel, on_stdout, cwd)

    def _run_blocking(
        self, argv: List[str], timeout: float, cwd: Optional[str]
    ) -> Tuple[int, str, str]:
        """Fallback runner for platforms where pipes cannot be polled.

        No live progress and no mid-command cancellation, but the module stays
        importable and functional everywhere.
        """
        try:
            proc = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_base_env(),
                cwd=cwd,
                timeout=timeout,
                **_platform_popen_kwargs(),
            )
        except subprocess.TimeoutExpired:
            raise CameraError(t("gphoto2.timeout", seconds=int(timeout)))
        except OSError as exc:
            raise CameraError(t("gphoto2.spawn_failed", detail=str(exc)))
        return proc.returncode, _decode(proc.stdout or b""), _decode(proc.stderr or b"")

    def _run_streaming(
        self,
        argv: List[str],
        timeout: float,
        cancel: Optional[CancelToken],
        on_stdout: Optional[Callable[[bytes], None]],
        cwd: Optional[str],
    ) -> Tuple[int, str, str]:
        """Run gphoto2 while draining both pipes, so progress and cancel work.

        Both streams are polled together: reading them one after the other would
        deadlock as soon as the unread one filled its 64 kB pipe buffer.
        """
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_base_env(),
                cwd=cwd,
                bufsize=0,  # raw pipes: read() returns what is available now
                **_platform_popen_kwargs(),
            )
        except OSError as exc:
            raise CameraError(t("gphoto2.spawn_failed", detail=str(exc)))

        out_parts = []  # type: List[bytes]
        err_parts = []  # type: List[bytes]
        deadline = time.monotonic() + timeout
        timed_out = False
        cancelled = False

        selector = selectors.DefaultSelector()
        drained = False
        try:
            selector.register(proc.stdout, selectors.EVENT_READ, out_parts)
            selector.register(proc.stderr, selectors.EVENT_READ, err_parts)
            open_streams = 2
            while open_streams:
                if cancel is not None and cancel.cancelled():
                    cancelled = True
                    break
                if time.monotonic() > deadline:
                    timed_out = True
                    break
                for key, _mask in selector.select(timeout=0.2):
                    try:
                        chunk = key.fileobj.read(65536)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        selector.unregister(key.fileobj)
                        try:
                            key.fileobj.close()
                        except OSError:
                            pass
                        open_streams -= 1
                        continue
                    key.data.append(chunk)
                    if key.data is out_parts and on_stdout is not None:
                        on_stdout(chunk)
            drained = True
        finally:
            selector.close()
            # Whatever went wrong — a progress callback that threw, a selector
            # error — a gphoto2 process holding the USB interface must never
            # outlive this call, or the next operation fails with -53.
            if not drained:
                self._terminate(proc)
                _close_streams(proc)

        if cancelled or timed_out:
            self._terminate(proc)
            _close_streams(proc)
            if cancelled:
                raise TransferAborted(t("gphoto2.cancelled"))
            raise CameraError(t("gphoto2.timeout", seconds=int(timeout)))

        try:
            returncode = proc.wait(timeout=_KILL_GRACE_SECONDS * 2)
        except subprocess.TimeoutExpired:
            # Both pipes are closed but the process lingers: nothing useful can
            # arrive any more, so stop waiting on a possibly wedged USB stack.
            self._terminate(proc)
            returncode = proc.returncode if proc.returncode is not None else -1
        _close_streams(proc)
        return returncode, _decode(b"".join(out_parts)), _decode(b"".join(err_parts))

    def _terminate(self, proc: "subprocess.Popen[bytes]") -> None:
        """Stop a running gphoto2, escalating SIGINT -> SIGKILL.

        SIGINT is what gphoto2 handles cleanly: it aborts the transfer without
        writing a partial destination file. The whole process group is signalled
        because libgphoto2 may have spawned helpers.
        """
        if proc.poll() is not None:
            return
        try:
            if _IS_POSIX:
                os.killpg(os.getpgid(proc.pid), signal.SIGINT)
            else:  # pragma: no cover - Windows has no SIGINT for children
                proc.terminate()
        except OSError:
            return
        try:
            proc.wait(timeout=_KILL_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            if _IS_POSIX:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:  # pragma: no cover - Windows
                proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover - unkillable child
            pass

    def _run_checked(
        self,
        argv: List[str],
        timeout: float,
        progress: ProgressCallback,
        cancel: Optional[CancelToken] = None,
        on_stdout: Optional[Callable[[bytes], None]] = None,
        cwd: Optional[str] = None,
        phase: str = "download",
    ) -> Tuple[int, str, str]:
        """``_run`` plus one automatic retry when the device is held elsewhere.

        macOS starts ``ptpcamerad`` on demand and it can grab the camera in the
        moment between our release and our claim. One kill-and-retry turns that
        race from a user-visible failure into a hiccup.
        """
        returncode, out, err = self._run(argv, timeout, cancel, on_stdout, cwd)
        if returncode != 0 and _looks_like_claim_conflict(err):
            self._release_device(progress, force=True, phase=phase)
            returncode, out, err = self._run(argv, timeout, cancel, on_stdout, cwd)
        return returncode, out, err

    # -- macOS device release ---------------------------------------------- #

    def _release_device(
        self,
        progress: ProgressCallback = noop_progress,
        force: bool = False,
        phase: str = "detect",
    ) -> None:
        """Ask macOS to let go of the camera before we claim it.

        macOS auto-launches a PTP daemon whenever a camera-like device appears
        and holds the USB interface, which makes libgphoto2 fail with
        ``-53 Could not claim the USB device``. Both daemon names are tried:
        ``ptpcamerad`` on macOS 13+, ``PTPCamera`` on older systems. Both run as
        the current user, so no privileges are needed.

        Entirely best-effort — ``killall`` exits non-zero when nothing matched,
        which is the normal case and not an error. It is also possible that the
        daemon never claims a Canon-protocol body at all, in which case this is
        harmless prophylaxis.
        """
        if not _IS_MACOS:
            return
        now = time.monotonic()
        if (
            not force
            and self._last_release is not None
            and (now - self._last_release) < _RELEASE_THROTTLE_SECONDS
        ):
            return
        self._last_release = now

        for name in ("ptpcamerad", "PTPCamera"):
            try:
                proc = subprocess.run(
                    ["killall", name],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5.0,
                )
            except Exception:
                # A missing killall or a permission problem must never abort a
                # rescue: the claim may well succeed anyway.
                continue
            if proc.returncode == 0:
                _emit(
                    progress,
                    Progress(phase=phase, message=t("gphoto2.released_ptp", name=name)),
                )
        if force:
            # Give launchd a moment to notice before we try to claim again.
            time.sleep(0.3)

    # -- discovery --------------------------------------------------------- #

    def detect(self, progress: ProgressCallback = noop_progress) -> List[CameraInfo]:
        """Return every camera gphoto2 can currently see.

        ``--auto-detect`` exits 0 whether or not anything is attached, and still
        prints its header, so the exit code says nothing about detection: only
        the rows below the dashed separator do. "No camera" is a normal answer
        and returns an empty list rather than raising.
        """
        progress = progress or noop_progress
        self._release_device(progress)
        _emit(progress, Progress(phase="detect", message=t("gphoto2.detecting")))

        argv = [self.executable_name(), "--auto-detect"]
        returncode, out, err = self._run_checked(
            argv, _DETECT_TIMEOUT, progress, phase="detect"
        )
        if returncode != 0:
            # A real probe failure (broken install, unreadable USB bus) — the
            # user has to fix something, so this is not "no camera attached".
            raise self._error_for(err or out)

        cameras = []  # type: List[CameraInfo]
        for model, port in _parse_auto_detect(out):
            cameras.append(
                CameraInfo(
                    model=model,
                    port=port,
                    kind=self.kind,
                    detail=port,
                    raw={"backend": "gphoto2"},
                )
            )

        if not cameras:
            _emit(
                progress, Progress(phase="detect", message=t("gphoto2.detected_none"))
            )
        else:
            for cam in cameras:
                _emit(
                    progress,
                    Progress(
                        phase="detect",
                        message=t(
                            "gphoto2.detected_one", model=cam.model, port=cam.port
                        ),
                    ),
                )
        return cameras

    def list_files(
        self,
        camera: CameraInfo,
        progress: ProgressCallback = noop_progress,
        cancel: Optional[CancelToken] = None,
    ) -> List[CameraFile]:
        """List every file on the card, recursively, with exact byte sizes.

        ``--parsable -L`` is the only listing form worth using: it prints exact
        ``FILESIZE`` values, absolute paths and a machine-stable syntax that
        gettext does not touch. The human ``-L`` parser below is a fallback for
        gphoto2 builds that lack ``--parsable``; because its sizes are rounded
        up to whole KB it deliberately reports ``size=-1`` instead, so that
        verification never compares a real byte count against a rounded one. The
        rounded value is kept in ``raw['kb']`` purely so the UI can show
        something.
        """
        progress = progress or noop_progress
        if cancel is not None:
            cancel.raise_if_cancelled()
        self._release_device(progress, phase="list")
        _emit(progress, Progress(phase="list", message=t("gphoto2.listing")))

        common = self._common_argv(camera)
        # Recursion is the default for -L in current gphoto2; the -R variant is
        # a cheap insurance policy against a build where it is not.
        attempts = (
            (["--parsable", "-L"], _parse_parsable_listing),
            (["--parsable", "-L", "-R"], _parse_parsable_listing),
            (["-L", "-R"], _parse_human_listing),
        )

        last_error = ""
        for extra, parser in attempts:
            if cancel is not None:
                cancel.raise_if_cancelled()
            returncode, out, err = self._run_checked(
                common + list(extra), _LIST_TIMEOUT, progress, cancel, phase="list"
            )
            if returncode != 0:
                last_error = err or out
                continue
            found = parser(out)
            if found:
                # Stable (folder, name) order keeps re-runs reproducible and the
                # progress bar monotonic.
                found.sort(key=lambda f: (f.folder, f.name))
                _emit(
                    progress,
                    Progress(
                        phase="list",
                        total=len(found),
                        message=t("gphoto2.listed", count=len(found)),
                    ),
                )
                return found

        if last_error:
            raise self._error_for(last_error)
        # Every attempt succeeded and every attempt found nothing: empty card.
        _emit(progress, Progress(phase="list", message=t("gphoto2.listed", count=0)))
        return []

    # -- download ---------------------------------------------------------- #

    def download(
        self,
        camera: CameraInfo,
        files: Sequence[CameraFile],
        dest_dir: str,
        progress: ProgressCallback = noop_progress,
        cancel: Optional[CancelToken] = None,
        skip_existing: bool = True,
    ) -> List[DownloadOutcome]:
        """Copy ``files`` into ``dest_dir``, one gphoto2 process per file.

        Returns one outcome per input file, in the same order. A file that fails
        is recorded and the run continues: on a dying card the whole point is to
        rescue the 78 photos that can still be read, not to stop at the first
        one that cannot.

        Each file lands on a private ``.part`` name inside ``dest_dir``, is
        fsync'd, size-checked, and only then ``os.replace``-d onto the final
        name from :meth:`safe_dest_path`. A half-written file therefore never
        carries the final name, and an existing file is never overwritten.

        ``ok`` here means "the bytes arrived and match the announced size"; the
        transfer engine runs the real verification afterwards and produces the
        final outcome that the delete gate reads.

        On cancellation this raises :class:`TransferAborted`, with the outcomes
        collected so far attached as ``exc.outcomes`` so a caller that wants a
        partial report can still build one.
        """
        progress = progress or noop_progress
        results = []  # type: List[DownloadOutcome]

        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as exc:
            raise CameraError(
                t("gphoto2.dest_unwritable", path=dest_dir, detail=str(exc))
            )

        self._release_device(progress, phase="download")
        common = self._common_argv(camera)
        total = len(files)
        # Destination paths already spoken for by this batch. Two Canon folders
        # legitimately hold the same base name once the frame counter rolls
        # over, and without this the second file would "resume" onto the first
        # file's copy — see _maybe_skip.
        claimed = set()  # type: set

        for index, cam_file in enumerate(files):
            if cancel is not None and cancel.cancelled():
                raise _aborted_with(results)

            _emit(
                progress,
                Progress(
                    phase="download",
                    index=index,
                    total=total,
                    name=cam_file.name,
                    bytes_total=max(cam_file.size, 0),
                    message=t("gphoto2.downloading", name=cam_file.name),
                ),
            )

            skipped = self._maybe_skip(dest_dir, cam_file, skip_existing, claimed)
            if skipped is not None:
                _emit(
                    progress,
                    Progress(
                        phase="download",
                        index=index + 1,
                        total=total,
                        name=cam_file.name,
                        message=t("gphoto2.skipped_existing", name=cam_file.name),
                    ),
                )
                if skipped.dest_path:
                    claimed.add(_dest_key(skipped.dest_path))
                results.append(skipped)
                continue

            try:
                outcome = self._download_one(
                    common, cam_file, dest_dir, index, total, progress, cancel
                )
            except TransferAborted:
                raise _aborted_with(results)
            if outcome.dest_path:
                claimed.add(_dest_key(outcome.dest_path))
            results.append(outcome)

            _emit(
                progress,
                Progress(
                    phase="download",
                    index=index + 1,
                    total=total,
                    name=cam_file.name,
                    bytes_done=max(cam_file.size, 0) if outcome.ok else 0,
                    bytes_total=max(cam_file.size, 0),
                    message="" if outcome.ok else outcome.error,
                ),
            )

        return results

    def _maybe_skip(
        self,
        dest_dir: str,
        cam_file: CameraFile,
        skip_existing: bool,
        claimed: Optional[set] = None,
    ) -> Optional[DownloadOutcome]:
        """Return a skip outcome when an identical copy is already on disk.

        This is the resume path for an interrupted rescue, and it is the one
        place where a wrong answer is expensive: a skipped file is reported
        ``ok``, which is what the delete gate reads, so skipping the wrong file
        could erase a photo that was never actually downloaded.

        Three independent facts must therefore agree before we skip, and each
        must be *known* — a missing fact never counts as a match:

        1. **The name is not already spoken for by this batch.** Both names
           :meth:`CameraBackend.safe_dest_path` can produce are tried, in the
           same order it tries them, and any path an earlier file in this run
           already took is skipped. Without this, ``119CANON/IMG_0001.JPG``
           matches the flat ``IMG_0001.JPG`` that ``118CANON``'s *different*
           photo wrote moments earlier — same name, possibly the same size,
           entirely different picture — and the engine would then verify the
           first photo's bytes and green-light erasing the second one.
        2. **An exact byte-for-byte size match** — never the rounded KB of the
           human listing, which is why that parser reports ``size=-1``.
        3. **The modification time.** gphoto2 stamps each downloaded file with
           the camera's timestamp and ``os.replace`` preserves it, so a genuine
           earlier copy carries it too. When the camera reports no timestamp
           there is only one fact left, and one fact is not enough to justify an
           irreversible delete, so we re-download instead.

        Anything less certain falls through to a real download: re-reading a
        photo over USB 1.1 costs seconds, and getting this wrong costs the photo.
        """
        if not skip_existing or not cam_file.size_known or cam_file.size <= 0:
            return None
        if not cam_file.mtime:
            # No timestamp to corroborate the size: see rule 3 above.
            return None

        base = _safe_basename(cam_file.name)
        folder_tag = cam_file.folder.rstrip("/").split("/")[-1] or "DCIM"
        for name in (base, "%s_%s" % (folder_tag, base)):
            candidate = os.path.join(dest_dir, name)
            if claimed is not None and _dest_key(candidate) in claimed:
                continue  # another file in this batch owns that copy
            try:
                if not os.path.isfile(candidate):
                    continue
                if os.path.getsize(candidate) != cam_file.size:
                    continue
                # 2 s tolerance: FAT stores timestamps at 2-second granularity
                # and gphoto2 truncates to whole seconds when it stamps a file.
                if abs(os.path.getmtime(candidate) - cam_file.mtime) > 2.0:
                    continue
            except OSError:
                continue
            return DownloadOutcome(
                file=cam_file,
                dest_path=candidate,
                ok=True,
                error="",
                skipped=True,
            )
        return None

    def _download_one(
        self,
        common: List[str],
        cam_file: CameraFile,
        dest_dir: str,
        index: int,
        total: int,
        progress: ProgressCallback,
        cancel: Optional[CancelToken],
    ) -> DownloadOutcome:
        """Fetch a single file. Never raises except for cancellation."""
        base_name = _safe_basename(cam_file.name)
        # Our own name, unique per attempt and hidden: gphoto2 can never be
        # pointed at a real destination file, so --force-overwrite below is
        # safe and cannot silently destroy anything the user cares about.
        tmp_path = os.path.join(
            dest_dir, ".retrocam-{0}-{1}.part".format(uuid.uuid4().hex[:12], base_name)
        )
        _unlink_quietly(tmp_path)

        folder = cam_file.folder.rstrip("/") or "/"
        argv = list(common) + [
            "--folder",
            folder,
            "--get-file",
            cam_file.name,
            # Any '%' in a path is a strftime-style conversion for gphoto2, so
            # '%d' in a folder name would silently become the day of the month.
            "--filename",
            tmp_path.replace("%", "%%"),
            "--force-overwrite",
        ]

        size = cam_file.size if cam_file.size_known else -1

        def emit_percent(pct: float) -> None:
            done = int(size * pct / 100.0) if size > 0 else 0
            _emit(
                progress,
                Progress(
                    phase="download",
                    index=index,
                    total=total,
                    name=cam_file.name,
                    bytes_done=done,
                    bytes_total=max(size, 0),
                ),
            )

        parser = _PercentParser(emit_percent)

        try:
            returncode, out, err = self._run_checked(
                argv,
                _download_timeout(size),
                progress,
                cancel,
                on_stdout=parser.feed,
                # If --filename were ever ignored, the bytes still land in the
                # destination directory rather than in the app's own CWD.
                cwd=dest_dir,
            )
        except TransferAborted:
            _unlink_quietly(tmp_path)
            raise
        except CameraError as exc:
            _unlink_quietly(tmp_path)
            return DownloadOutcome(
                file=cam_file, dest_path=None, ok=False, error=str(exc)
            )

        if returncode != 0:
            _unlink_quietly(tmp_path)
            return DownloadOutcome(
                file=cam_file,
                dest_path=None,
                ok=False,
                error=self._explain(err or out),
            )

        if not os.path.isfile(tmp_path):
            return DownloadOutcome(
                file=cam_file, dest_path=None, ok=False, error=t("gphoto2.no_output")
            )

        try:
            actual = os.path.getsize(tmp_path)
        except OSError as exc:
            _unlink_quietly(tmp_path)
            return DownloadOutcome(
                file=cam_file, dest_path=None, ok=False, error=str(exc)
            )

        # A zero-length result is always a failure, even if the listing claimed
        # zero bytes: there is nothing to rescue and nothing to verify, and a
        # file the delete gate might accept must never be built from it.
        if actual == 0:
            _unlink_quietly(tmp_path)
            return DownloadOutcome(
                file=cam_file, dest_path=None, ok=False, error=t("gphoto2.empty_file")
            )

        if size >= 0 and actual != size:
            _unlink_quietly(tmp_path)
            return DownloadOutcome(
                file=cam_file,
                dest_path=None,
                ok=False,
                error=t("gphoto2.size_mismatch", got=actual, expected=size),
            )

        _fsync_path(tmp_path)

        # Resolved as late as possible so a file that appeared in the meantime
        # (another folder's IMG_0001.JPG earlier in this same batch) is seen.
        try:
            final_path = self.safe_dest_path(dest_dir, cam_file)
            os.replace(tmp_path, final_path)
        except (OSError, RuntimeError) as exc:
            _unlink_quietly(tmp_path)
            return DownloadOutcome(
                file=cam_file,
                dest_path=None,
                ok=False,
                error=t("gphoto2.replace_failed", detail=str(exc)),
            )
        _fsync_dir(dest_dir)

        return DownloadOutcome(file=cam_file, dest_path=final_path, ok=True, error="")

    # -- delete ------------------------------------------------------------ #

    def delete(
        self,
        camera: CameraInfo,
        files: Sequence[CameraFile],
        progress: ProgressCallback = noop_progress,
        cancel: Optional[CancelToken] = None,
    ) -> List[DeleteOutcome]:
        """Erase ``files`` from the camera, one file per invocation.

        Never ``--delete-all-files``, never ``--recurse``, never a folder: only
        the exact names handed in, one ``--delete-file`` at a time. That keeps a
        listing mismatch from escalating into total loss.

        The exit code alone is not trusted. On the Canon driver a refused delete
        can surface as ``-108 File not found`` (the same code as "no such
        file"), so after the batch each affected folder is listed again and the
        verdicts are corrected against reality: a file that is really gone is
        reported as deleted, and a file gphoto2 claimed to delete but that is
        still on the card is reported as *not* deleted. When the re-listing
        itself fails, the exit-code verdict stands and is flagged as unconfirmed
        rather than invented.
        """
        progress = progress or noop_progress
        self._release_device(progress, phase="delete")
        common = self._common_argv(camera)
        total = len(files)

        outcomes = []  # type: List[DeleteOutcome]
        for index, cam_file in enumerate(files):
            if cancel is not None and cancel.cancelled():
                raise _aborted_with(outcomes)

            _emit(
                progress,
                Progress(
                    phase="delete",
                    index=index,
                    total=total,
                    name=cam_file.name,
                    message=t("gphoto2.deleting", name=cam_file.name),
                ),
            )

            folder = cam_file.folder.rstrip("/") or "/"
            argv = list(common) + ["--folder", folder, "--delete-file", cam_file.name]
            try:
                returncode, out, err = self._run_checked(
                    argv, _DELETE_TIMEOUT, progress, cancel, phase="delete"
                )
            except TransferAborted:
                raise _aborted_with(outcomes)
            except CameraError as exc:
                outcomes.append(DeleteOutcome(file=cam_file, ok=False, error=str(exc)))
                continue

            if returncode == 0:
                outcomes.append(DeleteOutcome(file=cam_file, ok=True, error=""))
            else:
                outcomes.append(
                    DeleteOutcome(
                        file=cam_file, ok=False, error=self._explain(err or out)
                    )
                )

            _emit(
                progress,
                Progress(
                    phase="delete", index=index + 1, total=total, name=cam_file.name
                ),
            )

        return self._confirm_deletions(camera, outcomes, progress, cancel)

    def _confirm_deletions(
        self,
        camera: CameraInfo,
        outcomes: List[DeleteOutcome],
        progress: ProgressCallback,
        cancel: Optional[CancelToken],
    ) -> List[DeleteOutcome]:
        """Re-list the touched folders and correct the verdicts against them.

        Absence from the card is the only real proof a file is gone, and the
        only way to tell the "-108 because it was already deleted" case apart
        from the "-108 because the camera refused" case.
        """
        if not outcomes:
            return outcomes

        folders = []  # type: List[str]
        for outcome in outcomes:
            folder = outcome.file.folder.rstrip("/") or "/"
            if folder not in folders:
                folders.append(folder)

        _emit(
            progress, Progress(phase="delete", message=t("gphoto2.delete_confirming"))
        )

        common = self._common_argv(camera)
        surviving = {}  # type: Dict[str, bool]
        checked_folders = set()  # type: set
        for folder in folders:
            if cancel is not None and cancel.cancelled():
                break
            argv = common + ["--folder", folder, "--no-recurse", "--parsable", "-L"]
            try:
                returncode, out, _err = self._run(argv, _LIST_TIMEOUT, cancel)
            except CameraError:
                # Includes TransferAborted: the deletions already happened, so
                # returning what we know beats raising and losing the record.
                continue  # Unconfirmed; the exit-code verdict stands.
            if returncode != 0:
                continue
            checked_folders.add(folder)
            for cam_file in _parse_parsable_listing(out):
                surviving[cam_file.path] = True

        confirmed = []  # type: List[DeleteOutcome]
        for outcome in outcomes:
            folder = outcome.file.folder.rstrip("/") or "/"
            if folder not in checked_folders:
                # Could not verify: keep what gphoto2 said, but say so when it
                # claimed success, because "probably deleted" is not deleted.
                if outcome.ok:
                    confirmed.append(
                        DeleteOutcome(
                            file=outcome.file,
                            ok=True,
                            error=t("gphoto2.delete_unconfirmed"),
                        )
                    )
                else:
                    confirmed.append(outcome)
                continue

            still_there = outcome.file.path in surviving
            if still_there:
                confirmed.append(
                    DeleteOutcome(
                        file=outcome.file,
                        ok=False,
                        error=outcome.error or t("gphoto2.still_present"),
                    )
                )
            else:
                # Gone from the card: deleted, whatever the exit code claimed.
                confirmed.append(DeleteOutcome(file=outcome.file, ok=True, error=""))
        return confirmed

    # -- error translation -------------------------------------------------- #

    def _explain(self, stderr: str) -> str:
        """Turn a gphoto2 failure into one actionable sentence plus evidence.

        The libgphoto2 numeric code is matched first because it is the only part
        of the banner that survives translation; the message text is used only
        as a fallback for the few failures that carry no code.
        """
        text = stderr or ""
        code = _gp_error_code(text)
        key = None
        if code is not None and code in _ERROR_MAP:
            key = _ERROR_MAP[code][0]
        elif _looks_like_claim_conflict(text):
            key = "gphoto2.err_claim"
        elif any(marker in text.lower() for marker in _NO_CAMERA_MARKERS):
            key = "gphoto2.err_no_camera"

        detail = _tail(text)
        if key is None:
            return detail or t("gphoto2.err_generic", detail="")
        return t(key, detail=detail)

    def _error_for(self, stderr: str) -> CameraError:
        """Build the right :class:`CameraError` subclass for a failure."""
        text = stderr or ""
        code = _gp_error_code(text)
        exc_class = CameraError  # type: type
        if code is not None and code in _ERROR_MAP:
            exc_class = _ERROR_MAP[code][1]
        elif any(marker in text.lower() for marker in _NO_CAMERA_MARKERS):
            exc_class = CameraNotFound
        return exc_class(self._explain(text))


# --------------------------------------------------------------------------- #
# Module-level parsing helpers (pure functions, trivially testable)
# --------------------------------------------------------------------------- #


def _base_env() -> Dict[str, str]:
    """Environment for every gphoto2 call: English, machine-parsable output.

    gphoto2 localizes everything a human reads, error messages included, so a
    parser written against the English strings silently breaks on an Italian
    desktop. Forcing the C locale is what makes the parsers below deterministic.
    """
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    env["LANGUAGE"] = ""
    return env


def _platform_popen_kwargs() -> Dict[str, Any]:
    """Per-platform Popen flags for every child process this backend starts.

    POSIX: the child gets its own session so cancelling can signal the whole
    process group without the risk of hitting the GUI process itself.

    Windows: CREATE_NO_WINDOW, so no console box blinks over the Tk window.
    :meth:`GPhoto2Backend.is_available` reports False on Windows, so today this
    branch is unreachable in the shipped flow — it is set anyway because an
    un-flagged Popen is exactly the kind of thing that gets copied into a path
    that *is* reachable, and to a person watching a rescue a black rectangle
    flashing over the window is indistinguishable from a crash.
    """
    if _IS_POSIX:
        return {"start_new_session": True}
    if os.name == "nt":
        # Same Win32 flag as deps._CREATE_NO_WINDOW; spelled out rather than
        # imported so this backend keeps no dependency on the deps module.
        return {"creationflags": 0x08000000}
    return {}


def _close_streams(proc: "subprocess.Popen[bytes]") -> None:
    """Close any pipe the reader loop did not consume to EOF."""
    for stream in (proc.stdout, proc.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError:
            pass


def _emit(progress: Optional[ProgressCallback], tick: Progress) -> None:
    """Push a progress tick, never letting a GUI bug break the transfer."""
    if progress is None:
        return
    try:
        progress(tick)
    except Exception:  # pragma: no cover - defensive
        pass


def _aborted_with(partial: Sequence[Any]) -> TransferAborted:
    """Build the cancellation error, carrying the outcomes collected so far.

    The base contract says a backend raises :class:`TransferAborted` promptly on
    cancel, which normally throws away the per-file results. Attaching them lets
    a caller that knows about ``exc.outcomes`` still report "31 of 82 rescued
    before you pressed Stop" instead of nothing.
    """
    error = TransferAborted(t("gphoto2.cancelled"))
    setattr(error, "outcomes", list(partial))
    return error


def _dest_key(path: str) -> str:
    """Comparison key for a destination path, case-folded where the OS is."""
    return os.path.normcase(os.path.abspath(path))


def _safe_basename(name: str) -> str:
    """Strip any directory component a malformed listing might carry.

    Mirrors what :meth:`CameraBackend.safe_dest_path` does, so the temporary
    name and the final name can never disagree about where the file goes.
    """
    cleaned = os.path.basename((name or "").replace("\\", "/")).strip()
    if not cleaned or cleaned in (".", ".."):
        return "unnamed.bin"
    return cleaned


def _parse_auto_detect(stdout: str) -> List[Tuple[str, str]]:
    """Parse ``gphoto2 --auto-detect`` into ``[(model, port), ...]``.

    Output is a two-line header (a localized ``Model  Port`` line and a row of
    58 dashes) followed by one fixed-width row per camera. Column slicing would
    be wrong because the model field is padded but never truncated, and a plain
    ``split()`` would be wrong because model names contain spaces — so the split
    is made at the last whitespace run, anchored on the port's ``scheme:``
    prefix at the end of the line.
    """
    lines = (stdout or "").splitlines()

    start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if len(stripped) >= 10 and set(stripped) == {"-"}:
            start = i + 1
            break
    if start is None:
        # No header at all: not a listing we understand, so claim nothing.
        return []

    found = []  # type: List[Tuple[str, str]]
    for line in lines[start:]:
        line = line.rstrip()
        if not line.strip():
            continue
        match = _PORT_RE.search(line)
        if match:
            port = match.group("port")
            model = line[: match.start("port")].rstrip()
        else:
            # Unknown port scheme: fall back to the last whitespace run rather
            # than dropping a camera we could still address.
            parts = line.rsplit(None, 1)
            if len(parts) != 2:
                continue
            model, port = parts[0].rstrip(), parts[1]
        if model and port:
            found.append((model, port))
    return found


def _parse_parsable_listing(stdout: str) -> List[CameraFile]:
    """Parse ``--parsable -L`` output. Sizes here are exact bytes.

    Unparsable lines are skipped rather than guessed at: gphoto2 mixes a
    debugging boilerplate block into stdout on some failures, and a half-read
    line must never become a CameraFile with a wrong size.
    """
    files = []  # type: List[CameraFile]
    for line in (stdout or "").splitlines():
        match = _PARSABLE_RE.match(line.strip())
        if not match:
            continue
        folder, name = _normalise_device_path(match.group("path"))
        if not name:
            continue
        try:
            size = int(match.group("size"))
        except ValueError:  # pragma: no cover - regex guarantees digits
            continue
        mtime = None  # type: Optional[float]
        try:
            raw_mtime = int(match.group("mtime"))
            if raw_mtime > 0:
                mtime = float(raw_mtime)
        except ValueError:  # pragma: no cover
            pass
        files.append(
            CameraFile(
                folder=folder,
                name=name,
                size=size,
                mtime=mtime,
                raw={
                    "mime": match.group("mime"),
                    # 'rd' = read+delete, 'r-' = read only. The camlib's claim,
                    # not a guarantee: good enough to gray out a delete button,
                    # never good enough to assume a delete will succeed.
                    "perms": match.group("perms"),
                    "source": "parsable",
                },
            )
        )
    return files


def _parse_human_listing(stdout: str) -> List[CameraFile]:
    """Parse the human ``-L`` listing. Fallback only — sizes are NOT exact.

    The KB column is ``ceil(bytes / 1024)``, so a 987-byte file reads as "1 KB".
    Handing that to verification would fail every single good file, so ``size``
    stays ``-1`` ("unknown") and the rounded figure goes to ``raw['kb']`` for
    display. Folder context comes from the "There are N files in folder '...'"
    headers, which are only stable because everything runs under ``LC_ALL=C``.
    """
    files = []  # type: List[CameraFile]
    folder = "/"
    for line in (stdout or "").splitlines():
        header = _HUMAN_FOLDER_RE.search(line)
        if header:
            folder = re.sub(r"/{2,}", "/", header.group("folder").strip()) or "/"
            continue

        entry = _HUMAN_ENTRY_RE.match(line.rstrip())
        if not entry:
            continue
        name = entry.group("name").strip()
        if not name:
            continue
        mtime = None  # type: Optional[float]
        raw_mtime = entry.group("mtime")
        if raw_mtime:
            try:
                value = int(raw_mtime)
                if value > 0:
                    mtime = float(value)
            except ValueError:  # pragma: no cover
                pass
        try:
            kb = int(entry.group("kb"))
        except ValueError:  # pragma: no cover
            kb = -1
        files.append(
            CameraFile(
                folder=folder,
                name=name,
                size=-1,  # deliberately unknown: see the docstring
                mtime=mtime,
                raw={
                    "kb": kb,
                    "mime": entry.group("mime"),
                    "perms": entry.group("perms"),
                    "source": "human",
                },
            )
        )
    return files
