"""Two-language string table (Italian and English), with no dependencies.

Why a hand-written table instead of ``gettext``: a rescue tool must be a single
folder of ``.py`` files that runs from a USB stick on a machine that may have no
compiler, no network and no ``msgfmt``. Compiled ``.mo`` catalogues would add a
build step and a class of "works on my machine" failures for the sake of two
languages that fit in one file.

Contract
--------
* :func:`t` never raises. An unknown key returns the key itself, and a template
  whose placeholders do not match the arguments returns the template unformatted.
  A missing translation must degrade to something ugly, never to a crash in
  front of someone rescuing twenty-year-old photographs.
* Every key that exists in one language exists in the other. Should one drift,
  the English entry is used as the fallback, so the worst case is a mixed-language
  line rather than a raw key. :func:`missing_translations` exists so a test can
  catch the drift.
* Placeholders use :meth:`str.format` syntax (``{name}``). Values passed in are
  never re-scanned for placeholders, so a device path containing braces is safe.

Callers that must survive a broken table (the backends, the transfer engine)
import :func:`t` defensively and carry their own English fallback; that is by
design, and it is why nothing here is allowed to raise at import time either.
"""

from __future__ import annotations

import locale
import os
import sys
import threading
from typing import Any, Dict, List, Tuple

__all__ = [
    "t",
    "set_language",
    "current_language",
    "available_languages",
    "detect_language",
    "missing_translations",
    "DEFAULT_LANGUAGE",
]


#: The language used when nothing better can be determined, and the fallback
#: table for any key a translation is missing.
DEFAULT_LANGUAGE = "en"

#: Environment variable that overrides locale detection entirely. Useful for
#: screenshots, bug reports and tests: ``RETROCAM_LANG=it python -m retrocam``.
_ENV_OVERRIDE = "RETROCAM_LANG"


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #
#
# Keys are grouped by the module that consumes them:
#   app.*, env.*, deps.label.*, camera.*, dest.*, run.*, after.*, log.*,
#   error.*/info.*/quit.*/busy.*   -> the Tkinter GUI
#   main.*, cli.*                  -> the __main__ entry point
#   transfer.*                     -> retrocam.transfer (which also carries its
#                                     own English fallbacks, so these are the
#                                     Italian half of the job)
#   gphoto2.*                      -> the gphoto2 backend, which has no fallback
#                                     text of its own beyond echoing the key
#
# Style rules for the user-facing text: say what happened and what to do next;
# never mention a stack trace; never promise that something was erased unless it
# was.

