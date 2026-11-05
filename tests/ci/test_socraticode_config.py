"""SocratiCode's client config for co-index's shared store holds (docs/SOCRATICODE.md, #76).

Mirrors CannObserv/processor's ``tests/test_socraticode_config.py`` (processor#5), itself
from broker's (broker#17, broker#33), adapted: this repo's env sources, its one linked
sibling, the vendored links in ``.claude/hooks/`` out of the index, and the skills
refresh hook's entry kept beside SocratiCode's two. Nothing here reaches the store; the
VM-local checks skip loudly where their file is absent rather than pass vacuously.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG = ROOT / ".socraticode.json"
MANIFEST = ROOT / ".socraticodecontextartifacts.json"
INDEX_IGNORE = ROOT / ".socraticodeignore"
SETTINGS = ROOT / ".claude" / "settings.json"
SETTINGS_LOCAL = ROOT / ".claude" / "settings.local.json"
HOOKS_DIR = ROOT / ".claude" / "hooks"

#: Where a file on this host puts a variable in the MCP server's environment: the env
#: file a shell here sources, and the three settings scopes Claude Code merges into a
#: session (the plugin's server inherits the session's environment). Not one:
#: ``/etc/status/.env``, which only the units read.
ENV_SOURCES = {
    "repo-env": ROOT / ".env",
    "project-settings": SETTINGS,
    "local-settings": SETTINGS_LOCAL,
    "user-settings": Path.home() / ".claude" / "settings.json",
}

#: Each one splits or redirects the namespace while every health check stays green:
#: the prefix is also prepended to the store-wide ``socraticode_metadata``; branch
#: awareness indexes a fresh set per branch; the project-id override outranks
#: ``.socraticode.json`` and is for one removal, never persisted; and a URL built from
#: ``QDRANT_HOST`` defaults to port 16333, which reads as a network fault.
GUARDED = [
    "QDRANT_COLLECTION_PREFIX",
    "SOCRATICODE_BRANCH_AWARE",
    "SOCRATICODE_PROJECT_ID",
    "QDRANT_HOST",
]

#: The store on co-index, spelled the way every other client spells it. A collection
#: holds vectors from one model at one dimension, so a client that differs poisons
#: what its siblings read.
CLIENT_ENV = {
    "QDRANT_MODE": "external",
    "QDRANT_URL": "https://index.taild0fb76.ts.net:6333",
    "OLLAMA_MODE": "external",
    "OLLAMA_URL": "http://index:11434",
    "EMBEDDING_MODEL": "nomic-embed-text",
    "EMBEDDING_DIMENSIONS": "768",
}

#: The newest version any cohort client writes with (notifier's, 2026-11-04). Pin
#: forward, never back: a client older than a collection's ``indexFormatVersion``
#: reads zero rows from it with no error (notifier#72). Moving it is a decision, and
#: the driver is re-pinned with it.
SOCRATICODE_SPEC = "socraticode@1.16.1"
_EXACT_SPEC = re.compile(r"^socraticode@(\d+\.\d+\.\d+)$")

#: The driver's pre-install (the health hook, index, status, verify), outside the repo.
DRIVER_PIN = Path.home() / ".socraticode" / "pin" / "node_modules" / "socraticode"

#: The sibling this repo reads: the check-in contract it freezes, and ``/dispatch``,
#: reached through ``notifier-client``. The operator chose it on 2026-11-04.
LINKED = {"../notifier"}

#: Vendored skills and their installed tooling. Nothing in ``skills/`` is first-party,
#: so ``skills/`` is excluded whole.
VENDORED = {"skills-vendor/", "skills/", ".claude/skills/", ".skills/"}

#: using-git-worktrees' root and the harness's, both inside this tree.
NESTED_CHECKOUTS = {".worktrees/", ".claude/worktrees/"}

#: An indexer that follows symlinks would reach the vendored tree through a hook
#: link, so each is excluded by name. The directory isn't: ``gh-env.sh`` (#92) is
#: this repo's own and stays in the index.
HOOK_LINKS = ".claude/hooks/"

#: Dated prose. It is out of the code index so it can't outrank source, and stays
#: searchable as a context artifact.
DATED_PROSE = "docs/plans/"

#: Each SessionStart hook with the dedupe marker its entry carries. The markers are
#: distinct per hook so that one hook's strip can't evict another hook's entry.
HOOKS = {
    "socraticode-reminder.sh": "socraticode-prefetch",
    "socraticode-health.sh": "socraticode-health",
}

#: managing-skills' refresh hook (#86). It has no marker in its command, and
#: install-hook.sh's dedupe must leave it alone.
REFRESH_HOOK = "skills-submodule-update.sh"

#: Upstream's ``assertValidProjectId``, which throws on anything else rather than sanitize.
PROJECT_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")

_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _read_if_present(path: Path) -> str | None:
    """Only ``FileNotFoundError`` means absent; a ``PermissionError`` propagates."""
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def _variables(path: Path, text: str) -> dict[str, str]:
    """What the file puts in the server's environment: a settings file's ``env``
    block, or an env file's assignments, so a comment naming a variable is no finding.
    """
    if path.suffix == ".json":
        return {k: str(v) for k, v in json.loads(text).get("env", {}).items()}
    return {
        m[1]: m[2].strip().strip("'\"") for ln in text.splitlines() if (m := _ASSIGNMENT.match(ln))
    }


def _index_ignore_entries() -> set[str]:
    lines = (ln.strip() for ln in INDEX_IGNORE.read_text().splitlines())
    return {ln for ln in lines if ln and not ln.startswith("#")}


def _session_start_commands() -> list[str]:
    groups = json.loads(SETTINGS.read_text())["hooks"]["SessionStart"]
    return [h["command"] for group in groups for h in group["hooks"]]


@pytest.fixture(scope="module")
def config() -> dict:
    return json.loads(CONFIG.read_text())


@pytest.fixture(scope="module")
def client_env() -> dict[str, str]:
    return _variables(SETTINGS, SETTINGS.read_text())


# ── Guards ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", list(ENV_SOURCES.values()), ids=list(ENV_SOURCES))
@pytest.mark.parametrize("variable", GUARDED)
def test_guarded_variables_are_set_nowhere(variable: str, path: Path) -> None:
    text = _read_if_present(path)
    if text is None:
        pytest.skip(f"{path} not present on this machine")
    assert variable not in _variables(path, text), f"{path} sets {variable}"


def test_the_shared_store_is_never_addressed_without_this_project_id() -> None:
    """The store's address and this repo's name ship together, or not at all.

    Without ``.socraticode.json`` the id is ``sha256(abs_path)[:12]``, and exe.dev VMs
    check repos out at the same path. That means two hosts writing one collection under
    a host-local lock. The check uses a literal, not ``ROOT.name``, because in a
    worktree ``ROOT.name`` is the worktree's directory.
    """
    addressing = []
    for label, path in ENV_SOURCES.items():
        text = _read_if_present(path)
        if text is None:
            continue
        env = _variables(path, text)
        if env.get("QDRANT_MODE") == "external" or "QDRANT_URL" in env:
            addressing.append(label)
    if not addressing:
        return
    assert CONFIG.exists(), f"{addressing} address the shared store, but {CONFIG.name} is missing"
    assert json.loads(CONFIG.read_text()).get("projectId") == "status"


def test_local_settings_are_ignored_by_the_tracked_gitignore() -> None:
    """This file holds the cohort's one Qdrant key, so a leak means rotating it everywhere.

    The check goes by the source ``check-ignore -v`` names, because a global excludes
    file protects only one machine. A tracked file is never reported as ignored, so a
    committed key fails this too.
    """
    if not shutil.which("git"):
        pytest.skip("git not installed")
    target = str(SETTINGS_LOCAL.relative_to(ROOT))
    result = subprocess.run(
        ["git", "check-ignore", "-v", target], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, f"{target} is not ignored, or is tracked: {result.stderr}"
    source = result.stdout.split(":", 1)[0]
    assert source == ".gitignore", f"{target} is ignored by {source}, not the tracked .gitignore"


def test_local_settings_are_untracked() -> None:
    if not shutil.which("git"):
        pytest.skip("git not installed")
    target = str(SETTINGS_LOCAL.relative_to(ROOT))
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", target], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode != 0, f"{target} is tracked"


def test_the_committed_settings_hold_no_qdrant_key(client_env: dict[str, str]) -> None:
    assert "QDRANT_API_KEY" not in client_env


# ── The config ──────────────────────────────────────────────────────────────────


def test_project_id_is_status(config: dict) -> None:
    """``codebase_status`` in the shared store. Upstream ignores a malformed file
    without reporting it, so parsing it here is half the point."""
    assert config["projectId"] == "status"
    assert set(config["projectId"]) <= PROJECT_ID_CHARS


def test_linked_projects_are_relative_siblings(config: dict) -> None:
    """A relative path works on every clone; an absolute one names one host's layout."""
    linked = config["linkedProjects"]
    assert len(linked) == len(set(linked))
    assert set(linked) == LINKED
    assert all(entry.startswith("../") and not Path(entry).is_absolute() for entry in linked)


