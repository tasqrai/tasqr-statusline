# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
python3 -m unittest discover -s tests            # whole suite; no network, no fixtures
python3 -m unittest tests.test_statusline.GoldenTests -v   # one test class
python3 -m unittest tests.test_statusline.LogicTests.test_a_long_title_scrolls_and_wraps_through_the_gap  # one test
python3 tests/test_statusline.py --update-golden # regenerate tests/golden.json
ruff check .                                     # lint, config in ruff.toml

# render a line end to end, the way Claude Code invokes it
echo '{"model":{"display_name":"Opus 5"},"workspace":{"current_dir":"'"$PWD"'"},"context_window":{"used_percentage":22}}' \
  | python3 tasqr_statusline.py

python3 tasqr_statusline.py --refresh            # foreground refresh; --tags=foo for a scoped cache
cat ~/.cache/tasqr-statusline/cache*.json        # inspect the snapshot, incl. `error`/`failed_at`
```

CI runs the suite on Python 3.9–3.13 plus ruff. 3.9 is the supported floor, so no
match statements, no `X | Y` at runtime (the module relies on
`from __future__ import annotations`), no `dict | dict` merges.

## Constraints

One file (`tasqr_statusline.py`), standard library only. Install is a git clone plus a
`statusLine.command` pointing at that file — it must not become a package or grow
dependencies, and the script must stay directly executable.

## Architecture

Two entry paths through `main()`:

- **Render** (default): read the Claude Code session JSON on stdin, load a cached
  snapshot, print one or more lines. Never touches the network — Claude Code re-runs
  the command on every conversation update.
- **`--refresh`**: hit the Tasqr API and write the snapshot. Spawned detached by the
  render path (`spawn_refresh`) when the cache is older than the TTL, the `refresh`
  setting is `auto`, and `has_credentials()` is true. `spawn_refresh` takes a lock
  first (`acquire_lock`, `cache*.lock` beside the snapshot) and passes the token to
  the child as `--lock=`, which releases it in a `finally`. Without that lock every
  tick during an in-flight refresh spawns another process, because the snapshot —
  and so the staleness `needs_refresh` reads — is only written when the fetch ends.
  A lock older than `LOCK_STALE` is taken over; a refresh run by hand holds none.

The pipeline is: **settings → cache → session → items → style**.

`load_settings(project_dir)` merges four layers (global config, `projects.conf`
section, project `.tasqr-statusline` file, `TASQR_STATUSLINE_*` env), later wins.
Every key (`tags`, `theme`, `style`, `segments`, `ttl`, `refresh`) works in every
layer, so new settings go in `SETTING_KEYS` and get all four layers for free. `projects.conf`
sections match by directory identity (`os.stat` ino/dev via `_dir_id`), not path text,
and a section covers everything beneath it with the deepest section winning per key.

`tags` are load-bearing beyond filtering: they select the cache file
(`cache_path` hashes them), so sessions on different projects never overwrite each
other's snapshot.

`refresh()` writes the whole snapshot atomically. Slow-moving fields (`email`,
`quota`) ride along from the previous cache under their own TTLs. On failure it
rewrites the *old* snapshot with `error`/`failed_at`, so a failing network keeps
serving the last good data and `needs_refresh` backs off for a full TTL. Refresh
never prints — a stray traceback would garble the status line.

`Session.from_stdin` is the only reader of the Claude Code payload; everything
downstream works off that dataclass.

With no key configured, `tasqr_segments` says `tasqr: set TASQR_API_KEY` rather than
the `tasqr …` placeholder and nothing is spawned: an unconfigured install (the
statuslin.es sandbox is one) renders a working line from the session data alone,
and the gallery listing declares `api.tasqr.ai` because refreshes reach it.

### Rendering

`tasqr_segments()` and `build_items()` produce `(text, role, kind)` tuples:
`role` picks a color (chip background in `bubble`, `getattr(s, role)` elsewhere),
`kind` names the segment so a layout can label or strip it. `plain` ignores both —
its inline ANSI codes are already in `text`.

`render()` branches on style. `plain` and `bubble` join the items list;
`panel` and `meters` are *layouts* — they call `tasqr_segments`/`build_items`
themselves with an uncolored `Style`, choose their own rows, and ignore the
`segments` setting. Adding a segment means extending `build_items`; adding a layout
means a new function plus a branch in `render()` and an entry in `STYLES`.

Colors live in `PALETTES` (per-theme role→ANSI) and `CHIPS` (per-theme
role→bg/fg pair). Roles carry Tasqr dashboard meanings: amber/`warn` = in progress,
accent = pending, coral/`alert` = blocked. A new theme is one entry in each dict.

### Rules the layouts depend on

- **No double-width characters in `panel` or `meters`.** The box is sized from its own
  content (a status line is headless — no tty width to query), so one miscounted
  column frays the border. `_panel_text` and `_chip_text` strip the inline `▶`/`⛔`
  glyphs and spell the state out instead. A test asserts every panel row has the same
  `display_width` and that no glyph is East-Asian wide.
- **No private-use glyphs** anywhere — nothing may require a patched font.
- `NO_COLOR` collapses styles listed in `COLOR_ONLY_STYLES` to `plain`; layouts that
  carry meaning in structure keep their shape.
- Titles scroll via `marquee()`, which is a pure function of `now` — one column per
  second. Tests pin `NOW` to a whole loop boundary so goldens start at column 0.

## Tests

`tests/golden.json` pins appearance: every style × every fixture in `FIXTURES`
(`active`, `next`, `empty`, `bare`, `unavailable`, `no-key`), rendered with colors
stripped and compared verbatim. That table covers rendering, `Session` parsing, bar
and meter fill, truncation and segment composition — do not add per-style cosmetic
assertions; regenerate the golden after a deliberate look change and read the diff.

The rest of the suite covers what a golden cannot see: box geometry, font
portability, settings resolution, the cache/refresh cycle, credentials, and
clock-dependent logic. `Base.setUp` clears every `TASQR_STATUSLINE_*` var and mocks
`git_branch`/`git_dirty`, so tests never read the developer's environment or repo.
Adding a style or fixture changes the golden key set and fails the suite until
regenerated.
