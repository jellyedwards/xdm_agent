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
        "search_hint": "modern editorial photos; literal subject + a mood/style word ('foggy forest path'); evocative phrasing ok",
        "requires_env": ["UNSPLASH_ACCESS_KEY"],
    },
    "pexels": {
        "display_name": "Pexels", "kind": "stock", "hotlink_ok": True,
        "license_default": "Pexels License",
        "license_url": "https://www.pexels.com/license/",
        "attribution_template": "Photo by {creator} from Pexels",
        "rights_status_default": "clear",
        "search_hint": "modern photos; literal subject + mood/style words; evocative phrasing ok",
        "requires_env": ["PEXELS_API_KEY"],
    },
    "pixabay": {
        "display_name": "Pixabay", "kind": "stock", "hotlink_ok": True,
        "license_default": "Pixabay License",
        "license_url": "https://pixabay.com/service/license-summary/",
        "attribution_template": "Image by {creator} from Pixabay",
        "rights_status_default": "clear",
        "search_hint": "broad stock; short generic nouns (textures, patterns, single objects); avoid long phrases",
        "requires_env": ["PIXABAY_API_KEY"],
    },
    "met": {
        "display_name": "The Met", "kind": "museum", "hotlink_ok": True,
        "license_default": "Public Domain (Met OA)",
        "license_url": "https://www.metmuseum.org/about-the-met/policies-and-documents/open-access",
        "attribution_template": "{title}, {creator}, The Met",
        "rights_status_default": "clear",
        "search_hint": "keyword API: literal object/material/culture terms ('porcelain vase', 'samurai armour'); not metaphors",
    },
    "smithsonian": {
        "display_name": "Smithsonian", "kind": "museum", "hotlink_ok": True,
        "license_default": "CC0 (Smithsonian Open Access)",
        "license_url": "https://www.si.edu/openaccess",
        "attribution_template": "{title}, Smithsonian",
        "rights_status_default": "clear",
        "search_hint": "keyword API: literal subject/specimen/object names; not metaphors",
    },
    "europeana": {
        "display_name": "Europeana", "kind": "museum", "hotlink_ok": True,
        "license_default": "Per-item (open subset)",
        "license_url": "https://www.europeana.eu/en/rights",
        "attribution_template": "{title} — {creator} ({provider})",
        "rights_status_default": "caveat",
        "search_hint": "keyword API: literal subjects, historical/European terms; short phrases",
        "requires_env": ["EUROPEANA_API_KEY"],
    },
    "nasa": {
        "display_name": "NASA", "kind": "museum", "hotlink_ok": True,
        "license_default": "Public Domain (NASA)",
        "license_url": "https://www.nasa.gov/nasa-brand-center/images-and-media/",
        "attribution_template": "{title} / NASA",
        "rights_status_default": "clear",
        "search_hint": "literal space subjects ('nebula', 'aurora', mission or instrument names)",
    },
    "wikimedia": {
        "display_name": "Wikimedia Commons", "kind": "museum", "hotlink_ok": True,
        "license_default": "Per-item (mostly CC BY-SA)",
        "license_url": "https://commons.wikimedia.org/wiki/Commons:Licensing",
        "attribution_template": "{title} — {creator}, Wikimedia Commons",
        "rights_status_default": "caveat",
        "search_hint": "keyword API: literal named subjects, species, or places; short terms, not metaphors",
    },
    # ── Open cultural / scientific sources (mirror of xdm_server endpoints) ──
    "vanda": {
        "display_name": "Victoria & Albert Museum", "kind": "museum", "hotlink_ok": True,
        "license_default": "Mixed — verify per object",
        "license_url": "https://www.vam.ac.uk/info/va-image-licensing",
        "attribution_template": "{title} — {creator}, V&A",
        "rights_status_default": "caveat",
        "search_hint": "keyword API: literal design/material terms ('Art Nouveau textile', 'lacquer box')",
    },
    "artic": {
        "display_name": "Art Institute of Chicago", "kind": "museum", "hotlink_ok": True,
        "license_default": "Public Domain / CC0 (verify per item)",
        "license_url": "https://www.artic.edu/open-access",
        "attribution_template": "{title} — {creator}, Art Institute of Chicago",
        "rights_status_default": "clear",
        "search_hint": "keyword API: literal art subject, medium, or artist name",
    },
    "cleveland": {
        "display_name": "Cleveland Museum of Art", "kind": "museum", "hotlink_ok": True,
        "license_default": "Open Access — many CC0 (verify per item)",
        "license_url": "https://www.clevelandart.org/open-access",
        "attribution_template": "{title} — {creator}, Cleveland Museum of Art",
        "rights_status_default": "clear",
        "search_hint": "keyword API: literal art subject, medium, or culture",
    },
    "wellcome": {
        "display_name": "Wellcome Collection", "kind": "museum", "hotlink_ok": True,
        "license_default": "Mostly CC-BY / CC0 — verify per item",
        "license_url": "https://wellcomecollection.org/",
        "attribution_template": "{title} — {creator}, Wellcome Collection",
        "rights_status_default": "caveat",
        "search_hint": "keyword API: literal anatomy/medical/specimen terms ('dissection', 'microscope')",
    },
    "loc": {
        "display_name": "Library of Congress", "kind": "museum", "hotlink_ok": True,
        "license_default": "Rights vary — many no known restrictions; verify per item",
        "license_url": "https://www.loc.gov/legal/",
        "attribution_template": "{title} — {creator}",
        "rights_status_default": "caveat",
        "search_hint": "keyword API: literal place/era/event ('1930s dust bowl', 'tenement')",
    },
    "gbif": {
        "display_name": "GBIF", "kind": "nature", "hotlink_ok": True,
        "license_default": "Per-item CC licence (often CC-BY / CC-BY-NC) — verify",
        "license_url": "https://www.gbif.org/terms",
        "attribution_template": "{title} — {creator}, via GBIF",
        "rights_status_default": "caveat",
        "search_hint": "scientific or common species names only ('Lepidoptera', 'bracket fungus'); one taxon per query",
    },
    "openverse": {
        "display_name": "Openverse", "kind": "stock", "hotlink_ok": True,
        "license_default": "Open / Creative Commons — varies per item",
        "license_url": "https://openverse.org/",
        "attribution_template": "{title} — {creator}, via Openverse",
        "rights_status_default": "caveat",
        "search_hint": "CC aggregator; literal subjects in short phrases; weak on metaphor",
    },
    "openi": {
        "display_name": "Open-i (NLM)", "kind": "science", "hotlink_ok": True,
        "license_default": "Rights vary by source article — verify per item",
        "license_url": "https://openi.nlm.nih.gov/",
        "attribution_template": "{title} — {creator}, Open-i / NLM",
        "rights_status_default": "caveat",
        "search_hint": "keyword API: literal biomedical terms ('histology', 'fluorescence microscopy', cell type)",
    },
    "rijksmuseum": {
        "display_name": "Rijksmuseum", "kind": "museum", "hotlink_ok": True,
        "license_default": "Mostly CC0 / Public Domain — verify per item",
        "license_url": "https://data.rijksmuseum.nl/",
        "attribution_template": "{title} — {creator}, Rijksmuseum",
        "rights_status_default": "clear",
        "search_hint": "matches title/description only: literal subject words ('still life', 'tulip', 'windmill')",
    },
    "harvard": {
        "display_name": "Harvard Art Museums", "kind": "museum", "hotlink_ok": True,
        "license_default": "Rights vary — verify reuse",
        "license_url": "https://www.harvardartmuseums.org/collections/api",
        "attribution_template": "{title} — {creator}, Harvard Art Museums",
        "rights_status_default": "caveat",
        "search_hint": "keyword API: literal art subject, medium, or culture",
        "requires_env": ["HARVARD_ART_API_KEY"],
    },
    "bhl": {
        "display_name": "Biodiversity Heritage Library", "kind": "nature", "hotlink_ok": True,
        "license_default": "Mostly public domain — verify per item",
        "license_url": "https://www.biodiversitylibrary.org/terms",
        "attribution_template": "{title} — {creator}, BHL",
        "rights_status_default": "caveat",
        "search_hint": "keyword API: literal taxonomic/natural-history terms ('orchid plate', genus name)",
        "requires_env": ["BHL_API_KEY"],
    },
    "flickr": {
        "display_name": "Flickr (Commons / CC)", "kind": "photo", "hotlink_ok": True,
        "license_default": "Per-item Flickr licence (CC / public domain) — verify",
        "license_url": "https://www.flickr.com/creativecommons/",
        "attribution_template": "{title} — {creator}, Flickr",
        "rights_status_default": "caveat",
        "search_hint": "weak fuzzy match: 2-3 short noun keywords or tags, not long descriptive phrases",
        "requires_env": ["FLICKR_API_KEY"],
    },
    "google_cse": {
        "display_name": "Google Search", "kind": "web", "hotlink_ok": False,
        "license_default": "Creative Commons / public domain (CC-filtered) — verify per item",
        "license_url": None,
        "attribution_template": "via {source_host}",
        "rights_status_default": "caveat",
        "search_hint": "broad web, CC-filtered: evocative or very specific phrases both fine; good for named subjects",
        "requires_env": ["GOOGLE_CSE_API_KEY", "GOOGLE_CSE_CX"],
    },
    "vertex_search": {
        "display_name": "Vertex AI Search", "kind": "web", "hotlink_ok": False,
        "license_default": "Unknown — Vertex AI Search",
        "license_url": None,
        "attribution_template": "via {source_host}",
        "rights_status_default": "unknown",
        "search_hint": "broad web: evocative or specific phrases fine",
        "requires_env": ["GOOGLE_CLOUD_PROJECT", "GOOGLE_VERTEX_SEARCH_APP_ID"],
    },
    "serpapi": {
        "display_name": "Web search", "kind": "web", "hotlink_ok": False,
        "license_default": "Unknown — web search",
        "license_url": None,
        "attribution_template": "via {source_host}",
        "rights_status_default": "unknown",
        "search_hint": "broad web: evocative or specific phrases fine; best for specific named subjects (rights unknown — site:-scope to a curated domain to mark editorial)",
        "requires_env": ["SERPAPI_API_KEY"],
    },
    "evident_ioty": {
        "display_name": "Evident IOTY", "kind": "competition", "hotlink_ok": True,
        "license_default": "Editorial/personal use only (Evident IOTY)",
        "license_url": "https://evidentscientific.com/en/ioty-gallery",
        "attribution_template": "{title} — {creator} ({affiliation}), Evident IOTY {year}",
        "rights_status_default": "caveat",
        "search_hint": "small curated microscopy set; category or year terms",
    },
    "nikon_small_world": {
        "display_name": "Nikon Small World", "kind": "competition", "hotlink_ok": True,
        "license_default": "Editorial use (Nikon Small World)",
        "license_url": "https://www.nikonsmallworld.com/about-us/copyright",
        "attribution_template": "{title} — {creator}, Nikon Small World {year}",
        "rights_status_default": "caveat",
        "search_hint": "photomicrography competition; category/year terms (not yet active)",
        "stub": True,
    },
}


