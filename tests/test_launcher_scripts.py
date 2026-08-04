"""Static validation of the development launcher scripts.

These tests never start, stop or signal a process. They read start_app.ps1 and
stop_app.ps1 as text, check they parse as PowerShell, and assert the safety
properties that make them safe to run on a machine that also hosts the separate
LinkedIn/Dice auto-apply project:

* nothing is ever selected for termination by process name
* port 8000 is only ever referenced as a port to protect, never to bind or kill
* the other project's database is not mentioned at all
* stopping works from recorded PID files, and stale ones are removed

A launcher is the one script most likely to be run half-asleep at the wrong
moment, so the blast radius is worth pinning down in tests rather than review.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
START_SCRIPT = REPO_ROOT / "start_app.ps1"
STOP_SCRIPT = REPO_ROOT / "stop_app.ps1"

BACKEND_PORT = "8100"
FRONTEND_PORT = "5173"
OTHER_PROJECT_PORT = "8000"
FORBIDDEN_DATABASE = "linkedin_auto_apply"


@pytest.fixture(scope="module")
def start_text() -> str:
    return START_SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def stop_text() -> str:
    return STOP_SCRIPT.read_text(encoding="utf-8")


def _strip_comments(script: str) -> str:
    """Drop comment blocks and line comments, leaving executable text.

    Several assertions below are about what the scripts *do*. Comments explain
    the guards in prose and would otherwise satisfy those assertions without
    any code backing them.
    """
    without_blocks = re.sub(r"<#.*?#>", "", script, flags=re.DOTALL)
    lines = []
    for line in without_blocks.splitlines():
        # Naive but adequate: no '#' appears inside a string literal in either
        # script, and a stray removal would only make the tests stricter.
        lines.append(line.split("#", 1)[0])
    return "\n".join(lines)


# --- The scripts exist and are valid PowerShell -----------------------------


def test_both_launcher_scripts_exist() -> None:
    assert START_SCRIPT.is_file()
    assert STOP_SCRIPT.is_file()


@pytest.mark.parametrize("script", [START_SCRIPT, STOP_SCRIPT], ids=lambda p: p.name)
def test_script_parses_as_powershell(script: Path) -> None:
    """Catch syntax errors without executing a single statement."""
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not available on this host.")

    # Parser::ParseFile only tokenises and builds an AST; nothing runs.
    command = (
        "$errors = $null; "
        "$null = [System.Management.Automation.Language.Parser]::ParseFile("
        f"'{script.as_posix()}', [ref]$null, [ref]$errors); "
        "if ($errors) { $errors | ForEach-Object { $_.ToString() }; exit 1 }; exit 0"
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"{script.name} has syntax errors:\n{result.stdout}"


@pytest.mark.parametrize("script", [START_SCRIPT, STOP_SCRIPT], ids=lambda p: p.name)
def test_script_has_comment_based_help(script: Path) -> None:
    text = script.read_text(encoding="utf-8")

    assert ".SYNOPSIS" in text
    assert ".DESCRIPTION" in text
    assert ".EXAMPLE" in text


# --- Isolation from the other project ---------------------------------------


@pytest.mark.parametrize("script", [START_SCRIPT, STOP_SCRIPT], ids=lambda p: p.name)
def test_the_other_projects_database_is_never_mentioned(script: Path) -> None:
    assert FORBIDDEN_DATABASE not in script.read_text(encoding="utf-8").lower()


def test_start_binds_the_project_ports_only(start_text: str) -> None:
    code = _strip_comments(start_text)

    assert f"$BackendPort   = {BACKEND_PORT}" in code
    assert f"$FrontendPort  = {FRONTEND_PORT}" in code
    # uvicorn is told a variable, not a literal, and the variable is checked.
    assert '"$BackendPort"' in code


def test_port_8000_appears_only_as_a_protected_port(start_text: str, stop_text: str) -> None:
    """The other project's port may be named, but only to be defended."""
    for name, script in (("start_app.ps1", start_text), ("stop_app.ps1", stop_text)):
        code = _strip_comments(script)

        for line in code.splitlines():
            if OTHER_PROJECT_PORT not in line:
                continue
            # The single permitted use: defining the constant that guards it.
            assert re.search(
                rf"\$ForbiddenPort\s*=\s*{OTHER_PROJECT_PORT}\b", line
            ), f"{name} references port {OTHER_PROJECT_PORT} outside the guard: {line.strip()}"


def test_start_refuses_to_run_if_configured_onto_the_other_projects_port(
    start_text: str,
) -> None:
    code = _strip_comments(start_text)

    assert "$configuredPort -eq $ForbiddenPort" in code
    assert "Stop-WithError" in code


def test_stop_protects_the_other_projects_port(stop_text: str) -> None:
    code = _strip_comments(stop_text)

    # Whatever a PID file claims, a listener on that port is off limits.
    assert "Get-NetTCPConnection -LocalPort $ForbiddenPort" in code
    assert "$protected.Contains" in code


# --- Nothing is ever killed by name -----------------------------------------


BY_NAME_TERMINATION = (
    r"Stop-Process\s+(-\w+\s+)*-Name",
    r"Stop-Process\s+.*-ProcessName",
    r"Get-Process\s+-Name\s+\w+\s*\|\s*Stop-Process",
    r"taskkill.*\/IM",
    r"Stop-Process.*node",
    r"Stop-Process.*python",
)


