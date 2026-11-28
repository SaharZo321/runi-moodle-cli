# moodle-cli (`mdl` / `moodle-cli`)

A lightweight, cross-platform CLI (Windows, macOS, Linux) for Moodle, with first-class support for Reichman University (RUNI) and any other university Moodle that enables the Moodle Mobile App web service.

Uses the same long-lived **Moodle Mobile App token (`wstoken`)** as the official Moodle mobile app, so you only sign in once per academic year instead of re-authenticating every 30 minutes.

Built on the Moodle API modules from [shanikn/Moodle_MCP](https://github.com/shanikn/Moodle_MCP) (MIT License).

## Install

### Standalone binary (no Python required)
Download the binary for your OS from the [latest GitHub Release](https://github.com/SaharZo321/moodle-cli/releases/latest), plus `SHA256SUMS.txt` to verify it. Browser SSO (`--browser`) works out of the box with your installed Chrome, Brave, or Edge.

| OS | File |
| --- | --- |
| Windows (x64) | `mdl-windows-x64.exe` |
| macOS (Apple Silicon) | `mdl-macos-arm64` |
| Linux (x64) | `mdl-linux-x64` |

On Intel Macs, or other architectures, install with Python / `uv` below.

#### Windows
```powershell
# Verify the checksum (compare with the line for mdl-windows-x64.exe in SHA256SUMS.txt)
Get-FileHash .\mdl-windows-x64.exe -Algorithm SHA256

# Optional: rename to mdl.exe and move it to a folder on your PATH
Rename-Item .\mdl-windows-x64.exe mdl.exe
.\mdl.exe --version
```
The binary is unsigned, so Windows SmartScreen may warn on first run: choose **More info** then **Run anyway**.

#### macOS
```bash
# Verify the checksum
shasum -a 256 -c SHA256SUMS.txt --ignore-missing

# Mark executable and clear the Gatekeeper quarantine flag (the binary is unsigned)
chmod +x ./mdl-macos-arm64
xattr -d com.apple.quarantine ./mdl-macos-arm64

# Optional: install on your PATH as `mdl`
sudo mv ./mdl-macos-arm64 /usr/local/bin/mdl
mdl --version
```

#### Linux
```bash
sha256sum -c SHA256SUMS.txt --ignore-missing
chmod +x ./mdl-linux-x64
sudo mv ./mdl-linux-x64 /usr/local/bin/mdl
mdl --version
```

### With Python / `uv`
Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/) (or `pipx` / `pip`):

```bash
# Install directly from GitHub (includes browser SSO via your existing Chrome, Brave, or Edge)
uv tool install git+https://github.com/SaharZo321/moodle-cli.git

# Or clone and install locally
git clone https://github.com/SaharZo321/moodle-cli.git
cd moodle-cli
uv tool install .

# Or run once without installing (works the same on Windows, macOS, and Linux)
uvx --from git+https://github.com/SaharZo321/moodle-cli.git mdl --version
```

Installs `mdl` and `moodle-cli` (with `runi` and `runi-moodle-cli` kept as backward-compatible aliases).

The `[browser]` extra is no longer needed (browser SSO is built in); it is kept only so older install commands keep working.

## Sign In

### Reichman University (RUNI)
Reichman hosts each academic year as its own Moodle site (`https://moodle.runi.ac.il/<year>`). You can sign in using whichever method fits your machine:

#### Option 1: Interactive RUNI SSO login (default, no browser needed)
```bash
mdl auth login --year 2027
```
Prompts for your RUNI username and password in the terminal, completes RUNI SAML2 SSO over HTTPS, and saves only the resulting **long-lived Mobile App token** (your password is never stored). Works on Windows, macOS, and headless Linux. If your account requires MFA or an extra verification step, use `--cookie` or `--browser` instead.

#### Option 2: Paste cookie once (if your account requires MFA)
1. Log in to `https://moodle.runi.ac.il/2027` in your browser.
2. Open DevTools (`F12`) -> Application (or Storage) -> Cookies -> `https://moodle.runi.ac.il` and copy `MoodleSession2027`.
3. Run:
```bash
mdl auth login --year 2027 --cookie
```
The CLI exchanges that short-lived cookie for a **long-lived Mobile App token** automatically and saves it.

#### Option 3: Browser SSO (Chrome, Brave, or Edge)
```bash
mdl auth login --year 2027 --browser
```
Opens an isolated temporary window using your installed Chrome, Brave, or Edge (no extra browser download needed; override with `MOODLE_BROWSER_PATH`, e.g. `C:\Program Files\Google\Chrome\Application\chrome.exe` on Windows or `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome` on macOS), supports MFA, and captures the long-lived token automatically. Works in both `uv` installs and standalone binaries.

#### Option 4: Direct mobile token
```bash
mdl auth login --year 2027 --token
```
Accepts either a raw `wstoken` or a `moodlemobile://token=...` redirect URL.

### Other Universities (Any Moodle Site)
Any university whose Moodle enables the Moodle Mobile App web service (`moodle_mobile_app`) is supported via `--site <https://moodle.your-uni.edu>`:

```bash
# Direct username/password (for Moodles that support /login/token.php)
mdl auth login --site https://moodle.uni.edu

# Browser SSO (for Moodles behind Azure AD / Shibboleth / CAS / MFA)
mdl auth login --site https://moodle.uni.edu --browser

# Session cookie exchange (headless / SSH; paste MoodleSession<suffix>=<value> if your cookie has a suffix)
mdl auth login --site https://moodle.uni.edu --cookie

# Direct mobile token
mdl auth login --site https://moodle.uni.edu --token
```

### Managing Sessions
```bash
# Check saved logins and verify token validity
mdl auth status

# Remove one year's token, or remove all saved tokens (default when no --year is given)
mdl auth logout --year 2027
mdl auth logout
```

> **Security:** Your saved `wstoken` grants full Moodle access like a password and is stored with `0600` permissions on POSIX systems. To revoke it on Moodle's side as well, reset the Moodle mobile web service key under **Preferences -> Security keys**.

## Usage

Tables are shown in an interactive terminal; clean JSON is emitted automatically when piped (or with `--json` / `--pretty`, either before or after a subcommand). `mdl read` is the one exception: it outputs plain extracted text even when piped, and emits JSON only when `--json` or `--pretty` is explicitly passed.

```bash
# Quick dashboard (upcoming deadlines in the next 14 days)
mdl

# Deadlines & calendar
mdl due --days 14 --overdue
mdl calendar --days 30

# Courses (accepts course ID, RUNI course number, or partial name)
mdl courses
mdl course "Data Structures" contents
mdl course 592 staff
mdl course 592 grades

# Assignments & grades
mdl assignments --status pending
mdl assignment 12345
mdl submit 12345 --file ./hw1.pdf --dry-run
mdl submit 12345 --file ./hw1.pdf --draft
mdl submit 12345 --file ./hw1.pdf --accept-statement
mdl grades
mdl grades "Computer Networks"

# Staff announcements, forum replies, direct messages & search
mdl messages --days 7
mdl search "exam"

# Read text from PDFs/slides/docs/notebooks or download files
mdl read "https://moodle.runi.ac.il/2027/pluginfile.php/..."
mdl download "https://moodle.runi.ac.il/2027/pluginfile.php/..." --out ./slides.pdf
mdl download "Data Structures" --out ./ds-files --section "Week 1"

# Query another academic year or switch Moodle language (en, he, ar, de, auto, ...)
mdl --year 2026 courses
mdl courses --lang he
```

## Notes

- **Network:** Reichman's Moodle (`moodle.runi.ac.il`) requires an Israeli IP address.
- **Language:** Defaults to `en` (English) for consistent course names and labels. Override with `--lang <code|auto>` (e.g. `en`, `he`, `ar`, `de`, `es`, `auto`), `MOODLE_LANG`, or `lang` in `config.json`.
- **Config location:** Stored in your OS config folder via `platformdirs`:
  - Linux: `~/.config/moodle-cli/config.json`
  - macOS: `~/Library/Application Support/moodle-cli/config.json`
  - Windows: `%LOCALAPPDATA%\moodle-cli\moodle-cli\config.json`
  - Override with `MOODLE_CLI_CONFIG` (and `MOODLE_CLI_DATA_DIR` for assignment state). To use multiple universities simultaneously, point `MOODLE_CLI_CONFIG` to a separate file per university.
- **Environment variables:**
  - `MOODLE_SITE_URL`: exact full Moodle URL without a `/{year}` suffix (e.g. `https://moodle.uni.edu`). Also persisted when running `mdl auth login`.
  - `MOODLE_BASE_URL`: base URL for year-based Moodles (`<base>/<year>`, e.g. `https://moodle.runi.ac.il`). Also persisted when running `mdl auth login`.
  - `MOODLE_DEFAULT_YEAR`, `MOODLE_LANG`, and `MOODLE_TOKEN_<year>`.
- **Upgrading from v1.0 (`runi-moodle-cli`):** v1.1.0 renamed the package and config directory to `moodle-cli` (keeping `runi` and `runi-moodle-cli` as CLI aliases). Sign in again with `mdl auth login` (or copy `~/.config/runi-moodle-cli/config.json` to `~/.config/moodle-cli/config.json`), and update any `RUNI_MOODLE_CONFIG` / `RUNI_MOODLE_DATA_DIR` env vars to `MOODLE_CLI_CONFIG` / `MOODLE_CLI_DATA_DIR`.