def source_ready(sid: str) -> Tuple[bool, str]:
    meta = SOURCE_REGISTRY.get(sid)
    if not meta:
        return False, "unknown source"
    if meta.get("stub"):
        return False, "not implemented yet"
    missing = [k for k in meta.get("requires_env") or [] if not os.environ.get(k)]
    if missing:
        return False, "missing env: " + ", ".join(missing)
    return True, ""


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


def _rights_status(license_text: Optional[str]) -> str:
    """Map a per-item licence string to clear|caveat for open-source results.

    NonCommercial / NoDerivatives CC variants are checked FIRST and downgraded to
    "caveat": they contain "cc by" as a substring (e.g. "CC BY-NC-SA") but
    restrict reuse, so they must never be reported as "clear" (free to use).
    """
    t = (license_text or "").lower()
    restricted = ("-nc", "-nd", "noncommercial", "non-commercial", "noderiv", "no-deriv")
    if any(k in t for k in restricted):
        return "caveat"
    open_markers = ("cc0", "public domain", "publicdomain", "pdm", "cc by",
                    "cc-by", "no known copyright", "government work")
    return "clear" if any(k in t for k in open_markers) else "caveat"


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


# ───── Open cultural / scientific sources ─────────────────────────────────────
# Ports of the xdm_server /search_* endpoints. Each hits a free source, builds
# best-first Candidate lists, and lets the hunt's judge pick the keepers. The
# server-side equivalents live in xdm_server/main.py; keep the two in sync.

