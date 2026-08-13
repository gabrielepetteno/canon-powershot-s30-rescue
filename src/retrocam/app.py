"""The RetroCam Rescue window: a numbered, top-to-bottom rescue flow.

The person using this program is typically not technical, is often anxious, and
is holding hardware older than some of the people who will read this file. The
layout is therefore a single vertical column of numbered steps — environment,
camera, destination, download, and only then deletion — so that "what do I do
now" is always answered by the next box down.

Threading
---------
Tkinter is not thread-safe: touching a widget from anything other than the
thread that created it produces crashes that look like corrupt cameras. So this
module obeys one rule without exception:

    **Every camera operation runs on a worker thread, and the worker's only way
    to reach the GUI is** :class:`queue.Queue`.

:meth:`RetroCamApp._run_async` is the single place a thread is started, and
:meth:`RetroCamApp._pump` — rescheduled forever with ``root.after()`` — is the
single place the queue is drained. Progress ticks and finished-job results are
the only two message kinds. No widget, no ``StringVar``, no ``messagebox`` is
ever touched from a worker; conversely, nothing on the GUI thread ever blocks on
a camera.

Only one operation runs at a time. That is a deliberate simplification and also
a safety property: two threads talking to one 20-year-old USB device is a
reliable way to wedge it, and a delete must never race a download.

Failure policy
--------------
The worker boundary catches ``BaseException``. A backend bug must surface as a
dialog the user can read, never as a silent dead window or a traceback — and
``_busy`` must be cleared no matter how the job ended, or the GUI would lock up
permanently with every button greyed out.
"""

from __future__ import annotations

import datetime
import os
import platform
import queue
import sys
import threading
from typing import Any, Callable, List, Optional, Sequence, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import deps, registry
from .backends.base import CameraBackend
from .i18n import set_language, t
from .model import (
    CameraError,
    CameraFile,
    CameraInfo,
    CancelToken,
    DeleteOutcome,
    Progress,
    TransferAborted,
)
from .transfer import TransferEngine, TransferReport

__all__ = ["RetroCamApp", "run_gui", "APP_VERSION"]


#: Shown in the title bar and the log header. Read from the package rather than
#: written here, so ``__init__.py`` stays the single source of truth that
#: pyproject.toml and the PyInstaller spec also read.
try:
    from . import __version__ as APP_VERSION
except Exception:  # pragma: no cover - a checkout without __init__.py
    APP_VERSION = "0.0.0"

#: How often the Tk main loop drains the worker queue. 100 ms is imperceptible
#: to a human and cheap enough to leave running for the whole session.
_POLL_MS = 100

#: Used instead of :data:`_POLL_MS` when the queue was still full after a drain,
#: so a burst of progress ticks catches up in a few frames rather than minutes.
_POLL_BUSY_MS = 15

#: Maximum messages handled per drain. A transfer emits thousands of ticks; an
#: unbounded loop here would freeze the window it is supposed to be updating.
_MAX_DRAIN = 250

#: Log pane ceiling. Older lines are dropped: a 4 GB card produces a lot of
#: lines, and an unbounded Text widget eventually makes the window sluggish.
_LOG_MAX_LINES = 4000

#: Above this many files, deletion asks twice. Confirming the loss of a dozen
#: photos is a decision; confirming the loss of a whole card is an event.
_LARGE_DELETE = 25

#: How long a quit waits for a cancelled worker to unwind. Long enough for a
#: backend to drop its temp file and finish an os.replace(); short enough that
#: a wedged USB stack cannot make the window refuse to close.
_QUIT_JOIN_SECONDS = 2.0

#: Minimum sensible window. Small enough for a 1280x800 laptop, large enough
#: that no step collapses into an unreadable sliver.
_MIN_WIDTH = 860
_MIN_HEIGHT = 700

_GLYPH_OK = "✔"  # heavy check mark
_GLYPH_WARN = "⚠"  # warning sign


def _now_hhmmss() -> str:
    """Timestamp for a log line. Saved logs are read hours later, in emails."""
    return datetime.datetime.now().strftime("%H:%M:%S")


def _backend_name(backend: Any) -> str:
    """Human-readable name of a backend instance or class."""
    name = getattr(backend, "display_name", "") or ""
    if name:
        return str(name)
    cls = backend if isinstance(backend, type) else type(backend)
    return cls.__name__


def _safe_supports_delete(engine: TransferEngine) -> bool:
    """Ask a transport whether it can erase, from a worker thread, safely.

    Called only off the GUI thread. It is a real question for the backend — the
    mass-storage one answers it by creating and removing a probe file on the
    card — so a failure here means "we could not establish that erasing works",
    which must read as False rather than as an exception that kills the job.
    """
    try:
        return bool(engine.backend.supports_delete())
    except Exception:
        return False


def _derived_font(base: str, larger: int = 0, bold: bool = False) -> Any:
    """A copy of a *named* Tk font, optionally larger and bold, or ``None``.

    Written the long way on purpose. Passing ``("TkDefaultFont", 17, "bold")``
    as a font looks like it works and does not: Tk reads the first element of
    such a tuple as a font *family*, no family is called ``TkDefaultFont``, and
    the label silently falls back to a generic face — which is exactly the "why
    does this app look wrong on a Mac" bug. Copying the named font keeps the
    platform's own UI typeface and only changes size and weight.
    """
    try:
        import tkinter.font as tkfont

        font = tkfont.nametofont(base).copy()
        if larger:
            size = int(font.cget("size"))
            # A negative size means pixels rather than points in Tk; growing it
            # means moving away from zero either way.
            font.configure(size=size + larger if size >= 0 else size - larger)
        if bold:
            font.configure(weight="bold")
        return font
    except Exception:
        return None


