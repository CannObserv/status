# co-status

The CannObserv cohort's monitoring service. Its first job is dead-man's timers: a consumer checks in on a cadence, and a check-in that does not arrive is the alert. Alerts are delivered through [notifier](https://github.com/CannObserv/notifier).

- Design: [docs/specs/2026-09-26-co-status-mvp-design.md](docs/specs/2026-09-26-co-status-mvp-design.md)
- Tracking: [#2](https://github.com/CannObserv/status/issues/2)
- Agent guidelines: [AGENTS.md](AGENTS.md)
