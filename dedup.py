import io
import re
import logging
import hashlib
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
import httpx

try:
    from PIL import Image
    import imagehash
    _HAS_IMAGEHASH = True
except Exception:
    _HAS_IMAGEHASH = False


TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "_ga", "_gl", "ref", "referrer",
    "ixid", "ixlib",  # unsplash tracking
}


def canonicalise_url(url: str) -> str:
    if not url:
        return url
    try:
        p = urlparse(url.strip())
    except Exception:
        return url
    scheme = "https" if p.scheme in ("http", "https") else p.scheme
    netloc = p.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = re.sub(r"/+$", "", p.path) or "/"
    qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k not in TRACKING_PARAMS]
    qs.sort()
    return urlunparse((scheme, netloc, path, "", urlencode(qs), ""))


def url_fingerprint(url: str) -> str:
    return hashlib.sha1(canonicalise_url(url).encode("utf-8")).hexdigest()


def fetch_thumb(url: str, timeout: float = 6.0, max_bytes: int = 800_000) -> bytes:
    with httpx.Client(follow_redirects=True, timeout=timeout) as c:
        r = c.get(url, headers={"User-Agent": "xdm-agent/0.1"})
        if r.status_code >= 400:
            raise Exception(f"thumb fetch {r.status_code}: {url}")
        data = r.content
        if len(data) > max_bytes:
            data = data[:max_bytes]
        return data


# Skip phashing any image that would decode to more than this many pixels. JPEGs
# are scale-decoded via draft() below, but PNG/WebP/GIF have no draft support and
# decode at full resolution — a small-but-huge-dimension non-JPEG could allocate
# hundreds of MB of RGB and OOM the container. Dimensions are read from the
# header (cheap) before any pixel buffer is allocated. ~24 MP keeps the peak
# bounded even at PHASH_CONCURRENCY; a skipped image just misses near-dup checks.
_MAX_PHASH_PIXELS = 24_000_000


def phash_url(url: str) -> str:
    if not _HAS_IMAGEHASH:
        return ""
    try:
        data = fetch_thumb(url)
        img = Image.open(io.BytesIO(data))
        # Decode JPEGs at reduced scale (no-op for other formats) so a high-res
        # image doesn't expand to tens of MB of RGB in memory before we shrink
        # it. With phash run many-at-once this is what keeps dedupe under the
        # container memory limit.
        try:
            img.draft("RGB", (256, 256))
        except Exception:
            pass
        w, h = img.size  # post-draft for JPEG; full size for other formats
        if w * h > _MAX_PHASH_PIXELS:
            logging.info(f"phash_url skip oversized {w}x{h}: {url}")
            return ""
        img = img.convert("RGB")
        img.thumbnail((256, 256))
        return str(imagehash.phash(img))
    except Exception as exc:
        logging.info(f"phash_url failed for {url}: {exc}")
        return ""


def phash_distance(a: str, b: str) -> int:
    if not a or not b or len(a) != len(b):
        return 64
    try:
        return imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b)
    except Exception:
        return 64


def is_near_duplicate(a_phash: str, b_phash: str, threshold: int = 6) -> bool:
    return phash_distance(a_phash, b_phash) <= threshold
