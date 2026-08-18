import re
import requests

from config import SERPER_API_KEY

SERPER_SHOPPING_URL = "https://google.serper.dev/shopping"


def _parse_price(price_str):
    """Extract a float from strings like '$49.99' or '49.99 USD'. Returns None if
    unparseable, so items with no price never get dropped by a max_price filter.
    (Previously returned inf, which the <= filter always dropped — the exact
    opposite of the stated intent.)"""
    if not price_str:
        return None
    match = re.search(r"[\d,]+\.?\d*", str(price_str))
    if not match:
        return None
    return float(match.group().replace(",", ""))


def search_shopping(query: str, num: int = 10, max_price: float = None) -> list[dict]:
    """Search Google Shopping via Serper. Returns [] if no API key or no results."""
    if not SERPER_API_KEY:
        return []

    q = f"{query} under ${max_price:g}" if max_price else query
    try:
        resp = requests.post(
            SERPER_SHOPPING_URL,
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": q, "num": num},
            timeout=10
        )
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code in (401, 403):
            raise RuntimeError("Shopping search failed — check that SERPER_API_KEY in .env is valid.") from e
        if e.response is not None and e.response.status_code == 429:
            raise RuntimeError("Shopping search rate limit hit — try again in a bit.") from e
        raise RuntimeError("Shopping search failed — the search provider returned an error.") from e
    except requests.exceptions.RequestException as e:
        raise RuntimeError("Shopping search failed — could not reach the search provider.") from e

    results = resp.json().get("shopping", [])
    if max_price:
        results = [
            r for r in results
            if (p := _parse_price(r.get("price"))) is None or p <= max_price
        ]
    # Serper's shopping endpoint doesn't honor `num` — it returns as many as it finds
    results = results[:num]

    return [
        {
            "title": r.get("title"),
            "price": r.get("price"),
            "source": r.get("source"),
            "link": r.get("link"),
            "image": r.get("imageUrl"),
        }
        for r in results
    ]
