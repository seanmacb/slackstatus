#!/usr/bin/env python3
"""slackstatus: set your Slack status across multiple workspaces at once.

Standard library only. Configuration lives in ~/.config/slackstatus/config.json.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional, Tuple

CONFIG_PATH = Path.home() / ".config" / "slackstatus" / "config.json"
API_BASE = "https://slack.com/api/"
HTTP_TIMEOUT = 15  # seconds
FALLBACK_CA_FILES = ("/etc/ssl/cert.pem",)  # macOS system bundle

DEFAULT_CONFIG = {
    "workspaces": [],
    "presets": {
        "focus": {"text": "Focusing", "emoji": ":headphones:", "duration": "2h"},
        "meeting": {"text": "In a meeting", "emoji": ":calendar:", "duration": "1h"},
        "lunch": {"text": "Out for lunch", "emoji": ":fork_and_knife:", "duration": "1h"},
        "ooo": {"text": "Out of office", "emoji": ":palm_tree:"},
    },
}


class ConfigError(Exception):
    pass


# --- Time parsing -----------------------------------------------------------

_DURATION_RE = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?$")
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_duration(text: str) -> int:
    """Parse a duration like '30m', '1h', '2h30m' or '1d' into seconds."""
    cleaned = text.strip().lower()
    match = _DURATION_RE.match(cleaned)
    if not cleaned or not match:
        raise ValueError(f"invalid duration {text!r} (use e.g. 30m, 1h, 2h30m, 1d)")
    days, hours, minutes = (int(g) if g else 0 for g in match.groups())
    seconds = days * 86400 + hours * 3600 + minutes * 60
    if seconds <= 0:
        raise ValueError(f"duration {text!r} must be greater than zero")
    return seconds


def next_occurrence(hhmm: str, now: Optional[datetime] = None) -> datetime:
    """Return the next local datetime (strictly after `now`) at wall-clock HH:MM."""
    match = _HHMM_RE.match(hhmm.strip())
    if not match:
        raise ValueError(f"invalid time {hhmm!r} (expected HH:MM, 24-hour)")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"invalid time {hhmm!r} (expected HH:MM, 24-hour)")
    now = now or datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        # Naive local datetimes keep the wall-clock time across DST changes.
        target += timedelta(days=1)
    return target


def compute_expiration(
    duration: Optional[str] = None,
    until: Optional[str] = None,
    now: Optional[datetime] = None,
) -> int:
    """Unix timestamp for status_expiration; 0 means the status never expires."""
    now = now or datetime.now()
    if duration:
        return int(now.timestamp()) + parse_duration(duration)
    if until:
        return int(next_occurrence(until, now).timestamp())
    return 0


def describe_expiration(ts: int) -> str:
    if not ts:
        return "no expiry"
    return "until " + datetime.fromtimestamp(ts).strftime("%a %H:%M")


# --- Config -----------------------------------------------------------------

def save_config(cfg: dict) -> None:
    """Atomically write the config with 0600 permissions."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=CONFIG_PATH.parent, prefix=".config-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        print(f"Created config template at {CONFIG_PATH}", file=sys.stderr)
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(f"{CONFIG_PATH} is not valid JSON (line {e.lineno}, column {e.colno})")
    if not isinstance(cfg, dict):
        raise ConfigError(f"{CONFIG_PATH} must contain a JSON object")
    cfg.setdefault("workspaces", [])
    cfg.setdefault("presets", {})
    workspaces = cfg["workspaces"]
    if not isinstance(workspaces, list):
        raise ConfigError("'workspaces' must be a list")
    for i, ws in enumerate(workspaces, 1):
        if not isinstance(ws, dict) or not ws.get("name") or not ws.get("token"):
            raise ConfigError(f"workspace #{i} needs both 'name' and 'token'")
    if not isinstance(cfg["presets"], dict):
        raise ConfigError("'presets' must be an object")
    return cfg


def require_workspaces(cfg: dict) -> list:
    if not cfg["workspaces"]:
        raise ConfigError(
            f"No workspaces configured. Run `slackstatus add-workspace` or edit {CONFIG_PATH}"
        )
    return cfg["workspaces"]


# --- Slack API --------------------------------------------------------------

def _retry_after(headers) -> int:
    try:
        return max(0, int(headers.get("Retry-After", "1")))
    except (TypeError, ValueError):
        return 1


def ssl_context() -> ssl.SSLContext:
    """Default verifying context, falling back to the OS CA bundle.

    The python.org macOS installer ships without CA certificates until
    "Install Certificates.command" is run, so every HTTPS call fails with
    CERTIFICATE_VERIFY_FAILED. Verification stays on either way.
    """
    ctx = ssl.create_default_context()
    paths = ssl.get_default_verify_paths()
    if not paths.cafile and not paths.capath:
        for ca_file in FALLBACK_CA_FILES:
            if os.path.exists(ca_file):
                ctx.load_verify_locations(cafile=ca_file)
                break
    return ctx


def slack_call(token: str, method: str, payload: Optional[dict] = None) -> dict:
    """POST to a Slack Web API method and return the JSON response.

    Transport failures are reported as {"ok": False, "error": ...} so callers
    only ever deal with Slack-style results. HTTP 429 is retried once after
    the server's Retry-After delay.
    """
    body = json.dumps(payload or {}).encode("utf-8")
    for attempt in range(2):
        req = urllib.request.Request(
            API_BASE + method,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ssl_context()) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                if attempt == 0:
                    time.sleep(_retry_after(e.headers))
                    continue
                return {"ok": False, "error": "ratelimited"}
            return {"ok": False, "error": f"http_{e.code}"}
        except urllib.error.URLError as e:
            return {"ok": False, "error": f"network_error ({e.reason})"}
        except (OSError, TimeoutError) as e:
            return {"ok": False, "error": f"network_error ({type(e).__name__})"}
        except ValueError:
            return {"ok": False, "error": "invalid_response"}
    return {"ok": False, "error": "ratelimited"}


