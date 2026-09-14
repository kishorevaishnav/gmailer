# Gmailer — agent working rules

## Dev loop
- Run: `.venv/bin/uvicorn main:app --port 8000 --reload --reload-include '*.py'`
  (include filter keeps `data/*.db` writes from restarting the server).
- Live reload: every page under `static/*.html` must end with
  `<script src="/static/dev-reload.js"></script>`. It polls `GET /api/dev-hash`
  (stat-hash over `static/*`, `main.py`, `backend/*.py`) every 2.5s and reloads
  on change or after a server restart. When adding a page, add the tag and add
  its assets to the hash list in `api_dev_hash` if they live outside `static/`.
- Static files are served with `Cache-Control: no-cache`; still, tell the user
  to hard-refresh (Cmd+Shift+R) if a change looks stale.

## Verify before claiming done
- `node --check static/<file>.js` for every touched script.
- `.venv/bin/python -m py_compile` for touched Python files.
- `.venv/bin/python -m pytest backend/tests -q`. Known failures (do not
  "fix" by changing API field names): 6 in `test_api_wiki.py` expect a legacy
  `skill_md` request field while the API takes `markdown`.
- Restart the app and smoke-test touched endpoints with curl before reporting.

## Data safety
- Never commit secrets, tokens, or databases (`credentials.json`, `token.json`,
  `data/`, `*.db*` are gitignored — keep it that way).
- Back up `data/gmailer.db` to `/tmp` before any migration that deletes or
  rewrites rows.

## Code conventions
- No code comments unless asked.
- Single-user local tool: SQLite + lock patterns in `backend/store.py` are the
  norm; watch for nested `_lock` acquisition (use plain queries inside).
- FastAPI request models use `markdown` (skill-markdown source of truth);
  rule match JSON uses sender **lists**.
- Commit only when the user explicitly asks. Keep commits single-purpose with
  `feat|fix|test:` prefixes.
