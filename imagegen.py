"""Image generation layer — Google Gemini 2.5 Flash Image ("nano-banana").

Two jobs:
  1. reconstruct_garment() — turn a cropped garment (worn, occluded) into a clean
     transparent-PNG catalog cutout. Gemini renders it on a solid green screen;
     chroma_key_to_png() then keys the green out to alpha.
  2. tryon() — render the user's own body photo wearing a set of garment cutouts.

Mirrors vision.py's conventions. The Gemini client is created lazily so the app
still boots when GEMINI_API_KEY is unset (routes guard on the key and 503).
"""
import base64
import hashlib
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image
from google import genai
from google.genai import types

from config import (
    GEMINI_API_KEY, IDENTITY_PROMPT_V2, BODY_INPUT_MAXPX,
    CUTOUT_FIDELITY, CUTOUT_RETRY, CACHE_CUTOUTS, DATA_DIR,
)

# nano-banana. Image generation + editing from reference images.
GEMINI_IMAGE_MODEL = "gemini-2.5-flash-image"

# H: cache reconstructed cutouts by input hash so re-analyzing the same photo
# (or re-confirming) never re-bills Gemini. Survives restarts (on disk).
_CACHE_DIR = DATA_DIR / ".image_cache"

_client = None


def _get_client():
    global _client
    if _client is None:
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY not set in .env")
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


def _cache_get(key: str):
    if not CACHE_CUTOUTS:
        return None
    p = _CACHE_DIR / f"{key}.png"
    return p.read_bytes() if p.is_file() else None


def _cache_put(key: str, data: bytes):
    if not CACHE_CUTOUTS:
        return
    _CACHE_DIR.mkdir(exist_ok=True)
    (_CACHE_DIR / f"{key}.png").write_bytes(data)


def _resize_to_jpeg(image_bytes: bytes, max_size: int = 1024) -> bytes:
    """Reference images don't need to be huge; keep token cost sane."""
    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    img.thumbnail((max_size, max_size), Image.LANCZOS)
    out = BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()


def _extract_image(response) -> bytes:
    """Pull the first inline image out of a Gemini generate_content response."""
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in (getattr(content, "parts", None) or []):
            inline = getattr(part, "inline_data", None)
            if inline is not None and getattr(inline, "data", None):
                data = inline.data
                # SDK usually returns raw bytes; tolerate base64 str too.
                if isinstance(data, str):
                    return base64.b64decode(data)
                return data
    raise RuntimeError("Gemini returned no image (possibly refused or safety-filtered)")


# --- Garment cutout ---------------------------------------------------------

_RECONSTRUCT_PROMPT = """The reference photo shows a person wearing a full outfit — multiple garments at once. Find and isolate ONLY this one item among everything they have on: {description}.

Treat every other garment or accessory they're wearing (and their body, face, hair) as unwanted background to remove, exactly like you would remove a room background — the same way you'd exclude anything else that isn't the target item.

Reconstruct the isolated garment as a complete flat product-catalog image:
- Remove the wearer, body, skin, hair, hands, every other clothing layer, and the entire scene/background.
- Show the whole garment alone, front-facing and neatly presented (ghost-mannequin style). Infer parts hidden in the reference ONLY from what the visible fabric and construction clearly imply.
- Preserve exactly the color, material, texture, silhouette, pattern, fastenings, and any logos or lettering that are visible in the reference.
- Do NOT invent any logo, text, label, pocket, seam, fastener, hardware, color, or decoration that the reference does not support.
- Place the garment centered on a completely uniform solid pure-green background (#00ff00) that fills the frame to every edge. No shadow on the background."""

# D: fidelity-focused variant — leans harder on exact color/logo/text preservation.
_RECONSTRUCT_PROMPT_V2 = """The reference photo shows a person wearing a full outfit — multiple garments at once. Find and isolate ONLY this one item among everything they have on: {description}.

Treat every other garment or accessory they're wearing (and their body, face, hair) as unwanted background to remove, exactly like you would remove a room background — the same way you'd exclude anything else that isn't the target item.

Reconstruct the isolated garment as a complete, high-fidelity flat product-catalog image:
- Remove the wearer, body, skin, hair, hands, every other clothing layer, and the entire scene/background.
- Show the whole garment alone, front-facing and neatly presented (ghost-mannequin style). Infer parts hidden in the reference ONLY from what the visible fabric and construction clearly imply.
- Reproduce the EXACT colour(s) and shade — do not lighten, darken, or shift the hue. Match the fabric texture, weave, sheen, and drape precisely.
- Reproduce every visible logo, graphic, text, print, stripe, and trim EXACTLY as positioned and styled in the reference — same wording, same placement, same proportions.
- Preserve the exact silhouette, fit, seams, stitching, pockets, buttons, zips, and hardware that are visible.
- Do NOT invent, add, remove, or restyle any logo, text, pocket, seam, fastener, hardware, colour, or decoration the reference does not clearly show.
- Place the garment centered on a completely uniform solid pure-green background (#00ff00) filling the frame to every edge. No shadow on the background."""