def test_the_client_env_is_the_cohort_store(client_env: dict[str, str]) -> None:
    assert {k: client_env.get(k) for k in CLIENT_ENV} == CLIENT_ENV


def test_qdrant_is_addressed_by_a_full_https_url(client_env: dict[str, str]) -> None:
    """The URL must use the MagicDNS FQDN the certificate names, and https, or upstream
    refuses to send the key. The port is spelled out, and there is no path, which
    upstream's client drops."""
    url = urlparse(client_env["QDRANT_URL"])
    assert url.scheme == "https"
    assert url.port == 6333
    assert url.hostname is not None and url.hostname.endswith(".ts.net")
    assert url.hostname.count(".") >= 2, f"{url.hostname} is not the full MagicDNS name"
    assert url.path in ("", "/")


def test_the_session_launch_is_pinned(client_env: dict[str, str]) -> None:
    """Unset, the plugin launches ``npx -y --prefer-online socraticode@latest``: an
    install at every session start, the 1.2 G peak behind broker's 2026-09-16 outage."""
    assert client_env.get("SOCRATICODE_SPEC") == SOCRATICODE_SPEC


def test_the_session_and_driver_pins_agree(client_env: dict[str, str]) -> None:
    """A half re-pin leaves two builds writing one store. VM-local."""
    text = _read_if_present(DRIVER_PIN / "package.json")
    if text is None:
        pytest.skip(f"no driver pin at {DRIVER_PIN}")
    match = _EXACT_SPEC.match(client_env.get("SOCRATICODE_SPEC", ""))
    assert match, "SOCRATICODE_SPEC is not an exact pin"
    assert json.loads(text)["version"] == match[1]


