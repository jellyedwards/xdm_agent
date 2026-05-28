import os
import re
import logging
import json
import urllib.parse
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple
from xml.etree import ElementTree as ET
import httpx
import requests
from dotenv import load_dotenv

from storage import Candidate, now_iso, new_id
from dedup import canonicalise_url

load_dotenv()

UA = "xdm-agent/0.1 (+https://github.com/jpleonard/xdm-agent)"
HTTP_TIMEOUT = 12.0


SOURCE_REGISTRY: Dict[str, Dict[str, Any]] = {
    "unsplash": {
        "display_name": "Unsplash", "kind": "stock", "hotlink_ok": True,
        "license_default": "Unsplash License",
        "license_url": "https://unsplash.com/license",
        "attribution_template": "Photo by {creator} on Unsplash",
        "rights_status_default": "clear",
    },
    "pexels": {
        "display_name": "Pexels", "kind": "stock", "hotlink_ok": True,
        "license_default": "Pexels License",
        "license_url": "https://www.pexels.com/license/",
        "attribution_template": "Photo by {creator} from Pexels",
        "rights_status_default": "clear",
    },
    "pixabay": {
        "display_name": "Pixabay", "kind": "stock", "hotlink_ok": True,
        "license_default": "Pixabay License",
        "license_url": "https://pixabay.com/service/license-summary/",
        "attribution_template": "Image by {creator} from Pixabay",
        "rights_status_default": "clear",
    },
    "met": {
        "display_name": "The Met", "kind": "museum", "hotlink_ok": True,
        "license_default": "Public Domain (Met OA)",
        "license_url": "https://www.metmuseum.org/about-the-met/policies-and-documents/open-access",
        "attribution_template": "{title}, {creator}, The Met",
        "rights_status_default": "clear",
    },
    "smithsonian": {
        "display_name": "Smithsonian", "kind": "museum", "hotlink_ok": True,
        "license_default": "CC0 (Smithsonian Open Access)",
        "license_url": "https://www.si.edu/openaccess",
        "attribution_template": "{title}, Smithsonian",
        "rights_status_default": "clear",
    },
    "europeana": {
        "display_name": "Europeana", "kind": "museum", "hotlink_ok": True,
        "license_default": "Per-item (open subset)",
        "license_url": "https://www.europeana.eu/en/rights",
        "attribution_template": "{title} — {creator} ({provider})",
        "rights_status_default": "caveat",
    },
    "nasa": {
        "display_name": "NASA", "kind": "museum", "hotlink_ok": True,
        "license_default": "Public Domain (NASA)",
        "license_url": "https://www.nasa.gov/nasa-brand-center/images-and-media/",
        "attribution_template": "{title} / NASA",
        "rights_status_default": "clear",
    },
    "wikimedia": {
        "display_name": "Wikimedia Commons", "kind": "museum", "hotlink_ok": True,
        "license_default": "Per-item (mostly CC BY-SA)",
        "license_url": "https://commons.wikimedia.org/wiki/Commons:Licensing",
        "attribution_template": "{title} — {creator}, Wikimedia Commons",
        "rights_status_default": "caveat",
    },
    "google_cse": {
        "display_name": "Google Search", "kind": "web", "hotlink_ok": False,
        "license_default": "Unknown — web search",
        "license_url": None,
        "attribution_template": "via {source_host}",
        "rights_status_default": "unknown",
    },
    "vertex_search": {
        "display_name": "Vertex AI Search", "kind": "web", "hotlink_ok": False,
        "license_default": "Unknown — Vertex AI Search",
        "license_url": None,
        "attribution_template": "via {source_host}",
        "rights_status_default": "unknown",
    },
    "serpapi": {
        "display_name": "Web search", "kind": "web", "hotlink_ok": False,
        "license_default": "Unknown — web search",
        "license_url": None,
        "attribution_template": "via {source_host}",
        "rights_status_default": "unknown",
    },
    "evident_ioty": {
        "display_name": "Evident IOTY", "kind": "competition", "hotlink_ok": True,
        "license_default": "Editorial/personal use only (Evident IOTY)",
        "license_url": "https://evidentscientific.com/en/ioty-gallery",
        "attribution_template": "{title} — {creator} ({affiliation}), Evident IOTY {year}",
        "rights_status_default": "caveat",
    },
    "nikon_small_world": {
        "display_name": "Nikon Small World", "kind": "competition", "hotlink_ok": True,
        "license_default": "Editorial use (Nikon Small World)",
        "license_url": "https://www.nikonsmallworld.com/about-us/copyright",
        "attribution_template": "{title} — {creator}, Nikon Small World {year}",
        "rights_status_default": "caveat",
    },
}


