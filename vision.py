import base64
import json
from io import BytesIO

from PIL import Image
import anthropic

from config import ANTHROPIC_API_KEY

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

_LABEL_PROMPT = """Look at this clothing label/tag photo and extract the details printed on it.

- brand: brand name exactly as printed, or empty string if not visible
- size: size exactly as printed, e.g. 'M', 'L', '32x32', '10', or empty string if not visible
- material: fabric composition exactly as printed, e.g. '100% cotton' or '80% wool, 20% polyester', or empty string
- care: brief care summary if visible, e.g. 'machine wash cold', or empty string
- origin: country of origin if printed, or empty string"""

_LABEL_SCHEMA = {
    "type": "object",
    "properties": {
        "brand": {"type": "string"},
        "size": {"type": "string"},
        "material": {"type": "string"},
        "care": {"type": "string"},
        "origin": {"type": "string"}
    },
    "required": ["brand", "size", "material", "care", "origin"],
    "additionalProperties": False
}


def autotag_label(image_bytes: bytes) -> dict:
    jpeg_bytes = _resize_to_jpeg(image_bytes)
    b64 = base64.standard_b64encode(jpeg_bytes).decode("utf-8")

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=200,
        output_config={"format": {"type": "json_schema", "schema": _LABEL_SCHEMA}},
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": _LABEL_PROMPT}
            ]
        }]
    )

    return json.loads(response.content[0].text)


_AUTOTAG_PROMPT = """Look at this clothing item and extract its details.

- name: short descriptive name for this item
- type: the garment type
- color: primary color or color combination
- brand: brand name if visible on label or logo, otherwise empty string
- fit: the fit, or empty string if unclear
- occasion: the occasion it suits, or empty string if unclear
- notes: any notable details worth remembering (fabric texture, pattern, distinctive features)"""

_AUTOTAG_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "type": {"type": "string", "enum": [
            "shirt", "t-shirt", "polo", "hoodie", "sweater", "jacket", "coat",
            "trousers", "jeans", "shorts", "chinos", "suit", "blazer", "dress",
            "skirt", "shoes", "sneakers", "boots", "loafers", "belt", "bag",
            "accessory", "other"
        ]},
        "color": {"type": "string"},
        "brand": {"type": "string"},
        "fit": {"type": "string", "enum": ["slim", "regular", "relaxed", "oversized", ""]},
        "occasion": {"type": "string", "enum": ["casual", "smart-casual", "formal", "athletic", ""]},
        "notes": {"type": "string"}
    },
    "required": ["name", "type", "color", "brand", "fit", "occasion", "notes"],
    "additionalProperties": False
}


def _resize_to_jpeg(image_bytes: bytes, max_size: int = 768) -> bytes:
    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    img.thumbnail((max_size, max_size), Image.LANCZOS)
    out = BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue()


_DESCRIBE_OUTFIT_PROMPT = """Look at this photo of a person wearing an outfit.
List each visible garment on its own line. For each one write: type, color, pattern, fit, and any brand or logo you can see.
Use plain text only — no markdown, no bullet points, no headers. One line per garment."""


def describe_outfit(image_bytes: bytes) -> str:
    jpeg_bytes = _resize_to_jpeg(image_bytes)
    b64 = base64.standard_b64encode(jpeg_bytes).decode("utf-8")
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=200,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
            {"type": "text", "text": _DESCRIBE_OUTFIT_PROMPT}
        ]}]
    )
    return response.content[0].text.strip()


_DETECT_PROMPT = """Look at this photo of a person wearing an outfit. Identify each distinct wearable garment or accessory they have on.

For EACH item, return:
- name: short descriptive name
- type: the garment type
- color: primary color or combination
- brand: brand/logo if visible, else empty string
- fit: the fit, or empty string if unclear
- occasion: the occasion it suits, or empty string if unclear
- notes: notable details (pattern, texture, distinctive features)
- bbox: the item's bounding box in the photo as [left, top, right, bottom], each a fraction from 0.0 to 1.0 (0,0 is top-left). Make the box generous enough to include the whole visible garment.

Only list clothing/footwear/accessories actually worn. Ignore the background, furniture, and body parts."""

