"""``GH_TOKEN`` reaches ``gh`` from ``.env``, read as data (#92; gregoryfoster/skills#576).

Ported from CannObserv/processor#115 (``tests/test_gh_env.py`` at ``9ff80046``), a
local replica of upstream's planned ``using-github-cli`` (gregoryfoster/skills#334)
until it ships, then replaced by it. Its consumer is the agent's Bash calls, through
``.claude/hooks/gh-env.sh`` and ``$CLAUDE_ENV_FILE``.

Not the refresh hook. Its registration once read the token itself and scoped
``gh auth git-credential`` to it through ``GIT_CONFIG_COUNT``. Since
gregoryfoster/skills#576 the hook reads ``.env`` as data and hands the token to ``gh``
and its https push per call, never exported, so the registration is
``install-refresh.sh``'s own again; the tests below hold it there.

``.env`` is never executed (``set -a; . .env`` would run it), the token is never
printed, and the hook always exits 0.
"""

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ENV_VALUE = ROOT / "scripts" / "env-value.sh"
GH_ENV = ROOT / ".claude" / "hooks" / "gh-env.sh"
SETTINGS = ROOT / ".claude" / "settings.json"
REFRESH_HOOK = Path(".claude") / "hooks" / "skills-submodule-update.sh"
# Shell metacharacters, both quotes and a command substitution: %q must carry all.
NASTY = 'gh\'p_"a b" $(touch pwned) ;x`y`'
# git, isolated from this host's global and system config.
HERMETIC_GIT = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _value(env: Path, name: str = "GH_TOKEN") -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(ENV_VALUE), name, str(env)], capture_output=True, text=True, cwd=env.parent
    )


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("GH_TOKEN=abc\n", "abc"),
        ("export GH_TOKEN=abc\n", "abc"),
        ('GH_TOKEN="a b"\n', "a b"),
        ("GH_TOKEN='a b'\n", "a b"),
        ("GH_TOKEN=abc\r\n", "abc"),
        ("GH_TOKEN=old\nGH_TOKEN=new\n", "new"),
        ("GH_TOKEN_BROKER=no\nGH_TOKEN=yes\nGH_TOKEN_X=no\n", "yes"),
        ("# GH_TOKEN=commented\nGH_TOKEN=live\n", "live"),
    ],
)
def test_env_value_reads_one_name(tmp_path: Path, text: str, want: str) -> None:
    env = tmp_path / ".env"
    env.write_text(text)
    out = _value(env)
    assert (out.returncode, out.stdout) == (0, want), out.stderr


def test_env_value_never_executes_the_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("touch executed\nGH_TOKEN=$(touch substituted)\n")
    out = _value(env)
    assert out.stdout == "$(touch substituted)"
    assert not (tmp_path / "executed").exists()
    assert not (tmp_path / "substituted").exists()


@pytest.mark.parametrize("text", [None, "OTHER=1\n", "GH_TOKEN_BROKER=x\n"])
def test_env_value_without_the_name_prints_nothing(tmp_path: Path, text: str | None) -> None:
    env = tmp_path / ".env"
    if text is not None:
        env.write_text(text)
    out = _value(env)
    assert (out.returncode, out.stdout) == (1, "")


def _project(tmp_path: Path, env_text: str | None) -> Path:
    """A git checkout holding the two scripts and, optionally, an ``.env``."""
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for src in (ENV_VALUE, GH_ENV):
        dst = repo / src.relative_to(ROOT)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    if env_text is not None:
        (repo / ".env").write_text(env_text)
    return repo


def _fake_gh(tmp_path: Path, version: str) -> str:
    bin_dir = tmp_path / f"bin-{version}"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(f'#!/bin/sh\necho "gh version {version} (2026-01-01)"\n')
    gh.chmod(0o755)
    return f"{bin_dir}{os.pathsep}{os.environ['PATH']}"


def _hook(
    project: Path, tmp_path: Path, *, env_file: bool = True, extra: dict | None = None
) -> tuple[subprocess.CompletedProcess, Path]:
    target = tmp_path / "claude-env"
    env = {k: v for k, v in os.environ.items() if k != "GH_TOKEN"}
    env |= {"CLAUDE_PROJECT_DIR": str(project), "PATH": _fake_gh(tmp_path, "2.101.0")}
    if env_file:
        env["CLAUDE_ENV_FILE"] = str(target)
    else:
        env.pop("CLAUDE_ENV_FILE", None)
    env |= extra or {}
    out = subprocess.run(
        ["bash", str(project / GH_ENV.relative_to(ROOT))],
        capture_output=True,
        text=True,
        env=env,
        cwd=project,
    )
    return out, target


def _exported(target: Path) -> str:
    """What a shell that sources the env file sees, as the harness does."""
    script = f'. {shlex.quote(str(target))}; printf %s "$GH_TOKEN"'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout


def test_hook_exports_the_token_exactly_and_never_prints_it(tmp_path: Path) -> None:
    project = _project(tmp_path, f"GH_TOKEN={NASTY}\n")
    out, target = _hook(project, tmp_path)
    assert out.returncode == 0, out.stderr
    assert _exported(target) == NASTY
    assert "gh'p_" not in out.stdout + out.stderr
    assert not (project / "pwned").exists()


def test_an_env_file_the_hook_creates_is_private(tmp_path: Path) -> None:
    """CR 1: the token's copy is no more readable than ``.env`` (600), whatever the
    caller's umask."""
    project = _project(tmp_path, "GH_TOKEN=t\n")
    old = os.umask(0o022)
    try:
        _, target = _hook(project, tmp_path)
    finally:
        os.umask(old)
    assert target.stat().st_mode & 0o777 == 0o600


