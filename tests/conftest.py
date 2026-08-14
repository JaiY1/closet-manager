import os
import sys
import tempfile

# Point all mutable state (DB, uploads, caches) at a throwaway dir and disable
# the access-code gate BEFORE config/app are imported — config reads env at
# import time.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="closet-test-")
os.environ.pop("ACCESS_CODE", None)
# Tell config.py not to redirect HOME at that throwaway dir (it normally does,
# in production, so ChromaDB's ONNX model cache survives redeploys instead of
# re-downloading ~79MB from an ephemeral filesystem every time) — here it
# would force a full fresh download+extract on every single test run instead
# of reusing whatever's already cached under the real dev machine's ~/.cache.
os.environ["CLOSET_MANAGER_TESTING"] = "1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def app_module():
    import app as app_mod
    app_mod.app.config["TESTING"] = True
    return app_mod


@pytest.fixture()
def client(app_module):
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture()
def logged_in(app_module):
    """A test client with an authenticated session for a dedicated test user.
    Has a password set (auth_provider != 'legacy') so it doesn't get bounced to
    the secure-account prompt — use `legacy_logged_in` to test that prompt."""
    from database import get_or_create_user, set_user_password
    user = get_or_create_user("Pytest User")
    set_user_password(user["id"], app_module._hash_password("pytest-password-123"))
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["user_id"] = user["id"]
            s["user_name"] = user["name"]
        yield c, user["id"]


@pytest.fixture()
def legacy_logged_in(app_module):
    """A test client whose session belongs to a pre-auth (name-only) account —
    for testing the 'secure your account' gate itself."""
    from database import get_or_create_user
    user = get_or_create_user("Legacy Pytest User")
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["user_id"] = user["id"]
            s["user_name"] = user["name"]
        yield c, user["id"]
