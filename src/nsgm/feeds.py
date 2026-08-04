"""Read game deal and release feeds.

Handles the three formats in the wild - RSS 2.0, Atom and RDF - by
ignoring XML namespaces and looking for whichever element names are
present. That way a feed pasted in by hand works without anyone having to
know or care which flavour it is.

Nothing here downloads games. It fetches headlines and links, and the
links open in a normal browser tab.
"""

from __future__ import annotations

import codecs
import gzip
import html
import html.entities
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

USER_AGENT = (
    "Mozilla/5.0 (compatible; NonSteamGameManager/1.0; "
    "+local feed reader)"
)

# Starting points. Every one of these is a public feed of storefront
# deals, giveaways or new releases. Add your own in the sidebar; anything
# that serves RSS or Atom will work.
DEFAULT_FEEDS: list[dict] = [
    {"name": "itch.io sales", "url": "https://itch.io/feed/sales.xml",
     "enabled": True, "tag": "Sales"},
    {"name": "itch.io free games", "url": "https://itch.io/browse/price-free.xml",
     "enabled": True, "tag": "Free"},
    {"name": "itch.io new releases", "url": "https://itch.io/feed/new.xml",
     "enabled": False, "tag": "New"},
    {"name": "r/GameDeals", "url": "https://www.reddit.com/r/GameDeals/.rss",
     "enabled": True, "tag": "Deals"},
    {"name": "r/FreeGameFindings", "url": "https://www.reddit.com/r/FreeGameFindings/.rss",
     "enabled": True, "tag": "Free"},
    {"name": "Free games group", "url": "https://steamcommunity.com/groups/GrabFreeGames/rss/",
     "enabled": True, "tag": "Free"},
    {"name": "Fanatical", "url": "https://steamcommunity.com/groups/wearefanatical/rss/",
     "enabled": False, "tag": "Sales"},
    {"name": "IndieGala", "url": "https://steamcommunity.com/groups/indiegala/rss/",
     "enabled": False, "tag": "Sales"},
    {"name": "Steam news", "url": "https://store.steampowered.com/feeds/news.rss",
     "enabled": False, "tag": "News"},
]


class FeedError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

# html.unescape honours HTML5's legacy entities that need no semicolon,
# so a literal "&notarealentity;" becomes "¬arealentity;". Feed text is
# already decoded by the XML parser, and only genuine, fully terminated
# references should be decoded a second time for double-escaped feeds.
STRICT_REFERENCE = re.compile(r"&(#\d{1,7}|#[xX][0-9A-Fa-f]{1,6}|[A-Za-z][A-Za-z0-9]{0,31});")


def unescape(text: str) -> str:
    """Decode entity references, but only properly terminated ones."""
    def replace(match: "re.Match[str]") -> str:
        body = match.group(1)
        if body.startswith("#"):
            try:
                code = int(body[2:], 16) if body[1] in "xX" else int(body[1:])
            except ValueError:
                return match.group(0)
            return chr(code) if 0 < code < 0x110000 else match.group(0)
        return html.entities.html5.get(body + ";", match.group(0))

    return STRICT_REFERENCE.sub(replace, text)


def _plain(tag: str) -> str:
    """Drop the namespace: ``{http://...}entry`` becomes ``entry``."""
    return tag.rsplit("}", 1)[-1].lower()


def _find(node: ET.Element, *names: str) -> ET.Element | None:
    wanted = {n.lower() for n in names}
    for child in node:
        if _plain(child.tag) in wanted:
            return child
    return None


def _text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return "".join(node.itertext()).strip()


TAGS = re.compile(r"<[^>]+>")
IMG = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.I)
WHITESPACE = re.compile(r"\s+")


def _summarise(raw: str, limit: int = 260) -> tuple[str, str]:
    """Turn feed HTML into plain text, and pull out the first image."""
    if not raw:
        return "", ""
    image_match = IMG.search(raw)
    image = image_match.group(1) if image_match else ""
    text = unescape(TAGS.sub(" ", raw))
    text = WHITESPACE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "\u2026"
    return text, image


def _timestamp(raw: str) -> float:
    """Feed dates come in several shapes. Return epoch seconds, or 0."""
    raw = (raw or "").strip()
    if not raw:
        return 0.0
    try:
        return parsedate_to_datetime(raw).timestamp()
    except (TypeError, ValueError, IndexError):
        pass
    cleaned = raw.replace("Z", "+00:00")
    for candidate in (cleaned, cleaned.split(".")[0]):
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            continue
    return 0.0


