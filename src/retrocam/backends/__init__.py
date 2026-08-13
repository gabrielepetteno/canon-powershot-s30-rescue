"""Transports for reaching photos on a device.

One module per transport. The concrete backends are re-exported here for
convenience::

    from retrocam.backends import GPhoto2Backend, MassStorageBackend, WiaBackend

but they are resolved **lazily**, through the module-level ``__getattr__`` below
(PEP 562), and that is the whole point of this file. Importing
``retrocam.backends`` must stay nearly free: eagerly importing the three
submodules here costs ~50x more (360us -> ~18ms measured) and, worse, drags the
Windows backend's ``win32com`` probing into every process that only wanted a
type annotation. :mod:`retrocam.registry` still imports the concrete backends
explicitly, and it remains the only module that *needs* to.

The names below therefore behave exactly like normal re-exports for callers and
type checkers, while a bare ``import retrocam.backends`` still touches nothing.

To add a transport: subclass :class:`retrocam.backends.base.CameraBackend`, drop
the module in here, and add one import plus one list entry to
``registry.ALL_BACKENDS``. See CONTRIBUTING.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List

__all__ = [
    # base contract
    "Availability",
    "CameraBackend",
    "noop_progress",
    # concrete transports, in the registry's preference order
    "MassStorageBackend",
    "GPhoto2Backend",
    "WiaBackend",
]

#: Exported name -> submodule that defines it. Kept as data rather than as
#: import statements so that ``__getattr__`` and ``__dir__`` cannot disagree.
_EXPORTS = {
    "Availability": "base",
    "CameraBackend": "base",
    "noop_progress": "base",
    "MassStorageBackend": "massstorage",
    "GPhoto2Backend": "gphoto2_backend",
    "WiaBackend": "wia",
}

if TYPE_CHECKING:  # pragma: no cover - type checkers only, never at runtime
    from .base import Availability, CameraBackend, noop_progress
    from .gphoto2_backend import GPhoto2Backend
    from .massstorage import MassStorageBackend
    from .wia import WiaBackend


def __getattr__(name: str) -> Any:
    """Resolve a re-exported name on first use (PEP 562).

    Raising :class:`AttributeError` for anything unknown is required: it is what
    lets ``hasattr`` and ``from ... import ...`` report a typo normally instead
    of failing later with a confusing ``ImportError``.
    """
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError("module %r has no attribute %r" % (__name__, name))

    from importlib import import_module

    value = getattr(import_module("." + module_name, __name__), name)
    # Cache on the module so repeated access skips this function entirely.
    globals()[name] = value
    return value


def __dir__() -> List[str]:
    """Make the lazy names discoverable to ``dir()``, REPL completion and help."""
    return sorted(set(globals()) | set(_EXPORTS))
