"""Turn a file host's page into the file itself.

A release blog's "direct links" are rarely direct. Most point at a file
host's page for the file, which shows a button, sometimes after a short
wait or a form to submit, and only then hands over the real address.

This walks that the way a browser would, without running any script:

1. ask for the address; if a file comes back, that is the answer;
2. if a page comes back, look in it for the file - a link or a quoted
   address that names the same file, or anything that looks like a file
   on a download server;
3. failing that, submit the page's download form, after waiting as long
   as the page says to;
4. repeat from the new page, a few times at most.

Cookies the host sets on the way are kept and handed to the downloader,
because most hosts tie the final address to the visit that produced it.

When a host builds its link entirely in script, there is nothing here to
find, and that is said plainly so the store can try the next mirror.
"""

from __future__ import annotations

import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .sitecatalog import USER_AGENT, WRAPPER_HINT, file_name, parse

MAX_PAGE = 2 * 1024 * 1024
MAX_HOPS = 4
MAX_WAIT = 20

NOT_A_FILE = re.compile(
    r"(?i)\.(css|js|mjs|png|jpe?g|gif|webp|svg|ico|woff2?|ttf|eot|map|json|xml|php)(\?|$)")
SOCIAL = re.compile(r"(?i)(^|\.)(facebook|twitter|x|t|telegram|discord|reddit|youtube|google|"
                    r"gstatic|cloudflare|jsdelivr|unpkg|googletagmanager|doubleclick)\.")
QUOTED_URL = re.compile(r"""["'](https?://[^"'\s<>\\]{8,})["']""")
WAIT = re.compile(r"(?i)(?:countdown|seconds|timer|wait|delay)[^0-9]{0,24}(\d{1,3})")
DOWNLOAD_WORD = re.compile(r"(?i)\b(download|dl|get file|get link|free download|continue)\b")


class HosterError(RuntimeError):
    pass


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", urllib.parse.unquote(name or "").lower())


def _score(candidate: str, page: str, wanted: str, text: str = "") -> int:
    if not candidate.lower().startswith(("http://", "https://")):
        return -1
    if candidate.split("#")[0] == page.split("#")[0] or WRAPPER_HINT.search(candidate):
        return -1
    parsed = urllib.parse.urlparse(candidate)
    if SOCIAL.search(parsed.netloc) or NOT_A_FILE.search(parsed.path):
        return -1
    score = 0
    joined = _norm(parsed.path + parsed.query)
    if wanted and _norm(wanted) and _norm(wanted) in joined:
        score += 50
    if file_name(candidate):
        score += 20
    if re.search(r"(?i)(^|[./-])(dl|download|cdn|files?|storage|srv|s\d+)[./-]",
                 parsed.netloc + parsed.path):
        score += 10
    if DOWNLOAD_WORD.search(text or ""):
        score += 10
    return score


def _candidates(text: str, page: str, wanted: str) -> list[tuple[int, str]]:
    root = parse(text)
    found: dict[str, int] = {}
    for node in root.walk():
        for key in ("href", "data-href", "data-url", "data-link", "content"):
            value = node.get(key).strip()
            if not value or value.startswith(("#", "javascript:", "mailto:")):
                continue
            absolute = urllib.parse.urljoin(page, value)
            label = node.text() if node.tag in ("a", "button") else ""
            score = _score(absolute, page, wanted, label)
            if score > found.get(absolute, -1):
                found[absolute] = score
    for match in QUOTED_URL.finditer(text):
        absolute = match.group(1).replace("\\/", "/")
        score = _score(absolute, page, wanted)
        if score > found.get(absolute, -1):
            found[absolute] = score
    ranked = sorted(((s, u) for u, s in found.items() if s >= 20), reverse=True)
    return ranked


