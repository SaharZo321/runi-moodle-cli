"""Typer + Rich CLI for Reichman University (RUNI) Moodle."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Callable, Optional

import httpx
import typer
from rich import box
from rich.console import Console
from rich.markup import escape as escape_markup
from rich.table import Table

from runi_moodle_cli import (
    __version__,
    assignments,
    auth,
    calendar,
    courses,
    deadlines,
    grades,
    messages,
    reading,
    search,
)
from runi_moodle_cli.client import MoodleError, get_site_info, sanitize_http_error
from runi_moodle_cli.config import (
    AuthError,
    DEFAULT_BASE_URL,
    config_path,
    current_academic_year,
    get_site,
    load_config,
    normalize_lang,
    remove_token,
    save_token,
    set_lang_override,
)
from runi_moodle_cli.formatting import sanitize_filename

app = typer.Typer(
    name="runi",
    help="Lightweight cross-platform CLI for Reichman University (RUNI) Moodle.",
    no_args_is_help=False,
    add_completion=False,
)
auth_app = typer.Typer(help="Sign in to Moodle and manage long-lived tokens.")
app.add_typer(auth_app, name="auth")

console = Console()
err_console = Console(stderr=True)


_TERM_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _clean_term(value: Any) -> str:
    return _TERM_CONTROL_RE.sub("", str(value))


def _esc(value: Any) -> str:
    return escape_markup(_clean_term(value))


def _table(title: str | None = None) -> Table:
    """Create a borderless table (no vertical box lines that misalign with Hebrew RTL fonts)."""
    return Table(title=_esc(title) if title is not None else None, box=box.SIMPLE_HEAD, pad_edge=False)


JsonOpt = Annotated[bool, typer.Option("--json", help="Emit JSON output.")]
PrettyOpt = Annotated[bool, typer.Option("--pretty", help="Pretty-print JSON output.")]
YearOpt = Annotated[Optional[int], typer.Option("--year", "-y", help="Academic year (e.g. 2027). Defaults to configured default.")]
LangOpt = Annotated[Optional[str], typer.Option("--lang", help="Moodle language: en (default), he, or auto (profile default).")]


@dataclass
class GlobalState:
    year: int | None = None
    lang: str | None = None
    as_json: bool = False
    pretty: bool = False


def _state(
    ctx: typer.Context,
    *,
    as_json: bool = False,
    pretty: bool = False,
    year: int | None = None,
    lang: str | None = None,
) -> GlobalState:
    if ctx.obj is None:
        ctx.obj = GlobalState()
    state: GlobalState = ctx.obj
    if as_json or pretty:
        state.as_json = True
    if pretty:
        state.pretty = True
    if year is not None:
        state.year = year
    if lang is not None:
        try:
            state.lang = normalize_lang(lang)
        except ValueError as exc:
            _fail(state, "usage", str(exc), 2)
    set_lang_override(state.lang)
    return state


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def _stdout_is_tty() -> bool:
    return sys.stdout.isatty()


def _wants_json(state: GlobalState, *, auto_json: bool = True) -> bool:
    return state.as_json or (auto_json and not _stdout_is_tty())


def _emit(
    state: GlobalState,
    data: Any,
    render_human: Callable[[Any], None],
    *,
    auto_json: bool = True,
) -> None:
    if _wants_json(state, auto_json=auto_json):
        indent = 2 if state.pretty else None
        sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=indent) + "\n")
    else:
        render_human(data)


def _fail(
    state: GlobalState,
    code: str,
    message: str,
    exit_code: int,
    *,
    auto_json: bool = True,
) -> None:
    if _wants_json(state, auto_json=auto_json):
        indent = 2 if state.pretty else None
        sys.stdout.write(
            json.dumps({"ok": False, "error": {"code": code, "message": message}, "exit_code": exit_code}, indent=indent)
            + "\n"
        )
    else:
        err_console.print(f"[red]Error:[/red] {_esc(message)}")
    raise typer.Exit(code=exit_code)


def _run(
    state: GlobalState,
    fn: Callable[[], Any],
    render_human: Callable[[Any], None],
    *,
    auto_json: bool = True,
) -> Any:
    try:
        data = fn()
    except AuthError as exc:
        _fail(state, "auth", str(exc), 3, auto_json=auto_json)
    except ValueError as exc:
        _fail(state, "usage", str(exc), 2, auto_json=auto_json)
    except MoodleError as exc:
        _fail(state, "moodle", str(exc), 4, auto_json=auto_json)
    except httpx.HTTPError as exc:
        _fail(state, "network", f"Could not reach Moodle: {sanitize_http_error(exc)}", 4, auto_json=auto_json)
    except OSError as exc:
        _fail(state, "io", str(exc), 1, auto_json=auto_json)
    except (KeyboardInterrupt, typer.Abort):
        _fail(state, "interrupted", "Cancelled.", 130, auto_json=auto_json)
    except Exception as exc:
        _fail(state, "error", str(exc), 1, auto_json=auto_json)
    _emit(state, data, render_human, auto_json=auto_json)
    return data


def _read_secret(prompt_text: str, *, strip_whitespace: bool = True) -> str:
    if not _stdin_is_tty():
        raw = sys.stdin.read()
        return raw.strip() if strip_whitespace else raw.rstrip("\r\n")
    val = typer.prompt(prompt_text, hide_input=True, err=True)
    return val.strip() if strip_whitespace else val


def _print_deadlines(items: list[dict]) -> None:
    if not items:
        console.print("[green]No upcoming deadlines.[/green]")
        return
    table = _table("Upcoming Deadlines")
    table.add_column("Due", style="cyan", no_wrap=True)
    table.add_column("Course", style="magenta")
    table.add_column("Item")
    table.add_column("Type", style="dim")
    table.add_column("Status")
    for item in items:
        status = "[red]OVERDUE[/red]" if item["overdue"] else _esc(item.get("action") or "")
        table.add_row(
            _esc(item["due"] or "-"),
            _esc(item["course"]),
            _esc(item["name"]),
            _esc(item["type"] or "-"),
            status,
        )
    console.print(table)


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    year: Optional[int] = typer.Option(None, "--year", "-y", help="Academic year (e.g. 2027). Defaults to configured default."),
    lang: LangOpt = None,
    as_json: bool = typer.Option(False, "--json", help="Emit JSON output (automatic when piped)."),
    pretty: bool = typer.Option(False, "--pretty", help="Pretty-print JSON output."),
    version: bool = typer.Option(False, "--version", "-V", help="Show version and exit."),
) -> None:
    if version:
        console.print(f"runi-moodle-cli {__version__}")
        raise typer.Exit()

    state = GlobalState(year=year, as_json=as_json or pretty, pretty=pretty)
    if lang is not None:
        try:
            state.lang = normalize_lang(lang)
        except ValueError as exc:
            _fail(state, "usage", str(exc), 2)
    set_lang_override(state.lang)
    ctx.obj = state

    if ctx.invoked_subcommand is None:
        _run(state, lambda: deadlines.upcoming_deadlines(14, False, state.year), _print_deadlines)


# --- auth ----------------------------------------------------------------------------------


@auth_app.command("login")
def auth_login(
    ctx: typer.Context,
    year: Optional[int] = typer.Option(None, "--year", "-y", help="Academic year (defaults to current academic year)."),
    credentials: bool = typer.Option(False, "--credentials", help="Sign in interactively with RUNI username and password (default)."),
    username: Optional[str] = typer.Option(None, "--username", "-u", help="RUNI username for credential login."),
    browser: bool = typer.Option(False, "--browser", help="Open a browser window (Playwright) to complete RUNI SSO."),
    cookie: bool = typer.Option(False, "--cookie", help="Exchange a pasted MoodleSession<year> cookie for a long-lived token."),
    token: bool = typer.Option(False, "--token", help="Save a Moodle mobile wstoken (or moodlemobile://token=... URL) directly."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help=f"Moodle base URL (defaults to configured URL or {DEFAULT_BASE_URL})."),
    no_default: bool = typer.Option(False, "--no-default", help="Do not make this year the default year."),
    lang: LangOpt = None,
    as_json: JsonOpt = False,
    pretty: PrettyOpt = False,
) -> None:
    """Sign in to Reichman Moodle and save a long-lived Mobile App token."""
    state = _state(ctx, as_json=as_json, pretty=pretty, lang=lang)
    target_year = year or state.year or current_academic_year()

    chosen = sum([credentials, browser, cookie, token])
    if chosen > 1:
        _fail(state, "usage", "Pass at most one of --credentials, --browser, --cookie, or --token.", 2)
    if chosen == 0:
        credentials = True
    if username is not None and not credentials:
        _fail(state, "usage", "--username can only be used with credential login.", 2)

    def do_login() -> dict:
        effective_base = base_url if base_url is not None else load_config().base_url
        if credentials:
            if username is not None:
                user = username.strip()
                pw = _read_secret("RUNI password", strip_whitespace=False)
            elif _stdin_is_tty():
                user = typer.prompt("RUNI username", err=True).strip()
                pw = typer.prompt("RUNI password", hide_input=True, err=True)
            else:
                lines = sys.stdin.read().splitlines()
                if len(lines) < 2:
                    raise AuthError(
                        "Expected username and password on stdin (two lines), "
                        "or pass --username, --cookie, or --token."
                    )
                user, pw = lines[0].strip(), lines[1]
            tok, info = auth.login_with_credentials(user, pw, year=target_year, base_url=effective_base)
            method = "credentials"
        elif browser:
            if not _wants_json(state):
                console.print(f"Opening browser to sign in to {_esc(effective_base.rstrip('/'))}/{target_year}...")
            tok, info = auth.login_with_browser(year=target_year, base_url=effective_base)
            method = "browser"
        elif cookie:
            raw_cookie = _read_secret(f"Paste your MoodleSession{target_year} cookie value")
            tok, info = auth.exchange_cookie_for_token(raw_cookie, year=target_year, base_url=effective_base)
            method = "cookie"
        else:
            raw_token = _read_secret(f"Paste your Moodle mobile token for {target_year}").strip()
            if not raw_token:
                raise AuthError("Empty Moodle token.")
            tok = auth.parse_mobile_redirect(raw_token) if raw_token.lower().startswith("moodlemobile://") else raw_token
            info = auth.verify_token(year=target_year, token=tok, base_url=effective_base)
            method = "token"

        saved_to = save_token(
            year=target_year,
            token=tok,
            base_url=effective_base,
            set_default=not no_default,
            lang=state.lang,
        )
        return {
            "ok": True,
            "method": method,
            "year": target_year,
            "site": f"{effective_base.rstrip('/')}/{target_year}",
            "userid": info.get("userid"),
            "fullname": info.get("fullname"),
            "username": info.get("username"),
            "config_path": str(saved_to),
        }

    def render(res: dict) -> None:
        console.print(
            f"[green]Signed in[/green] as [bold]{_esc(res.get('fullname') or res.get('username'))}[/bold] "
            f"(userid {res.get('userid')}) for [cyan]{_esc(res['site'])}[/cyan] via {res['method']}."
        )
        console.print(f"[dim]Saved long-lived mobile token to {_esc(res['config_path'])}[/dim]")

    _run(state, do_login, render)


@auth_app.command("status")
def auth_status(
    ctx: typer.Context,
    check: bool = typer.Option(True, "--check/--no-check", help="Verify stored tokens against Moodle."),
    lang: LangOpt = None,
    as_json: JsonOpt = False,
    pretty: PrettyOpt = False,
) -> None:
    """Show configured Moodle years and verify whether stored tokens are valid."""
    state = _state(ctx, as_json=as_json, pretty=pretty, lang=lang)

    def do_status() -> dict:
        cfg = load_config()
        years_info = []
        for y, tok in sorted(cfg.tokens.items()):
            entry: dict[str, Any] = {
                "year": y,
                "url": f"{cfg.base_url}/{y}",
                "default": y == cfg.default_year,
            }
            if check:
                try:
                    info = get_site_info(site=get_site(y))
                except httpx.HTTPError as exc:
                    entry["valid"] = False
                    entry["error"] = sanitize_http_error(exc)
                except (AuthError, MoodleError) as exc:
                    entry["valid"] = False
                    entry["error"] = str(exc)
                else:
                    entry["valid"] = True
                    entry["userid"] = info.get("userid")
                    entry["fullname"] = info.get("fullname")
            years_info.append(entry)
        return {
            "config_path": str(config_path()),
            "base_url": cfg.base_url,
            "default_year": cfg.default_year,
            "years": years_info,
        }

    def render(res: dict) -> None:
        if not res["years"]:
            console.print(
                f"[yellow]Not signed in.[/yellow] Run `runi auth login --year {current_academic_year()}`."
            )
            return
        table = _table(f"Moodle Auth ({res['config_path']})")
        table.add_column("Year", style="cyan")
        table.add_column("Site")
        table.add_column("Default")
        table.add_column("Status")
        table.add_column("User")
        for row in res["years"]:
            status = "-"
            if "valid" in row:
                status = "[green]valid[/green]" if row["valid"] else f"[red]invalid[/red] ({_esc(row.get('error', ''))})"
            table.add_row(
                str(row["year"]),
                _esc(row["url"]),
                "yes" if row["default"] else "",
                status,
                _esc(row.get("fullname") or "-"),
            )
        console.print(table)

    _run(state, do_status, render)


@auth_app.command("logout")
def auth_logout(
    ctx: typer.Context,
    year: Optional[int] = typer.Option(None, "--year", "-y", help="Remove only this year's token."),
    all_years: bool = typer.Option(False, "--all", help="Remove all stored tokens."),
    as_json: JsonOpt = False,
    pretty: PrettyOpt = False,
) -> None:
    """Remove stored Moodle tokens from local config."""
    state = _state(ctx, as_json=as_json, pretty=pretty)
    target = None if all_years else (year or state.year)

    def do_logout() -> dict:
        if not all_years and target is None:
            cfg = load_config()
            if not cfg.tokens:
                return {"ok": True, "removed": []}
            path = remove_token(None)
            return {"ok": True, "removed": sorted(cfg.tokens), "config_path": str(path)}
        path = remove_token(target)
        return {"ok": True, "removed": "all" if target is None else [target], "config_path": str(path)}

    def render(res: dict) -> None:
        console.print(f"[green]Removed stored Moodle token(s)[/green] ({res['removed']}).")
        console.print("[dim]To revoke the token on Moodle too, reset it under Preferences -> Security keys.[/dim]")

    _run(state, do_logout, render)


# --- Moodle commands -----------------------------------------------------------------------


@app.command("due")
def cmd_due(
    ctx: typer.Context,
    days: int = typer.Option(14, "--days", "-d", help="Days ahead to check."),
    overdue: bool = typer.Option(False, "--overdue", help="Include items past due in the last 30 days."),
    year: YearOpt = None,
    lang: LangOpt = None,
    as_json: JsonOpt = False,
    pretty: PrettyOpt = False,
) -> None:
    """Show upcoming actionable deadlines."""
    state = _state(ctx, as_json=as_json, pretty=pretty, year=year, lang=lang)
    _run(state, lambda: deadlines.upcoming_deadlines(days, overdue, state.year), _print_deadlines)


@app.command("courses")
def cmd_courses(
    ctx: typer.Context,
    semester: Optional[int] = typer.Option(None, "--semester", "-s", help="Filter by semester (1 or 2)."),
    year: YearOpt = None,
    lang: LangOpt = None,
    as_json: JsonOpt = False,
    pretty: PrettyOpt = False,
) -> None:
    """List your enrolled courses."""
    state = _state(ctx, as_json=as_json, pretty=pretty, year=year, lang=lang)

    def render(items: list[dict]) -> None:
        if not items:
            console.print("No courses found.")
            return
        table = _table("Courses")
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Number", style="dim", no_wrap=True)
        table.add_column("Sem", no_wrap=True)
        table.add_column("Name", style="bold")
        for c in items:
            table.add_row(
                str(c["id"]),
                str(c["course_number"]) if c["course_number"] else "-",
                str(c["semester"]) if c["semester"] else "-",
                _esc(c["name"]),
            )
        console.print(table)

    _run(state, lambda: courses.list_courses(semester, state.year), render)


@app.command("course")
def cmd_course(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Course ID, RUNI course number, or partial course name."),
    view: str = typer.Argument("contents", help="What to show: contents, staff, or grades."),
    year: YearOpt = None,
    lang: LangOpt = None,
    as_json: JsonOpt = False,
    pretty: PrettyOpt = False,
) -> None:
    """Inspect a single course (contents, staff, or grades)."""
    state = _state(ctx, as_json=as_json, pretty=pretty, year=year, lang=lang)
    mode = view.lower()
    if mode not in {"contents", "staff", "grades"}:
        _fail(state, "usage", f"view must be one of contents, staff, grades (got {view!r}).", 2)

    def fetch() -> dict:
        c = courses.resolve_course(query, state.year)
        if mode == "staff":
            return {"course": c, "staff": courses.get_course_staff(c["id"], state.year)}
        if mode == "grades":
            return {"course": c, "grades": grades.get_grades(c["id"], state.year)}
        return {"course": c, "sections": courses.get_course_contents(c["id"], state.year)}

    def render(data: dict) -> None:
        c = data["course"]
        console.print(f"[bold cyan]{_esc(c['name'])}[/bold cyan] [dim](id={c['id']})[/dim]")
        if mode == "staff":
            table = _table()
            table.add_column("Role", style="cyan")
            table.add_column("Name")
            for s in data["staff"]:
                table.add_row(_esc(s["role"]), _esc(s["name"]))
            console.print(table)
        elif mode == "grades":
            g = data["grades"]
            if g.get("total"):
                console.print(f"Course total: [bold]{_esc(g['total'])}[/bold]")
            table = _table()
            table.add_column("Item")
            table.add_column("Grade", style="green")
            table.add_column("Range", style="dim")
            table.add_column("Weight", style="dim")
            for item in g["items"]:
                table.add_row(
                    _esc(item["name"]),
                    _esc(item["grade"] or "-"),
                    _esc(item["range"] or "-"),
                    _esc(item["weight"] or "-"),
                )
            console.print(table)
        else:
            for sec in data["sections"]:
                console.print(f"\n[bold]{_esc(sec['section'])}[/bold]")
                for item in sec["items"]:
                    console.print(f"  \\[{_esc(item['type'])}] {_esc(item['name'])}")
                    for f in item.get("files", []):
                        console.print(f"    [dim]- {_esc(f['filename'])}: {_esc(f['url'])}[/dim]")
                    if item.get("link"):
                        console.print(f"    [dim]-> {_esc(item['link'])}[/dim]")

    _run(state, fetch, render)


@app.command("assignments")
def cmd_assignments(
    ctx: typer.Context,
    status: str = typer.Option("all", "--status", help="Filter: all, pending, or submitted."),
    overdue: bool = typer.Option(False, "--overdue", help="Include past-due assignments."),
    new_only: bool = typer.Option(False, "--new", help="Show assignments added or updated today."),
    year: YearOpt = None,
    lang: LangOpt = None,
    as_json: JsonOpt = False,
    pretty: PrettyOpt = False,
) -> None:
    """List assignments across all courses."""
    state = _state(ctx, as_json=as_json, pretty=pretty, year=year, lang=lang)
    if new_only:
        def render_new(res: dict) -> None:
            if res["first_run"]:
                console.print("[dim]Baseline saved. Future runs will report newly added assignments.[/dim]")
            if res["new"]:
                console.print("[bold green]New today:[/bold green]")
                for a in res["new"]:
                    console.print(f"  #{a['id']} {_esc(a['name'])} ({_esc(a['course'])}) due {_esc(a['due'] or '-')}")
            if res["updated"]:
                console.print("[bold yellow]Updated today:[/bold yellow]")
                for a in res["updated"]:
                    console.print(f"  #{a['id']} {_esc(a['name'])} ({_esc(a['course'])}) due {_esc(a['due'] or '-')}")
            if not res["new"] and not res["updated"]:
                console.print("No new or updated assignments today.")

        _run(state, lambda: assignments.new_assignments(state.year), render_new)
        return

    def render(items: list[dict]) -> None:
        if not items:
            console.print("No assignments found.")
            return
        table = _table("Assignments")
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Due", no_wrap=True)
        table.add_column("Course", style="magenta")
        table.add_column("Name")
        if status != "all":
            table.add_column("Status")
        for a in items:
            row = [str(a["id"]), _esc(a["due"] or "-"), _esc(a["course"]), _esc(a["name"])]
            if status != "all":
                row.append(_esc(a.get("status", "-")))
            table.add_row(*row)
        console.print(table)

    _run(state, lambda: assignments.list_assignments(status, overdue, state.year), render)


@app.command("assignment")
def cmd_assignment(
    ctx: typer.Context,
    assignment_id: int = typer.Argument(..., help="Assignment ID from `runi assignments`."),
    year: YearOpt = None,
    lang: LangOpt = None,
    as_json: JsonOpt = False,
    pretty: PrettyOpt = False,
) -> None:
    """Show full details, submission status, grade, and feedback for one assignment."""
    state = _state(ctx, as_json=as_json, pretty=pretty, year=year, lang=lang)

    def render(a: dict) -> None:
        console.print(f"[bold cyan]{_esc(a['name'])}[/bold cyan] ({_esc(a['course'])})")
        console.print(f"Due: {_esc(a['due'] or '-')}  |  Cutoff: {_esc(a['cutoff'] or '-')}")
        sub = a["submission"]
        console.print(
            f"Submission: [bold]{_esc(sub['status'])}[/bold] (modified: {_esc(sub['last_modified'] or '-')})"
        )
        if a["grade"]:
            console.print(f"Grade: [green]{_esc(a['grade'])}[/green]")
        if a["description"]:
            console.print()
            console.print(_clean_term(a["description"]), markup=False, highlight=False, emoji=False)
        if a["attachments"]:
            console.print("\n[bold]Attachments:[/bold]")
            for f in a["attachments"]:
                console.print(f"  - {_esc(f['filename'])}: {_esc(f['url'])}")
        if a["feedback_comments"]:
            console.print("\n[bold]Feedback:[/bold]")
            console.print(_clean_term(a["feedback_comments"]), markup=False, highlight=False, emoji=False)

    _run(state, lambda: assignments.get_assignment(assignment_id, state.year), render)


@app.command("grades")
def cmd_grades(
    ctx: typer.Context,
    course: Optional[str] = typer.Argument(None, help="Optional course ID, number, or partial name."),
    year: YearOpt = None,
    lang: LangOpt = None,
    as_json: JsonOpt = False,
    pretty: PrettyOpt = False,
) -> None:
    """Show overall grades across courses, or detailed grade items for one course."""
    state = _state(ctx, as_json=as_json, pretty=pretty, year=year, lang=lang)

    def fetch() -> dict:
        if course is None:
            return grades.get_grades(None, state.year)
        resolved = courses.resolve_course(course, state.year)
        data = grades.get_grades(resolved["id"], state.year)
        data["course"] = resolved["name"]
        return data

    def render(data: dict) -> None:
        if "courses" in data:
            table = _table("Course Grades")
            table.add_column("ID", style="cyan", no_wrap=True)
            table.add_column("Course")
            table.add_column("Grade", style="green")
            for c in data["courses"]:
                table.add_row(str(c["course_id"]), _esc(c["course"] or "-"), _esc(c["grade"] or "-"))
            console.print(table)
            return
        title = f"Grades: {data.get('course', data['course_id'])}"
        if data.get("total"):
            title += f" (Total: {data['total']})"
        table = _table(title)
        table.add_column("Item")
        table.add_column("Grade", style="green")
        table.add_column("Range", style="dim")
        table.add_column("Weight", style="dim")
        for item in data["items"]:
            table.add_row(
                _esc(item["name"]),
                _esc(item["grade"] or "-"),
                _esc(item["range"] or "-"),
                _esc(item["weight"] or "-"),
            )
        console.print(table)

    _run(state, fetch, render)


@app.command("messages")
def cmd_messages(
    ctx: typer.Context,
    days: int = typer.Option(7, "--days", "-d", help="How many days back to search."),
    course: Optional[str] = typer.Option(None, "--course", "-c", help="Limit to one course (ID, number, or name)."),
    year: YearOpt = None,
    lang: LangOpt = None,
    as_json: JsonOpt = False,
    pretty: PrettyOpt = False,
) -> None:
    """Show announcements, staff forum replies, and direct messages."""
    state = _state(ctx, as_json=as_json, pretty=pretty, year=year, lang=lang)

    def fetch() -> list[dict]:
        cid = courses.resolve_course(course, state.year)["id"] if course is not None else None
        return messages.staff_messages(days, cid, state.year)

    def render(items: list[dict]) -> None:
        if not items:
            console.print(f"No staff messages in the last {days} days.")
            return
        for m in items:
            header = f"[cyan]{_esc(m['time'] or '-')}[/cyan] \\[{_esc(m['kind'])}] [bold]{_esc(m.get('author') or 'Staff')}[/bold]"
            if m.get("course"):
                header += f" in [magenta]{_esc(m['course'])}[/magenta]"
            console.print(header)
            if m.get("subject"):
                console.print(f"  [bold]{_esc(m['subject'])}[/bold]")
            console.print(f"  {_clean_term(m['text'])}\n", markup=False, highlight=False, emoji=False)

    _run(state, fetch, render)


@app.command("calendar")
def cmd_calendar(
    ctx: typer.Context,
    days: int = typer.Option(30, "--days", "-d", help="Days ahead to show."),
    course: Optional[str] = typer.Option(None, "--course", "-c", help="Limit to one course (ID, number, or name)."),
    year: YearOpt = None,
    lang: LangOpt = None,
    as_json: JsonOpt = False,
    pretty: PrettyOpt = False,
) -> None:
    """Show upcoming Moodle calendar events."""
    state = _state(ctx, as_json=as_json, pretty=pretty, year=year, lang=lang)

    def fetch() -> list[dict]:
        cid = courses.resolve_course(course, state.year)["id"] if course is not None else None
        return calendar.calendar_events(days, cid, state.year)

    def render(items: list[dict]) -> None:
        if not items:
            console.print(f"No calendar events in the next {days} days.")
            return
        table = _table(f"Calendar (next {days} days)")
        table.add_column("Start", style="cyan", no_wrap=True)
        table.add_column("Course", style="magenta")
        table.add_column("Event")
        table.add_column("Type", style="dim")
        for e in items:
            table.add_row(_esc(e["start"] or "-"), _esc(e["course"] or "-"), _esc(e["name"]), _esc(e["type"]))
        console.print(table)

    _run(state, fetch, render)


@app.command("search")
def cmd_search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Search query (Hebrew or English)."),
    page: int = typer.Option(0, "--page", "-p", help="Result page number (0-based)."),
    year: YearOpt = None,
    lang: LangOpt = None,
    as_json: JsonOpt = False,
    pretty: PrettyOpt = False,
) -> None:
    """Search across all your Moodle courses."""
    state = _state(ctx, as_json=as_json, pretty=pretty, year=year, lang=lang)

    def render(res: dict) -> None:
        console.print(f"Found {res['total']} results (page {res['page']}):")
        for r in res["results"]:
            console.print(f"  [bold]{_esc(r['title'])}[/bold] [dim]({_esc(r['course'])} | {_esc(r['kind'])})[/dim]")
            if r["snippet"]:
                console.print(f"    {_clean_term(r['snippet'])}", markup=False, highlight=False, emoji=False)
            if r["url"]:
                console.print(f"    [dim]{_esc(r['url'])}[/dim]")

    _run(state, lambda: search.search(query, page, state.year), render)


@app.command("read")
def cmd_read(
    ctx: typer.Context,
    url: str = typer.Argument(..., help="Moodle file URL (pluginfile.php) or Page URL."),
    offset: int = typer.Option(0, "--offset", help="Character offset for long documents."),
    as_json: JsonOpt = False,
    pretty: PrettyOpt = False,
) -> None:
    """Extract and print text from a Moodle file (PDF, PPTX, DOCX, ipynb, HTML/text) or Page."""
    state = _state(ctx, as_json=as_json, pretty=pretty)

    def render(res: dict) -> None:
        if not _stdout_is_tty():
            sys.stdout.write(res["text"])
            if res["text"] and not res["text"].endswith("\n"):
                sys.stdout.write("\n")
            if "next_offset" in res:
                err_console.print(f"[dim]More text available: rerun with --offset {res['next_offset']}[/dim]")
            return
        console.print(f"[bold cyan]{_esc(res['name'])}[/bold cyan] [dim]({res['total_chars']} chars)[/dim]\n")
        console.print(_clean_term(res["text"]), markup=False, highlight=False, soft_wrap=True, emoji=False)
        if "next_offset" in res:
            console.print(f"\n[dim]More text available: rerun with --offset {res['next_offset']}[/dim]")

    _run(state, lambda: reading.read_content(url, offset), render, auto_json=False)


@app.command("download")
def cmd_download(
    ctx: typer.Context,
    target: str = typer.Argument(..., help="A Moodle file URL (pluginfile.php) OR a course (ID, number, or name)."),
    out: Optional[str] = typer.Option(None, "--out", "-o", help="Destination file or directory."),
    section: Optional[str] = typer.Option(None, "--section", help="When downloading a course, only sections matching this text."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite existing files instead of skipping."),
    year: YearOpt = None,
    lang: LangOpt = None,
    as_json: JsonOpt = False,
    pretty: PrettyOpt = False,
) -> None:
    """Download a single Moodle file URL or all files from a course."""
    state = _state(ctx, as_json=as_json, pretty=pretty, year=year, lang=lang)
    out_path = Path(out) if out is not None else None
    out_is_dir = bool(out and out.endswith(("/", "\\")))

    if target.lower().startswith(("http://", "https://")):
        def render_one(res: dict) -> None:
            verb = "Skipped (exists)" if res["skipped"] else "Downloaded"
            console.print(f"[green]{verb}:[/green] {_esc(res['path'])} ({res['bytes']} bytes)")

        _run(
            state,
            lambda: reading.download_file(target, dest=out_path, overwrite=overwrite, dest_is_dir=out_is_dir),
            render_one,
        )
        return

    def fetch_course() -> dict:
        c = courses.resolve_course(target, state.year)
        dest_dir = out_path or (Path.cwd() / sanitize_filename(c["name"], fallback=f"course_{c['id']}"))
        result = reading.download_course_files(
            c["id"],
            dest_dir=dest_dir,
            year=state.year,
            section_filter=section,
            overwrite=overwrite,
        )
        return {"course": c["name"], "dest": str(dest_dir), **result}

    def render_course(res: dict) -> None:
        console.print(f"[bold cyan]{_esc(res['course'])}[/bold cyan] -> {_esc(res['dest'])}")
        console.print(
            f"[green]Downloaded:[/green] {len(res['downloaded'])}  "
            f"[yellow]Skipped:[/yellow] {len(res['skipped'])}  "
            f"[red]Errors:[/red] {len(res['errors'])}"
        )
        for err in res["errors"]:
            err_console.print(f"  [red]Failed[/red] {_esc(err['filename'])}: {_esc(err['error'])}")

    result = _run(state, fetch_course, render_course)
    if result and result.get("errors"):
        raise typer.Exit(code=1)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    app()


if __name__ == "__main__":
    main()
