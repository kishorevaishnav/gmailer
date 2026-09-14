# Restricted Files

These files contain sensitive credentials and **must never be read, edited, or referenced** by Kilo or any agent.

## Always Restricted

| File | Reason |
|---|---|
| `credentials.json` | Google OAuth client ID (private key, client secret) |
| `token.json` | Active OAuth access/refresh tokens for Gmail API |
| `client_secret_*.json` | Google OAuth client secret files |

## Enforced By

- **`kilo.jsonc`** — `permission.read` and `permission.edit` set to `deny` for these patterns
- **`.gitignore`** — Already excluded from version control (lines 6-8)
- **`~/.config/kilo/.gitignore`** — Added to global Kilo ignore list

## What To Do If You Need Them

These files are needed only for initial OAuth setup (Phase 0). After setup they should be deleted or kept outside the project:

```bash
# After initial setup, remove tokens from repo
rm token.json
```

If you need to re-authenticate, temporarily place the files in the project root, complete the OAuth flow, then move them out.