# Some CDNs (loc.gov, Wikimedia, Openverse, Art Institute) reject blank or
# generic UAs, so reuse the descriptive module UA above for all of them.

def search_artic(mindset_id: str, query: str, n: int = 10) -> List[Candidate]:
    """Art Institute of Chicago open API (no key, IIIF images)."""
    logging.info(f"search_artic: {query=} {n=}")
    r = requests.get(
        "https://api.artic.edu/api/v1/artworks/search",
        params={"q": query, "fields": "id,title,image_id,artist_display", "limit": min(n * 2, 30)},
        headers={"AIC-User-Agent": UA, "User-Agent": UA},
        timeout=HTTP_TIMEOUT + 8,
    )
    if r.status_code != 200:
        raise Exception(f"artic {r.status_code}: {r.text[:200]}")
    data = r.json()
    iiif = (data.get("config") or {}).get("iiif_url", "https://www.artic.edu/iiif/2")
    out = []
    for a in data.get("data", []):
        image_id = a.get("image_id")
        if not image_id:
            continue
        page = f"https://www.artic.edu/artworks/{a.get('id')}"
        title = a.get("title") or query
        creator = (a.get("artist_display") or "Art Institute of Chicago").split("\n")[0]
        out.append(_candidate(
            mindset_id, "artic", f"{iiif}/{image_id}/full/843,/0/default.jpg", page,
            title=title, creator=creator,
            attribution=_attribution("artic", title=title, creator=creator),
        ))
        if len(out) >= n:
            break
    return out


