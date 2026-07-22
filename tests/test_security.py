"""Auth gates, upload safety, and cross-user isolation regressions."""

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


def test_login_creates_session_and_grants_access(client):
    r = client.post("/login", data={"name": "Login Flow User"}, follow_redirects=False)
    assert r.status_code == 302
    assert client.get("/").status_code == 200


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
