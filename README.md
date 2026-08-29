# tasqr-statusline

A [Claude Code](https://code.claude.com) status line for people using
[Tasqr](https://tasqr.ai) for intelligent task tracking: the task you're on, what the queue would
hand you next, and what's blocked — in the terminal where the work is happening.

```
Opus 5 | tasqr-statusline main* | ctx 22% | ▶ Fix lease reclaim on worker restart | ⛔ 5 | tasks 87%
```

Alongside the session basics (model, directory, git branch, context usage), it shows live Tasqr
state:

| Segment | Meaning |
|---|---|
| `▶ <title>` | Your in-progress task (`+N` when you hold more than one) |
| `next: <title> P2 · 44 pending` | Nothing claimed: the task claim-order would pick next, and the queue depth. `P1`/`P2` marks high priority |
| `tasqr: queue empty` | No in-progress or pending work |
| `⛔ 5` | Blocked tasks in the org (shown only when non-zero) |
| `tasks 87%` | Monthly task quota (shown only at 80% or above; never shown on unmetered plans) |
| `(stale)` | The snapshot is over 10 minutes old (network trouble, or Tasqr unreachable) |
| `tasqr: unavailable` | Refreshes have run but none has succeeded yet |
| `main*` | Git branch, starred when the working tree is dirty |

A title longer than its window scrolls marquee-style, advancing one column per second as the
status line re-renders. The scroll is only as smooth as your re-render rate, so set
`refreshInterval` in the `statusLine` block to 1 or 2 seconds if you want it gliding.

Only `plain` uses the `⛔` emoji. `bubble` spells out `5 blocked` instead, because the emoji's
baked-in red is unreadable on the coral chip; `panel` and `meters` spell it out because an emoji
occupies two columns, and a width calculation one column out frays a border.

## What it's for

Tasqr holds work that outlives a session: a queue agents claim from, dependencies between tasks,
blocked work waiting on something else. That state lives on the dashboard, and the terminal is
where you are actually working. This puts the part you need mid-task — what you hold, what's
next, what's stuck — on the status line, so checking it costs a glance instead of a context
switch.

It assumes you are already running Tasqr. Without a key the session segments still render and the
task segments read `tasqr: no api key`; see *Credentials* below.

Single-file Python, standard library only. No pip install, no Node.

## Install

```bash
git clone https://github.com/tasqrai/tasqr-statusline ~/.local/share/tasqr-statusline
```

Then add to `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 ~/.local/share/tasqr-statusline/tasqr_statusline.py",
    "refreshInterval": 30
  }
}
```

Requires Python 3.9+ and a Tasqr API key (sign up at [tasqr.ai](https://tasqr.ai)).

## Credentials

Resolved in order:

1. `TASQR_API_KEY` (and optionally `TASQR_API_URL`) environment variables
2. The [tasqr-mcp](https://github.com/tasqrai/tasqr-mcp-python) credentials file:
   `~/.config/tasqr/credentials` (`%APPDATA%\tasqr\credentials` on Windows), an INI file whose
   sections are profiles; `TASQR_PROFILE` selects one (default: `default`):

```ini
[default]
api_key = tasqr_...
```

If you already use the Tasqr MCP server, there is nothing to configure — the status line reuses
the same file. An exported `TASQR_API_KEY` **always wins** over the file; if you have a stale one
in your shell profile, either remove it or wrap the command as
`env -u TASQR_API_KEY python3 …`.

## Settings

Every setting (`tags`, `theme`, `style`, `segments`, `ttl`) can live in any of four layers.
Later layers win:

1. **Global config** at `~/.config/tasqr-statusline/config`: your defaults everywhere.
   ```
   style = bubble
   segments = model,dir,ctx,tasqr,cost
   ```
2. **Per-project map** at `~/.config/tasqr-statusline/projects.conf`, if you'd rather not add
   files to repos. One INI section per project directory, any setting inside:
   ```ini
   [~/Developer/faultline]
   tags = faultline

   [~/Developer/microadventures]
   tags = microadventures
   style = meters
   ```
   A section covers its directory **and everything under it**, so a section on a folder that
   holds several repos covers all of them. Where sections nest, the deepest one wins per setting
   and inherits the rest — a parent can set `tags` while a child overrides only `style`.
   Directories are matched by identity rather than by path text, so a section still matches a
   project reached through a symlink, or written in a different case on a case-insensitive
   filesystem.
3. **Project file**: a `.tasqr-statusline` file in the project directory (committable, so a team
   shares it):
   ```
   tags = faultline
   ```
4. **Environment variables**: `TASQR_STATUSLINE_TAGS`, `_THEME`, `_STYLE`, `_SEGMENTS`, `_TTL`.
   An empty `TASQR_STATUSLINE_TAGS` explicitly disables any file-based filter.

The project directory comes from the Claude Code session, so two concurrent sessions in different
projects each get their own settings, and their own cache snapshot.

## Scoping to a project with tags

If your workspace tracks several projects, an unfiltered queue shows next-up tasks from projects
you aren't touching. Set `tags` in any settings layer and every task segment — active, next-up,
pending count, blocked count — is scoped to tasks carrying **all** of those tags. One tag per
project is the natural setup.

Tags filter the queries themselves, so a scoped line also means fewer tasks fetched, and each
filter gets its own cache file. Two sessions in different projects never overwrite each other's
snapshot.

## Worked setups

**One project, nothing to configure.** Install, and the line shows your whole queue. This is the
right setup when your Tasqr workspace tracks one thing.

**Several projects, mapped centrally.** Tag each project's tasks, then map directories to tags in
`~/.config/tasqr-statusline/projects.conf`. Nothing lands in the repos themselves:

```ini
[~/Developer/faultline]
tags = faultline

[~/Developer/microadventures]
tags = microadventures
style = meters
```

Each session's line now shows only that project's work, and `microadventures` also gets a
different layout — any setting can go in a section, not just tags.

**A folder holding many repos.** A section covers its directory and everything under it, so one
section can cover a whole tree, with a child overriding just the part that differs:

```ini
[~/Developer/clients/acme]
tags = acme
theme = dark

[~/Developer/clients/acme/infra]
tags = acme, infra
```

Every repo under `acme/` gets the `acme` filter and the dark palette; the `infra` repo narrows to
tasks carrying both tags and keeps the palette it inherited.

**A repo your team shares.** Commit a `.tasqr-statusline` file, and everyone who clones the repo
gets the same filter without configuring anything:

```
tags = faultline
```

Keep team-wide settings (`tags`) in the committed file and personal ones (`style`, `theme`) in
your global config, so a shared file doesn't impose your palette on everyone else.

**Trying something for one session.** Environment variables win over every file, so they are the
way to test a look without editing anything:

```bash
TASQR_STATUSLINE_STYLE=panel TASQR_STATUSLINE_THEME=dracula claude
```

An empty `TASQR_STATUSLINE_TAGS=` turns off a file-based filter for one session, which is the
quickest way to check whether a tag filter is why the queue looks empty.

## Themes

Colors follow Tasqr's Signal design language, and the status colors carry the same meanings they
carry on the Tasqr dashboard: amber is in progress, accent blue is pending, coral is blocked.

By default the status line reads your Claude Code `theme` setting (`~/.claude/settings.json`) and
picks a palette tuned for a light or dark background. Any other theme, or no settings file at
all, falls back to plain ANSI colors, which follow your terminal theme and stay mapped by
meaning. Override with `TASQR_STATUSLINE_THEME=light|dark|ansi|dracula`.

`dracula` is not a tuning of Signal but the Dracula palette, assigned by the same meanings:
purple carries accent, pink the branch, orange stands in for amber. It is a theme rather than a
style, so it composes with every layout — `bubble`, `panel` and `meters` all inherit it.

## Styles

Four styles, chosen with `style = plain | bubble | panel | meters` (any settings layer). The
first two are one line; the last two use more than one row. All of them render in an ordinary
monospace font — nothing here needs a patched font.

- **`plain`** (default): colored text with `|` separators.
- **`bubble`**: every segment is a filled chip in the Signal colors, the terminal form of the
  dashboard's status badges. Amber for the task you're on, accent blue for next up, coral for
  blocked. Pure background color, so it renders in any font.
- **`panel`**: a boxed panel across several rows, task state first. The queue gets a row each for
  what you're on, what's next and what's blocked, then a session section with a context bar,
  cost, duration and lines changed:
  ```
  ╭─ TASQR ──────────────────────────────────────────────────────╮
  │ run      Build tasqr-statusline: a public Claude Code line   │
  │ next     Fix lease reclaim on worker restart P2 · 44 pending │
  │ blocked  5                                                   │
  ├─ SESSION ────────────────────────────────────────────────────┤
  │ Opus 5 · tasqr-statusline main* · 08:00                      │
  │ ██░░░░░░░░ 22% · $0.41 · 10m · +128/-34                      │
  ╰──────────────────────────────────────────────────────────────╯
  ```
  Titles get roughly twice the room of a one-line style, so most of them stop scrolling. The box
  sizes itself to its own content — a status line runs headless, with no terminal to ask for a
  width — and stays inside single-column characters, since a width calculation one column out
  shows up as a ragged border.
- **`meters`**: two lines split by what the numbers are about. The work goes on top, and
  underneath it the model with the capacity it is spending:
  ```
  next Fix lease reclaim on worker restart P2 · 44 pending  │  5 blocked  │  tasqr-statusline main*
  Opus 5  │  ctx ▓▓░░░░░░ 22%  │  5h ▓▓░░░░┃░ 26%  │  7d ▓▓▓┃░░░░ 41%
  ```
  Blocked and quota counts sit with the task rather than with the meters: they are task data, and
  grouping numbers by where they come from beats grouping them by shape. Fields are separated by
  `│` rather than `·`, because a title scrolling through its wrap gap shows a `·` of its own, and
  a matching separator would hide where the title ends.

  The `┃` is a pace cursor, marking how far through the reset window the clock is. Fill to the
  left of the cursor means you are burning that window faster than time is passing, so the meter
  reads as ahead of pace or behind rather than as a bare percentage.

`NO_COLOR` collapses `bubble` to plain, since color is all it is. `panel` and `meters` are
structure rather than color, so they keep their shape and render without it.

## Choosing segments

`TASQR_STATUSLINE_SEGMENTS` reorders or trims the line. Default: `model,dir,ctx,tasqr`.

| Segment | Shows |
|---|---|
| `model` | Model display name |
| `dir` | Directory name, plus git branch and a `*` when dirty |
| `ctx` | Context usage as a percentage |
| `bar` | Context usage as a progress bar, `██░░░░░░░░ 22%` |
| `tasqr` | The task segments described at the top |
| `cost` | Session spend, `$1.23` |
| `limits` | Rate-limit meters with pace cursors, `5h ▓▓░░░░┃░ 26% · 7d ▓▓▓┃░░░░ 41%` |
| `lines` | Lines changed this session, `+128/-34` |
| `dur` | Session length, `10m` |
| `time` | Wall clock, `16:21` |

```bash
TASQR_STATUSLINE_SEGMENTS=dir,tasqr,cost
```

Segments other than `tasqr` come from the session JSON Claude Code hands the status line, so a
segment whose data isn't present is simply left out. The `panel` and `meters` styles pick their
own rows and ignore this setting.

## How it stays cheap

The status line command re-runs on every conversation update, so it never touches the network
directly. Renders read a snapshot from `~/.cache/tasqr-statusline/` and, when it is older than
`TASQR_STATUSLINE_TTL` seconds (default 60), spawn one detached background refresh. A refresh is
3 task-list reads, plus a quota read every 5 minutes: roughly 3 requests/minute while you are
actively working, nothing when you are not. Failed refreshes keep serving the last good snapshot
and back off for a full TTL.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `TASQR_PROFILE` | `default` | Profile section in the credentials file |
| `TASQR_API_KEY` / `TASQR_API_URL` | — | Override the credentials file entirely |
| `TASQR_STATUSLINE_TTL` | `60` | Seconds between background refreshes |
| `TASQR_STATUSLINE_TAGS` | — | Tag filter (see *Scoping to a project*) |
| `TASQR_STATUSLINE_THEME` | auto | `light`, `dark`, `ansi` or `dracula`; auto-detects from Claude Code settings |
| `TASQR_STATUSLINE_SEGMENTS` | `model,dir,ctx,tasqr` | Segment list and order (see *Choosing segments*) |
| `TASQR_STATUSLINE_STYLE` | `plain` | `bubble`, `panel`, `meters` |
| `NO_COLOR` | — | Disable ANSI colors (collapses the color-only styles to plain) |

## When the line says something unexpected

The task segments report their own state rather than disappearing, so what the line says narrows
the cause:

| The line says | What it means | What to do |
|---|---|---|
| `tasqr …` | No snapshot yet — the first render after install | Nothing; a background refresh fills it within a TTL |
| `tasqr: no api key` | No key in the environment or the credentials file | See *Credentials*; check `TASQR_PROFILE` if you use profiles |
| `tasqr: unavailable` | Refreshes have run, none has succeeded | Refresh in the foreground, then read the error (below) |
| `(stale)` | The snapshot is over 10 minutes old | Refreshes are failing, or the machine was asleep |
| `tasqr: queue empty` when you expect work | Usually a tag filter narrower than the tasks | Re-run with `TASQR_STATUSLINE_TAGS=` to confirm |

A refresh never prints an error — it records one in the cache, so a failing network can't garble
the status line. To see it, run the refresh yourself and read the snapshot:

```bash
python3 tasqr_statusline.py --refresh              # add --tags=foo for a scoped cache
cat ~/.cache/tasqr-statusline/cache*.json
```

The `error` field holds the exception name from the last failed attempt, and `failed_at` its
timestamp. A cache that still has an `active` key alongside them is being served from the last
good snapshot, which is why a failed refresh shows `(stale)` rather than an empty queue.

## Development

```bash
python3 -m unittest discover -s tests     # the suite; no network, no fixtures to install
ruff check .                              # lint, configured in ruff.toml
```

Both run in CI on every push against Python 3.9 through 3.13, along with a render of the line
end to end. 3.9 is the supported floor.

The suite covers configuration resolution, the cache and refresh cycle, credentials, and the
logic that depends on the clock. What each style looks like is pinned separately, by golden
output in `tests/golden.json`: every style is rendered against every state — active task, next
up, empty queue, stale cache, missing key, first run — and compared verbatim. After deliberately
changing a look, regenerate that file and read the diff:

```bash
python3 tests/test_statusline.py --update-golden
```

## License

MIT
