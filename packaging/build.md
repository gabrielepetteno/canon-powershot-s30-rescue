# Building RetroCam Rescue binaries

How to turn this checkout into a `.app` (macOS) or an `.exe` (Windows) with
PyInstaller, and what the person who downloads the result will actually see.

**These binaries are the primary distribution channel.** The documented route
for a user is: open Releases, download the one file for their computer,
double-click it. "Install Python and run a script" has already lost a
non-technical reader on line one, so running from source is now the developer
route and lives at the bottom of the README.

That reordering puts weight on this file. If a release ships two artifacts
instead of three, or ships an arm64 bundle under a name that says Intel, the
project's entire user-facing story breaks at the first click. The workflow is
built to fail loudly rather than publish any of those.

- [What gets built](#what-gets-built)
- [Release artifact names](#release-artifact-names)
- [Prerequisites](#prerequisites)
- [Build on macOS](#build-on-macos)
- [Build on Windows](#build-on-windows)
- [Build on Linux](#build-on-linux)
- [macOS: the unsigned-app warning](#macos-the-unsigned-app-warning)
- [Windows: SmartScreen and antivirus](#windows-smartscreen-and-antivirus)
- [Three traps specific to this app](#three-traps-specific-to-this-app)
- [Releasing](#releasing)

---

## What gets built

`packaging/retrocam.spec` is the single source of truth and picks the mode by
platform. Do not pass `--onefile` / `--onedir` on the command line; edit the
spec instead.

| Platform | Mode              | Output                     | Shipped as                         |
| -------- | ----------------- | -------------------------- | ---------------------------------- |
| macOS    | onedir, windowed  | `dist/RetroCam Rescue.app` | `.zip` made with `ditto`           |
| Windows  | onefile, windowed | `dist/RetroCam Rescue.exe` | the raw `.exe`                     |
| Linux    | onedir            | `dist/RetroCam Rescue/`    | `.tar.gz` (not currently released) |

**Sizes.** Measured on macOS 15, arm64, built with the exact CI dependency set
(`uv sync --no-editable --group build --extra image --extra windows`):
the `.app` is **36 MB** on disk and **16 MB** as the `ditto` zip the user
downloads. Earlier notes said ~28 MB; that was a build without the `image`
extra. Pillow is worth its ~8 MB — it is what lets `verify.py` actually decode
each rescued JPEG instead of only checking its markers, and this program's one
promise is that the photo arrived intact. The Windows `.exe` size has not been
measured on real hardware; read it off the release page rather than quoting a
number from here.

**Why macOS is not onefile.** PyInstaller 6 prints this and PyInstaller 7 turns
it into an error:

```
DEPRECATION: Onefile mode in combination with macOS .app bundles (windowed mode)
don't make sense (a .app bundle can not be a single file) and clashes with macOS's
security. Please migrate to onedir mode. This will become an error in v7.0.
```

There is also a measurable user-facing cost: a onefile `.app` takes ~2.4 s to
start because it unpacks itself on every launch, against ~0.37 s for onedir.

**Why Windows is onefile.** One file to download and double-click is the whole
point for a non-technical user, and Windows has no bundle format to preserve.
The trade-offs are real and accepted: a slower first start (it extracts into
`%TEMP%\_MEIxxxxxx`) and a self-extracting bootloader that is the single most
common trigger for antivirus false positives. If Defender starts flagging
releases, switch the `IS_WIN` branch in the spec to the onedir path and ship a
ZIP.

---

## Release artifact names

Exactly three files are attached to every release, plus `SHA256SUMS.txt`:

| Filename                                 | For                                  |
| ---------------------------------------- | ------------------------------------ |
| `RetroCam-Rescue-Windows.exe`            | any Windows PC                       |
| `RetroCam-Rescue-macOS-AppleSilicon.zip` | Mac with an Apple chip (M1/M2/M3/M4) |
| `RetroCam-Rescue-macOS-Intel.zip`        | Mac with an Intel chip               |

The names are the product, not an implementation detail. The rules behind them:

- **No version number in the filename.** `RetroCam-Rescue-0.1.0-macos-arm64.zip`
  forces a reader to parse three fields before they can act. The GitHub release
  page already prints the version directly above the download buttons, and the
  app reports its own version in its window and via `--version`. Stable names
  also mean a support answer written today ("download
  `RetroCam-Rescue-Windows.exe`") is still literally correct next year.
- **No triples, no `darwin`, no `x86_64`.** "Apple Silicon" and "Intel" are the
  exact words Apple itself puts in **About This Mac**, so the user can match
  them by looking, not by knowing.
- **Windows ships as a bare `.exe`, not a zip.** Download → double-click is one
  step. A zip adds "right-click → Extract All → find the file", and
  double-clicking an `.exe` from inside Explorer's zip viewer runs it out of a
  temp folder, which fails in ways that are hard to explain over email.
- **macOS must be a zip**, because a `.app` is a directory. `ditto` is the only
  correct archiver (see below).

Changing any of these names is a user-visible change. Change them in
`.github/workflows/build.yml` (the `asset` column of the build matrix and the
checklist in the `release` job), in this table, and in both READMEs — the
workflow's release job hard-fails if a file listed there is missing, which is
the mechanism that stops a rename from half-landing.

---

## Prerequisites

Python **3.12 or 3.13** for release builds. The 3.9 floor is for _running_ the
app, not for building it.

```bash
# with uv (recommended: its CPython ships a working Tcl/Tk)
uv sync --no-editable --group build --extra image

# or with pip, in a virtualenv
python3 -m venv .venv && . .venv/bin/activate
pip install -e . "pyinstaller>=6.22,<7" Pillow
```

Two things to know about the build interpreter:

1. **The build Python decides which Tk ends up in the bundle.** PyInstaller's
   hooks collect whatever `_tkinter` links against. uv-managed CPython currently
   ships **Tk 9.0**; a python.org installer ships **Tk 8.6.x**. Both work. If
   you want the frozen app to look identical to what contributors see when they
   run from source, build with a python.org installer.
2. **Never build with `/usr/bin/python3` on macOS.** It is Python 3.9.6 with
   Apple's **Tk 8.5**, a known-buggy build, and you would ship that to everyone.

Verify before building:

```bash
python -c "import tkinter, sys; print('Tk', tkinter.TkVersion, sys.version)"
```

---

## Build on macOS

```bash
cd /path/to/canon-powershot-s30-rescue
uv run pyinstaller packaging/retrocam.spec --noconfirm --clean

# Smoke-test the real binary inside the bundle (no Finder, no display needed).
"dist/RetroCam Rescue.app/Contents/MacOS/RetroCam Rescue" --version

# Confirm which architecture you actually built before you name the file after it.
lipo -archs "dist/RetroCam Rescue.app/Contents/MacOS/RetroCam Rescue"   # arm64 | x86_64

# Archive it. ditto, NOT `zip -r`. Name it after what lipo just said.
ditto -c -k --sequesterRsrc --keepParent \
  "dist/RetroCam Rescue.app" "RetroCam-Rescue-macOS-AppleSilicon.zip"
```

**`ditto`, never `zip -r`.** The bundle contains symlinks and a `_CodeSignature`
seal. `zip -r` flattens symlinks and mangles the seal, and the app then fails to
launch with _"RetroCam Rescue is damaged and can't be opened"_ — which users
report as corruption when it is really a broken archive.

### Architectures: why two files and not one universal2

A single universal2 `.app` would be the nicest thing to hand a user — one
download, works on every Mac. It was considered and rejected, on these grounds:

`target_arch="universal2"` requires a universal2 CPython **and** a universal2
wheel for every binary dependency. uv-managed CPython
(python-build-standalone) is single-arch, Homebrew's is single-arch, and Pillow
no longer publishes universal2 wheels. Only a python.org installer is
universal2, and building against it would still leave Pillow — our `image`
extra, which strengthens the download verification — as a single-arch slice.
PyInstaller's failure mode there is not always an error; it can quietly produce
a bundle that is universal2 in name and broken on one of the two architectures.
That is precisely the outcome this project cannot afford.

So: **one artifact per architecture, built on its own runner, with the
architecture verified after the fact.**

| Runner                     | Release file                             | Minimum macOS |
| -------------------------- | ---------------------------------------- | ------------- |
| `macos-26` (Apple Silicon) | `RetroCam-Rescue-macOS-AppleSilicon.zip` | 11.0 Big Sur  |
| `macos-26-intel`           | `RetroCam-Rescue-macOS-Intel.zip`        | 11.0 Big Sur  |

**The architecture is checked, not assumed.** The workflow runs
`lipo -archs` on the built binary and fails the job if the result does not match
the name the file is about to be given. Without that check, a change to the
runner labels could silently ship an arm64 bundle called `…-Intel.zip`; the
Intel owner would get _"You can't open the application because it is not
supported on this Mac"_, which reads as "this program is broken". A failed
release is recoverable. A wrong file that reaches a nervous user is not.

**About the macOS 11.0 minimum.** `LSMinimumSystemVersion` is `11.0` for both
architectures. arm64 macOS starts at 11.0 anyway. The Intel build's real
deployment target is probably lower, but nobody has run it on a Mac older than
Big Sur, and declaring an untested floor trades a clear _"requires macOS 11"_
refusal for an obscure dyld crash on launch. CI logs the binary's actual minimum
(`vtool -show-build`), so the number can be lowered later on evidence. Anyone on
an older Mac uses the run-from-source path, which has no floor beyond Python 3.9.

**What is actually verified, and what is not.** The Apple Silicon build is the
one that has been used end to end, on a real Mac, with a real Canon PowerShot
S30. The Intel build is compiled and started (`--version`) on genuine Intel
hardware in CI, so it is known to be a valid Intel executable that boots — but
no one has yet rescued a photograph with it. Say exactly that in the release
notes; do not round it up to "supported".

---

## Build on Windows

```bat
cd C:\path\to\canon-powershot-s30-rescue
uv run pyinstaller packaging\retrocam.spec --noconfirm --clean

REM The windowed build has no stdout, so check the exit code, not the output.
"dist\RetroCam Rescue.exe" --version
if errorlevel 1 echo SMOKE TEST FAILED

ren "dist\RetroCam Rescue.exe" "RetroCam-Rescue-Windows.exe"
```

`packaging/version_info.txt` is embedded automatically by the spec. Keep its
version tuples in sync with `src/retrocam/__init__.py` — an executable with no
publisher and no version is exactly the profile reputation heuristics distrust.

Include `pywin32` in the build environment (`--extra windows`) if you want the
WIA backend inside the binary. Without it the frozen app still starts and the
environment panel reports WIA as unavailable, which is the designed behaviour,
but a shipped Windows build should have it.

---

## Build on Linux

Not part of the release matrix — the source path is better on Linux, where
`gphoto2` is one `apt install` away. If you want a bundle anyway:

```bash
pyinstaller packaging/retrocam.spec --noconfirm --clean
tar czf RetroCam-Rescue-0.1.0-linux-x86_64.tar.gz -C dist "RetroCam Rescue"
```

A PyInstaller bundle links against the glibc of the machine that built it, so
build on the **oldest** distribution you intend to support.

---

## macOS: the unsigned-app warning

This is the part to get right, because every macOS user hits it and the advice
circulating online is out of date.

### What is actually true

- PyInstaller **already ad-hoc signs** the bundle (`codesign -dv` reports
  `Signature=adhoc`, `TeamIdentifier=not set`). This is mandatory on Apple
  Silicon: an unsigned arm64 binary will not execute at all.
- Ad-hoc is **not** a Developer ID signature and **not** notarization.
  `spctl -a -vv` on the built bundle returns **`rejected`**.
- Therefore Gatekeeper blocks the first launch of any copy that arrived carrying
  the `com.apple.quarantine` attribute — a browser download, AirDrop, or
  anything extracted from a downloaded archive.

### What the user sees

> **"RetroCam Rescue" Not Opened**
> Apple could not verify "RetroCam Rescue" is free of malware that may harm your
> Mac or compromise your privacy.
> _[Done]_ _[Move to Trash]_

Occasionally, with a badly made archive, the variant is _"RetroCam Rescue is
damaged and can't be opened."_ That is **not** corruption — it is the same
quarantine check. (If you built the zip yourself with `zip -r` instead of
`ditto`, then it probably _is_ corruption. See above.)

### The workaround to document

**Right-click → Open no longer works.** macOS Sequoia (15) removed the
Gatekeeper contextual-menu override, so the trick that every old tutorial
recommends now just opens the app's normal warning dialog again. On macOS 14 and
earlier it still works, which is why the instructions below list it last.

**1. Supported path (macOS 15, 26 and later):**

```
1. Double-click the app. The warning appears. Click "Done".
2. Open  System Settings > Privacy & Security
3. Scroll down to the "Security" section. There is now a line saying
   "RetroCam Rescue" was blocked ...   ->  click "Open Anyway"
4. Confirm with Touch ID or your password.
5. Launch the app again. This is a one-time step per version.
```

**2. Terminal equivalent (one command, any macOS version):**

```bash
xattr -dr com.apple.quarantine "/Applications/RetroCam Rescue.app"
```

Use `-dr com.apple.quarantine` (delete _this one_ attribute, recursively), not
the widely copy-pasted `xattr -cr`, which strips **all** extended attributes
from every file in the bundle.

**3. macOS 14 (Sonoma) and earlier only:** Control-click the app in Finder,
choose **Open**, then **Open** again in the dialog.

### Making the warning go away for real

The only real fix is an Apple Developer Program membership (**$99/year**):

```bash
codesign --deep --force --options runtime --timestamp \
  --sign "Developer ID Application: Your Name (TEAMID)" \
  "dist/RetroCam Rescue.app"

ditto -c -k --sequesterRsrc --keepParent "dist/RetroCam Rescue.app" notarize.zip
xcrun notarytool submit notarize.zip --apple-id you@example.com \
  --team-id TEAMID --password "app-specific-password" --wait
xcrun stapler staple "dist/RetroCam Rescue.app"
```

Below that threshold there is no trick. Do not pretend otherwise in the release
notes: tell users the app is unsigned, tell them why, and publish the SHA-256
checksums so they can verify what they downloaded.

---

## Windows: SmartScreen and antivirus

The unsigned `.exe` triggers:

> **Windows protected your PC** — Microsoft Defender SmartScreen prevented an
> unrecognised app from starting.
> _More info_ → _Run anyway_

Two clicks, and the pattern is familiar to Windows users. Reputation accrues
with download volume, so this fades for popular releases.

Free-ish improvements, in order of effort:

- **SignPath Foundation** issues free code-signing certificates to qualifying
  open-source projects. The publisher then shows as "SignPath Foundation".
  Best free option.
- **Azure Artifact Signing** (formerly Trusted Signing), ~$10/month, accepts
  individual developers. Note that EV certificates no longer grant an instant
  SmartScreen bypass.
- Keep `upx=False` in the spec (it already is). UPX compression massively
  increases antivirus false positives.

---

## Three traps specific to this app

These have all been hit before. Check them after any packaging change.

### 1. A Finder-launched `.app` has almost no `PATH`

It inherits `PATH=/usr/bin:/bin:/usr/sbin:/sbin` — no `/opt/homebrew/bin`. So
`shutil.which("gphoto2")` returns `None` even though `brew install gphoto2`
succeeded, and the app tells the user the tool is missing while it sits right
there. This is the number one reason a macOS bundle "works for the developer
and fails for the user".

`deps.which()` already repairs this by probing `/opt/homebrew/bin`,
`/usr/local/bin`, `/opt/local/bin` and `~/.local/bin` explicitly. Any new code
that looks for an external tool must go through `deps.which()`, never bare
`shutil.which()`.

### 2. Sanitize the environment before spawning a child process

The PyInstaller bootloader prepends the bundle directory to `LD_LIBRARY_PATH`
(Linux) and runtime hooks can touch `DYLD_LIBRARY_PATH`; on Windows it calls
`SetDllDirectoryW`. A spawned `gphoto2` would then load _our_ bundled libraries
instead of the system's, and fail in ways that make no sense. Restore the
originals before spawning:

```python
def _clean_env() -> dict:
    """External programs must see the system's libraries, not our bundled ones."""
    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        for var in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "LIBPATH"):
            orig = env.pop(var + "_ORIG", None)
            if orig is not None:
                env[var] = orig
            else:
                env.pop(var, None)
    return env
```

### 3. `sys.stdout` can be `None` in a windowed Windows build

A bare `print()` then raises `AttributeError: 'NoneType' object has no attribute
'write'` and kills the thread it happened on — silently, because there is no
console to show it. Route everything through `logging` with a `FileHandler`;
never `print()` outside `__main__.py`. On macOS stdout is valid when launched from a
terminal, so this bites Windows only, i.e. exactly where you cannot see it.

Related: `deps.install()` should refuse to pip-install into a frozen bundle
(`getattr(sys, "frozen", False)`), because the site-packages inside a `.app` is
read-only and, on Windows onefile, is deleted when the process exits. Return
`(False, "brew install gphoto2")` instead.

---

## Releasing

Pushing a `v*` tag is the whole release process. `.github/workflows/build.yml`
then runs three jobs in order, and the release only appears if every one of them
passed.

### What the workflow does

**1. `test`** — pytest on Ubuntu against Python **3.9** and **3.13**. 3.9 is the
floor for _running_ the app and an untested floor is a claim, not a fact. A
failing test means no binaries, by construction.

**2. `build`** — a three-way matrix, each entry building with `uv sync
--no-editable --group build --extra image --extra windows` and then
`pyinstaller packaging/retrocam.spec`:

| Runner           | Produces                                 |
| ---------------- | ---------------------------------------- |
| `macos-26`       | `RetroCam-Rescue-macOS-AppleSilicon.zip` |
| `macos-26-intel` | `RetroCam-Rescue-macOS-Intel.zip`        |
| `windows-latest` | `RetroCam-Rescue-Windows.exe`            |

Each build is gated by four checks before its artifact is uploaded:

- tkinter must import in the build environment (catches a Python without Tk
  before it becomes a broken bundle);
- the frozen binary must run `--version` and, on macOS, print a string
  containing "RetroCam Rescue" — a build that lost its own package could still
  exit 0, so the exit code alone is not enough;
- on macOS, `lipo -archs` on the built binary must equal the architecture the
  filename is about to claim;
- `upload-artifact` uses `if-no-files-found: error`, so a packaging step that
  produced nothing fails the job instead of uploading an empty artifact.

`fail-fast: false` is set so a break on one platform still yields the other two
sets of logs. It does **not** let a partial release through: `release` needs
`build` to succeed.

**3. `release`** — downloads all artifacts, then, before anything is published:

- asserts that all three expected filenames are present **and non-empty**;
- writes `SHA256SUMS.txt`;
- calls `softprops/action-gh-release` with `fail_on_unmatched_files: true`.

`generate_release_notes` is **off** on purpose. The release page is the
installation page for a non-technical user, and an auto-generated changelog
under the download buttons buries the one instruction that matters. The commit
list is one click away under the tag. The release body is a fixed, plain-language
text held inline in the workflow — edit it there, and keep it in step with the
Gatekeeper and SmartScreen wording in this file.

The workflow's Windows smoke test asserts only the exit code, because a
`--windowed` build has no stdout to capture (`sys.stdout` is `None`, and
`print()` is then a silent no-op). That is why `main()` must handle `--version`
**before** it constructs a Tk root: CI runners are headless and any `Tk()` call
would fail there.

### Cutting a release

```bash
# 1. Bump the version in all three places — they are not derived from each other.
#      src/retrocam/__init__.py        __version__
#      packaging/retrocam.spec         VERSION
#      packaging/version_info.txt      filevers / prodvers / FileVersion / ProductVersion

# 2. Prove it locally before spending CI time.
PYTHONPATH=src python3 -m unittest discover -s tests    # or: python -m pytest
./run.sh --version                                      # must print the new number

# 3. Optional but cheap: prove the packaging change itself without tagging.
#    The workflow has workflow_dispatch for exactly this. It builds and
#    uploads all three artifacts as run artifacts and creates no release.
gh workflow run build.yml

# 4. Tag and push. This is the point of no return for the tag name.
git tag -a v0.1.0 -m "RetroCam Rescue 0.1.0"
git push origin v0.1.0

# 5. Watch it. ~10 minutes; the macOS runners are the slow part.
gh run watch
```

### After the release exists — check it as a user would

Do not skip this. The download page is the product now.

1. Open the release page in a browser. Confirm **four** assets are listed:
   the three binaries and `SHA256SUMS.txt`.
2. Confirm the body renders as instructions, not as a changelog.
3. Download the Apple Silicon zip on a Mac and open it. Safari expands zips
   automatically, so you should end up with `RetroCam Rescue.app` in Downloads.
4. Double-click it and confirm you get the Gatekeeper dialog described below —
   and that the steps written in the release body actually clear it. If Apple
   changes that dialog's wording again, the release body is now wrong for every
   past release too, and the fix is to edit the workflow and re-run it.

### If a release went out wrong

Delete the release **and** the tag, fix, re-tag. Never edit an artifact in place:
`SHA256SUMS.txt` would then be a lie, and the checksum file is the only thing a
careful user has to verify with.

```bash
gh release delete v0.1.0 --yes
git push --delete origin v0.1.0
git tag -d v0.1.0
# fix, commit, then re-tag the same version
```
