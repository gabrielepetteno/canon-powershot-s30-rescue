"""Tests for :mod:`retrocam.i18n` and the shared helpers in ``backends/base.py``.

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests

Two very different things live here because they share one property: they are
the parts of the project that *every* other part leans on, and both fail
silently rather than loudly.

**i18n** fails silently by design — :func:`~retrocam.i18n.t` never raises, so a
key that exists in English but not in Italian, or a template whose placeholders
drift apart between the two languages, produces a raw key or an unformatted
``{dest}`` on screen instead of a crash. Nothing at runtime will ever tell us.
The tests below are that alarm: the two tables must hold the same keys, with the
same placeholders, and every literal key any module actually asks for must exist
in both. The last check is done by parsing the source with :mod:`ast`, because
grep-level drift is exactly what gets missed in review.

**base.safe_dest_path** fails silently in a worse way: it is the single function
that decides where a rescued photograph lands. Get the collision rules wrong and
one file overwrites another — two Canon folders legitimately hold the same base
name once the frame counter rolls over — after which the transfer engine
verifies the survivor, marks both as recovered, and clears both for deletion
from the card. All three backends call this one static method, so the tests
here protect all three at once.

Stdlib only. Nothing here touches a camera, the network, or the user's files.
"""

from __future__ import annotations

import ast
import inspect
import os
import string
import sys
import unittest
from typing import Dict, List, Set, Tuple