_EN: Dict[str, str] = {
    # -- window chrome ----------------------------------------------------- #
    "app.title": "RetroCam Rescue",
    "app.tagline": "Rescue the photos from an old digital camera. "
    "Nothing is ever erased before every file has been copied and checked.",
    # -- 1. environment ---------------------------------------------------- #
    "env.section": "1. Environment",
    "env.intro": "What RetroCam can use on this computer. "
    "A memory card in a reader needs none of it.",
    "env.refresh": "Re-check",
    "env.install": "Install",
    "env.checking": "Checking the environment...",
    "env.checked": "Environment checked.",
    "env.none": "Nothing to check on this system.",
    "env.present": "available",
    "env.missing": "not installed",
    "env.install_started": "Installing {label}...",
    "env.install_ok": "{label} installed. {message}",
    "env.install_failed": "{label} was not installed. {message}",
    "env.install_title": "Install {label}",
    "env.backend_ok": "Connection available: {name}",
    "env.backend_ko": "Connection unavailable: {name} - {hint}",
    # Labels for deps.Dependency.key. Unknown keys fall back to Dependency.label.
    "deps.label.gphoto2": "gphoto2",
    "deps.label.homebrew": "Homebrew",
    "deps.label.pillow": "Pillow",
    "deps.label.pywin32": "pywin32",
    "deps.label.wsl": "WSL2",
    "deps.label.usbipd": "usbipd-win",
    # -- 2. camera --------------------------------------------------------- #
    "camera.section": "2. Camera",
    "camera.intro": "Connect the camera and switch it on, or put its memory card "
    "into a reader, then press Search.",
    "camera.search": "Search for camera",
    "camera.searching": "Looking for a camera...",
    "camera.device": "Device:",
    "camera.none_selected": "No camera found yet.",
    "camera.none": "No camera found.",
    "camera.none_help": "Check that the camera is switched on, that the cable is "
    "plugged into the computer directly (not through a hub), and that the card is "
    "not write-protected. A card reader is always the fastest and safest route.",
    "camera.found": "{n} device(s) found.",
    "camera.info": "{model} · port {port} · via {backend}",
    "camera.info_files": "{model} · port {port} · via {backend} · {n} file(s)",
    "camera.listing": "Reading the file list...",
    "camera.listed": "{n} file(s) on the camera.",
    "camera.empty": "The camera reports no files.",
    "camera.list_failed": "The file list could not be read.",
    # -- 3. destination ---------------------------------------------------- #
    "dest.section": "3. Destination",
    "dest.intro": "The photos are copied here. The folder is created when the "
    "download starts.",
    "dest.browse": "Browse...",
    "dest.dialog": "Choose the destination folder",
    "dest.missing": "Choose a destination folder first.",
    # -- 4. download ------------------------------------------------------- #
    "run.section": "4. Download",
    "run.start": "Download everything",
    "run.cancel": "Cancel",
    "run.idle": "Ready.",
    "run.cancelling": "Stopping after the current file...",
    "run.starting": "Copying {n} file(s) into {dest}",
    "run.item": "{index} / {total} · {name}",
    "run.item_bytes": "{index} / {total} · {name} · {percent}%",
    "run.working": "Working...",
    "run.finished": "Download finished.",
    "run.aborted": "Download stopped. The files already copied are safe in the "
    "destination folder and nothing was erased from the camera.",
    "run.need_camera": "Search for a camera first.",
    "run.no_files": "There is nothing to download: the camera reports no files.",
    # -- 5. after the download --------------------------------------------- #
    "after.section": "5. After the download",
    "after.none": "No download yet.",
    "after.summary": "{ok} of {total} downloaded and verified",
    "after.summary_failed": "{ok} of {total} downloaded and verified - "
    "{failed} could not be recovered",
    "after.summary_aborted": "{ok} of {total} downloaded and verified before the "
    "run was stopped",
    "after.delete": "Delete from camera",
    "after.hint_none": "Deletion becomes possible only after a download.",
    "after.hint_unverified": "Deletion stays locked: every requested file must be "
    "downloaded and verified first.",
    "after.hint_unsupported": "This connection cannot erase files (it is "
    "read-only, or the card is write-protected).",
    "after.hint_ready": "{n} file(s) are verified on disk and can be erased from "
    "the camera.",
    "after.confirm_title": "Erase from the camera?",
    "after.confirm_body": "{n} file(s) will be erased from the camera.\n\n"
    "Every one of them has been copied to:\n{dest}\n\n"
    "and read back and checked. Nothing else on the camera is touched.\n\n"
    "This cannot be undone. Continue?",
    "after.confirm2_title": "Last check",
    "after.confirm2_body": "{n} photographs are about to be erased from the "
    "camera for good.\n\nIf you have not looked at the copies in the destination "
    "folder yet, say No, open the folder, and come back.\n\nErase them now?",
    "after.deleting": "Erasing from the camera...",
    "after.deleted_all": "{n} file(s) erased from the camera.",
    "after.deleted_partial": "{ok} file(s) erased, {failed} kept on the camera.",
    "after.deleted_none": "Nothing was erased from the camera.",
    "after.delete_title": "Deletion finished",
    "after.delete_kept": "Kept on the camera: {name} - {why}",
    "after.delete_recheck": "Press Search for camera again to see what is left on "
    "the card.",
    # -- log --------------------------------------------------------------- #
    "log.section": "Log",
    "log.save": "Save log...",
    "log.dialog": "Save the log",
    "log.saved": "Log saved to {path}",
    "log.save_failed": "The log could not be saved: {err}",
    "log.empty": "There is nothing to save yet.",
    "log.filter_log": "Log file",
    "log.filter_text": "Text file",
    "log.filter_all": "All files",
    "log.started": "RetroCam Rescue {version} - {platform} - Python {python}",
    # -- dialogs ----------------------------------------------------------- #
    "error.title": "Error",
    "error.unexpected": "Unexpected failure ({kind}): {err}",
    "info.title": "RetroCam Rescue",
    "warn.title": "Careful",
    "quit.title": "Quit RetroCam Rescue?",
    "quit.body": "An operation is still running.\n\nQuitting now stops it. The "
    "files already copied stay in the destination folder, and nothing has been "
    "erased from the camera.\n\nQuit anyway?",
    "busy.title": "One thing at a time",
    "busy.body": "Another operation is still running. Wait for it to finish, or "
    "press Cancel.",
    # -- entry point / headless mode --------------------------------------- #
    "main.description": "Rescue photos from an old digital camera.",
    "main.help_lang": "Interface language: it, en, or auto (default: auto).",
    "main.help_version": "Print the version and exit.",
    "main.help_cli": "Do not open the window: detect the camera, list its files "
    "and print the result.",
    "main.tk_missing_title": "Tkinter is missing",
    "main.tk_missing": "RetroCam Rescue needs Tkinter, the graphical toolkit that "
    "normally ships with Python, and this Python does not have it "
    "({err}).\n\n{hint}\n\nYou can still use the text mode in the meantime:\n"
    "    {argv0} --cli",
    "main.tk_hint_macos": "Install Python from python.org (its installer includes "
    "Tk), or with Homebrew run: brew install python-tk",
    "main.tk_hint_linux": "Install your distribution's Tk package, for example:\n"
    "    sudo apt install python3-tk        (Debian, Ubuntu, Mint)\n"
    "    sudo dnf install python3-tkinter   (Fedora, RHEL)\n"
    "    sudo pacman -S tk                  (Arch)",
    "main.tk_hint_windows": "Re-run the Python installer, choose Modify, and tick "
    "'tcl/tk and IDLE'.",
    "main.tk_hint_generic": "Install the Tk support package for this Python.",
    "main.import_failed": "RetroCam Rescue could not start: {err}",
    "main.gui_failed": "The window could not be opened: {err}",
    "cli.header": "RetroCam Rescue {version} - text mode (read-only)",
    "cli.env": "Environment",
    "cli.env_row": "  [{mark}] {label} {version} {hint}",
    "cli.backends": "Connections",
    "cli.backend_row": "  [{mark}] {name} {hint}",
    "cli.detecting": "Looking for a camera...",
    "cli.no_camera": "No camera found.",
    "cli.camera": "[{n}] {model} - port {port} - via {backend}",
    "cli.detail": "    {detail}",
    "cli.listing": "    reading the file list...",
    "cli.files": "    {n} file(s), {size}",
    "cli.file": "    {folder}/{name}  {size}",
    "cli.more": "    ... and {n} more",
    "cli.list_failed": "    the file list could not be read: {err}",
    "cli.readonly": "Text mode only reads. Nothing was downloaded and nothing was "
    "erased. Run without --cli to open the window.",
    "cli.interrupted": "Interrupted.",
    # -- transfer engine (English fallbacks live in transfer.py) ------------ #
    "transfer.summary.dest": "Destination: {dest}",
    "transfer.summary.recovered": "Recovered and verified: {ok} of {total}",
    "transfer.summary.aborted": "Run interrupted by the user - the remaining "
    "files were not transferred.",
    "transfer.summary.skipped": "Already present and re-checked on disk: {n}",
    "transfer.summary.deep": "Fully decoded during the check: {deep} of {ok} "
    "(the rest passed the structural check)",
    "transfer.summary.failed": "Not recovered: {n}",
    "transfer.summary.more": "  ... and {n} more",
    "transfer.summary.unknown_reason": "unknown reason",
    "transfer.summary.deletable": "Safe to erase from the camera: {n}",
    "transfer.msg.listed": "{n} file(s) found on the camera ({size}).",
    "transfer.msg.cancelled": "Cancelled - checking which files completed before "
    "stopping.",
    "transfer.msg.verified": "Checked {n} file(s) on disk.",
    "transfer.msg.recovered_after_cancel": "{n} file(s) completed and verified "
    "before the run was stopped.",
    "transfer.msg.space_ok": "{need} to copy, {free} free in {dest}.",
    "transfer.msg.space_partial": "({n} file(s) of unreported size are not included.)",
    "transfer.msg.space_unknown": "The camera did not report file sizes; free "
    "space could not be checked.",
    "transfer.msg.space_uncheckable": "Free space on {dest} could not be "
    "measured; continuing.",
    "transfer.msg.partial_delete": "{bad} file(s) were not recovered and will be "
    "left on the camera.",
    "transfer.msg.delete_refused": "Keeping {name} on the camera: {why}",
    "transfer.msg.delete_all_refused": "Nothing was erased: the verified copies "
    "are no longer on disk.",
    "transfer.msg.deleting": "Erasing {n} verified file(s) from {camera}.",
    "transfer.err.list_failed": "Could not read the file list from {camera}: {err}",
    "transfer.err.download_failed": "The transfer stopped unexpectedly: {err}. "
    "Nothing was erased from the camera.",
    "transfer.err.dest_create": "Cannot create the destination folder {dest}: "
    "{err}. Choose another folder.",
    "transfer.err.dest_not_dir": "The destination {dest} is not a folder. Choose "
    "another folder.",
    "transfer.err.dest_readonly": "Cannot write into {dest}: {err}. Pick a folder "
    "you own, such as your Downloads folder.",
    "transfer.err.dest_on_camera": "The destination {dest} is on the camera's own "
    "memory card ({root}). Copying the photos onto the card they came from would "
    "leave no second copy, so nothing was transferred. Choose a folder on this "
    "computer, such as your Downloads folder.",
    "transfer.err.no_space": "Not enough free space in {dest}: about {need} is "
    "required (including a 10% margin) but only {free} is available. Free some "
    "space or choose another folder.",
    "transfer.err.outcome_unknown": "The camera driver reported a file that was "
    "not requested ({path}). The downloaded files are safe in the destination "
    "folder and nothing was erased from the camera.",
    "transfer.err.outcome_missing": "The camera driver did not report back on {n} "
    "file(s), starting with {path}. The downloaded files are safe in the "
    "destination folder and nothing was erased from the camera.",
    "transfer.err.not_verified": "cancelled before this file could be checked",
    "transfer.err.no_bytes": "no file was written",
    "transfer.err.verify_crashed": "the integrity check could not run: {err}",
    "transfer.err.cancelled_before": "cancelled before this file was transferred",
    "transfer.err.delete_unsupported": "This connection cannot erase files from "
    "{camera}. The card may be write-protected, or the driver may be read-only.",
    "transfer.err.delete_failed": "Erasing stopped unexpectedly: {err}. Re-scan "
    "the camera to see which files are still there.",
    "transfer.err.delete_outside": "The {backend} backend reported erasing {n} "
    "file(s) that were never verified: {paths}. Stop using this card and copy "
    "anything left on it.",
    "transfer.err.gate_no_dest": "no local copy was recorded",
    "transfer.err.gate_unreadable": "the local copy is no longer readable ({err})",
    "transfer.err.gate_missing": "the local copy is missing from {dest}",
    "transfer.err.gate_empty": "the local copy is empty",
    "transfer.err.gate_changed": "the local copy changed since it was checked "
    "({now} bytes, expected {want})",
    # -- gphoto2 backend ---------------------------------------------------- #
    "gphoto2.missing_macos": "gphoto2 is not installed - press Install to add it "
    "with Homebrew.",
    "gphoto2.missing_linux": "gphoto2 is not installed - install it with your "
    "distribution's package manager.",
    "gphoto2.missing_generic": "gphoto2 is not installed.",
    "gphoto2.unavailable_windows": "gphoto2 has no supported Windows build. Use a "
    "memory card reader, or the WSL2 bridge.",
    "gphoto2.install_hint_windows": "Use a memory card reader, or set up WSL2 "
    "with usbipd-win.",
    "gphoto2.broken_binary": "gphoto2 was found at {path} but it will not run.",
    "gphoto2.released_ptp": "Asked the system to release the camera so gphoto2 "
    "can claim it ({name}).",
    "gphoto2.detecting": "Asking gphoto2 which cameras are connected...",
    "gphoto2.detected_none": "gphoto2 sees no camera. Switch the camera on, and "
    "plug it in directly rather than through a hub.",
    "gphoto2.detected_one": "gphoto2 found {model} on {port}.",
    "gphoto2.listing": "Reading the file list through gphoto2...",
    "gphoto2.listed": "{count} file(s) listed.",
    "gphoto2.downloading": "Downloading {name}",
    "gphoto2.skipped_existing": "{name} is already in the destination folder - "
    "it will be checked, not copied again.",
    "gphoto2.deleting": "Erasing {name} from the camera",
    "gphoto2.delete_confirming": "Re-reading the camera to confirm what was erased...",
    "gphoto2.delete_unconfirmed": "the camera did not confirm the deletion",
    "gphoto2.still_present": "the file is still on the camera",
    "gphoto2.timeout": "The camera did not answer within {seconds} s. Switch it "
    "off and on, then try again.",
    "gphoto2.cancelled": "Stopped at your request.",
    "gphoto2.spawn_failed": "gphoto2 could not be started: {detail}",
    "gphoto2.dest_unwritable": "Cannot write into {path}: {detail}",
    "gphoto2.no_output": "gphoto2 wrote no file",
    "gphoto2.empty_file": "the file arrived empty",
    "gphoto2.size_mismatch": "incomplete transfer: {got} bytes instead of {expected}",
    "gphoto2.replace_failed": "the file could not be put into place: {detail}",
    "gphoto2.err_claim": "Another program is holding the camera. On macOS close "
    "Photos and Image Capture; on Linux stop gvfs-gphoto2-volume-monitor, then "
    "try again. {detail}",
    "gphoto2.err_unplugged": "The camera was unplugged or switched itself off. "
    "{detail}",
    "gphoto2.err_no_camera": "No camera is connected, or it is not switched on. "
    "{detail}",
    "gphoto2.err_io": "The USB link failed while talking to the camera. Try "
    "another cable or another port - avoid hubs. {detail}",
    "gphoto2.err_port_timeout": "The camera stopped answering. Switch it off and "
    "on, then try again. {detail}",
    "gphoto2.err_os": "The operating system refused the request. {detail}",
    "gphoto2.err_camera_op": "The camera refused this operation. {detail}",
    "gphoto2.err_file_not_found": "That file is no longer on the camera. {detail}",
    "gphoto2.err_dir_not_found": "That folder is no longer on the camera. {detail}",
    "gphoto2.err_no_space": "There is not enough free space. {detail}",
    "gphoto2.err_busy": "The camera is busy. Wait a moment and try again. {detail}",
    "gphoto2.err_corrupt": "The data coming from the camera is damaged. {detail}",
    "gphoto2.err_bad_params": "gphoto2 rejected the request. {detail}",
    "gphoto2.err_unknown_port": "That USB port no longer exists - the camera was "
    "probably unplugged. {detail}",
    "gphoto2.err_unsupported": "This camera does not support that operation. {detail}",
    "gphoto2.err_generic": "gphoto2 failed. {detail}",
}