Result = Tuple[bool, str]


def run_all(workspaces: list, action: Callable[[dict], Result]) -> bool:
    """Run `action` on every workspace in parallel and print one line each.

    Returns True only if every workspace succeeded.
    """
    width = max(len(ws["name"]) for ws in workspaces)
    all_ok = True
    with ThreadPoolExecutor(max_workers=len(workspaces)) as pool:
        futures = [pool.submit(action, ws) for ws in workspaces]
        for ws, future in zip(workspaces, futures):
            try:
                ok, detail = future.result()
            except Exception as e:  # one broken workspace must not stop the rest
                ok, detail = False, f"unexpected_error ({type(e).__name__})"
            all_ok &= ok
            if ok:
                line = "ok" + (f"  ({detail})" if detail else "")
            else:
                line = f"error: {detail}"
            print(f"{ws['name']:<{width}}  {line}", flush=True)
    return all_ok


def set_status(token: str, text: str, emoji: str, expiration: int) -> Result:
    resp = slack_call(
        token,
        "users.profile.set",
        {"profile": {"status_text": text, "status_emoji": emoji, "status_expiration": expiration}},
    )
    if resp.get("ok"):
        return True, ""
    return False, resp.get("error", "unknown_error")


def auth_test(token: str) -> Result:
    resp = slack_call(token, "auth.test")
    if resp.get("ok"):
        return True, f"{resp.get('team', '?')} as {resp.get('user', '?')}"
    return False, resp.get("error", "unknown_error")


# --- Commands ---------------------------------------------------------------

def apply_status(cfg: dict, text: str, emoji: str, expiration: int) -> int:
    workspaces = require_workspaces(cfg)
    if text or emoji:
        label = " ".join(p for p in (f'"{text}"' if text else "", emoji) if p)
        print(f"Setting {label} ({describe_expiration(expiration)}) on {len(workspaces)} workspace(s)")
    else:
        print(f"Clearing status on {len(workspaces)} workspace(s)")
    ok = run_all(workspaces, lambda ws: set_status(ws["token"], text, emoji, expiration))
    return 0 if ok else 1


def cmd_set(args, cfg: dict) -> int:
    try:
        expiration = compute_expiration(args.duration, args.until)
    except ValueError as e:
        print(f"slackstatus: {e}", file=sys.stderr)
        return 2
    return apply_status(cfg, args.text, args.emoji, expiration)


def cmd_preset(args, cfg: dict) -> int:
    presets = cfg["presets"]
    preset = presets.get(args.name)
    if not isinstance(preset, dict):
        available = ", ".join(sorted(presets)) or "(none)"
        print(f"slackstatus: unknown preset {args.name!r}. Available: {available}", file=sys.stderr)
        return 2
    try:
        expiration = compute_expiration(preset.get("duration"))
    except ValueError as e:
        print(f"slackstatus: preset {args.name!r}: {e}", file=sys.stderr)
        return 2
    return apply_status(cfg, preset.get("text", ""), preset.get("emoji", ""), expiration)


def cmd_clear(args, cfg: dict) -> int:
    return apply_status(cfg, "", "", 0)


def cmd_list(args, cfg: dict) -> int:
    workspaces = require_workspaces(cfg)
    ok = run_all(workspaces, lambda ws: auth_test(ws["token"]))
    return 0 if ok else 1


def cmd_add_workspace(args, cfg: dict) -> int:
    name = input("Workspace name: ").strip()
    if not name:
        print("slackstatus: name cannot be empty", file=sys.stderr)
        return 2
    existing = next((ws for ws in cfg["workspaces"] if ws.get("name") == name), None)
    if existing:
        answer = input(f"Workspace {name!r} already exists. Replace its token? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 1
    token = getpass.getpass("User OAuth Token (xoxp-..., input hidden): ").strip()
    if not token.startswith("xoxp-"):
        print("slackstatus: expected a user token starting with xoxp-", file=sys.stderr)
        return 2
    ok, detail = auth_test(token)
    if not ok:
        print(f"slackstatus: token rejected by Slack: {detail}", file=sys.stderr)
        return 1
    if existing:
        existing["token"] = token
    else:
        cfg["workspaces"].append({"name": name, "token": token})
    save_config(cfg)
    print(f"Saved {name!r} ({detail}) to {CONFIG_PATH}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slackstatus",
        description="Set your Slack status across multiple workspaces at once.",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    p_set = sub.add_parser("set", help="set a custom status")
    p_set.add_argument("text", help="status text")
    p_set.add_argument("emoji", nargs="?", default="", help="status emoji, e.g. :calendar:")
    when = p_set.add_mutually_exclusive_group()
    when.add_argument("--for", dest="duration", metavar="DURATION",
                      help="expire after a duration, e.g. 30m, 1h, 2h30m, 1d")
    when.add_argument("--until", metavar="HH:MM",
                      help="expire at the next occurrence of this local time")
    p_set.set_defaults(func=cmd_set)

    p_preset = sub.add_parser("preset", help="apply a preset from config.json")
    p_preset.add_argument("name")
    p_preset.set_defaults(func=cmd_preset)

    sub.add_parser("clear", help="clear your status").set_defaults(func=cmd_clear)
    sub.add_parser("list", help="show workspaces and check their tokens").set_defaults(func=cmd_list)
    sub.add_parser("add-workspace", help="add a workspace token").set_defaults(func=cmd_add_workspace)
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config()
        return args.func(args, cfg)
    except ConfigError as e:
        print(f"slackstatus: {e}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print(file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
