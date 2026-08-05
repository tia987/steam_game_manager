"""Work out what a page's download button actually points at.

Given the address of a project's download page, this returns a ranked
list of the files it offers, so a link can be picked and handed to
``download.download`` without opening a browser.

It works in two ways, and the difference matters:

**Host adapters.** GitHub, GitLab, Codeberg and SourceForge all publish
release metadata through an API. Where one of those is recognised the
answer is exact: real version numbers, real file sizes, no guessing.

**Reading the page.** Everywhere else the HTML is parsed and every link
that looks like a file is scored. This is honest guesswork. It works on
ordinary static download pages and fails on ones that build their links
in the browser, which is reported rather than hidden.

Nothing is ever downloaded automatically. Resolution produces
candidates; a person picks one. A page's own published checksums are
collected on the way past, so a finished download can be checked against
what the publisher said it should be.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent import futures
from html.parser import HTMLParser
from pathlib import PurePosixPath

from . import archive, catalog, torrents
from . import proc

USER_AGENT = "NonSteamGameManager/1.0 (+local download helper)"
MAX_PAGE = 4 * 1024 * 1024


class ResolveError(RuntimeError):
    pass


# Extensions that are plausibly "the thing you came here for".
DOWNLOADABLE = (
    ".exe", ".msi", ".msix", ".appx", ".appxbundle",
    ".zip", ".7z", ".rar", ".gz", ".tgz", ".xz", ".bz2", ".zst",
    ".dmg", ".pkg", ".deb", ".rpm", ".appimage", ".snap", ".flatpakref",
    ".jar", ".apk", ".iso", ".img", ".run", ".bin",
)

# Links that are never the download, however they are labelled.
NOT_A_DOWNLOAD = re.compile(
    r"(?i)(^mailto:|^javascript:|^tel:|^#|/(login|signin|signup|register|donate|"
    r"sponsor|patreon|paypal|privacy|terms|cookie)s?(/|$|\?))"
)

# Some projects publish over BitTorrent rather than plain HTTP, which is
# how large ISOs and archives are often shared. A magnet is a reference,
# not a file: there is nothing here to fetch over HTTP, so it is reported
# as its own kind and handed to whatever torrent client is installed.
MAGNET_SCHEME = re.compile(r"(?i)^magnet:\?")
BTIH = re.compile(r"(?i)urn:btih:([a-f0-9]{40}|[a-z2-7]{32})")


def parse_magnet(uri: str) -> dict:
    """Pull the readable parts out of a magnet link."""
    query = uri.split("?", 1)[1] if "?" in uri else ""
    fields = urllib.parse.parse_qs(query, keep_blank_values=False)

    topic = " ".join(fields.get("xt", []))
    match = BTIH.search(topic)
    size = 0
    for raw in fields.get("xl", []):
        if raw.isdigit():
            size = int(raw)
            break

    return {
        "hash": (match.group(1).lower() if match else ""),
        "name": (fields.get("dn", [""])[0] or "").strip(),
        "trackers": fields.get("tr", []),
        "web_seeds": fields.get("ws", []),
        "size": size,
    }


def open_magnet(uri: str) -> dict:
    """Hand a magnet to the system's registered torrent handler.

    This is exactly what a browser does when a magnet link is clicked;
    the tool has no BitTorrent client of its own and does not fetch
    anything itself. What sits on the other end of a magnet is between
    you and whoever published it.
    """
    if not MAGNET_SCHEME.match(uri or ""):
        raise ResolveError("That is not a magnet link.")
    import platform as _platform
    import subprocess

    system = _platform.system()
    try:
        if system == "Windows":
            import os
            os.startfile(uri)  # noqa: S606 - the OS picks the handler
        elif system == "Darwin":
            proc.Popen(["open", uri])
        else:
            proc.Popen(["xdg-open", uri])
    except (OSError, subprocess.SubprocessError) as exc:
        raise ResolveError(
            "No torrent client is registered for magnet links on this computer."
        ) from exc
    return {"opened": True, "magnet": parse_magnet(uri)}


DOWNLOAD_WORDS = re.compile(
    r"(?i)\b(download|télécharger|descargar|herunterladen|scarica|baixar|"
    r"ladda ner|pobierz|скачать|下载|ダウンロード|installer|get it)\b"
)

SOURCE_WORDS = re.compile(r"(?i)\b(source|src|sources)\b")
CHECKSUM = re.compile(r"\b([a-fA-F0-9]{64})\b")
VERSIONISH = re.compile(r"\d+[._]\d+")


# --------------------------------------------------------------------------
# reading the page
# --------------------------------------------------------------------------

class _Collector(HTMLParser):
    """Pull out links, their visible text, and any embedded metadata."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict] = []
        self.ld_json: list[str] = []
        self.meta_refresh: str = ""
        self.next_link: str = ""
        self.script_count = 0
        self.row = 0
        self._open: list[dict] = []
        self._in_ld = False

    def handle_starttag(self, tag, attrs):
        values = {k.lower(): (v or "") for k, v in attrs}
        if tag in ("tr", "li", "article", "section"):
            # Listings repeat one container per entry. Counting them groups
            # each row's links together, so a magnet can be matched with the
            # title beside it rather than whatever link came last on the page.
            self.row += 1
        if tag == "link" and "next" in values.get("rel", "").lower():
            self.next_link = self.next_link or values.get("href", "")
        if tag == "a" and values.get("href"):
            if "next" in values.get("rel", "").lower():
                self.next_link = self.next_link or values["href"]
            entry = {
                "href": values["href"],
                "row": self.row,
                "text": "",
                "hint": " ".join(
                    values.get(key, "")
                    for key in ("rel", "class", "id", "title", "aria-label", "download")
                ),
                "explicit": "download" in values,
            }
            self.links.append(entry)
            self._open.append(entry)
        elif tag == "script":
            self.script_count += 1
            if values.get("type", "").lower() == "application/ld+json":
                self._in_ld = True
                self.ld_json.append("")
        elif tag == "meta":
            if values.get("http-equiv", "").lower() == "refresh":
                match = re.search(r"(?i)url\s*=\s*(.+)$", values.get("content", ""))
                if match:
                    self.meta_refresh = match.group(1).strip().strip("'\"")

    def handle_endtag(self, tag):
        if tag == "a" and self._open:
            self._open.pop()
        elif tag == "script":
            self._in_ld = False

    def handle_data(self, data):
        if self._in_ld and self.ld_json:
            self.ld_json[-1] += data
        for entry in self._open:
            if len(entry["text"]) < 160:
                entry["text"] += data


