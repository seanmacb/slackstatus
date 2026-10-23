# slackstatus

Set your Slack status in several workspaces at once from the command line.

```
$ slackstatus set "In a meeting" :calendar: --for 1h
Setting "In a meeting" :calendar: (until Fri 14:30) on 3 workspace(s)
work      ok
oss       ok
client    error: missing_scope
```

Single Python file, Python 3.9+, standard library only.

## Install

```bash
chmod +x slackstatus.py
mkdir -p ~/.local/bin
ln -s "$PWD/slackstatus.py" ~/.local/bin/slackstatus
```

Make sure `~/.local/bin` is on your `PATH` (for zsh, add `export PATH="$HOME/.local/bin:$PATH"` to `~/.zshrc`).

## Create a Slack app (once per workspace)

slackstatus uses a **user** token (`xoxp-...`), so the status is set on your own profile.

A Slack app belongs to one workspace. The simplest setup is to create a small private app in each workspace you want to control:

1. Go to <https://api.slack.com/apps> and click **Create New App** → **From scratch**. Give it a name (e.g. `slackstatus`) and pick the workspace.
2. Open **OAuth & Permissions**. Under **Scopes → User Token Scopes** (not *Bot Token Scopes*), add:
   - `users.profile:write`: set your status
   - `users.profile:read`: read your profile
3. Leave **token rotation** off. Rotated tokens expire after 12 hours and slackstatus does not refresh them.
4. At the top of **OAuth & Permissions**, click **Install to Workspace** and approve.
5. Copy the **User OAuth Token** (starts with `xoxp-`).
6. Run `slackstatus add-workspace`, enter a short name for the workspace, and paste the token. The input is hidden, and the token is checked with `auth.test` before it is saved.

Repeat for each workspace.

Instead of steps 1–3 you can choose **Create New App → From an app manifest** and paste this:

```yaml
display_information:
  name: slackstatus
oauth_config:
  scopes:
    user:
      - users.profile:read
      - users.profile:write
settings:
  token_rotation_enabled: false
```

> **Admin approval:** Some workspaces only let admins approve new apps. In that case you'll see **Request to Install** instead of **Install to Workspace**. The token is only available after an admin approves the request. Enterprise Grid orgs may also block user-installed apps entirely.

If you add scopes later, reinstall the app and copy the new token. Until you do, Slack returns `missing_scope`.

## Usage

```bash
slackstatus set "In a meeting" :calendar: --for 1h     # expires in 1 hour
slackstatus set "Heads down" :headphones: --until 17:00 # expires at the next 17:00 local time
slackstatus set "Commuting" :bus:                       # no expiry
slackstatus preset lunch                                # apply a preset from config.json
slackstatus clear                                       # clear status text and emoji
slackstatus list                                        # check each workspace's token (auth.test)
slackstatus add-workspace                               # add or replace a workspace token
```

- `--for` takes durations like `30m`, `1h`, `2h30m`, `1d`, `1d2h`. Units go in d/h/m order.
- `--until` takes `HH:MM` (24-hour, local time). If that time has already passed today, the next day's is used.

All workspaces are updated in parallel. Each one prints a line with `ok` or the Slack error code (`invalid_auth`, `missing_scope`, `ratelimited`, `token_revoked`, ...). A failure in one workspace doesn't stop the others. If Slack answers HTTP 429, slackstatus waits for the `Retry-After` delay and retries once.

Exit codes: `0` if every workspace succeeded, `1` if any failed, `2` for usage or config errors.

## Configuration

`~/.config/slackstatus/config.json` is created from a template on first run, with permissions `600`:

```json
{
  "workspaces": [
    {"name": "work", "token": "xoxp-..."},
    {"name": "oss",  "token": "xoxp-..."}
  ],
  "presets": {
    "focus":   {"text": "Focusing",      "emoji": ":headphones:",     "duration": "2h"},
    "meeting": {"text": "In a meeting",  "emoji": ":calendar:",       "duration": "1h"},
    "lunch":   {"text": "Out for lunch", "emoji": ":fork_and_knife:", "duration": "1h"},
    "ooo":     {"text": "Out of office", "emoji": ":palm_tree:"}
  }
}
```

A preset's `duration` is optional and uses the same format as `--for`. Leave it out for a status that doesn't expire. Edit or add presets freely.

Tokens are stored in plain text in this file, so keep it `600`. slackstatus never prints tokens.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
