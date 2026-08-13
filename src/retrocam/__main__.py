"""``python -m retrocam`` — argument parsing, then the window.

Why this exists as a separate module: ``run.sh`` / ``run.bat`` must be able to
start the application straight from a checkout, with nothing installed and only
``PYTHONPATH=src`` set. That rules out console-script shims, which only exist
after a ``pip install``.

Three jobs, in order of how often they matter:

1. Open the GUI. That is what almost every run does, and it is the default.
2. Explain a missing Tkinter *in words the user can act on*. Some Linux
   distributions ship Python without Tk, and so do a few macOS Python builds;
   the raw failure is an ``ImportError`` mentioning ``_tkinter``, which tells a
   non-programmer nothing at all.
3. Offer ``--cli``: a strictly read-only detect-and-list that prints what the
   program can see. It exists so that "it does not find my camera" can be
   answered by pasting one block of text into a bug report, and so the
   detection path can be exercised on a machine with no display at all.

``--cli`` never downloads and never deletes. There is no headless path to a
destructive operation in this program, deliberately: erasing a card is a
decision that belongs in front of a confirmation dialog.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional, Sequence

from .i18n import set_language, t

#: Used when the package carries no ``__version__`` and is not pip-installed.
#: A visibly fake number is better than a plausible wrong one in a bug report.
_FALLBACK_VERSION = "0.0.0"

#: How many file names ``--cli`` prints per camera before collapsing the rest.
#: A 1 GB card holds hundreds of photos, and the point of the listing is the
#: shape of the archive rather than every name in it.
_CLI_FILE_LIMIT = 40


def _version() -> str:
    """The application version, from whichever source actually knows it."""
    try:
        from . import __version__  # type: ignore[attr-defined]

        if __version__:
            return str(__version__)
    except Exception:
        pass
    try:
        from .app import APP_VERSION

        return str(APP_VERSION)
    except Exception:
        # app.py imports tkinter, so this branch is the normal path on a machine
        # with no Tk — exactly the machine most likely to be asking --version.
        pass
    try:
        import importlib.metadata as metadata

        return str(metadata.version("retrocam-rescue"))
    except Exception:
        return _FALLBACK_VERSION


def _human_bytes(n: int) -> str:
    """Byte count for a console line, or ``'?'`` when unknown."""
    if n is None or n < 0:
        return "?"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0 or unit == "GB":
            return "%d B" % int(value) if unit == "B" else "%.1f %s" % (value, unit)
        value /= 1024.0
    return "%.1f GB" % value  # pragma: no cover - unreachable


def _tk_hint() -> str:
    """Platform-specific instructions for installing Tk."""
    if sys.platform == "darwin":
        return t("main.tk_hint_macos")
    if os.name == "nt":
        return t("main.tk_hint_windows")
    if sys.platform.startswith("linux"):
        return t("main.tk_hint_linux")
    return t("main.tk_hint_generic")


def _invocation() -> str:
    """How the user started us, so the ``--cli`` suggestion is copy-pasteable."""
    try:
        executable = os.path.basename(sys.executable) or "python3"
    except Exception:  # pragma: no cover - defensive
        executable = "python3"
    return "%s -m retrocam" % executable


def _print(text: str, stream: Optional[object] = None) -> None:
    """Print without ever dying on a console that cannot encode a character.

    A Windows console in a legacy code page raises ``UnicodeEncodeError`` on
    perfectly ordinary accented text, and losing an accent is much better than
    losing the message.
    """
    target = stream if stream is not None else sys.stdout
    try:
        print(text, file=target)  # type: ignore[arg-type]
    except Exception:
        try:
            encoding = getattr(target, "encoding", None) or "ascii"
            print(
                text.encode(encoding, "replace").decode(encoding, "replace"),
                file=target,  # type: ignore[arg-type]
            )
        except Exception:
            pass


def _fatal(message: str, title: str = "") -> None:
    """Report a startup failure on stderr, and in a dialog when one is possible.

    The dialog matters: a shortcut-launched or double-clicked app has no console
    at all, so a stderr-only message is indistinguishable from the program
    silently refusing to start.
    """
    _print(message, sys.stderr)
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror(title or t("error.title"), message)
        root.destroy()
    except Exception:
        # No Tk — often the very reason we are here. stderr already has it.
        pass


# --------------------------------------------------------------------------- #
# Headless mode
# --------------------------------------------------------------------------- #


def _run_cli() -> int:
    """Detect cameras and list their files. Strictly read-only.

    Returns 0 when at least one camera was found, 1 when none was — so a script
    can branch on it — and 2 when the program could not run at all.
    """
    try:
        from . import deps, registry
        from .model import CameraError, Progress
    except Exception as exc:
        _print(t("main.import_failed", err=exc), sys.stderr)
        return 2

    _print(t("cli.header", version=_version()))
    _print("")

    # -- environment ------------------------------------------------------- #
    _print(t("cli.env"))
    try:
        for dependency in deps.check_all():
            _print(
                t(
                    "cli.env_row",
                    mark="x" if dependency.present else " ",
                    label=dependency.label,
                    version=dependency.version or "",
                    hint=dependency.hint or "",
                ).rstrip()
            )
    except Exception as exc:
        _print("  %s" % exc)
    _print("")

    # -- transports -------------------------------------------------------- #
    _print(t("cli.backends"))
    try:
        for backend_cls, ok, hint in registry.backend_status():
            name = getattr(backend_cls, "display_name", "") or backend_cls.__name__
            _print(
                t(
                    "cli.backend_row",
                    mark="x" if ok else " ",
                    name=name,
                    hint="" if ok else "- %s" % (hint or ""),
                ).rstrip()
            )
    except Exception as exc:
        _print("  %s" % exc)
    _print("")

    # -- detection --------------------------------------------------------- #
    _print(t("cli.detecting"))

    def echo(item: Progress) -> None:
        if item.message:
            _print("  %s" % item.message)

    try:
        found = registry.detect_all(echo)
    except CameraError as exc:
        _print("  %s" % exc, sys.stderr)
        return 2
    except Exception as exc:
        _print(t("error.unexpected", kind=type(exc).__name__, err=exc), sys.stderr)
        return 2

    _print("")
    if not found:
        _print(t("cli.no_camera"))
        _print("")
        _print(t("cli.readonly"))
        return 1

    for number, (backend, camera) in enumerate(found, start=1):
        _print(
            t(
                "cli.camera",
                n=number,
                model=camera.model,
                port=camera.port or "-",
                backend=getattr(backend, "display_name", "") or type(backend).__name__,
            )
        )
        if camera.detail:
            _print(t("cli.detail", detail=camera.detail))

        _print(t("cli.listing"))
        try:
            # Listing is read-only by contract; nothing below writes to the card.
            files = list(backend.list_files(camera))
        except CameraError as exc:
            _print(t("cli.list_failed", err=exc))
            continue
        except Exception as exc:
            _print(t("cli.list_failed", err="%s: %s" % (type(exc).__name__, exc)))
            continue

        known = sum(f.size for f in files if f.size_known)
        _print(t("cli.files", n=len(files), size=_human_bytes(known) if known else "?"))
        for camera_file in files[:_CLI_FILE_LIMIT]:
            _print(
                t(
                    "cli.file",
                    folder=camera_file.folder.rstrip("/"),
                    name=camera_file.name,
                    size=_human_bytes(camera_file.size),
                )
            )
        if len(files) > _CLI_FILE_LIMIT:
            _print(t("cli.more", n=len(files) - _CLI_FILE_LIMIT))
        _print("")

    _print(t("cli.readonly"))
    return 0


# --------------------------------------------------------------------------- #
# Argument parsing and dispatch
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    """The three optional arguments, described in the user's own language."""
    parser = argparse.ArgumentParser(prog="retrocam", description=t("main.description"))
    parser.add_argument(
        "--lang",
        choices=("it", "en", "auto"),
        default="auto",
        help=t("main.help_lang"),
    )
    parser.add_argument("--version", action="store_true", help=t("main.help_version"))
    parser.add_argument("--cli", action="store_true", help=t("main.help_cli"))
    return parser


