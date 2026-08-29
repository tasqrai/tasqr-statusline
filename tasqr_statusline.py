#!/usr/bin/env python3
"""Tasqr status line for Claude Code.

Reads the Claude Code status-line JSON on stdin and prints one line showing
your live Tasqr state next to the session basics:

    Fable | llm_task_tracker main | ctx 42% | ▶ Fix lease reclaim…

Tasqr data is served from a local cache (~/.cache/tasqr-statusline) and
refreshed by a detached background process, so rendering never blocks on the
network and the API sees at most a few read requests per minute while you are
actively working. Zero dependencies beyond the Python 3 standard library.

Credentials are read from TASQR_API_KEY, or the same INI file the tasqr-mcp
proxy uses (~/.config/tasqr/credentials, profile selected by TASQR_PROFILE).

Settings (tags, theme, style, segments, ttl) resolve in layers: a global
config file, then this project's entry in projects.conf, then a
.tasqr-statusline file in the project directory, then TASQR_STATUSLINE_*
environment variables. Later layers win.
"""

from __future__ import annotations

import configparser
import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_API_URL = "https://api.tasqr.ai"
DEFAULT_SEGMENTS = "model,dir,ctx,tasqr"
TTL = int(os.environ.get("TASQR_STATUSLINE_TTL", "60"))  # task-list refresh, seconds
QUOTA_TTL = 300  # quota changes slowly; don't spend requests on it
ME_TTL = 3600  # your email effectively never changes
STALE_AFTER = 600  # cache older than this renders a stale marker
TITLE_MAX = 44
HTTP_TIMEOUT = 5

SETTING_KEYS = ("tags", "theme", "style", "segments", "ttl")


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "tasqr-statusline"


def cache_path(tags: list[str]) -> Path:
    """One cache file per tag filter, so concurrent sessions on different
    projects don't overwrite each other's snapshot."""
    if not tags:
        return cache_dir() / "cache.json"
    digest = hashlib.sha256(",".join(tags).encode()).hexdigest()[:8]
    return cache_dir() / f"cache-{digest}.json"


# ---------------------------------------------------------------------------
# Themes

# Role-based palettes following tasqr's Signal design language: accent
# #5B8CFF, mint #54E6B5, amber #E0B23C (--in-progress), coral #FF6B6B
# (--warn: errors and blocked), muted #7C879E. The status semantics match the
# tasqr dashboard: amber means in progress, accent blue means pending, coral
# means blocked. "dark" maps the Signal tokens to their nearest xterm-256
# values (Signal is dark-first); "light" re-derives each hue at readable
# contrast on a light background; "ansi" keeps the terminal's own 16-color
# palette, mapped by meaning, so it adapts to any terminal theme.
PALETTES = {
    "ansi": {
        "dim": "\033[2m",
        "accent": "\033[34m",  # model name, next-up (pending)
        "branch": "\033[2m",
        "ok": "\033[32m",
        "warn": "\033[33m",  # in-progress, quota pressure
        "alert": "\033[31m",  # blocked, critical
    },
    "dark": {
        "dim": "\033[38;5;103m",  # muted #7C879E
        "accent": "\033[38;5;69m",  # accent #5B8CFF
        "branch": "\033[38;5;103m",
        "ok": "\033[38;5;79m",  # mint #54E6B5
        "warn": "\033[38;5;179m",  # amber #E0B23C
        "alert": "\033[38;5;203m",  # coral #FF6B6B
    },
    "light": {
        "dim": "\033[38;5;60m",
        "accent": "\033[38;5;26m",
        "branch": "\033[38;5;60m",
        "ok": "\033[38;5;29m",
        "warn": "\033[38;5;136m",
        "alert": "\033[38;5;167m",
    },
    # Dracula, mapped by the same meanings: purple carries accent, pink the
    # branch, orange stands in for amber, comment blue for muted text.
    "dracula": {
        "dim": "\033[38;5;61m",  # comment #6272a4
        "accent": "\033[38;5;141m",  # purple #bd93f9
        "branch": "\033[38;5;212m",  # pink #ff79c6
        "ok": "\033[38;5;84m",  # green #50fa7b
        "warn": "\033[38;5;215m",  # orange #ffb86c
        "alert": "\033[38;5;203m",  # red #ff5555
    },
}

