"""Fetch a single retailer product page for its image + title/price — the
"paste a link" alternative to uploading or screenshotting a photo yourself,
for adding something you just bought online.

Mirrors the og:image-scraping technique already proven in news-aggregator's
scraper.py (same fix for sites that pack the <head> with tracking tags before
the og:image meta, pushing it past a small byte budget).
"""
import ipaddress
import json
import socket
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_MAX_REDIRECTS = 5
_MAX_PAGE_BYTES = 1_500_000   # generous — enough to reach og:image + JSON-LD price on most sites
_MAX_IMAGE_BYTES = 15_000_000  # matches the app's normal upload cap ballpark


class LinkFetchError(Exception):
    """User-facing message — caller falls back to "paste a screenshot instead"."""


def _assert_public_host(url: str):
    """Block requests to private/loopback/link-local addresses (SSRF guard) —
    this fetches whatever URL a logged-in user pastes, so it must never be able
    to reach internal services (cloud metadata endpoints, localhost, etc.)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise LinkFetchError("Only http/https links are supported.")
    host = parsed.hostname
    if not host:
        raise LinkFetchError("That doesn't look like a valid URL.")
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(host))
    except (socket.gaierror, ValueError):
        raise LinkFetchError("Couldn't resolve that URL's host.")
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
        raise LinkFetchError("That URL isn't reachable.")


def _fetch_capped(url: str, max_bytes: int) -> tuple:
    """requests.get() that re-validates the host on every redirect hop (so a
    public URL can't redirect its way into a private address) and caps how much
    of the body gets read. Returns (response, content_bytes) — response is
    already closed, headers are still readable."""
    for _ in range(_MAX_REDIRECTS):
        _assert_public_host(url)
        resp = requests.get(url, headers=_HEADERS, timeout=8, stream=True, allow_redirects=False)
        if resp.is_redirect and resp.headers.get("Location"):
            url = urljoin(url, resp.headers["Location"])
            resp.close()
            continue
        if resp.status_code >= 400:
            resp.close()
            raise LinkFetchError(f"That page returned an error ({resp.status_code}) — check the link.")
        content = b""
        for chunk in resp.iter_content(8192):
            content += chunk
            if len(content) > max_bytes:
                break
        resp.close()
        return resp, content
    raise LinkFetchError("Too many redirects.")


def fetch_product_page(url: str) -> dict:
    """Returns {"image_bytes", "title", "price", "source_url"}. Raises
    LinkFetchError with a user-facing message on any failure."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        _, page_content = _fetch_capped(url, _MAX_PAGE_BYTES)
    except LinkFetchError:
        raise
    except requests.exceptions.RequestException:
        raise LinkFetchError("Couldn't reach that URL — check the link and try again.")

    soup = BeautifulSoup(page_content, "html.parser")
    img_tag = (
        soup.find("meta", property="og:image")
        or soup.find("meta", attrs={"name": "twitter:image"})
        or soup.find("meta", property="og:image:url")
    )
    image_url = img_tag["content"].strip() if img_tag and img_tag.get("content") else None
    if not image_url:
        raise LinkFetchError("Couldn't find a product photo on that page — try pasting a screenshot instead.")
    image_url = urljoin(url, image_url)

    title_tag = soup.find("meta", property="og:title")
    if title_tag and title_tag.get("content"):
        title = title_tag["content"].strip()
    elif soup.title and soup.title.string:
        title = soup.title.string.strip()
    else:
        title = ""

    price = _extract_price(soup)

    try:
        img_resp, image_bytes = _fetch_capped(image_url, _MAX_IMAGE_BYTES)
    except LinkFetchError:
        raise
    except requests.exceptions.RequestException:
        raise LinkFetchError("Found a product photo but couldn't download it — try pasting a screenshot instead.")

    content_type = img_resp.headers.get("Content-Type", "")
    if not content_type.startswith("image/"):
        raise LinkFetchError("That link's photo isn't actually an image — try pasting a screenshot instead.")

    return {
        "image_bytes": image_bytes,
        "title": title,
        "price": price,
        "source_url": url,
    }


def _extract_price(soup) -> str:
    """Best-effort, structured sources only — no free-text $ regex, since a
    wrong guess silently prefilled is worse than an empty field the user fills
    in themselves. Checks schema.org Product/Offer JSON-LD first (most
    reliable, common on Shopify and major retailers), then price meta tags."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (ValueError, TypeError):
            continue
        for node in (data if isinstance(data, list) else [data]):
            if not isinstance(node, dict):
                continue
            offers = node.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            if isinstance(offers, dict) and offers.get("price"):
                currency = offers.get("priceCurrency", "")
                return f"{offers['price']} {currency}".strip()
    for prop in ("product:price:amount", "og:price:amount"):
        tag = soup.find("meta", property=prop)
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""