def _fetch(url: str, timeout: int, accept: str) -> tuple[bytes, str]:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": accept}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(MAX_PAGE), response.geturl()
    except urllib.error.HTTPError as exc:
        raise ResolveError(f"That page answered HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ResolveError(f"Could not reach that page ({reason}).") from exc


def _decode(blob: bytes) -> str:
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return blob.decode(encoding)
        except UnicodeDecodeError:
            continue
    return blob.decode("utf-8", "replace")


def file_name_of(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    return urllib.parse.unquote(PurePosixPath(path).name)


def looks_like_file(url: str) -> bool:
    name = file_name_of(url).lower()
    if name.endswith(".tar.gz") or name.endswith(".tar.xz") or name.endswith(".tar.bz2"):
        return True
    return name.endswith(DOWNLOADABLE)


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

# Formats that belong to no particular platform. catalog.score_asset is
# tuned for game release assets and rejects things like a plain .iso, so
# these get judged on whether the file name names a *different* OS.
NEUTRAL_EXT = (".iso", ".img", ".zip", ".7z", ".jar", ".tar.gz", ".tgz", ".tar.xz", ".bin")

FOREIGN_OS = {
    "Windows": re.compile(r"(?i)(macos|mac-|darwin|osx|linux|\.dmg|\.pkg|\.deb|\.rpm|appimage)"),
    "Linux": re.compile(r"(?i)(windows|win32|win64|win-|macos|mac-|darwin|osx|\.exe|\.msi|\.dmg|\.pkg)"),
    "Darwin": re.compile(r"(?i)(windows|win32|win64|win-|linux|\.exe|\.msi|\.deb|\.rpm|appimage)"),
}


def _neutral_fit(name: str, system: str) -> bool:
    lowered = name.lower()
    if not lowered.endswith(NEUTRAL_EXT):
        return False
    foreign = FOREIGN_OS.get(system, FOREIGN_OS["Windows"])
    return not foreign.search(lowered)


def score_link(url: str, text: str, hint: str, system: str) -> tuple[int, bool]:
    """Rank one link, and say separately whether it suits this computer.

    The two answers have to stay apart. A macOS disk image on a Windows
    machine can still be the most prominent link on the page, so it may
    rank highly, but it must never be offered as something to run here.
    """
    name = file_name_of(url)
    if not name:
        return -100, False

    platform_fit = catalog.score_asset(name, system)
    fits = platform_fit > 0
    if fits:
        score = platform_fit
    elif _neutral_fit(name, system):
        fits = True
        score = 25
    elif looks_like_file(url):
        score = 1
    else:
        return -100, False

    blob = f"{text} {hint}"
    if DOWNLOAD_WORDS.search(blob):
        score += 35
    if "download" in hint.lower():
        score += 20
    if SOURCE_WORDS.search(blob) and not DOWNLOAD_WORDS.search(blob):
        score -= 40
    if VERSIONISH.search(name):
        score += 10
    if urllib.parse.urlparse(url).scheme == "http":
        score -= 15
    return score, fits


def _label(text: str, name: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    return cleaned[:90] if cleaned else name


# --------------------------------------------------------------------------
# host adapters
# --------------------------------------------------------------------------

FORGE_HOSTS = {
    "github.com": ("https://api.github.com/repos/{path}/releases/latest", "github"),
    "codeberg.org": ("https://codeberg.org/api/v1/repos/{path}/releases/latest", "codeberg"),
}


def forge_repo(url: str) -> tuple[str, str, str] | None:
    """Split a forge address into (repo, api template, method), or None."""
    parts = urllib.parse.urlparse(url)
    host = parts.netloc.lower().removeprefix("www.")
    if host not in FORGE_HOSTS:
        return None
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2:
        return None
    api, method = FORGE_HOSTS[host]
    return f"{segments[0]}/{segments[1]}", api, method


def _forge(url: str, system: str, token: str, timeout: int,
           tag: str = "") -> dict | None:
    """GitHub and Gitea-style forges expose releases through an API."""
    parsed = forge_repo(url)
    if parsed is None:
        return None
    repo, api, method = parsed

    endpoint = api.format(path=repo)
    if tag:
        # A specific version was asked for rather than whatever is newest.
        endpoint = endpoint.replace("/releases/latest",
                                    f"/releases/tags/{urllib.parse.quote(tag)}")

    if method == "github":
        # catalog already knows how to talk to GitHub, including how to
        # explain a spent rate limit and when it resets. Reusing it keeps
        # one story for the person and one place to fix if the API moves.
        try:
            data, _remaining = catalog._request(endpoint, token, timeout)
        except catalog.RateLimit:
            raise
        except catalog.CatalogError as exc:
            if "no published release" in str(exc):
                return None
            raise ResolveError(str(exc)) from exc
    else:
        try:
            blob, _ = _fetch(endpoint, timeout, "application/json")
            data = json.loads(blob.decode("utf-8", "replace"))
        except (ResolveError, json.JSONDecodeError):
            return None
        if isinstance(data, list):
            data = data[0] if data else {}

    candidates = []
    for asset in data.get("assets", []) or []:
        name = asset.get("name", "")
        link = asset.get("browser_download_url") or asset.get("url", "")
        if not link:
            continue
        value = catalog.score_asset(name, system)
        candidates.append(
            {
                "url": link,
                "name": name,
                "label": name,
                "size": asset.get("size", 0),
                "score": value,
                "usable": value > 0,
                "kind": "file",
                "mirrors": 0,
            }
        )
    if not candidates:
        return None
    candidates.sort(key=lambda c: -c["score"])
    return {
        "method": method,
        "project": repo,
        "version": data.get("tag_name", ""),
        "released": (data.get("published_at") or "")[:10],
        "page": data.get("html_url", url),
        "candidates": candidates,
        "checksums": set(),
        "note": "",
    }


def _sourceforge(url: str, system: str, timeout: int) -> dict | None:
    parts = urllib.parse.urlparse(url)
    if not parts.netloc.lower().endswith("sourceforge.net"):
        return None
    match = re.search(r"/projects?/([^/]+)", parts.path)
    if not match:
        return None
    project = match.group(1)
    api = f"https://sourceforge.net/projects/{project}/best_release.json"
    try:
        blob, _ = _fetch(api, timeout, "application/json")
        data = json.loads(blob.decode("utf-8", "replace"))
    except (ResolveError, json.JSONDecodeError):
        return None

    candidates = []
    platform_releases = data.get("platform_releases") or {}
    seen: set[str] = set()
    for entry in list(platform_releases.values()) + [data.get("release") or {}]:
        filename = entry.get("filename") or ""
        if not filename or filename in seen:
            continue
        seen.add(filename)
        name = PurePosixPath(filename).name
        link = entry.get("url") or f"https://sourceforge.net/projects/{project}/files{filename}/download"
        value = catalog.score_asset(name, system)
        candidates.append(
            {"url": link, "name": name, "label": name, "size": 0,
             "score": value, "usable": value > 0, "kind": "file", "mirrors": 0}
        )
    if not candidates:
        return None
    candidates.sort(key=lambda c: -c["score"])
    return {
        "method": "sourceforge", "project": project,
        "version": (data.get("release") or {}).get("filename", "")[:60],
        "released": "", "page": url, "candidates": candidates,
        "checksums": set(), "note": "",
    }


# --------------------------------------------------------------------------
# the generic path
# --------------------------------------------------------------------------

# Anchor text that means "there is more after this", for sites that split a
# release list over several pages.
NEXT_WORDS = re.compile(
    r"(?i)^\s*(next|older|more|weiter|suivant|siguiente|»|>>|→|›)\s*(page|releases?)?\s*$"
)

# Most listings are not labelled "next" at all - they are a row of page
# numbers, or a ?page= parameter driven by script. All three are handled.
BARE_NUMBER = re.compile(r"^\s*\d{1,4}\s*$")
PAGE_QUERY = re.compile(r"(?i)([?&](?:page|pagina|seite|pg|p|start|offset|from)=)(\d+)")
PAGE_SEGMENT = re.compile(r"(?i)(/(?:page|pagina|seite|p)/)(\d+)(/?)$")


# Parameters that change the address without changing the page.
NOISE_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "referer", "referrer", "source", "from",
    "sid", "session", "sessionid", "phpsessid", "token", "_", "cb",
}


def canonical(url: str) -> str:
    """One address per page, so the same page is not fetched repeatedly.

    Sites reach the same listing by several routes: a trailing slash, a
    sort order tacked on, a tracking parameter. Comparing raw strings
    counted each of those as a new page, which is how a site of seven
    hundred pages became sixteen hundred fetches.
    """
    parts = urllib.parse.urlsplit(url)
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in NOISE_PARAMS
    ]
    query.sort()
    path = parts.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path,
         urllib.parse.urlencode(query), "")
    )


