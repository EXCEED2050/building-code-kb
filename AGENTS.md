# Repository instructions

- Read `CONTRIBUTING.md` before editing or committing. Use Conventional Commits for new commits.
- Run `python scripts/check.py` for code changes. Ruff configuration is in `pyproject.toml`; format only active Python code, tests and maintenance scripts.
- Keep fixes and formatting in separate commits when practical. Do not rewrite published history or force push.
- Preserve original JSONL bytes and data provenance unless the task explicitly requires a data change. Update the appropriate manifest when changing a tracked snapshot.
- Use `building_kb.paths` for workspace paths. Do not shadow that module with local variables.
- Do not commit `.env`, real OCR machine configuration, `archive/`, `var/`, raw local documents or model weights. Follow `.gitignore` and the requested sharing scope.
