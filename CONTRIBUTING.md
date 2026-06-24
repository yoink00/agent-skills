# Contributing

Thanks for contributing! A few short conventions keep the repo tidy.

## Commit & PR titles — Conventional Commits

**All commit messages and pull request titles must follow
[Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/):**

```
<type>(<optional scope>): <subject>
```

- **`type`** — the kind of change:
  - `feat` — a new feature
  - `fix` — a bug fix
  - `docs` — documentation only
  - `refactor` — code change that neither fixes a bug nor adds a feature
  - `perf` — code change that improves performance
  - `test` — adding or correcting tests
  - `build` — build system or external dependencies
  - `ci` — CI configuration and scripts
  - `style` — formatting, whitespace, etc. (no code change)
  - `chore` — other maintenance tasks
  - `revert` — revert a previous commit
- **`scope`** *(optional)* — the affected skill or area, e.g. `mdedit`. For
  cross-cutting changes (repo-wide tooling, CI) the scope may be omitted.
- **`subject`** — a short, imperative-mood summary, lowercase, no trailing period.

### Examples

```
feat(mdedit): add idle auto-shutdown
fix(mdedit): remove stale state file on SIGTERM
docs(readme): document skill layout
ci: add lint and test workflow
test(mdedit): cover keepalive polling
```

> **PR titles follow the same format.** When a PR is merged with "Squash and
> merge", set the squash-commit title to the PR's Conventional Commits title so
> the commit history stays consistent.

### Breaking changes

Append `!` before the colon (and/or add a `BREAKING CHANGE:` footer):

```
feat(mdedit)!: rename --idle-timeout to --shutdown-after

BREAKING CHANGE: --idle-timeout is renamed to --shutdown-after.
```

## Static checks & tests

CI (`.github/workflows/ci.yml`) runs the same checks you should run locally
before pushing. All of them are zero-config — settings live in
`pyproject.toml` (`[tool.ruff]`, `[tool.mypy]`) — and `uvx`/`uv run` pull in
the tools on demand, so there's nothing to install.

```sh
# Lint (ruff's curated defaults + I/UP/B).
uvx ruff check .

# Format check. To apply fixes instead of just checking, drop `--check`.
uvx ruff format --check .
# uvx ruff format .          # apply

# Static type-check (mypy, pinned to skills/markdown-review/mdedit.py).
uvx mypy

# Tests. Dev/test tooling is declared as PEP 735 dependency groups in
# pyproject.toml and pinned in the committed uv.lock; `uv run --group` builds
# an ephemeral env from the lock. The skills themselves stay pure-stdlib at
# runtime — these groups are dev-only.
#
# Fast suite (browser tests skip via `pytest.importorskip`):
uv run --frozen --group test pytest -v

# Browser suite (installs Playwright + Chromium):
uv run --frozen --group browser python -m playwright install chromium
uv run --frozen --group browser pytest -v
```

A one-liner to run the full local gate:

```sh
uvx ruff check . && uvx ruff format --check . && uvx mypy && \
uv run --frozen --group test pytest
```

When you add or bump a dev dependency, update `pyproject.toml`'s
`[dependency-groups]` and re-run `uv lock` to refresh the committed lockfile.