def _preset_language(argv: List[str]) -> None:
    """Apply ``--lang`` early, accepting both ``--lang it`` and ``--lang=it``.

    Done by hand before argparse runs because the parser's own help text is
    built from translated strings: waiting for argparse would mean ``--lang it
    --help`` printing English help.
    """
    for index, token in enumerate(argv):
        value = ""
        if token.startswith("--lang="):
            value = token.split("=", 1)[1]
        elif token == "--lang" and index + 1 < len(argv):
            value = argv[index + 1]
        if value:
            set_language(value)
            return


def _run_gui() -> int:
    """Import the GUI and run it, turning both plausible failures into prose."""
    try:
        from .app import run_gui
    except ImportError as exc:
        # Two very different causes land here. A missing ``_tkinter`` is the
        # common one and has a fix the user can carry out; anything else is a
        # broken or half-copied installation and is reported as such rather
        # than mislabelled as a Tk problem.
        text = str(exc)
        if "tkinter" in text.lower():
            _fatal(
                t("main.tk_missing", err=text, hint=_tk_hint(), argv0=_invocation()),
                t("main.tk_missing_title"),
            )
            return 3
        _fatal(t("main.import_failed", err=text))
        return 3
    except Exception as exc:  # pragma: no cover - broken install
        _fatal(t("main.import_failed", err=exc))
        return 3

    try:
        return run_gui()
    except KeyboardInterrupt:  # pragma: no cover - Ctrl-C in a terminal
        _print("")
        _print(t("cli.interrupted"))
        return 130
    except Exception as exc:
        # The GUI catches its own errors; reaching here means Tk itself failed
        # (no display, no window server), which is a startup problem.
        _fatal(t("main.gui_failed", err=exc))
        return 3


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse arguments and start either the window or the headless listing."""
    raw = list(sys.argv[1:] if argv is None else argv)
    _preset_language(raw)

    args = _build_parser().parse_args(raw)
    set_language(args.lang)

    if args.version:
        _print("%s %s" % (t("app.title"), _version()))
        return 0

    if args.cli:
        try:
            return _run_cli()
        except KeyboardInterrupt:
            _print("")
            _print(t("cli.interrupted"))
            return 130

    return _run_gui()


if __name__ == "__main__":
    sys.exit(main())