# Gemini honors "solid green background" but softens the exact hue (it renders a
# muted sage, not #00ff00), so we sample the ACTUAL border color rather than assume
# a fixed key. Backgrounds come back near-uniform (border std ~2), so a tight ramp
# keys them out while leaving the garment fully opaque.
_TRANSPARENT_T = 28.0
_OPAQUE_T = 72.0


def generate_garment_cutout(photo_bytes: bytes, description: str) -> bytes:
    """Gemini render of the garment alone on a green screen (not yet keyed).
    photo_bytes is expected to be the full source photo (not a pre-cropped
    region) — Gemini locates and isolates the named garment itself, which is
    far more reliable than pre-cropping to a vision-model-estimated bounding
    box (those are unreliable for this kind of spatial localization).
    D: fidelity mode sends a higher-res reference and a stricter prompt."""
    ref_px = 1280 if CUTOUT_FIDELITY else 1024
    prompt = _RECONSTRUCT_PROMPT_V2 if CUTOUT_FIDELITY else _RECONSTRUCT_PROMPT
    ref = _resize_to_jpeg(photo_bytes, max_size=ref_px)
    response = _get_client().models.generate_content(
        model=GEMINI_IMAGE_MODEL,
        contents=[
            types.Part.from_bytes(data=ref, mime_type="image/jpeg"),
            prompt.format(description=description or "the clothing item"),
        ],
    )
    return _extract_image(response)