# Chip colors for the background-painting styles, as (bg, fg) 256-color pairs,
# one table per theme so chips follow the palette. Colored chips use the token
# color as the background with dark on-accent text; neutral chips sit on a
# surface color that tracks the theme's background.
CHIPS = {
    "ansi": {
        "accent": (69, 17),
        "default": (235, 189),
        "dim": (235, 103),
        "ok": (79, 235),
        "warn": (179, 235),
        "alert": (203, 235),
    },
    "dark": {
        "accent": (69, 17),
        "default": (235, 189),
        "dim": (235, 103),
        "ok": (79, 235),
        "warn": (179, 235),
        "alert": (203, 235),
    },
    "light": {
        "accent": (26, 231),
        "default": (254, 236),  # light surface, not the dark-theme near-black
        "dim": (254, 60),
        "ok": (29, 231),
        "warn": (136, 231),
        "alert": (167, 231),
    },
    "dracula": {
        "accent": (141, 236),
        "default": (238, 255),  # current line #44475a, foreground #f8f8f2
        "dim": (238, 61),
        "ok": (84, 236),
        "warn": (215, 236),
        "alert": (203, 236),
    },
}


class Style:
    def __init__(self, theme: str = "ansi", enabled: bool = True):
        palette = PALETTES.get(theme, PALETTES["ansi"])
        for role, code in palette.items():
            setattr(self, role, code if enabled else "")
        self.reset = "\033[0m" if enabled else ""
        self.theme = theme if theme in PALETTES else "ansi"
        self.chips = CHIPS[self.theme]


def resolve_theme(pref: str | None = None, settings_path: Path | None = None) -> str:
    """TASQR_STATUSLINE_THEME wins, then the configured preference, then the
    Claude Code theme setting (~/.claude/settings.json), then adaptive ANSI."""
    theme = os.environ.get("TASQR_STATUSLINE_THEME", "") or (pref or "")
    if not theme:
        path = settings_path or Path.home() / ".claude" / "settings.json"
        try:
            with open(path) as f:
                theme = str(json.load(f).get("theme", ""))
        except (OSError, json.JSONDecodeError, AttributeError):
            theme = ""
    theme = theme.lower()
    if theme in PALETTES:
        return theme
    if "light" in theme:
        return "light"
    if "dark" in theme:
        return "dark"
    return "ansi"


def _want_color() -> bool:
    return "NO_COLOR" not in os.environ


# Bubble style: each segment is painted as a standalone chip using the active
# theme's CHIPS pairs. No emoji, and pure background color rather than edge
# glyphs, so it renders correctly in any font.

STYLES = ("plain", "bubble", "panel", "meters")
# Styles that carry their meaning in color alone; NO_COLOR leaves them with
# nothing to show, so they collapse to plain. The layouts below are structure
# and survive it.
COLOR_ONLY_STYLES = ("bubble",)


def resolve_style(pref: str | None = None) -> str:
    """"plain" (default) or any entry in STYLES. NO_COLOR forces plain for the
    styles that are only color; the multi-line layouts keep their shape."""
    style = (os.environ.get("TASQR_STATUSLINE_STYLE", "") or (pref or "plain")).lower()
    if style not in STYLES or style == "plain":
        return "plain"
    return style if _want_color() or style not in COLOR_ONLY_STYLES else "plain"


def _chip_text(text: str, kind: str) -> str:
    """Adjust segment text for background-chip modes: the ⛔ emoji keeps its
    own red color, which is unreadable on the coral chip — and the chip color
    already says blocked — so spell it out instead."""
    if kind == "blocked":
        return _strip(text, "⛔ ") + " blocked"
    if kind == "stale":
        return "stale"
    return text


def _strip(text: str, prefix: str) -> str:
    return text[len(prefix) :] if text.startswith(prefix) else text


def _bubble(items: list[tuple[str, str, str]], table: dict | None = None) -> str:
    table = table or CHIPS["ansi"]
    pills = []
    for text, role, kind in items:
        bg, fg = table.get(role, table["default"])
        pills.append(f"\033[48;5;{bg}m\033[38;5;{fg}m {_chip_text(text, kind)} \033[0m")
    return " ".join(pills)


# ---------------------------------------------------------------------------
# Credentials


def credentials_path() -> Path:
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or str(Path.home())
        return Path(appdata) / "tasqr" / "credentials"
    return Path.home() / ".config" / "tasqr" / "credentials"


def read_credentials() -> tuple[str | None, str]:
    """Return (api_key, api_url). Env vars win; the tasqr-mcp INI is the fallback."""
    api_key = os.environ.get("TASQR_API_KEY")
    api_url = os.environ.get("TASQR_API_URL")
    if not api_key or not api_url:
        path = credentials_path()
        if path.exists():
            config = configparser.ConfigParser()
            try:
                config.read(path)
            except configparser.Error:
                config = None
            if config is not None:
                profile = os.environ.get("TASQR_PROFILE", "default")
                if profile in config:
                    section = config[profile]
                    api_key = api_key or section.get("api_key") or None
                    api_url = api_url or section.get("api_url") or None
    return api_key or None, (api_url or DEFAULT_API_URL).rstrip("/")