def page_identity(url: str) -> str:
    """Which page of which listing an address points at.

    ``/list?page=3`` and ``/list?page=3&sort=name`` are page three of the
    same listing however they are sorted. Keyed on the address alone they
    were two pages, and a site linking each page several ways got crawled
    once per way.
    """
    parts = urllib.parse.urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    family = _page_family(url)
    if family is not None:
        _template, number = family
        return f"{parts.netloc.lower()}{path}#page={number}"

    # A listing's bare address is its first page. Without this, "/list" and
    # "/list?page=1" were fetched as two pages. Only done when there is no
    # meaningful query, so distinct searches on one path stay distinct.
    leftovers = [
        key for key, _value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in NOISE_PARAMS
    ]
    if not leftovers:
        return f"{parts.netloc.lower()}{path}#page=1"
    return canonical(url)


def _page_family(url: str) -> tuple[str, int] | None:
    """If a URL carries a page number, return a template and that number."""
    match = PAGE_SEGMENT.search(url)
    if match:
        template = url[:match.start()] + match.group(1) + "{n}" + match.group(3)
        return template, int(match.group(2))
    match = PAGE_QUERY.search(url)
    if match:
        template = url[:match.start()] + match.group(1) + "{n}" + url[match.end():]
        return template, int(match.group(2))
    return None


def _page_links(parser: "_Collector", final_url: str, origin: str) -> list[str]:
    """Every link on this page that looks like another page of the listing."""
    here = urllib.parse.urlparse(final_url)
    out: list[str] = []

    def add(href: str) -> None:
        if not href or href.startswith("#"):
            return
        absolute = urllib.parse.urljoin(final_url, href.strip()).split("#", 1)[0]
        if urllib.parse.urlparse(absolute).netloc.lower() != origin:
            return
        if absolute not in out:
            out.append(absolute)

    if parser.next_link:
        add(parser.next_link)

    for entry in parser.links:
        text = re.sub(r"\s+", " ", entry["text"]).strip()
        href = entry["href"].strip()
        if not href:
            continue
        if BARE_NUMBER.match(text) or NEXT_WORDS.match(text) or NEXT_WORDS.match(entry["hint"].strip()):
            add(href)
            continue
        # A link to the same path with a different page number is a page of
        # this listing; the same test against a different path is not.
        absolute = urllib.parse.urljoin(final_url, href).split("#", 1)[0]
        parsed = urllib.parse.urlparse(absolute)
        if parsed.path == here.path and PAGE_QUERY.search(absolute):
            add(href)
        elif PAGE_SEGMENT.search(absolute) and parsed.path.rsplit("/", 2)[0] == here.path.rsplit("/", 2)[0]:
            add(href)
    return out


def _guess_pages(url: str, wanted: int) -> list[str]:
    """Build page addresses for listings whose controls are script-driven."""
    family = _page_family(url)
    if family:
        template, current = family
        return [template.format(n=n) for n in range(current + 1, current + 1 + wanted)]
    joiner = "&" if "?" in url else "?"
    return [f"{url}{joiner}page={n}" for n in range(2, 2 + wanted)]


def _fetch_page(url: str, timeout: int) -> tuple[str, bytes, str] | None:
    try:
        blob, final_url = _fetch(url, timeout, "text/html,application/xhtml+xml,*/*")
        return url, blob, final_url
    except ResolveError:
        return None


