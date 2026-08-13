"""RetroCam Rescue — recover photos from digital cameras the OS cannot see.

This module is deliberately almost empty. It is imported by the packaging
tooling (``[tool.hatch.version]`` reads ``__version__`` straight out of this
file) and by every submodule, so importing a backend, Tkinter or anything that
touches the filesystem from here would make ``python -c "import retrocam"``
slow, fragile, and capable of failing on a machine where the whole point is
that something is missing.

Import what you need from the submodule that owns it:

    from retrocam.model import CameraFile          # shared data types
    from retrocam.registry import detect_all       # what is plugged in
    from retrocam.transfer import TransferEngine   # download + delete gate
"""

from __future__ import annotations

#: Single source of truth for the version. Read by pyproject.toml (hatch), by
#: packaging/retrocam.spec and by the ``--version`` flag. Update it here only.
__version__ = "0.1.0"

__all__ = ["__version__"]