def _link_of(node: ET.Element) -> str:
    """RSS puts the URL in the element text, Atom in an href attribute."""
    fallback = ""
    for child in node:
        if _plain(child.tag) != "link":
            continue
        href = child.get("href")
        if href:
            rel = (child.get("rel") or "alternate").lower()
            if rel == "alternate":
                return href
            fallback = fallback or href
        elif (child.text or "").strip():
            return child.text.strip()
    if not fallback:
        guid = _find(node, "guid", "id")
        text = _text(guid)
        if text.startswith("http"):
            return text
    return fallback


def _image_of(node: ET.Element) -> str:
    for child in node:
        name = _plain(child.tag)
        if name == "enclosure" and (child.get("type") or "").startswith("image"):
            return child.get("url") or ""
        if name in ("thumbnail", "content") and child.get("url"):
            if name == "content" and not (child.get("type") or "image").startswith("image"):
                continue
            return child.get("url") or ""
        if name == "group":
            nested = _image_of(child)
            if nested:
                return nested
    return ""


# XML predefines only these five entities. Feeds routinely use HTML ones
# like &nbsp; &mdash; &eacute;, which a strict parser rejects outright.
XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}

# Repairs run on decoded text rather than raw bytes. A UTF-16 feed has a
# null byte between every character, so a byte-level pattern silently
# matches nothing and the repair appears to do its job while changing
# nothing at all.
ENTITY_REF = re.compile(r"&([A-Za-z_:][A-Za-z0-9._:\-]{0,63});")
BARE_AMP = re.compile(r"&(?!#\d+;|#[xX][0-9A-Fa-f]+;|amp;|lt;|gt;|quot;|apos;)")
ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
DOCTYPE = re.compile(r"<!DOCTYPE[^>\[]*(?:\[[^\]]*\])?[^>]*>", re.I | re.S)
XML_DECL = re.compile(r"<\?xml[^>]*\?>", re.I | re.S)
INTERNAL_ENTITY = re.compile(
    r"""<!ENTITY\s+([A-Za-z_:][A-Za-z0-9._:\-]*)\s+(["'])(.*?)\2\s*>""", re.S
)
ENCODING_DECL = re.compile(rb"""encoding\s*=\s*["']([A-Za-z0-9._\-]+)["']""")

BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"),
)


def decode_document(blob: bytes) -> str:
    """Get text out of a feed whatever encoding it arrived in."""
    for bom, encoding in BOMS:
        if blob.startswith(bom):
            return blob[len(bom):].decode(encoding, "replace")

    # A wide encoding with no byte order mark shows up as a sea of nulls.
    head = blob[:120]
    if head.count(b"\x00") > len(head) // 4:
        for encoding in ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
            try:
                text = blob.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
            if "<" in text[:400]:
                return text

    match = ENCODING_DECL.search(blob[:400])
    if match:
        name = match.group(1).decode("ascii", "replace")
        try:
            return blob.decode(name, "replace")
        except LookupError:
            pass
    return blob.decode("utf-8", "replace")


def _escape_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _replace_entity(match: "re.Match[str]", custom: dict) -> str:
    name = match.group(1)
    if name in XML_ENTITIES:
        return match.group(0)
    if name in custom:
        return _escape_text(custom[name])
    codepoint = html.entities.name2codepoint.get(name)
    if codepoint is not None:
        return f"&#{codepoint};"
    expanded = html.entities.html5.get(name + ";")
    if expanded:
        return "".join(f"&#{ord(c)};" for c in expanded)
    return "&amp;" + name + ";"     # unknown: keep it visible as text


def repair_xml(blob: bytes) -> bytes:
    """Make the sloppy XML that feeds actually publish parseable.

    Handled: HTML entities XML never defined, bare ampersands in titles
    like "Rock & Roll", control characters XML forbids, byte order marks
    and stray leading whitespace, a DOCTYPE pointing at a DTD nobody is
    going to fetch, and any encoding at all.

    Entities declared inside the document's own DOCTYPE are collected
    before it is dropped, so a feed defining its own names keeps them.
    """
    text = decode_document(blob).lstrip("\ufeff \t\r\n")
    opening = text.find("<")
    if opening > 0:
        text = text[opening:]

    custom = {m.group(1): m.group(3) for m in INTERNAL_ENTITY.finditer(text)}
    text = DOCTYPE.sub("", text)
    # The declaration is dropped because the output is re-encoded as
    # UTF-8; leaving a stale encoding="UTF-16" behind fails differently.
    text = XML_DECL.sub("", text, count=1)
    text = ILLEGAL_XML.sub("", text)
    text = ENTITY_REF.sub(lambda m: _replace_entity(m, custom), text)
    text = BARE_AMP.sub("&amp;", text)
    return text.lstrip().encode("utf-8")


