"""Read a release blog as a store catalogue.

Plenty of sites publish games as blog posts rather than as a JSON file:
one post per release, a cover, a few "Key: value" lines (company, sizes,
languages), and a list of download mirrors, often with a magnet link
beside them. WordPress and most other blog software lay that out the same
way, so the store can read such a site directly.

Two things are read here, both without running any of the page's script:

* **a listing** - the front page or a category page, and the pages after
  it - which becomes a shelf of games, one per post that offers
  downloads;
* **one post** - which becomes a structured list of downloads: the
  mirrors in the order the site recommends them, the files each mirror
  holds, which of those files are optional extras, and any magnet or
  .torrent links.

Nothing is downloaded here. ``store`` decides which route to take and
``hosters`` turns a file host's page into the file itself.
"""

from __future__ import annotations

import html
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MAX_PAGE = 6 * 1024 * 1024


class SiteError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# a small, forgiving document tree
# --------------------------------------------------------------------------

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr"}
# Starting one of these ends an open <p>, as browsers do.
BLOCK = {"address", "article", "aside", "blockquote", "div", "dl", "fieldset", "figure",
         "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li",
         "main", "nav", "ol", "p", "pre", "section", "table", "ul"}
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict, parent: "Node | None" = None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list = []          # Node or str
        self.parent = parent

    def get(self, key: str) -> str:
        return self.attrs.get(key, "")

    @property
    def classes(self) -> set:
        return set(self.get("class").lower().split())

    def walk(self):
        """Every node below this one, in document order."""
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.walk()

    def find_all(self, tag: str = "", cls: str = "") -> list:
        return [n for n in self.walk()
                if (not tag or n.tag == tag) and (not cls or cls in n.classes)]

    def find(self, tag: str = "", cls: str = "") -> "Node | None":
        for node in self.walk():
            if (not tag or node.tag == tag) and (not cls or cls in node.classes):
                return node
        return None

    def text(self) -> str:
        parts: list[str] = []
        self._text(parts)
        return re.sub(r"[ \t\r\f\v ]+", " ", "".join(parts))

    def _text(self, parts: list) -> None:
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            elif child.tag in ("script", "style"):
                continue
            else:
                if child.tag == "br" or child.tag in BLOCK:
                    parts.append("\n")
                child._text(parts)
                if child.tag in BLOCK:
                    parts.append("\n")

    def inside(self, tag: str = "", cls: str = "") -> bool:
        node = self.parent
        while node is not None:
            if (not tag or node.tag == tag) and (not cls or cls in node.classes):
                return True
            node = node.parent
        return False


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {})
        self.stack = [self.root]

    def _close(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        values = {k.lower(): (v or "") for k, v in attrs}
        open_tags = [n.tag for n in self.stack]
        if tag in BLOCK and "p" in open_tags:
            # Only the innermost paragraph, and only if nothing block-level
            # sits between it and here.
            top = self.stack[-1]
            if top.tag == "p":
                self.stack.pop()
        if tag == "li":
            # A new item ends the previous one in the same list.
            for index in range(len(self.stack) - 1, 0, -1):
                current = self.stack[index].tag
                if current in ("ul", "ol"):
                    break
                if current == "li":
                    del self.stack[index:]
                    break
        node = Node(tag, values, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID:
            self._close(tag.lower())

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in VOID:
            return
        if tag in ("ul", "ol"):
            # Closing a list closes whatever item was left open inside it.
            self._close(tag)
            return
        self._close(tag)

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse(text: str) -> Node:
    builder = _Builder()
    try:
        builder.feed(text)
        builder.close()
    except Exception:
        pass        # whatever was parsed before the bad markup is still useful
    return builder.root


# --------------------------------------------------------------------------
# telling files, mirrors and torrents apart
# --------------------------------------------------------------------------

FILE_EXT = re.compile(
    r"(?i)\.(rar|zip|7z|exe|msi|bin|iso|img|gz|tgz|xz|bz2|zst|tar|cab|dat|"
    r"appimage|deb|rpm|dmg|pkg|jar|apk|r\d{2}|\d{3})$"
)
MAGNET = re.compile(r"(?i)^magnet:\?")
BTIH = re.compile(r"(?i)urn:btih:([a-f0-9]{40}|[a-z2-7]{32})")

# Files a release ships beside the game that it can be installed without.
OPTIONAL = re.compile(r"(?i)(?:^|[-_. ])(optional|bonus)(?:[-_. ]|$)")
# A language picked separately, e.g. "...-selective-french.bin".
SELECTIVE = re.compile(r"(?i)(?:^|[-_. ])selective[-_. ]+([a-z]+)")

TORRENT_HEADING = re.compile(r"(?i)torrent|magnet")
DOWNLOAD_HEADING = re.compile(r"(?i)download|mirror|direct|links?\b|filehost")
LABEL_PREFIX = re.compile(r"(?i)^\s*(file\s*hoster|filehost|host|mirror|server)\s*[:#-]?\s*")

# Hosts that only wrap links (encrypted pastes, link shorteners): there is
# nothing to download from them without a browser.
WRAPPER_HINT = re.compile(r"(?i)(^|\.)paste\.|privatebin|/\?[0-9a-f]{12,}#")


def file_name(url: str, text: str = "") -> str:
    """The file a link stands for, from its path, its #fragment or its text."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return ""
    last = urllib.parse.unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1])
    if FILE_EXT.search(last):
        return last
    fragment = urllib.parse.unquote(parsed.fragment or "")
    if fragment and FILE_EXT.search(fragment) and "/" not in fragment:
        return fragment
    label = re.sub(r"\s+", "", text or "")
    if label and FILE_EXT.search(label) and len(label) < 200 and not WRAPPER_HINT.search(url):
        return label
    return ""


def is_optional(name: str, language: str = "english") -> bool:
    match = SELECTIVE.search(name)
    if match:
        return match.group(1).lower() != (language or "english").lower()
    return bool(OPTIONAL.search(name))


def magnet_info(uri: str) -> dict:
    query = uri.split("?", 1)[1] if "?" in uri else ""
    fields = urllib.parse.parse_qs(query)
    match = BTIH.search(" ".join(fields.get("xt", [])))
    size = 0
    for raw in fields.get("xl", []):
        if raw.isdigit():
            size = int(raw)
    return {"hash": match.group(1).lower() if match else "",
            "name": (fields.get("dn") or [""])[0], "size": size}


def _clean_label(text: str, fallback: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = LABEL_PREFIX.sub("", text).strip(" :|-[]")
    return text[:60] or fallback


def read_downloads(content: Node, base_url: str, language: str = "english") -> dict:
    """The mirrors and torrents in one post's body.

    A mirror is the list item that introduces it, with every file link
    inside it or folded away just after it; without list items, the host
    the files live on. Mirrors stay in the order the page lists them, so
    the site's own recommendation is tried first.
    """
    mirrors: dict[str, dict] = {}
    torrents: dict[str, dict] = {}
    section = ""            # "", "direct", "torrent" or "other"
    item_label = ""         # text of the list item currently open
    item_node = None

    for node in content.walk():
        if node.tag in HEADINGS:
            item_node, item_label = None, ""
            heading = node.text().strip()
            if TORRENT_HEADING.search(heading):
                section = "torrent"
            elif DOWNLOAD_HEADING.search(heading):
                section = "direct"
            elif section:
                section = "other"
            continue
        if node.tag == "li" and not node.inside(cls="su-spoiler-content"):
            item_node = node
            first = node.find("a")
            item_label = first.text() if first is not None else node.text()[:80]
            continue
        if node.tag != "a":
            continue

        href = html.unescape(node.get("href").strip())
        if not href:
            continue
        absolute = href if MAGNET.match(href) else urllib.parse.urljoin(base_url, href)
        row = item_label if item_node is not None else ""

        if MAGNET.match(absolute):
            info = magnet_info(absolute)
            key = info["hash"] or absolute[:120]
            if key not in torrents:
                torrents[key] = {"kind": "magnet", "url": absolute,
                                 "label": _clean_label(row, "magnet"),
                                 "name": info["name"], "hash": info["hash"],
                                 "size": info["size"]}
            continue

        parsed = urllib.parse.urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        if parsed.path.lower().endswith(".torrent"):
            key = absolute.lower()
            torrents.setdefault(key, {"kind": "torrent", "url": absolute,
                                      "label": _clean_label(row, "torrent"),
                                      "name": file_name(absolute), "hash": "", "size": 0})
            continue
        if section in ("other", "torrent"):
            continue
        name = file_name(absolute, node.text())
        if not name or WRAPPER_HINT.search(absolute):
            continue

        host = parsed.netloc.lower().removeprefix("www.")
        key = f"item:{id(item_node)}" if item_node is not None else f"host:{host}"
        mirror = mirrors.get(key)
        if mirror is None:
            label = item_label if item_node is not None else ""
            if file_name("https://x/" + re.sub(r"\s+", "", label)):
                label = ""                  # the item's first link is itself a file
            mirror = mirrors[key] = {"host": host, "label": _clean_label(label, host),
                                     "files": []}
        if any(f["name"].lower() == name.lower() for f in mirror["files"]):
            continue
        mirror["files"].append({"url": absolute, "name": name,
                                "optional": is_optional(name, language)})

    return {"mirrors": [m for m in mirrors.values() if m["files"]],
            "torrents": list(torrents.values())}


# --------------------------------------------------------------------------
# a post: title, details, cover, downloads
# --------------------------------------------------------------------------

FIELD = re.compile(
    r"(?im)^\s*(genres?\s*/\s*tags|genres?|tags|compan(?:y|ies)|developers?|publishers?|"
    r"languages?|original size|repack size|download size|size|version|release date)"
    r"\s*:\s*(.+?)\s*$"
)
SIZE = re.compile(r"(?i)(\d+(?:[.,]\d+)?)\s*(?:/\s*\d+(?:[.,]\d+)?\s*)?(kb|mb|gb|tb)")
UNITS = {"kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3, "tb": 1024 ** 4}

VERSION_TAIL = re.compile(
    r"(?i)\s*(?:,|\s[-–—])?\s*\(?\b(v\s?\d[\w.\-]*(?:\s?\w+)?|build\s?\d[\w.\-]*|"
    r"r\d{3,}[\w.]*|\d+\.\d+[\w.\-]*)\)?\s*$"
)
EXTRAS_TAIL = re.compile(r"\s+\+\s+.*$")
EDITION_TAIL = re.compile(
    r"(?i)\s*(?:[:–—-]\s*|\s)(?:the\s+)?[\w’'\s]{0,40}?\b(edition|bundle|collection|"
    r"remaster(?:ed)?|director'?s cut|complete|anniversary|deluxe|goty|definitive|"
    r"ultimate|gold|premium)\b.*$"
)

# Posts that talk about releases rather than being one.
NEWS_TITLE = re.compile(r"(?i)\b(upcoming|digest|updates? list|announcement|news|"
                        r"weekly|monthly|faq|donat|repack updated|update notice)\b")


def parse_size(text: str) -> int:
    """'4.7 GB' or '688/823 MB' as bytes; the smaller figure of a range."""
    match = SIZE.search(text or "")
    if not match:
        return 0
    try:
        number = float(match.group(1).replace(",", "."))
    except ValueError:
        return 0
    return int(number * UNITS[match.group(2).lower()])


def split_title(title: str) -> dict:
    """'Game: Some Edition, v1.0.9 + Bonus OST' into its parts."""
    title = re.sub(r"\s+", " ", title or "").strip()
    extras = ""
    match = EXTRAS_TAIL.search(title)
    if match:
        extras = match.group(0).strip()
        title = title[:match.start()].strip()
    version = ""
    match = VERSION_TAIL.search(title)
    if match and match.start() > 0:
        version = match.group(1).strip()
        title = title[:match.start()].rstrip(" ,–—-:").strip()
    art = EDITION_TAIL.sub("", title).strip(" ,–—-:") or title
    return {"name": title, "version": version, "extras": extras, "art_name": art}


def _image_of(node: Node) -> str:
    for img in node.find_all("img"):
        if "emoji" in img.classes:
            continue
        try:
            if 0 < int(img.get("width") or 999) < 60 or 0 < int(img.get("height") or 999) < 60:
                continue
        except ValueError:
            pass
        src = img.get("data-src") or img.get("data-lazy-src") or img.get("src")
        if src and not src.startswith("data:"):
            return src
    return ""


def _content_of(article: Node) -> Node:
    for cls in ("entry-content", "post-content", "entry", "content", "post-body"):
        found = article.find(cls=cls)
        if found is not None:
            return found
    return article


def _title_of(article: Node, base_url: str) -> tuple[str, str]:
    for node in article.walk():
        if node.tag in HEADINGS and ("entry-title" in node.classes or "post-title" in node.classes):
            link = node.find("a")
            href = urllib.parse.urljoin(base_url, link.get("href")) if link is not None else ""
            return node.text().strip(), href
    for link in article.find_all("a"):
        if "bookmark" in link.get("rel").lower().split() and link.text().strip():
            return link.text().strip(), urllib.parse.urljoin(base_url, link.get("href"))
    heading = next((n for n in article.walk() if n.tag in HEADINGS), None)
    return (heading.text().strip() if heading is not None else ""), ""


DESCRIPTION = re.compile(r"(?i)^\s*(game\s+)?(description|about the game|overview)\s*:?\s*$")
FEATURES = re.compile(r"(?i)^\s*(game\s+)?features|^\s*system requirements|^\s*screenshots")


def _blurb(content: Node) -> str:
    """The game's description: the text under a 'Description' heading.

    Blogs often fold the description away in a spoiler ("click to show")
    whose title says what it is, so those count as headings too.
    """
    for node in content.walk():
        cls = " ".join(node.classes)
        if not (node.tag == "summary" or ("spoiler" in cls and "title" in cls)):
            continue
        if not DESCRIPTION.match(node.text()):
            continue
        siblings = [c for c in node.parent.children if isinstance(c, Node)]
        after = siblings[siblings.index(node) + 1:] if node in siblings else []
        lines: list[str] = []
        for sibling in after:
            for line in sibling.text().split("\n"):
                line = line.strip()
                if not line:
                    continue
                if FEATURES.match(line) or sum(len(x) for x in lines) > 600:
                    return re.sub(r"\s+", " ", " ".join(lines))[:600]
                lines.append(line)
        if lines:
            return re.sub(r"\s+", " ", " ".join(lines))[:600]

    capture, parts = False, []
    for child in content.walk():
        if child.tag in HEADINGS:
            if capture:
                break
            capture = bool(re.search(r"(?i)description|about the game|overview", child.text()))
            continue
        if capture and child.tag in ("p", "div") and not child.find("p"):
            text = child.text().strip()
            if text:
                parts.append(text)
        if sum(len(p) for p in parts) > 600:
            break
    text = re.sub(r"\s+", " ", " ".join(parts)).strip()
    return text[:600]


def read_post(article: Node, base_url: str, language: str = "english") -> dict:
    title, url = _title_of(article, base_url)
    content = _content_of(article)
    fields: dict[str, str] = {}
    for key, value in FIELD.findall(content.text()):
        key = re.sub(r"\s+", " ", key.lower())
        fields.setdefault(key, re.sub(r"\s+", " ", value).strip()[:200])

    tags: list[str] = []
    for link in content.find_all("a"):
        href = link.get("href").lower()
        if re.search(r"/(tag|genre)s?/", href) or "tag" in link.get("rel").lower().split():
            text = link.text().strip()
            if text and text not in tags and len(text) <= 24:
                tags.append(text)
    if not tags:
        raw = fields.get("genres/tags") or fields.get("genres") or fields.get("tags") or ""
        tags = [t.strip() for t in raw.split(",") if t.strip()][:8]

    company = (fields.get("companies") or fields.get("company") or fields.get("developer")
               or fields.get("developers") or fields.get("publisher") or "")
    when = article.find("time")
    parts = split_title(title)
    return {
        "title": title,
        "url": url or base_url,
        **parts,
        "tags": tags[:8],
        "company": company,
        "languages": fields.get("languages") or fields.get("language") or "",
        "size": parse_size(fields.get("original size") or fields.get("size") or ""),
        "download_size": parse_size(fields.get("repack size") or fields.get("download size")
                                    or ""),
        "cover": urllib.parse.urljoin(base_url, _image_of(content)) if _image_of(content) else "",
        "blurb": _blurb(content),
        "date": when.get("datetime")[:10] if when is not None else "",
        "downloads": read_downloads(content, url or base_url, language),
    }


def has_downloads(post: dict) -> bool:
    found = post.get("downloads") or {}
    return bool(found.get("mirrors") or found.get("torrents"))


def _articles(root: Node) -> list:
    found = root.find_all("article")
    if not found:
        found = [n for n in root.walk() if n.tag == "div" and "hentry" in n.classes
                 and not n.inside(cls="hentry")]
    return [a for a in found if not a.inside("article")]


def next_page(root: Node, base_url: str) -> str:
    for node in root.walk():
        if node.tag in ("a", "link") and "next" in node.get("rel").lower().split():
            return urllib.parse.urljoin(base_url, node.get("href"))
    for node in root.find_all("a"):
        if "next" in node.classes and node.get("href"):
            return urllib.parse.urljoin(base_url, node.get("href"))
    return ""


def read_listing(text: str, base_url: str, language: str = "english") -> dict:
    """The posts on one page of a site, and the address of the next page.

    A front page mixes releases with news ("Upcoming", "Updates digest").
    When some posts on the page offer downloads, the ones that offer none
    are news and are left out. When none do, the page only shows excerpts,
    so every post is kept and its own page is read when it is installed.
    """
    root = parse(text)
    articles = _articles(root)
    if not articles:
        articles = [root]
    posts = []
    for article in articles:
        post = read_post(article, base_url, language)
        # A pinned post sits on top of the front page whatever its age.
        post["sticky"] = "sticky" in article.classes
        posts.append(post)
    # News posts can carry links too (a digest of updates links patches),
    # but they never describe one release with a size of its own.
    posts = [p for p in posts if p["name"] and not (
        NEWS_TITLE.search(p["title"]) and not (p["size"] or p["download_size"]))]
    if any(has_downloads(p) for p in posts):
        posts = [p for p in posts if has_downloads(p)]
    else:
        posts = [p for p in posts if not NEWS_TITLE.search(p["title"])]
    return {"posts": posts, "next": next_page(root, base_url),
            "last_page": last_page(root), "site": site_name(text)}


PAGE_NUMBER = re.compile(r"(?i)(?:/page/|[?&](?:paged|page)=)(\d{1,6})/?(?:[?#]|$)")


def last_page(root: Node) -> int:
    """The highest page number the pagination links to, or 0."""
    best = 0
    for node in root.find_all("a"):
        match = PAGE_NUMBER.search(node.get("href"))
        if match:
            best = max(best, int(match.group(1)))
    return best


def site_name(text: str) -> str:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", text or "")
    title = html.unescape(re.sub(r"\s+", " ", match.group(1))).strip() if match else ""
    return re.split(r"\s+[-–|—]\s+", title)[0].strip()[:60] if title else ""


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def fetch(url: str, timeout: int = 20) -> tuple[str, str]:
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.8",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            blob = response.read(MAX_PAGE)
            final = response.geturl()
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as exc:
        raise SiteError(f"The site answered HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SiteError(f"Could not reach the site ({getattr(exc, 'reason', exc)}).") from exc
    try:
        return blob.decode(charset, "replace"), final
    except LookupError:
        return blob.decode("utf-8", "replace"), final


def looks_like_site(blob: bytes) -> bool:
    head = blob[:2048].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html", b"<head", b"<!--")) or b"<html" in head


def read_site(url: str, pages: int = 5, timeout: int = 20, language: str = "english",
              text: str = "", progress=None) -> dict:
    """Every release post over the first ``pages`` pages of a site.

    ``text`` is the first page when the caller already has it.
    """
    posts: list[dict] = []
    seen: set = set()
    address, visited, title = url, [], ""
    for number in range(max(1, pages)):
        if number or not text:
            try:
                text, address = fetch(address, timeout)
            except SiteError:
                if not posts and not number:
                    raise
                break            # a later page failing still leaves the shelf so far
        visited.append(address)
        if progress:
            progress(number + 1, len(posts))
        if not title:
            match = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
            title = html.unescape(re.sub(r"\s+", " ", match.group(1))).strip() if match else ""
        page = read_listing(text, address, language)
        for post in page["posts"]:
            key = post["url"].rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            posts.append(post)
        if not page["next"] or page["next"] in visited:
            break
        address = page["next"]
        time.sleep(0.3)          # be gentle with somebody else's server
    name = re.split(r"\s+[-–|—]\s+", title)[0].strip() if title else ""
    return {"name": name[:60], "posts": posts, "pages": visited}


def read_one(url: str, timeout: int = 20, language: str = "english") -> dict:
    """A single post's page, read fresh for installing."""
    text, final = fetch(url, timeout)
    root = parse(text)
    articles = _articles(root)
    article = articles[0] if len(articles) == 1 else None
    if article is None:
        # A post page with related posts below it: the one whose own
        # address this is, or the first with downloads.
        for candidate in articles:
            title, link = _title_of(candidate, final)
            if link and link.rstrip("/") == final.rstrip("/"):
                article = candidate
                break
    if article is None:
        posts = [read_post(a, final, language) for a in articles] or [read_post(root, final, language)]
        return next((p for p in posts if has_downloads(p)), posts[0])
    return read_post(article, final, language)
