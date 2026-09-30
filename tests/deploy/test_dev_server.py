"""Drift tests for scripts/dev_server.sh — the guarded dev-server launch path.

The script must not reimplement the URL parsing in bash. Archiver's bash
``db_name()`` diverged from its python guard on three URL shapes
(CannObserv/archiver#99, commit ee0c952); co-status avoids the whole class of
bug by shelling out to ``src.core.db_safety`` instead.
"""

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_SERVER = REPO_ROOT / "scripts" / "dev_server.sh"


def test_dev_server_script_exists_and_is_executable():
    assert DEV_SERVER.is_file()
    assert DEV_SERVER.stat().st_mode & stat.S_IXUSR


def test_dev_server_delegates_the_guard_to_python():
    """One implementation of the URL check, not two."""
    body = DEV_SERVER.read_text()
    assert "src.core.db_safety" in body
    assert "db_name" not in body, "bash must not reimplement the URL parsing"


def test_dev_server_delegates_env_loading_to_the_shared_loader():
    """One env-loading recipe, in scripts/load_env.sh — not a second copy."""
    body = DEV_SERVER.read_text()
    assert "load_env.sh" in body
    assert "export $(cat" not in body
    assert "| xargs" not in body


def test_dev_server_sources_the_loader_by_resolved_path():
    """Not repo-root-relative — that silently couples it to the earlier cd."""
    body = DEV_SERVER.read_text()
    assert '. "$(dirname "${BASH_SOURCE[0]}")/load_env.sh"' in body
    assert ". scripts/load_env.sh" not in body


def test_dev_server_delegates_the_migration_check_to_python():
    """An unmigrated dev DB starts cleanly and 500s on every request (notifier#23).

    Since #9 the check is src.core.schema_state, the one the sweep and /ready
    use, so it also catches a database merely *behind* the code, not only an
    empty one: bash no longer decides what "migrated" means.
    """
    body = DEV_SERVER.read_text()
    assert "src.core.schema_state" in body
    assert "alembic current" not in body


def test_dev_server_refuses_a_database_behind_the_code(tmp_path):
    """schema_state exits 3: say what to run, and never reach uvicorn."""
    fake = tmp_path / "uv"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *schema_state* ]]; then echo behind; exit 3; fi\n'
        'echo "uv $*"\n'
    )
    fake.chmod(0o755)
    env = {
        **os.environ,
        "STATUS_DEV_SERVER_SKIP_ENV_FILES": "1",
        "DEV_DATABASE_URL": "postgresql+asyncpg://u@h/status_dev",
        "STATUS_BIND_HOST": "127.0.0.1",
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
    }
    env.pop("STATUS_ALLOW_PROD_DB", None)
    result = subprocess.run(
        [str(DEV_SERVER)], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "behind" in combined
    assert "alembic upgrade head" in combined
    assert "uvicorn" not in result.stdout


def test_dev_server_refuses_an_unreachable_dev_database():
    env = {
        **os.environ,
        "STATUS_DEV_SERVER_SKIP_ENV_FILES": "1",
        # Port 1 is unroutable, so the connection failure is structural rather
        # than depending on no database named nope_dev ever existing.
        "DEV_DATABASE_URL": "postgresql+asyncpg://status@localhost:1/nope_dev",
    }
    result = subprocess.run(
        [str(DEV_SERVER)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "migration" in (result.stdout + result.stderr).lower()


def test_dev_server_binds_the_tailnet_address_not_a_wildcard(tmp_path):
    """notifier#43 D3 — the dev endpoint carries real consumer traffic from watcher.

    Asserts the resolved address reaches uvicorn, so a future edit cannot
    quietly reintroduce ``--host 0.0.0.0`` while the probe still runs.
    """
    line = _run_dev_server(tmp_path, STATUS_BIND_HOST="100.64.1.2")
    assert "--host 100.64.1.2" in line
    assert "0.0.0.0" not in line


def test_dev_server_targets_port_9001():
    """Port 9000 belongs to systemd."""
    body = DEV_SERVER.read_text()
    assert "--port 9001" in body
    assert "--port 9000" not in body


def test_dev_server_refuses_a_production_database_url():
    """The failure path exits non-zero before uvicorn is ever reached."""
    env = {
        **os.environ,
        # Skip the env files — they set DEV_DATABASE_URL to the real dev
        # database, which would override the production URL under test.
        "STATUS_DEV_SERVER_SKIP_ENV_FILES": "1",
        "DEV_DATABASE_URL": "postgresql+asyncpg://u@h/status",
    }
    env.pop("STATUS_ALLOW_PROD_DB", None)
    # Short timeout on purpose: if the guard ever stops refusing, uvicorn
    # starts and this must fail fast rather than hang the suite.
    result = subprocess.run(
        [str(DEV_SERVER)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "status" in combined
    assert "uvicorn running" not in combined.lower()


def test_dev_server_refuses_when_no_dev_database_is_configured():
    env = {**os.environ}
    env.pop("DEV_DATABASE_URL", None)
    env.pop("STATUS_ALLOW_PROD_DB", None)
    result = subprocess.run(
        [str(DEV_SERVER)],
        cwd=REPO_ROOT,
        env={**env, "STATUS_DEV_SERVER_SKIP_ENV_FILES": "1"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "DEV_DATABASE_URL" in result.stdout + result.stderr


def _fake_uv_path(tmp_path):
    """A `uv` that echoes its arguments instead of running anything.

    Lets the launch path be exercised end to end — guard call, migration
    check, final uvicorn line — with no database and no server. The migration
    check reads schema_state's exit status, so exiting 0 satisfies it.
    """
    fake = tmp_path / "uv"
    fake.write_text('#!/usr/bin/env bash\necho "uv $*"\n')
    fake.chmod(0o755)
    return tmp_path


def _run_dev_server(tmp_path, **overrides):
    env = {
        **os.environ,
        "STATUS_DEV_SERVER_SKIP_ENV_FILES": "1",
        "DEV_DATABASE_URL": "postgresql+asyncpg://u@h/status_dev",
        # CI has no tailnet, so the real probe would burn its full wait and
        # then fail every test in this file. The override exists for exactly
        # this; test_systemd_unit.py asserts no unit or env file carries it.
        "STATUS_BIND_HOST": "127.0.0.1",
        "PATH": f"{_fake_uv_path(tmp_path)}:{os.environ['PATH']}",
        **overrides,
    }
    env.pop("STATUS_ALLOW_PROD_DB", None)
    result = subprocess.run(
        [str(DEV_SERVER)], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
    uvicorn_line = next((line for line in result.stdout.splitlines() if "uvicorn" in line), None)
    assert uvicorn_line is not None, f"never reached uvicorn:\n{result.stdout}{result.stderr}"
    return uvicorn_line


def test_dev_server_reloads_by_default(tmp_path):
    """The hand-run server is an editing loop; keep --reload there."""
    assert "--reload" in _run_dev_server(tmp_path)


def test_dev_server_reload_can_be_switched_off(tmp_path):
    """deploy/status-dev.service sets this (notifier#24).

    Under systemd the reloader is wrong twice over: an edit mid-request drops
    a consumer's connection, and a syntax error on main leaves the reloader
    wedged and *running*, so Restart=on-failure never fires and the endpoint
    is silently dead.
    """
    assert "--reload" not in _run_dev_server(tmp_path, STATUS_DEV_RELOAD="0")


def test_dev_server_still_guards_when_the_reloader_is_off(tmp_path):
    """The toggle must not become a way around the checks."""
    line = _run_dev_server(tmp_path, STATUS_DEV_RELOAD="0")
    assert "--port 9001" in line