def _explain(error: ET.ParseError, repaired: bytes) -> str:
    """Say what is actually at the spot that failed, not just where."""
    line, column = getattr(error, "position", (0, 0))
    detail = ""
    try:
        lines = repaired.decode("utf-8", "replace").splitlines()
        if 0 < line <= len(lines):
            row = lines[line - 1]
            window = row[max(0, column - 70):column + 70].strip()
            if window:
                detail = f' Line {line} contains: "{window[:180]}"'
    except (UnicodeDecodeError, IndexError):
        pass
    return f"That did not parse as a feed, even after tidying it up ({error})." + detail


def _parse_xml(blob: bytes) -> ET.Element:
    try:
        return ET.fromstring(blob)
    except ET.ParseError:
        pass
    repaired = repair_xml(blob)
    try:
        return ET.fromstring(repaired)
    except ET.ParseError as second:
        raise FeedError(_explain(second, repaired)) from second


def parse_feed(blob: bytes) -> dict:
    """Parse any of RSS, Atom or RDF into a common shape."""
    if blob[:2] == b"\x1f\x8b":
        blob = gzip.decompress(blob)

    root = _parse_xml(blob)

    root_name = _plain(root.tag)
    if root_name in ("html", "xhtml"):
        raise FeedError(
            "That address is a web page, not a feed. Look for an RSS or Atom "
            "link on the site, often in the footer."
        )
    if root_name not in ("rss", "feed", "rdf", "channel"):
        raise FeedError(f"That is XML, but not a feed ({root_name!r} at the root).")

    channel = _find(root, "channel") or root
    feed_title = _text(_find(channel, "title"))

    entries: list[ET.Element] = []
    for node in root.iter():
        if _plain(node.tag) in ("item", "entry"):
            entries.append(node)

    items = []
    for node in entries:
        title = _text(_find(node, "title"))
        if not title:
            continue
        body = _text(_find(node, "description", "summary", "content", "encoded"))
        summary, inline_image = _summarise(body)
        published = _timestamp(
            _text(_find(node, "pubdate", "published", "updated", "date"))
        )
        items.append(
            {
                "title": unescape(title).strip(),
                "link": _link_of(node),
                "summary": summary,
                "image": _image_of(node) or inline_image,
                "published": published,
            }
        )

    items.sort(key=lambda i: i["published"], reverse=True)
    return {"title": feed_title, "items": items}


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

HTML_START = re.compile(rb"(?is)^\s*(?:<!doctype\s+html|<html[\s>]|<head[\s>]|<meta[\s>])")


def looks_like_html(blob: bytes) -> bool:
    """Web pages are not feeds, and they fail XML parsing in confusing ways."""
    head = blob[:2000].lstrip()
    if HTML_START.match(head):
        return True
    lowered = head.lower()
    return b"<html" in lowered and b"<rss" not in lowered and b"<feed" not in lowered


def discover_feeds(blob: bytes, base_url: str) -> list[str]:
    """Read the feed addresses a page advertises in its <link> tags.

    This is what happens when a feed reader accepts a plain site address:
    the page names its own feed, and the reader follows it. Shares the
    tag parsing used by ``discover`` below rather than repeating it,
    since two copies of this drifted apart once already.
    """
    text = decode_document(blob)[:400000]
    found: list[str] = []
    for tag in LINK_TAG.findall(text):
        attributes = _attributes(tag)
        if "alternate" not in (attributes.get("rel") or "").lower():
            continue
        kind = (attributes.get("type") or "").lower()
        if kind not in FEED_TYPES and not re.search(r"(?i)(rss|atom|rdf)", kind):
            continue
        href = attributes.get("href")
        if not href:
            continue
        target = urllib.parse.urljoin(base_url, unescape(href).strip())
        if target not in found:
            found.append(target)
    return found


