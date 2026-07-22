# WuxiaWorld Bot

A Playwright/Python bot that claims the free chapter-unlock reward on
[wuxiaworld.com](https://www.wuxiaworld.com) for two novels, and handles
daily mission rewards (Read 2 / 5 / 10 new chapters), optionally spending
keys to finish the 10-chapter mission when only a few chapters are needed.

Runs locally on demand, or on a schedule via GitHub Actions.

## What it does

1. Logs in (or reuses a saved session).
2. For each tracked book, checks the free-unlock cooldown widget:
   - **Ready** (0% or 100%) → expands every chapter accordion, collects the
     earliest locked chapters, and visits them directly to consume the free
     unlock.
   - **On cooldown** → logs the remaining time and unlock ETA, no action.
3. Checks daily mission progress (`Read 2/5/10 new chapters`), auto-claims
   any completed mission, and — if the 10-chapter mission needs 4 or fewer
   more chapters — spends keys to finish it rather than waiting.
4. Logs a summary of what was claimed vs. still on cooldown.

| Book    | Slug      | Free chapters / 23h |
|---------|-----------|----------------------|
| RMJI    | `rmji`    | 4                    |
| RMJIIR  | `rmjiir`  | 2                    |

## Setup (Windows, CMD)

```cmd
pip install playwright --break-system-packages
playwright install chromium

set WW_EMAIL=you@example.com
set WW_PASSWORD=yourpassword

python wuxiaworld_bot.py
```

Add `--force` to claim even while the cooldown widget shows it isn't ready
yet (useful for testing):

```cmd
python wuxiaworld_bot.v11.py --force
```

### Output files

| File                          | Purpose                                   |
|--------------------------------|--------------------------------------------|
| `wuxiaworld_state.json`        | Saved session (cookies/localStorage) — **never commit this** |
| `wuxiaworld_bot.log`           | Full run log                              |
| `error_YYYYMMDD_HHMMSS.png`    | Screenshot taken on an unhandled error    |

## Running on a schedule (GitHub Actions)

The workflow at `.github/workflows/wuxiaworld-bot.yml` runs the bot twice
daily — 12am and 12pm UTC — instead of waiting for the exact
23-hour unlock moment, trading a little precision for far fewer runs.

**One-time setup:**

1. In the repo: **Settings → Secrets and variables → Actions** → add
   `WW_EMAIL` and `WW_PASSWORD`.
2. Push the workflow file. It'll also appear under the **Actions** tab with
   a manual "Run workflow" button (`workflow_dispatch`) for on-demand runs.

**How it stays cheap and resilient:**

- Session state is cached between runs (`actions/cache`), so most runs skip
  logging in entirely.
- A run that's still on cooldown exits in seconds — no wasted compute.
- The bot reports back via a `GITHUB_OUTPUT` flag (`unlocked=true/false`)
  whether it actually claimed anything. The workflow only pushes a commit
  (`last_unlock.txt`, a timestamp marker — not the log) on runs where that's
  `true`. Since one of the two daily runs typically lands within the
  unlock window, this works out to roughly **one commit/day** — enough to
  keep GitHub from auto-disabling the schedule after 60 days of repo
  inactivity, without spamming the commit history.
- Logs and any error/debug screenshots are uploaded as a downloadable
  artifact on every run, success or failure.

## Known issues / not yet implemented

- Mission-progress parsing scrapes visible page text with a regex rather
  than a structured API — works against the current page markup, but isn't
  bulletproof against a layout change.
- Two books are processed sequentially in one browser tab rather than in
  parallel tabs; fine at the current scale, would need rework to speed up
  further.

> The daily login-streak reward (separate from the mission rewards above)
> doesn't need any explicit claiming logic — it's awarded just for visiting
> the site once a day, which every run already does as part of logging in
> and checking the book pages. No code needed.
