"""Entry point used only by PyInstaller.

Why a separate stub: PyInstaller must be pointed at a *script*, not at a module
inside a package. Keeping that script here rather than in ``src/retrocam/``
means the frozen build, ``python -m retrocam`` and the ``retrocam`` entry point
all end up calling exactly one implementation — ``retrocam.__main__.main()`` —
so there is no second code path that only exists in the release binary.

``main()`` parses the arguments and handles ``--version`` / ``--cli`` *before*
importing tkinter, which is what lets the release workflow smoke-test the built
binary on a headless CI runner.
"""

from __future__ import annotations

import multiprocessing
import sys


def _run() -> int:
    # freeze_support() must be the first thing that happens if multiprocessing
    # is ever introduced: on a frozen Windows build a child process re-executes
    # the bundled binary, which would otherwise start a second copy of the GUI.
    # It is a no-op while multiprocessing is unused, which is why it is cheap
    # insurance rather than speculation.
    multiprocessing.freeze_support()

    from retrocam.__main__ import main

    return main() or 0


if __name__ == "__main__":
    sys.exit(_run())