def fetch_feed(url: str, etag: str = "", modified: str = "", timeout: int = 20,
               _depth: int = 0) -> dict:
    """Fetch one feed, using conditional headers so repeat checks are cheap.

    Returns ``{"status": "ok"|"unchanged", ...}``. Raises FeedError for
    anything the caller should show the user.
    """
    if not re.match(r"^https?://", url.strip(), re.I):
        raise FeedError("Feed addresses must start with http:// or https://")

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    }
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified

    request = urllib.request.Request(url.strip(), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            blob = response.read()
            # After redirects this may differ from the address asked for,
            # and relative <link href> values must resolve against it.
            final_url = response.geturl() or url
            new_etag = response.headers.get("ETag", "")
            new_modified = response.headers.get("Last-Modified", "")
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return {"status": "unchanged"}
        if exc.code == 429:
            raise FeedError("That site asked us to slow down. Try again shortly.") from exc
        if exc.code in (403, 401):
            raise FeedError(f"That site refused the request (HTTP {exc.code}).") from exc
        raise FeedError(f"HTTP {exc.code} from that address.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise FeedError(f"Could not reach that address ({exc.reason if hasattr(exc, 'reason') else exc}).") from exc

    if looks_like_html(blob):
        if _depth >= 2:
            raise FeedError("That address keeps leading to web pages rather than a feed.")
        for candidate in discover_feeds(blob, final_url):
            try:
                result = fetch_feed(candidate, timeout=timeout, _depth=_depth + 1)
            except FeedError:
                continue
            result["resolved"] = candidate
            return result
        # Nothing advertised, so try the addresses most sites use.
        for path in COMMON_PATHS:
            guess = urllib.parse.urljoin(final_url, path)
            if guess.rstrip("/") == final_url.rstrip("/"):
                continue
            try:
                result = fetch_feed(guess, timeout=timeout, _depth=_depth + 1)
            except FeedError:
                continue
            result["resolved"] = guess
            return result
        raise FeedError(
            "That address is a web page, not a feed, and it does not advertise "
            "one. Look for an RSS or Atom link on the site and use that address."
        )

    parsed = parse_feed(blob)
    return {
        "status": "ok",
        "title": parsed["title"],
        "items": parsed["items"],
        "etag": new_etag,
        "modified": new_modified,
    }


# --------------------------------------------------------------------------
# cache on disk, so opening the tab is instant
# --------------------------------------------------------------------------

def _fetch_raw(url: str, timeout: int = 20) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/rss+xml,application/atom+xml,application/xml,*/*",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        blob = response.read(2_000_000)
        kind = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        return blob, kind


LINK_TAG = re.compile(r"(?is)<link\b[^>]*>")
ATTRIBUTE = re.compile(
    r"""(?is)([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))"""
)
FEED_TYPES = ("application/rss+xml", "application/atom+xml",
              "application/rdf+xml", "application/feed+json", "application/xml")

# Tried only when a page advertises no feed of its own.
COMMON_PATHS = ("/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml", "/index.xml", "/feeds/all.atom.xml")


def _attributes(tag: str) -> dict:
    found = {}
    for match in ATTRIBUTE.finditer(tag):
        value = match.group(2) or match.group(3) or match.group(4) or ""
        found[match.group(1).lower()] = value.strip()
    return found


def _describe(url: str) -> dict | None:
    """Fetch a candidate and confirm it really is a feed."""
    try:
        blob, _kind = _fetch_raw(url)
        parsed = parse_feed(blob)
    except (FeedError, urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, OSError, ValueError):
        return None
    items = parsed.get("items") or []
    return {
        "url": url,
        "title": parsed.get("title") or url,
        "count": len(items),
        "newest": items[0]["title"][:110] if items else "",
    }


def discover(url: str, timeout: int = 20) -> list[dict]:
    """Turn whatever someone pasted into a list of usable feed addresses.

    Handles the three things people actually paste: a feed address, a
    site's home page, or a site address with no scheme on the front.
    """
    url = url.strip()
    if not url:
        raise FeedError("Paste an address first.")
    bare = not re.match(r"^https?://", url, re.I)
    if bare:
        url = "https://" + url

    try:
        blob, kind = _fetch_raw(url, timeout)
    except urllib.error.HTTPError as exc:
        raise FeedError(f"That address answered HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = str(getattr(exc, "reason", exc))
        # Prepending https to a bare address is right for almost every site,
        # but silently retrying over http would be a poor trade, so say so.
        if bare and ("SSL" in reason or "WRONG_VERSION" in reason):
            raise FeedError(
                "That address did not answer over https. If the site really is "
                "http only, type the address with http:// at the front."
            ) from exc
        raise FeedError(f"Could not reach that address ({reason}).") from exc

    # Already a feed?
    if kind in FEED_TYPES or kind.endswith("xml"):
        try:
            parsed = parse_feed(blob)
            items = parsed.get("items") or []
            return [{
                "url": url,
                "title": parsed.get("title") or url,
                "count": len(items),
                "newest": items[0]["title"][:110] if items else "",
            }]
        except FeedError:
            pass

    found: list[dict] = []
    seen: set[str] = set()

    text = blob.decode("utf-8", "replace")
    for tag in LINK_TAG.findall(text):
        attrs = _attributes(tag)
        rel = attrs.get("rel", "").lower()
        kind_attr = attrs.get("type", "").lower()
        href = attrs.get("href", "")
        if not href or "alternate" not in rel or kind_attr not in FEED_TYPES:
            continue
        absolute = urllib.parse.urljoin(url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        described = _describe(absolute)
        if described:
            described["title"] = attrs.get("title") or described["title"]
            found.append(described)

    if found:
        return found

    # Nothing advertised, so try the addresses most sites use.
    root = f"{urllib.parse.urlparse(url).scheme}://{urllib.parse.urlparse(url).netloc}"
    for suffix in COMMON_PATHS:
        candidate = root + suffix
        if candidate in seen:
            continue
        seen.add(candidate)
        described = _describe(candidate)
        if described and described["count"]:
            found.append(described)
            break

    if not found:
        raise FeedError(
            "No feed found there. Many sites link theirs in the footer, often "
            "as an orange icon or a link marked RSS — paste that address instead."
        )
    return found


def cache_file(config_dir: Path) -> Path:
    return config_dir / "feed-cache.json"


def load_cache(config_dir: Path) -> dict:
    path = cache_file(config_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(config_dir: Path, cache: dict) -> None:
    path = cache_file(config_dir)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(cache), "utf-8")
        tmp.replace(path)
    except OSError:
        pass


def refresh(feeds: list[dict], config_dir: Path, progress=None) -> tuple[list[dict], list[str]]:
    """Update every enabled feed and return the merged, newest-first list."""
    cache = load_cache(config_dir)
    notes: list[str] = []
    merged: list[dict] = []

    for feed in feeds:
        if not feed.get("enabled", True) or not feed.get("url"):
            continue
        name = feed.get("name") or feed["url"]
        if progress:
            progress(name)
        entry = cache.get(feed["url"], {})
        address = entry.get("resolved") or feed["url"]
        try:
            result = fetch_feed(address, entry.get("etag", ""), entry.get("modified", ""))
            if result.get("resolved") and result["resolved"] != feed["url"]:
                notes.append(f"{name}: that was a web page; using its feed at "
                             f"{result['resolved']}")
            if result["status"] == "ok":
                entry = {
                    "resolved": result.get("resolved") or entry.get("resolved", ""),
                    "items": result["items"][:60],
                    "etag": result.get("etag", ""),
                    "modified": result.get("modified", ""),
                    "fetched": time.time(),
                    "title": result.get("title", ""),
                }
                cache[feed["url"]] = entry
            else:
                entry["fetched"] = time.time()
                cache[feed["url"]] = entry
        except FeedError as exc:
            notes.append(f"{name}: {exc}")
            if not entry.get("items"):
                continue

        for item in entry.get("items", []):
            merged.append({**item, "feed": name, "tag": feed.get("tag", "")})

    save_cache(config_dir, cache)
    merged.sort(key=lambda i: i["published"], reverse=True)
    return merged, notes


def cached_items(feeds: list[dict], config_dir: Path) -> list[dict]:
    """Whatever we already have, without touching the network."""
    cache = load_cache(config_dir)
    merged: list[dict] = []
    for feed in feeds:
        if not feed.get("enabled", True):
            continue
        entry = cache.get(feed.get("url", ""), {})
        for item in entry.get("items", []):
            merged.append({**item, "feed": feed.get("name", ""), "tag": feed.get("tag", "")})
    merged.sort(key=lambda i: i["published"], reverse=True)
    return merged
