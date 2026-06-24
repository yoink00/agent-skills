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