# ── Index scope ─────────────────────────────────────────────────────────────────


def test_the_index_excludes_vendored_skills_nested_checkouts_and_hook_links() -> None:
    """Left in, vendored prose outnumbers this repo's own files, and every sibling's
    ``includeLinked`` search would serve it as status."""
    assert VENDORED | NESTED_CHECKOUTS <= _index_ignore_entries()


def test_every_excluded_skill_is_vendored_or_an_upstream_override() -> None:
    """``skills/`` is excluded whole only because nothing in it belongs to this repo.

    A first-party skill would drop out of the index without a word, so narrow the
    ``skills/`` entry before adding one.
    """
    for entry in sorted((ROOT / "skills").iterdir()):
        if entry.is_symlink():
            assert os.readlink(entry).startswith("../skills-vendor/"), entry
        else:
            assert "overrides:" in (entry / "SKILL.md").read_text(), f"{entry} is first-party"
    for entry in sorted((ROOT / ".claude" / "skills").iterdir()):
        assert entry.is_symlink() and os.readlink(entry).startswith("../../skills/"), entry


def test_each_vendored_hook_link_is_excluded_by_name_and_own_hooks_are_indexed() -> None:
    """A link left in serves vendored code as status; excluding ``.claude/hooks/``
    whole would drop this repo's own hooks out of the index without a word."""
    ignored = _index_ignore_entries()
    assert HOOK_LINKS not in ignored, "excludes gh-env.sh (#92) along with the links"
    for entry in sorted(HOOKS_DIR.iterdir()):
        name = f"{HOOK_LINKS}{entry.name}"
        if entry.is_symlink():
            assert "skills-vendor/" in os.readlink(entry), entry
            assert name in ignored, f"{name}: a vendored link the index would follow"
        else:
            assert name not in ignored, f"{name}: this repo's own, dropped from the index"


