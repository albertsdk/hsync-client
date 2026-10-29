## Description

Brief summary of the changes and why they are needed.

## Type of change

- [ ] Bug fix (non-breaking change)
- [ ] New feature (non-breaking change)
- [ ] Breaking change (protocol, API, or CLI)
- [ ] Documentation update
- [ ] Internal refactor / performance improvement

## Checklist

- [ ] Tests pass locally (`poetry run pytest`)
- [ ] Code is linted (`poetry run ruff check --fix . && poetry run ruff format .`)
- [ ] Documentation is updated:
  - [ ] `docs/` (user-facing docs) updated if CLI options, env vars, or behavior changed
  - [ ] `llm-wiki/` (internal wiki) updated if internal architecture changed
- [ ] `CHANGELOG.md` updated under `## [Unreleased]` if the change is user-facing
- [ ] WD OS5 package updated in `hsync-wd-os5` if CLI flags or env vars changed
- [ ] `docs/configuration.md` updated if CLI options changed

## Related issues

Closes # (issue number) or N/A.
