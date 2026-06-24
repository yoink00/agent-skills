# Agent Skills

[![skills.sh](https://skills.sh/b/yoink00/agent-skills)](https://skills.sh/yoink00/agent-skills)

A collection of [agent skills](https://skills.sh) by **Stuart Wallace**, each
installable with the `skills` CLI.

## Install

Install every skill in this repo:

```bash
npx skills add yoink00/agent-skills
```

Install a single skill:

```bash
npx skills add yoink00/agent-skills --skill markdown-review
```

## Skills

| Skill                                            | What it does                                                                       |
| ------------------------------------------------ | ---------------------------------------------------------------------------------- |
| [`markdown-review`](./skills/markdown-review/)   | Edit a markdown doc with a live browser diff and collect inline comments back as JSON. |

Each skill folder has its own `README.md` and `SKILL.md` with full details.

## Contributing

See [**CONTRIBUTING.md**](./CONTRIBUTING.md) — in short, commit messages and
PR titles follow [Conventional Commits](https://www.conventionalcommits.org/)
(`type(scope): subject`, e.g. `feat(mdedit): ...`).

## Repository layout

This is a **multi-skill** repo. Each skill is self-contained in a folder under
`skills/`:

```
skills/<skill-name>/
├── SKILL.md          # required — frontmatter (name, description, …) + agent instructions
├── README.md         # human-facing overview
├── LICENSE           # MIT
├── .gitignore        # build/runtime cruft specific to this skill
└── …                 # bundled scripts, assets, vendored dependencies
```

There is intentionally **no `SKILL.md` at the repo root** — that is what makes
this a collection rather than a single skill. The `skills` CLI discovers each
skill by scanning `skills/*/SKILL.md` (or any subdirectory with a `SKILL.md`).

### Adding a new skill

1. Create `skills/<skill-name>/` — name the folder to match the `name` field in
   its `SKILL.md` frontmatter.
2. Add a `SKILL.md` (frontmatter + agent workflow) and a `README.md`.
3. Bundle any scripts/assets the skill needs, and a `.gitignore` for its cruft.
4. Add a row to the **Skills** table above.
5. Commit.

The quickest way to start is to copy [`skills/markdown-review`](./skills/markdown-review/)
as a template. You can also scaffold a bare skill with `npx skills init <name>`.

## License

[MIT](./LICENSE).