_IT: Dict[str, str] = {
    # -- window chrome ----------------------------------------------------- #
    "app.title": "RetroCam Rescue",
    "app.tagline": "Recupera le foto da una vecchia fotocamera digitale. "
    "Niente viene mai cancellato prima che ogni file sia stato copiato e "
    "verificato.",
    # -- 1. environment ---------------------------------------------------- #
    "env.section": "1. Ambiente",
    "env.intro": "Cosa puo usare RetroCam su questo computer. "
    "Una scheda di memoria in un lettore non ha bisogno di nulla.",
    "env.refresh": "Ricontrolla",
    "env.install": "Installa",
    "env.checking": "Controllo dell'ambiente in corso...",
    "env.checked": "Controllo dell'ambiente completato.",
    "env.none": "Non c'e nulla da controllare su questo sistema.",
    "env.present": "disponibile",
    "env.missing": "non installato",
    "env.install_started": "Installazione di {label} in corso...",
    "env.install_ok": "{label} installato. {message}",
    "env.install_failed": "{label} non e stato installato. {message}",
    "env.install_title": "Installa {label}",
    "env.backend_ok": "Collegamento disponibile: {name}",
    "env.backend_ko": "Collegamento non disponibile: {name} - {hint}",
    "deps.label.gphoto2": "gphoto2",
    "deps.label.homebrew": "Homebrew",
    "deps.label.pillow": "Pillow",
    "deps.label.pywin32": "pywin32",
    "deps.label.wsl": "WSL2",
    "deps.label.usbipd": "usbipd-win",
    # -- 2. camera --------------------------------------------------------- #
    "camera.section": "2. Fotocamera",
    "camera.intro": "Collega la fotocamera e accendila, oppure inserisci la sua "
    "scheda di memoria in un lettore, poi premi Cerca.",
    "camera.search": "Cerca la fotocamera",
    "camera.searching": "Ricerca della fotocamera in corso...",
    "camera.device": "Dispositivo:",
    "camera.none_selected": "Nessuna fotocamera trovata per ora.",
    "camera.none": "Nessuna fotocamera trovata.",
    "camera.none_help": "Controlla che la fotocamera sia accesa, che il cavo sia "
    "collegato direttamente al computer (non a un hub) e che la scheda non sia "
    "protetta da scrittura. Un lettore di schede resta la strada piu veloce e "
    "piu sicura.",
    "camera.found": "{n} dispositivo/i trovato/i.",
    "camera.info": "{model} · porta {port} · tramite {backend}",
    "camera.info_files": "{model} · porta {port} · tramite {backend} · {n} file",
    "camera.listing": "Lettura dell'elenco dei file in corso...",
    "camera.listed": "{n} file sulla fotocamera.",
    "camera.empty": "La fotocamera non riporta alcun file.",
    "camera.list_failed": "Non e stato possibile leggere l'elenco dei file.",
    # -- 3. destination ---------------------------------------------------- #
    "dest.section": "3. Destinazione",
    "dest.intro": "Le foto vengono copiate qui. La cartella viene creata quando "
    "parte il download.",
    "dest.browse": "Sfoglia...",
    "dest.dialog": "Scegli la cartella di destinazione",
    "dest.missing": "Scegli prima una cartella di destinazione.",
    # -- 4. download ------------------------------------------------------- #
    "run.section": "4. Download",
    "run.start": "Scarica tutto",
    "run.cancel": "Annulla",
    "run.idle": "Pronto.",
    "run.cancelling": "Interruzione dopo il file in corso...",
    "run.starting": "Copia di {n} file in {dest}",
    "run.item": "{index} / {total} · {name}",
    "run.item_bytes": "{index} / {total} · {name} · {percent}%",
    "run.working": "Elaborazione in corso...",
    "run.finished": "Download completato.",
    "run.aborted": "Download interrotto. I file gia copiati sono al sicuro nella "
    "cartella di destinazione e niente e stato cancellato dalla fotocamera.",
    "run.need_camera": "Cerca prima una fotocamera.",
    "run.no_files": "Non c'e nulla da scaricare: la fotocamera non riporta alcun file.",
    # -- 5. after the download --------------------------------------------- #
    "after.section": "5. Dopo il download",
    "after.none": "Nessun download effettuato.",
    "after.summary": "{ok} di {total} scaricati e verificati",
    "after.summary_failed": "{ok} di {total} scaricati e verificati - "
    "{failed} non recuperati",
    "after.summary_aborted": "{ok} di {total} scaricati e verificati prima "
    "dell'interruzione",
    "after.delete": "Cancella dalla fotocamera",
    "after.hint_none": "La cancellazione diventa possibile solo dopo un download.",
    "after.hint_unverified": "Cancellazione bloccata: ogni file richiesto deve "
    "prima essere scaricato e verificato.",
    "after.hint_unsupported": "Questo collegamento non puo cancellare file (e in "
    "sola lettura, oppure la scheda e protetta da scrittura).",
    "after.hint_ready": "{n} file sono verificati su disco e possono essere "
    "cancellati dalla fotocamera.",
    "after.confirm_title": "Cancellare dalla fotocamera?",
    "after.confirm_body": "{n} file verranno cancellati dalla fotocamera.\n\n"
    "Ognuno di essi e stato copiato in:\n{dest}\n\n"
    "ed e stato riletto e verificato. Nient'altro sulla fotocamera viene "
    "toccato.\n\nL'operazione non e reversibile. Continuare?",
    "after.confirm2_title": "Ultima conferma",
    "after.confirm2_body": "{n} fotografie stanno per essere cancellate "
    "definitivamente dalla fotocamera.\n\nSe non hai ancora aperto le copie nella "
    "cartella di destinazione, rispondi No, controllale e torna qui.\n\n"
    "Cancellarle adesso?",
    "after.deleting": "Cancellazione dalla fotocamera in corso...",
    "after.deleted_all": "{n} file cancellati dalla fotocamera.",
    "after.deleted_partial": "{ok} file cancellati, {failed} lasciati sulla "
    "fotocamera.",
    "after.deleted_none": "Non e stato cancellato nulla dalla fotocamera.",
    "after.delete_title": "Cancellazione terminata",
    "after.delete_kept": "Lasciato sulla fotocamera: {name} - {why}",
    "after.delete_recheck": "Premi di nuovo Cerca la fotocamera per vedere cosa e "
    "rimasto sulla scheda.",
    # -- log --------------------------------------------------------------- #
    "log.section": "Registro",
    "log.save": "Salva il registro...",
    "log.dialog": "Salva il registro",
    "log.saved": "Registro salvato in {path}",
    "log.save_failed": "Non e stato possibile salvare il registro: {err}",
    "log.empty": "Non c'e ancora nulla da salvare.",
    "log.filter_log": "File di registro",
    "log.filter_text": "File di testo",
    "log.filter_all": "Tutti i file",
    "log.started": "RetroCam Rescue {version} - {platform} - Python {python}",
    # -- dialogs ----------------------------------------------------------- #
    "error.title": "Errore",
    "error.unexpected": "Errore imprevisto ({kind}): {err}",
    "info.title": "RetroCam Rescue",
    "warn.title": "Attenzione",
    "quit.title": "Uscire da RetroCam Rescue?",
    "quit.body": "Un'operazione e ancora in corso.\n\nUscire adesso la "
    "interrompe. I file gia copiati restano nella cartella di destinazione e "
    "niente e stato cancellato dalla fotocamera.\n\nUscire lo stesso?",
    "busy.title": "Una cosa alla volta",
    "busy.body": "Un'altra operazione e ancora in corso. Aspetta che finisca "
    "oppure premi Annulla.",
    # -- entry point / headless mode --------------------------------------- #
    "main.description": "Recupera le foto da una vecchia fotocamera digitale.",
    "main.help_lang": "Lingua dell'interfaccia: it, en oppure auto "
    "(predefinito: auto).",
    "main.help_version": "Mostra la versione ed esce.",
    "main.help_cli": "Non apre la finestra: cerca la fotocamera, elenca i suoi "
    "file e stampa il risultato.",
    "main.tk_missing_title": "Tkinter non e disponibile",
    "main.tk_missing": "RetroCam Rescue ha bisogno di Tkinter, la libreria "
    "grafica normalmente inclusa in Python, e questo Python non ce l'ha "
    "({err}).\n\n{hint}\n\nNel frattempo puoi usare la modalita testuale:\n"
    "    {argv0} --cli",
    "main.tk_hint_macos": "Installa Python da python.org (il suo installer "
    "include Tk), oppure con Homebrew esegui: brew install python-tk",
    "main.tk_hint_linux": "Installa il pacchetto Tk della tua distribuzione, per "
    "esempio:\n"
    "    sudo apt install python3-tk        (Debian, Ubuntu, Mint)\n"
    "    sudo dnf install python3-tkinter   (Fedora, RHEL)\n"
    "    sudo pacman -S tk                  (Arch)",
    "main.tk_hint_windows": "Riavvia l'installer di Python, scegli Modify e "
    "spunta 'tcl/tk and IDLE'.",
    "main.tk_hint_generic": "Installa il pacchetto di supporto Tk per questo Python.",
    "main.import_failed": "RetroCam Rescue non e riuscito ad avviarsi: {err}",
    "main.gui_failed": "Non e stato possibile aprire la finestra: {err}",
    "cli.header": "RetroCam Rescue {version} - modalita testuale (sola lettura)",
    "cli.env": "Ambiente",
    "cli.env_row": "  [{mark}] {label} {version} {hint}",
    "cli.backends": "Collegamenti",
    "cli.backend_row": "  [{mark}] {name} {hint}",
    "cli.detecting": "Ricerca della fotocamera in corso...",
    "cli.no_camera": "Nessuna fotocamera trovata.",
    "cli.camera": "[{n}] {model} - porta {port} - tramite {backend}",
    "cli.detail": "    {detail}",
    "cli.listing": "    lettura dell'elenco dei file...",
    "cli.files": "    {n} file, {size}",
    "cli.file": "    {folder}/{name}  {size}",
    "cli.more": "    ... e altri {n}",
    "cli.list_failed": "    non e stato possibile leggere l'elenco dei file: {err}",
    "cli.readonly": "La modalita testuale si limita a leggere. Non e stato "
    "scaricato ne cancellato nulla. Esegui senza --cli per aprire la finestra.",
    "cli.interrupted": "Interrotto.",
    # -- transfer engine ---------------------------------------------------- #
    "transfer.summary.dest": "Destinazione: {dest}",
    "transfer.summary.recovered": "Recuperati e verificati: {ok} di {total}",
    "transfer.summary.aborted": "Operazione interrotta dall'utente - i file "
    "restanti non sono stati trasferiti.",
    "transfer.summary.skipped": "Gia presenti e ricontrollati su disco: {n}",
    "transfer.summary.deep": "Decodificati completamente durante il controllo: "
    "{deep} di {ok} (gli altri hanno superato il controllo strutturale)",
    "transfer.summary.failed": "Non recuperati: {n}",
    "transfer.summary.more": "  ... e altri {n}",
    "transfer.summary.unknown_reason": "motivo sconosciuto",
    "transfer.summary.deletable": "Cancellabili in sicurezza dalla fotocamera: {n}",
    "transfer.msg.listed": "{n} file trovati sulla fotocamera ({size}).",
    "transfer.msg.cancelled": "Annullato - controllo di quali file erano stati "
    "completati prima dell'interruzione.",
    "transfer.msg.verified": "Controllati {n} file su disco.",
    "transfer.msg.recovered_after_cancel": "{n} file completati e verificati "
    "prima dell'interruzione.",
    "transfer.msg.space_ok": "{need} da copiare, {free} liberi in {dest}.",
    "transfer.msg.space_partial": "({n} file di dimensione non dichiarata non "
    "sono inclusi.)",
    "transfer.msg.space_unknown": "La fotocamera non ha dichiarato le dimensioni "
    "dei file; lo spazio libero non e stato verificato.",
    "transfer.msg.space_uncheckable": "Non e stato possibile misurare lo spazio "
    "libero su {dest}; si prosegue.",
    "transfer.msg.partial_delete": "{bad} file non sono stati recuperati e "
    "resteranno sulla fotocamera.",
    "transfer.msg.delete_refused": "{name} resta sulla fotocamera: {why}",
    "transfer.msg.delete_all_refused": "Non e stato cancellato nulla: le copie "
    "verificate non sono piu su disco.",
    "transfer.msg.deleting": "Cancellazione di {n} file verificati da {camera}.",
    "transfer.err.list_failed": "Non e stato possibile leggere l'elenco dei file "
    "da {camera}: {err}",
    "transfer.err.download_failed": "Il trasferimento si e interrotto in modo "
    "imprevisto: {err}. Niente e stato cancellato dalla fotocamera.",
    "transfer.err.dest_create": "Impossibile creare la cartella di destinazione "
    "{dest}: {err}. Scegli un'altra cartella.",
    "transfer.err.dest_not_dir": "La destinazione {dest} non e una cartella. "
    "Scegli un'altra cartella.",
    "transfer.err.dest_readonly": "Impossibile scrivere in {dest}: {err}. Scegli "
    "una cartella di tua proprieta, per esempio la cartella Download.",
    "transfer.err.dest_on_camera": "La destinazione {dest} si trova sulla scheda "
    "di memoria della fotocamera ({root}). Copiare le foto sulla stessa scheda da "
    "cui provengono non lascerebbe alcuna seconda copia, quindi non e stato "
    "trasferito niente. Scegli una cartella su questo computer, per esempio la "
    "cartella Download.",
    "transfer.err.no_space": "Spazio insufficiente in {dest}: servono circa "
    "{need} (incluso un margine del 10%) ma ne sono disponibili solo {free}. "
    "Libera spazio oppure scegli un'altra cartella.",
    "transfer.err.outcome_unknown": "Il driver della fotocamera ha riportato un "
    "file che non era stato richiesto ({path}). I file scaricati sono al sicuro "
    "nella cartella di destinazione e niente e stato cancellato dalla "
    "fotocamera.",
    "transfer.err.outcome_missing": "Il driver della fotocamera non ha riportato "
    "l'esito di {n} file, a partire da {path}. I file scaricati sono al sicuro "
    "nella cartella di destinazione e niente e stato cancellato dalla "
    "fotocamera.",
    "transfer.err.not_verified": "annullato prima che il file potesse essere "
    "verificato",
    "transfer.err.no_bytes": "non e stato scritto alcun file",
    "transfer.err.verify_crashed": "il controllo di integrita non e stato "
    "eseguito: {err}",
    "transfer.err.cancelled_before": "annullato prima che il file venisse trasferito",
    "transfer.err.delete_unsupported": "Questo collegamento non puo cancellare "
    "file da {camera}. La scheda potrebbe essere protetta da scrittura, oppure "
    "il driver e in sola lettura.",
    "transfer.err.delete_failed": "La cancellazione si e interrotta in modo "
    "imprevisto: {err}. Riesegui la ricerca per vedere quali file sono ancora "
    "presenti.",
    "transfer.err.delete_outside": "Il backend {backend} ha dichiarato di aver "
    "cancellato {n} file mai verificati: {paths}. Smetti di usare questa scheda e "
    "copia tutto quello che vi e rimasto.",
    "transfer.err.gate_no_dest": "non risulta alcuna copia locale",
    "transfer.err.gate_unreadable": "la copia locale non e piu leggibile ({err})",
    "transfer.err.gate_missing": "la copia locale non si trova piu in {dest}",
    "transfer.err.gate_empty": "la copia locale e vuota",
    "transfer.err.gate_changed": "la copia locale e cambiata dopo il controllo "
    "({now} byte invece di {want})",
    # -- gphoto2 backend ---------------------------------------------------- #
    "gphoto2.missing_macos": "gphoto2 non e installato - premi Installa per "
    "aggiungerlo con Homebrew.",
    "gphoto2.missing_linux": "gphoto2 non e installato - installalo con il "
    "gestore di pacchetti della tua distribuzione.",
    "gphoto2.missing_generic": "gphoto2 non e installato.",
    "gphoto2.unavailable_windows": "gphoto2 non ha una versione supportata per "
    "Windows. Usa un lettore di schede di memoria oppure il ponte WSL2.",
    "gphoto2.install_hint_windows": "Usa un lettore di schede di memoria, oppure "
    "configura WSL2 con usbipd-win.",
    "gphoto2.broken_binary": "gphoto2 e stato trovato in {path} ma non si avvia.",
    "gphoto2.released_ptp": "Ho chiesto al sistema di rilasciare la fotocamera "
    "perche gphoto2 possa usarla ({name}).",
    "gphoto2.detecting": "Chiedo a gphoto2 quali fotocamere sono collegate...",
    "gphoto2.detected_none": "gphoto2 non vede nessuna fotocamera. Accendila e "
    "collegala direttamente, non tramite un hub.",
    "gphoto2.detected_one": "gphoto2 ha trovato {model} su {port}.",
    "gphoto2.listing": "Lettura dell'elenco dei file tramite gphoto2...",
    "gphoto2.listed": "{count} file elencati.",
    "gphoto2.downloading": "Scarico {name}",
    "gphoto2.skipped_existing": "{name} e gia nella cartella di destinazione - "
    "verra verificato, non ricopiato.",
    "gphoto2.deleting": "Cancello {name} dalla fotocamera",
    "gphoto2.delete_confirming": "Rileggo la fotocamera per confermare cosa e "
    "stato cancellato...",
    "gphoto2.delete_unconfirmed": "la fotocamera non ha confermato la cancellazione",
    "gphoto2.still_present": "il file e ancora sulla fotocamera",
    "gphoto2.timeout": "La fotocamera non ha risposto entro {seconds} s. "
    "Spegnila, riaccendila e riprova.",
    "gphoto2.cancelled": "Interrotto su tua richiesta.",
    "gphoto2.spawn_failed": "Non e stato possibile avviare gphoto2: {detail}",
    "gphoto2.dest_unwritable": "Impossibile scrivere in {path}: {detail}",
    "gphoto2.no_output": "gphoto2 non ha scritto alcun file",
    "gphoto2.empty_file": "il file e arrivato vuoto",
    "gphoto2.size_mismatch": "trasferimento incompleto: {got} byte invece di "
    "{expected}",
    "gphoto2.replace_failed": "non e stato possibile mettere il file al suo "
    "posto: {detail}",
    "gphoto2.err_claim": "Un altro programma sta occupando la fotocamera. Su "
    "macOS chiudi Foto e Acquisizione Immagine; su Linux ferma "
    "gvfs-gphoto2-volume-monitor, poi riprova. {detail}",
    "gphoto2.err_unplugged": "La fotocamera e stata scollegata o si e spenta. {detail}",
    "gphoto2.err_no_camera": "Nessuna fotocamera collegata, oppure non e accesa. "
    "{detail}",
    "gphoto2.err_io": "Il collegamento USB con la fotocamera e caduto. Prova un "
    "altro cavo o un'altra porta, evitando gli hub. {detail}",
    "gphoto2.err_port_timeout": "La fotocamera ha smesso di rispondere. Spegnila, "
    "riaccendila e riprova. {detail}",
    "gphoto2.err_os": "Il sistema operativo ha rifiutato la richiesta. {detail}",
    "gphoto2.err_camera_op": "La fotocamera ha rifiutato questa operazione. {detail}",
    "gphoto2.err_file_not_found": "Quel file non e piu sulla fotocamera. {detail}",
    "gphoto2.err_dir_not_found": "Quella cartella non e piu sulla fotocamera. {detail}",
    "gphoto2.err_no_space": "Lo spazio libero non e sufficiente. {detail}",
    "gphoto2.err_busy": "La fotocamera e occupata. Aspetta un attimo e riprova. "
    "{detail}",
    "gphoto2.err_corrupt": "I dati che arrivano dalla fotocamera sono "
    "danneggiati. {detail}",
    "gphoto2.err_bad_params": "gphoto2 ha rifiutato la richiesta. {detail}",
    "gphoto2.err_unknown_port": "Quella porta USB non esiste piu: la fotocamera e "
    "stata probabilmente scollegata. {detail}",
    "gphoto2.err_unsupported": "Questa fotocamera non supporta quell'operazione. "
    "{detail}",
    "gphoto2.err_generic": "gphoto2 ha segnalato un errore. {detail}",
}


