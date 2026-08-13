# Contributing to RetroCam Rescue

Thanks for looking. This is a small project with one job: get photos off old
cameras without ever losing one.

## Ground rules

Two rules override everything else, including elegance, performance and my
opinion:

1. **Never write to the device except in `delete()`.** Listing and downloading
   are strictly read-only. A card from 2003 may be one bad write away from
   unreadable. There is exactly one sanctioned exception in the tree today —
   `MassStorageBackend._probe_writable`, a zero-byte create-and-remove used to
   decide whether the Delete button may be enabled — and it is documented in the
   README's safety model. Do not add a second one without the same treatment.
2. **Never widen the delete gate.** A file may only reach `backend.delete()`
   through `TransferReport.deletable`, which requires bytes on disk plus a
   passing `VerifyResult` produced during that same run. If a change makes that
   set larger, it is wrong — even if it makes a test pass.

If a change touches `transfer.py` or `verify.py`, say in the PR description
which of these two rules you thought about and why the change is still safe.

These two are the rules a reviewer will block a PR over. They are the sharp end
of the seven rules in the README's
[safety model](README.md#is-it-safe), which names the enforcement point
for each one — read that section once before your first PR, and re-read the rule
you are touching before you change it.

## Setup

```bash
git clone https://github.com/gabrielepetteno/canon-powershot-s30-rescue.git
cd canon-powershot-s30-rescue

# Run it straight from the checkout — no install needed.
./run.sh          # macOS / Linux
run.bat           # Windows

# Tests
uv run --group dev python -m pytest -q
# or
python -m pytest          # with the package installed
PYTHONPATH=src python -m pytest   # from a bare checkout

# The suite is plain unittest, so pytest is optional:
PYTHONPATH=src python -m unittest discover -s tests
```

All 387 tests must pass before you open a PR. The suite needs no network, no
camera, no admin rights and no Pillow, takes about eleven seconds, and leaves
nothing behind. If any of those stops being true, that is a bug in the test.

To run one file or one class while you work — `tests/` is deliberately not a
package, so address files by path, not by dotted module name:

```bash
# with pytest
python -m pytest tests/test_delete_gate.py
python -m pytest tests/test_delete_gate.py::GateRefusalTest -v

# with unittest only — every test file is also runnable on its own
PYTHONPATH=src python tests/test_delete_gate.py
PYTHONPATH=src python -m unittest discover -s tests -p 'test_delete_gate.py'
```

## What the code has to be

- **Python 3.9 compatible.** Use `from __future__ import annotations`. No
  `match`, no PEP 604 `X | Y` in runtime-evaluated positions, no PEP 695
  generics. CI proves the floor on every release.
- **Standard library only in the core.** Pillow and pywin32 are optional
  enhancements: the app must fully start, detect, download and verify with
  neither installed. Import them lazily, inside the function that needs them,
  and degrade gracefully.
- **Type-hinted, with docstrings.** Comments explain **why**, not what.
- **English** in code, comments and docstrings — this is a public repository.
  User-facing strings go through `i18n.t()` and must exist in both Italian and
  English.
- **No `print()`** outside `__main__.py`. A `--windowed` build on Windows has
  `sys.stdout is None`, and a bare `print()` there raises `AttributeError` and
  kills the thread silently. `__main__.py` prints through a helper that survives
  both a missing stdout and a console that cannot encode the character.

## Where things live

| File                            | Owns                                                                          |
| ------------------------------- | ----------------------------------------------------------------------------- |
| `src/retrocam/model.py`         | The shared data types. **Authoritative** — do not redefine or duplicate them. |
| `src/retrocam/backends/base.py` | The `CameraBackend` contract and its five rules.                              |
| `src/retrocam/backends/*.py`    | One transport each.                                                           |
| `src/retrocam/registry.py`      | The backend list and `detect_all()`.                                          |
| `src/retrocam/verify.py`        | Is this file on disk intact?                                                  |
| `src/retrocam/transfer.py`      | Download orchestration **and the delete gate**.                               |
| `src/retrocam/deps.py`          | Dependency probing and assisted install.                                      |
| `src/retrocam/i18n.py`          | `t()`, Italian and English.                                                   |
| `src/retrocam/app.py`           | Tkinter. Knows nothing about any specific backend.                            |
| `src/retrocam/__main__.py`      | Argument parsing, `--version`, `--cli`, then the window.                      |
| `packaging/`                    | PyInstaller spec, frozen entry stub, build docs.                              |

## Adding a backend

The extension point the design exists for. Three steps, documented in the
[README](README.md#how-the-backends-plug-in): subclass `CameraBackend`, add a
`BackendKind`, add one import and one entry to `registry.ALL_BACKENDS`.

Keep the registry imports **static**. Lazy or `importlib`-based discovery
silently produces an empty backend list inside a frozen PyInstaller build, and
the app then reports "no camera found" on a machine with a camera plugged in.

Read the five rules at the top of `base.py` before you start. The ones people
get wrong:

- **Wrap every exception.** A backend must never leak `subprocess`, COM or
  `OSError` to the GUI. Raise a `CameraError` whose message a non-technical
  person can act on, and which says what to try next.
- **Report exact byte sizes** in `list_files()` when the transport exposes them.
  Use `-1` for unknown, never `0` — `0` is a legitimate size for a corrupt file.
- **Download to a temp name, `fsync`, then `os.replace()`.** A half-written file
  must never carry the final name.
- **Never hold a live device handle on `self`.** The camera can be unplugged
  between two GUI actions. State travels in `CameraInfo.raw` / `CameraFile.raw`.
- **Check the cancel token** between files and raise `TransferAborted` promptly.

## Tests

New behaviour needs a test. Safety-relevant behaviour needs a test that **fails
before the fix and passes after it** — write the test first and watch it fail,
because a safety test that has never failed has never been shown to work.

Three rules, in order of how much a reviewer cares:

1. **Never delete a test that was passing.** If a test is genuinely wrong, say
   so in the PR description and explain why; do not quietly drop it.
2. **Never weaken a test to make it pass.** A failing test that found a real
   defect is the system working. Fix the product.
3. **A test that cannot fail is worse than no test**, because it buys false
   confidence. Prefer few sharp tests to many shallow ones, and name each test
   method after the property it protects.

### Use the shared fixtures

`tests/helpers.py` is the shared fixture module. Read it before writing a test;
do not reinvent what is already there. It gives you, among other things:

- **file builders** — `tiny_jpeg()`, `truncated_jpeg()`, `not_a_jpeg()`,
  `riff_avi()`, and `sha()` for byte-identity assertions;
- **`make_card()` / `DEFAULT_CARD`** — a real DCIM tree on disk. The default
  spec deliberately puts the same base name in two folders
  (`118CANON/IMG_0001` and `119CANON/IMG_0001`), which is the case that once
  broke all three backends;
- **`FakeGphoto2`** — installs a real scripted executable named `gphoto2` first
  on `PATH`, so the backend genuinely spawns a process and parses genuine
  output. Inspect the argv it received with `.calls()`;
- **`fake_wia`** — patches the module's single COM seam plus `sys.platform`, so
  the real `WiaBackend` runs its own code paths on macOS;
- **`RecordingProgress`** — collects `Progress` objects, and with
  `fail_on_thread=True` doubles as a cross-thread tripwire;
- **`TempDirCase`** — a `TestCase` base that cleans up after itself, including
  directories a test made read-only.

### Where the gaps are

If you want to make the biggest difference, this is the list:

- **The Windows path has never run on Windows.** `test_wia.py` proves the WIA
  backend's logic against a fake COM layer, and nothing more. The same is true
  of the Windows-only ctypes code in `massstorage.py`. Anyone who can run the
  suite — or just the app — on a real Windows machine and report back is doing
  the most valuable thing available.
- **No test has ever talked to a camera.** See "Hardware reports" below.
- **`app.py` is the thinnest-covered module.** Only two of its behaviours are
  tested (`test_coverage_gaps.py`): the Delete button's own gate, and discarding
  the report when the user switches camera. Everything else about the window is
  covered only by the translation-key scan in `test_i18n_and_base.py`. If you
  add a GUI test, keep it headless — the suite must never need a display.

## Pull requests

- One topic per PR. A refactor and a fix in the same diff is two PRs.
- Say what you tested on: OS, Python version, and which camera or card, if any.
  "Tested with a mounted directory, no hardware" is a perfectly good answer.
- If it changes user-facing text, update **both** language tables.
- If it changes packaging, update `packaging/build.md`.

## Hardware reports are contributions

Getting a specific camera working is genuinely useful data. If you rescued
photos from a model that is not mentioned in the README — or failed to — open an
issue with the model, the OS, the backend that worked, and the output of
`gphoto2 --auto-detect` if relevant. That is how the compatibility list grows.

## Licence

By contributing you agree that your contribution is licensed under the
[MIT License](LICENSE), like the rest of the project.
