"""Dependency detection and assisted installation.

RetroCam Rescue works out of the box with nothing but the Python standard
library: a memory card in a reader needs no tools at all. Everything probed here
is an *enhancement* — a way to reach the camera body itself (gphoto2, WIA,
the WSL bridge) or a way to verify a downloaded photo more deeply (Pillow).

Two hard rules govern this module, because it is the only place in the app that
runs other people's installers:

* **Never escalate.** No ``sudo``, no ``runas``, no UAC trickery. When a step
  needs administrator rights or a manual decision, :func:`install` returns
  ``(False, instructions)`` and lets the human do it.
* **Never pipe a remote script into a shell.** We invoke package managers that
  are already on the machine, by absolute path where we can resolve one, with a
  fixed argument list — never through ``shell=True``.

Every probe is read-only, time-boxed and swallow-all: :func:`check_all` is
called while the GUI is painting its environment panel and must not be able to
raise, hang, or change the machine.

A note on language: the strings returned here are technical (tool names, shell
commands, URLs) and are deliberately *not* run through :mod:`retrocam.i18n`.
The stable :attr:`Dependency.key` is the seam the GUI translates against — it
can render ``t("deps.label." + dep.key)`` and fall back to
:attr:`Dependency.label` — while ``hint`` carries the literal command a user
would type, which must not be translated.
"""

from __future__ import annotations

import collections
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .backends.base import noop_progress
from .model import Progress, ProgressCallback

__all__ = [
    "Dependency",
    "KEY_GPHOTO2",
    "KEY_HOMEBREW",
    "KEY_PILLOW",
    "KEY_PYWIN32",
    "KEY_USBIPD",
    "KEY_WSL",
    "INSTALLABLE_KEYS",
    "check_all",
    "install",
    "which",
    "default_download_dir",
    "suggested_dest",
]


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #

#: Stable identifiers. They are part of the public API: the GUI passes them to
#: :func:`install` and may use them to look up translated labels.
KEY_GPHOTO2 = "gphoto2"
KEY_HOMEBREW = "homebrew"
KEY_PILLOW = "pillow"
KEY_PYWIN32 = "pywin32"
KEY_WSL = "wsl"
KEY_USBIPD = "usbipd"

#: The subset :func:`install` will attempt. Everything else is manual by design
#: (Homebrew itself and WSL2 both need a shell script or an elevated reboot).
INSTALLABLE_KEYS = (KEY_GPHOTO2, KEY_PILLOW, KEY_PYWIN32, KEY_USBIPD)

_IS_WINDOWS = os.name == "nt"
_IS_MACOS = sys.platform == "darwin"

#: Force tool output to a language we can parse. gphoto2, brew and pip are all
#: gettext-localized; matching on translated prose is how parsers rot.
_C_LOCALE = {"LC_ALL": "C", "LANG": "C", "LANGUAGE": ""}

#: Stops a console window flashing over the Tk GUI on Windows.
_CREATE_NO_WINDOW = 0x08000000

_HOMEBREW_URL = "https://brew.sh"
_HOMEBREW_INSTALL_SH = (
    '/bin/bash -c "$(curl -fsSL '
    'https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
)
_USBIPD_RELEASES_URL = "https://github.com/dorssel/usbipd-win/releases"


# --------------------------------------------------------------------------- #
# The report type
# --------------------------------------------------------------------------- #


@dataclass
class Dependency:
    """One external thing RetroCam can use, and whether it is here.

    Instances are snapshots: the GUI re-runs :func:`check_all` after an install
    rather than mutating them, so a stale row can never be shown as fixed.
    """

    key: str
    """Stable identifier, one of the ``KEY_*`` constants. Never localized."""

    label: str
    """Short human-readable name for the environment panel, e.g. 'Pillow'."""

    present: bool = False
    """True when the dependency is usable *right now*, not merely recorded as
    installed somewhere."""

    version: str = ""
    """Short identifying string when we can get one cheaply — usually a version
    number, but for WSL it is the list of installed distributions. Empty is
    normal and never means 'absent'; read :attr:`present` for that."""

    hint: str = ""
    """One sentence the GUI can show verbatim. When absent, says how to get it;
    when present, either empty or a useful detail."""

    can_autoinstall: bool = False
    """True when pressing Install has a real chance of working unattended: the
    package manager we would drive is itself present, and no administrator
    rights are needed beyond a prompt the user can answer."""


# --------------------------------------------------------------------------- #
# Small, safe process helpers
# --------------------------------------------------------------------------- #