def _download_form(text: str, page: str):
    """(method, action, fields) of the form that asks for the file, or None."""
    root = parse(text)
    best = None
    for form in root.find_all("form"):
        label = form.text() + " " + " ".join(
            n.get("value") + " " + n.get("name") + " " + n.get("id")
            for n in form.walk() if n.tag in ("input", "button"))
        if not DOWNLOAD_WORD.search(label) and not re.search(r"(?i)op=|download", form.get("action")):
            continue
        if re.search(r"(?i)search|login|sign ?in|register|comment|password|captcha", label):
            continue
        fields: list[tuple[str, str]] = []
        for node in form.walk():
            name = node.get("name")
            if not name:
                continue
            if node.tag == "input":
                kind = node.get("type").lower() or "text"
                if kind in ("checkbox", "radio") and "checked" not in node.attrs:
                    continue
                if kind in ("submit", "image") and not DOWNLOAD_WORD.search(
                        node.get("value") + " " + name):
                    continue
                fields.append((name, node.get("value")))
            elif node.tag == "button" and DOWNLOAD_WORD.search(node.text() + " " + name):
                fields.append((name, node.get("value")))
            elif node.tag == "select":
                option = next((o for o in node.find_all("option") if "selected" in o.attrs),
                              node.find("option"))
                if option is not None:
                    fields.append((name, option.get("value") or option.text().strip()))
        method = (form.get("method") or "get").upper()
        action = urllib.parse.urljoin(page, form.get("action") or page)
        best = best or (method, action, fields)
    return best


def _url_in_json(blob: bytes) -> str:
    try:
        data = json.loads(blob.decode("utf-8", "replace"))
    except ValueError:
        return ""
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                if isinstance(value, str) and value.startswith(("http://", "https://")) \
                        and re.search(r"(?i)url|link|download|file|href", key):
                    return value
                stack.append(value)
        elif isinstance(item, list):
            stack.extend(item)
    return ""


def _is_page(response) -> bool:
    kind = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if "attachment" in (response.headers.get("Content-Disposition") or "").lower():
        return False
    return kind in ("text/html", "application/xhtml+xml", "application/json", "text/plain", "")


def resolve(url: str, wanted: str = "", timeout: int = 30) -> dict:
    """``{"url", "headers"}`` for the file behind ``url``.

    ``headers`` carries the cookies and referrer the host expects, for the
    downloader to send with its own request.
    """
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    wanted = wanted or file_name(url)
    address = url.split("#", 1)[0]
    referer = ""
    data: bytes | None = None
    tried: set = set()

    for _hop in range(MAX_HOPS):
        headers = {"User-Agent": USER_AGENT,
                   "Accept": "text/html,application/xhtml+xml,application/octet-stream,*/*;q=0.8",
                   "Accept-Language": "en-GB,en;q=0.8"}
        if referer:
            headers["Referer"] = referer
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(address, data=data, headers=headers)
        tried.add((address, data))
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            raise HosterError(f"{_host(address)} answered HTTP {exc.code}.") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HosterError(f"Could not reach {_host(address)} "
                              f"({getattr(exc, 'reason', exc)}).") from exc

        with response:
            final = response.geturl()
            if not _is_page(response):
                return {"url": final, "headers": _headers_for(jar, final, referer or url)}
            blob = response.read(MAX_PAGE)

        head = blob[:64].lstrip().lower()
        if not head.startswith((b"<", b"{", b"[")) and blob:
            # Served as text, but it is the file.
            return {"url": final, "headers": _headers_for(jar, final, referer or url)}

        found = _url_in_json(blob) if head.startswith((b"{", b"[")) else ""
        text = blob.decode("utf-8", "replace")
        if not found:
            ranked = _candidates(text, final, wanted)
            found = ranked[0][1] if ranked else ""
        if found:
            referer, address, data = final, found.split("#", 1)[0], None
            continue

        form = _download_form(text, final)
        if form is None:
            break
        method, action, fields = form
        encoded = urllib.parse.urlencode(fields)
        if method == "POST":
            next_step = (action, encoded.encode("ascii"))
        else:
            joiner = "&" if "?" in action else "?"
            next_step = (f"{action}{joiner}{encoded}" if encoded else action, None)
        if next_step in tried:
            break
        wait = WAIT.search(text)
        if wait:
            time.sleep(min(int(wait.group(1)), MAX_WAIT))
        referer, (address, data) = final, next_step

    raise HosterError(f"{_host(url)} would not hand over the file without a browser; it "
                      "probably builds the download button in script.")


def _host(url: str) -> str:
    return urllib.parse.urlparse(url).netloc or url


def _headers_for(jar, url: str, referer: str) -> dict:
    request = urllib.request.Request(url)
    jar.add_cookie_header(request)
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    cookie = request.get_header("Cookie")
    if cookie:
        headers["Cookie"] = cookie
    return headers