def search_cleveland(mindset_id: str, query: str, n: int = 10) -> List[Candidate]:
    """Cleveland Museum of Art open-access API (no key)."""
    logging.info(f"search_cleveland: {query=} {n=}")
    r = requests.get(
        "https://openaccess-api.clevelandart.org/api/artworks/",
        params={"q": query, "has_image": 1, "limit": min(n * 2, 30)},
        headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT + 8,
    )
    if r.status_code != 200:
        raise Exception(f"cleveland {r.status_code}: {r.text[:200]}")
    out = []
    for a in r.json().get("data", []):
        imgs = a.get("images") or {}
        # web (jpg) preferred; print is a bigger jpg; skip 'full' (huge TIFF).
        u = (imgs.get("web") or {}).get("url") or (imgs.get("print") or {}).get("url")
        if not u:
            continue
        creators = a.get("creators") or []
        creator = creators[0].get("description") if creators else "Cleveland Museum of Art"
        title = a.get("title") or query
        out.append(_candidate(
            mindset_id, "cleveland", u, a.get("url") or "",
            title=title, creator=creator,
            attribution=_attribution("cleveland", title=title, creator=creator),
        ))
        if len(out) >= n:
            break
    return out


def search_vanda(mindset_id: str, query: str, n: int = 10) -> List[Candidate]:
    """Victoria & Albert Museum collections API v2 (no key, IIIF)."""
    logging.info(f"search_vanda: {query=} {n=}")
    r = requests.get(
        "https://api.vam.ac.uk/v2/objects/search",
        params={"q": query, "images_exist": "true", "page_size": min(n * 2, 30)},
        headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT + 8,
    )
    if r.status_code != 200:
        raise Exception(f"vanda {r.status_code}: {r.text[:200]}")
    out = []
    for rec in r.json().get("records", []):
        base = (rec.get("_images") or {}).get("_iiif_image_base_url")
        if not base:
            continue
        sysnum = rec.get("systemNumber")
        page = f"https://collections.vam.ac.uk/item/{sysnum}" if sysnum else ""
        maker = rec.get("_primaryMaker") or {}
        creator = maker.get("name") or "Victoria and Albert Museum"
        title = rec.get("_primaryTitle") or rec.get("objectType") or query
        out.append(_candidate(
            mindset_id, "vanda", f"{base}full/843,/0/default.jpg", page,
            title=title, creator=creator,
            attribution=_attribution("vanda", title=title, creator=creator),
        ))
        if len(out) >= n:
            break
    return out


def search_wellcome(mindset_id: str, query: str, n: int = 10) -> List[Candidate]:
    """Wellcome Collection images API (no key, IIIF; per-item licence)."""
    logging.info(f"search_wellcome: {query=} {n=}")
    r = requests.get(
        "https://api.wellcomecollection.org/catalogue/v2/images",
        params={"query": query, "pageSize": min(n * 2, 30)},
        headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT + 8,
    )
    if r.status_code != 200:
        raise Exception(f"wellcome {r.status_code}: {r.text[:200]}")
    out = []
    for rec in r.json().get("results", []):
        thumb = rec.get("thumbnail") or {}
        info = thumb.get("url")  # .../image/{id}/info.json
        if not info:
            continue
        img = info.replace("/info.json", "/full/880,/0/default.jpg")
        src = rec.get("source") or {}
        wid = src.get("id")
        page = f"https://wellcomecollection.org/works/{wid}" if wid else ""
        lic = thumb.get("license") or {}
        title = src.get("title") or query
        creator = thumb.get("credit") or "Wellcome Collection"
        out.append(_candidate(
            mindset_id, "wellcome", img, page,
            title=title, creator=creator,
            license_name=lic.get("label") or SOURCE_REGISTRY["wellcome"]["license_default"],
            license_url=lic.get("url") or SOURCE_REGISTRY["wellcome"]["license_url"],
            rights_status=_rights_status(lic.get("label")),
            attribution=_attribution("wellcome", title=title, creator=creator),
        ))
        if len(out) >= n:
            break
    return out


def search_loc(mindset_id: str, query: str, n: int = 10) -> List[Candidate]:
    """Library of Congress photos (no key, loc.gov JSON view)."""
    logging.info(f"search_loc: {query=} {n=}")
    r = requests.get(
        "https://www.loc.gov/photos/",
        params={"q": query, "fo": "json", "c": min(n * 2, 40)},
        headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT + 13,
    )
    if r.status_code != 200:
        raise Exception(f"loc {r.status_code}: {r.text[:200]}")
    try:
        data = r.json()
    except Exception:
        raise Exception("loc: non-JSON response (likely rate-limited)")
    out = []
    for rec in data.get("results", []):
        imgs = rec.get("image_url") or []
        if not imgs:
            continue
        u = imgs[-1].split("#")[0]  # last entry is largest; drop #h=..&w=..
        if not u.startswith("http"):
            continue
        title = rec.get("title")
        if isinstance(title, list):
            title = title[0] if title else query
        page = rec.get("id") or rec.get("url") or ""
        out.append(_candidate(
            mindset_id, "loc", u, page,
            title=title or query, creator="Library of Congress",
            attribution=_attribution("loc", title=title or query, creator="Library of Congress"),
        ))
        if len(out) >= n:
            break
    return out