def _decode(raw: bytes) -> str:
    """Decode tool output without guessing wrong.

    ``wsl.exe`` emits UTF-16LE unless ``WSL_UTF8=1`` is set, and it sometimes
    emits *error* text as UTF-16 even when success output is UTF-8. Sniffing for
    an embedded NUL in the first bytes distinguishes the two reliably; the
    trailing replace() cleans up any NUL that survived a split.
    """
    if b"\x00" in raw[:64]:
        text = raw.decode("utf-16-le", "replace")
    else:
        text = raw.decode("utf-8", "replace")
    return text.replace("\x00", "")


def _popen_kwargs() -> Dict[str, object]:
    """Platform flags shared by every child process we start."""
    if _IS_WINDOWS:
        return {"creationflags": _CREATE_NO_WINDOW}
    # Own session, so a hung tool cannot be steered by (or steer) the GUI's
    # signals, and so a timeout can kill the whole process group.
    return {"start_new_session": True}


def _run(
    cmd: Sequence[str],
    timeout: float = 10.0,
    extra_env: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[int], str, str]:
    """Run a short, side-effect-free probe.

    Returns ``(returncode, stdout, stderr)`` with ``returncode`` set to ``None``
    when the command could not be started or did not finish in time. Never
    raises: a probe that explodes must degrade to "not present", never to a
    traceback in front of someone rescuing their photos.
    """
    env = dict(os.environ)
    env.update(_C_LOCALE)
    if extra_env:
        env.update(extra_env)
    try:
        proc = subprocess.run(
            list(cmd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=env,
            **_popen_kwargs(),  # type: ignore[arg-type]
        )
    except subprocess.TimeoutExpired:
        return None, "", "timed out after %.0f s" % timeout
    except Exception as exc:  # OSError, ValueError, anything the OS throws
        return None, "", str(exc)
    return proc.returncode, _decode(proc.stdout or b""), _decode(proc.stderr or b"")


def _extra_tool_dirs() -> List[str]:
    """Directories to search when PATH is too small to be trusted.

    A Tkinter app double-clicked from Finder inherits a minimal PATH
    (``/usr/bin:/bin:/usr/sbin:/sbin``) that contains neither Homebrew prefix,
    so ``shutil.which('gphoto2')`` says "not installed" about a perfectly good
    installation. Same story for a shortcut-launched app on Windows that was
    started before an installer extended the machine PATH.
    """
    if _IS_WINDOWS:
        dirs = []
        for var in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(var)
            if base:
                dirs.append(os.path.join(base, "usbipd-win"))
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(os.path.join(local, "Microsoft", "WindowsApps"))
        return dirs
    return [
        "/opt/homebrew/bin",  # Homebrew on Apple Silicon
        "/usr/local/bin",  # Homebrew on Intel, and most manual installs
        "/opt/local/bin",  # MacPorts
        "/usr/bin",
        "/bin",
        os.path.expanduser("~/.local/bin"),
    ]


def which(name: str, extra_dirs: Iterable[str] = ()) -> Optional[str]:
    """Absolute path to an executable, or ``None``.

    Public because the gphoto2 backend needs exactly the same PATH repair — a
    camera that is invisible only because the app was launched from Finder is
    the single most confusing failure this project can produce.
    """
    try:
        found = shutil.which(name)
        if found:
            return found
        candidates = list(extra_dirs) + _extra_tool_dirs()
        suffixes = (".exe", ".cmd", "") if _IS_WINDOWS else ("",)
        for directory in candidates:
            if not directory:
                continue
            for suffix in suffixes:
                candidate = os.path.join(os.path.expanduser(directory), name + suffix)
                if os.path.isfile(candidate) and (
                    _IS_WINDOWS or os.access(candidate, os.X_OK)
                ):
                    return candidate
    except Exception:
        return None
    return None


def _emit(progress: ProgressCallback, key: str, message: str) -> None:
    """Push one line to the GUI log. Logging must never break an install."""
    if not message:
        return
    try:
        progress(Progress(phase="deps", name=key, message=message))
    except Exception:
        pass


def _terminate(proc: "subprocess.Popen") -> None:
    """Stop a runaway installer, children included.

    SIGTERM first: a package manager killed mid-write can leave a half-unpacked
    prefix, so give it a moment to unwind before SIGKILL.
    """
    try:
        if _IS_WINDOWS:
            proc.terminate()
        else:
            os.killpg(os.getpgid(proc.pid), 15)
    except Exception:
        pass
    try:
        proc.wait(timeout=3)
        return
    except Exception:
        pass
    try:
        if _IS_WINDOWS:
            proc.kill()
        else:
            os.killpg(os.getpgid(proc.pid), 9)
    except Exception:
        pass


def _stream(
    cmd: Sequence[str],
    key: str,
    progress: ProgressCallback,
    timeout: float,
    extra_env: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[int], List[str], bool]:
    """Run an installer, forwarding its output line by line to ``progress``.

    Returns ``(returncode, tail, timed_out)``. ``returncode`` is ``None`` when
    the command could not be started at all; ``tail`` is the last few output
    lines, which is what we quote back to the user when something fails.

    The command line itself is echoed first: a tool that installs software on
    someone's machine should show exactly what it ran.
    """
    _emit(progress, key, "$ " + " ".join(cmd))

    env = dict(os.environ)
    env.update(_C_LOCALE)
    if extra_env:
        env.update(extra_env)

    try:
        proc = subprocess.Popen(
            list(cmd),
            stdin=subprocess.DEVNULL,  # no installer may ever block on a prompt
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,  # unbuffered: read() returns whatever has arrived
            env=env,
            **_popen_kwargs(),  # type: ignore[arg-type]
        )
    except Exception as exc:
        return None, [str(exc)], False

    # A blocking read cannot notice a deadline on its own, so the watchdog runs
    # on its own thread and guarantees we come back even from a wedged tool.
    timed_out = [False]

    def _watchdog() -> None:
        timed_out[0] = True
        _terminate(proc)

    timer = threading.Timer(timeout, _watchdog)
    timer.daemon = True
    timer.start()

    tail = collections.deque(maxlen=15)  # type: collections.deque
    last_line = ""
    buffer = b""
    try:
        while True:
            try:
                chunk = proc.stdout.read(4096) if proc.stdout else b""
            except Exception:
                break
            if not chunk:
                break
            buffer += chunk
            # Split on \r as well as \n: brew, pip and winget all draw progress
            # with carriage returns, and a log that only updates on newlines
            # looks frozen for minutes at a time.
            parts = re.split(rb"[\r\n]", buffer)
            buffer = parts.pop()
            for raw in parts:
                line = _decode(raw).strip()
                # Drop pure spinner/progress-bar rows and immediate repeats.
                if not line or not any(ch.isalnum() for ch in line):
                    continue
                if line == last_line:
                    continue
                last_line = line
                tail.append(line)
                _emit(progress, key, line)
        remainder = _decode(buffer).strip()
        if remainder and any(ch.isalnum() for ch in remainder):
            tail.append(remainder)
            _emit(progress, key, remainder)
    finally:
        timer.cancel()
        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:
            pass

    try:
        returncode = proc.wait(timeout=15)
    except Exception:
        _terminate(proc)
        returncode = proc.poll()
    return returncode, list(tail), timed_out[0]


def _quote_tail(tail: Sequence[str], limit: int = 4) -> str:
    """The last few output lines, for a failure message the user can act on."""
    lines = [ln for ln in tail if ln][-limit:]
    return " / ".join(lines) if lines else "no output"


# --------------------------------------------------------------------------- #
# Probes — each fast, silent and read-only
# --------------------------------------------------------------------------- #


def _read_os_release() -> Dict[str, str]:
    """Parse ``/etc/os-release`` so Linux hints name the right package manager."""
    values = {}  # type: Dict[str, str]
    try:
        with open("/etc/os-release", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                name, sep, value = line.strip().partition("=")
                if sep:
                    values[name] = value.strip().strip('"').strip("'")
    except Exception:
        return {}
    return values


def _linux_install_command(package: str = "gphoto2") -> str:
    """Best-guess distro command. Shown to the user; never executed by us."""
    info = _read_os_release()
    family = " ".join([info.get("ID", ""), info.get("ID_LIKE", "")]).lower()
    if any(name in family for name in ("debian", "ubuntu", "mint", "raspbian")):
        return "sudo apt install " + package
    if any(name in family for name in ("fedora", "rhel", "centos", "almalinux")):
        return "sudo dnf install " + package
    if "arch" in family or "manjaro" in family:
        return "sudo pacman -S " + package
    if "suse" in family:
        return "sudo zypper install " + package
    if "alpine" in family:
        return "sudo apk add " + package
    return "install the '%s' package with your distribution's package manager" % package


def _probe_gphoto2() -> Dependency:
    """gphoto2 CLI — the only way to reach a pre-PTP camera body directly."""
    dep = Dependency(key=KEY_GPHOTO2, label="gphoto2")
    binary = which("gphoto2")
    if not binary:
        if _IS_MACOS:
            brew = which("brew")
            dep.can_autoinstall = bool(brew)
            dep.hint = (
                "Not installed. Press Install to run 'brew install gphoto2'."
                if brew
                else "Not installed, and Homebrew is missing. Install Homebrew "
                "from %s first." % _HOMEBREW_URL
            )
        elif _IS_WINDOWS:
            # Kept honest rather than hopeful: there is no supported Windows
            # build that can claim a USB camera without replacing its driver.
            dep.hint = (
                "There is no supported gphoto2 build for Windows. Use a memory "
                "card reader, or the WSL2 bridge."
            )
        else:
            dep.hint = "Not installed. Run: " + _linux_install_command("gphoto2")
        return dep

    # --version is a pure version print: it opens no port and touches no camera.
    returncode, out, err = _run([binary, "--version"], timeout=15.0)
    if returncode != 0:
        dep.hint = (
            "Found at %s but it would not run (%s)." % (binary, _first_line(err or out))
            if returncode is not None
            else "Found at %s but it did not respond (%s)." % (binary, _first_line(err))
        )
        return dep

    dep.present = True
    match = re.search(r"^gphoto2\s+(\d[\w.\-]*)", out, re.MULTILINE)
    dep.version = match.group(1) if match else ""
    dep.hint = binary
    return dep


def _probe_homebrew() -> Dependency:
    """Homebrew — not used directly, but it is what makes gphoto2 installable."""
    dep = Dependency(key=KEY_HOMEBREW, label="Homebrew")
    binary = which("brew")
    if not binary:
        # Installing Homebrew means running its official shell script, which
        # this app will not do on the user's behalf. We show the command.
        dep.hint = "Not installed. See %s — the official command is: %s" % (
            _HOMEBREW_URL,
            _HOMEBREW_INSTALL_SH,
        )
        return dep
    dep.present = True
    # `brew --version` reads a local git checkout and opens no socket, but the
    # opt-out variables are passed anyway: this probe runs unprompted at every
    # startup, and the README promises that nothing before an explicit Install
    # touches the network. Stating that to Homebrew costs nothing and keeps the
    # promise true even if a future brew release decides --version is a good
    # moment to auto-update.
    returncode, out, _ = _run(
        [binary, "--version"],
        timeout=15.0,
        extra_env={
            "HOMEBREW_NO_AUTO_UPDATE": "1",
            "HOMEBREW_NO_ANALYTICS": "1",
            "HOMEBREW_NO_ENV_HINTS": "1",
        },
    )
    if returncode == 0:
        match = re.search(r"Homebrew\s+([\w.\-]+)", out)
        dep.version = match.group(1) if match else ""
    dep.hint = binary
    return dep


def _probe_pillow() -> Dependency:
    """Pillow — optional, and only ever an *upgrade* to verification strength."""
    dep = Dependency(
        key=KEY_PILLOW,
        label="Pillow",
        can_autoinstall=True,
        hint="Optional. Without it, downloads are checked structurally "
        "(size and JPEG markers) but not decoded.",
    )
    try:
        import importlib.util

        if importlib.util.find_spec("PIL") is None:
            return dep
    except Exception:
        return dep

    try:
        # Import PIL.Image, not just PIL: a Pillow whose compiled _imaging
        # module is broken satisfies find_spec but cannot decode anything, and
        # verify.py needs Image.open(), not package metadata.
        from PIL import Image  # noqa: F401
        import PIL
    except Exception as exc:
        dep.hint = "Installed but not usable (%s). Press Install to repair it." % (
            _first_line(str(exc)),
        )
        return dep

    dep.present = True
    dep.can_autoinstall = False
    dep.version = str(getattr(PIL, "__version__", ""))
    dep.hint = "Downloads are fully decoded before a delete is allowed."
    return dep


def _probe_pywin32() -> Dependency:
    """pywin32 — the WIA backend's only dependency. Windows-only."""
    dep = Dependency(
        key=KEY_PYWIN32,
        label="pywin32",
        can_autoinstall=True,
        hint="Needed to see cameras through Windows itself (WIA). "
        "Card readers work without it.",
    )
    try:
        import importlib.util

        # win32com.client is the entry point the WIA backend uses; pythoncom is
        # the DLL-backed half that actually fails when an install is broken.
        for module in ("pythoncom", "win32com.client"):
            if importlib.util.find_spec(module) is None:
                return dep
    except Exception:
        return dep

    dep.present = True
    dep.can_autoinstall = False
    dep.version = _distribution_version("pywin32")
    dep.hint = ""
    return dep


def _probe_wsl() -> Dependency:
    """WSL2 — the bridge that lets gphoto2 reach a camera body on Windows."""
    dep = Dependency(
        key=KEY_WSL,
        label="WSL2",
        hint="Not installed. In an Administrator PowerShell run: wsl --install "
        "(a reboot is required). Only needed for the advanced camera bridge.",
    )
    binary = _wsl_exe()
    if not binary:
        return dep

    # '--list --quiet' is machine-readable and does not boot the WSL VM.
    # '--status' would be easier to read and impossible to parse: it is
    # localized prose.
    returncode, out, _ = _run(
        [binary, "--list", "--quiet"], timeout=15.0, extra_env={"WSL_UTF8": "1"}
    )
    distros = [line.strip() for line in out.splitlines() if line.strip()]
    if returncode != 0 or not distros:
        # wsl.exe exists on every modern Windows as an app-execution alias even
        # when WSL is not installed, so its presence proves nothing.
        return dep

    dep.present = True
    dep.version = ", ".join(distros[:4])
    dep.hint = ""
    return dep


def _probe_usbipd() -> Dependency:
    """usbipd-win — attaches a USB camera to WSL. Windows-only."""
    dep = Dependency(
        key=KEY_USBIPD,
        label="usbipd-win",
        can_autoinstall=True,
        hint="Not installed. Press Install to run winget; Windows will ask for "
        "administrator rights because a driver is involved.",
    )
    binary = which("usbipd")
    if not binary:
        return dep

    dep.present = True
    dep.can_autoinstall = False
    returncode, out, err = _run([binary, "--version"], timeout=15.0)
    if returncode == 0:
        match = re.search(r"(\d+\.\d+\.\d+[\w.\-]*)", out or err)
        dep.version = match.group(1) if match else ""
    dep.hint = binary
    return dep


def _distribution_version(name: str) -> str:
    """Installed version from package metadata, or '' — never raises."""
    try:
        import importlib.metadata as md

        return str(md.version(name))
    except Exception:
        return ""


def _first_line(text: str, limit: int = 160) -> str:
    """First meaningful line of tool output, trimmed for a one-line message."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""


def _wsl_exe() -> Optional[str]:
    """Path to wsl.exe, honouring WOW64 redirection.

    A 32-bit Python on 64-bit Windows has System32 redirected to SysWOW64,
    which contains no wsl.exe; 'Sysnative' is the documented escape hatch.
    """
    if not _IS_WINDOWS:
        return None
    root = os.environ.get("SystemRoot", r"C:\Windows")
    subdir = "Sysnative" if sys.maxsize <= 2**32 else "System32"
    candidate = os.path.join(root, subdir, "wsl.exe")
    if os.path.isfile(candidate):
        return candidate
    return which("wsl")


def check_all() -> List[Dependency]:
    """Probe everything relevant to *this* machine, best-first.

    Only dependencies that could plausibly become present are listed: gphoto2 is
    omitted on Windows, because no amount of clicking will produce a working
    build there, and a permanently red row teaches the user to ignore the panel.
    The Windows story for a pre-PTP camera is told by the backend status list
    instead.

    Results are never cached — the user is expected to install something and
    press Refresh.
    """
    if _IS_WINDOWS:
        probes = (_probe_pywin32, _probe_pillow, _probe_wsl, _probe_usbipd)
    elif _IS_MACOS:
        probes = (_probe_gphoto2, _probe_homebrew, _probe_pillow)
    else:
        probes = (_probe_gphoto2, _probe_pillow)

    results = []  # type: List[Dependency]
    for probe in probes:
        try:
            results.append(probe())
        except Exception as exc:  # a probe bug must not empty the whole panel
            results.append(
                Dependency(
                    key=getattr(probe, "__name__", "unknown").replace("_probe_", ""),
                    label="unknown",
                    hint="Could not be checked: %s" % _first_line(str(exc)),
                )
            )
    return results


# --------------------------------------------------------------------------- #
# Assisted installation
# --------------------------------------------------------------------------- #


def _pip_command(package: str) -> List[str]:
    """pip argv for a user-scoped install.

    ``--user`` is the whole point — it keeps us out of system directories and
    therefore out of sudo — but it is rejected outright inside a virtualenv
    ("Can not perform a '--user' install"). Inside a venv the venv *is* the
    user's private site, so dropping the flag preserves the guarantee instead of
    breaking the command.
    """
    argv = [sys.executable, "-m", "pip", "install"]
    in_venv = getattr(sys, "base_prefix", sys.prefix) != sys.prefix
    if not in_venv:
        argv.append("--user")
    argv.append(package)
    return argv


def _explain_pip_failure(tail: Sequence[str]) -> str:
    """Turn pip's most common refusals into something actionable."""
    text = " ".join(tail).lower()
    if "externally-managed-environment" in text or "externally managed" in text:
        return (
            "This Python is managed by your operating system and refuses pip "
            "installs (PEP 668). Either install the package with your system "
            "package manager, or run RetroCam from a virtual environment."
        )
    if "no module named pip" in text:
        return (
            "pip is not available for %s. Install pip for this interpreter, or "
            "install the package with your system package manager." % sys.executable
        )
    if "could not find a version" in text or "no matching distribution" in text:
        return "No installable build was found for this Python version and platform."
    if any(
        marker in text
        for marker in (
            "network",
            "timed out",
            "temporary failure",
            "connection",
            "proxy",
            "ssl",
        )
    ):
        return "The download failed — check the internet connection and try again."
    if "permission denied" in text or "access is denied" in text:
        return (
            "Permission denied. RetroCam never installs with administrator "
            "rights; install the package yourself from a terminal."
        )
    return ""


def _install_gphoto2(progress: ProgressCallback) -> Tuple[bool, str]:
    """Install gphoto2 via Homebrew. macOS only, never elevated."""
    if _IS_WINDOWS:
        return (
            False,
            "There is no supported gphoto2 build for Windows. Use a memory card "
            "reader (the fastest and safest route), or set up the WSL2 bridge.",
        )
    if not _IS_MACOS:
        return (
            False,
            "RetroCam does not install system packages on Linux. Run this "
            "yourself in a terminal: %s" % _linux_install_command("gphoto2"),
        )

    brew = which("brew")
    if not brew:
        return (
            False,
            "Homebrew is required and was not found. Install it from %s — the "
            "official command is: %s — then press Install again."
            % (_HOMEBREW_URL, _HOMEBREW_INSTALL_SH),
        )

    # No auto-update: it turns a 30-second install into a multi-minute git
    # operation that fails on its own for reasons unrelated to gphoto2.
    env = {
        "HOMEBREW_NO_AUTO_UPDATE": "1",
        "HOMEBREW_NO_ANALYTICS": "1",
        "HOMEBREW_NO_COLOR": "1",
        "HOMEBREW_NO_EMOJI": "1",
        "HOMEBREW_NO_ENV_HINTS": "1",
    }
    returncode, tail, timed_out = _stream(
        [brew, "install", "gphoto2"],
        KEY_GPHOTO2,
        progress,
        timeout=1800.0,  # a cold install pulls libgphoto2, libusb and friends
        extra_env=env,
    )
    if timed_out:
        return (
            False,
            "Homebrew took too long and was stopped. Try again, or run "
            "'brew install gphoto2' in a terminal to see where it hangs.",
        )
    if returncode is None:
        return False, "Could not start Homebrew: %s" % _quote_tail(tail)
    if returncode != 0:
        return False, "Homebrew failed (exit %d): %s" % (returncode, _quote_tail(tail))

    # Trust the re-probe, not the exit code.
    dep = _probe_gphoto2()
    if not dep.present:
        return (
            False,
            "Homebrew reported success but gphoto2 still cannot be run. Open a "
            "terminal and check 'brew doctor'.",
        )
    return True, "gphoto2 %s installed." % (dep.version or "").strip()


def _install_pip_package(
    key: str, package: str, progress: ProgressCallback
) -> Tuple[bool, str]:
    """Shared body for the two pip-installable extras."""
    argv = _pip_command(package)
    returncode, tail, timed_out = _stream(argv, key, progress, timeout=900.0)
    if timed_out:
        return (
            False,
            "The install took too long and was stopped. Check the "
            "internet connection and try again.",
        )
    if returncode is None:
        return False, "Could not start pip: %s" % _quote_tail(tail)
    if returncode != 0:
        explanation = _explain_pip_failure(tail)
        if explanation:
            return False, explanation
        return False, "pip failed (exit %d): %s" % (returncode, _quote_tail(tail))
    return True, ""


def _install_pillow(progress: ProgressCallback) -> Tuple[bool, str]:
    ok, message = _install_pip_package(KEY_PILLOW, "Pillow", progress)
    if not ok:
        return False, message
    dep = _probe_pillow()
    if not dep.present:
        return (
            False,
            "pip reported success but Pillow still cannot be imported. Restart "
            "RetroCam; if it persists, the install went to a different Python.",
        )
    return True, "Pillow %s installed — downloads will now be fully decoded." % (
        dep.version or ""
    )


def _install_pywin32(progress: ProgressCallback) -> Tuple[bool, str]:
    if not _IS_WINDOWS:
        return False, "pywin32 only exists on Windows."
    ok, message = _install_pip_package(KEY_PYWIN32, "pywin32", progress)
    if not ok:
        return False, message
    dep = _probe_pywin32()
    if not dep.present:
        return (
            False,
            "pip reported success but pywin32 cannot be imported yet. Restart "
            "RetroCam. If it still fails, run this once from a terminal: "
            '"%s" -m pywin32_postinstall -install' % sys.executable,
        )
    return True, "pywin32 %s installed — restart RetroCam to enable WIA." % (
        dep.version or ""
    )


def _install_usbipd(progress: ProgressCallback) -> Tuple[bool, str]:
    """Install usbipd-win via winget. Windows will prompt for elevation."""
    if not _IS_WINDOWS:
        return False, "usbipd-win only exists on Windows."

    winget = which("winget")
    if not winget:
        return (
            False,
            "winget (App Installer) was not found. Install usbipd-win manually "
            "from %s, then restart RetroCam." % _USBIPD_RELEASES_URL,
        )

    # The exact package id, not the bare name: a bare 'winget install usbipd'
    # can match several packages and then wait forever for a choice we cannot
    # give it (stdin is closed on purpose).
    argv = [
        winget,
        "install",
        "--exact",
        "--id",
        "dorssel.usbipd-win",
        "--source",
        "winget",
        "--accept-package-agreements",
        "--accept-source-agreements",
    ]
    _emit(
        progress,
        KEY_USBIPD,
        "Windows will show an administrator (UAC) prompt — usbipd installs a "
        "driver. RetroCam cannot and will not answer it for you.",
    )
    returncode, tail, timed_out = _stream(argv, KEY_USBIPD, progress, timeout=1800.0)
    if timed_out:
        return (
            False,
            "winget took too long and was stopped. Run 'winget install "
            "--exact --id dorssel.usbipd-win' in a terminal to see why.",
        )
    if returncode is None:
        return False, "Could not start winget: %s" % _quote_tail(tail)
    if returncode != 0:
        return (
            False,
            "winget failed (exit %d): %s. If the administrator prompt was "
            "declined or blocked by policy, install usbipd-win manually from %s."
            % (returncode, _quote_tail(tail), _USBIPD_RELEASES_URL),
        )
    return (
        True,
        "usbipd-win installed. Restart RetroCam (or sign out and back in) so "
        "the 'usbipd' command appears on PATH.",
    )


def install(key: str, progress: ProgressCallback = noop_progress) -> Tuple[bool, str]:
    """Try to install one dependency, reporting progress as it goes.

    Returns ``(ok, message)``. ``ok`` is True only when a fresh probe confirms
    the dependency is now usable — an installer's exit code is a claim, not
    evidence. The message is one or two sentences meant to be shown verbatim.

    Every line the installer prints is forwarded as
    ``Progress(phase='deps', message=line)`` so the log pane shows the real
    work. Nothing here escalates privileges: if a step needs administrator
    rights (usbipd) or a manual choice (Homebrew, WSL2), this returns False with
    the instructions instead.
    """
    normalized = (key or "").strip().lower()
    handlers = {
        KEY_GPHOTO2: _install_gphoto2,
        KEY_PILLOW: _install_pillow,
        KEY_PYWIN32: _install_pywin32,
        KEY_USBIPD: _install_usbipd,
    }
    handler = handlers.get(normalized)
    if handler is None:
        if normalized == KEY_HOMEBREW:
            return (
                False,
                "RetroCam will not install Homebrew for you — that means running "
                "a script downloaded from the internet. Install it from %s: %s"
                % (_HOMEBREW_URL, _HOMEBREW_INSTALL_SH),
            )
        if normalized == KEY_WSL:
            return (
                False,
                "WSL2 needs administrator rights and a reboot. In an "
                "Administrator PowerShell run: wsl --install",
            )
        return (
            False,
            "Nothing can be installed for '%s'. Installable: %s."
            % (key, ", ".join(INSTALLABLE_KEYS)),
        )

    try:
        return handler(progress)
    except Exception as exc:  # last resort: an install must not crash the GUI
        return False, "The install failed unexpectedly: %s" % _first_line(str(exc))


# --------------------------------------------------------------------------- #
# Destination folders
# --------------------------------------------------------------------------- #


def _windows_downloads_dir() -> str:
    """Downloads via SHGetKnownFolderPath, which honours a relocated folder.

    ``%USERPROFILE%\\Downloads`` is wrong on any machine where the user moved
    Downloads to another drive — common exactly on the machines with room for a
    photo rescue.
    """
    try:
        import ctypes
        from ctypes import wintypes

        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        # FOLDERID_Downloads {374DE290-123F-4565-9164-39C4925E467B}
        folder_id = _GUID(
            0x374DE290,
            0x123F,
            0x4565,
            (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B),
        )
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        shell32.SHGetKnownFolderPath.argtypes = [
            ctypes.POINTER(_GUID),
            wintypes.DWORD,
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole32.CoTaskMemFree.restype = None

        out = ctypes.c_wchar_p()
        # KF_FLAG_DEFAULT (0): return the path, do not create the folder.
        hresult = shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, None, ctypes.byref(out)
        )
        try:
            if hresult != 0 or not out.value:
                return ""
            return str(out.value)
        finally:
            ole32.CoTaskMemFree(out)
    except Exception:
        return ""


def _linux_downloads_dir() -> str:
    """Downloads per the XDG user-dirs spec.

    The folder is localized: on an Italian desktop it is ``~/Scaricati``, and
    hard-coding ``~/Downloads`` would quietly create a second, wrong folder.
    """
    from_env = os.environ.get("XDG_DOWNLOAD_DIR", "").strip()
    if from_env:
        return os.path.expandvars(os.path.expanduser(from_env))

    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    config_file = os.path.join(config_home, "user-dirs.dirs")
    try:
        with open(config_file, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith("XDG_DOWNLOAD_DIR"):
                    continue
                _, sep, value = line.partition("=")
                if not sep:
                    continue
                value = value.strip().strip('"').strip("'")
                if not value:
                    continue
                value = value.replace("$HOME", os.path.expanduser("~"))
                return os.path.expandvars(os.path.expanduser(value))
    except Exception:
        pass
    return ""


def default_download_dir() -> str:
    """The system Downloads folder, or the best available fallback.

    Falls back to the home directory and finally to the current directory, so
    the GUI always has somewhere to point at. Never raises, never creates
    anything: picking a folder must not have side effects.
    """
    candidates = []  # type: List[str]
    try:
        if _IS_WINDOWS:
            known = _windows_downloads_dir()
            if known:
                candidates.append(known)
            profile = os.environ.get("USERPROFILE")
            if profile:
                candidates.append(os.path.join(profile, "Downloads"))
        elif not _IS_MACOS:
            xdg = _linux_downloads_dir()
            if xdg:
                candidates.append(xdg)

        home = os.path.expanduser("~")
        candidates.append(os.path.join(home, "Downloads"))

        for candidate in candidates:
            try:
                if candidate and os.path.isdir(candidate):
                    return os.path.abspath(candidate)
            except Exception:
                continue

        if home and os.path.isdir(home):
            return os.path.abspath(home)
    except Exception:
        pass

    try:
        return os.path.abspath(os.getcwd())
    except Exception:
        return "."


#: Vendor words worth dropping from a folder name. 'Canon PowerShot S30'
#: identifies itself perfectly well as 'PowerShot_S30'.
_VENDOR_WORDS = frozenset(
    [
        "canon",
        "nikon",
        "sony",
        "olympus",
        "fujifilm",
        "fuji",
        "kodak",
        "casio",
        "pentax",
        "ricoh",
        "panasonic",
        "minolta",
        "konica",
        "samsung",
        "sigma",
        "leica",
        "epson",
        "agfa",
        "toshiba",
        "sanyo",
        "hp",
        "jvc",
        "apple",
    ]
)

_MAX_MODEL_CHARS = 60


def _sanitize_model(camera_model: str) -> str:
    """Model name reduced to a safe, readable folder component.

    Restricted to ``[A-Za-z0-9._-]`` because this string becomes a path on three
    filesystems with three different opinions about spaces, colons and
    backslashes, and a rescue must not fail on the folder name.
    """
    raw = (camera_model or "").strip()
    if not raw:
        return "Camera"

    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", raw)
    cleaned = re.sub(r"_{2,}", "_", cleaned).strip("_.-")
    if not cleaned:
        return "Camera"

    tokens = cleaned.split("_")
    # Strip a leading vendor word, but only while the rest still identifies the
    # camera: 'Kodak 4800' must not become '4800', and 'Canon' must stay 'Canon'.
    if len(tokens) > 1 and tokens[0].lower() in _VENDOR_WORDS:
        remainder = tokens[1:]
        joined = "_".join(remainder)
        if len(joined) >= 3 and any(ch.isalpha() for ch in joined):
            tokens = remainder

    result = "_".join(token for token in tokens if token)[:_MAX_MODEL_CHARS]
    result = result.strip("_.-")
    return result or "Camera"


def suggested_dest(camera_model: str, base: str = "") -> str:
    """Propose ``<Downloads>/<Model>_<YYYY-MM-DD>`` for this rescue.

    For a Canon PowerShot S30 on 13 August 2026 this returns
    ``<Downloads>/PowerShot_S30_2026-08-13``.

    The path is *not* created — the GUI shows it, the user may change it, and
    the transfer engine creates it only when a download actually starts. It is
    also deliberately not made unique: re-running on the same day should land in
    the same folder, where ``skip_existing`` can resume an interrupted rescue
    instead of scattering half-copies across four directories.
    """
    try:
        parent = (base or "").strip()
        if parent:
            parent = os.path.abspath(os.path.expanduser(os.path.expandvars(parent)))
        else:
            parent = default_download_dir()
        name = "%s_%s" % (
            _sanitize_model(camera_model),
            datetime.now().strftime("%Y-%m-%d"),
        )
        return os.path.join(parent, name)
    except Exception:
        # Still better than an exception in front of someone rescuing photos.
        return os.path.join(
            default_download_dir(), "Camera_%s" % datetime.now().strftime("%Y-%m-%d")
        )