def _from_html(url: str, system: str, timeout: int, max_pages: int = 1,
               workers: int = 5, progress=None) -> dict:
    """Read a listing, following its pagination as far as the budget allows.

    Pages are fetched several at a time, because a ten page crawl done one
    request after another is a long wait for no reason.
    """
    found: dict[str, dict] = {}
    checksums: set = set()
    visited: list[str] = []
    script_count = 0
    budget = max(1, max_pages)
    origin = urllib.parse.urlparse(
        url if url.lower().startswith(("http://", "https://")) else "https://" + url
    ).netloc.lower()

    queue: list[str] = [url]
    seen: set = {page_identity(url)}
    final_url = url
    guessed = False
    saw_pagination = False
    barren = 0
    entry_count = 0
    diagnostics: dict = {}

    while queue and len(visited) < budget:
        batch = queue[: min(workers, budget - len(visited))]
        del queue[: len(batch)]

        if len(batch) == 1:
            results = [_fetch_page(batch[0], timeout)]
        else:
            with futures.ThreadPoolExecutor(max_workers=len(batch)) as pool:
                results = list(pool.map(lambda u: _fetch_page(u, timeout), batch))

        gained = 0
        for result in results:
            if result is None:
                continue
            page_url, blob, page_final = result
            visited.append(page_url)
            final_url = page_final
            if progress:
                swarm_so_far = sum(
                    1 for c in found.values() if c.get("kind") in ("magnet", "torrent")
                )
                progress(len(visited), len(found), swarm_so_far, len(queue))
            text = _decode(blob)
            parser = _Collector()
            try:
                parser.feed(text)
            except Exception:
                pass  # malformed markup still yields what was parsed so far
            script_count = max(script_count, parser.script_count)
            checksums |= set(CHECKSUM.findall(text))

            before = len(found)
            _harvest(parser, found, page_final, system)
            page_gain = len(found) - before
            gained += page_gain

            if len(visited) == 1:      # first page only
                entries = _entry_links(parser, page_final, origin)
                keep = _dominant_shapes(entries)
                entry_count = len([e for e in entries if e["shape"] in keep])

                # Record what was actually seen. Four rounds of guessing at
                # why a page came back short is three too many.
                shape_counts: dict = {}
                for item in entries:
                    label = "/".join(str(x) for x in item["shape"])
                    shape_counts[label] = shape_counts.get(label, 0) + 1
                same_host = 0
                file_like = 0
                for link in parser.links:
                    absolute = urllib.parse.urljoin(page_final, link["href"].strip())
                    if urllib.parse.urlparse(absolute).netloc.lower() == origin:
                        same_host += 1
                    if looks_like_file(absolute) or MAGNET_SCHEME.match(absolute):
                        file_like += 1
                # Both sides of the verdict must come from this same page.
                # Comparing page-one entries against files gathered over
                # three pages let the file count win every time.
                first_usable = sum(1 for c in found.values() if c["usable"])
                diagnostics = {
                    "first_page_usable": first_usable,
                    "links_total": len(parser.links),
                    "same_host": same_host,
                    "file_like": file_like,
                    "entry_like": len(entries),
                    "entries_kept": entry_count,
                    "top_shapes": sorted(shape_counts.items(), key=lambda kv: -kv[1])[:6],
                    "pagination_found": _page_links(parser, page_final, origin)[:8],
                    "scripts": parser.script_count,
                }

            links = _page_links(parser, page_final, origin)
            if links:
                saw_pagination = True
            # A page that adds nothing is the same listing reached another
            # way, usually a sort order tacked onto the address. Following
            # its pagination walks the entire site a second time.
            if page_gain == 0 and visited[:-1]:
                links = []
            for link in links:
                key = page_identity(link)
                if key not in seen:
                    seen.add(key)
                    queue.append(link)

        # Nothing linked onward, but the budget is unspent: the controls may
        # be script-driven, so try the obvious addresses once.
        # Guessing is for listings whose controls are drawn by script. A
        # site that published seventeen page links has seventeen pages;
        # inventing an eighteenth just fetched empty pages.
        if not queue and len(visited) < budget and not guessed and not saw_pagination:
            guessed = True
            for candidate in _guess_pages(url, budget - len(visited)):
                key = page_identity(candidate)
                if key not in seen:
                    seen.add(key)
                    queue.append(candidate)

        # The stop rule counts files gained, and a listing page holds none,
        # so applying it to real pagination ended crawls after about three
        # pages however large the budget was. Links the site actually
        # published are followed to the budget; only addresses this tool
        # invented can spin, so only those are cut short.
        if guessed:
            barren = barren + 1 if gained == 0 else 0
            if barren >= 2:
                break

    result = _summarise_page(found, checksums, script_count, final_url, visited,
                             entry_count)
    diagnostics["pages_visited"] = visited[:12]
    result["diagnostics"] = diagnostics
    return result


# --------------------------------------------------------------------------
# crawling an index of programs
# --------------------------------------------------------------------------

# Words that mark a link as site furniture rather than an entry.
CHROME_WORDS = re.compile(
    r"(?i)^\s*(home|about|contact|login|log in|sign ?in|sign ?up|register|"
    r"privacy|terms|cookies?|help|support|faq|blog|news|forum|donate|"
    r"advertise|sitemap|rss|search|menu|back|top|share|tweet|previous|prev|"
    r"next|older|newer|more|all|categories|tags|archive|\d+)\s*$"
)


def _shape(url: str) -> tuple:
    """A coarse fingerprint of a URL's structure.

    Entries in a listing nearly always share a shape - ``/app/audacity``
    and ``/app/vlc`` both look like ``('app', 2)`` - while the navigation
    around them does not. Counting shapes is what separates the two
    without knowing anything about the site.
    """
    path = urllib.parse.urlparse(url).path.rstrip("/")
    segments = [s for s in path.split("/") if s]
    if not segments:
        return ("", 0)
    return (segments[0].lower(), len(segments))


# Sections that group entries rather than being entries. These repeat far
# more than the programs do, so shape-counting alone would settle on them
# and collect a list of tags instead of a list of software.
TAXONOMY = re.compile(
    r"(?i)/(tags?|categor(y|ies)|genres?|topics?|labels?|keywords?|platforms?|"
    r"licen[cs]es?|filters?|browse|collections?|series|authors?|developers?|"
    r"publishers?|users?|profiles?|search|sort|archives?|dates?|years?)(/|$|\?)"
)


def _entry_links(parser: "_Collector", final_url: str, origin: str) -> list[dict]:
    """Links on this page that look like entries in a listing."""
    here = urllib.parse.urlparse(final_url)
    seen: dict[str, dict] = {}

    for entry in parser.links:
        href = entry["href"].strip()
        if not href or href.startswith("#") or NOT_A_DOWNLOAD.search(href):
            continue
        absolute = urllib.parse.urljoin(final_url, href).split("#", 1)[0]
        parsed = urllib.parse.urlparse(absolute)
        if parsed.netloc.lower() != origin or parsed.scheme not in ("http", "https"):
            continue
        if absolute.rstrip("/") == final_url.rstrip("/"):
            continue
        if looks_like_file(absolute) or MAGNET_SCHEME.match(absolute):
            continue                       # that is a download, not an entry
        if TAXONOMY.search(parsed.path) or TAXONOMY.search(parsed.query):
            continue                       # a tag or category, not a program

        text = re.sub(r"\s+", " ", entry["text"]).strip()
        if len(text) < 2 or len(text) > 90 or CHROME_WORDS.match(text):
            continue
        if parsed.path.rstrip("/") == here.path.rstrip("/"):
            continue                       # a page of this same listing

        if absolute not in seen:
            seen[absolute] = {"name": text, "url": absolute, "shape": _shape(absolute)}
    return list(seen.values())


