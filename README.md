# runi-moodle-cli (`runi`)

A lightweight, cross-platform CLI (Windows, macOS, Linux) for Reichman University (RUNI) Moodle.

Uses the same long-lived **Moodle Mobile App token (`wstoken`)** as the official Moodle mobile app, so you only sign in once per academic year instead of re-authenticating every 30 minutes.

Built on the Moodle API modules from [shanikn/Moodle_MCP](https://github.com/shanikn/Moodle_MCP) (MIT License).

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/) (or `pipx` / `pip`):

```bash
# Lightweight install (Windows, macOS, Linux)
uv tool install .

# With optional Playwright browser SSO login (--browser)
uv tool install ".[browser]"
playwright install chromium
```

Both `runi-moodle-cli` and the short alias `runi` are installed.

## Sign In

Reichman University hosts each academic year as its own Moodle site (`https://moodle.runi.ac.il/<year>`). You can sign in using whichever method fits your machine:

### Option 1: Interactive RUNI SSO login (default, no browser needed)
```bash
runi auth login --year 2027
```
Prompts for your RUNI username and password in the terminal, completes RUNI SAML2 SSO over HTTPS, and saves only the resulting **long-lived Mobile App token** (your password is never stored). Works on Windows, macOS, and headless Linux. If your account requires MFA or an extra verification step, use `--cookie` or `--browser` instead.

### Option 2: Paste cookie once (if your account requires MFA)
1. Log in to `https://moodle.runi.ac.il/2027` in your browser.
2. Open DevTools (`F12`) -> Application (or Storage) -> Cookies -> `https://moodle.runi.ac.il` and copy `MoodleSession2027`.
3. Run:
```bash
runi auth login --year 2027 --cookie
```
The CLI exchanges that short-lived cookie for a **long-lived Mobile App token** automatically and saves it.

### Option 3: Browser SSO (desktop with Playwright)
```bash
runi auth login --year 2027 --browser
```
Opens a Chromium window for RUNI SSO (supports MFA) and captures the long-lived token automatically.

### Option 4: Direct mobile token
```bash
runi auth login --year 2027 --token
```
Accepts either a raw `wstoken` or a `moodlemobile://token=...` redirect URL.

Check your saved login anytime:
```bash
runi auth status
```

## Usage

Tables are shown in an interactive terminal; clean JSON is emitted automatically when piped (or with `--json` / `--pretty`, either before or after a subcommand). `runi read` is the one exception: it outputs plain extracted text even when piped, and emits JSON only when `--json` or `--pretty` is explicitly passed.

```bash
# Quick dashboard (upcoming deadlines in the next 14 days)
runi

# Deadlines & calendar
runi due --days 14 --overdue
runi calendar --days 30

# Courses (accepts course ID, RUNI course number, or partial name)
runi courses
runi course "Data Structures" contents
runi course 592 staff
runi course 592 grades

# Assignments & grades
runi assignments --status pending
runi assignment 12345
runi grades
runi grades "Computer Networks"

# Staff announcements, forum replies, direct messages & search
runi messages --days 7
runi search "exam"

# Read text from PDFs/slides/docs/notebooks or download files
runi read "https://moodle.runi.ac.il/2027/pluginfile.php/..."
runi download "https://moodle.runi.ac.il/2027/pluginfile.php/..." --out ./slides.pdf
runi download "Data Structures" --out ./ds-files --section "Week 1"

# Query another academic year or switch Moodle language (en, he, auto)
runi --year 2026 courses
runi courses --lang he
```

## Notes

- **Network:** Reichman's Moodle (`moodle.runi.ac.il`) requires an Israeli IP address.
- **Language:** Defaults to `en` (English) for consistent course names and labels. Override with `--lang en|he|auto`, `MOODLE_LANG`, or `lang` in `config.json`.
- **Config location:** Stored in your OS config folder via `platformdirs` (`~/.config/runi-moodle-cli/config.json` on Linux, `~/Library/Application Support/runi-moodle-cli/config.json` on macOS, `%APPDATA%\runi-moodle-cli\config.json` on Windows). Override with `RUNI_MOODLE_CONFIG`.
- **Environment variables:** `MOODLE_BASE_URL`, `MOODLE_DEFAULT_YEAR`, `MOODLE_LANG`, and `MOODLE_TOKEN_<year>` are also supported as overrides.