# ---------------------------------------------------------------------------
# Settings — layered: global config < projects.conf < project file < env


def parse_tags(text: str) -> list[str]:
    """Parse a tag list from free-ish text: commas or whitespace separate,
    '#' starts a comment, an optional 'tags =' prefix is tolerated."""
    tags: list[str] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0]
        if "=" in line:
            key, _, value = line.partition("=")
            if key.strip().lower() != "tags":
                continue
            line = value
        tags.extend(t.strip().lower() for t in re.split(r"[,\s]+", line) if t.strip())
    return sorted(set(tags))


def parse_settings_text(text: str) -> dict[str, str]:
    """key = value lines ('#' comments); a bare non-kv line is tag shorthand."""
    out: dict[str, str] = {}
    bare_tags: list[str] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip().lower()
            if key in SETTING_KEYS:
                out[key] = value.strip()
        else:
            bare_tags.append(line)
    if bare_tags and "tags" not in out:
        out["tags"] = " ".join(bare_tags)
    return out


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "tasqr-statusline"


def projects_conf_path() -> Path:
    return config_dir() / "projects.conf"


def _projects_conf_settings(project_dir: str) -> dict[str, str]:
    """Settings from every projects.conf section covering this directory."""
    conf = projects_conf_path()
    if not conf.is_file():
        return {}
    config = configparser.ConfigParser()
    try:
        config.read(conf)
    except configparser.Error:
        return {}
    chain = _self_and_ancestors(project_dir)
    matches = []
    for section in config.sections():
        base = _norm(section)
        if _dir_id(base) in chain:
            matches.append((base.count(os.sep), section))
    # Shallowest first, so a section deeper in the tree overrides one above it
    # but inherits everything it doesn't name — the same way the config file,
    # projects.conf, project file and environment layer.
    merged: dict[str, str] = {}
    for _, section in sorted(matches):
        merged.update({k: config[section][k] for k in SETTING_KEYS if k in config[section]})
    return merged


def _norm(path: str) -> str:
    """Absolute, ~-expanded, symlinks resolved, no trailing separator."""
    return os.path.realpath(os.path.expanduser(path)).rstrip(os.sep)


def _dir_id(path: str):
    """A directory's identity for comparison. Two paths can name one
    directory without matching as strings — reached through a symlink, or
    written in a different case on a case-insensitive filesystem — so use
    (device, inode) where the directory exists. Paths that don't exist fall
    back to their normalized string, which keeps section matching working for
    a directory that is merely absent."""
    norm = _norm(path)
    try:
        st = os.stat(norm)
    except OSError:
        return norm
    return (st.st_dev, st.st_ino)


def _self_and_ancestors(path: str) -> set:
    """Identities of the directory and every directory above it. A section
    matches when it names any of them, which is what makes a section cover
    everything underneath it. Walking components this way is also why
    /w/micro never matches /w/microadventures."""
    seen, current = set(), _norm(path)
    while True:
        seen.add(_dir_id(current))
        parent = os.path.dirname(current)
        if parent == current:
            return seen
        current = parent


def load_settings(project_dir: str | None) -> dict:
    """Merge all settings sources; every key works in every layer."""
    merged: dict[str, str] = {}

    global_conf = config_dir() / "config"
    try:
        if global_conf.is_file():
            merged.update(parse_settings_text(global_conf.read_text()))
    except OSError:
        pass

    if project_dir:
        merged.update(_projects_conf_settings(project_dir))
        local = Path(project_dir) / ".tasqr-statusline"
        try:
            if local.is_file():
                merged.update(parse_settings_text(local.read_text()))
        except OSError:
            pass

    for key in SETTING_KEYS:
        env = os.environ.get(f"TASQR_STATUSLINE_{key.upper()}")
        if env is not None:
            merged[key] = env

    segments = None
    if merged.get("segments"):
        segments = [p.strip() for p in merged["segments"].split(",") if p.strip()]
    try:
        ttl = int(merged["ttl"])
    except (KeyError, ValueError):
        ttl = TTL
    return {
        "tags": parse_tags(merged.get("tags", "")),
        "theme": merged.get("theme") or None,
        "style": merged.get("style") or None,
        "segments": segments,
        "ttl": ttl,
    }


def resolve_tags(project_dir: str | None) -> list[str]:
    return load_settings(project_dir)["tags"]