def _dominant_shapes(entries: list[dict], floor: int = 3) -> set:
    """Keep the shapes that repeat; those are the listing's own entries."""
    counts: dict = {}
    for entry in entries:
        counts[entry["shape"]] = counts.get(entry["shape"], 0) + 1
    if not counts:
        return set()
    best = max(counts.values())
    if best < floor:
        return set()
    # Anything appearing nearly as often as the most common shape is part
    # of the same listing; one-off links are navigation.
    return {shape for shape, count in counts.items() if count >= max(floor, best * 0.4)}


def crawl_index(url: str, timeout: int = 20, max_pages: int = 25,
                workers: int = 5, progress=None, cap: int = 4000) -> dict:
    """Walk a listing and collect every program it lists.

    Returns entries rather than files: each one is a page that can be
    resolved on its own when somebody actually wants that program.
    """
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url

    # A cursor beats crawling: archive.org will hand over every item in a
    # collection without a page number being involved at all.
    if archive.is_archive(url):
        try:
            def relay(found: int, total: int) -> None:
                if progress:
                    progress(max(1, found // 100), found)
            return archive.collect(url, want=min(cap, max_pages * 100),
                                   timeout=timeout, progress=relay)
        except archive.ArchiveError:
            pass
    origin = urllib.parse.urlparse(url).netloc.lower()

    programs: dict[str, dict] = {}
    visited: list[str] = []
    seen_pages: set = {page_identity(url)}
    queue: list[str] = [url]
    guessed = False
    saw_pagination = False
    barren = 0
    shapes: set = set()
    appearances: dict = {}

    while queue and len(visited) < max(1, max_pages) and len(programs) < cap:
        batch = queue[: min(workers, max_pages - len(visited))]
        del queue[: len(batch)]
        if progress:
            progress(len(visited) + 1, len(programs))

        if len(batch) == 1:
            results = [_fetch_page(batch[0], timeout)]
        else:
            with futures.ThreadPoolExecutor(max_workers=len(batch)) as pool:
                results = list(pool.map(lambda u: _fetch_page(u, timeout), batch))

        gained = 0
        for result in results:
            if result is None:
                continue
            page_url, blob, page_final = result
            visited.append(page_url)
            parser = _Collector()
            try:
                parser.feed(_decode(blob))
            except Exception:
                pass

            found = _entry_links(parser, page_final, origin)
            if not shapes:
                shapes = _dominant_shapes(found)
            for item in found:
                if shapes and item["shape"] not in shapes:
                    continue
                appearances[item["url"]] = appearances.get(item["url"], 0) + 1
                if item["url"] not in programs:
                    programs[item["url"]] = {"name": item["name"], "url": item["url"]}
                    gained += 1

            links = _page_links(parser, page_final, origin)
            if links:
                saw_pagination = True
            for link in links:
                key = page_identity(link)
                if key not in seen_pages:
                    seen_pages.add(key)
                    queue.append(link)

        if not queue and len(visited) < max_pages and not guessed and not saw_pagination:
            guessed = True
            for candidate in _guess_pages(url, max_pages - len(visited)):
                key = page_identity(candidate)
                if key not in seen_pages:
                    seen_pages.add(key)
                    queue.append(candidate)

        if guessed:
            barren = barren + 1 if gained == 0 else 0
            if barren >= 2:
                break

    # A tag, category or sidebar link sits on every page of a listing.
    # A program sits on exactly one. Counting appearances separates them
    # without needing to recognise any particular site's URL scheme, which
    # a list of known path names never managed.
    dropped = 0
    if len(visited) >= 3:
        everywhere = max(2, int(len(visited) * 0.6))
        for address, count in appearances.items():
            if count >= everywhere and address in programs:
                del programs[address]
                dropped += 1

    ordered = sorted(programs.values(), key=lambda p: p["name"].lower())
    return {
        "programs": ordered,
        "repeated_dropped": dropped,
        "pages_read": len(visited),
        "exhausted": not queue or len(programs) >= cap,
        "next_pages": queue[:20],
        "site": origin,
    }


def _is_index(entries: int, files_on_first_page: int) -> bool:
    """Is this a catalogue of programs rather than one program's downloads?

    Many catalogues put a download button on every row, so the presence of
    files proves nothing on its own. What settles it is whether the page
    is mostly links to other pages, counted over the same single page.
    """
    if entries < 8:
        return False
    return entries >= max(3, files_on_first_page * 0.75)


def add_derived_magnets(candidates: list, timeout: int = 25, limit: int = 6) -> int:
    """Turn .torrent links into magnets.

    Most torrent directories link a .torrent file and never print the
    magnet, so without this a site full of torrents looks like a site
    with no magnets at all.
    """
    targets = [c for c in candidates if c.get("kind") == "torrent"][:limit]
    if not targets:
        return 0
    have = {c["url"] for c in candidates if c.get("kind") == "magnet"}

    def build(candidate: dict):
        try:
            blob = archive.fetch_bytes(candidate["url"], timeout)
            info = torrents.read(blob)
            return {
                "url": torrents.magnet(blob),
                "name": info["name"] or candidate["name"],
                "label": "magnet, derived from the torrent",
                "size": info["size"],
                "score": candidate.get("score", 40) + 15,
                "usable": False,
                "kind": "magnet",
                "mirrors": 0,
                "magnet": {"hash": info["hash"], "name": info["name"],
                           "trackers": info["trackers"], "size": info["size"]},
            }
        except Exception:
            return None

    added = 0
    with futures.ThreadPoolExecutor(max_workers=min(4, len(targets))) as pool:
        for made in pool.map(build, targets):
            if made and made["url"] not in have:
                candidates.append(made)
                have.add(made["url"])
                added += 1
    return added


# Text that labels a link without naming what is behind it. On a torrent
# listing the magnet's own anchor is nearly always one of these, branded or
# not, while the title sits in a separate link in the same row.
GENERIC_LABEL = re.compile(
    r"(?i)^\W*(magnet( link| uri| dl)?|torrent( file| link)?|\.torrent|dl|get|grab|"
    r"download( now| here| link)?|link|here|click|copy|open|share|info|details|"
    r"mirror|direct|seed|leech|\d+)\W*$"
)


def _names_nothing(text: str) -> bool:
    """Is this link text a label rather than a name."""
    value = re.sub(r"\s+", " ", text or "").strip()
    if len(value) < 2 or len(value) > 90:
        return True
    if GENERIC_LABEL.match(value):
        return True
    # "Kickass Torrents Magnet" names the site and the link type, not the
    # thing being linked to, so anything ending in the link type is out.
    if re.search(r"(?i)\b(magnet|torrent|download)s?\b\W*$", value):
        return True
    return bool(CHROME_WORDS.match(value) or DOWNLOAD_WORDS.fullmatch(value))


def _harvest(parser: "_Collector", found: dict, final_url: str, system: str) -> None:

    def offer(link: str, label: str, hint: str, context: str = "") -> None:
        if not link:
            return
        raw = link.strip()

        if MAGNET_SCHEME.match(raw):
            info = parse_magnet(raw)
            key = f"magnet:{info['hash'] or raw[:80]}"
            if key in found:
                return
            # A magnet without a dn= parameter has no name of its own, but
            # the text of the link that held it almost always does. Falling
            # straight through to the info hash produced rows reading
            # "bfeb9f31b52fab57", which names nothing to anyone.
            anchor = re.sub(r"\s+", " ", label or "").strip()
            if _names_nothing(anchor):
                anchor = ""
            # Fall back to the row's own title link before giving up and
            # showing a hash, which named nothing to anybody.
            name = info["name"] or anchor or context or (
                f"unnamed torrent {info['hash'][:8]}" if info["hash"] else "magnet link"
            )
            found[key] = {
                "url": raw,
                "name": name,
                "label": _label(label, name),
                "size": info["size"],
                "score": 45 if DOWNLOAD_WORDS.search(f"{label} {hint}") else 30,
                "usable": False,      # nothing here for the HTTP downloader
                "kind": "magnet",
                "magnet": info,
                "mirrors": 0,
            }
            return

        if NOT_A_DOWNLOAD.search(raw):
            return
        absolute = urllib.parse.urljoin(final_url, raw)
        if not absolute.lower().startswith(("http://", "https://")):
            return
        absolute = absolute.split("#", 1)[0]

        name = file_name_of(absolute)
        if name.lower().endswith(".torrent"):
            key = f"torrent:{name.lower()}"
            if key not in found:
                found[key] = {
                    "url": absolute, "name": name, "label": _label(label, name),
                    "size": 0, "score": 40, "usable": False,
                    "kind": "torrent", "mirrors": 0,
                }
            return

        value, fits = score_link(absolute, label, hint, system)
        if value <= -100:
            return

        # Pages routinely name the same file twice, once as a plain link
        # and once in metadata or on a mirror. Key on the file name so
        # that reads as one download with alternatives, not as clutter.
        key = name.lower()
        existing = found.get(key)
        if existing:
            existing["mirrors"] = existing.get("mirrors", 0) + 1
            if existing["score"] >= value:
                return
            existing.update({"url": absolute, "score": value, "usable": fits,
                             "label": _label(label, name)})
            return
        found[key] = {
            "url": absolute,
            "name": name,
            "label": _label(label, name),
            "size": 0,
            "score": value,
            "usable": fits,
            "kind": "file",
            "mirrors": 0,
        }

    # Each listing row holds a title and its downloads together. Take the
    # title from the row a magnet actually sits in; falling back to "the
    # last title anywhere on the page" attached names from the wrong entry
    # whenever a row was laid out differently.
    row_titles: dict = {}
    for entry in parser.links:
        text = re.sub(r"\s+", " ", entry["text"] or "").strip()
        href = entry["href"].strip()
        if _names_nothing(text) or MAGNET_SCHEME.match(href):
            continue
        if looks_like_file(href):
            continue
        # A row often holds its category as well as its title. Taking the
        # first link found gave rows named "Utilities"; the category is
        # recognisable by its address, and between what is left the longer
        # text is the title rather than a breadcrumb.
        absolute = urllib.parse.urljoin(final_url, href)
        parsed = urllib.parse.urlparse(absolute)
        if TAXONOMY.search(parsed.path) or TAXONOMY.search(parsed.query):
            continue
        row = entry.get("row", 0)
        if len(text) > len(row_titles.get(row, "")):
            row_titles[row] = text

    last_title = ""
    for entry in parser.links:
        row = entry.get("row", 0)
        # The row's own title first, then the one before it for layouts that
        # put the heading above the block of links.
        context = row_titles.get(row) or row_titles.get(row - 1) or last_title
        offer(entry["href"], entry["text"], entry["hint"], context)
        if row in row_titles:
            last_title = row_titles[row]

    # schema.org SoftwareApplication is how a lot of project sites
    # describe their own download, and it is exact when present.
    for raw in parser.ld_json:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        for node in data if isinstance(data, list) else [data]:
            if not isinstance(node, dict):
                continue
            for key in ("downloadUrl", "installUrl", "contentUrl"):
                value = node.get(key)
                if isinstance(value, str):
                    offer(value, "download", "ld+json")

    if parser.meta_refresh:
        offer(parser.meta_refresh, "download", "meta refresh")


def _summarise_page(found: dict, checksums: set, script_count: int,
                    final_url: str, visited: list, entry_count: int = 0) -> dict:
    """Turn everything gathered across one or more pages into a result."""
    candidates = sorted(found.values(), key=lambda c: -c["score"])

    kinds = {c.get("kind", "file") for c in candidates}
    note = ""
    if candidates and kinds == {"magnet"}:
        note = (
            "This page publishes over BitTorrent only. There is no file to "
            "fetch directly; opening a magnet hands it to your torrent client."
        )
    elif candidates and "magnet" in kinds and not any(
            c["usable"] for c in candidates):
        note = ("Nothing on this page downloads directly to this computer. "
                "The magnet links need a torrent client.")
    elif not candidates:
        note = (
            "No file links were found on that page. It probably builds its "
            "download button in the browser, which cannot be read from here. "
            "Open it once, copy the direct link, and paste that instead — or "
            "watch the page so you are told when it changes."
            if script_count > 4
            else "No file links were found on that page."
        )
    elif not any(c["usable"] for c in candidates):
        note = "Files were found, but none of them look right for this computer."

    if len(visited) > 1:
        extra = f"Read {len(visited)} pages of this listing."
        note = f"{note} {extra}".strip()

    return {
        "method": "page",
        "project": urllib.parse.urlparse(final_url).netloc,
        "version": "", "released": "", "page": visited[0] if visited else final_url,
        "candidates": candidates, "checksums": checksums, "note": note,
        "pages_read": len(visited),
        "entry_count": entry_count,
        # What was actually fetched, so an unexpected page count can be
        # looked at rather than guessed about.
        "page_list": visited,
    }


# --------------------------------------------------------------------------
# public entry points
# --------------------------------------------------------------------------

def resolve(url: str, system: str = "", token: str = "", timeout: int = 20,
            limit: int = 25, tag: str = "", pages: int = 3,
            cache_dir=None, force: bool = False, ttl: int = 1800,
            progress=None) -> dict:
    """Find the downloadable files behind one page address.

    ``tag`` asks a forge for one particular version instead of the newest,
    which is what makes older releases reachable. ``pages`` is how far to
    follow a paginated listing.

    A crawl of ten pages is not something to repeat every time the tab is
    opened, so results are kept on disk for half an hour. ``force`` skips
    that and fetches again.
    """
    url = (url or "").strip()
    if not url:
        raise ResolveError("Paste a page address first.")
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    system = system or __import__("platform").system()

    # The depth used to be part of the key, so a full scan and an ordinary
    # look at the same address were two unrelated entries. Reopening the
    # app asked for the ordinary depth, missed the full scan sitting right
    # there, and crawled again from shallow. One entry per address now, and
    # a result is reused whenever it went at least as deep as what is asked.
    key = f"{url}|{system}|{tag}"
    if cache_dir is not None and not force:
        cached = _cache_get(cache_dir, key, ttl, pages)
        if cached is not None:
            cached["cached"] = True
            return cached

    result = _resolve_uncached(url, system, token, timeout, limit, tag, pages,
                               progress)
    if cache_dir is not None:
        _cache_put(cache_dir, key, result, pages)
    result["cached"] = False
    return result


CACHE_FILE = "resolve-cache.json"
CACHE_ENTRIES = 60


def _cache_path(cache_dir) -> "Path":
    from pathlib import Path as _Path
    return _Path(cache_dir) / CACHE_FILE


def _cache_load(cache_dir) -> dict:
    path = _cache_path(cache_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


# A full scan can take minutes, so it is kept for a week. A quick look is
# cheap to repeat and goes stale fast.
DEEP_TTL = 7 * 24 * 3600
DEEP_PAGES = 50


def _cache_get(cache_dir, key: str, ttl: int, pages: int = 1):
    entry = _cache_load(cache_dir).get(key)
    if not entry:
        return None
    depth = entry.get("pages", 1)
    age = time.time() - entry.get("at", 0)
    if age > (DEEP_TTL if depth >= DEEP_PAGES else ttl):
        return None
    # A shallower stored result cannot answer a deeper request.
    if depth < pages and not entry.get("exhausted"):
        return None
    return dict(entry.get("result") or {})


def _cache_put(cache_dir, key: str, result: dict, pages: int = 1) -> None:
    cache = _cache_load(cache_dir)
    existing = cache.get(key) or {}
    # Never let a shallow look overwrite a full scan of the same address.
    if existing.get("pages", 0) > max(pages, result.get("pages_read", 0)):
        return
    cache[key] = {
        "at": time.time(),
        "pages": max(pages, result.get("pages_read", 0)),
        "exhausted": result.get("pages_read", 0) < pages,
        "result": result,
    }
    if len(cache) > CACHE_ENTRIES:
        newest = sorted(cache.items(), key=lambda kv: -kv[1].get("at", 0))[:CACHE_ENTRIES]
        cache = dict(newest)
    path = _cache_path(cache_dir)
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache), "utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _resolve_uncached(url: str, system: str, token: str, timeout: int,
                      limit: int, tag: str, pages: int, progress=None) -> dict:
    # archive.org publishes proper APIs; reading its HTML would be a
    # worse answer to a question it already answers directly.
    result = None
    if archive.is_archive(url) and archive.identifier_of(url):
        try:
            if not archive.is_collection(archive.identifier_of(url), timeout):
                result = archive.item(url, timeout)
        except archive.ArchiveError:
            result = None

    if result is None:
        result = _forge(url, system, token, timeout, tag)
    if result is None:
        result = _sourceforge(url, system, timeout)
    if result is None:
        result = _from_html(url, system, timeout, pages, progress=progress)

    page_host = urllib.parse.urlparse(result["page"]).netloc.lower()
    for candidate in result["candidates"]:
        candidate.setdefault("kind", "file")
        candidate.setdefault("mirrors", 0)
        host = urllib.parse.urlparse(candidate["url"]).netloc.lower()
        candidate["host"] = host
        candidate["offsite"] = bool(host) and host != page_host

    # What runs here leads, whatever else scored highly. A Windows
    # installer can out-rank a universal archive on prominence alone,
    # and it must not sit above it in the list on a Linux machine.
    result["candidates"].sort(key=lambda c: (not c["usable"], -c["score"]))

    # A flat cut at 25 quietly deleted magnets on any deep crawl. Magnets
    # and torrents never rank as runnable here, so they sort last, and a
    # few dozen merged pages of installers pushed them past the cutoff.
    # They are few by nature, so they are kept whole and the cap is spent
    # on files, growing with how much was actually read.
    swarm = [c for c in result["candidates"] if c["kind"] in ("magnet", "torrent")]
    files = [c for c in result["candidates"] if c["kind"] not in ("magnet", "torrent")]

    # These ceilings exist only to stop a runaway page from exhausting
    # memory. They are far above any real listing, and whatever they cut
    # is reported rather than quietly dropped, because a round number
    # appearing in the results is indistinguishable from a real total.
    file_room = max(limit, 20000)
    swarm_room = 20000
    kept = files[:file_room] + swarm[:swarm_room]
    kept.sort(key=lambda c: (not c["usable"], -c["score"]))
    result["candidates"] = kept
    result["shown"] = {"files": len(files[:file_room]), "swarm": len(swarm[:swarm_room]),
                       "files_total": len(files), "swarm_total": len(swarm),
                       "truncated": len(files) > file_room or len(swarm) > swarm_room}
    # Torrent directories link the .torrent and leave the magnet unsaid.
    if any(c["kind"] == "torrent" for c in result["candidates"]):
        try:
            add_derived_magnets(result["candidates"], timeout)
            result["candidates"].sort(key=lambda c: (not c["usable"], -c["score"]))
        except Exception:
            pass

    result["checksums"] = sorted(result.get("checksums") or set())

    # A page listing dozens of programs is an index, not a download page.
    # Saying so lets the caller collect the entries instead of reporting
    # that a perfectly good catalogue has nothing on it.
    entries = result.get("entry_count", 0)
    first_usable = (result.get("diagnostics") or {}).get("first_page_usable", 0)
    result["mode"] = "index" if _is_index(entries, first_usable) else "files"
    result.setdefault("entry_count", entries)
    return result


# --------------------------------------------------------------------------
# version history and finding a project by name
# --------------------------------------------------------------------------

def looks_like_url(text: str) -> bool:
    """Tell an address apart from a search term typed into the same box."""
    value = (text or "").strip()
    if not value or " " in value:
        return False
    if value.lower().startswith(("http://", "https://", "magnet:")):
        return True
    return bool(re.match(r"^[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}(/|$)", value, re.I))


def releases(url: str, token: str = "", timeout: int = 20, limit: int = 25) -> dict:
    """Every published version of a forge project, newest first.

    Only forges keep a machine-readable history. For an ordinary web page
    there is nothing to list, so this says so rather than inventing one.
    """
    parsed = forge_repo(url if url.lower().startswith(("http://", "https://"))
                        else "https://" + url)
    if parsed is None:
        return {"project": "", "method": "page", "releases": [],
                "note": "Version history is only available for GitHub and "
                        "Codeberg projects. Elsewhere only what the page "
                        "shows right now can be read."}
    repo, api, method = parsed
    endpoint = api.format(path=repo).replace("/releases/latest", "/releases")
    joiner = "&" if "?" in endpoint else "?"
    endpoint += f"{joiner}per_page={limit}" if method == "github" else f"{joiner}limit={limit}"

    if method == "github":
        try:
            data, _ = catalog._request(endpoint, token, timeout)
        except catalog.RateLimit:
            raise
        except catalog.CatalogError as exc:
            raise ResolveError(str(exc)) from exc
    else:
        try:
            blob, _ = _fetch(endpoint, timeout, "application/json")
            data = json.loads(blob.decode("utf-8", "replace"))
        except (ResolveError, json.JSONDecodeError):
            return {"project": repo, "method": method, "releases": [], "note": ""}

    out = []
    for entry in data if isinstance(data, list) else []:
        if entry.get("draft"):
            continue
        out.append(
            {
                "tag": entry.get("tag_name") or entry.get("name") or "",
                "name": entry.get("name") or entry.get("tag_name") or "",
                "published": (entry.get("published_at") or entry.get("created_at") or "")[:10],
                "prerelease": bool(entry.get("prerelease")),
                "files": len(entry.get("assets") or []),
            }
        )
    return {"project": repo, "method": method, "releases": out[:limit], "note": ""}


def search_projects(term: str, token: str = "", timeout: int = 20,
                    limit: int = 15) -> list[dict]:
    """Find projects by name, so a program can be reached without its URL.

    GitHub's search endpoint has a tighter allowance than the rest of the
    API - ten a minute without a token - so this is meant for the odd
    lookup rather than bulk use.
    """
    term = (term or "").strip()
    if len(term) < 2:
        raise ResolveError("Type at least two characters to search.")
    query = urllib.parse.urlencode(
        {"q": term, "sort": "stars", "order": "desc", "per_page": limit}
    )
    endpoint = f"{catalog.API}/search/repositories?{query}"
    try:
        data, _ = catalog._request(endpoint, token, timeout)
    except catalog.RateLimit:
        raise
    except catalog.CatalogError as exc:
        raise ResolveError(str(exc)) from exc

    results = []
    for item in (data.get("items") or [])[:limit]:
        results.append(
            {
                "name": item.get("name", ""),
                "full_name": item.get("full_name", ""),
                "url": item.get("html_url", ""),
                "description": (item.get("description") or "")[:140],
                "stars": item.get("stargazers_count", 0),
                "updated": (item.get("pushed_at") or "")[:10],
                "language": item.get("language") or "",
                "archived": bool(item.get("archived")),
            }
        )
    return results


def fingerprint(result: dict) -> str:
    """A short signature of what a page currently offers.

    Hashing the download links rather than the whole page means rotating
    adverts, view counters and session tokens do not read as a new
    release, while a genuine version bump does.
    """
    parts = [result.get("version", "")]
    parts += sorted(c["url"] for c in result.get("candidates", [])[:10])
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def verify(sha256: str, published: list[str]) -> str:
    """Compare a finished download against checksums printed on the page."""
    if not sha256:
        return "unknown"
    if not published:
        return "none published"
    return "matches" if sha256.lower() in {p.lower() for p in published} else "MISMATCH"


# --------------------------------------------------------------------------
# watching pages so nobody has to keep opening them
# --------------------------------------------------------------------------

def check_watched(entries: list[dict], system: str = "", token: str = "",
                  timeout: int = 20, progress=None) -> tuple[list[dict], list[dict]]:
    """Re-resolve every watched page and report which ones moved.

    Each entry keeps the last fingerprint. A page counts as changed only
    when the files it offers change, so a site that reshuffles its
    banners every hour stays quiet while a real release speaks up.

    Returns the updated entries and the subset that changed.
    """
    updated: list[dict] = []
    changed: list[dict] = []

    for entry in entries:
        record = dict(entry)
        name = record.get("name") or record.get("url", "")
        if progress:
            progress(name)
        if not record.get("url") or not record.get("enabled", True):
            updated.append(record)
            continue

        try:
            found = resolve(record["url"], system=system, token=token, timeout=timeout)
        except (ResolveError, catalog.CatalogError) as exc:
            record["error"] = str(exc)
            record["checked"] = time.time()
            updated.append(record)
            continue

        record["error"] = ""
        record["checked"] = time.time()
        signature = fingerprint(found)
        best = next((c for c in found["candidates"] if c["usable"]), None)

        previous = record.get("fingerprint", "")
        record.update(
            {
                "fingerprint": signature,
                "version": found.get("version", "") or record.get("version", ""),
                "method": found["method"],
                "best_name": best["name"] if best else "",
                "best_url": best["url"] if best else "",
                "count": len(found["candidates"]),
            }
        )
        # A first sighting is recorded, not announced: everything would
        # look like news the first time the list is checked.
        if previous and previous != signature:
            record["changed"] = True
            changed.append(record)
        elif not previous:
            record.setdefault("changed", False)
        updated.append(record)

    return updated, changed