def _enable_dpi_awareness() -> None:
    """Ask Windows not to bitmap-scale the window into a blur.

    Purely cosmetic, and wrapped tightly: a missing DLL or an old Windows must
    cost a slightly fuzzy window, never a failed launch.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        # PROCESS_SYSTEM_DPI_AWARE. Available from Windows 8.1; the older
        # SetProcessDPIAware is the fallback for Windows 7/8.
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
        except Exception:
            pass


class RetroCamApp(object):
    """The single window, its state machine, and its worker-thread plumbing."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root

        # -- worker plumbing ----------------------------------------------- #
        #: Worker -> GUI. Tuples: ('progress', Progress) and
        #: ('done', callback, result, error).
        self._queue: "queue.Queue[Tuple[Any, ...]]" = queue.Queue()
        self._busy = False
        self._cancellable = False
        self._cancel = CancelToken()
        self._after_id: Optional[str] = None
        #: The running worker, kept only so :meth:`_on_close` can give a
        #: cancelled job a moment to unwind instead of being cut mid-write.
        self._worker: Optional[threading.Thread] = None

        # -- application state --------------------------------------------- #
        self._deps: List[deps.Dependency] = []
        self._backend_status: List[Tuple[Any, bool, str]] = []
        self._devices: List[Tuple[CameraBackend, CameraInfo]] = []
        self._engine: Optional[TransferEngine] = None

        #: ``None`` means "not listed yet"; ``[]`` means "listed, card empty".
        #: The difference decides whether pressing Download lists first or
        #: refuses outright, so it must not be collapsed into a falsy check.
        self._files: Optional[List[CameraFile]] = None
        self._report: Optional[TransferReport] = None

        #: Whether the selected transport can erase, answered on the worker.
        #: ``None`` means "not asked yet" and is treated as "no", because
        #: ``supports_delete()`` is not a cheap property: the mass-storage
        #: backend answers it by creating and removing a probe file *on the
        #: card*, which must never happen on the Tk thread. It is therefore
        #: computed beside the listing and the download, and only read here.
        self._delete_supported: Optional[bool] = None

        #: True once the user has typed or browsed a destination. From then on
        #: picking another camera must never overwrite their choice.
        self._dest_touched = False
        #: Guards the trace callback while *we* set the destination variable.
        self._setting_dest = False

        self._install_buttons: List[ttk.Button] = []

        self._build_ui()

        # A crash inside any Tk callback lands here instead of on stderr, where
        # a double-clicked app would swallow it and simply appear to do nothing.
        self.root.report_callback_exception = self._on_tk_exception

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._log(
            t(
                "log.started",
                version=APP_VERSION,
                platform=platform.platform(terse=True),
                python=platform.python_version(),
            )
        )

        self._pump()
        # Probing the environment shells out to brew/wsl/pip, so it waits until
        # the window is on screen: a blank grey rectangle for two seconds is
        # exactly how a rescue tool loses someone's trust.
        self.root.after(60, self._refresh_env)

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    def _apply_theme(self) -> None:
        """Pick the most native-looking ttk theme available here.

        The theme list is queried rather than assumed: ``aqua`` only exists on
        macOS, ``vista`` only on Windows, and asking for a missing one raises.
        ``clam`` is the best-looking portable fallback.
        """
        style = ttk.Style(self.root)
        try:
            names = list(style.theme_names())
        except Exception:
            return

        if sys.platform == "darwin":
            preferred = ("aqua", "clam", "default")
        elif os.name == "nt":
            preferred = ("vista", "winnative", "xpnative", "clam", "default")
        else:
            preferred = ("clam", "alt", "default")

        for name in preferred:
            if name in names:
                try:
                    style.theme_use(name)
                    break
                except Exception:
                    continue

        # Colour-blind users get the glyph as well as the colour, which is why
        # the environment rows carry a check/warning character too.
        try:
            style.configure("Ok.TLabel", foreground="#1a7f37")
            style.configure("Warn.TLabel", foreground="#b35c00")
            style.configure("Muted.TLabel", foreground="#5a5a5a")
        except Exception:
            pass
        try:
            style.configure("Primary.TButton", padding=(16, 10))
            style.configure("Danger.TButton", padding=(10, 6))
        except Exception:
            pass

    def _build_ui(self) -> None:
        """Lay out the whole window: header, five numbered steps, log."""
        self._apply_theme()

        self.root.title("%s %s" % (t("app.title"), APP_VERSION))
        self.root.minsize(_MIN_WIDTH, _MIN_HEIGHT)
        try:
            self.root.geometry("%dx%d" % (_MIN_WIDTH + 60, _MIN_HEIGHT + 60))
        except Exception:
            pass

        outer = ttk.Frame(self.root, padding=12)
        outer.grid(row=0, column=0, sticky="nsew")
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        # Only the log grows when the window does; the steps keep their height
        # so the flow never reflows under the user's cursor mid-transfer.
        outer.rowconfigure(6, weight=1)

        self._build_header(outer, 0)
        self._build_env(outer, 1)
        self._build_camera(outer, 2)
        self._build_dest(outer, 3)
        self._build_run(outer, 4)
        self._build_after(outer, 5)
        self._build_log(outer, 6)

        self._update_controls()

    def _build_header(self, parent: ttk.Frame, row: int) -> None:
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        frame.columnconfigure(0, weight=1)

        title = ttk.Label(frame, text=t("app.title"))
        heading = _derived_font("TkDefaultFont", larger=6, bold=True)
        if heading is not None:
            title.configure(font=heading)
        title.grid(row=0, column=0, sticky="w")

        ttk.Label(
            frame,
            text=t("app.tagline"),
            style="Muted.TLabel",
            wraplength=760,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))

    # -- step 1: environment ------------------------------------------- #

    def _build_env(self, parent: ttk.Frame, row: int) -> None:
        frame = ttk.LabelFrame(parent, text=t("env.section"), padding=10)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        frame.columnconfigure(0, weight=1)

        ttk.Label(
            frame,
            text=t("env.intro"),
            style="Muted.TLabel",
            wraplength=700,
            justify="left",
        ).grid(row=0, column=0, sticky="w")

        self._btn_env_refresh = ttk.Button(
            frame, text=t("env.refresh"), command=self._refresh_env
        )
        self._btn_env_refresh.grid(row=0, column=1, sticky="e", padx=(8, 0))

        self._env_rows = ttk.Frame(frame)
        self._env_rows.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self._env_rows.columnconfigure(2, weight=1)

        ttk.Label(self._env_rows, text=t("env.checking")).grid(
            row=0, column=0, sticky="w"
        )

    # -- step 2: camera ------------------------------------------------- #

    def _build_camera(self, parent: ttk.Frame, row: int) -> None:
        frame = ttk.LabelFrame(parent, text=t("camera.section"), padding=10)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        frame.columnconfigure(1, weight=1)

        ttk.Label(
            frame,
            text=t("camera.intro"),
            style="Muted.TLabel",
            wraplength=700,
            justify="left",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        self._btn_search = ttk.Button(
            frame, text=t("camera.search"), command=self._on_search
        )
        self._btn_search.grid(row=1, column=0, sticky="w")

        ttk.Label(frame, text=t("camera.device")).grid(
            row=1, column=1, sticky="e", padx=(12, 6)
        )

        self._device_var = tk.StringVar()
        self._combo_devices = ttk.Combobox(
            frame, textvariable=self._device_var, state="disabled", width=46
        )
        self._combo_devices.grid(row=1, column=2, sticky="ew")
        self._combo_devices.bind("<<ComboboxSelected>>", self._on_device_selected)

        self._camera_info_var = tk.StringVar(value=t("camera.none_selected"))
        ttk.Label(
            frame, textvariable=self._camera_info_var, wraplength=760, justify="left"
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))

    # -- step 3: destination -------------------------------------------- #

    def _build_dest(self, parent: ttk.Frame, row: int) -> None:
        frame = ttk.LabelFrame(parent, text=t("dest.section"), padding=10)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        frame.columnconfigure(0, weight=1)

        ttk.Label(
            frame,
            text=t("dest.intro"),
            style="Muted.TLabel",
            wraplength=700,
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self._dest_var = tk.StringVar()
        self._entry_dest = ttk.Entry(frame, textvariable=self._dest_var)
        self._entry_dest.grid(row=1, column=0, sticky="ew")

        self._btn_browse = ttk.Button(
            frame, text=t("dest.browse"), command=self._on_browse
        )
        self._btn_browse.grid(row=1, column=1, sticky="e", padx=(8, 0))

        # Set the initial suggestion before wiring the trace, so the very first
        # programmatic write cannot be mistaken for the user typing.
        self._set_dest(self._suggested_dest(""))
        try:
            self._dest_var.trace_add("write", self._on_dest_written)
        except Exception:  # pragma: no cover - ancient Tk
            self._dest_var.trace("w", self._on_dest_written)  # type: ignore[attr-defined]

    # -- step 4: download ------------------------------------------------ #

    def _build_run(self, parent: ttk.Frame, row: int) -> None:
        frame = ttk.LabelFrame(parent, text=t("run.section"), padding=10)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        frame.columnconfigure(1, weight=1)

        self._btn_download = ttk.Button(
            frame,
            text=t("run.start"),
            style="Primary.TButton",
            command=self._on_download,
        )
        self._btn_download.grid(row=0, column=0, sticky="w", rowspan=2, padx=(0, 12))

        self._progress = ttk.Progressbar(
            frame, orient="horizontal", mode="determinate", maximum=1, value=0
        )
        self._progress.grid(row=0, column=1, sticky="ew")

        self._btn_cancel = ttk.Button(
            frame, text=t("run.cancel"), command=self._on_cancel
        )
        self._btn_cancel.grid(row=0, column=2, sticky="e", padx=(12, 0))

        self._status_var = tk.StringVar(value=t("run.idle"))
        ttk.Label(
            frame, textvariable=self._status_var, wraplength=620, justify="left"
        ).grid(row=1, column=1, columnspan=2, sticky="w", pady=(6, 0))

    # -- step 5: after the download -------------------------------------- #

    def _build_after(self, parent: ttk.Frame, row: int) -> None:
        frame = ttk.LabelFrame(parent, text=t("after.section"), padding=10)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        frame.columnconfigure(1, weight=1)

        self._btn_delete = ttk.Button(
            frame,
            text=t("after.delete"),
            style="Danger.TButton",
            command=self._on_delete,
        )
        self._btn_delete.grid(row=0, column=0, sticky="w", padx=(0, 12))

        self._summary_var = tk.StringVar(value=t("after.none"))
        summary = ttk.Label(
            frame, textvariable=self._summary_var, wraplength=620, justify="left"
        )
        # The one line the user is waiting for; it earns the extra weight.
        strong = _derived_font("TkDefaultFont", larger=2, bold=True)
        if strong is not None:
            summary.configure(font=strong)
        summary.grid(row=0, column=1, sticky="w")

        self._delete_hint_var = tk.StringVar(value=t("after.hint_none"))
        ttk.Label(
            frame,
            textvariable=self._delete_hint_var,
            style="Muted.TLabel",
            wraplength=760,
            justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

    # -- log ------------------------------------------------------------- #

    def _build_log(self, parent: ttk.Frame, row: int) -> None:
        frame = ttk.LabelFrame(parent, text=t("log.section"), padding=10)
        frame.grid(row=row, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self._log_text = ScrolledText(frame, height=10, wrap="word", state="disabled")
        self._log_text.grid(row=0, column=0, sticky="nsew")
        try:
            # The whole named font, not a family: file names and byte counts
            # only line up in a monospaced face.
            self._log_text.configure(font="TkFixedFont")
        except Exception:
            pass

        self._btn_save_log = ttk.Button(
            frame, text=t("log.save"), command=self._on_save_log
        )
        self._btn_save_log.grid(row=1, column=0, sticky="e", pady=(8, 0))

    # ------------------------------------------------------------------ #
    # Worker-thread plumbing
    # ------------------------------------------------------------------ #

    def _run_async(
        self,
        work: Callable[[Callable[[Progress], None]], Any],
        on_done: Callable[[Any, Optional[BaseException]], None],
        cancellable: bool = False,
        status: str = "",
    ) -> bool:
        """Run ``work`` off the GUI thread and call ``on_done`` back on it.

        ``work`` receives one argument: a progress callback it may call as
        often as it likes, from its own thread. That callback only enqueues, so
        it is safe and non-blocking, as the :class:`~retrocam.model.Progress`
        contract requires.

        ``on_done(result, error)`` runs on the GUI thread with exactly one of
        the two set. It is called for *every* job, including one killed by an
        error nobody anticipated, because leaving ``_busy`` set would freeze the
        window with every control disabled.

        Returns False (and says so) when another operation is already running.
        """
        if self._busy:
            messagebox.showinfo(t("busy.title"), t("busy.body"), parent=self.root)
            return False

        self._busy = True
        self._cancellable = cancellable
        self._cancel.reset()
        if status:
            self._set_status(status)
        self._set_progress(0, 0)
        self._update_controls()

        sink = self._queue  # bound locally: the thread must not touch `self`

        def emit(item: Progress) -> None:
            sink.put(("progress", item))

        def runner() -> None:
            result: Any = None
            error: Optional[BaseException] = None
            try:
                result = work(emit)
            except BaseException as exc:  # noqa: BLE001 - see the docstring
                # Deliberately wider than Exception. Whatever a backend manages
                # to raise, the GUI must come back to life and say so.
                error = exc
            finally:
                sink.put(("done", on_done, result, error))

        thread = threading.Thread(target=runner, name="retrocam-worker", daemon=True)
        self._worker = thread
        thread.start()
        return True

    def _pump(self) -> None:
        """Drain the worker queue on the GUI thread, then reschedule.

        Bounded per tick so that a burst of progress messages cannot starve
        redraws — the progress bar exists to prove the program is alive, and it
        cannot do that if updating it is what blocks the event loop.
        """
        drained = 0
        saturated = False
        try:
            while drained < _MAX_DRAIN:
                message = self._queue.get_nowait()
                drained += 1
                self._dispatch(message)
        except queue.Empty:
            pass
        else:
            saturated = True
        finally:
            delay = _POLL_BUSY_MS if saturated else _POLL_MS
            self._after_id = self.root.after(delay, self._pump)

    def _dispatch(self, message: Sequence[Any]) -> None:
        """Handle one queued message. Must not raise: it runs inside the pump."""
        try:
            kind = message[0]
            if kind == "progress":
                self._on_progress(message[1])
            elif kind == "done":
                self._finish_job(message[1], message[2], message[3])
        except Exception as exc:  # pragma: no cover - defensive
            try:
                self._log("internal: %s: %s" % (type(exc).__name__, exc))
            except Exception:
                pass

    def _finish_job(
        self,
        on_done: Callable[[Any, Optional[BaseException]], None],
        result: Any,
        error: Optional[BaseException],
    ) -> None:
        """Clear the busy state, then run the job's callback."""
        # Cleared *before* the callback so that a callback which chains another
        # job (search -> list, for instance) is not refused as "busy".
        self._busy = False
        self._cancellable = False
        try:
            on_done(result, error)
        except Exception as exc:
            self._report_error(exc)
        finally:
            self._update_controls()

    def _on_progress(self, item: Progress) -> None:
        """Render one progress tick: log line, status line, progress bar."""
        if not isinstance(item, Progress):  # pragma: no cover - defensive
            return

        if item.message:
            self._log(item.message)

        total = max(0, int(item.total or 0))
        index = max(0, int(item.index or 0))

        if total > 0:
            self._set_progress(index, total)
            # Backends emit index=i *before* working on item i, and sometimes
            # index=i+1 after finishing it. Showing a 1-based number for a
            # named item matches what a human counts, and clamping keeps it
            # from ever reading "83 / 82".
            shown = min(index + 1, total) if item.name else min(index, total)
            if item.bytes_total > 0 and item.name:
                percent = int(
                    100.0
                    * min(1.0, max(0.0, item.bytes_done / float(item.bytes_total)))
                )
                self._set_status(
                    t(
                        "run.item_bytes",
                        index=shown,
                        total=total,
                        name=item.name,
                        percent=percent,
                    )
                )
            else:
                self._set_status(
                    t("run.item", index=shown, total=total, name=item.name or "")
                )
        elif item.name:
            self._set_status(item.name)

    # ------------------------------------------------------------------ #
    # Small GUI helpers (GUI thread only)
    # ------------------------------------------------------------------ #

    def _log(self, line: str) -> None:
        """Append one timestamped line to the log pane and scroll to it."""
        if not line:
            return
        try:
            widget = self._log_text
            widget.configure(state="normal")
            widget.insert("end", "%s  %s\n" % (_now_hhmmss(), line))

            # Trim from the top so a long rescue cannot make the pane sluggish.
            last = int(widget.index("end-1c").split(".")[0])
            if last > _LOG_MAX_LINES:
                widget.delete("1.0", "%d.0" % (last - _LOG_MAX_LINES + 1))

            widget.see("end")
            widget.configure(state="disabled")
        except Exception:
            # Logging is never worth failing an operation over.
            pass

    def _set_status(self, text: str) -> None:
        try:
            self._status_var.set(text)
        except Exception:
            pass

    def _set_progress(self, value: int, total: int) -> None:
        try:
            self._progress.configure(
                maximum=max(1, total), value=max(0, min(value, total))
            )
        except Exception:
            pass

    def _report_error(self, exc: BaseException) -> None:
        """Show one readable sentence. Never a traceback, never nothing."""
        if isinstance(exc, TransferAborted):
            # Cancelling is a decision the user made, not a failure.
            self._log(t("run.aborted"))
            self._set_status(t("run.aborted"))
            messagebox.showinfo(t("info.title"), t("run.aborted"), parent=self.root)
            return

        if isinstance(exc, (CameraError, ValueError)):
            # CameraError messages are written for this exact dialog; ValueError
            # is what the transfer engine raises when the delete gate refuses.
            text = str(exc) or type(exc).__name__
        else:
            text = t("error.unexpected", kind=type(exc).__name__, err=exc)

        self._log(text)
        self._set_status(text)
        try:
            messagebox.showerror(t("error.title"), text, parent=self.root)
        except Exception:  # pragma: no cover - window already gone
            pass

    def _on_tk_exception(self, exc_type, exc_value, tb) -> None:  # type: ignore[no-untyped-def]
        """Catch-all for exceptions raised inside Tk callbacks."""
        del tb  # the user gets a sentence; the traceback would only frighten
        try:
            self._report_error(
                exc_value
                if isinstance(exc_value, BaseException)
                else Exception(exc_type)
            )
        except Exception:  # pragma: no cover - last resort
            pass

    # ------------------------------------------------------------------ #
    # Step 1: environment
    # ------------------------------------------------------------------ #

    def _dep_label(self, dependency: deps.Dependency) -> str:
        """Translated label for a dependency, falling back to its own.

        ``deps.py`` deliberately does not translate itself; the stable
        ``key`` is the seam, and an unknown key simply keeps the English label.
        """
        key = "deps.label.%s" % dependency.key
        translated = t(key)
        return dependency.label if translated == key else translated

    def _refresh_env(self) -> None:
        """Re-probe dependencies and backend availability on the worker."""

        def work(_emit: Callable[[Progress], None]) -> Any:
            # Both probes shell out (brew --version, wsl --list, ...), so both
            # belong off the GUI thread even though each is time-boxed.
            return deps.check_all(), registry.backend_status()

        self._run_async(work, self._on_env_done, status=t("env.checking"))

    def _on_env_done(self, result: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            self._report_error(error)
            return

        self._deps, self._backend_status = result
        self._render_deps()

        for backend_cls, ok, hint in self._backend_status:
            name = _backend_name(backend_cls)
            if ok:
                self._log(t("env.backend_ok", name=name))
            else:
                self._log(t("env.backend_ko", name=name, hint=hint or "-"))

        self._set_status(t("env.checked"))

    def _render_deps(self) -> None:
        """Rebuild the dependency rows from scratch.

        Rebuilding rather than mutating is the point: ``check_all()`` returns
        snapshots, and a row edited in place could show a stale "installed"
        next to a fresh hint.
        """
        for child in self._env_rows.winfo_children():
            child.destroy()
        self._install_buttons = []

        if not self._deps:
            ttk.Label(self._env_rows, text=t("env.none")).grid(
                row=0, column=0, sticky="w"
            )
            return

        for row, dependency in enumerate(self._deps):
            present = bool(dependency.present)
            ttk.Label(
                self._env_rows,
                text=_GLYPH_OK if present else _GLYPH_WARN,
                style="Ok.TLabel" if present else "Warn.TLabel",
                width=2,
            ).grid(row=row, column=0, sticky="w", pady=1)

            label = self._dep_label(dependency)
            if dependency.version:
                label = "%s %s" % (label, dependency.version)
            ttk.Label(self._env_rows, text=label, width=20).grid(
                row=row, column=1, sticky="w", padx=(0, 10)
            )

            detail = dependency.hint or (
                t("env.present") if present else t("env.missing")
            )
            ttk.Label(self._env_rows, text=detail, wraplength=470, justify="left").grid(
                row=row, column=2, sticky="w"
            )

            if dependency.can_autoinstall and not present:
                button = ttk.Button(
                    self._env_rows,
                    text=t("env.install"),
                    # Bound now, not read later: the row is rebuilt on every
                    # refresh and a late lookup could target a different item.
                    command=lambda k=dependency.key, l=label: self._on_install(k, l),
                )
                button.grid(row=row, column=3, sticky="e", padx=(10, 0))
                self._install_buttons.append(button)

        self._update_controls()

    def _on_install(self, key: str, label: str) -> None:
        """Install one dependency, streaming the installer's output to the log."""
        self._log(t("env.install_started", label=label))

        def work(emit: Callable[[Progress], None]) -> Any:
            # deps.install() forwards every line of the installer as a
            # Progress(phase='deps'), which the pump drops straight into the log.
            return deps.install(key, emit)

        def done(result: Any, error: Optional[BaseException]) -> None:
            if error is not None:
                self._report_error(error)
                self._refresh_env()
                return
            ok, message = result
            if ok:
                self._log(t("env.install_ok", label=label, message=message))
                messagebox.showinfo(
                    t("env.install_title", label=label),
                    t("env.install_ok", label=label, message=message),
                    parent=self.root,
                )
            else:
                self._log(t("env.install_failed", label=label, message=message))
                messagebox.showwarning(
                    t("env.install_title", label=label),
                    t("env.install_failed", label=label, message=message),
                    parent=self.root,
                )
            # Trust a fresh probe, never the installer's own verdict.
            self._refresh_env()

        self._run_async(work, done, status=t("env.install_started", label=label))

    # ------------------------------------------------------------------ #
    # Step 2: camera
    # ------------------------------------------------------------------ #

    def _on_search(self) -> None:
        """Ask every available backend what it can see."""
        self._log(t("camera.searching"))

        def work(emit: Callable[[Progress], None]) -> Any:
            return registry.detect_all(emit)

        self._run_async(work, self._on_search_done, status=t("camera.searching"))

    def _on_search_done(self, result: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            self._report_error(error)
            return

        self._devices = list(result or [])
        labels = [camera.label for _backend, camera in self._devices]

        try:
            self._combo_devices.configure(values=labels)
        except Exception:
            pass

        if not self._devices:
            self._engine = None
            self._files = None
            self._device_var.set("")
            self._camera_info_var.set(t("camera.none"))
            self._set_status(t("camera.none"))

            # The hints of the backends that could *not* run are the actual
            # instructions here, so they go in the dialog rather than only the
            # log where nobody would look.
            lines = [t("camera.none"), "", t("camera.none_help")]
            for backend_cls, ok, hint in self._backend_status:
                if not ok and hint:
                    lines.append("")
                    lines.append(
                        t("env.backend_ko", name=_backend_name(backend_cls), hint=hint)
                    )
            self._log(t("camera.none"))
            messagebox.showinfo(t("info.title"), "\n".join(lines), parent=self.root)
            return

        self._log(t("camera.found", n=len(self._devices)))
        self._device_var.set(labels[0])
        # One device needs no choice; more than one does, and only then does the
        # combobox become interactive (see _update_controls).
        self._select_device(0)

    def _on_device_selected(self, _event: Any = None) -> None:
        try:
            index = self._combo_devices.current()
        except Exception:
            index = -1
        if 0 <= index < len(self._devices):
            self._select_device(index)

    def _select_device(self, index: int) -> None:
        """Bind a device to a fresh engine and start listing its files."""
        backend, camera = self._devices[index]
        try:
            self._engine = TransferEngine(backend, camera)
        except Exception as exc:
            # TransferEngine refuses a backend/camera pair whose kinds disagree.
            self._engine = None
            self._report_error(exc)
            self._update_controls()
            return

        # A new device invalidates everything downstream. In particular the old
        # report must go: it is the delete gate's evidence, and it says nothing
        # whatsoever about this camera.
        self._files = None
        self._report = None
        self._delete_supported = None
        self._summary_var.set(t("after.none"))

        self._suggest_dest(camera.model)
        self._update_camera_info()
        self._start_listing()

    def _start_listing(self) -> None:
        """List the selected camera's files (read-only) to show a count."""
        engine = self._engine
        if engine is None:
            return

        cancel = self._cancel

        def work(emit: Callable[[Progress], None]) -> Any:
            files = engine.list_files(emit, cancel)
            # Asked here, on the worker: the mass-storage backend answers by
            # writing a probe file to the card, and it can only answer at all
            # once list_files() has told it which card we are on.
            return files, _safe_supports_delete(engine)

        self._run_async(
            work, self._on_list_done, cancellable=True, status=t("camera.listing")
        )

    def _on_list_done(self, result: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            self._files = None  # unknown, not empty: Download may list again
            self._update_camera_info()
            self._log(t("camera.list_failed"))
            self._report_error(error)
            return

        files, self._delete_supported = result
        self._files = list(files or [])
        self._update_camera_info()
        if self._files:
            self._set_status(t("camera.listed", n=len(self._files)))
        else:
            self._set_status(t("camera.empty"))
            self._log(t("camera.empty"))

    def _update_camera_info(self) -> None:
        """Refresh the one-line description of the selected camera."""
        if self._engine is None:
            self._camera_info_var.set(t("camera.none_selected"))
            return

        camera = self._engine.camera
        backend = _backend_name(self._engine.backend)
        if self._files is None:
            text = t(
                "camera.info",
                model=camera.model,
                port=camera.port or "-",
                backend=backend,
            )
        else:
            text = t(
                "camera.info_files",
                model=camera.model,
                port=camera.port or "-",
                backend=backend,
                n=len(self._files),
            )
        if camera.detail:
            text = "%s\n%s" % (text, camera.detail)
        self._camera_info_var.set(text)

    # ------------------------------------------------------------------ #
    # Step 3: destination
    # ------------------------------------------------------------------ #

    @staticmethod
    def _suggested_dest(model: str) -> str:
        """``<Downloads>/<Model>_<date>``, never raising."""
        try:
            return deps.suggested_dest(model)
        except Exception:
            try:
                return deps.default_download_dir()
            except Exception:
                return ""

    def _set_dest(self, path: str) -> None:
        """Write the destination field without arming the "user typed" flag."""
        self._setting_dest = True
        try:
            self._dest_var.set(path)
        finally:
            self._setting_dest = False

    def _on_dest_written(self, *_args: Any) -> None:
        """Remember that the path on screen is the user's, not ours."""
        if not self._setting_dest:
            self._dest_touched = True

    def _suggest_dest(self, model: str) -> None:
        """Re-suggest a folder for this camera, unless the user chose one.

        Silently replacing a path someone typed is the kind of small betrayal
        that ends with photos in a folder they never find again.
        """
        if self._dest_touched:
            return
        self._set_dest(self._suggested_dest(model))

    def _on_browse(self) -> None:
        current = self._dest_var.get().strip()
        initial = current or self._suggested_dest("")
        # Point the dialog at the nearest folder that exists: the suggestion
        # itself normally does not yet.
        while initial and not os.path.isdir(initial):
            parent = os.path.dirname(initial)
            if not parent or parent == initial:
                initial = ""
                break
            initial = parent

        chosen = filedialog.askdirectory(
            parent=self.root,
            title=t("dest.dialog"),
            initialdir=initial or os.path.expanduser("~"),
            mustexist=False,
        )
        if chosen:
            self._dest_touched = True
            self._set_dest(os.path.abspath(chosen))

    # ------------------------------------------------------------------ #
    # Step 4: download
    # ------------------------------------------------------------------ #

    def _on_download(self) -> None:
        """Copy every file on the camera into the destination and verify it."""
        engine = self._engine
        if engine is None:
            messagebox.showinfo(t("info.title"), t("run.need_camera"), parent=self.root)
            return

        dest = self._dest_var.get().strip()
        if not dest:
            messagebox.showinfo(t("info.title"), t("dest.missing"), parent=self.root)
            try:
                self._entry_dest.focus_set()
            except Exception:
                pass
            return
        dest = os.path.abspath(os.path.expanduser(dest))

        # `[]` means the listing succeeded and the card is empty; `None` means
        # we never got a listing, in which case the job below takes one first.
        if self._files is not None and not self._files:
            messagebox.showinfo(t("info.title"), t("run.no_files"), parent=self.root)
            return

        files = list(self._files) if self._files is not None else None
        cancel = self._cancel
        self._log(
            t("run.starting", n=len(files) if files is not None else "?", dest=dest)
        )

        def work(emit: Callable[[Progress], None]) -> Any:
            target = files
            if target is None:
                target = list(engine.list_files(emit, cancel))
            # download() swallows TransferAborted and returns a partial report
            # instead: the files that did arrive are the whole point.
            report = engine.download(target, dest, emit, cancel, True)
            # Still on the worker, and deliberately after the download, so the
            # answer reflects the card as it is now.
            return target, report, _safe_supports_delete(engine)

        self._run_async(
            work, self._on_download_done, cancellable=True, status=t("run.working")
        )

    def _on_download_done(self, result: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            self._report_error(error)
            return

        files, report, self._delete_supported = result
        self._files = list(files)
        self._report = report

        for line in report.summary_lines():
            self._log(line)

        total = len(report.outcomes)
        if report.aborted:
            summary = t("after.summary_aborted", ok=report.ok_count, total=total)
        elif report.failed_count:
            summary = t(
                "after.summary_failed",
                ok=report.ok_count,
                total=total,
                failed=report.failed_count,
            )
        else:
            summary = t("after.summary", ok=report.ok_count, total=total)

        self._summary_var.set(summary)
        self._set_status(t("run.aborted") if report.aborted else t("run.finished"))
        self._set_progress(total, total)
        self._update_camera_info()

        messagebox.showinfo(
            t("info.title"),
            "\n".join([summary, ""] + report.summary_lines()),
            parent=self.root,
        )

    def _on_cancel(self) -> None:
        """Ask the running operation to stop at its next checkpoint."""
        if not self._busy:
            return
        self._cancel.cancel()
        self._cancellable = False  # one press is enough; stop offering it
        self._set_status(t("run.cancelling"))
        self._log(t("run.cancelling"))
        self._update_controls()

    # ------------------------------------------------------------------ #
    # Step 5: deletion — the only destructive path in the program
    # ------------------------------------------------------------------ #

    def _can_delete(self) -> bool:
        """Whether the delete button may be pressed at all.

        Three independent conditions, none of which is a matter of taste:
        a report exists, *every* requested file was downloaded and verified,
        and the transport is actually able to erase. The engine enforces the
        per-file gate again on its own; this is the button-level guard.
        """
        if self._busy or self._engine is None or self._report is None:
            return False
        # Read the cached answer rather than asking the backend: this runs on
        # the Tk thread on every state change, and supports_delete() can touch
        # the card. `None` (never asked) counts as no.
        if self._delete_supported is not True:
            return False
        try:
            return bool(self._report.all_verified)
        except Exception:
            return False

    def _delete_hint(self) -> str:
        """One sentence explaining why deletion is or is not available."""
        if self._report is None:
            return t("after.hint_none")
        if self._delete_supported is not True:
            return t("after.hint_unsupported")
        if not self._report.all_verified:
            return t("after.hint_unverified")
        return t("after.hint_ready", n=len(self._report.deletable))

    def _on_delete(self) -> None:
        """Erase the verified files from the camera, after explicit consent."""
        if not self._can_delete():
            return

        engine = self._engine
        report = self._report
        if engine is None or report is None:  # pragma: no cover - _can_delete
            return

        count = len(report.deletable)
        if count <= 0:
            messagebox.showinfo(
                t("info.title"), t("after.hint_unverified"), parent=self.root
            )
            return

        # First confirmation: the exact number, the exact folder the copies are
        # in, and the word irreversible. Default is No.
        if not messagebox.askyesno(
            t("after.confirm_title"),
            t("after.confirm_body", n=count, dest=report.dest_dir),
            icon="warning",
            default="no",
            parent=self.root,
        ):
            self._log(t("after.deleted_none"))
            return

        # Second confirmation for a whole card. Losing 12 photos is a mistake;
        # losing 300 is a different kind of afternoon.
        if count >= _LARGE_DELETE and not messagebox.askyesno(
            t("after.confirm2_title"),
            t("after.confirm2_body", n=count),
            icon="warning",
            default="no",
            parent=self.root,
        ):
            self._log(t("after.deleted_none"))
            return

        cancel = self._cancel

        def work(emit: Callable[[Progress], None]) -> Any:
            # The gate lives in the engine, not here: it re-derives the verified
            # set from the report and re-stats every local copy first.
            return engine.delete_verified(report, emit, cancel)

        self._run_async(
            work, self._on_delete_done, cancellable=True, status=t("after.deleting")
        )

    def _on_delete_done(self, result: Any, error: Optional[BaseException]) -> None:
        # Whatever happened, this report has been spent. Dropping it disables
        # the button, so a second click cannot re-issue a delete against a
        # camera whose contents have just changed.
        self._report = None
        self._files = None
        self._update_camera_info()

        if error is not None:
            self._report_error(error)
            self._log(t("after.delete_recheck"))
            return

        outcomes: List[DeleteOutcome] = list(result or [])
        deleted = sum(1 for o in outcomes if o.ok)
        kept = len(outcomes) - deleted

        for outcome in outcomes:
            if not outcome.ok:
                self._log(
                    t(
                        "after.delete_kept",
                        name=outcome.file.name,
                        why=outcome.error or "-",
                    )
                )

        if deleted and not kept:
            summary = t("after.deleted_all", n=deleted)
        elif deleted:
            summary = t("after.deleted_partial", ok=deleted, failed=kept)
        else:
            summary = t("after.deleted_none")

        self._summary_var.set(summary)
        self._set_status(summary)
        self._log(summary)
        self._log(t("after.delete_recheck"))
        messagebox.showinfo(
            t("after.delete_title"),
            "%s\n\n%s" % (summary, t("after.delete_recheck")),
            parent=self.root,
        )

    # ------------------------------------------------------------------ #
    # Control state
    # ------------------------------------------------------------------ #

    def _update_controls(self) -> None:
        """Enable exactly the controls that are safe to use right now.

        Called after every state change. Everything that could alter what a
        running transfer is doing — the device list, the destination, the
        environment installers — is disabled while it runs.
        """
        busy = self._busy
        idle = "disabled" if busy else "normal"

        def configure(widget: Any, state: str) -> None:
            try:
                widget.configure(state=state)
            except Exception:
                pass

        configure(self._btn_env_refresh, idle)
        for button in self._install_buttons:
            configure(button, idle)

        configure(self._btn_search, idle)
        # The combobox is only a control when there is a choice to make.
        configure(
            self._combo_devices,
            "disabled" if (busy or len(self._devices) < 2) else "readonly",
        )

        configure(self._entry_dest, idle)
        configure(self._btn_browse, idle)

        configure(
            self._btn_download,
            "disabled" if (busy or self._engine is None) else "normal",
        )
        configure(
            self._btn_cancel,
            "normal" if (busy and self._cancellable) else "disabled",
        )

        configure(self._btn_delete, "normal" if self._can_delete() else "disabled")
        try:
            self._delete_hint_var.set(self._delete_hint())
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Log saving and shutdown
    # ------------------------------------------------------------------ #

    def _on_save_log(self) -> None:
        """Write the log pane to a text file the user can attach to a report."""
        try:
            content = self._log_text.get("1.0", "end-1c")
        except Exception:
            content = ""
        if not content.strip():
            messagebox.showinfo(t("info.title"), t("log.empty"), parent=self.root)
            return

        suggested = "retrocam-%s.log" % datetime.datetime.now().strftime(
            "%Y%m%d-%H%M%S"
        )
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title=t("log.dialog"),
            defaultextension=".log",
            initialfile=suggested,
            filetypes=[
                (t("log.filter_log"), "*.log"),
                (t("log.filter_text"), "*.txt"),
                (t("log.filter_all"), "*.*"),
            ],
        )
        if not path:
            return

        try:
            with open(path, "w", encoding="utf-8", errors="replace") as handle:
                handle.write(content)
                handle.write("\n")
        except OSError as exc:
            message = t("log.save_failed", err=exc.strerror or exc)
            self._log(message)
            messagebox.showerror(t("error.title"), message, parent=self.root)
            return

        self._log(t("log.saved", path=path))

    def _on_close(self) -> None:
        """Confirm before closing on top of a running operation."""
        if self._busy:
            if not messagebox.askyesno(
                t("quit.title"),
                t("quit.body"),
                icon="warning",
                default="no",
                parent=self.root,
            ):
                return
            # Ask the worker to stop, then actually wait a moment for it. The
            # thread is a daemon, so without this join the interpreter exits
            # immediately and kills it wherever it happens to be — typically
            # holding an open '.part' file, which is then left behind in the
            # user's destination folder. Backends check the cancel token every
            # chunk (mass storage) or every 0.2 s (gphoto2), so this normally
            # returns in milliseconds; the timeout is what guarantees that a
            # transport which cannot be interrupted (WIA) still lets us quit.
            self._cancel.cancel()
            worker = self._worker
            if worker is not None and worker.is_alive():
                self._set_status(t("run.cancelling"))
                try:
                    worker.join(_QUIT_JOIN_SECONDS)
                except Exception:  # pragma: no cover - join never normally fails
                    pass

        try:
            if self._after_id is not None:
                self.root.after_cancel(self._after_id)
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:  # pragma: no cover - already destroyed
            pass


# --------------------------------------------------------------------------- #
# Entry point used by __main__
# --------------------------------------------------------------------------- #


def run_gui(lang: str = "") -> int:
    """Open the window and run until it is closed. Returns a process exit code.

    ``lang`` is applied before any widget is built, because widget text is
    resolved once at construction time.
    """
    if lang:
        set_language(lang)

    _enable_dpi_awareness()

    root = tk.Tk()
    RetroCamApp(root)
    root.mainloop()
    return 0
