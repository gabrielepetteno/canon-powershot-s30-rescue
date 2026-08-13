# Canon PowerShot S30 — rescue your photos (RetroCam Rescue)

**Get the photos off a Canon PowerShot S30, or another digital camera from
1999–2002, when Windows 11 or macOS does not see the camera at all.**

English | [Italiano](README.it.md)

![Licence: MIT](https://img.shields.io/badge/licence-MIT-blue)
![Tests: 387](https://img.shields.io/badge/tests-387%20passing-brightgreen)
![Platforms: Windows, macOS, Linux](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

You plug the camera in and nothing happens. No window opens. No drive appears.
The camera is not broken and the memory card is not broken — modern computers
simply stopped understanding how cameras of that age talk.

RetroCam Rescue is a small free program that gets the photos off anyway. It
copies every picture onto your computer, reads each copy back to check it
really arrived in one piece, and only then offers to clear the camera.

It works the same way for the cameras that share the S30's problem: Canon
PowerShot **S40**, **S100 / S110 / S200** (the original Digital IXUS / ELPH),
**G1**, **G2**, **Pro90 IS**, **A10**, **A20** — and comparable Nikon, Olympus,
Kodak, Fujifilm and Casio models from the same years.

Everything happens on your own computer. Nothing is uploaded anywhere. There is
no account, no sign-up and no cost.

> **Not a computer person, and you just want the photos back?**
> Read (or print) the **[quick guide](GUIDA-RAPIDA.md)** — eight steps, no
> jargon, two sides of one sheet of paper. Written in Italian, for someone who
> has never opened a terminal.

---

## Table of contents

- [Is this for you?](#is-this-for-you)
- [Get it working](#get-it-working)
  - [Windows, step by step](#windows-step-by-step)
  - [Mac, step by step](#mac-step-by-step)
  - [Why macOS says "unidentified developer"](#why-macos-says-unidentified-developer)
- [How to use it](#how-to-use-it)
- [Before you erase anything](#before-you-erase-anything)
- [If it does not work](#if-it-does-not-work)
- [Which method to use](#which-method-to-use)
- [Is it safe?](#is-it-safe)
- [Project status](#project-status)
- [For developers](#for-developers)

---

## Is this for you?

- You have an old digital camera, or its memory card, with photos still on it.
- You plug it into your computer and **nothing happens** — no new drive, no
  window, nothing in Photos, Image Capture or File Explorer.
- You want those photos safely onto your computer.

If your camera **does** already show up as a drive when you plug it in, this
program still works, and it is still the easiest way to do it: it copies
everything, checks every file afterwards, never overwrites a photo, and refuses
to erase anything it has not verified.

> ### Before you go looking for a driver
>
> If you search the web for _"Canon PowerShot S30 driver Windows 11"_, you will
> find pages offering exactly that, with a big green Download button.
>
> **Do not install them.** No such driver exists. Canon's last drivers for these
> cameras were for Windows XP and old Macs, and were discontinued many years
> ago. Nobody has written a new one. The sites that claim otherwise are almost
> always "driver updater" subscriptions, adware, or plain malware. If a page
> asks you to turn off your antivirus to fix a 2001 camera, close the tab.

---

## Get it working

You do **not** need to install Python, open a terminal, or understand any of
the words in the developer section at the bottom. You need two things: download
a file, and double-click it.

**Start here:**
[**Releases page — download the program**](https://github.com/gabrielepetteno/canon-powershot-s30-rescue/releases/latest)

On that page there is a section called **Assets**. If it looks like a closed
list, click the word _Assets_ to open it. Then pick your file:

| Your computer                        | The file to download                     |
| ------------------------------------ | ---------------------------------------- |
| Windows 10 or Windows 11             | `RetroCam-Rescue-Windows.exe`            |
| Mac with an Apple chip (M1/M2/M3/M4) | `RetroCam-Rescue-macOS-AppleSilicon.zip` |
| Mac with an Intel processor          | `RetroCam-Rescue-macOS-Intel.zip`        |

There are three programs, and the names never change from one release to the
next. There is no version number in the name — the file called
`RetroCam-Rescue-Windows.exe` on this page is always the newest Windows one.
(A fourth item, `SHA256SUMS.txt`, is a small text file for technical people.
Ignore it.)

**Taking the wrong Mac file is not dangerous.** The Mac will simply say it
cannot open it. Come back and take the other one.

> **If you have an Intel Mac, read this.** The Intel file is built
> automatically, and a machine checks that it really is an Intel program and
> that it starts. But nobody has yet rescued real photographs with it on a real
> Intel Mac. We expect it to work. We have not watched it work. If the photos
> matter and you have any doubt, use the memory-card method described in
> [Which method to use](#which-method-to-use) — it needs nothing installed at
> all and is the most reliable route on any computer.

### Windows, step by step

1. Open the [Releases page](https://github.com/gabrielepetteno/canon-powershot-s30-rescue/releases/latest)
   and click **`RetroCam-Rescue-Windows.exe`**. It downloads like any other
   file, normally into your **Downloads** folder.
2. Your browser may warn that this kind of file "can harm your computer" and
   ask you to confirm. Choose **Keep**. (In Chrome and Edge you sometimes have
   to click the three dots next to the download first, then **Keep anyway**.)
   That message appears for every program downloaded from the internet, not
   just this one.
3. **Double-click the downloaded file.** There is nothing to install. The
   program _is_ that single file. You can leave it in Downloads or drag it to
   your Desktop.
4. A blue window will appear saying **"Windows protected your PC"**. This is
   normal — see the explanation below; the program is not signed with a paid
   certificate. Click the small text **More info**, then the button
   **Run anyway**.
5. The **RetroCam Rescue** window opens. Continue with
   [How to use it](#how-to-use-it).

**One honest warning for Windows users.** Nobody has yet run this program on a
real Windows PC with a real old camera attached. Automated tests confirm that
the Windows version builds and starts, and the memory-card method uses the same
code that is thoroughly tested elsewhere — but the Windows-specific camera
parts have never met real hardware. If the photos are irreplaceable and you are
on Windows, use **a memory-card reader** (see
[Which method to use](#which-method-to-use)). It is the best-tested route on
every system. And if you do try the camera directly, please tell us what
happened — that report is genuinely valuable.

### Mac, step by step

1. **First, check which Mac you have.** Click the **Apple menu** at the very
   top-left of the screen, then **About This Mac**.
   - If you see a line saying **Chip: Apple M1** (or M2, M3, M4), you have an
     Apple Silicon Mac. Good — continue.
   - If you see **Processor: ... Intel ...**, you have an Intel Mac. Take the
     Intel file instead, and read the Intel note above first.
2. Open the [Releases page](https://github.com/gabrielepetteno/canon-powershot-s30-rescue/releases/latest)
   and click **`RetroCam-Rescue-macOS-AppleSilicon.zip`** (or
   **`RetroCam-Rescue-macOS-Intel.zip`** on an Intel Mac). It lands in your
   **Downloads** folder.
3. **Double-click the downloaded zip file.** It unpacks into an app called
   **RetroCam Rescue**. You can drag it into your **Applications** folder if
   you like things tidy — it works from anywhere.
4. **Double-click the app.** It will **not** open, and that is expected. A box
   appears saying macOS could not verify the app is free of malware, offering
   only **Move to Trash** and **Done**. Do not move it to the trash. Click
   **Done**. ([Why does it say that?](#why-macos-says-unidentified-developer))
5. Apple menu (top-left) → **System Settings** → **Privacy & Security**.
6. Scroll down to the **Security** section. There is now a new line saying
   **"RetroCam Rescue" was blocked**. Click **Open Anyway** beside it, and
   confirm with Touch ID or your Mac password.
7. A last box appears with an **Open** button. Click it. (If it does not
   appear, double-click the app again.) You never have to do this again.
   Continue with [How to use it](#how-to-use-it).

> Older instructions on the internet say to right-click the app and choose
> **Open**. On a Mac running macOS 14 or older that still works and saves you
> steps 5 and 6. Apple removed it in macOS 15, so on a recent Mac right-clicking
> just shows the same box again — use the steps above instead.

**Your Mac will ask permission twice more, later on.** These are macOS asking,
not the program, and both are normal:

- when you first search for a camera, a box asks whether RetroCam Rescue may
  access files on **a removable volume** — that is your memory card. Click
  **Allow**. If you refuse, the card becomes invisible to the program and you
  get "No camera found" with no other explanation.
- when the download starts, a box asks about your **Downloads folder**, because
  that is where the photos are going. Click **Allow**.

If you clicked the wrong button, you can change your mind: **System Settings** →
**Privacy & Security** → **Files and Folders** → **RetroCam Rescue**.

**Reading a memory card needs nothing else.** But to reach the **camera body
itself** over a cable, a Mac needs one extra free program called `gphoto2`.
Step 1 inside the app can install it for you — if you already have Homebrew (a
tool technical people use to install software on a Mac). If you do not have
Homebrew, the app will tell you so and will _not_ install it behind your back.
In that case, the simplest answer by far is a memory-card reader; see
[Which method to use](#which-method-to-use).

### Why macOS says "unidentified developer"

This is the part that frightens people, so here is the plain explanation.

macOS checks whether a program was signed with a certificate bought from Apple.
That certificate costs money every year, and this program is free and
non-commercial, so it does not have one. macOS therefore cannot confirm _who_
made the app, and it says so — as _"unidentified developer"_ on older versions,
and as _"Apple could not verify it is free of malware"_ on newer ones.

**It is not a virus warning.** macOS is not telling you the app is dangerous —
it is telling you it does not know who to blame if it were. You get the same
message from a great many free, well-known programs.

What you can do if you are still unsure, in order of effort:

- Use the **memory-card reader** method instead. It needs no program from the
  internet at all, and works with the file manager you already have.
- Ask someone technical to look. The whole source code is on this page,
  publicly readable, and each release lists a `SHA256SUMS.txt` file so a
  technical person can confirm the file you downloaded is the file that was
  built.

The Windows message ("Windows protected your PC") is the same thing in
different words, for the same reason.

---

## How to use it

The window is one column of five numbered steps, top to bottom. Do them in
order. Whatever the program is doing, it is written in the **Log** panel at the
bottom, which you can save to a file with the **Save log...** button.

**1. Environment.** The top box lists what this computer can use, each line
marked _available_ or _not installed_, with an **Install** button where the
program can help. You do not have to do anything here — a memory card in a
reader needs none of it. It matters only if you want to connect the camera
itself by cable. If a camera is listed later with the words
_"never tested on real hardware"_ under its name, believe that label.

**2. Camera.** Connect the camera with its USB cable and **switch it on**, or
put its memory card into a card reader. Then press **Search for camera**. After
a few seconds a line appears next to **Device:**. With a card in a reader it
looks like this:

```
Canon card (NO NAME) · port /Volumes/NO NAME · via Memory card or USB drive · 214 file(s)
```

and with the camera on the end of its cable, like this:

```
Canon PowerShot S30 · port usb:001,004 · via gphoto2 (vintage / proprietary protocol) · 214 file(s)
```

The words in the middle are the program telling you _how_ it got in; the part
that matters to you is the name at the start and the number of files at the
end. Both of those lines mean it worked. If more than one device is found,
choose the right one from the **Device** list next to the button. If nothing is
found, go to [If it does not work](#if-it-does-not-work).

**3. Destination.** This is the folder the photos will be copied into. It is
already filled in for you with a sensible suggestion inside your **Downloads**
folder, named after the camera and today's date, for example
`Downloads/PowerShot_S30_2026-08-13`. You can leave it exactly as it is. If you
want somewhere else, press **Browse...** and choose. The folder is created when
the download starts, not before. One rule: it must be a folder **on this
computer**, never on the camera's own card. If you pick the card, the program
stops and explains — copying the photos onto the card they came from would
leave you no second copy at all.

**4. Download.** Press the big **Download everything** button. The progress bar
moves and the line underneath names the file being copied, like
`12 / 214 · IMG_0012.JPG · 47%`. There is a **Cancel** button; pressing it is
always safe — the photos already copied stay copied, and nothing is ever erased
from the camera because of a cancelled run.

Be patient here. Over the camera's own cable this is genuinely slow: see
[If it does not work](#if-it-does-not-work).

**5. After the download.** A line in bold tells you the result, for example
_"214 of 214 downloaded and verified"_. Underneath is the **Delete from
camera** button.

**Why the Delete button is greyed out.** It stays disabled until _every single
file_ has been copied to your computer and read back and checked — because a
button that could erase a photo you do not actually have yet would be the most
dangerous thing in this program. Grey means "not proven yet", and the grey line
under the button tells you which of the three conditions is missing: no
download yet, some files not verified, or a connection that cannot erase at all
(a locked card, or a read-only connection). If some photos are genuinely
damaged on the card, they will never verify — so the button stays grey and the
originals stay where they are. That is deliberate.

When you do press it, the program asks you to confirm — twice, if 25 or more
photos are involved.

---

## Before you erase anything

Please read these four lines. They are the difference between a rescue and a
loss.

1. **Open the photos first.** Go to the destination folder and actually open
   several pictures — the first, the last, and a few in the middle. Look at
   them. The program checks each file thoroughly, but your own eyes are the
   last check that costs nothing.
2. **Count them.** The summary says "214 of 214". Does the number look like the
   number of photos you expected?
3. **Make a second copy somewhere else** before you erase the camera: another
   folder is not a backup, but an external drive, a second computer, or a USB
   stick is. Photographs that exist in exactly one place are one accident away
   from existing in none.
4. **Then, and only then, erase.** A memory card that has sat in a drawer for
   twenty years may not give you a second chance. There is no undo, on the card
   or in this program.

If the photos are precious and the card may be failing (odd noises, files that
appear and disappear, read errors), the safest course is to stop, leave the
card alone, and take it to a data-recovery specialist rather than reading it
repeatedly.

---

## If it does not work

| What you see                                                                   | What it means                                                                                                                                                                                   | What to do                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| ------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **"No camera found"** — camera is plugged in and switched on                   | Very likely nothing is broken. Your computer never recognised the camera in the first place, so the program has nothing to talk to.                                                             | Check the camera is **switched on** (many of these cameras show nothing at all to the computer while off). Plug the cable **straight into the computer**, not into a hub or a monitor. Look in the camera's own menu for a _USB_ or _PC connection_ setting. On a Mac, install gphoto2 in step 1. On Windows, use a card reader.                                                                                                                                      |
| **"No camera found"** — the memory card is in a reader                         | On a Mac, the likeliest cause is that the permission box was answered "Don't Allow". Otherwise the card did not mount, or the photos are not in the standard `DCIM` folder that cameras create. | On a Mac: **System Settings** → **Privacy & Security** → **Files and Folders** → **RetroCam Rescue**, and switch **Removable Volumes** on. Otherwise, check the card appears in Finder or File Explorer first. If it appears but the program finds nothing, the photos were probably moved out of `DCIM` at some point in the past — in that case simply copy the folders across by hand with Finder or File Explorer. Nothing is lost; the card is an ordinary disk. |
| **"Another program is holding the camera"**                                    | Another program on your computer grabbed the camera first and will not let go. This is the most common failure with a cable connection, and it is not a fault in your camera.                   | On a Mac, close **Photos** and **Image Capture** completely, unplug the camera, plug it back in, and press **Search for camera** again. The program already tries to release the camera by itself before each attempt, so a second try often just works. On Linux, close any file-manager window showing the camera.                                                                                                                                                  |
| **The transfer is extremely slow** — minutes for a handful of photos           | Nothing is wrong. That USB socket on the camera is from 2001, and the camera's way of handing over files adds more delay on top. A card reader does the same job 20 to 50 times faster.         | Let it run; it is not stuck. The progress line changes with each file. A 128 MB card can take 5–10 minutes, a 1 GB card over an hour. Do not unplug it. If it is too slow to bear, use a card reader: the same card copies in seconds.                                                                                                                                                                                                                                |
| **The camera switches itself off during the transfer**                         | Old batteries, or the camera's own power-saving timer.                                                                                                                                          | Use fresh batteries, or the mains adapter if you have one. Then simply start again into the **same folder**: photos already copied are re-checked rather than downloaded again, so the rescue picks up where it stopped.                                                                                                                                                                                                                                              |
| **Windows, and the camera is from before 2003**                                | There is no way for Windows itself to talk to these cameras any more. The old drivers were 32-bit and Windows 10/11 will not load them. This is a limitation of Windows, not of this program.   | Use a **card reader** — it turns a hard problem into a five-minute one. (A technical workaround exists; it is in [For developers](#for-developers).)                                                                                                                                                                                                                                                                                                                  |
| **The Delete button stays greyed out**                                         | Not every file was copied _and_ verified, or this connection cannot erase at all. The button is doing its job.                                                                                  | Read the grey line under the button and the summary above it: they name what is missing. Run the download again to retry failed files. Files that never verify are genuinely damaged on the card — leave them there.                                                                                                                                                                                                                                                  |
| **Some photos are reported as damaged**                                        | They probably are. A card that has sat unused since 2004 can have bad spots, and transfers interrupted twenty years ago can leave half-written files.                                           | They are still copied to your computer — they are just not offered for deletion. Try a photo-repair tool on the copies. **Do not erase them from the card.**                                                                                                                                                                                                                                                                                                          |
| **The Mac app will not open**, or says it "cannot be verified" or "is damaged" | The app is not signed with a paid Apple certificate. It is not corrupted.                                                                                                                       | Follow [Mac, step by step](#mac-step-by-step): System Settings → Privacy & Security → **Open Anyway**. Do not click **Move to Trash**.                                                                                                                                                                                                                                                                                                                                |
| **Windows: the downloaded file disappears on its own**                         | Your antivirus quarantined it. This is a false alarm typical of programs packed into a single file, and it is not proof of anything.                                                            | Restore it from your antivirus's quarantine list. If you would rather not overrule your antivirus — a perfectly reasonable choice — use a **card reader** instead; it needs no program at all.                                                                                                                                                                                                                                                                        |
| **Nothing here helped**                                                        | —                                                                                                                                                                                               | **Buy a CompactFlash card reader.** See below.                                                                                                                                                                                                                                                                                                                                                                                                                        |

Still stuck? Open an issue on
[GitHub](https://github.com/gabrielepetteno/canon-powershot-s30-rescue/issues)
saying which computer and which camera you have, and **what you saw on screen**,
word for word. If the program opened at all, attach the log (**Log** panel →
**Save log...**) — it contains your folder and file names, so read it first and
remove anything you would rather not publish. A report of a failure is a useful
contribution, not a nuisance.

### The single most effective fix

**Buy a CompactFlash card reader — €10 to €15, any electronics shop or online.**

Take the memory card out of the camera, put it in the reader, plug the reader
into your computer, and skip the camera entirely.

This works because the _card_ was never exotic. Cameras of that era wrote to
ordinary CompactFlash cards in an ordinary format that every computer built
since has been able to read. Only the _camera's_ way of talking over USB went
obsolete.

- **It cannot fail for driver reasons.** There is no driver, no protocol, no
  cooperation from a twenty-year-old camera required.
- **It is 20 to 50 times faster.** Seconds instead of an hour.
- **The camera's battery cannot die halfway through**, because the camera is
  not involved.

Check which card your camera uses before buying — most of these models use
**CompactFlash Type I**, a few use SmartMedia; it is printed inside the card
door or in the manual. If the photos matter to you, this is the thing to do.

---

## Which method to use

The program never asks you to choose. It tries all three ways of reaching your
photos and shows you what it found. This table is only so you know what you are
looking at.

| Method                        | What it is                                                                                                       | What you need                                                                               | Works on                       | Can it erase the camera afterwards?                     |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- | ------------------------------ | ------------------------------------------------------- |
| **Memory card or USB drive**  | The card taken out of the camera and put into a reader. **The recommended way.**                                 | A card reader (€10–15). No software at all.                                                 | Windows, Mac, Linux            | Yes, unless the card is locked or write-protected       |
| **gphoto2** (camera by cable) | Talks to the old camera directly in its own language. The **only** way to reach a pre-2003 Canon over its cable. | The free `gphoto2` program — step 1 of the app can install it on a Mac if you have Homebrew | Mac and Linux. **Not Windows** | Usually yes; individual protected files may refuse      |
| **Windows camera (WIA)**      | Uses the camera support built into Windows, which only works for cameras from roughly 2003 onwards.              | Nothing to install: it is inside the downloaded program                                     | Windows only                   | Sometimes; often not. **Never tested on real hardware** |

The order is a safety decision. The card reader is tried first because it
cannot fail for driver reasons. gphoto2 comes second because it can reach the
camera body itself. The Windows method is last, because it only sees cameras
Windows already understands — and a 2001 PowerShot is not one of them.

---

## Is it safe?

**Yes, by design, and here is exactly what that means.**

- **It only reads, until you say otherwise.** Finding the camera, listing the
  photos and copying them never change a photo on the card. (The one small
  exception is the write test described below.)
- **It never formats or wipes a card.** There is no such button. Deletion is
  always file by file, one named photo at a time, never "everything matching".
- **It cannot delete a photo it has not proven it saved.** Before a single file
  is erased, that exact file must have been copied to your computer, read back
  and checked against the size the camera reported, checked for a valid image
  structure, and confirmed still present on your disk one last instant before
  the erase. If anything at all is uncertain, the answer is no.
- **It never overwrites anything.** If two photos would end up with the same
  name — which happens often, because these cameras restart their numbering —
  the second becomes `118CANON_IMG_0001.JPG` rather than replacing the first.
- **A half-copied file never gets the final name.** If the transfer is
  interrupted, you are never left with a file that looks complete but is not.
- **It is 100% offline.** No account, no sign-up, no upload, no cloud, no
  analytics, no telemetry, no crash reports, no update check. Your photographs
  never leave your computer. The program opens a network connection in exactly
  one situation: if _you_ press an **Install** button in step 1, the package
  manager already on your computer downloads that package. Never press it, and
  the program never touches the network.

**The one exception, stated plainly.** When you use a card in a reader, the
program has to find out whether that card accepts writing at all — otherwise it
cannot know whether the **Delete** button would work.
It does this by creating one empty, zero-byte file inside the card's `DCIM`
folder — or, if there is no `DCIM` folder, at the top of the card — and
deleting it again immediately. It happens as soon as the card is found — that
is, when you press **Search for camera**, not later — once per card, and it is
the only write this program ever performs outside a deletion you asked for. On
these cards an empty
file uses no actual storage space. If even that is one write too many for a
card you are worried about, do not use this program on the card directly: have
a specialist make an image of it first and rescue the photos from the image.

**Something no program can control**: your operating system may write to the
card by itself the moment you plug it in — macOS creates hidden index files,
Windows creates a hidden folder — before this program is even running. SD cards
have a small lock switch that prevents this; CompactFlash cards do not. This is
another reason a genuinely fragile card belongs with a specialist.

---

## Project status

Version 0.1.0. Honest summary, no marketing.

**What is tested and ready to use:**

- **The memory-card route, on every system.** This is the best-proven path in
  the project. Its automated tests run against real folders of real files on a
  real disk, with nothing faked.
- **macOS**, including the app you can download. It has been built and run.
- **A real Canon PowerShot S30**, connected over its own cable with gphoto2, on
  macOS, end to end with real photographs: found, listed, downloaded, verified,
  and erased only after verification. Including the awkward cases — two photos
  with the same filename, and a damaged file that the delete gate correctly
  refused to clear.
- **387 automated tests**, all passing, in about 11 seconds, on Python 3.9 and
  on current Python, and re-run automatically before any downloadable file is
  published. They cover the delete gate, the transfer engine, all three
  connection methods, and a permanent test for every defect ever found in
  review. The test code has itself been checked by deliberately introducing 86
  faults to confirm the tests catch them; all 86 were caught.

**What is not tested — please do not assume otherwise:**

- **The Windows camera path has never run on real Windows hardware.** Not once.
  It is written against Microsoft's documentation and exercised only against an
  imitation of Windows on a Mac. It is built to fail visibly rather than
  silently, and every camera it finds is labelled _"never tested on real
  hardware"_ under its own name, in the window where you press Delete — but if your photos are
  irreplaceable and you are on Windows, use a card reader. **Help wanted:** a
  report from a real Windows PC, even a failing one, is the most useful
  contribution this project can receive.
- **Intel Macs.** An Intel build is produced automatically for every release,
  and CI checks that it really is an Intel binary and that it starts. Nobody has
  yet rescued real photographs with it on a real Intel Mac. The hands-on
  end-to-end run described above was done on Apple Silicon.
- **Most of the app window itself** is not covered by automated tests. The two
  behaviours that could cost you a photograph are — the Delete button's own
  lock, and throwing away the previous result when you switch cameras.
- **No test has ever spoken to a real camera.** The camera-by-cable tests drive
  a stand-in that imitates the real tool's output. A real 2001 Canon is messier
  than any imitation. The one thing that proves it works is the real-hardware
  run described above.

---

## For developers

Everything above this line is for people who just want their photos. This part
is not.

### Run from source

Requires **Python 3.9+** with Tkinter (Tk 8.6+). No third-party packages are
required.

```bash
git clone https://github.com/gabrielepetteno/canon-powershot-s30-rescue.git
cd canon-powershot-s30-rescue

./run.sh          # macOS / Linux (or double-click "RetroCam Rescue.command")
run.bat           # Windows
```

The launchers locate a suitable interpreter, verify Tkinter imports, set
`PYTHONPATH=src`, and start the GUI. Override with `RETROCAM_PYTHON=/path/to/python`.

As a package:

```bash
pip install .                 # or pipx install .
retrocam                      # GUI
retrocam-cli --cli            # headless, read-only detect-and-list; paste this into bug reports
pip install ".[image]"        # Pillow: full image decode during verification
pip install ".[windows]"      # pywin32: enables the WIA backend
```

macOS needs `brew install gphoto2` to reach a pre-PTP body; Linux needs
`gphoto2` plus `python3-tk`. There is no working gphoto2 for native Windows —
the route there is a card reader, or WSL2 with `usbipd-win` forwarding the USB
device into Linux.

### Run the tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

387 tests, about 11 seconds, no network, no camera, no admin rights, nothing
left behind. Six skip without Pillow. It also runs under pytest:

```bash
uv run --group dev python -m pytest -q
```

CI runs the suite on Python 3.9 and 3.13 before building any release artefact
(`.github/workflows/build.yml`, triggered on `v*` tags).

### How the backends plug in

Every transport is a `CameraBackend` subclass
([`src/retrocam/backends/base.py`](src/retrocam/backends/base.py)) implementing
`is_available`, `detect`, `list_files`, `download`, `delete`. The GUI never
imports a backend directly; it asks
[`registry.py`](src/retrocam/registry.py), which holds a static list ordered by
reliability — mass storage, gphoto2, WIA.

To add one: subclass `CameraBackend` in a new module under
`src/retrocam/backends/`, add a `BackendKind` in
[`model.py`](src/retrocam/model.py), and add one static import plus one entry
to `ALL_BACKENDS` in `registry.py`. Keep the import static — lazy or
`importlib`-based discovery silently yields an empty backend list inside a
frozen PyInstaller build.

The contract is documented at the top of `base.py`. The load-bearing rules:
never raise a raw exception (wrap it in a `CameraError` carrying a message a
frightened non-technical user can act on), never write to the device outside
`delete()`, report exact byte sizes from `list_files()`, and check the cancel
token between files. `TransferEngine.delete_verified` in
[`transfer.py`](src/retrocam/transfer.py) is the only place in the entire
program that calls `backend.delete()`.

Bug reports and hardware reports are contributions — see
[CONTRIBUTING.md](CONTRIBUTING.md). Build details are in
[packaging/build.md](packaging/build.md).

### Licence

MIT — see [LICENSE](LICENSE). Use it, fork it, sell it, embed it.

No warranty. This software erases files from memory cards when you ask it to,
and although it goes to considerable lengths to prove a good copy exists first,
your backups are your own responsibility.
