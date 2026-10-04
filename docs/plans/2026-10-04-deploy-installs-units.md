---
title: deploy.sh installs the units (#18)
date: 2026-10-04
status: done
---

# deploy.sh installs the units

Issue: [#18](https://github.com/CannObserv/status/issues/18), found while reviewing #12. Builds on #9 ([spec](../specs/2026-09-30-deploy-releases-design.md), [plan](2026-09-30-deploy-releases.md)).

## Problem

Since #9, code reaches the units only through `scripts/deploy.sh`. The unit files never do: `deploy/*.service` and `deploy/*.timer` reach `/etc/systemd/system/` only by a hand-run `sudo cp`. On 2026-10-04 four of them (`status`, `status-dev`, `status-sweep`, `status-sweep-dev`) differ from the repo, in comments only, and nothing said so. A directive edit (a new `LoadCredential=`, a new `ExecStart=` path) would land at some unknown later time, or never. Release and unit can each depend on the other, and rollback has the same problem.

## Approach

The issue's option 1, **per target**: each target's step installs that target's units from the release it switches to, right after the switch and before the restart. Units and code switch together, and verification covers both.

- **Which units are whose: by name.** `<name>-dev.service` and `<name>-dev.timer` are dev's (`status-dev`, `status-sweep-dev`); every other `deploy/*.service` and `deploy/*.timer` is live's (`status`, `status-sweep`, `status-drift`). A test holds the repo to it: a unit's release root is `/srv/status/dev` exactly when its name ends `-dev`, and a timer belongs to its service's target. This answers the issue's "which release's units win": each target's own. A plain deploy installs both from the same commit; `--dev origin/<branch>` installs dev's units from the branch, so dev rehearses a unit edit as it rehearses a migration.
- **Only what differs.** `cmp` each unit against its installed copy. None differs (nearly every deploy): no `sudo`, no `daemon-reload`. Otherwise `sudo install -m 644`, one `daemon-reload`, and `systemctl try-restart` for each changed timer, so it re-arms on its new schedule. A timer that will not restart fails the target (CR 1): `status-sweep.timer` is the only thing watching for silence. The API restart and the forced sweep pass that follow already exercise the new service units.
- **New units are installed, not enabled.** Enabling is a decision (#12: create the healthchecks.io check first). The deploy names each new unit and the `enable --now` it needs.
- **Units a release lacks are left as they are.** A rollback to a build before #12 leaves `status-drift.*` installed (its `203/EXEC` is already in the RUNBOOK).
- **A failed verify restores the units too.** Before overwriting, the installed copies are copied aside (they are world-readable); on switch-back they go back, units the deploy added are removed, then `daemon-reload`. That is "switch back" for units: exactly what was there, as the link goes back to exactly what it named (CR 31 of #9). A failed install is a failed verify. With `--no-restart` a failed install restores before it stops (CR 3); with nothing kept aside, the switch back still proves the old build (CR 4).
- **Redeploying the running build** has no link to switch back, but may have replaced units, a hand fix most likely. On failure those go back and the build is proved on them; exit 4 only when nothing was replaced (CR 10).
- **A deliberate rollback** (`deploy.sh <old build>`) installs the old release's units. Units always match the build their target runs.
- **`--no-restart`** installs and reloads too, so first-time setup loses its `sudo cp` list and keeps the `enable --now` lines.
- **Host configs are compared, never installed** (the issue's option 2 for them). Sysctl, earlyoom, the slice and Postgres drop-ins and needrestart change rarely, and installing them means `sysctl -p`, an earlyoom restart, or a slice reload. After a live deploy, each that differs from its installed copy, or is missing, is a warning naming both paths and the RUNBOOK. Never a failure. A test holds every file under `deploy/` to being a unit or one of these.
- **Paths** come from `STATUS_DEPLOY_ETC` (default `/etc`): units in `systemd/system/`, host configs where the RUNBOOK installs them. The tests run against a throwaway one; `sudo` gains `install`, `rm` and `systemctl`.

## Tradeoffs / alternatives

- **Live's units for both targets, installed in the live step** (the issue's suggestion). Rejected: `--dev` could never try a dev unit edit, and a live failure would leave dev's units on a build dev does not run.
- **Warn only** (option 2). Rejected for units: drift becomes visible but still needs a human, and the "done when" wants the edit to arrive.
- **`systemctl link` into the release** (option 3). Rejected: dev's units would follow live's release unless linked per target, and every switch would need a `daemon-reload` anyway, so it saves nothing over copying.
- **Restore from the previous release's `deploy/`** on a failed verify. Rejected: the link may name a hand-made directory (CR 31), and the installed copy is what actually ran.
- **Install before the switch.** No better: in either order a sweep tick between the two steps runs one release's code under the other's unit. The window is the time between two commands, and the forced pass that follows is the one verified.

## Steps

1. **Harness.** `World` gains an `/etc` root and a helper that commits `deploy/` files to `main`; the `sudo` stub runs `install` and `rm` for real.
2. **Install per target** (tests first): each target's units from its release; only differing ones; one `daemon-reload`; `try-restart` of changed timers; dev-only installs dev's alone; new units named with `enable --now`; units the release lacks untouched; `--no-restart` installs. *Done when* green.
3. **Switch back restores** (tests first): a failed verify puts the installed copies back and removes added units; a failed install switches back. *Done when* green.
4. **Host configs** (tests first): warning per differing or missing file after a live deploy, none for `--dev`, exit 0; every `deploy/` file is a unit or a host config; repo units follow the naming rule. *Done when* green.
5. **Docs:** DEPLOYMENT (order, § Units replacing "Units are not deployed", variables), RUNBOOK (setup, § Watching co-status step 4, a routine-ops row), AGENTS, deploy spec, `deploy.sh` header.
6. **Ship (operator + agent).** Merge, `scripts/deploy.sh`: it installs the four differing units. Copy the two differing host configs by hand (comments only, so no `sysctl -p` or restart). Confirm with `cmp` that nothing differs.

   **Done 2026-10-04.**
   - `ec4008c56b2e` live and dev at 19:44Z, through the CI gate. The deploy installed `status-dev.service status-sweep-dev.service` (dev) and `status-sweep.service status.service` (live), and warned about `99-status-memory.conf` and `earlyoom.default`, exactly as the read-only rehearsal had said.
   - Both host configs installed by hand; their differences were comments only, so no `sysctl -p` or earlyoom restart.
   - `cmp`: every unit, drop-in and host config under `deploy/` matches its installed copy. No unit needs a daemon-reload; `/health` names `ec4008c56b2e` on both ports; the next production pass answered `/ready` 200 and pinged `co-status-api`.

## Open questions

None.
