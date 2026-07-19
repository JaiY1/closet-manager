import os
import sys
import tempfile

# Point all mutable state (DB, uploads, caches) at a throwaway dir and disable
# the access-code gate BEFORE config/app are imported — config reads env at
# import time.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="closet-test-")
os.environ.pop("ACCESS_CODE", None)

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
    """A test client with an authenticated session for a dedicated test user."""
    from database import get_or_create_user
    user = get_or_create_user("Pytest User")
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["user_id"] = user["id"]
            s["user_name"] = user["name"]
        yield c, user["id"]