# ---------------------------------------------------------------------------
# Cache


def load_cache(tags: list[str]) -> dict:
    try:
        with open(cache_path(tags)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_cache(data: dict, tags: list[str]) -> None:
    d = cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".cache.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(data))
    tmp.replace(cache_path(tags))


def needs_refresh(cache: dict, now: float, ttl: int | None = None) -> bool:
    """True when the cache is past its TTL, honoring the failure back-off."""
    last_attempt = max(cache.get("fetched_at", 0), cache.get("failed_at", 0))
    return now - last_attempt > (ttl or TTL)


def spawn_refresh(tags: list[str]) -> None:
    """Kick a detached refresh; rendering never waits on the network."""
    args = [sys.executable, os.path.abspath(__file__), "--refresh"]
    if tags:
        args.append("--tags=" + ",".join(tags))
    try:
        with open(os.devnull, "r+b") as devnull:
            subprocess.Popen(
                args, stdin=devnull, stdout=devnull, stderr=devnull, start_new_session=True
            )
    except OSError:
        # No subprocess available (restricted container, process limit). Render
        # from whatever snapshot exists; the next render retries the spawn.
        pass


# ---------------------------------------------------------------------------
# Tasqr API


def api_get(api_url: str, api_key: str, path: str) -> dict:
    req = urllib.request.Request(api_url + path, headers={"X-Api-Key": api_key})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.load(resp)


def _count(payload: dict) -> str:
    """Render a list_tasks page count; a cursor means there are more."""
    n = payload.get("count", len(payload.get("tasks", [])))
    return f"{n}+" if payload.get("cursor") else str(n)


def _next_up(tasks: list[dict]) -> dict | None:
    """The task claim-order would favor: highest priority, then least recently touched."""
    if not tasks:
        return None
    return min(tasks, key=lambda t: (t.get("priority", 3), t.get("updated_at", "")))


def refresh(tags: list[str]) -> None:
    now = time.time()
    old = load_cache(tags)
    api_key, api_url = read_credentials()
    if not api_key:
        write_cache({"fetched_at": now, "error": "no_key"}, tags)
        return

    data: dict = {"fetched_at": now, "tags": tags}
    # Slow-moving fields ride along from the previous cache until their own TTL lapses.
    for field in ("email", "me_at", "quota", "quota_at"):
        if field in old:
            data[field] = old[field]

    tag_query = "&tags=" + urllib.parse.quote(",".join(tags)) if tags else ""
    try:
        if now - data.get("me_at", 0) > ME_TTL:
            data["email"] = api_get(api_url, api_key, "/me").get("email")
            data["me_at"] = now

        email = data.get("email")
        active_path = "/tasks?status=in_progress&limit=50" + tag_query
        if email:
            active_path += "&assignee=" + urllib.parse.quote(email)
        active = api_get(api_url, api_key, active_path)
        pending = api_get(api_url, api_key, "/tasks?status=pending&limit=50" + tag_query)
        blocked = api_get(api_url, api_key, "/tasks?status=blocked&limit=50" + tag_query)

        if now - data.get("quota_at", 0) > QUOTA_TTL:
            q = api_get(api_url, api_key, "/quota")
            limit = q.get("limit") or 0
            data["quota"] = {
                "used": q.get("used", 0),
                "limit": limit,
                "pct": round(100 * q.get("used", 0) / limit) if limit else 0,
            }
            data["quota_at"] = now
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        # Keep serving the previous snapshot; failed_at backs off the next attempt.
        old["failed_at"] = now
        old["error"] = type(e).__name__
        write_cache(old, tags)
        return

    active_tasks = active.get("tasks", [])
    data["active"] = [
        {"title": t.get("title", ""), "priority": t.get("priority", 3)} for t in active_tasks[:3]
    ]
    data["active_count"] = len(active_tasks)
    nxt = _next_up(pending.get("tasks", []))
    data["next"] = (
        {"title": nxt.get("title", ""), "priority": nxt.get("priority", 3)} if nxt else None
    )
    data["pending_count"] = _count(pending)
    data["blocked_count"] = _count(blocked)
    data.pop("error", None)
    write_cache(data, tags)


# ---------------------------------------------------------------------------
# Session


def _limit(raw: dict | None) -> dict | None:
    """One rate-limit window, or None when Claude Code didn't report it."""
    if not raw:
        return None
    return {"pct": raw.get("used_percentage", 0), "resets_at": raw.get("resets_at")}


@dataclasses.dataclass
class Session:
    """The Claude Code status-line JSON, normalized once. Styles that draw
    bars and meters need the numbers, not the pre-formatted "ctx 22%" text,
    so parsing happens here and every segment reads from the result."""

    model: str | None = None
    directory: str | None = None
    worktree: str | None = None
    ctx_pct: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    window_size: int | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    lines_added: int | None = None
    lines_removed: int | None = None
    five_hour: dict | None = None
    seven_day: dict | None = None
    vim_mode: str | None = None

    @classmethod
    def from_stdin(cls, data: dict) -> Session:
        workspace = data.get("workspace") or {}
        cost = data.get("cost") or {}
        ctx = data.get("context_window") or {}
        limits = data.get("rate_limits") or {}
        return cls(
            model=(data.get("model") or {}).get("display_name"),
            directory=workspace.get("current_dir") or data.get("cwd"),
            worktree=workspace.get("git_worktree"),
            ctx_pct=ctx.get("used_percentage"),
            input_tokens=ctx.get("total_input_tokens"),
            output_tokens=ctx.get("total_output_tokens"),
            window_size=ctx.get("context_window_size"),
            cost_usd=cost.get("total_cost_usd"),
            duration_ms=cost.get("total_duration_ms"),
            lines_added=cost.get("total_lines_added"),
            lines_removed=cost.get("total_lines_removed"),
            five_hour=_limit(limits.get("five_hour")),
            seven_day=_limit(limits.get("seven_day")),
            vim_mode=(data.get("vim") or {}).get("mode"),
        )


# ---------------------------------------------------------------------------
# Bars, meters, widths

ANSI_RE = re.compile(r"\033\[[0-9;]*m")
BAR_FULL = "\u2588"  # solid, for the context bar
BAR_EMPTY = "\u2591"
METER_FULL = "\u2593"  # shaded, so meters read differently from the context bar
PACE_CURSOR = "\u2503"

FIVE_HOURS = 5 * 3600
SEVEN_DAYS = 7 * 86400


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def display_width(text: str) -> int:
    """Columns the text occupies. Correct only for narrow characters, which is
    why the panel style sticks to them — a box that miscounts is a box with
    ragged borders."""
    return len(strip_ansi(text))


def _cells(pct: float | None, width: int) -> int:
    return round(min(100.0, max(0.0, float(pct or 0))) / 100 * width)


def progress_bar(pct: float | None, width: int = 10) -> str:
    filled = _cells(pct, width)
    return BAR_FULL * filled + BAR_EMPTY * (width - filled)


def meter(pct: float | None, width: int = 8, pace: float | None = None) -> str:
    """A usage meter. `pace` (0..1) marks how far through the reset window the
    clock is, so the bar shows whether you are burning quota faster than time:
    fill left of the cursor is ahead of pace, right of it is behind."""
    filled = _cells(pct, width)
    cells = [METER_FULL] * filled + [BAR_EMPTY] * (width - filled)
    if pace is not None:
        cells[min(width - 1, max(0, int(pace * width)))] = PACE_CURSOR
    return "".join(cells)


def pace_fraction(resets_at: float | None, window_seconds: int, now: float) -> float | None:
    """Elapsed share of a rate-limit window, from the reset timestamp."""
    if not resets_at or not window_seconds:
        return None
    return min(1.0, max(0.0, 1.0 - (resets_at - now) / window_seconds))


def human_duration(ms: float | None) -> str | None:
    """One unit, widest that fits: 45s, 10m, 3h15m."""
    if ms is None:
        return None
    secs = int(ms / 1000)
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    return f"{mins}m" if mins < 60 else f"{mins // 60}h{mins % 60:02d}m"


# ---------------------------------------------------------------------------
# Rendering


MARQUEE_GAP = "  ·  "


def marquee(text: str, limit: int = TITLE_MAX, now: float | None = None) -> str:
    """Fit text into `limit` columns. A long title scrolls marquee-style,
    advancing one column per second of `now` and wrapping through a gap, so
    each re-render of the status line shows the next window. Without `now`
    it truncates with an ellipsis."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    if now is None:
        return text[: limit - 1].rstrip() + "…"
    loop = text + MARQUEE_GAP
    offset = int(now) % len(loop)
    return (loop + loop)[offset : offset + limit]


def _git(directory: str, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", directory, *args], capture_output=True, text=True, timeout=2
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


def git_branch(directory: str) -> str | None:
    out = _git(directory, "rev-parse", "--abbrev-ref", "HEAD")
    branch = out.strip() if out else ""
    return branch or None


def git_dirty(directory: str) -> bool:
    out = _git(directory, "status", "--porcelain", "-uno")
    return bool(out and out.strip())


def _next_segment(cache: dict, now: float, s: Style, limit: int) -> tuple[str, str, str]:
    """The next-up task. Accent blue, matching the dashboard's pending color."""
    nxt = cache["next"]
    prio = f" {s.alert}P{nxt['priority']}{s.reset}" if nxt.get("priority", 3) <= 2 else ""
    return (
        f"{s.accent}next:{s.reset} {marquee(nxt['title'], limit=limit, now=now)}{prio}"
        f" {s.dim}· {cache.get('pending_count', '?')} pending{s.reset}",
        "accent",
        "next",
    )


def tasqr_segments(
    cache: dict,
    now: float,
    s: Style,
    limit: int = TITLE_MAX,
    include_next: bool = False,
) -> list[tuple[str, str, str]]:
    """Tasqr items as (text, role, kind) — role picks the chip color in the
    styled modes, kind picks the label or decoration the chip and layout styles
    put on it; plain style ignores both (the inline codes in text carry the
    color)."""
    if not cache:
        return [(f"{s.dim}tasqr …{s.reset}", "dim", "status")]
    if cache.get("error") == "no_key":
        return [(f"{s.dim}tasqr: no api key{s.reset}", "dim", "status")]
    if "active" not in cache:
        # Refreshes have run but never succeeded — don't claim the queue is empty.
        return [(f"{s.dim}tasqr: unavailable{s.reset}", "dim", "status")]

    segs = []
    active = cache.get("active") or []
    if active:
        # Amber, matching the dashboard's in-progress status color.
        seg = f"{s.warn}▶{s.reset} {marquee(active[0]['title'], limit=limit, now=now)}"
        if cache.get("active_count", 1) > 1:
            seg += f" {s.dim}+{cache['active_count'] - 1}{s.reset}"
        segs.append((seg, "warn", "active"))
        if include_next and cache.get("next"):
            segs.append(_next_segment(cache, now, s, limit))
    elif cache.get("next"):
        segs.append(_next_segment(cache, now, s, limit))
    else:
        segs.append((f"{s.dim}tasqr: queue empty{s.reset}", "dim", "empty"))

    blocked = cache.get("blocked_count", "0")
    if blocked not in ("0", 0):
        segs.append((f"{s.alert}⛔ {blocked}{s.reset}", "alert", "blocked"))

    quota = cache.get("quota") or {}
    pct = quota.get("pct", 0)
    if pct >= 80:
        color = s.alert if pct >= 95 else s.warn
        segs.append((f"{color}tasks {pct}%{s.reset}", "alert" if pct >= 95 else "warn", "quota"))

    if now - cache.get("fetched_at", 0) > STALE_AFTER:
        segs.append((f"{s.dim}(stale){s.reset}", "dim", "stale"))
    return segs


def build_items(
    session: Session,
    cache: dict,
    now: float,
    s: Style,
    segments: list[str],
) -> list[tuple[str, str, str]]:
    """Turn the requested segment names into (text, role, kind) tuples."""
    items: list[tuple[str, str, str]] = []
    for name in segments:
        if name == "model":
            if session.model:
                items.append((f"{s.accent}{session.model}{s.reset}", "accent", "model"))
        elif name == "dir":
            if session.directory:
                seg = Path(session.directory).name
                branch = git_branch(session.directory)
                if branch:
                    dirty = f"{s.warn}*{s.reset}" if git_dirty(session.directory) else ""
                    seg += f" {s.branch}{branch}{s.reset}{dirty}"
                items.append((seg, "default", "dir"))
        elif name == "ctx":
            if session.ctx_pct is not None:
                role = _usage_role(session.ctx_pct)
                items.append(
                    (f"{getattr(s, role)}ctx {session.ctx_pct:.0f}%{s.reset}", role, "ctx")
                )
        elif name == "bar":
            if session.ctx_pct is not None:
                role = _meter_role(session.ctx_pct)
                bar = f"{progress_bar(session.ctx_pct)} {session.ctx_pct:.0f}%"
                items.append((f"{getattr(s, role)}{bar}{s.reset}", role, "bar"))
        elif name == "limits":
            seg = _limits_text(session, now, s)
            if seg:
                items.append(seg)
        elif name == "lines":
            if session.lines_added is not None and session.lines_removed is not None:
                text = f"+{session.lines_added}/-{session.lines_removed}"
                items.append((f"{s.dim}{text}{s.reset}", "dim", "lines"))
        elif name == "dur":
            spent = human_duration(session.duration_ms)
            if spent:
                items.append((f"{s.dim}{spent}{s.reset}", "dim", "dur"))
        elif name == "time":
            clock = time.strftime("%H:%M", time.localtime(now))
            items.append((f"{s.dim}{clock}{s.reset}", "dim", "time"))
        elif name == "cost":
            if session.cost_usd is not None:
                items.append((f"{s.dim}${session.cost_usd:.2f}{s.reset}", "dim", "cost"))
        elif name == "tasqr":
            items.extend(tasqr_segments(cache, now, s))
    return items


def _usage_role(pct: float) -> str:
    """Shared thresholds for anything measuring how full something is."""
    return "alert" if pct >= 80 else ("warn" if pct >= 60 else "dim")


def _meter_role(pct: float) -> str:
    """As above, but for drawn gauges. A bar already shows its level by how
    full it is, so a healthy one reads as healthy (mint) rather than as
    absent — dim is right for an understated "ctx 22%", wrong for a meter."""
    return "alert" if pct >= 85 else ("warn" if pct >= 60 else "ok")


RATE_WINDOWS = (("5h", "five_hour", FIVE_HOURS), ("7d", "seven_day", SEVEN_DAYS))


def _limits_text(session: Session, now: float, s: Style) -> tuple[str, str, str] | None:
    parts, worst = [], 0.0
    for label, field, window in RATE_WINDOWS:
        limit = getattr(session, field)
        if not limit:
            continue
        pct = limit["pct"]
        worst = max(worst, pct)
        gauge = meter(pct, pace=pace_fraction(limit["resets_at"], window, now))
        parts.append(f"{label} {gauge} {pct:.0f}%")
    if not parts:
        return None
    role = _meter_role(worst)
    return (f"{getattr(s, role)}{' · '.join(parts)}{s.reset}", role, "limits")


# ---------------------------------------------------------------------------
# Panel style: a multi-line box, task state first. Claude Code renders each
# printed line as its own row. The box is sized to its own content because a
# status line runs headless — there is no tty to ask for a width — and it
# stays inside single-column characters, since a width calculation that is one
# column off shows up as a ragged border.

PANEL_TITLE_MAX = 56
PANEL_LABELS = {
    "active": "run",
    "next": "next",
    "empty": "queue",
    "blocked": "blocked",
    "quota": "quota",
    "stale": "cache",
    "status": "tasqr",
}
PANEL_ROWS = (("model", "dir", "time"), ("bar", "cost", "dur", "lines"))


def _panel_text(text: str, kind: str) -> str:
    """Segment text with its inline glyph dropped: the label column already
    says what the row is, and ⛔ is two columns wide."""
    if kind == "active":
        return _strip(text, "▶ ")
    if kind == "next":
        return _strip(text, "next: ")
    if kind == "blocked":
        return _strip(text, "⛔ ")
    if kind == "quota":
        return _strip(text, "tasks ")
    if kind == "empty":
        return "empty"
    if kind == "stale":
        return "over 10 minutes old"
    if kind == "status":
        return _strip(_strip(text, "tasqr: "), "tasqr ")
    return text


def _panel(session: Session, cache: dict, now: float, s: Style) -> str:
    plain = Style(enabled=False)
    tasks = [
        (PANEL_LABELS.get(kind, kind), _panel_text(text, kind), role)
        for text, role, kind in tasqr_segments(
            cache, now, plain, limit=PANEL_TITLE_MAX, include_next=True
        )
    ]
    meta = []
    for names in PANEL_ROWS:
        parts = [text for text, _, _ in build_items(session, cache, now, plain, list(names))]
        if parts:
            meta.append(("", " · ".join(parts), "default"))

    sections = [("TASQR", tasks)] + ([("SESSION", meta)] if meta else [])
    body: list[tuple[str, str, str]] = []  # (kind, bare, colored)
    for title, rows in sections:
        body.append(("head", title, f"{s.dim}{title}{s.reset}"))
        pad = max((len(label) for label, _, _ in rows), default=0)
        for label, text, role in rows:
            head = f"{label:<{pad}}  " if pad else ""
            colored = f"{getattr(s, role, '')}{head}{s.reset}{text}" if label else head + text
            body.append(("row", head + text, colored))

    inner = max([len(bare) for kind, bare, _ in body if kind == "row"] + [0])
    inner = max(inner, *(len(title) + 4 for title, _ in sections))
    def rule(left: str, right: str, title: str) -> str:
        return f"{s.dim}{left}─ {title} " + "─" * (inner - len(title) - 1) + f"{right}{s.reset}"

    out = []
    for i, (kind, bare, colored) in enumerate(body):
        if kind == "head":
            out.append(rule(*("╭", "╮") if i == 0 else ("├", "┤"), bare))
        else:
            out.append(f"{s.dim}│{s.reset} {colored}{' ' * (inner - len(bare))} {s.dim}│{s.reset}")
    out.append(f"{s.dim}╰" + "─" * (inner + 2) + f"╯{s.reset}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Meters style: two lines — the work, then how full everything is. The pace
# cursor marks how far through each reset window the clock is, so a meter
# reads as ahead of or behind pace rather than as a bare percentage.

METER_WIDTH = 8
METERS_TITLE_MAX = 40
# Line 1 carries the task data: active or next-up, blocked, quota, staleness.
# Line 2 carries the model and the capacity meters.
TASK_KINDS = ("active", "next", "empty", "status")
# Not the marquee gap: a title wrapping mid-scroll prints that gap, and a
# separator matching it would hide where the title ends.
METERS_SEP = "  │  "

def _meters(session: Session, cache: dict, now: float, s: Style) -> str:
    plain = Style(enabled=False)
    segs = tasqr_segments(cache, now, plain, limit=METERS_TITLE_MAX)

    top = [
        f"{getattr(s, role, '')}{PANEL_LABELS.get(kind, kind)}{s.reset} {_panel_text(text, kind)}"
        if kind in TASK_KINDS
        else f"{getattr(s, role, '')}{_chip_text(text, kind)}{s.reset}"
        for text, role, kind in segs
    ]
    top += [text for text, _, _ in build_items(session, cache, now, s, ["dir"])]

    # The model leads the bottom row; the rest of it measures what that model
    # is consuming.
    gauges = [text for text, _, _ in build_items(session, cache, now, s, ["model"])]
    if session.ctx_pct is not None:
        role = _meter_role(session.ctx_pct)
        gauges.append(
            f"{getattr(s, role)}ctx {meter(session.ctx_pct, METER_WIDTH)}"
            f" {session.ctx_pct:.0f}%{s.reset}"
        )
    for label, field, window in RATE_WINDOWS:
        limit = getattr(session, field)
        if not limit:
            continue
        pct = limit["pct"]
        gauge = meter(pct, METER_WIDTH, pace=pace_fraction(limit["resets_at"], window, now))
        gauges.append(f"{getattr(s, _meter_role(pct))}{label} {gauge} {pct:.0f}%{s.reset}")

    lines = [METERS_SEP.join(part for part in top if part)]
    if gauges:
        lines.append(METERS_SEP.join(gauges))
    return "\n".join(lines)


def render(
    stdin_data: dict,
    cache: dict,
    now: float,
    s: Style,
    segments: list[str] | None = None,
    style: str | None = None,
) -> str:
    if style is None:
        style = resolve_style()
    if segments is None:
        raw = os.environ.get("TASQR_STATUSLINE_SEGMENTS", DEFAULT_SEGMENTS)
        segments = [p.strip() for p in raw.split(",") if p.strip()]

    session = Session.from_stdin(stdin_data)
    if style == "panel":
        # Layouts, not segment joiners: they choose their own rows.
        return _panel(session, cache, now, s)
    if style == "meters":
        return _meters(session, cache, now, s)

    # The styled modes paint whole chips per role, so inline codes must stay
    # out of the text; plain keeps its finer-grained inline coloring.
    inner = s if style == "plain" else Style(enabled=False)
    items = build_items(session, cache, now, inner, segments)

    if style == "bubble":
        return _bubble(items, s.chips)
    sep = f" {s.dim}|{s.reset} "
    return sep.join(text for text, _, _ in items)


def main() -> None:
    argv = sys.argv[1:]
    if "--refresh" in argv:
        tags: list[str] = []
        for arg in argv:
            if arg.startswith("--tags="):
                tags = parse_tags(arg[len("--tags=") :])
        refresh(tags)
        return

    try:
        stdin_data = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        stdin_data = {}
    workspace = stdin_data.get("workspace") or {}
    project_dir = (
        workspace.get("project_dir") or workspace.get("current_dir") or stdin_data.get("cwd")
    )
    settings = load_settings(project_dir)
    now = time.time()
    cache = load_cache(settings["tags"])
    if needs_refresh(cache, now, settings["ttl"]):
        spawn_refresh(settings["tags"])
    style = Style(theme=resolve_theme(settings["theme"]), enabled=_want_color())
    print(
        render(
            stdin_data,
            cache,
            now,
            style,
            segments=settings["segments"],
            style=resolve_style(settings["style"]),
        )
    )


if __name__ == "__main__":
    main()
