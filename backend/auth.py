from __future__ import annotations

import json
import logging

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from . import config

logger = logging.getLogger("gmailer.auth")

# In-memory store of pending OAuth flows, keyed by state value.
_PENDING_FLOWS: dict[str, Flow] = {}


def credentials_exists() -> bool:
    return config.CREDENTIALS_FILE.exists()


def token_exists() -> bool:
    return config.TOKEN_FILE.exists()


def start_flow() -> tuple[str, str]:
    flow = Flow.from_client_secrets_file(
        str(config.CREDENTIALS_FILE), scopes=config.SCOPES
    )
    flow.redirect_uri = config.AUTH_REDIRECT_URI
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    _PENDING_FLOWS[state] = flow
    return auth_url, state


def finish_flow(state: str, code: str) -> str:
    flow = _PENDING_FLOWS.pop(state, None)
    if flow is None:
        raise ValueError("OAuth state mismatch or flow expired — restart auth.")
    flow.fetch_token(code=code)
    creds = flow.credentials
    _save_credentials(creds)
    return creds


def _save_credentials(creds: Credentials) -> None:
    config.TOKEN_FILE.write_text(creds.to_json())


def load_credentials() -> Credentials:
    if not token_exists():
        raise FileNotFoundError("No token.json found — authenticate first.")

    data = json.loads(config.TOKEN_FILE.read_text())
    creds = Credentials.from_authorized_user_info(data, scopes=config.SCOPES)

    # Refresh + persist any expired token so re-runs keep working offline.
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        _save_credentials(creds)

    if not creds.valid:
        raise PermissionError("Saved token is no longer valid — re-authenticate.")

    return creds


def clear_token() -> None:
    if config.TOKEN_FILE.exists():
        config.TOKEN_FILE.unlink()