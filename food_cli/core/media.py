"""Dish image handling.

Swiggy's menu responses embed image URLs as `[image: https://...]`. Dish search
does NOT include them, so images have to be cross-referenced from the owning
restaurant's menu.

There is no description field anywhere in the API - do not invent one.

Downloaded files land in a predictable directory so a calling agent can hand the
image to whatever renderer it has, rather than reading a path aloud.
"""

from __future__ import annotations

import hashlib
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import paths, security

# Not /tmp: that is world-writable and shared between local users, so a
# predictable filename there can be pre-created or read by anyone on the box.
MEDIA_DIR = Path(os.environ.get("FOOD_CLI_MEDIA")
                 or os.environ.get("SWIGGY_CLI_MEDIA")
                 or paths.subdir("media"))

IMAGE_TAG = re.compile(r"\[image:\s*(?P<url>https?://[^\]\s]+)\s*\]")
# "  - Sample Wrap — ₹179 | Veg, has addons [image: URL] (ID: 111222)"
MENU_ITEM = re.compile(
    r"^\s*[-*]\s*(?P<name>.+?)\s*—\s*₹(?P<price>[\d.]+)\s*\|"
    r"(?P<flags>[^\[\(]*)"
    r"(?:\[image:\s*(?P<image>[^\]\s]+)\s*\])?"
    r"\s*\(ID:\s*(?P<id>\w+)\)",
)


def parse_menu_items(text: str) -> dict[str, dict]:
    """Map itemId -> {name, price, image, flags} from a menu response."""
    items: dict[str, dict] = {}
    for line in (text or "").splitlines():
        m = MENU_ITEM.match(line)
        if not m:
            continue
        g = m.groupdict()
        flags = (g["flags"] or "").strip().strip("|").strip()
        items[g["id"]] = {
            "item_id": g["id"],
            "name": g["name"].strip(),
            "price": float(g["price"]),
            "image_url": g["image"],
            "veg": "non-veg" not in flags.lower() and "veg" in flags.lower(),
            "bestseller": "bestseller" in flags.lower(),
            "has_addons": "addon" in flags.lower(),
        }
    return items


def _filename(url: str) -> str:
    digest = hashlib.sha1(url.encode()).hexdigest()[:16]
    ext = ".jpg"
    tail = url.split("?")[0].rsplit(".", 1)
    # The extension comes from a remote URL, so allow only plain alphanumerics -
    # a "/" or ".." in it would otherwise steer the write somewhere unintended.
    if len(tail) == 2 and 2 <= len(tail[1]) <= 5 and tail[1].isalnum():
        ext = "." + tail[1].lower()
    return f"{digest}{ext}"


def download(url: str, dest_dir: Path = MEDIA_DIR) -> str | None:
    """Fetch one image. Cached by URL hash, so repeat calls are free."""
    if not url:
        return None
    security.secure_dir(dest_dir)
    path = dest_dir / _filename(url)
    if path.exists() and not path.is_symlink() and path.stat().st_size > 0:
        return str(path)

    # URL came from a tool response: validate scheme/host and cap the body
    # before writing anything to disk.
    body = security.safe_get(url, timeout=20)
    if not body:
        return None
    try:
        security.secure_write_bytes(path, body)
    except OSError:
        return None
    return str(path)


def download_many(urls: list[str], dest_dir: Path = MEDIA_DIR, workers: int = 8) -> dict[str, str]:
    """Fetch images concurrently. Returns {url: local_path} for successes only."""
    unique = [u for u in dict.fromkeys(urls) if u]
    if not unique:
        return {}
    out: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for url, path in zip(unique, pool.map(lambda u: download(u, dest_dir), unique)):
            if path:
                out[url] = path
    return out