# The package lives in src/ and is not installed while the suite runs from a
# checkout. Derived from this file's location rather than the cwd, so discovery
# works from anywhere.
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_TESTS_DIR)
_SRC_DIR = os.path.join(_PROJECT_DIR, "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from helpers import TempDirCase  # noqa: E402

from retrocam import i18n  # noqa: E402
from retrocam.backends import (  # noqa: E402
    GPhoto2Backend,
    MassStorageBackend,
    WiaBackend,
)
from retrocam.backends.base import CameraBackend, noop_progress  # noqa: E402
from retrocam.model import (  # noqa: E402
    BackendKind,
    CameraFile,
    Progress,
)

#: The directory the AST scan walks.
_PACKAGE_DIR = os.path.join(_SRC_DIR, "retrocam")

#: Every concrete transport, so the ABC checks cover all of them at once.
_BACKENDS = (MassStorageBackend, GPhoto2Backend, WiaBackend)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _placeholders(template: str) -> Set[str]:
    """Names of the ``{...}`` fields in a :meth:`str.format` template.

    ``{a.b}`` and ``{a[0]}`` are reduced to ``a``, because that is the keyword
    the caller must supply. ``{{`` escapes yield no field, as they should.
    Raises :class:`ValueError` on a malformed template, which is a defect worth
    surfacing rather than swallowing.
    """
    names: Set[str] = set()
    for _literal, field, _spec, _conversion in string.Formatter().parse(template):
        if field is None:
            continue
        root = field.split(".")[0].split("[")[0].strip()
        names.add(root)  # "" for an auto-numbered {}, asserted against below
    return names


#: Functions whose first positional argument is a translation key. ``t`` is the
#: direct call, ``_translate`` is the alias ``transfer.py`` imports it under, and
#: ``_msg`` is that module's wrapper — ``_msg(key, english_fallback, **kw)``.
_TRANSLATION_CALLERS = ("t", "_translate", "_msg")


def _iter_source_files() -> List[str]:
    """Every ``.py`` file in the package, sorted, excluding caches."""
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(_PACKAGE_DIR):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in sorted(filenames):
            if name.endswith(".py"):
                found.append(os.path.join(dirpath, name))
    return sorted(found)


def _scan_call_sites() -> Tuple[Dict[str, List[str]], List[str]]:
    """Find every literal translation key in the package source.

    Returns ``({key: ["app.py:412", ...]}, [dynamic sites])``. A key reaching
    ``t()`` through a variable (``t("deps.label." + dep.key)``) cannot be checked
    statically, so those sites are only counted, and reported in the failure
    message that says how much of the source the scan actually saw. The one
    place where a dynamic key has no fallback of its own — the gphoto2 error
    map — is checked directly instead, below.
    """
    keys: Dict[str, List[str]] = {}
    dynamic: List[str] = []
    for path in _iter_source_files():
        rel = os.path.relpath(path, _PACKAGE_DIR)
        with open(path, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                called = func.id
            elif isinstance(func, ast.Attribute):
                called = func.attr
            else:
                continue
            if called not in _TRANSLATION_CALLERS:
                continue
            where = "%s:%d" % (rel, node.lineno)
            first = node.args[0] if node.args else None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                keys.setdefault(first.value, []).append(where)
            else:
                dynamic.append(where)
    return keys, dynamic


class _LanguageStateCase(unittest.TestCase):
    """Restores the process-wide language after each test.

    :mod:`retrocam.i18n` keeps the selected language in module globals and the
    whole suite runs in one process, so a test that switches to Italian and
    walks away would change what every later test sees.
    """

    def setUp(self) -> None:
        super().setUp()
        self._saved_language = i18n.current_language()
        self._saved_env = os.environ.get(i18n._ENV_OVERRIDE)
        self.addCleanup(self._restore_language)

    def _restore_language(self) -> None:
        if self._saved_env is None:
            os.environ.pop(i18n._ENV_OVERRIDE, None)
        else:
            os.environ[i18n._ENV_OVERRIDE] = self._saved_env
        i18n.set_language(self._saved_language)

    def pin_detection(self, locale_code: str = "en_US.UTF-8") -> None:
        """Make ``detect_language()`` deterministic for this test.

        ``RETROCAM_LANG`` is the first source the detector consults, so setting
        it decides the answer regardless of the machine's real locale — which
        matters because "falls back to detection" is otherwise untestable
        without knowing what the developer's laptop is set to.
        """
        os.environ[i18n._ENV_OVERRIDE] = locale_code


# --------------------------------------------------------------------------- #
# The tables themselves
# --------------------------------------------------------------------------- #


class LanguageTableTests(unittest.TestCase):
    """The Italian and English tables must stay structurally identical."""

    def test_both_tables_hold_exactly_the_same_keys(self) -> None:
        # Asserted in both directions explicitly, and not only through
        # missing_translations(), so the failure message names the offending
        # keys and the language that is short of them. Adding an English-only
        # string is the easy mistake; adding an Italian-only one happens too.
        english = set(i18n._EN)
        italian = set(i18n._IT)
        self.assertEqual(
            sorted(english - italian),
            [],
            "keys present in English but missing from Italian",
        )
        self.assertEqual(
            sorted(italian - english),
            [],
            "keys present in Italian but missing from English",
        )
        self.assertEqual(i18n.missing_translations(), [])

    def test_missing_translations_detects_an_injected_gap(self) -> None:
        # Without this, the assertion above is worthless: a missing_translations()
        # that always returned [] would pass it forever. Remove one Italian
        # string and the report must name it, and only it.
        victim = "camera.section"
        original = i18n._IT[victim]
        self.addCleanup(i18n._IT.__setitem__, victim, original)

        before = set(i18n.missing_translations())
        del i18n._IT[victim]
        after = set(i18n.missing_translations())
        # The delta, not the absolute answer: reporting real drift is the other
        # test's job, and this one must keep making its own point when it fails.
        self.assertEqual(after - before, {("it", victim)})

    def test_placeholders_match_between_the_two_languages(self) -> None:
        # A template translated as "Copia {n} file" while the English says
        # "Copying {n} file(s) into {dest}" raises KeyError inside str.format at
        # the worst possible moment. t() catches it and shows the raw template,
        # so the user gets "{dest}" on screen and nobody hears about it.
        for key, english in sorted(i18n._EN.items()):
            italian = i18n._IT.get(key)
            if italian is None:
                continue  # already reported by the key-set test
            with self.subTest(key=key):
                self.assertEqual(
                    sorted(_placeholders(english)),
                    sorted(_placeholders(italian)),
                    "placeholders differ between languages",
                )

    def test_every_template_formats_with_its_own_placeholders(self) -> None:
        # Catches an unbalanced brace or a stray format spec: t() would return
        # the template unformatted forever, and the tests above would not
        # notice because both languages can be wrong in the same way.
        for language, table in sorted(i18n._TABLES.items()):
            for key, template in sorted(table.items()):
                with self.subTest(language=language, key=key):
                    fields = _placeholders(template)
                    self.assertNotIn(
                        "",
                        fields,
                        "positional/auto-numbered field: t() only ever passes "
                        "keyword arguments, so {} can never be filled",
                    )
                    template.format(**{name: "X" for name in fields})

    def test_no_entry_is_blank(self) -> None:
        # A blank value renders as an empty button or an empty dialog, which is
        # indistinguishable from a broken window and impossible to report.
        for language, table in sorted(i18n._TABLES.items()):
            for key, value in sorted(table.items()):
                with self.subTest(language=language, key=key):
                    self.assertIsInstance(value, str)
                    self.assertTrue(value.strip(), "empty translation")

    def test_the_fallback_language_has_a_table(self) -> None:
        # t() drops to _EN for any key the current language lacks, so
        # DEFAULT_LANGUAGE naming a table that does not exist would turn the
        # whole degradation ladder into a raw-key generator.
        self.assertIn(i18n.DEFAULT_LANGUAGE, i18n.available_languages())
        self.assertEqual(i18n.available_languages(), ["en", "it"])


# --------------------------------------------------------------------------- #
# t()
# --------------------------------------------------------------------------- #


class TranslationLookupTests(_LanguageStateCase):
    """:func:`retrocam.i18n.t` must degrade, never raise."""

    def setUp(self) -> None:
        super().setUp()
        self.pin_detection("en_US.UTF-8")
        i18n.set_language("en")

    def test_unknown_key_returns_the_key_itself(self) -> None:
        # transfer.py depends on this exact behaviour: it treats "the answer is
        # the key" as the signal to use its own baked-in English fallback.
        self.assertEqual(i18n.t("no.such.key"), "no.such.key")
        self.assertEqual(i18n.t("no.such.key", n=1), "no.such.key")

    def test_empty_key_returns_empty_string(self) -> None:
        self.assertEqual(i18n.t(""), "")

    def test_missing_arguments_return_the_template_unformatted(self) -> None:
        # Ugly on screen, but the sentence around the placeholder still tells
        # the user what happened. Raising here would abort a rescue.
        template = i18n._EN["run.starting"]
        self.assertEqual(i18n.t("run.starting"), template)
        self.assertEqual(i18n.t("run.starting", wrong_name=1), template)

    def test_extra_arguments_are_ignored(self) -> None:
        # Callers pass a superset of the placeholders when a template is
        # shortened; that must not turn into a crash either.
        self.assertEqual(
            i18n.t("run.starting", n=3, dest="/tmp/out", leftover="ignored"),
            "Copying 3 file(s) into /tmp/out",
        )

    def test_braces_inside_an_argument_are_not_re_scanned(self) -> None:
        # A WIA device id looks like "{6BDD1FC6-810F-11D0-...}\\0001". If the
        # interpolated value were scanned for placeholders, every Windows
        # camera would render its own name as a KeyError and fall back to the
        # unformatted template.
        rendered = i18n.t(
            "camera.info",
            model="Canon PowerShot S30",
            port="{6BDD1FC6-810F-11D0-BEC7-08002BE2092F}\\0001",
            backend="WIA",
        )
        self.assertIn("{6BDD1FC6-810F-11D0-BEC7-08002BE2092F}\\0001", rendered)

    def test_a_key_missing_from_italian_falls_back_to_english(self) -> None:
        # Step 2 of the documented degradation ladder, and the reason a drifted
        # table shows a mixed-language line rather than a raw key. Nothing
        # else in the suite exercises it, because the tables are complete.
        victim = "camera.section"
        original = i18n._IT[victim]
        self.addCleanup(i18n._IT.__setitem__, victim, original)
        del i18n._IT[victim]

        i18n.set_language("it")
        self.assertEqual(i18n.t(victim), i18n._EN[victim])
        self.assertNotEqual(i18n.t(victim), victim)

    def test_every_key_renders_without_arguments_in_every_language(self) -> None:
        # The contract is "never raises", for any key, in any language. Callers
        # in the error paths frequently pass nothing at all.
        for language in i18n.available_languages():
            i18n.set_language(language)
            for key in sorted(i18n._TABLES[language]):
                with self.subTest(language=language, key=key):
                    rendered = i18n.t(key)
                    self.assertIsInstance(rendered, str)
                    self.assertTrue(rendered)


# --------------------------------------------------------------------------- #
# Language selection
# --------------------------------------------------------------------------- #


class LanguageSelectionTests(_LanguageStateCase):
    """``--lang`` must never be able to stop a rescue."""

    def test_explicit_languages_round_trip(self) -> None:
        for language in ("it", "en"):
            with self.subTest(language=language):
                i18n.set_language(language)
                self.assertEqual(i18n.current_language(), language)

    def test_switching_language_actually_changes_the_text(self) -> None:
        # Guards the internal table swap: current_language() could report "it"
        # while t() kept answering in English if _table were not rebound.
        i18n.set_language("en")
        english = i18n.t("camera.section")
        i18n.set_language("it")
        italian = i18n.t("camera.section")
        self.assertEqual(english, "2. Camera")
        self.assertEqual(italian, "2. Fotocamera")

    def test_auto_follows_the_environment(self) -> None:
        for locale_code, expected in (
            ("it", "it"),
            ("it_IT.UTF-8", "it"),
            ("fr_FR.UTF-8", "en"),  # unsupported locale -> English, not a crash
        ):
            with self.subTest(locale=locale_code):
                self.pin_detection(locale_code)
                i18n.set_language("auto")
                self.assertEqual(i18n.current_language(), expected)

    def test_regional_and_encoded_codes_are_accepted(self) -> None:
        # A user copying LANG out of their shell passes "it_IT.UTF-8", and a
        # Windows shortcut may pass "it-CH". Both must select Italian rather
        # than silently falling back to detection.
        self.pin_detection("en_US.UTF-8")
        for requested in ("it_IT", "it-CH.UTF-8", "IT", "  it  "):
            with self.subTest(requested=requested):
                i18n.set_language("en")
                i18n.set_language(requested)
                self.assertEqual(i18n.current_language(), "it")

    def test_unknown_language_falls_back_instead_of_raising(self) -> None:
        # A typo in --lang must not stop the program: detection takes over.
        self.pin_detection("it_IT.UTF-8")
        for requested in ("klingon", "zz", "", "   ", "auto", "system", "default"):
            with self.subTest(requested=requested):
                i18n.set_language("en")
                i18n.set_language(requested)
                self.assertEqual(i18n.current_language(), "it")
                self.assertIn(i18n.current_language(), i18n.available_languages())

    def test_none_is_treated_as_auto(self) -> None:
        # argparse hands over None when --lang is absent from a programmatic
        # call; set_language must survive it.
        self.pin_detection("it_IT.UTF-8")
        i18n.set_language("en")
        i18n.set_language(None)  # type: ignore[arg-type]
        self.assertEqual(i18n.current_language(), "it")

    def test_detect_language_returns_a_supported_code(self) -> None:
        # Whatever this machine's locale is, the answer must be usable.
        self.assertIn(i18n.detect_language(), i18n.available_languages())


# --------------------------------------------------------------------------- #
# Call sites
# --------------------------------------------------------------------------- #


class CallSiteKeyTests(unittest.TestCase):
    """Every key the code asks for must exist, in both languages.

    This is the check that catches the drift nobody sees: a key renamed in the
    table but not at the call site renders as ``after.hint_ready`` in the
    window, and no exception is ever raised.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.keys, cls.dynamic = _scan_call_sites()

    def test_the_scan_actually_found_the_call_sites(self) -> None:
        # A scanner that silently matches nothing would make the test below
        # pass forever. These floors are far under the real counts (185 literal
        # keys at the time of writing) and only fail if the scan breaks or a
        # module stops being translated.
        self.assertGreaterEqual(
            len(self.keys),
            100,
            "the AST scan saw %d literal and %d dynamic call sites in %s"
            % (len(self.keys), len(self.dynamic), _PACKAGE_DIR),
        )
        for module in (
            "app.py",
            "transfer.py",
            "__main__.py",
            os.path.join("backends", "gphoto2_backend.py"),
        ):
            with self.subTest(module=module):
                found = [
                    key
                    for key, sites in self.keys.items()
                    if any(site.startswith(module + ":") for site in sites)
                ]
                self.assertGreaterEqual(
                    len(found), 5, "no translation call sites found in %s" % module
                )

    def test_every_literal_key_exists_in_both_tables(self) -> None:
        for key in sorted(self.keys):
            with self.subTest(key=key, sites=self.keys[key]):
                self.assertIn(key, i18n._EN, "missing from the English table")
                self.assertIn(key, i18n._IT, "missing from the Italian table")

    def test_gphoto2_error_map_keys_exist_in_both_tables(self) -> None:
        # These keys reach t() through a variable, so the AST scan cannot see
        # them — and the call site has no fallback text of its own. A renamed
        # key here shows the user "gphoto2.err_io" at the exact moment the USB
        # link has just failed.
        from retrocam.backends import gphoto2_backend

        error_map = gphoto2_backend._ERROR_MAP
        self.assertGreaterEqual(len(error_map), 10)
        for code, (key, _exception) in sorted(error_map.items()):
            with self.subTest(code=code, key=key):
                self.assertIn(key, i18n._EN)
                self.assertIn(key, i18n._IT)


# --------------------------------------------------------------------------- #
# safe_dest_path
# --------------------------------------------------------------------------- #


class SafeDestPathTests(TempDirCase):
    """Where a rescued photograph lands. Shared by all three backends."""

    def setUp(self) -> None:
        super().setUp()
        self.dest = self.path("out")
        os.makedirs(self.dest, exist_ok=True)

    def resolve(self, name: str, folder: str = "/DCIM/118CANON") -> str:
        return CameraBackend.safe_dest_path(
            self.dest, CameraFile(folder=folder, name=name, size=1)
        )

    def touch(self, name: str) -> str:
        path = os.path.join(self.dest, name)
        with open(path, "wb") as handle:
            handle.write(b"x")
        return path

    def test_plain_name_lands_directly_in_dest(self) -> None:
        self.assertEqual(
            self.resolve("IMG_0001.JPG"), os.path.join(self.dest, "IMG_0001.JPG")
        )

    def test_collision_is_disambiguated_by_the_device_folder(self) -> None:
        # The case that broke three backends: 119CANON/IMG_0001.JPG is a
        # different photograph from 118CANON/IMG_0001.JPG, and overwriting one
        # with the other loses a picture that the engine then reports as saved.
        self.touch("IMG_0001.JPG")
        self.assertEqual(
            self.resolve("IMG_0001.JPG", folder="/DCIM/119CANON"),
            os.path.join(self.dest, "119CANON_IMG_0001.JPG"),
        )

    def test_second_collision_falls_back_to_a_number(self) -> None:
        # Same base name in a third folder, or a re-run after the tagged name
        # was already taken. The suffix goes before the extension so the file
        # still opens by double-click.
        self.touch("IMG_0001.JPG")
        self.touch("118CANON_IMG_0001.JPG")
        self.assertEqual(
            self.resolve("IMG_0001.JPG"),
            os.path.join(self.dest, "IMG_0001_2.JPG"),
        )

    def test_a_device_supplied_name_can_never_escape_dest(self) -> None:
        # A malformed listing (or a hostile one) must not be able to write
        # outside the destination folder. Every one of these must reduce to a
        # single leaf name inside dest.
        hostile = [
            "../../etc/passwd",
            "../IMG_0001.JPG",
            "/etc/passwd",
            "/tmp/IMG_0001.JPG",
            "..\\..\\Windows\\System32\\evil.dll",
            "C:\\Windows\\System32\\evil.dll",
            "\\\\server\\share\\evil.dll",
            "DCIM/../../../IMG_0001.JPG",
            "sub/dir/IMG_0001.JPG",
        ]
        for name in hostile:
            with self.subTest(name=name):
                result = self.resolve(name)
                self.assertEqual(
                    os.path.dirname(os.path.abspath(result)),
                    os.path.abspath(self.dest),
                    "escaped the destination folder",
                )
                leaf = os.path.basename(result)
                self.assertNotIn("/", leaf)
                self.assertNotIn("\\", leaf)
                self.assertNotIn(os.sep, leaf)
                self.assertNotIn(leaf, (".", "..", ""))

    def test_empty_and_dot_names_become_a_placeholder(self) -> None:
        # "." and ".." would resolve to the destination directory itself; an
        # empty name would resolve to it too. Both must become an ordinary file.
        for name in ("", "   ", ".", "..", "a/b/c/"):
            with self.subTest(name=name):
                result = self.resolve(name)
                self.assertEqual(result, os.path.join(self.dest, "unnamed.bin"), name)

    def test_placeholder_names_still_disambiguate(self) -> None:
        # Two unnamed files from different folders are still two photographs.
        first = self.resolve("", folder="/DCIM/118CANON")
        self.touch(os.path.basename(first))
        second = self.resolve("", folder="/DCIM/119CANON")
        self.assertNotEqual(first, second)
        self.assertEqual(second, os.path.join(self.dest, "119CANON_unnamed.bin"))

    def test_root_folder_uses_the_dcim_tag(self) -> None:
        # Some cameras report files at "/" with no folder at all; the tag must
        # not come out empty, which would produce a leading underscore name.
        self.touch("IMG_0001.JPG")
        self.assertEqual(
            self.resolve("IMG_0001.JPG", folder="/"),
            os.path.join(self.dest, "DCIM_IMG_0001.JPG"),
        )

    def test_the_result_is_always_a_free_name_inside_dest(self) -> None:
        # Walked repeatedly: every answer must be unused at the moment it is
        # given, or a backend would open it with "wb" and destroy a photo.
        for index in range(6):
            with self.subTest(index=index):
                result = self.resolve("IMG_0001.JPG")
                self.assertFalse(os.path.exists(result))
                self.assertEqual(
                    os.path.dirname(os.path.abspath(result)),
                    os.path.abspath(self.dest),
                )
                self.touch(os.path.basename(result))

    def test_a_hostile_folder_name_cannot_add_a_path_separator(self) -> None:
        # The folder tag is device-supplied too. On POSIX a backslash is an
        # ordinary filename character so nothing escapes, but the invariant
        # asserted here is the portable one: whatever the tag contains, the
        # answer stays a single component inside dest. On Windows a tag holding
        # a backslash WOULD escape, which is why this is asserted rather than
        # assumed. See the note in the parent report.
        # The flat name has to be taken, or the folder tag is never consulted.
        self.touch("IMG_0001.JPG")
        for folder in ("/DCIM/..", "/DCIM/../..", "/DCIM/..\\..\\evil", "", "///"):
            with self.subTest(folder=folder):
                result = self.resolve("IMG_0001.JPG", folder=folder)
                self.assertEqual(
                    os.path.dirname(os.path.abspath(result)),
                    os.path.abspath(self.dest),
                    "the folder tag escaped the destination folder",
                )

    def test_it_gives_up_rather_than_reusing_a_taken_name(self) -> None:
        # Exhausting every variant is absurd in practice, but the alternative
        # behaviour — returning a name that already exists — silently overwrites
        # a rescued photograph. Raising is the safe end of that road.
        self.touch("IMG_0001.JPG")
        self.touch("118CANON_IMG_0001.JPG")
        for n in range(2, 1000):
            self.touch("IMG_0001_%d.JPG" % n)
        with self.assertRaises(RuntimeError):
            self.resolve("IMG_0001.JPG")


# --------------------------------------------------------------------------- #
# The backend contract
# --------------------------------------------------------------------------- #


class BackendContractTests(unittest.TestCase):
    """Every transport must satisfy the ABC, in full, without being asked."""

    def test_the_base_class_is_abstract(self) -> None:
        self.assertTrue(inspect.isabstract(CameraBackend))
        with self.assertRaises(TypeError):
            CameraBackend()  # type: ignore[abstract]

    def test_the_abstract_surface_is_the_documented_five(self) -> None:
        # base.py and CONTRIBUTING.md both promise "the five abstract methods".
        # A sixth would break every third-party backend without warning, so
        # this failing is a signal to update the docs and revisit all three
        # implementations — not to edit this list.
        self.assertEqual(
            sorted(CameraBackend.__abstractmethods__),
            ["delete", "detect", "download", "is_available", "list_files"],
        )

    def test_every_backend_is_concrete(self) -> None:
        for backend in _BACKENDS:
            with self.subTest(backend=backend.__name__):
                self.assertTrue(issubclass(backend, CameraBackend))
                self.assertFalse(inspect.isabstract(backend))
                self.assertEqual(set(backend.__abstractmethods__), set())
                # Constructible with no arguments: the registry builds them
                # that way, and a required constructor argument would only
                # surface when a camera is plugged in.
                self.assertIsInstance(backend(), CameraBackend)

    def test_is_available_is_a_classmethod_returning_a_two_tuple(self) -> None:
        # The environment panel calls this on the class, before any instance
        # exists. If it were a plain method the whole panel would raise.
        for backend in _BACKENDS:
            with self.subTest(backend=backend.__name__):
                self.assertIsInstance(
                    inspect.getattr_static(backend, "is_available"), classmethod
                )
                self.assertTrue(inspect.ismethod(backend.is_available))

                result = backend.is_available()
                self.assertIsInstance(result, tuple)
                self.assertEqual(len(result), 2)
                available, hint = result
                self.assertIsInstance(available, bool)
                self.assertIsInstance(hint, str)
                if not available:
                    # The hint is shown verbatim in the environment panel, so
                    # an empty one leaves the user with a red mark and no idea
                    # what to install.
                    self.assertTrue(hint.strip())

    def test_is_available_never_raises_on_this_machine(self) -> None:
        # Called for every backend at startup on the GUI thread. It probes the
        # real environment here on purpose: a missing tool is a normal state,
        # not an exception. (WIA answers False off Windows without touching COM.)
        for backend in _BACKENDS:
            with self.subTest(backend=backend.__name__):
                try:
                    backend.is_available()
                except Exception as exc:  # pragma: no cover - the failure path
                    self.fail("%s.is_available() raised %r" % (backend.__name__, exc))

    def test_install_hint_is_a_string_for_every_backend(self) -> None:
        for backend in _BACKENDS:
            with self.subTest(backend=backend.__name__):
                self.assertIsInstance(backend.install_hint(), str)

    def test_every_backend_declares_its_identity(self) -> None:
        # ``kind`` is only an annotation on the ABC, so a backend that forgot it
        # would raise AttributeError from the registry and from __repr__.
        kinds = set()
        for backend in _BACKENDS:
            with self.subTest(backend=backend.__name__):
                self.assertIsInstance(backend.kind, BackendKind)
                self.assertTrue(backend.display_name.strip())
                kinds.add(backend.kind)
        self.assertEqual(len(kinds), len(_BACKENDS), "two backends share a kind")

    def test_an_incomplete_subclass_cannot_be_instantiated(self) -> None:
        # Proves the ABC is enforced rather than merely documented: a new
        # transport that forgets delete() must fail at construction, not
        # halfway through a rescue.
        class HalfBackend(CameraBackend):
            kind = BackendKind.MASS_STORAGE

            @classmethod
            def is_available(cls):
                return (True, "")

            def detect(self, progress=noop_progress):
                return []

            def list_files(self, camera, progress=noop_progress, cancel=None):
                return []

            def download(
                self,
                camera,
                files,
                dest_dir,
                progress=noop_progress,
                cancel=None,
                skip_existing=True,
            ):
                return []

        self.assertTrue(inspect.isabstract(HalfBackend))
        with self.assertRaises(TypeError):
            HalfBackend()  # type: ignore[abstract]

    def test_supports_delete_defaults_to_true(self) -> None:
        # The default is permissive, so a transport that cannot erase must say
        # so explicitly. Checked on a minimal subclass rather than on the real
        # backends, whose answers legitimately depend on the device.
        class MinimalBackend(CameraBackend):
            kind = BackendKind.MASS_STORAGE

            @classmethod
            def is_available(cls):
                return (True, "")

            def detect(self, progress=noop_progress):
                return []

            def list_files(self, camera, progress=noop_progress, cancel=None):
                return []

            def download(
                self,
                camera,
                files,
                dest_dir,
                progress=noop_progress,
                cancel=None,
                skip_existing=True,
            ):
                return []

            def delete(self, camera, files, progress=noop_progress, cancel=None):
                return []

        self.assertTrue(MinimalBackend().supports_delete())

    def test_noop_progress_accepts_a_tick_and_returns_nothing(self) -> None:
        # It is the default argument of four abstract methods; anything other
        # than "silently accept" would break every caller that omits a callback.
        self.assertIsNone(noop_progress(Progress(phase="detect")))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