@pytest.mark.parametrize("pattern", BY_NAME_TERMINATION)
def test_no_script_terminates_processes_by_name(
    pattern: str, start_text: str, stop_text: str
) -> None:
    """`Stop-Process -Name python` would kill the other project outright."""
    for name, script in (("start_app.ps1", start_text), ("stop_app.ps1", stop_text)):
        match = re.search(pattern, _strip_comments(script), flags=re.IGNORECASE)
        assert match is None, f"{name} may terminate by name: {match.group(0)!r}"


def test_start_never_terminates_anything(start_text: str) -> None:
    """Starting is not stopping. Shutdown belongs to one script only."""
    code = _strip_comments(start_text)

    assert "Stop-Process" not in code
    assert "taskkill" not in code.lower()


def test_stop_only_terminates_by_id(stop_text: str) -> None:
    code = _strip_comments(stop_text)

    calls = re.findall(r"Stop-Process[^\r\n]*", code)
    assert calls, "stop_app.ps1 does not stop anything."

    for call in calls:
        assert "-Id " in call, f"Stop-Process without an explicit -Id: {call.strip()}"


# --- PID tracking -----------------------------------------------------------


def test_start_records_a_pid_file_per_service(start_text: str) -> None:
    code = _strip_comments(start_text)

    assert "$RunDir" in code
    assert "'.run'" in code
    assert '"$Service.pid"' in code
    # A bare PID is not proof of identity; the start time is what makes it one.
    assert "startTimeUtc" in code
    assert "processName" in code


def test_stop_verifies_identity_before_terminating(stop_text: str) -> None:
    code = _strip_comments(stop_text)

    assert "$process.ProcessName -ne $Record.processName" in code
    assert "startTimeUtc" in code
    # A recycled PID must fail the check rather than be killed.
    assert "TotalSeconds) -gt 2" in code


def test_stop_removes_stale_pid_files(stop_text: str) -> None:
    code = _strip_comments(stop_text)

    assert "Remove-Item -LiteralPath $pidFile -Force" in code
    assert "stale PID file" in stop_text


def test_stop_reports_each_services_outcome(stop_text: str) -> None:
    for phrase in ("not running (no PID file)", "stale PID file", "stopped on port"):
        assert phrase in stop_text


def test_stop_limits_child_termination_to_processes_started_after_the_parent(
    stop_text: str,
) -> None:
    code = _strip_comments(stop_text)

    assert "Get-TrackedDescendants" in code
    assert "ParentStartUtc" in code
    assert "$created -lt $ParentStartUtc" in code


# --- Duplicate-start protection ---------------------------------------------


def test_start_checks_both_ports_before_starting_anything(start_text: str) -> None:
    code = _strip_comments(start_text)

    assert "Test-PortListening -Port $BackendPort" in code
    assert "Test-PortListening -Port $FrontendPort" in code
    assert "Get-TrackedProcess -Service 'backend'" in code
    assert "Get-TrackedProcess -Service 'frontend'" in code


def test_start_waits_for_both_services(start_text: str) -> None:
    code = _strip_comments(start_text)

    assert "$HealthUrl" in code
    assert "/api/v1/health" in code
    assert "$FrontendUrl" in code
    assert "$TimeoutSeconds" in code


def test_start_opens_the_dashboard_unless_told_not_to(start_text: str) -> None:
    code = _strip_comments(start_text)

    assert "[switch]$NoBrowser" in code
    assert "if (-not $NoBrowser)" in code
    assert "Start-Process $FrontendUrl" in code


# --- Prerequisite reporting -------------------------------------------------


@pytest.mark.parametrize(
    ("requirement", "expected_hint"),
    [
        (".venv", "python -m venv .venv"),
        (".env", "Copy-Item .env.example .env"),
        ("node/npm", "https://nodejs.org/"),
        ("node_modules", "npm install"),
        ("postgres", "docker compose up -d"),
        ("frontend env", "frontend\\.env.example"),
    ],
)
def test_start_explains_how_to_fix_each_missing_prerequisite(
    requirement: str, expected_hint: str, start_text: str
) -> None:
    assert expected_hint in start_text, f"no actionable fix shown for {requirement}"


def test_start_verifies_postgres_before_launching(start_text: str) -> None:
    code = _strip_comments(start_text)

    assert "Test-TcpReachable -TargetHost $pgHost -Port $pgPort" in code
    # And confirms afterwards that the app can actually reach it.
    assert "$dbCheck" in code


# --- Repository hygiene -----------------------------------------------------


def test_run_directory_is_git_ignored() -> None:
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert ".run/" in ignored


def test_launcher_adds_no_auto_apply_concepts(start_text: str, stop_text: str) -> None:
    """CLAUDE.md section 18: this product never applies to a job."""
    banned = ("easyapply", "easy_apply", "autosubmit", "auto_submit", "auto-apply")

    for name, script in (("start_app.ps1", start_text), ("stop_app.ps1", stop_text)):
        collapsed = script.lower().replace(" ", "")
        for term in banned:
            # "auto-apply project" is the isolation note, not a feature.
            occurrences = collapsed.count(term)
            allowed = collapsed.count(f"{term}project")
            assert occurrences == allowed, f"{name} mentions {term!r}"
