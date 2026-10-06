---
title: Root-owned releases, links and deploy root (#14)
date: 2026-10-06
status: implemented; ship pending
---

# Root-owned releases, links and deploy root

Issue: [#14](https://github.com/CannObserv/status/issues/14), deferred from #9 ([spec R2](../specs/2026-09-30-deploy-releases-design.md), § Deferred). Builds on #18 ([plan](2026-10-04-deploy-installs-units.md)).

## Problem

Checked 2026-10-06 on `co-status`:

- Every release under `/srv/status/releases/` is `exedev:exedev` with write removed (`dr-xr-xr-x`). `/srv/status` itself, `releases/`, `.deploy.lock` and the `live`/`dev` links are `exedev`'s too.
- `exedev` is the user every agent session, editor and unit runs as, and it has passwordless sudo.
- So `chmod -R u+w /srv/status/live/src && $EDITOR …` changes what production runs, with no deploy and no record. So does `ln -sfn ~/status /srv/status/live`, which repoints live at a checkout. Root-owned releases alone would not stop the second, because the links belong to their directory, and so does the directory.
- **New finding:** the release venvs are hardlinked into the uv cache (`site-packages/fastapi/__init__.py` has 7 links: the cache, each release, any worktree venv). `chmod -R a-w` on a release already rewrites the modes of the cache's inodes. A `chmod u+w` and edit of a dependency in a release would edit the cache and every venv sharing that file. Today's read-only boundary does not even hold against an accident once someone has run `chmod`.

## Decision

**Option 1, extended to the deploy root.** Root owns `/srv/status`, `releases/` and every finished release; the links are replaced only by `sudo`. `exedev` builds, the units run as `exedev` and read, and nothing `exedev` does without `sudo` can change what a unit runs.

```
/srv/status/              root:root 0755   (refused otherwise)
  live -> releases/<b>    replaced by `sudo` rename only; a link is protected by its directory
  dev  -> releases/<b>
  releases/               root:root 0755   (created by the deploy if missing)
    <build>/              root:root, read-only; REVISION root's, written last
```

What this buys, given that `exedev` can still `sudo` anything:

- **Changing production code takes `sudo`.** It is no longer a silent `chmod`. sudo journals each command it runs with its user, directory and arguments (`journalctl _COMM=sudo`), so a hand edit leaves a record, unless it is made in a root shell, which is journaled only as a shell (CR 7). Agents running as `exedev` see a permission error where they now succeed.
- **The code matches its units.** `/etc/systemd/system/` and `/etc/status/` are already root's. #18 installs units with `sudo`, so the deploy path already uses sudo. That removes R2's objection, "a deploy-time `sudo` for no real boundary".
- **The release no longer shares inodes with the uv cache** (`--link-mode copy`, below), so it is the release's own copy whoever owns it.

It is **not an adversarial boundary**: a process running as `exedev` can `sudo` past it. The spec and DEPLOYMENT.md say so.

**Rejected:**

- **Option 2, a `status` user with a sudoers rule for `deploy.sh`.** It means nothing until `exedev`'s blanket sudo is narrowed. That is exe.dev's default on every cohort VM, and narrowing it is a VM-wide decision beyond co-status. Until then it buys no more than option 1, and costs a second user, a unit `User=` change for every unit, and readable `/etc/status` and credentials for that user.
- **Option 3, keep R2 and document it.** That leaves the silent path open, and the hardlink finding shows the boundary is weaker than R2 assumed.

## Approach

`scripts/deploy.sh`:

- **Deploy root.** Refuse unless `$ROOT` is root's and not group- or world-writable. The message names the one-time fix. Create `releases/` with `sudo install -d -m 755` if it is missing, and hold it to the same rule.
- **Lock** with `flock` on the deploy root's directory, opened read-only. `exedev` cannot create `.deploy.lock` in a root-owned directory, and does not need to.
- **Build.** `sudo install -d -o exedev` makes the release directory, so the venv is still built at its final path (R4). Then come `git archive`, `uv sync … --link-mode copy`, compileall and the heads check as `exedev`, then `chmod -R a-w`, then `sudo chown -R root:root`. **`REVISION` is still written last, now by root** (`sudo tee`, then mode 444). A build cut short anywhere, the chown included, has no `REVISION` and is rebuilt.
- **Reuse.** A release whose directory is not root's is not finished: built before #14, or handed back by hand. It is rebuilt, or refused when a target runs it, as with any other unusable release (CR 15, 27). This replaces the `-w` check (CR 26): with `REVISION` written after the `chmod` and `chown`, a writable release never has one.
- **`sudo` for every write under the root:** the swap (`ln` and `mv`), `touch` (prune order), removing a release before a rebuild, and the prune (`rm` of `REVISION`, then `rm -rf`). `make_writable` goes, since root needs no `u+w`.
- **Still runs as `exedev`, never root.** Built by root, uv would use root's cache and interpreters, which the units cannot read.

**Disk:** a copied venv is about 70 MB. With 5 kept plus up to 2 linked, that is at most about 0.5 GB of the 10 GB free.

**Legacy releases** (`exedev`-owned, hardlinked to the cache) are **not** chowned: `chown -R` would reach the cache's inodes. They stay `exedev`'s until pruned. A rollback to one rebuilds it, which is the point. The first deploy after this ships builds a fresh root-owned release for both targets.

## Tests (`tests/deploy/test_deploy.py`)

An unprivileged test cannot make a file root's, so the harness plays root:

- A **ledger** of root-owned paths. `World` enters the deploy root in it, as the RUNBOOK's `sudo install -d` does. The `sudo` stub enters what it creates (`install -d` without `-o`) and what it chowns to `root:root` (recursively). It drops what it removes.
- A **`stat` stub** answers `%u` as `0` for anything in the ledger, and otherwise behaves like the real `stat`.
- **What root owns, the user cannot write.** Ledger directories are kept `a-w`. The `sudo` stub alone opens them for the one command it runs, and makes a release writable before an `rm -rf`, as root needs no permission. **Any write under the root that is not done with `sudo` fails the test.** That is the check that the rollback, a failed verify and the prune still work: their existing tests run against a root `exedev` cannot write.

New or changed tests:

- After a build: the release and everything under it are root's (the ledger). The order is `chmod -R a-w`, then `sudo chown -R root:root`, then `REVISION` by `sudo`, last. `REVISION` is mode 444. The sync is `--link-mode copy`.
- A failed chown switches nothing, and leaves no `REVISION`.
- A deploy root that is not root's, or is group/world-writable, is refused with the fix, and nothing is built.
- `releases/` missing is created by `sudo`, root's.
- A release not root's (built before #14): unlinked, it is rebuilt; linked, it is refused (replaces `test_a_writable_release_is_never_reused`).
- A concurrent deploy is refused: the lock is held on the directory.
- The links are replaced with `sudo`, on switch and switch back.
- The prune removes with `sudo`, `REVISION` first, and a failed prune is still a note.

## Steps

1. **Harness:** the ledger, `stat` stub, `sudo` stub, an unwritable root, and `World.as_root()` for tests that edit the root by hand. The existing suite fails where `deploy.sh` writes without `sudo` (red).
2. **Deploy root and lock** (tests first).
3. **Build and reuse** (tests first): build dir, copy link mode, chown, root's `REVISION`, ownership as the finished check.
4. **Swap, touch, rebuild, prune with `sudo`:** the whole suite green.
5. **Docs:** spec R2 amendment, § Deferred, Q1, Q4, Q6 and cutover; DEPLOYMENT (layout, the boundary and what it is not, a one-time migration); RUNBOOK (setup creates a root-owned root; routine ops); AGENTS; the `deploy.sh` header.
6. **Ship (operator).** Merge, then once on `co-status`:

   From a checkout with #14 in it (an older `deploy.sh` fails at its lock file with a bare `Permission denied`):

   ```bash
   cd /home/exedev/status && git switch main && git pull --ff-only
   sudo chown root:root /srv/status /srv/status/releases
   sudo chmod 755 /srv/status /srv/status/releases
   sudo rm -f /srv/status/.deploy.lock
   scripts/deploy.sh
   ```

   Check: `stat -c '%U %a %n' /srv/status /srv/status/releases "$(readlink -f /srv/status/live)"` reads `root` throughout. `stat -c %h` of a file in its venv is `1`. `/health` names the new build on both ports.

## Open questions

None.
