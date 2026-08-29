"""Tests for tasqr_statusline.

What the styles look like is pinned by golden output in `tests/golden.json`:
every style renders against every fixture and is compared verbatim with colors
stripped. One assertion table therefore covers rendering, Session parsing, bar
and meter fill, title truncation and segment composition. After a deliberate
change to a look, regenerate the file and read the diff:

    python3 tests/test_statusline.py --update-golden

The rest of the suite covers what a golden cannot see: box geometry, font
portability, config resolution, the cache/refresh cycle, credentials, and the
logic that depends on the clock.
"""

import json
import os
import sys
import time
import unicodedata
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tasqr_statusline as sl

os.environ["TZ"] = "UTC"  # the panel prints a clock; pin the zone as well as the instant
if hasattr(time, "tzset"):
    time.tzset()

GOLDEN_PATH = Path(__file__).with_name("golden.json")

# Longer than TITLE_MAX (44) but shorter than PANEL_TITLE_MAX (56), so the
# goldens show the one-line styles truncating a title the panel prints whole.
TITLE = "Build tasqr-statusline: a public Claude Code line"

# A fixed instant, rounded down to a whole marquee loop so a scrolling title
# starts at its first column and the goldens stay readable.
_LOOP = len(TITLE) + len(sl.MARQUEE_GAP)
NOW = float(_LOOP * (1_767_225_600 // _LOOP))

PLAIN = sl.Style(enabled=False)
COLOR = sl.Style(enabled=True, theme="dark")


def payload(**overrides):
    data = {
        "model": {"id": "claude-fable-5", "display_name": "Fable"},
        "workspace": {"current_dir": "/tmp/myproject", "project_dir": "/tmp/myproject"},
        "context_window": {"used_percentage": 22.0, "context_window_size": 200_000},
        "cost": {
            "total_cost_usd": 0.41,
            "total_duration_ms": 612_000,
            "total_lines_added": 128,
            "total_lines_removed": 34,
        },
        "rate_limits": {
            "five_hour": {"used_percentage": 26.0, "resets_at": NOW + 3600},
            "seven_day": {"used_percentage": 7.0, "resets_at": NOW + 6 * 86400},
        },
    }
    data.update(overrides)
    return data


# Every state the line has to render, as (stdin payload, cache) pairs.
FIXTURES = {
    "active": (payload(), {
        "fetched_at": NOW,
        "active": [{"title": TITLE, "priority": 2}],
        "active_count": 3,  # two more than the one shown
        "next": {"title": "Fix lease reclaim on worker restart", "priority": 2},
        "pending_count": "44",
        "blocked_count": "5",
        "quota": {"used": 870, "limit": 1000, "pct": 87},
    }),
    "next": (payload(), {
        "fetched_at": NOW - 3600,  # old enough to render the stale marker
        "active": [],
        "next": {"title": "Tidy docs", "priority": 4},  # low priority: no P marker
        "pending_count": "3",
        "blocked_count": "0",
    }),
    "empty": (payload(), {"fetched_at": NOW, "active": [], "next": None, "blocked_count": "0"}),
    "bare": ({}, {}),  # first run: nothing from Claude Code, nothing cached
    "unavailable": (payload(), {"failed_at": NOW, "error": "HTTPError"}),
    "no-key": (payload(), {"fetched_at": NOW, "error": "no_key"}),
}


def render(style, fixture, s=COLOR):
    data, cache = FIXTURES[fixture]
    return sl.strip_ansi(sl.render(data, cache, NOW, s, style=style))


def capture():
    return {
        f"{style}/{fixture}": render(style, fixture)
        for style in sl.STYLES
        for fixture in FIXTURES
    }


class Base(unittest.TestCase):
    """Isolate every test from the developer's own environment and git repo."""

    def setUp(self):
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for key in sl.SETTING_KEYS:
            os.environ.pop(f"TASQR_STATUSLINE_{key.upper()}", None)
        os.environ.pop("NO_COLOR", None)
        for fn, value in (("git_branch", "main"), ("git_dirty", True)):
            patcher = mock.patch.object(sl, fn, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)


# ---------------------------------------------------------------------------
# Rendering


class GoldenTests(Base):
    def test_every_style_renders_as_recorded(self):
        want = json.loads(GOLDEN_PATH.read_text())
        got = capture()
        self.assertEqual(
            set(got), set(want), "styles or fixtures changed; run --update-golden"
        )
        for key, expected in want.items():
            with self.subTest(key):
                self.assertEqual(got[key], expected)


class StyleInvariantTests(Base):
    """Properties the goldens record but cannot check: a box has to stay
    rectangular and every style has to render in an ordinary font."""

    def test_panel_box_is_rectangular(self):
        for fixture, (data, cache) in FIXTURES.items():
            with self.subTest(fixture):
                rows = sl.render(data, cache, NOW, COLOR, style="panel").split("\n")
                self.assertEqual(
                    len({sl.display_width(r) for r in rows}), 1, "ragged box"
                )
                plain = [sl.strip_ansi(r) for r in rows]
                self.assertTrue(plain[0].startswith("╭") and plain[-1].endswith("╯"))
                for row in plain[1:-1]:
                    self.assertIn(row[0], "│├")
                    self.assertIn(row[-1], "│┤")
                # A double-width glyph inside the box puts the width
                # calculation a column out and frays the border.
                for ch in "".join(plain):
                    self.assertNotIn(unicodedata.east_asian_width(ch), ("W", "F"), repr(ch))

    def test_no_style_needs_a_patched_font(self):
        # A private-use glyph renders as an empty box without a patched font.
        for key, out in capture().items():
            for ch in out:
                if 0xE000 <= ord(ch) <= 0xF8FF:
                    self.fail(f"private-use glyph {ch!r} in {key}")

    def test_no_color_collapses_the_chip_styles_but_keeps_the_layouts(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertEqual(sl.resolve_style("bubble"), "plain")
            for style in ("panel", "meters"):
                self.assertEqual(sl.resolve_style(style), style)
        self.assertEqual(sl.resolve_style("cyberpunk"), "plain")  # a style that no longer exists
        uncolored = sl.render(*FIXTURES["active"], NOW, PLAIN, style="panel")
        self.assertNotIn("\033[", uncolored)
        self.assertIn("╭", uncolored)


# ---------------------------------------------------------------------------
# Configuration


class SettingsTests(Base):
    def setUp(self):
        super().setUp()
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.confdir = Path(self.tmp.name) / "conf"
        self.confdir.mkdir()
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        patcher = mock.patch.object(sl, "config_dir", return_value=self.confdir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_settings_layer_env_over_project_over_global(self):
        self.assertEqual(
            sl.parse_settings_text("# c\nstyle = bubble\nttl = 120\nunknown = x\n"),
            {"style": "bubble", "ttl": "120"},
        )
        self.assertEqual(sl.parse_settings_text("faultline\n"), {"tags": "faultline"})
        self.assertEqual(
            sl.load_settings(str(self.project)),
            {"tags": [], "theme": None, "style": None, "segments": None, "ttl": sl.TTL},
        )

        (self.confdir / "config").write_text("style = bubble\ntheme = dark\nttl = 300\n")
        (self.project / ".tasqr-statusline").write_text("style = panel\ntags = faultline\n")
        s = sl.load_settings(str(self.project))
        self.assertEqual(s["style"], "panel")  # project beats global
        self.assertEqual(s["theme"], "dark")  # global fills the gap
        self.assertEqual((s["tags"], s["ttl"]), (["faultline"], 300))

        with mock.patch.dict(os.environ, {"TASQR_STATUSLINE_STYLE": "plain"}):
            self.assertEqual(sl.load_settings(str(self.project))["style"], "plain")

        (self.project / ".tasqr-statusline").write_text("ttl = soon\n")
        self.assertEqual(sl.load_settings(str(self.project))["ttl"], sl.TTL)

        (self.confdir / "projects.conf").write_text(
            f"[{self.project}]\ntags = micro\nsegments = dir,tasqr\n"
        )
        s = sl.load_settings(str(self.project))
        self.assertEqual((s["tags"], s["segments"]), (["micro"], ["dir", "tasqr"]))

    def test_projects_conf_sections_match_by_directory_identity(self):
        # A section covers its directory and everything under it, deepest
        # section winning; matching is by identity, not by path string.
        cases = [
            ("exact", "[/w/app]\ntags = app\n", "/w/app", {"tags": "app"}),
            ("nested repo", "[/w/box]\ntags = t\n", "/w/box/repo", {"tags": "t"}),
            ("deeper wins", "[/w/a]\ntags = out\n\n[/w/a/b]\ntags = in\n", "/w/a/b",
             {"tags": "in"}),
            ("deeper merges", "[/w/a]\ntags = out\nstyle = plain\n\n[/w/a/b]\nstyle = panel\n",
             "/w/a/b", {"tags": "out", "style": "panel"}),
            ("order is irrelevant", "[/w/a/b]\ntags = in\n\n[/w/a]\ntags = out\n", "/w/a/b",
             {"tags": "in"}),
            ("path boundaries", "[/w/micro]\ntags = wrong\n", "/w/microadventures", {}),
            ("siblings", "[/w/a/b]\ntags = b\n", "/w/a/c", {}),
            ("parents", "[/w/a/b]\ntags = b\n", "/w/a", {}),
            ("trailing slashes", "[/w/a/]\ntags = a\n", "/w/a/b/", {"tags": "a"}),
            ("~ expands", "[~/proj]\ntags = t\n", f"{Path.home()}/proj/nested", {"tags": "t"}),
        ]
        for name, conf, project, expected in cases:
            with self.subTest(name):
                (self.confdir / "projects.conf").write_text(conf)
                self.assertEqual(sl._projects_conf_settings(project), expected)

        # The same directory reached by another name: through a symlink, or in
        # another case on a case-insensitive filesystem. String comparison
        # gives such a project no settings at all, which shows up as the
        # unfiltered queue rather than as an error.
        real = Path(self.tmp.name) / "real"
        (real / "repo").mkdir(parents=True)
        link = Path(self.tmp.name) / "link"
        link.symlink_to(real)
        identity = [
            ("project is a symlink", real, link),
            ("section is a symlink", link, real),
            ("symlinked container", real, link / "repo"),
        ]
        lowered = Path(self.tmp.name) / "REAL"
        if lowered.exists():  # case-insensitive filesystem
            identity.append(("case differs", real, lowered))
        for name, section, project in identity:
            with self.subTest(name):
                (self.confdir / "projects.conf").write_text(f"[{section}]\ntags = t\n")
                self.assertEqual(sl._projects_conf_settings(str(project)), {"tags": "t"})

        other = Path(self.tmp.name) / "other"
        other.mkdir()
        (self.confdir / "projects.conf").write_text(f"[{real}]\ntags = t\n")
        self.assertEqual(sl._projects_conf_settings(str(other)), {})

    def test_tags_come_from_the_env_then_the_project_then_the_central_conf(self):
        self.assertEqual(sl.parse_tags("a, b  c"), ["a", "b", "c"])
        self.assertEqual(sl.parse_tags("tags = Backend, docs  # c"), ["backend", "docs"])
        self.assertEqual(sl.parse_tags("# only a comment"), [])

        with mock.patch.dict(os.environ, {"TASQR_STATUSLINE_TAGS": "faultline"}):
            self.assertEqual(sl.resolve_tags(str(self.project)), ["faultline"])
        with mock.patch.dict(os.environ, {"TASQR_STATUSLINE_TAGS": ""}):
            self.assertEqual(sl.resolve_tags(str(self.project)), [])  # empty disables

        self.assertEqual(sl.resolve_tags(str(self.project)), [])  # no config, no filter
        (self.project / ".tasqr-statusline").write_text("tags = faultline, chore\n")
        self.assertEqual(sl.resolve_tags(str(self.project)), ["chore", "faultline"])

        (self.confdir / "projects.conf").write_text(f"[{self.project}]\ntags = micro\n")
        (self.project / ".tasqr-statusline").unlink()
        self.assertEqual(sl.resolve_tags(str(self.project) + "/"), ["micro"])

        # Each filter caches separately, or one project would serve another's queue.
        self.assertEqual(sl.cache_path([]).name, "cache.json")
        self.assertNotEqual(sl.cache_path(["faultline"]), sl.cache_path([]))
        self.assertNotEqual(sl.cache_path(["faultline"]), sl.cache_path(["faultline", "chore"]))


# ---------------------------------------------------------------------------
# Talking to the API


class RefreshTests(Base):
    def setUp(self):
        super().setUp()
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(sl, "cache_dir", return_value=Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def cache(self, tags=()):
        return json.loads(sl.cache_path(list(tags)).read_text())

    def test_refresh_writes_a_snapshot(self):
        responses = {
            "/me": {"email": "me@example.com"},
            "/tasks?status=in_progress&limit=50&assignee=me%40example.com": {
                "tasks": [{"title": "Active", "priority": 2}], "count": 1,
            },
            "/tasks?status=pending&limit=50": {
                "tasks": [{"title": "Next", "priority": 1, "updated_at": "2026-01-01"}],
                "count": 12,
            },
            "/tasks?status=blocked&limit=50": {"tasks": [], "count": 0},
            "/quota": {"used": 900, "limit": 1000},
        }
        with (
            mock.patch.object(sl, "api_get", side_effect=lambda u, k, p: responses[p]),
            mock.patch.object(sl, "read_credentials", return_value=("key", "https://api")),
        ):
            sl.refresh([])
        cache = self.cache()
        self.assertEqual(cache["email"], "me@example.com")
        self.assertEqual(cache["active"], [{"title": "Active", "priority": 2}])
        self.assertEqual(cache["next"], {"title": "Next", "priority": 1})
        self.assertEqual((cache["pending_count"], cache["blocked_count"]), ("12", "0"))
        self.assertEqual(cache["quota"]["pct"], 90)

    def test_tags_filter_every_task_query_and_cache_separately(self):
        seen = []

        def fake_get(url, key, path):
            seen.append(path)
            if path == "/me":
                return {"email": "me@example.com"}
            if path == "/quota":
                return {"used": 0, "limit": 1000}
            return {"tasks": [], "count": 0}

        with (
            mock.patch.object(sl, "api_get", side_effect=fake_get),
            mock.patch.object(sl, "read_credentials", return_value=("key", "https://api")),
        ):
            sl.refresh(["chore", "faultline"])
        task_paths = [p for p in seen if p.startswith("/tasks")]
        self.assertEqual(len(task_paths), 3)
        for path in task_paths:
            self.assertIn("&tags=chore%2Cfaultline", path)
        self.assertTrue(sl.cache_path(["chore", "faultline"]).exists())
        self.assertFalse((Path(self.tmp.name) / "cache.json").exists())

    def test_a_failed_refresh_keeps_the_last_good_snapshot(self):
        old = {"fetched_at": NOW - 300, "active": [{"title": "Old", "priority": 3}]}
        (Path(self.tmp.name) / "cache.json").write_text(json.dumps(old))
        with (
            mock.patch.object(sl, "api_get", side_effect=OSError("down")),
            mock.patch.object(sl, "read_credentials", return_value=("key", "https://api")),
        ):
            sl.refresh([])
        cache = self.cache()
        self.assertEqual(cache["active"], old["active"])
        self.assertEqual(cache["error"], "OSError")
        self.assertGreater(cache["failed_at"], time.time() - 5)

    def test_a_blocked_spawn_does_not_break_the_render(self):
        # A sandbox may forbid subprocesses; rendering must survive it.
        with mock.patch.object(
            sl.subprocess, "Popen", side_effect=OSError(1, "Operation not permitted")
        ):
            sl.spawn_refresh([])
            sl.spawn_refresh(["chore"])

    def test_refresh_without_a_key_records_why(self):
        with mock.patch.object(sl, "read_credentials", return_value=(None, "https://api")):
            sl.refresh([])
        self.assertEqual(self.cache()["error"], "no_key")


class CredentialTests(Base):
    def test_credentials_come_from_the_env_then_the_ini_profile(self):
        with mock.patch.dict(
            os.environ, {"TASQR_API_KEY": "k-env", "TASQR_API_URL": "https://x/"}
        ):
            self.assertEqual(sl.read_credentials(), ("k-env", "https://x"))

        os.environ.pop("TASQR_API_KEY", None)
        os.environ.pop("TASQR_API_URL", None)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "credentials"
            path.write_text("[default]\napi_key = k-default\n\n[work]\napi_key = k-work\n")
            with (
                mock.patch.object(sl, "credentials_path", return_value=path),
                mock.patch.dict(os.environ, {"TASQR_PROFILE": "work"}),
            ):
                self.assertEqual(sl.read_credentials(), ("k-work", sl.DEFAULT_API_URL))
            with mock.patch.object(sl, "credentials_path", return_value=Path(tmp) / "nope"):
                self.assertEqual(sl.read_credentials(), (None, sl.DEFAULT_API_URL))


# ---------------------------------------------------------------------------
# Logic the goldens cannot see: they are one instant, in one palette


class LogicTests(Base):
    def test_a_long_title_scrolls_and_wraps_through_the_gap(self):
        title = "abcdefghij"
        self.assertEqual(sl.marquee("a\n  b\tc"), "a b c")  # whitespace collapses
        self.assertEqual(len(sl.marquee("x" * 100)), sl.TITLE_MAX)
        self.assertTrue(sl.marquee("x" * 100).endswith("…"))  # no clock: truncate
        self.assertEqual(sl.marquee("short", limit=44, now=12345), "short")
        self.assertEqual(sl.marquee(title, limit=5, now=0), "abcde")
        self.assertEqual(sl.marquee(title, limit=5, now=3), "defgh")
        self.assertEqual(sl.marquee(title, limit=5, now=3.9), "defgh")  # whole columns
        self.assertEqual(sl.marquee(title, limit=8, now=len(title)), sl.MARQUEE_GAP + "abc")
        loop = len(title) + len(sl.MARQUEE_GAP)
        self.assertEqual(sl.marquee(title, limit=5, now=loop), "abcde")  # full cycle

        # A wrapping title must not read as extra fields on the meters line.
        self.assertNotEqual(sl.METERS_SEP.strip(), sl.MARQUEE_GAP.strip())

    def test_gauges_escalate_with_pressure_and_plain_numbers_stay_quiet(self):
        # Gauges use the ok/warn/alert ramp; bare percentages use dim/warn/alert.
        self.assertEqual([sl._meter_role(p) for p in (22, 65, 90)], ["ok", "warn", "alert"])
        self.assertEqual(sl._usage_role(22), "dim")
        self.assertEqual(sl._usage_role(91), "alert")

    def test_meters_and_durations_stay_inside_their_bounds(self):
        window = sl.FIVE_HOURS
        self.assertEqual(sl.progress_bar(-5, width=4), sl.BAR_EMPTY * 4)
        self.assertEqual(sl.progress_bar(140, width=4), sl.BAR_FULL * 4)
        self.assertEqual(len(sl.meter(60, width=8, pace=0.25)), 8)  # cursor overwrites
        self.assertNotIn(sl.PACE_CURSOR, sl.meter(50, width=8))
        self.assertAlmostEqual(sl.pace_fraction(NOW + window / 2, window, NOW), 0.5)
        self.assertIsNone(sl.pace_fraction(None, window, NOW))
        self.assertEqual(sl.pace_fraction(NOW + 2 * window, window, NOW), 0.0)
        self.assertEqual(sl.pace_fraction(NOW - window, window, NOW), 1.0)
        self.assertEqual(
            [sl.human_duration(ms) for ms in (45_000, 612_000, 3 * 3_600_000 + 900_000)],
            ["45s", "10m", "3h15m"],
        )

    def test_the_queue_picks_by_priority_then_age_and_refreshes_with_backoff(self):
        tasks = [
            {"title": "low", "priority": 4, "updated_at": "2026-01-01"},
            {"title": "newer-p2", "priority": 2, "updated_at": "2026-02-01"},
            {"title": "older-p2", "priority": 2, "updated_at": "2026-01-01"},
        ]
        self.assertEqual(sl._next_up(tasks)["title"], "older-p2")
        self.assertIsNone(sl._next_up([]))
        self.assertEqual(sl._count({"tasks": [], "count": 50, "cursor": "abc"}), "50+")
        self.assertEqual(sl._count({"tasks": [], "count": 7}), "7")

        self.assertTrue(sl.needs_refresh({}, NOW))
        self.assertFalse(sl.needs_refresh({"fetched_at": NOW - 10}, NOW))
        self.assertTrue(sl.needs_refresh({"fetched_at": NOW - 3600}, NOW))
        # A recent failure suppresses respawning, old snapshot or not.
        self.assertFalse(sl.needs_refresh({"fetched_at": NOW - 3600, "failed_at": NOW - 5}, NOW))


def _update_golden():
    for key in sl.SETTING_KEYS:
        os.environ.pop(f"TASQR_STATUSLINE_{key.upper()}", None)
    os.environ.pop("NO_COLOR", None)
    patches = [
        mock.patch.object(sl, "git_branch", return_value="main"),
        mock.patch.object(sl, "git_dirty", return_value=True),
    ]
    for patcher in patches:
        patcher.start()
    try:
        GOLDEN_PATH.write_text(
            json.dumps(capture(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        )
    finally:
        for patcher in patches:
            patcher.stop()
    print(f"wrote {GOLDEN_PATH}")


if __name__ == "__main__":
    if "--update-golden" in sys.argv:
        _update_golden()
    else:
        unittest.main()