def search_openverse(mindset_id: str, query: str, n: int = 10) -> List[Candidate]:
    """Openverse — aggregator of openly-licensed images (no key)."""
    logging.info(f"search_openverse: {query=} {n=}")
    r = requests.get(
        "https://api.openverse.org/v1/images/",
        params={"q": query, "page_size": min(n * 2, 30)},
        headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT + 13,
    )
    if r.status_code != 200:
        raise Exception(f"openverse {r.status_code}: {r.text[:200]}")
    out = []
    for rec in r.json().get("results", []):
        u = rec.get("url")
        if not u:
            continue
        lic = " ".join(x for x in [(rec.get("license") or "").upper(), rec.get("license_version") or ""] if x).strip()
        title = rec.get("title") or query
        creator = rec.get("creator") or rec.get("source") or "Openverse"
        out.append(_candidate(
            mindset_id, "openverse", u, rec.get("foreign_landing_url") or "",
            creator_url=rec.get("creator_url"),
            title=title, creator=creator,
            license_name=lic or SOURCE_REGISTRY["openverse"]["license_default"],
            license_url=rec.get("license_url"),
            rights_status=_rights_status(lic),
            attribution=_attribution("openverse", title=title, creator=creator),
        ))
        if len(out) >= n:
            break
    return out


def search_openi(mindset_id: str, query: str, n: int = 10) -> List[Candidate]:
    """Open-i (NLM) — biomedical figures from the literature (no key; slow API)."""
    logging.info(f"search_openi: {query=} {n=}")
    r = requests.get(
        "https://openi.nlm.nih.gov/api/search",
        params={"query": query, "m": 1, "n": min(n * 2, 30)},
        headers={"User-Agent": UA}, timeout=40,  # Open-i can be slow to respond
    )
    if r.status_code != 200:
        raise Exception(f"openi {r.status_code}: {r.text[:200]}")
    out = []
    for rec in r.json().get("list", []):
        rel = rec.get("imgLarge") or rec.get("imgThumbLarge") or rec.get("imgThumb")
        if not rel:
            continue
        u = ("https://openi.nlm.nih.gov" + rel) if rel.startswith("/") else rel
        caption = re.sub(r"<[^>]+>", "", (rec.get("image") or {}).get("caption") or "").strip() or rec.get("title") or query
        page = rec.get("pmc_url") or rec.get("pubMed_url") or rec.get("fulltext_html_url") or ""
        journal = rec.get("journal_title") or "Open-i / NLM"
        out.append(_candidate(
            mindset_id, "openi", u, page,
            title=caption, creator=journal,
            attribution=_attribution("openi", title=caption, creator=journal),
        ))
        if len(out) >= n:
            break
    return out


def search_gbif(mindset_id: str, query: str, n: int = 10) -> List[Candidate]:
    """GBIF occurrence media — wild nature / biodiversity photos (no key)."""
    logging.info(f"search_gbif: {query=} {n=}")
    r = requests.get(
        "https://api.gbif.org/v1/occurrence/search",
        params={"q": query, "mediaType": "StillImage", "limit": min(n * 3, 40)},
        headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT + 13,
    )
    if r.status_code != 200:
        raise Exception(f"gbif {r.status_code}: {r.text[:200]}")
    out = []
    for rec in r.json().get("results", []):
        media = rec.get("media") or []
        m = next((x for x in media if x.get("identifier")), None)
        if not m:
            continue
        key = rec.get("key")
        page = m.get("source") or (f"https://www.gbif.org/occurrence/{key}" if key else "")
        lic = m.get("license") or rec.get("license")
        creator = m.get("creator") or m.get("rightsHolder") or "GBIF contributor"
        title = m.get("title") or rec.get("scientificName") or query
        out.append(_candidate(
            mindset_id, "gbif", m.get("identifier"), page,
            title=title, creator=creator,
            license_name=lic or SOURCE_REGISTRY["gbif"]["license_default"],
            rights_status=_rights_status(lic),
            attribution=_attribution("gbif", title=title, creator=creator),
        ))
        if len(out) >= n:
            break
    return out


def _rijks_get(url: str) -> dict:
    """Fetch one Rijksmuseum Linked-Art JSON-LD resource (no key needed)."""
    r = requests.get(
        url, headers={"User-Agent": UA, "Accept": "application/json"},
        timeout=HTTP_TIMEOUT + 3,
    )
    r.raise_for_status()
    return r.json()


