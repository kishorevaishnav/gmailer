from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "gmailer.db"

# Google auto-grants the identity scopes (openid/email/profile) for installed
# apps in the token response. Request them explicitly so oauthlib's scope check
# on the exchange passes instead of aborting with "Scope has changed".
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.modify",
]

CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"

HOST = "localhost"
PORT = 8000
AUTH_REDIRECT_URI = f"http://{HOST}:{PORT}/auth/callback"

DEFAULT_BATCH = 200
MAX_BATCH = 500

# Kept below Gmail's ~50 messages.get/sec per-user quota so concurrent
# metadata/action batches don't hammer into repeated 429 rate-limit errors.
METADATA_WORKERS = 8

BODY_MAX_CHARS = 80_000

# Local Ollama LLM used for AI summaries (set OLLAMA_MODEL to your best model).
OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "gemma3:4b"
SUMMARY_MAX_BODY_CHARS = 6000
SUMMARY_TIMEOUT_SECONDS = 30

# Grouped-sender summaries: cap each email body and group size so one
# combined Ollama call stays within the model's context window.
GROUP_SUMMARY_BODY_CHARS = 1500
GROUP_MAX_EMAILS = 15
GROUP_PREVIEW_CHARS = 600

# WikiSkill rules & wiki tuning
TRACE_WINDOW_SECONDS = 30 * 86400          # prefilter looks back this far
TRACE_RETENTION_DAYS = 90
MIN_ACTIONS_FOR_PATTERN = 3                 # minimum same-sender actions
PROMO_RATIO_FOR_PATTERN = 0.9               # >=90% promos => promo-only rule
PATTERN_RECENT_DAYS = 7                     # at least one action this recent
RECENT_SAMPLE_SIZE = 5                      # latest N messages for promo ratio
KEYWORD_MIN_LEN = 4
FREQ_EMAILS_PER_DAY = 3                     # volume threshold
# Queue AI summarization: how many emails to pre-summarize when the queue
# loads (sequential). 0 = disabled. Higher = more pre-computed but slower load.
QUEUE_SUMMARIZE_BATCH = 50
EVOLVE_MAX_CLUSTERS = 10
PROPOSAL_SUPPRESS_DAYS = 30