#: All tables, keyed by the two-letter code :func:`set_language` accepts.
_TABLES: Dict[str, Dict[str, str]] = {"en": _EN, "it": _IT}


# --------------------------------------------------------------------------- #
# Locale detection
# --------------------------------------------------------------------------- #


def _from_environment() -> str:
    """Locale string from the environment, or ``""``.

    ``LANGUAGE`` may hold a colon-separated priority list (``it:en``), so only
    its first entry is meaningful. The order below is the POSIX precedence,
    with our own override first so a bug report can be reproduced in either
    language without touching the system settings.
    """
    for name in (_ENV_OVERRIDE, "LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        try:
            value = os.environ.get(name, "")
        except Exception:  # pragma: no cover - os.environ is always there
            continue
        first = value.split(":")[0].strip()
        if first and first not in ("C", "POSIX"):
            return first
    return ""


def _from_locale_module() -> str:
    """Locale string from :mod:`locale`, tolerating every deprecation.

    ``getdefaultlocale()`` is deprecated and scheduled for removal, and
    ``getlocale()`` legitimately returns ``(None, None)`` before anyone calls
    ``setlocale()`` — which we deliberately do not do, because changing the
    process-wide locale would alter number and date formatting for the whole
    application as a side effect of guessing a language.
    """
    try:
        code = locale.getlocale()[0]
        if code:
            return str(code)
    except Exception:
        pass

    # Looked up dynamically so that its removal in a future Python is a missing
    # attribute rather than an ImportError at module load.
    legacy = getattr(locale, "getdefaultlocale", None)
    if legacy is not None:
        try:
            code = legacy()[0]
            if code:
                return str(code)
        except Exception:
            pass
    return ""


def _from_macos_preferences() -> str:
    """Locale from macOS user preferences, or ``""``.

    This exists for one very concrete failure: an app double-clicked in Finder
    inherits none of the shell's ``LANG``, so an Italian Mac would show an
    English interface for no visible reason. The global preferences plist is
    the same place the system itself reads, and :mod:`plistlib` is stdlib.
    """
    if sys.platform != "darwin":
        return ""
    try:
        import plistlib

        path = os.path.expanduser("~/Library/Preferences/.GlobalPreferences.plist")
        with open(path, "rb") as handle:
            prefs = plistlib.load(handle)
    except Exception:
        return ""

    try:
        value = prefs.get("AppleLocale") or ""
        if isinstance(value, str) and value:
            return value
        languages = prefs.get("AppleLanguages") or []
        if languages and isinstance(languages[0], str):
            return languages[0]
    except Exception:
        return ""
    return ""


def detect_language() -> str:
    """Best guess at the user's language: ``'it'`` or ``'en'``.

    Italian when the detected locale starts with ``it`` (``it``, ``it_IT``,
    ``it-CH.UTF-8``), English for everything else — including the case where
    nothing at all can be detected. English is the safer default for an
    open-source tool: an Italian speaker reading English strings is
    inconvenienced, while the reverse plus a missing translation is confusing.
    """
    for source in (_from_environment, _from_locale_module, _from_macos_preferences):
        try:
            value = source()
        except Exception:  # pragma: no cover - each source is already guarded
            continue
        if not value:
            continue
        normalized = value.strip().lower().replace("-", "_")
        if normalized.startswith("it"):
            return "it"
        return DEFAULT_LANGUAGE
    return DEFAULT_LANGUAGE


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

# The GUI reads this from the Tk main loop while a worker thread may be
# formatting a progress line with it. Rebinding a module global is atomic under
# CPython, but the lock keeps set_language() from being half-applied on any
# implementation where it is not.
_lock = threading.RLock()
_current = DEFAULT_LANGUAGE
_table = _EN


def available_languages() -> List[str]:
    """The language codes :func:`set_language` accepts, besides ``'auto'``."""
    return sorted(_TABLES)


def current_language() -> str:
    """The language currently in use: ``'it'`` or ``'en'``."""
    return _current


def set_language(lang: str) -> None:
    """Switch language. ``'auto'`` (or an unknown code) re-runs detection.

    Widgets already on screen keep the text they were built with — this is
    called once at startup from the ``--lang`` argument, before any window
    exists. It never raises: a bad ``--lang`` value falls back to detection
    rather than stopping a rescue over a typo.
    """
    global _current, _table

    requested = (lang or "auto").strip().lower().replace("-", "_")
    if requested in ("", "auto", "system", "default"):
        chosen = detect_language()
    else:
        # Accept 'it_IT', 'it-CH.UTF-8' and friends, not just the bare code.
        chosen = requested.split("_")[0].split(".")[0]

    if chosen not in _TABLES:
        chosen = detect_language()

    with _lock:
        _current = chosen
        _table = _TABLES.get(chosen, _EN)


def t(key: str, **kw: Any) -> str:
    """Translate ``key`` and interpolate ``kw``.

    Degradation ladder, each step chosen so the user still sees something they
    can act on:

    1. the entry for the current language;
    2. the English entry, when the current language lacks the key;
    3. the key itself — which is also the signal ``transfer.py`` uses to fall
       back to its own baked-in English text.

    Interpolation failures (a caller passing the wrong argument names, a
    template edited without updating its callers) return the template
    unformatted instead of raising. Never raises.
    """
    if not key:
        return ""

    text = _table.get(key)
    if text is None:
        text = _EN.get(key)
    if text is None:
        return key
    if not kw:
        return text

    try:
        return text.format(**kw)
    except Exception:
        # Missing or misnamed placeholder: showing '{name}' beats showing a
        # traceback, and the surrounding sentence is still readable.
        return text


def missing_translations() -> List[Tuple[str, str]]:
    """``(language, key)`` for every key present in one table but not another.

    Not called at runtime — a missing string must never stop the program — but
    a unit test can assert this is empty and catch drift the moment a new
    message is added to only one language.
    """
    gaps: List[Tuple[str, str]] = []
    every_key = set()
    for table in _TABLES.values():
        every_key.update(table)
    for lang in sorted(_TABLES):
        for key in sorted(every_key - set(_TABLES[lang])):
            gaps.append((lang, key))
    return gaps


# Detect once at import, so the first string the GUI asks for is already in the
# right language. --lang overrides it later via set_language().
set_language("auto")
