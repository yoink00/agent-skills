# Notes for coding agents

Before committing or pushing, **always follow [`CONTRIBUTING.md`](./CONTRIBUTING.md)**.

In particular, run the full local gate from that guide and make sure it is green:

```sh
uvx ruff check . && uvx ruff format --check . && uvx mypy && \
uv run --frozen --group test pytest
```

If any of these fail, fix them before pushing — CI runs the same checks and will
fail the PR otherwise. Settings are zero-config in `pyproject.toml`
(`[tool.ruff]`, `[tool.mypy]`); `uvx`/`uv run` pull the tools on demand.

Also remember:
- **Commit messages and PR titles must follow Conventional Commits**
  (`<type>(<scope>): <subject>`), e.g. `feat(mdedit): ...`. See CONTRIBUTING.md.