def test_hook_reads_the_primary_checkouts_env_from_a_worktree(tmp_path: Path) -> None:
    """``.env`` is git-ignored, so a linked worktree has none of its own."""
    project = _project(tmp_path, "GH_TOKEN=primary\n")
    subprocess.run(["git", "-C", str(project), "add", "-A"], check=True)
    git_id = ["-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "-C", str(project), *git_id, "commit", "-qm", "x"], check=True)
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(project), "worktree", "add", "-q", str(wt)], check=True)
    out, target = _hook(wt, tmp_path)
    assert out.returncode == 0, out.stderr
    assert _exported(target) == "primary"


def test_hook_leaves_a_set_token_alone(tmp_path: Path) -> None:
    project = _project(tmp_path, "GH_TOKEN=from-file\n")
    out, target = _hook(project, tmp_path, extra={"GH_TOKEN": "from-env"})
    assert out.returncode == 0
    assert not target.exists()


def test_hook_says_when_env_has_no_token(tmp_path: Path) -> None:
    project = _project(tmp_path, "OTHER=1\n")
    out, target = _hook(project, tmp_path)
    assert out.returncode == 0
    assert "no GH_TOKEN" in out.stdout
    assert not target.exists()


def test_hook_without_claude_env_file_prints_the_manual_export(tmp_path: Path) -> None:
    """anthropics/claude-code#15840: the variable can be unset."""
    project = _project(tmp_path, "GH_TOKEN=secret-value\n")
    out, _ = _hook(project, tmp_path, env_file=False)
    assert out.returncode == 0
    assert "scripts/env-value.sh GH_TOKEN" in out.stdout
    assert "secret-value" not in out.stdout + out.stderr


@pytest.mark.parametrize(
    ("version", "warns"), [("2.9.0", True), ("2.77.9", True), ("2.78.0", False), ("2.101.0", False)]
)
def test_hook_warns_below_the_gh_floor(tmp_path: Path, version: str, warns: bool) -> None:
    """cli/cli#11514, fixed in 2.78.0; compared as versions (2.9.0 < 2.78.0), not strings."""
    project = _project(tmp_path, "GH_TOKEN=t\n")
    out, _ = _hook(project, tmp_path, extra={"PATH": _fake_gh(tmp_path, version)})
    assert out.returncode == 0
    assert ("2.78.0" in out.stdout) is warns, out.stdout


def test_hook_warns_when_gh_is_missing(tmp_path: Path) -> None:
    project = _project(tmp_path, "GH_TOKEN=t\n")
    out, target = _hook(project, tmp_path, extra={"GH_ENV_GH": "gh-not-installed"})
    assert out.returncode == 0
    assert "not found" in out.stdout
    assert _exported(target) == "t"


def _session_start() -> list[dict]:
    return [
        h for g in json.loads(SETTINGS.read_text())["hooks"]["SessionStart"] for h in g["hooks"]
    ]


def test_gh_env_is_registered_once() -> None:
    entries = [h for h in _session_start() if h["command"].endswith("# gh-env")]
    assert len(entries) == 1, entries
    assert ".claude/hooks/gh-env.sh" in entries[0]["command"]


#: The refresh hook's registration as ``install-refresh.sh`` writes it, with nothing
#: before ``bash``: the hook reads ``.env``'s ``GH_TOKEN`` itself and hands it to
#: ``gh`` and the push per call, never exported (gregoryfoster/skills#576). An
#: exported token reaches the new pointer's ``install-doctor.sh`` before anyone has
#: reviewed the bump, and a set ``GIT_CONFIG_COUNT`` keeps the hook from adding its
#: own credential helper.
REFRESH_COMMAND = 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/skills-submodule-update.sh"'


def _refresh_entry() -> dict:
    (entry,) = [h for h in _session_start() if REFRESH_HOOK.name in h["command"]]
    return entry


def _run_refresh_registration(tmp_path: Path, env_text: str | None) -> Path:
    """Run the registered command as the harness does, against a stand-in hook that
    records the token and the git config its environment carries."""
    project = _project(tmp_path, env_text)
    stand_in = project / REFRESH_HOOK
    stand_in.write_text(
        "#!/usr/bin/env bash\n"
        'printf %s "${GH_TOKEN-}" > "$CLAUDE_PROJECT_DIR/seen"\n'
        'printf %s "${GIT_CONFIG_COUNT-}" > "$CLAUDE_PROJECT_DIR/config-count"\n'
        'git config --get-all credential.helper > "$CLAUDE_PROJECT_DIR/helpers" || true\n'
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("GH_TOKEN", "GITHUB_TOKEN") and not k.startswith("GIT_CONFIG_")
    }
    env |= HERMETIC_GIT | {"CLAUDE_PROJECT_DIR": str(project)}
    subprocess.run(["bash", "-c", _refresh_entry()["command"]], env=env, cwd=project, check=True)
    return project


def test_the_refresh_registration_is_the_installers_own() -> None:
    """No prefix: re-running ``install-refresh.sh`` changes nothing it owns."""
    entry = _refresh_entry()
    assert entry["command"] == REFRESH_COMMAND
    assert entry["timeout"] == 120


def test_the_refresh_registration_exports_no_token_and_no_git_config(tmp_path: Path) -> None:
    """``.env``'s token stays in ``.env`` for the hook to read; nothing scopes git."""
    project = _run_refresh_registration(tmp_path, f"GH_TOKEN={NASTY}\n")
    assert (project / "seen").read_text() == ""
    assert (project / "config-count").read_text() == ""
    assert (project / "helpers").read_text() == ""
    assert not (project / "pwned").exists()


def test_the_refresh_hook_still_runs_without_a_token(tmp_path: Path) -> None:
    """It reinstalls the doctor every session, token or not."""
    project = _run_refresh_registration(tmp_path, None)
    assert (project / "seen").read_text() == ""