def chroma_key_to_png(
    img_bytes: bytes,
    key_rgb=None,
    transparent_threshold: float = 12.0,
    opaque_threshold: float = 220.0,
) -> bytes:
    """Key a solid-color background out to alpha. Ported from the Extract-Clothing
    skill's Pillow fallback: sample the key (or use the border median), distance-
    threshold with a smoothstep ramp, despill the key channel, emit RGBA PNG."""
    img = Image.open(BytesIO(img_bytes)).convert("RGB")
    arr = np.asarray(img).astype(np.float32)
    h, w, _ = arr.shape

    if key_rgb is None:
        b = max(2, min(4, h // 2, w // 2))
        border = np.concatenate([
            arr[:b, :, :].reshape(-1, 3),
            arr[-b:, :, :].reshape(-1, 3),
            arr[:, :b, :].reshape(-1, 3),
            arr[:, -b:, :].reshape(-1, 3),
        ])
        key = np.median(border, axis=0)
    else:
        key = np.array(key_rgb, dtype=np.float32)

    dist = np.sqrt(((arr - key) ** 2).sum(axis=2))
    t = np.clip((dist - transparent_threshold) / (opaque_threshold - transparent_threshold), 0.0, 1.0)
    alpha = (t * t * (3 - 2 * t) * 255).astype(np.uint8)  # smoothstep

    # Despill: in partially-transparent zones, cap the key's dominant channel at
    # the average of the other two so green fringing doesn't bleed onto edges.
    rgb = arr.copy()
    kc = int(np.argmax(key))
    others = [i for i in range(3) if i != kc]
    cap = (rgb[:, :, others[0]] + rgb[:, :, others[1]]) / 2.0
    spill = (alpha < 255) & (rgb[:, :, kc] > cap)
    rgb[:, :, kc] = np.where(spill, cap, rgb[:, :, kc])

    out = np.dstack([rgb.astype(np.uint8), alpha])
    out[alpha == 0] = 0  # fully transparent -> (0,0,0,0)

    result = Image.fromarray(out, "RGBA")
    buf = BytesIO()
    result.save(buf, format="PNG")
    return buf.getvalue()


def _cutout_ok(png_bytes: bytes) -> bool:
    """A usable cutout has real transparency and mostly-clear corners. If the
    chroma key found almost nothing (background hue too close to the garment, or
    Gemini put it on a busy background), this is False and we retry."""
    try:
        a = np.asarray(Image.open(BytesIO(png_bytes)).convert("RGBA"))[:, :, 3]
    except Exception:
        return False
    transp_frac = float((a == 0).mean())
    h, w = a.shape
    corners = [a[1, 1], a[1, w - 2], a[h - 2, 1], a[h - 2, w - 2]]
    clear_corners = sum(1 for c in corners if c < 40)
    return transp_frac >= 0.10 and clear_corners >= 3


def _reconstruct_once(photo_bytes: bytes, description: str) -> bytes:
    raw = generate_garment_cutout(photo_bytes, description)
    return chroma_key_to_png(
        raw, key_rgb=None,
        transparent_threshold=_TRANSPARENT_T, opaque_threshold=_OPAQUE_T,
    )


def _cutout_key(photo_bytes: bytes, description: str) -> str:
    cfg_tag = f"f{int(CUTOUT_FIDELITY)}".encode()
    return hashlib.sha256(photo_bytes + b"|" + (description or "").encode() + b"|" + cfg_tag).hexdigest()


def cutout_cached(photo_bytes: bytes, description: str) -> bool:
    """True if reconstruct_garment would return a cached cutout (no API call).
    Lets callers avoid counting cache hits against cost caps."""
    return _cache_get(_cutout_key(photo_bytes, description)) is not None


def reconstruct_garment(photo_bytes: bytes, description: str, force: bool = False) -> bytes:
    """Full outfit photo -> clean transparent-PNG catalog cutout of one named
    garment (Gemini finds and isolates it — see generate_garment_cutout).
    H: result is cached by input hash. D: retries once if the first cutout is bad.
    force=True skips the cache read (used by "regenerate" to re-roll a fresh cutout)."""
    key = _cutout_key(photo_bytes, description)
    if not force:
        cached = _cache_get(key)
        if cached is not None:
            return cached

    png = _reconstruct_once(photo_bytes, description)
    if CUTOUT_RETRY and not _cutout_ok(png):
        retry = _reconstruct_once(photo_bytes, description)
        if _cutout_ok(retry):
            png = retry

    _cache_put(key, png)
    return png


# --- Catalog normalization (one-off batch) ----------------------------------
# Separate from the worn-outfit import path above. reconstruct_garment assumes the
# reference is a person wearing a full outfit; these catalog items are single-item
# photos (flat-lays, hanger shots, folded, occasionally worn), so they get their
# own prompt plus consistent per-category framing on a fixed square canvas.

_FRAMING_TOPS = {"shirt", "t-shirt", "polo", "hoodie", "sweater", "jacket",
                 "coat", "suit", "blazer", "dress"}
_FRAMING_BOTTOMS = {"trousers", "jeans", "shorts", "chinos", "skirt"}
_FRAMING_FOOT = {"shoes", "sneakers", "boots", "loafers"}


def _framing_for(category: str) -> str:
    c = (category or "").lower()
    if c in _FRAMING_TOPS:
        return ("Present it as an upper-body garment in a straight, symmetrical front view: "
                "collar or neckline at the top, both sleeves (if any) spread evenly and fully "
                "visible, the front panel flat and centered, and the complete hem visible at the bottom.")
    if c in _FRAMING_BOTTOMS:
        return ("Present it as a lower-body garment in a straight front view oriented vertically "
                "(portrait): waistband at the top, both legs (or the full skirt) straight and fully "
                "visible down to the complete hem at the bottom.")
    if c in _FRAMING_FOOT:
        return ("Present the matching PAIR side by side in a consistent elevated three-quarter front "
                "view, both facing the same direction, with toes and soles visible.")
    return ("Present the whole item centered and flat in a straightforward front view, with its full "
            "extent visible.")


_NORMALIZE_PROMPT = """The reference image shows a single clothing item: {description}. It may be laid flat, folded, on a hanger, or worn — ignore how it is presented and ignore any person, hanger, floor, wall, or scene around it.

Recreate this exact item as one clean, flat product-catalog image:
- {framing}
- Remove EVERYTHING that is not the item itself: any person, body, skin, hands, hanger, floor, furniture, wall, props, and the entire background.
- Reproduce the EXACT colour(s) and shade, the fabric texture and drape, the silhouette, seams, stitching, pockets, fastenings, and hardware visible in the reference. Reproduce every visible logo, graphic, text, print, stripe, and trim EXACTLY as positioned and styled — same wording, same placement, same proportions.
- Infer parts hidden in the reference ONLY from what the visible fabric and construction clearly imply. Do NOT invent, add, remove, or restyle any logo, text, pocket, seam, fastener, hardware, colour, or decoration the reference does not clearly show.
- Center the item within a SQUARE frame with generous, even margins on all sides, so every item sits at a consistent scale fully inside the frame with nothing cropped at the edges. Even, neutral studio lighting; no cast shadow.
- Place it on a completely uniform solid pure-green background (#00ff00) filling the frame to every edge. No shadow, gradient, or texture on the background."""


def _generate_normalized(image_bytes: bytes, description: str, framing: str) -> bytes:
    ref = _resize_to_jpeg(image_bytes, max_size=1280)
    response = _get_client().models.generate_content(
        model=GEMINI_IMAGE_MODEL,
        contents=[
            types.Part.from_bytes(data=ref, mime_type="image/jpeg"),
            _NORMALIZE_PROMPT.format(description=description or "the clothing item", framing=framing),
        ],
    )
    return _extract_image(response)


def _normalize_once(image_bytes: bytes, description: str, category: str) -> bytes:
    raw = _generate_normalized(image_bytes, description, _framing_for(category))
    return chroma_key_to_png(
        raw, key_rgb=None,
        transparent_threshold=_TRANSPARENT_T, opaque_threshold=_OPAQUE_T,
    )


def _normalize_key(image_bytes: bytes, description: str, category: str) -> str:
    tag = ("normalize|" + (category or "")).encode()
    return hashlib.sha256(image_bytes + b"|" + (description or "").encode() + b"|" + tag).hexdigest()


def normalize_garment_image(image_bytes: bytes, description: str, category: str = "",
                            force: bool = False) -> bytes:
    """One-off catalog normalization: turn any single-item garment photo (flat,
    hung, folded, or worn) into a consistent transparent-PNG catalog cutout with
    category-consistent framing on a square canvas. Cached in its own namespace so
    it never collides with the worn-outfit import cutouts."""
    key = _normalize_key(image_bytes, description, category)
    if not force:
        cached = _cache_get(key)
        if cached is not None:
            return cached
    png = _normalize_once(image_bytes, description, category)
    if CUTOUT_RETRY and not _cutout_ok(png):
        retry = _normalize_once(image_bytes, description, category)
        if _cutout_ok(retry):
            png = retry
    _cache_put(key, png)
    return png


# --- Virtual try-on ---------------------------------------------------------

_TRYON_PROMPT = """Generate one photorealistic image of the SAME person shown in the first image.

- Keep their face, hair, body shape, proportions, and skin tone unchanged.
- Dress them in the following garments together as one complete outfit: {description}.
- The remaining images are those exact garments (transparent cutouts). Reproduce each garment's real color, pattern, material, and details faithfully — do not restyle or recolor them.
- Natural standing pose, clean neutral studio background, even lighting. Full outfit visible."""

# B: identity-first variant — insists on preserving the specific real person.
_TRYON_PROMPT_V2 = """Photorealistically dress the EXACT person from the first image in a new outfit.

CRITICAL — preserve this specific individual's identity:
- Keep their real face and facial features, hairstyle, skin tone, body type, height, and build EXACTLY as in the first image. This is a real, specific person — NOT a fashion model. Do not beautify, slim, age, restyle, or substitute a different face or physique.
- Keep a natural, front-facing, full-length standing pose.

Outfit:
- Dress them in these garments together as one complete outfit: {description}.
- The remaining images are those exact garments. Reproduce each garment's real colour, pattern, material, fit, and details faithfully — do not recolour or restyle.

Output: one photorealistic full-length image with the whole outfit visible, on a clean neutral studio background with even lighting."""


def tryon(body_bytes: bytes, garment_pngs, description: str) -> bytes:
    """Render the user's body photo wearing the given garment cutouts (PNG bytes).
    B: stronger identity prompt. C: higher-res body reference for more likeness."""
    prompt = _TRYON_PROMPT_V2 if IDENTITY_PROMPT_V2 else _TRYON_PROMPT
    body_ref = _resize_to_jpeg(body_bytes, max_size=BODY_INPUT_MAXPX)
    contents = [types.Part.from_bytes(data=body_ref, mime_type="image/jpeg")]
    for png in garment_pngs:
        contents.append(types.Part.from_bytes(data=png, mime_type="image/png"))
    contents.append(prompt.format(description=description or "the selected garments"))

    response = _get_client().models.generate_content(
        model=GEMINI_IMAGE_MODEL,
        contents=contents,
    )
    return _extract_image(response)


# --- Free local background removal (rembg) ----------------------------------

_rembg_session = None


def remove_bg(image_bytes: bytes) -> bytes:
    """G: free, local background removal via rembg (u2net). Great for flat/product
    shots (removes the floor/wall, keeps the garment). NOTE: on a worn photo it
    keeps the whole person, so it is NOT a substitute for Gemini reconstruction."""
    global _rembg_session
    from rembg import remove, new_session  # heavy import; keep it lazy
    if _rembg_session is None:
        _rembg_session = new_session()
    img = Image.open(BytesIO(image_bytes)).convert("RGBA")
    out = remove(img, session=_rembg_session)
    buf = BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()