def _candidate(mindset_id: str, source_id: str, image_url: str, source_page_url: str, **extra) -> Candidate:
    src = SOURCE_REGISTRY.get(source_id, {})
    return Candidate(
        mindset_id=mindset_id,
        image_url=image_url,
        source_page_url=source_page_url,
        source_id=source_id,
        license_name=extra.pop("license_name", src.get("license_default")),
        license_url=extra.pop("license_url", src.get("license_url")),
        rights_status=extra.pop("rights_status", src.get("rights_status_default", "unknown")),
        **extra,
    )


def _attribution(source_id: str, **fields) -> str:
    t = SOURCE_REGISTRY.get(source_id, {}).get("attribution_template", "")
    try:
        return t.format(**{k: (v or "") for k, v in fields.items()})
    except Exception:
        return t


def search_unsplash(mindset_id: str, query: str, n: int = 10) -> List[Candidate]:
    logging.info(f"search_unsplash: {query=} {n=}")
    key = os.environ.get("UNSPLASH_ACCESS_KEY")
    if not key:
        return []
    r = requests.get(
        "https://api.unsplash.com/search/photos",
        params={"query": query, "per_page": min(n, 30), "content_filter": "high"},
        headers={"Authorization": f"Client-ID {key}", "User-Agent": UA},
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code != 200:
        raise Exception(f"unsplash {r.status_code}: {r.text[:200]}")
    out = []
    for p in r.json().get("results", []):
        u = p["urls"]["regular"]
        creator = p["user"]["name"]
        out.append(_candidate(
            mindset_id, "unsplash", u, p["links"]["html"],
            thumbnail_url=p["urls"]["small"],
            title=p.get("description") or p.get("alt_description") or "",
            caption=p.get("alt_description") or "",
            creator=creator, creator_url=p["user"]["links"]["html"],
            attribution=_attribution("unsplash", creator=creator),
        ))
    return out


def search_pexels(mindset_id: str, query: str, n: int = 10) -> List[Candidate]:
    logging.info(f"search_pexels: {query=} {n=}")
    key = os.environ.get("PEXELS_API_KEY")
    if not key:
        return []
    r = requests.get(
        "https://api.pexels.com/v1/search",
        params={"query": query, "per_page": min(n, 80)},
        headers={"Authorization": key, "User-Agent": UA},
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code != 200:
        raise Exception(f"pexels {r.status_code}: {r.text[:200]}")
    out = []
    for p in r.json().get("photos", []):
        u = p["src"]["large"]
        creator = p["photographer"]
        out.append(_candidate(
            mindset_id, "pexels", u, p["url"],
            thumbnail_url=p["src"]["medium"],
            title=p.get("alt") or "",
            creator=creator, creator_url=p.get("photographer_url"),
            attribution=_attribution("pexels", creator=creator),
        ))
    return out


def search_pixabay(mindset_id: str, query: str, n: int = 10) -> List[Candidate]:
    logging.info(f"search_pixabay: {query=} {n=}")
    key = os.environ.get("PIXABAY_API_KEY")
    if not key:
        return []
    r = requests.get(
        "https://pixabay.com/api/",
        params={"key": key, "q": query, "image_type": "photo", "per_page": min(max(n, 3), 200), "safesearch": "true"},
        headers={"User-Agent": UA},
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code != 200:
        raise Exception(f"pixabay {r.status_code}: {r.text[:200]}")
    out = []
    for p in r.json().get("hits", []):
        u = p.get("largeImageURL") or p.get("webformatURL")
        if not u:
            continue
        creator = p.get("user", "")
        out.append(_candidate(
            mindset_id, "pixabay", u, p.get("pageURL", ""),
            thumbnail_url=p.get("webformatURL"),
            title=" ".join((p.get("tags") or "").split(",")[:3]).strip(),
            creator=creator,
            attribution=_attribution("pixabay", creator=creator),
        ))
    return out


def search_met(mindset_id: str, query: str, n: int = 10) -> List[Candidate]:
    logging.info(f"search_met: {query=} {n=}")
    s = requests.get(
        "https://collectionapi.metmuseum.org/public/collection/v1/search",
        params={"q": query, "hasImages": "true"},
        headers={"User-Agent": UA},
        timeout=HTTP_TIMEOUT,
    )
    if s.status_code != 200:
        raise Exception(f"met search {s.status_code}: {s.text[:200]}")
    ids = (s.json().get("objectIDs") or [])[: n * 3]
    out = []
    for oid in ids:
        if len(out) >= n:
            break
        try:
            o = requests.get(
                f"https://collectionapi.metmuseum.org/public/collection/v1/objects/{oid}",
                headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT,
            )
            if o.status_code != 200:
                continue
            d = o.json()
            if not d.get("isPublicDomain"):
                continue
            u = d.get("primaryImage") or d.get("primaryImageSmall")
            if not u:
                continue
            title = d.get("title") or ""
            creator = d.get("artistDisplayName") or "Unknown"
            year = None
            try:
                year = int((d.get("objectDate") or "").split("–")[0].strip()[:4])
            except Exception:
                pass
            out.append(_candidate(
                mindset_id, "met", u, d.get("objectURL", ""),
                thumbnail_url=d.get("primaryImageSmall"),
                title=title, creator=creator, year=year,
                caption=d.get("objectName") or "",
                attribution=_attribution("met", title=title, creator=creator),
            ))
        except Exception as exc:
            logging.info(f"met object {oid} failed: {exc}")
    return out


def search_smithsonian(mindset_id: str, query: str, n: int = 10) -> List[Candidate]:
    logging.info(f"search_smithsonian: {query=} {n=}")
    key = os.environ.get("SMITHSONIAN_API_KEY", "")
    params = {"q": f"({query}) AND online_media_type:\"Images\" AND content.descriptiveNonRepeating.metadata_usage.access:\"CC0\"", "rows": min(n * 3, 30)}
    if key:
        params["api_key"] = key
    r = requests.get(
        "https://api.si.edu/openaccess/api/v1.0/search",
        params=params, headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT,
    )
    if r.status_code != 200:
        logging.info(f"smithsonian {r.status_code}: {r.text[:200]}")
        return []
    rows = r.json().get("response", {}).get("rows", [])
    out = []
    for row in rows:
        try:
            content = row.get("content", {})
            media = (content.get("descriptiveNonRepeating", {}).get("online_media") or {}).get("media") or []
            if not media:
                continue
            u = None
            for m in media:
                if m.get("type") == "Images":
                    res = m.get("resources") or []
                    # Prefer "Screen" / "High-resolution JPEG" type
                    pick = next((x for x in res if x.get("label", "").lower().startswith("screen")), None) or next(iter(res), None)
                    if pick:
                        u = pick.get("url")
                        break
            if not u:
                continue
            title = row.get("title") or ""
            creator = (content.get("freetext", {}).get("name") or [{}])[0].get("content", "Smithsonian")
            page = (row.get("content", {}).get("descriptiveNonRepeating", {}).get("record_link")) or row.get("url") or ""
            out.append(_candidate(
                mindset_id, "smithsonian", u, page,
                title=title, creator=creator,
                attribution=_attribution("smithsonian", title=title),
            ))
            if len(out) >= n:
                break
        except Exception as exc:
            logging.info(f"smithsonian row failed: {exc}")
    return out


def search_europeana(mindset_id: str, query: str, n: int = 10) -> List[Candidate]:
    logging.info(f"search_europeana: {query=} {n=}")
    key = os.environ.get("EUROPEANA_API_KEY", "")
    if not key:
        # Europeana allows anonymous in some flows; their docs prefer a key.
        return []
    r = requests.get(
        "https://api.europeana.eu/record/v2/search.json",
        params={
            "wskey": key, "query": query, "media": "true", "thumbnail": "true",
            "reusability": "open", "qf": "TYPE:IMAGE", "rows": min(n, 40),
        },
        headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT,
    )
    if r.status_code != 200:
        logging.info(f"europeana {r.status_code}: {r.text[:200]}")
        return []
    items = r.json().get("items", [])
    out = []
    for it in items:
        u = (it.get("edmIsShownBy") or [None])[0] or (it.get("edmPreview") or [None])[0]
        if not u:
            continue
        title = (it.get("title") or [""])[0]
        creator = ", ".join(it.get("dcCreator") or []) or "Unknown"
        provider = ", ".join(it.get("dataProvider") or []) or ""
        guid = it.get("guid") or ""
        rights = (it.get("rights") or [""])[0]
        out.append(_candidate(
            mindset_id, "europeana", u, guid,
            thumbnail_url=(it.get("edmPreview") or [None])[0],
            title=title, creator=creator,
            license_name=rights or SOURCE_REGISTRY["europeana"]["license_default"],
            license_url=rights or SOURCE_REGISTRY["europeana"]["license_url"],
            rights_status="clear" if "creativecommons" in (rights or "").lower() or "publicdomain" in (rights or "").lower() else "caveat",
            attribution=_attribution("europeana", title=title, creator=creator, provider=provider),
        ))
    return out


def search_nasa(mindset_id: str, query: str, n: int = 10) -> List[Candidate]:
    logging.info(f"search_nasa: {query=} {n=}")
    r = requests.get(
        "https://images-api.nasa.gov/search",
        params={"q": query, "media_type": "image", "page_size": min(n * 2, 50)},
        headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT,
    )
    if r.status_code != 200:
        raise Exception(f"nasa {r.status_code}: {r.text[:200]}")
    out = []
    for it in r.json().get("collection", {}).get("items", []):
        d = (it.get("data") or [{}])[0]
        nasa_id = d.get("nasa_id")
        if not nasa_id:
            continue
        u = f"https://images-assets.nasa.gov/image/{nasa_id}/{nasa_id}~small.jpg"
        title = d.get("title") or ""
        page = f"https://images.nasa.gov/details/{nasa_id}"
        out.append(_candidate(
            mindset_id, "nasa", u, page,
            title=title, creator=d.get("photographer") or d.get("center") or "NASA",
            attribution=_attribution("nasa", title=title),
        ))
        if len(out) >= n:
            break
    return out


def search_wikimedia(mindset_id: str, query: str, n: int = 10) -> List[Candidate]:
    logging.info(f"search_wikimedia: {query=} {n=}")
    r = requests.get(
        "https://commons.wikimedia.org/w/api.php",
        params={
            "action": "query", "format": "json", "generator": "search",
            "gsrsearch": f"{query} filetype:bitmap", "gsrlimit": min(n * 2, 50),
            "prop": "imageinfo", "iiprop": "url|extmetadata|user", "iiurlwidth": 1024,
        },
        headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT,
    )
    if r.status_code != 200:
        raise Exception(f"wikimedia {r.status_code}: {r.text[:200]}")
    pages = (r.json().get("query") or {}).get("pages") or {}
    out = []
    for _, p in pages.items():
        ii = (p.get("imageinfo") or [{}])[0]
        u = ii.get("thumburl") or ii.get("url")
        if not u:
            continue
        meta = ii.get("extmetadata") or {}
        lic = (meta.get("LicenseShortName") or {}).get("value") or ""
        rights = "clear" if any(k in lic.lower() for k in ("cc0", "public domain", "cc by")) else "caveat"
        creator = re.sub(r"<[^>]+>", "", (meta.get("Artist") or {}).get("value") or ii.get("user") or "")
        title = (p.get("title") or "").replace("File:", "")
        out.append(_candidate(
            mindset_id, "wikimedia", u, f"https://commons.wikimedia.org/wiki/{urllib.parse.quote(p.get('title', ''))}",
            thumbnail_url=ii.get("thumburl"),
            title=title, creator=creator,
            license_name=lic or SOURCE_REGISTRY["wikimedia"]["license_default"],
            rights_status=rights,
            attribution=_attribution("wikimedia", title=title, creator=creator),
        ))
        if len(out) >= n:
            break
    return out


def search_google_cse(mindset_id: str, query: str, n: int = 10) -> List[Candidate]:
    logging.info(f"search_google_cse: {query=} {n=}")
    key = os.environ.get("GOOGLE_CSE_API_KEY")
    cx = os.environ.get("GOOGLE_CSE_CX")
    if not key or not cx:
        return []
    r = requests.get(
        "https://www.googleapis.com/customsearch/v1",
        params={
            "key": key, "cx": cx, "q": query, "searchType": "image",
            "num": min(max(n, 1), 10), "safe": "active",
            "rights": "cc_publicdomain,cc_attribute,cc_sharealike",
        },
        headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT,
    )
    if r.status_code != 200:
        logging.info(f"cse {r.status_code}: {r.text[:200]}")
        return []
    out = []
    for it in r.json().get("items", []) or []:
        u = it.get("link")
        if not u:
            continue
        page = (it.get("image") or {}).get("contextLink") or ""
        host = urllib.parse.urlparse(page).hostname or ""
        out.append(_candidate(
            mindset_id, "google_cse", u, page,
            title=it.get("title", ""),
            attribution=_attribution("google_cse", source_host=host),
        ))
        if len(out) >= n:
            break
    return out


def search_vertex_search(mindset_id: str, query: str, n: int = 10) -> List[Candidate]:
    logging.info(f"search_vertex_search: {query=} {n=}")
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    app_id = os.environ.get("GOOGLE_VERTEX_SEARCH_APP_ID")
    if not project or not app_id:
        return []
    try:
        import google.auth
        from google.auth.transport.requests import Request as GARequest
        creds, _ = google.auth.default()
        creds.refresh(GARequest())
        token = creds.token
    except Exception as exc:
        logging.info(f"vertex auth failed: {exc}")
        return []
    endpoint = (
        f"https://discoveryengine.googleapis.com/v1/projects/{project}/"
        f"locations/global/collections/default_collection/engines/{app_id}/"
        f"servingConfigs/default_search:search"
    )
    r = requests.post(
        endpoint,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "X-Goog-User-Project": project, "User-Agent": UA},
        json={"query": query, "pageSize": max(n * 2, 12), "params": {"search_type": 1}},
        timeout=HTTP_TIMEOUT + 5,
    )
    if r.status_code != 200:
        logging.info(f"vertex {r.status_code}: {r.text[:200]}")
        return []
    out = []
    for result in r.json().get("results", []) or []:
        doc = result.get("document", {})
        ds = doc.get("derivedStructData", {}) or {}
        u = ds.get("image_url") or ds.get("link") or doc.get("structData", {}).get("image_url")
        if not u:
            continue
        page = ds.get("displayLink") or ds.get("link") or ""
        host = urllib.parse.urlparse(page or u).hostname or ""
        out.append(_candidate(
            mindset_id, "vertex_search", u, page,
            title=(ds.get("title") or [""])[0] if isinstance(ds.get("title"), list) else ds.get("title", ""),
            attribution=_attribution("vertex_search", source_host=host),
        ))
        if len(out) >= n:
            break
    return out


def search_serpapi(mindset_id: str, query: str, n: int = 10) -> List[Candidate]:
    logging.info(f"search_serpapi: {query=} {n=}")
    key = os.environ.get("SERPAPI_API_KEY")
    if not key:
        return []
    r = requests.get(
        "https://serpapi.com/search.json",
        params={"engine": "google_images", "q": query, "api_key": key, "num": min(max(n, 1), 50), "safe": "active"},
        headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT + 5,
    )
    if r.status_code != 200:
        logging.info(f"serpapi {r.status_code}: {r.text[:200]}")
        return []
    out = []
    for it in r.json().get("images_results", []) or []:
        u = it.get("original") or it.get("thumbnail")
        page = it.get("link") or it.get("source") or ""
        if not u:
            continue
        host = urllib.parse.urlparse(page or u).hostname or ""
        out.append(_candidate(
            mindset_id, "serpapi", u, page,
            thumbnail_url=it.get("thumbnail"),
            title=it.get("title") or "",
            attribution=_attribution("serpapi", source_host=host),
        ))
        if len(out) >= n:
            break
    return out


# ── Evident IOTY: hand-curated manifest, hot-linked from CDN ──
# Only entries whose CDN URLs have been verified live. The 2022/2021 winners
# at the old `landing-directory` paths now 404. Once the new paths are known
# they can be added back. The richer manifest in xdm_server/ingest_ioty.py is
# behind a similar staleness issue — needs a verification pass before reuse.
IOTY_MANIFEST: List[Tuple[int, str, str, str, str, str]] = [
    (2024, "Global Winner", "Igor Siwanowicz", "United States",
     "Beauty of Cosmic Proportions",
     "https://adobeassets.evidentscientific.com/content/dam/image/landing-directory/ioty2024/dl/Global-winner-ioty.jpg"),
]


def search_evident_ioty(mindset_id: str, query: str, n: int = 10) -> List[Candidate]:
    logging.info(f"search_evident_ioty: {query=} {n=}")
    q = (query or "").lower()
    # Crude relevance: token overlap with category + title + photographer.
    def score(row):
        year, category, photographer, affiliation, title, url = row
        hay = f"{category} {title} {photographer} {affiliation}".lower()
        toks = re.findall(r"[a-z0-9]+", q)
        return sum(1 for t in toks if t and t in hay)
    rows = sorted(IOTY_MANIFEST, key=score, reverse=True)
    out = []
    for year, category, photographer, affiliation, title, url in rows[: n * 2]:
        page = f"https://evidentscientific.com/en/image-of-the-year-{year}"
        out.append(_candidate(
            mindset_id, "evident_ioty", url, page,
            title=title, creator=photographer, year=year,
            caption=category,
            attribution=_attribution("evident_ioty", title=title, creator=photographer, affiliation=affiliation, year=year),
        ))
        if len(out) >= n:
            break
    return out


# ── Nikon Small World: stub. Real scrape comes next milestone. ──
def search_nikon_small_world(mindset_id: str, query: str, n: int = 10) -> List[Candidate]:
    logging.info(f"search_nikon_small_world: stub, returning []")
    return []


SEARCH_FUNCS = {
    "unsplash": search_unsplash,
    "pexels": search_pexels,
    "pixabay": search_pixabay,
    "met": search_met,
    "smithsonian": search_smithsonian,
    "europeana": search_europeana,
    "nasa": search_nasa,
    "wikimedia": search_wikimedia,
    "google_cse": search_google_cse,
    "vertex_search": search_vertex_search,
    "serpapi": search_serpapi,
    "evident_ioty": search_evident_ioty,
    "nikon_small_world": search_nikon_small_world,
}


def run_source(source_id: str, mindset_id: str, query: str, n: int = 10) -> List[Candidate]:
    fn = SEARCH_FUNCS.get(source_id)
    if not fn:
        raise Exception(f"unknown source: {source_id}")
    try:
        return fn(mindset_id, query, n=n)
    except Exception as exc:
        logging.info(f"source {source_id} failed for {query!r}: {exc}")
        return []