def _rijks_resolve_image(object_id: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Resolve a HumanMadeObject id to (image_url, title, page_url).

    The keyless Data Services API only returns LOD identifiers, so the image
    lives three hops away: object → VisualItem (`shows`) → DigitalObject
    (`digitally_shown_by`) → IIIF image (`access_point`).
    """
    obj = _rijks_get(object_id)
    title = next(
        (nm.get("content") for nm in (obj.get("identified_by") or [])
         if nm.get("type") == "Name" and nm.get("content")),
        None,
    )
    page = None
    for so in obj.get("subject_of") or []:
        for dc in so.get("digitally_carried_by") or []:
            if dc.get("format") == "text/html":
                page = next((ap.get("id") for ap in dc.get("access_point") or [] if ap.get("id")), None)
                if page:
                    break
        if page:
            break

    shows = obj.get("shows") or []
    if not shows:
        return None, title, page
    vi = _rijks_get(shows[0]["id"])
    dsb = vi.get("digitally_shown_by") or []
    if not dsb:
        return None, title, page
    do = _rijks_get(dsb[0]["id"])
    ap = do.get("access_point") or []
    if not ap:
        return None, title, page
    img = ap[0]["id"].replace("/full/max/", "/full/843,/")
    return img, title, page


def search_rijksmuseum(mindset_id: str, query: str, n: int = 10) -> List[Candidate]:
    """Rijksmuseum Data Services API (no key; Linked-Art JSON-LD, 3-hop resolve).

    The keyless API matches on title/description only, so merge a title and a
    description search, then resolve the first few hits to images (each resolve
    is 3 extra requests, so we cap it).
    """
    logging.info(f"search_rijksmuseum: {query=} {n=}")
    base = "https://data.rijksmuseum.nl/search/collection"
    seen, object_ids = set(), []
    for field in ("title", "description"):
        try:
            data = _rijks_get(f"{base}?{field}={requests.utils.quote(query)}&imageAvailable=true")
        except Exception as exc:
            logging.info(f"rijksmuseum: {field} search failed: {exc}")
            continue
        for item in data.get("orderedItems") or []:
            oid = item.get("id")
            if oid and oid not in seen:
                seen.add(oid)
                object_ids.append(oid)

    out = []
    for oid in object_ids[: max(n, 4)]:  # cap resolves — each is 3 extra requests
        try:
            img, title, page = _rijks_resolve_image(oid)
        except Exception as exc:
            logging.info(f"rijksmuseum: resolve failed for {oid}: {exc}")
            continue
        if not img:
            continue
        title = title or query
        out.append(_candidate(
            mindset_id, "rijksmuseum", img, page or oid,
            title=title, creator="Rijksmuseum",
            attribution=_attribution("rijksmuseum", title=title, creator="Rijksmuseum"),
        ))
        if len(out) >= n:
            break
    return out


def search_harvard(mindset_id: str, query: str, n: int = 10) -> List[Candidate]:
    """Harvard Art Museums API (free key; one-call, direct image)."""
    logging.info(f"search_harvard: {query=} {n=}")
    api_key = os.environ.get("HARVARD_ART_API_KEY")
    if not api_key:
        return []
    r = requests.get(
        "https://api.harvardartmuseums.org/object",
        params={"apikey": api_key, "q": query, "hasimage": 1, "size": min(n * 2, 30), "sort": "rank"},
        headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT + 8,
    )
    if r.status_code != 200:
        raise Exception(f"harvard {r.status_code}: {r.text[:200]}")
    out = []
    for rec in r.json().get("records", []):
        u = rec.get("primaryimageurl")
        if not u:
            continue
        people = rec.get("people") or []
        creator = people[0].get("name") if people else "Harvard Art Museums"
        img0 = (rec.get("images") or [{}])[0]
        copyright_ = img0.get("copyright")
        title = rec.get("title") or query
        out.append(_candidate(
            mindset_id, "harvard", u, rec.get("url") or "",
            title=title, creator=creator,
            license_name=copyright_ or SOURCE_REGISTRY["harvard"]["license_default"],
            rights_status=_rights_status(copyright_),
            attribution=_attribution("harvard", title=title, creator=creator),
        ))
        if len(out) >= n:
            break
    return out


def search_bhl(mindset_id: str, query: str, n: int = 10) -> List[Candidate]:
    """Biodiversity Heritage Library (free key).

    BHL is a scanned-book library, so search publications, then pull only the
    pages flagged Illustration (the botanical/zoological plates) — skipping text.
    """
    logging.info(f"search_bhl: {query=} {n=}")
    api_key = os.environ.get("BHL_API_KEY")
    if not api_key:
        return []

    def _bhl(params):
        resp = requests.get(
            "https://www.biodiversitylibrary.org/api3",
            params={**params, "apikey": api_key, "format": "json"},
            headers={"User-Agent": UA}, timeout=40,
        )
        resp.raise_for_status()
        return resp.json()

    found = _bhl({"op": "PublicationSearch", "searchterm": query, "searchtype": "C"})
    items = [r for r in (found.get("Result") or []) if r.get("ItemID")]
    if not items:
        return []

    out = []
    for item in items[:4]:  # cap item-metadata fetches
        try:
            meta = _bhl({"op": "GetItemMetadata", "id": item["ItemID"], "pages": "t", "ocr": "f", "parts": "f"})
        except Exception as exc:
            logging.info(f"bhl: item {item['ItemID']} metadata failed: {exc}")
            continue
        res = (meta.get("Result") or [None])[0]
        if not res:
            continue
        lic = res.get("LicenseUrl")
        license_name = res.get("CopyrightStatus") or SOURCE_REGISTRY["bhl"]["license_default"]
        holder = res.get("HoldingInstitution") or "Biodiversity Heritage Library"
        title = item.get("Title") or query
        for p in res.get("Pages") or []:
            is_illus = any(
                "illustration" in ((t.get("PageTypeName") if isinstance(t, dict) else t) or "").lower()
                for t in (p.get("PageTypes") or [])
            )
            if not is_illus or not p.get("FullSizeImageUrl"):
                continue
            out.append(_candidate(
                mindset_id, "bhl", p["FullSizeImageUrl"], p.get("PageUrl") or "",
                title=title, creator=holder,
                license_name=license_name, license_url=lic,
                rights_status=_rights_status(license_name),
                attribution=_attribution("bhl", title=title, creator=holder),
            ))
            if len(out) >= n:
                break
        if len(out) >= n:
            break
    return out


# Flickr photo-licence IDs → human label (flickr.photos.licenses.getInfo).
_FLICKR_LICENSES = {
    "0": "All Rights Reserved", "1": "CC BY-NC-SA 2.0", "2": "CC BY-NC 2.0",
    "3": "CC BY-NC-ND 2.0", "4": "CC BY 2.0", "5": "CC BY-SA 2.0",
    "6": "CC BY-ND 2.0", "7": "No known copyright restrictions (Flickr Commons)",
    "8": "United States Government Work", "9": "CC0 1.0", "10": "Public Domain Mark 1.0",
}
# The Biodiversity Heritage Library's Flickr photostream (curated plates).
_BHL_FLICKR_NSID = "61021753@N02"
# Licence band for broad passes: every reusable CC variant + Commons/CC0/PDM +
# US-gov work (codes 1-10) — i.e. everything except 0 (All Rights Reserved).
_FLICKR_OPEN_LICENSES = "1,2,3,4,5,6,7,8,9,10"

# Generic words to drop when reducing a query to keyword/tag terms — Flickr's
# full-text match is weak, so trimming to salient nouns broadens recall.
_FLICKR_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "and", "or", "with", "for", "to", "at",
    "by", "from", "into", "over", "under", "macro", "photo", "image", "picture",
    "shot", "closeup", "close", "up", "style", "light", "lighting", "colour",
    "color", "abstract", "detail", "view",
}


def _keyword_terms(query: str, max_terms: int = 4) -> List[str]:
    """Reduce a query to a few salient noun-ish keywords for keyword/tag search."""
    terms: List[str] = []
    for t in re.findall(r"[A-Za-z][A-Za-z0-9-]+", query or ""):
        tl = t.lower()
        if len(tl) < 3 or tl in _FLICKR_STOPWORDS or tl in terms:
            continue
        terms.append(tl)
        if len(terms) >= max_terms:
            break
    return terms


def search_flickr(mindset_id: str, query: str, n: int = 10) -> List[Candidate]:
    """Flickr (free key) — progressively broadened until it has n results.

    Flickr's full-text `text` match is weak (effectively ANDs terms, no fuzz), so
    long evocative queries return nothing. Start curated/narrow and broaden:
      1. BHL natural-history photostream (curated public domain), full text
      2. all-CC / Commons, full text
      3. all-CC / Commons, text trimmed to key nouns
      4. all-CC / Commons, those nouns as OR tags (tag_mode=any)
    Results accumulate across bands (deduped) until n is reached. Public photo
    search needs only the API key (not the secret).
    """
    logging.info(f"search_flickr: {query=} {n=}")
    api_key = os.environ.get("FLICKR_API_KEY")
    if not api_key:
        return []

    def _flickr(extra):
        resp = requests.get(
            "https://api.flickr.com/services/rest/",
            params={
                "method": "flickr.photos.search", "api_key": api_key,
                "extras": "url_l,url_c,owner_name,license,path_alias",
                "per_page": min(n * 2, 24), "sort": "relevance", "content_type": 1,
                "media": "photos", "safe_search": 1,
                "format": "json", "nojsoncallback": 1, **extra,
            },
            # Flickr responds in ~1-2s; keep the timeout tight so the 4-band
            # broadening loop can't monopolise a worker on a zero-yield query.
            headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("photos", {}).get("photo", []) or []

    terms = _keyword_terms(query)
    trimmed = " ".join(terms)
    bands = [
        {"text": query, "user_id": _BHL_FLICKR_NSID},
        {"text": query, "license": _FLICKR_OPEN_LICENSES},
    ]
    if trimmed and trimmed != (query or "").strip().lower():
        bands.append({"text": trimmed, "license": _FLICKR_OPEN_LICENSES})
    if terms:
        bands.append({"tags": ",".join(terms), "tag_mode": "any", "license": _FLICKR_OPEN_LICENSES})

    out, seen = [], set()
    for extra in bands:
        if len(out) >= n:
            break
        try:
            photos = _flickr(extra)
        except Exception as exc:
            logging.info(f"flickr: search {extra} failed: {exc}")
            continue
        for p in photos:
            pid = p.get("id")
            u = p.get("url_l") or p.get("url_c")
            if not pid or pid in seen or not u:
                continue
            seen.add(pid)
            owner = p.get("pathalias") or p.get("owner")
            page = f"https://www.flickr.com/photos/{owner}/{pid}"
            ptitle = p.get("title") or ""
            # BHL/scan titles are codes like "n58_w1150"; treat any spaceless
            # title containing a digit as junk and fall back to the query.
            is_scan_code = ptitle and " " not in ptitle and any(ch.isdigit() for ch in ptitle)
            title = query if is_scan_code else (ptitle or query)
            creator = p.get("ownername") or "Flickr"
            license_name = _FLICKR_LICENSES.get(str(p.get("license")), "See Flickr photo page")
            out.append(_candidate(
                mindset_id, "flickr", u, page,
                title=title, creator=creator,
                license_name=license_name,
                rights_status=_rights_status(license_name),
                attribution=_attribution("flickr", title=title, creator=creator),
            ))
            if len(out) >= n:
                break
    return out


SEARCH_FUNCS = {
    "unsplash": search_unsplash,
    "pexels": search_pexels,
    "pixabay": search_pixabay,
    "met": search_met,
    "smithsonian": search_smithsonian,
    "europeana": search_europeana,
    "nasa": search_nasa,
    "wikimedia": search_wikimedia,
    "vanda": search_vanda,
    "artic": search_artic,
    "cleveland": search_cleveland,
    "wellcome": search_wellcome,
    "loc": search_loc,
    "gbif": search_gbif,
    "openverse": search_openverse,
    "openi": search_openi,
    "rijksmuseum": search_rijksmuseum,
    "harvard": search_harvard,
    "bhl": search_bhl,
    "flickr": search_flickr,
    "google_cse": search_google_cse,
    "vertex_search": search_vertex_search,
    "serpapi": search_serpapi,
    "evident_ioty": search_evident_ioty,
    "nikon_small_world": search_nikon_small_world,
}


# Broad catch-all sources (in order) used to back-fill a 0-hit search — see
# agents.execute_searches. Keyless openverse/wikimedia first (their per-item
# rights survive rights-filtering), then the CC-filtered web search. serpapi is
# intentionally excluded: its results are rights_status "unknown" and would be
# dropped before judging.
FALLBACK_SOURCES = ["openverse", "wikimedia", "google_cse"]

# Sources backed by Google web search, whose query string understands the
# `site:` operator — so a domain scope can be composed in without changing the
# per-source function signature. (Vertex AI Search is excluded: its Discovery
# Engine endpoint takes structured filters, not inline site: operators.)
_SITE_SCOPED_SOURCES = {"serpapi", "google_cse"}


def domain_of(url_or_host: str) -> str:
    """Normalise a URL or host to a bare domain for site:-scoping ('' if blank).
    Strips scheme, path and a leading 'www.'."""
    s = (url_or_host or "").strip()
    if not s:
        return ""
    if "://" not in s:
        s = "//" + s
    host = urllib.parse.urlparse(s).hostname or ""
    return host[4:] if host.startswith("www.") else host


def run_source(source_id: str, mindset_id: str, query: str, n: int = 10, site: str = "") -> List[Candidate]:
    fn = SEARCH_FUNCS.get(source_id)
    if not fn:
        raise Exception(f"unknown source: {source_id}")
    dom = domain_of(site)
    site_scoped = bool(dom) and source_id in _SITE_SCOPED_SOURCES
    effective_query = f"{query} site:{dom}" if site_scoped else query
    try:
        got = fn(mindset_id, effective_query, n=n) or []
    except Exception as exc:
        logging.info(f"source {source_id} failed for {effective_query!r}: {exc}")
        return []
    if site_scoped:
        # A site:-scoped web search targets a curated domain (e.g. a competition
        # gallery), so results are editorial — mark them "caveat" (verify per
        # item) rather than the web source's default "unknown", which would be
        # dropped by apply_rights before judging.
        for c in got:
            c.rights_status = "caveat"
            if not c.license_name or "unknown" in c.license_name.lower():
                c.license_name = f"Editorial / curated source — verify per item ({dom})"
    return got