_DETECT_SCHEMA = {
    "type": "object",
    "properties": {
        "garments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": [
                        "shirt", "t-shirt", "polo", "hoodie", "sweater", "jacket", "coat",
                        "trousers", "jeans", "shorts", "chinos", "suit", "blazer", "dress",
                        "skirt", "shoes", "sneakers", "boots", "loafers", "belt", "bag",
                        "accessory", "other"
                    ]},
                    "color": {"type": "string"},
                    "brand": {"type": "string"},
                    "fit": {"type": "string", "enum": ["slim", "regular", "relaxed", "oversized", ""]},
                    "occasion": {"type": "string", "enum": ["casual", "smart-casual", "formal", "athletic", ""]},
                    "notes": {"type": "string"},
                    "bbox": {
                        "type": "array",
                        "items": {"type": "number"}
                    }
                },
                "required": ["name", "type", "color", "brand", "fit", "occasion", "notes", "bbox"],
                "additionalProperties": False
            }
        }
    },
    "required": ["garments"],
    "additionalProperties": False
}


def detect_garments_in_photo(image_bytes: bytes) -> list:
    """From an outfit photo, return one dict per worn garment with autotag fields
    plus a normalized [left, top, right, bottom] bbox used to crop each item."""
    jpeg_bytes = _resize_to_jpeg(image_bytes, max_size=1024)
    b64 = base64.standard_b64encode(jpeg_bytes).decode("utf-8")

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1500,
        output_config={"format": {"type": "json_schema", "schema": _DETECT_SCHEMA}},
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": _DETECT_PROMPT}
            ]
        }]
    )

    return json.loads(response.content[0].text)["garments"]


_BODY_CHECK_PROMPT = """Look at this photo, which someone wants to use as their reference photo for a virtual clothing try-on. Assess whether it's a good reference.

Report:
- single_person: true if exactly one person is the clear subject
- face_visible: true if their face is clearly visible and not covered (e.g. not blocked by a phone/hand/mask)
- full_body: true if roughly their whole body head-to-feet is in frame
- front_facing: true if they face the camera (not turned away or in profile)
- clear: true if well-lit and not blurry
A plain, uncluttered background is ideal but not required."""

_BODY_CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "single_person": {"type": "boolean"},
        "face_visible": {"type": "boolean"},
        "full_body": {"type": "boolean"},
        "front_facing": {"type": "boolean"},
        "clear": {"type": "boolean"},
    },
    "required": ["single_person", "face_visible", "full_body", "front_facing", "clear"],
    "additionalProperties": False,
}


def check_body_photo(image_bytes: bytes) -> dict:
    """Judge whether a photo is a good try-on reference. Returns the raw booleans
    plus an `ok` flag and a human-readable `warning` (empty when fine)."""
    jpeg_bytes = _resize_to_jpeg(image_bytes, max_size=768)
    b64 = base64.standard_b64encode(jpeg_bytes).decode("utf-8")
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=200,
        output_config={"format": {"type": "json_schema", "schema": _BODY_CHECK_SCHEMA}},
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": _BODY_CHECK_PROMPT}
            ]
        }]
    )
    r = json.loads(response.content[0].text)
    problems = []
    if not r.get("single_person"):
        problems.append("more than one person (or no clear subject)")
    if not r.get("face_visible"):
        problems.append("your face isn't clearly visible")
    if not r.get("full_body"):
        problems.append("it's not a full-body shot")
    if not r.get("front_facing"):
        problems.append("you're not facing the camera")
    if not r.get("clear"):
        problems.append("it's a bit dark or blurry")
    # Face + full-body are what actually break likeness; treat those as blocking-ish.
    r["ok"] = r.get("face_visible", False) and r.get("full_body", False) and r.get("single_person", False)
    r["warning"] = (
        "For a better try-on likeness, try another photo — " + "; ".join(problems) + "."
        if problems else ""
    )
    return r


def autotag_garment(image_bytes: bytes) -> dict:
    jpeg_bytes = _resize_to_jpeg(image_bytes)
    b64 = base64.standard_b64encode(jpeg_bytes).decode("utf-8")

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=350,
        output_config={"format": {"type": "json_schema", "schema": _AUTOTAG_SCHEMA}},
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}
                },
                {"type": "text", "text": _AUTOTAG_PROMPT}
            ]
        }]
    )

    return json.loads(response.content[0].text)
