"""Auth gates, upload safety, and cross-user isolation regressions."""

import base64
from io import BytesIO


# --- Login gate ---

def test_pages_require_login(client):
    for path in ("/", "/outfits", "/calendar", "/ask", "/shop", "/add"):
        r = client.get(path)
        assert r.status_code == 302 and "/login" in r.headers["Location"], path


def test_uploads_require_login(client):
    # Regression: serve_upload used to be login-exempt, exposing body photos
    # and try-on renders to anyone with a URL.
    r = client.get("/static/uploads/whatever.png")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_healthz_and_login_are_public(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/login").status_code == 200


def test_uploaded_images_are_cached_hard(app_module, logged_in):
    # Regression: uploaded/generated images had no Cache-Control at all, so the
    # browser re-fetched every one on every page load — painfully slow once the
    # catalog grew past a handful of garments. Filenames are minted fresh per
    # write (_unique_name()) and never reused, so long+immutable caching is safe.
    c, uid = logged_in
    (app_module.UPLOAD_DIR / "cache-test.png").write_bytes(b"fake-png-bytes")
    r = c.get("/static/uploads/cache-test.png")
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "private, max-age=31536000, immutable"


def test_signup_creates_session_and_grants_access(client):
    r = client.post("/signup", data={
        "name": "Signup Flow User", "email": "signup-flow@example.com", "password": "correct-horse-battery",
    }, follow_redirects=False)
    assert r.status_code == 302
    assert client.get("/").status_code == 200


def test_signup_rejects_short_password(client):
    r = client.post("/signup", data={
        "name": "Short Pw User", "email": "short-pw@example.com", "password": "short",
    })
    assert r.status_code == 400
    assert "8 characters" in r.get_data(as_text=True)


def test_signup_rejects_duplicate_email(client):
    client.post("/signup", data={
        "name": "Dup User One", "email": "dup@example.com", "password": "correct-horse-battery",
    })
    r = client.post("/signup", data={
        "name": "Dup User Two", "email": "dup@example.com", "password": "correct-horse-battery",
    })
    assert r.status_code == 409
    assert "already exists" in r.get_data(as_text=True)


def test_login_with_correct_password_succeeds(client):
    client.post("/signup", data={
        "name": "Login Pw User", "email": "login-pw@example.com", "password": "correct-horse-battery",
    })
    client.get("/logout")
    r = client.post("/login", data={"email": "login-pw@example.com", "password": "correct-horse-battery"},
                     follow_redirects=False)
    assert r.status_code == 302
    assert client.get("/").status_code == 200


def test_login_with_wrong_password_rejected(client):
    client.post("/signup", data={
        "name": "Wrong Pw User", "email": "wrong-pw@example.com", "password": "correct-horse-battery",
    })
    client.get("/logout")
    r = client.post("/login", data={"email": "wrong-pw@example.com", "password": "not-the-password"})
    assert r.status_code == 401
    assert client.get("/").status_code == 302  # still logged out


def test_login_unknown_email_rejected_generically(client):
    r = client.post("/login", data={"email": "nobody@example.com", "password": "whatever123"})
    assert r.status_code == 401
    assert "Invalid email or password" in r.get_data(as_text=True)


# --- Legacy account migration ---

def test_legacy_session_redirected_to_secure_account(legacy_logged_in):
    c, uid = legacy_logged_in
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 302 and "/secure-account" in r.headers["Location"]


def test_legacy_session_can_reach_logout_and_secure_page_directly(legacy_logged_in):
    c, uid = legacy_logged_in
    assert c.get("/secure-account").status_code == 200
    assert c.get("/logout").status_code == 302


def test_securing_legacy_account_grants_normal_access(legacy_logged_in):
    c, uid = legacy_logged_in
    r = c.post("/secure-account", data={"email": "secured@example.com", "password": "correct-horse-battery"},
               follow_redirects=False)
    assert r.status_code == 302 and "/secure-account" not in r.headers["Location"]
    assert c.get("/").status_code == 200


# --- Forgot / reset password ---

def test_forgot_password_always_returns_generic_success(client):
    r = client.post("/forgot-password", data={"email": "nobody-at-all@example.com"})
    assert r.status_code == 200
    assert "we&#39;ve sent" in r.get_data(as_text=True).lower() or "we've sent" in r.get_data(as_text=True).lower()


def test_reset_password_with_invalid_token_rejected(client):
    r = client.get("/reset-password/not-a-real-token")
    assert r.status_code == 200
    assert "invalid or has expired" in r.get_data(as_text=True)


def test_reset_password_flow_end_to_end(client, app_module):
    client.post("/signup", data={
        "name": "Reset Flow User", "email": "reset-flow@example.com", "password": "correct-horse-battery",
    })
    client.get("/logout")
    token = app_module._reset_serializer.dumps({"uid": app_module.get_user_by_email("reset-flow@example.com")["id"]})
    r = client.post(f"/reset-password/{token}", data={"password": "new-correct-horse"}, follow_redirects=False)
    assert r.status_code == 302
    r2 = client.post("/login", data={"email": "reset-flow@example.com", "password": "new-correct-horse"},
                      follow_redirects=False)
    assert r2.status_code == 302


# --- Google sign-in ---

def test_google_signin_disambiguates_colliding_display_name(client, app_module, monkeypatch):
    # Regression: create_user_google() used to be called with no try/except in
    # google_callback's fresh-signup branch — a Google account whose display
    # name collided (case-insensitively) with an existing user's name raised
    # NameTaken uncaught, 500ing instead of just creating the account under a
    # disambiguated name (the user has no form field to fix Google's name).
    client.post("/signup", data={
        "name": "Name Collision User", "email": "original-owner@example.com", "password": "correct-horse-battery",
    })
    client.get("/logout")

    class FakeGoogleOAuth:
        def authorize_access_token(self):
            return {"id_token": "fake"}

        def parse_id_token(self, token, nonce=None):
            return {
                "sub": "google-sub-collision-1", "email": "name-collision@example.com",
                "email_verified": True, "name": "Name Collision User",
            }

    monkeypatch.setattr(app_module, "google_oauth", FakeGoogleOAuth())
    r = client.get("/auth/google/callback", follow_redirects=False)
    assert r.status_code == 302 and "/login" not in r.headers["Location"]

    new_user = app_module.get_user_by_email("name-collision@example.com")
    assert new_user is not None
    assert new_user["name"] != "Name Collision User"  # disambiguated, not a collision
    assert new_user["name"].startswith("Name Collision User (")


# --- Admin: merge one account's data onto another ---

def test_admin_merge_user_requires_correct_code(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "ACCESS_CODE", "correct-code")
    r = client.post("/admin/merge-user", data={"code": "wrong-code", "from_user_id": "1", "to_user_id": "2"})
    assert r.status_code == 403


def test_admin_merge_user_moves_wardrobe_data(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "ACCESS_CODE", "correct-code")
    from database import (
        add_garment, add_outfit, log_outfit, save_style_profile, add_wishlist_item,
        add_tryon_history, set_body_photo, get_all_garments, get_style_profile,
        get_wishlist_items, get_tryon_history, get_calendar, get_user,
    )

    old = app_module.create_user_password("Old Name User", "old-name-user@example.com", "x")
    new = app_module.create_user_password("New Name User", "new-name-user@example.com", "x")

    add_garment(old["id"], "Test Jacket", "jacket", "navy", "", "", "", [], "", "")
    outfit_id = add_outfit(old["id"], "Test Outfit", "casual", 5, "", [])
    log_outfit(old["id"], "2026-08-01", outfit_id, [], "")
    save_style_profile(old["id"], "Old user's style summary.")
    add_wishlist_item(old["id"], "Test Item", "$10", "Test Store", "http://example.com/x", "", "", "")
    add_tryon_history(old["id"], "uploads/fake.png", [])
    set_body_photo(old["id"], "uploads/fake-body.png")

    r = client.post("/admin/merge-user", data={
        "code": "correct-code", "from_user_id": str(old["id"]), "to_user_id": str(new["id"]),
    })
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["moved"]["garments"] == 1

    assert len(get_all_garments(new["id"])) == 1
    assert len(get_all_garments(old["id"])) == 0
    assert get_style_profile(new["id"])["summary"] == "Old user's style summary."
    assert len(get_wishlist_items(new["id"])) == 1
    assert len(get_tryon_history(new["id"])) == 1
    assert len(get_calendar(new["id"])) == 1
    assert get_user(new["id"])["body_photo_path"] == "uploads/fake-body.png"


# --- Upload helpers ---

def test_safe_ext_whitelist(app_module):
    assert app_module._safe_ext("photo.jpg") == ".jpg"
    assert app_module._safe_ext("photo.PNG") == ".png"
    # Never persist active-content extensions
    assert app_module._safe_ext("evil.html") == ".jpg"
    assert app_module._safe_ext("evil.svg") == ".jpg"
    assert app_module._safe_ext("") == ".jpg"
    assert app_module._safe_ext(None) == ".jpg"


def test_delete_upload_refuses_paths_outside_uploads(app_module):
    from config import DATA_DIR
    # A real file under DATA_DIR but NOT under uploads/ must survive
    target = DATA_DIR / "precious.txt"
    target.write_text("keep me")
    app_module._delete_upload("precious.txt")
    app_module._delete_upload("../precious.txt")
    app_module._delete_upload("uploads/../precious.txt")
    assert target.is_file()
    # ...while a genuine upload is deleted
    inside = app_module.UPLOAD_DIR / "temp.png"
    inside.write_bytes(b"x")
    app_module._delete_upload("uploads/temp.png")
    assert not inside.exists()


# --- Cost caps ---

def test_daily_user_cap_blocks(app_module):
    from database import get_or_create_user, record_image_event
    from config import DAILY_USER_CAP
    user = get_or_create_user("Cap Test User")
    assert app_module._cap_blocked(user["id"]) is None or "limit" in app_module._cap_blocked(user["id"])
    for _ in range(DAILY_USER_CAP):
        record_image_event(user["id"], "cutout")
    msg = app_module._cap_blocked(user["id"])
    assert msg and "limit" in msg


# --- Friendly Gemini errors ---

def test_friendly_gemini_error_mapping(app_module):
    f = app_module._friendly_gemini_error
    assert "busy" in f(Exception("429 RESOURCE_EXHAUSTED quota"))
    assert "unavailable" in f(Exception("403 permission denied on billing"))
    assert "couldn't be processed" in f(Exception("blocked by safety filters"))
    assert "failed" in f(Exception("some novel explosion"))


# --- Cross-user isolation (regression for the outfit/calendar write hole) ---

def _add_garment_for(uid, name):
    from database import add_garment
    return add_garment(user_id=uid, name=name, type_="shirt", color="blue",
                       brand="", fit="", occasion="", tags=[], image_path="",
                       notes="")


def test_outfit_cannot_attach_foreign_garments():
    from database import get_or_create_user, add_outfit, get_outfits, get_garment
    victim = get_or_create_user("Victim User")["id"]
    attacker = get_or_create_user("Attacker User")["id"]
    vg = _add_garment_for(victim, "Victim Shirt")
    ag = _add_garment_for(attacker, "Attacker Shirt")

    oid = add_outfit(attacker, "steal", "", 0, "", [vg, ag, 999999, "junk"])
    outs = [o for o in get_outfits(attacker) if o["id"] == oid]
    names = [g["name"] for g in outs[0]["garments"]]
    assert names == ["Attacker Shirt"], names
    # Victim's worn_count untouched by the attempt
    assert get_garment(vg, victim)["worn_count"] == 0


def test_calendar_cannot_log_foreign_garments_or_outfits():
    from database import (get_or_create_user, add_outfit, log_outfit,
                         get_calendar, get_garment)
    victim = get_or_create_user("Victim User")["id"]
    attacker = get_or_create_user("Attacker User")["id"]
    vg = _add_garment_for(victim, "Victim Jacket")
    ag = _add_garment_for(attacker, "Attacker Jacket")
    victim_outfit = add_outfit(victim, "vfit", "", 0, "", [vg])

    log_outfit(attacker, "2026-07-18", victim_outfit, [vg, ag], "")
    logs = [l for l in get_calendar(attacker) if l["date"] == "2026-07-18"]
    assert logs and logs[0]["garment_ids"] == [ag]
    assert logs[0]["outfit_id"] is None  # foreign outfit id rejected
    assert get_garment(vg, victim)["worn_count"] == 1  # only the victim's own outfit bump


def test_calendar_relog_corrects_worn_count():
    from database import get_or_create_user, log_outfit, get_garment
    uid = get_or_create_user("Relog User")["id"]
    g1 = _add_garment_for(uid, "First")
    g2 = _add_garment_for(uid, "Second")
    log_outfit(uid, "2026-07-01", None, [g1], "")
    log_outfit(uid, "2026-07-01", None, [g2], "")  # correction replaces the day
    assert get_garment(g1, uid)["worn_count"] == 0
    assert get_garment(g2, uid)["worn_count"] == 1


def test_calendar_requires_date(logged_in):
    c, uid = logged_in
    r = c.post("/calendar", json={"garment_ids": []})
    assert r.status_code == 400


# --- Background photo-analyze jobs (regression: tab backgrounding used to kill
# the in-flight Gemini call; the work now runs server-side and is polled) ---

def test_photo_analyze_job_runs_and_can_be_polled(app_module, logged_in, monkeypatch):
    import time as _time
    c, uid = logged_in
    monkeypatch.setattr(app_module, "ANTHROPIC_API_KEY", "fake")
    monkeypatch.setattr(app_module, "OPTIN_CUTOUTS", True)  # skips Gemini reconstruction — just exercises the job plumbing
    monkeypatch.setattr(app_module, "detect_garments_in_photo",
                         lambda photo_bytes: [{"name": "Test Shirt", "type": "shirt", "color": "blue"}])

    r = c.post("/add/photo/analyze", data={'image': (BytesIO(b"fake-bytes"), 'photo.jpg')},
               content_type='multipart/form-data')
    assert r.status_code == 200
    job_id = r.get_json()["job_id"]

    body = {"status": "running"}
    for _ in range(50):
        body = c.get(f"/add/photo/analyze/{job_id}/status").get_json()
        if body["status"] != "running":
            break
        _time.sleep(0.05)
    assert body["status"] == "done"
    assert body["garments"][0]["name"] == "Test Shirt"


def test_photo_analyze_job_reconstructs_eager_cutouts_in_parallel(app_module, logged_in, monkeypatch):
    # Regression: the eager-cutout loop (OPTIN_CUTOUTS=False, the default) used
    # to reconstruct each detected garment's cutout sequentially. Now they run
    # concurrently via a ThreadPoolExecutor — this checks each garment still
    # gets its own correct result (no cross-thread mixing) and each billed item
    # is still recorded exactly once against the cost cap.
    import time as _time
    c, uid = logged_in
    monkeypatch.setattr(app_module, "ANTHROPIC_API_KEY", "fake")
    monkeypatch.setattr(app_module, "GEMINI_API_KEY", "fake")
    monkeypatch.setattr(app_module, "OPTIN_CUTOUTS", False)
    detected = [
        {"name": "Shirt", "type": "shirt", "color": "blue"},
        {"name": "Pants", "type": "pants", "color": "black"},
        {"name": "Shoes", "type": "shoes", "color": "white"},
    ]
    monkeypatch.setattr(app_module, "detect_garments_in_photo", lambda photo_bytes: detected)
    monkeypatch.setattr(app_module, "cutout_cached", lambda photo_bytes, desc: False)
    monkeypatch.setattr(app_module, "reconstruct_garment", lambda photo_bytes, desc: f"png-for-{desc}".encode())
    billed = []
    monkeypatch.setattr(app_module, "record_image_event", lambda uid, kind: billed.append((uid, kind)))

    r = c.post("/add/photo/analyze", data={'image': (BytesIO(b"fake-bytes"), 'photo.jpg')},
               content_type='multipart/form-data')
    assert r.status_code == 200
    job_id = r.get_json()["job_id"]

    body = {"status": "running"}
    for _ in range(50):
        body = c.get(f"/add/photo/analyze/{job_id}/status").get_json()
        if body["status"] != "running":
            break
        _time.sleep(0.05)
    assert body["status"] == "done"
    garments = body["garments"]
    assert len(garments) == 3
    for g in garments:
        expected_desc = f"{g['color']}, {g['name']}"
        assert base64.b64decode(g["cutout_b64"]) == f"png-for-{expected_desc}".encode()
    assert len(billed) == 3


def test_photo_analyze_status_unknown_job_404(logged_in):
    c, uid = logged_in
    assert c.get("/add/photo/analyze/not-a-real-job/status").status_code == 404


def test_photo_analyze_status_rejects_foreign_job(app_module, logged_in):
    from database import get_or_create_user
    c, uid = logged_in
    other_uid = get_or_create_user("Other Photo User")["id"]
    job_id = "some-other-users-job"
    with app_module._photo_jobs_lock:
        app_module._photo_jobs[job_id] = {"status": "done", "user_id": other_uid, "created_at": 0, "garments": []}
    r = c.get(f"/add/photo/analyze/{job_id}/status")
    assert r.status_code == 404


# --- Background try-on jobs (same rationale as photo-analyze, above) ---

def _make_test_image(app_module, name):
    from PIL import Image
    path = app_module.UPLOAD_DIR / name
    Image.new('RGB', (10, 10), color='red').save(path)
    return f"uploads/{name}"


def _setup_tryon_garment(app_module, uid, suffix):
    from database import add_garment, set_body_photo
    body_rel = _make_test_image(app_module, f"body_{uid}_{suffix}.png")
    set_body_photo(uid, body_rel)
    garment_rel = _make_test_image(app_module, f"garment_{uid}_{suffix}.png")
    gid = add_garment(user_id=uid, name="Test Shirt", type_="shirt", color="blue",
                      brand="", fit="", occasion="", tags=[], image_path=garment_rel, notes="")
    return body_rel, gid


def test_tryon_job_runs_and_can_be_polled(app_module, logged_in, monkeypatch):
    import time as _time
    c, uid = logged_in
    monkeypatch.setattr(app_module, "GEMINI_API_KEY", "fake")
    monkeypatch.setattr(app_module, "tryon", lambda body, pngs, names: b"fake-png-bytes")
    _, gid = _setup_tryon_garment(app_module, uid, "poll")

    r = c.post("/suggest/tryon", json={"garment_ids": [gid]})
    assert r.status_code == 200
    job_id = r.get_json()["job_id"]

    result = {"status": "running"}
    for _ in range(50):
        result = c.get(f"/suggest/tryon/{job_id}/status").get_json()
        if result["status"] != "running":
            break
        _time.sleep(0.05)
    assert result["status"] == "done"
    assert result["url"].startswith("/static/uploads/tryon_")


def test_tryon_status_unknown_job_404(logged_in):
    c, uid = logged_in
    assert c.get("/suggest/tryon/not-a-real-job/status").status_code == 404


def test_tryon_status_rejects_foreign_job(app_module, logged_in):
    from database import get_or_create_user
    c, uid = logged_in
    other_uid = get_or_create_user("Other Tryon User")["id"]
    job_id = "some-other-users-tryon-job"
    with app_module._tryon_jobs_lock:
        app_module._tryon_jobs[job_id] = {"status": "done", "user_id": other_uid, "created_at": 0, "url": "/static/x.png"}
    r = c.get(f"/suggest/tryon/{job_id}/status")
    assert r.status_code == 404


def test_tryon_cache_hit_resolves_without_a_job(app_module, logged_in, monkeypatch):
    c, uid = logged_in
    monkeypatch.setattr(app_module, "GEMINI_API_KEY", "fake")
    body_rel, gid = _setup_tryon_garment(app_module, uid, "cache")

    key = app_module._tryon_key(uid, body_rel, [gid])
    cached_rel = f"uploads/{app_module._unique_name('tryon_', '.png')}"
    (app_module.DATA_DIR / cached_rel).write_bytes(b"fake-cached-png")
    app_module._tryon_cache[key] = cached_rel

    r = c.post("/suggest/tryon", json={"garment_ids": [gid]})
    assert r.status_code == 200
    body = r.get_json()
    assert body.get("cached") is True
    assert "job_id" not in body
