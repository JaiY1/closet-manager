import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SERPER_API_KEY = os.getenv("SERPER_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SECRET_KEY = os.getenv("SECRET_KEY")

# All mutable data (SQLite DB, ChromaDB, uploaded/generated images, caches) lives
# under DATA_DIR. Defaults to the project root so local dev is unchanged; set
# DATA_DIR=/data (a mounted volume) in production so redeploys don't wipe data.
DATA_DIR = Path(os.getenv("DATA_DIR") or Path(__file__).resolve().parent).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ChromaDB's ONNX embedding model caches to ~/.cache (Path.home()), which on
# Railway is the container's ephemeral filesystem, not the DATA_DIR volume —
# every redeploy wipes it, forcing a fresh ~79MB download on the next embed
# call. Worse, if two requests both trigger that cold download around the same
# moment (concurrent gthread requests), they race on the same file and one
# crashes with FileNotFoundError mid-verify. Point HOME at DATA_DIR so this
# (and anything else that follows the same convention) persists across
# restarts and only ever downloads once. Must happen before anything imports
# chromadb — its DOWNLOAD_PATH is a class attribute evaluated at import time —
# so this sits here, in the module every other module imports first.
# Skipped under the test suite (conftest.py sets CLOSET_MANAGER_TESTING) —
# DATA_DIR there is a fresh throwaway tempdir every run, so this would force a
# full re-download on every single test run instead of reusing whatever's
# already cached under the real machine's actual home directory.
if not os.getenv("CLOSET_MANAGER_TESTING"):
    os.environ["HOME"] = str(DATA_DIR)


# --- Image-pipeline feature flags -------------------------------------------
# Each flag toggles one accuracy or cost improvement. Set to False (here or via
# the matching env var) to revert to the previous behavior. Defaults are the new
# improved behavior.
def _flag(name, default):
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


# Accuracy
IDENTITY_PROMPT_V2 = _flag("IDENTITY_PROMPT_V2", True)   # B: preserve the real person in try-on
BODY_INPUT_MAXPX   = int(os.getenv("BODY_INPUT_MAXPX", "1536"))  # C: was 1024
CUTOUT_FIDELITY    = _flag("CUTOUT_FIDELITY", True)      # D: hi-res crop + logo-preserve prompt
CUTOUT_RETRY       = _flag("CUTOUT_RETRY", True)         # D: auto-retry hazy/failed cutouts
BODY_PHOTO_GATE    = _flag("BODY_PHOTO_GATE", True)      # A: validate the body photo on upload

# Cost
OPTIN_CUTOUTS      = _flag("OPTIN_CUTOUTS", False)       # F: reconstruct only kept items, on confirm (off by
                                                          # default — see imagegen.py: reconstruction now runs
                                                          # against the full photo, not a bbox crop, so there's
                                                          # no cheap free preview to show without generating)
CACHE_CUTOUTS      = _flag("CACHE_CUTOUTS", True)        # H: cache cutouts by image hash
PERSIST_TRYON      = _flag("PERSIST_TRYON", True)        # E: disk-persist try-on renders
REMBG_HYBRID       = _flag("REMBG_HYBRID", True)         # G: free rembg bg-removal for flat photos

# --- Cost caps (protect the Gemini bill on a public deployment) --------------
# A "generation" is one real (non-cached) Gemini image. At ~$0.04 each, the
# monthly-global default caps spend near $40/mo. Set to 0 to disable a cap.
DAILY_USER_CAP     = int(os.getenv("DAILY_USER_CAP", "40"))     # per user, per day
DAILY_GLOBAL_CAP   = int(os.getenv("DAILY_GLOBAL_CAP", "200"))  # everyone, per day
MONTHLY_GLOBAL_CAP = int(os.getenv("MONTHLY_GLOBAL_CAP", "1000"))  # everyone, per month

# --- Deployment ---
# Legacy shared gate — no longer used for login (real per-user auth replaced it),
# kept only to gate the /admin/import-data escape hatch. Unset locally = no gate.
ACCESS_CODE   = os.getenv("ACCESS_CODE")
# Optional "Report an issue" link shown in the nav when set.
FEEDBACK_EMAIL = os.getenv("FEEDBACK_EMAIL")
# Send session cookies only over HTTPS. Enable in production (behind TLS); keep off
# locally so http://localhost login still works.
SECURE_COOKIES = _flag("SECURE_COOKIES", False)

# --- Auth ---
# Google OAuth (Sign in with Google). Both unset = the Google button is hidden
# and /auth/google routes redirect to login rather than erroring.
GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
# Resend (transactional email) for the forgot-password flow. Unset = reset links
# are logged instead of emailed — fine for local dev, not for production.
RESEND_API_KEY   = os.getenv("RESEND_API_KEY")
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "Closet <onboarding@resend.dev>")