def test_dated_prose_left_the_code_index_for_the_context_store() -> None:
    """It is excluded from one store only because the other still answers from it."""
    assert DATED_PROSE in _index_ignore_entries()
    paths = {a["path"] for a in json.loads(MANIFEST.read_text())["artifacts"]}
    assert f"./{DATED_PROSE.rstrip('/')}" in paths


def test_the_manifest_is_an_object_whose_paths_resolve() -> None:
    """A rejected manifest fails silently: the repo indexes 'successfully' with no
    context search. A bare array is refused outright, and an unresolved path is skipped
    on its own, so ``artifacts N/N`` never reaches parity."""
    manifest = json.loads(MANIFEST.read_text())
    assert isinstance(manifest, dict), "a top-level array is rejected outright"
    artifacts = manifest["artifacts"]
    assert artifacts
    names = [a["name"] for a in artifacts]
    assert len(names) == len({n.lower() for n in names}), names
    for artifact in artifacts:
        assert set(artifact) == {"name", "path", "description"}, artifact
        path = artifact["path"]
        assert not any(c in path for c in "*?["), f"{path} is a glob: the server stat()s it"
        assert (ROOT / path).exists(), f"{path} does not resolve"


# ── Hooks ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("hook", list(HOOKS))
def test_each_hook_is_a_symlink_into_the_vendored_tree(hook: str) -> None:
    """A copy freezes on the day it is installed, and the health hook is silent when
    clean, so a stale copy looks healthy. This checks shape, not resolution, because
    the link dangles without submodules."""
    path = HOOKS_DIR / hook
    assert path.is_symlink(), f"{hook} is not a symlink"
    target = os.readlink(path)
    assert not Path(target).is_absolute(), f"{hook} -> {target}"
    assert "skills-vendor/" in target, f"{hook} -> {target} leaves the vendored tree"


@pytest.mark.parametrize(("hook", "marker"), list(HOOKS.items()), ids=list(HOOKS))
def test_each_hook_is_registered_exactly_once(hook: str, marker: str) -> None:
    """Registered, or the hook is a file that never runs; once, or it runs twice."""
    entries = [c for c in _session_start_commands() if c.endswith(f"# {marker}")]
    assert len(entries) == 1, f"{marker}: {len(entries)} SessionStart entries"
    assert hook in entries[0]


def test_the_skills_refresh_hook_survived_the_merge() -> None:
    """install-hook.sh strips entries by marker and appends, and the refresh hook's
    entry carries no marker. It must still be there, once, with its 120 s bound (#86)."""
    groups = json.loads(SETTINGS.read_text())["hooks"]["SessionStart"]
    entries = [h for g in groups for h in g["hooks"] if REFRESH_HOOK in h["command"]]
    assert len(entries) == 1, f"{REFRESH_HOOK}: {len(entries)} SessionStart entries"
    assert entries[0]["timeout"] == 120
