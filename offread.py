#!/usr/bin/env python3
"""
offread - fetch web pages with wget, scrub them, and read them offline.

  python3 offread.py get URL [URL ...]     fetch and save one or more pages
      --open                open the last page when done
      --first-party         only pull images/CSS/fonts from the page's own domain
      --allow DOMAIN        extra domain allowed with --first-party (repeatable)
      --live                keep the page's own scripts running (no egress)
  python3 offread.py mark URL [URL ...]    bookmark links and save them now
      --name LABEL          give the bookmark a name (one URL at a time)
      (--first-party / --allow / --live work here too and are remembered)
  python3 offread.py marks                 list bookmarks
  python3 offread.py update [N ...]        re-fetch all bookmarks (or just N);
                                           the previous copy moves to the archive.
                                           Pages whose text hasn't changed are
                                           left alone (--force to save anyway)
  python3 offread.py unmark N [N ...]      drop bookmarks (saved copies stay)
  python3 offread.py list [--all]          show saved pages (--all adds archive)
  python3 offread.py read [N]              open page N (or the index) in your browser
  python3 offread.py read -b N             open bookmark N's latest copy
  python3 offread.py serve                 just run the local reader

  While reading, clicking a link to the live web fetches and saves that page,
  then opens the local copy.

Pages live in ~/OfflineReader (override with OFFREAD_DIR): current copies in
pages/, replaced bookmark copies in archive/.

What it does:
  Fetch: wget pulls the page plus its images/CSS/fonts. JavaScript files are
  refused by extension (.js/.mjs/...), cookies are off, and common ad/tracker
  hosts are refused. A script served from an extension-less URL would still
  download, but the scrubber removes its tag and the reader's CSP blocks it.
  Pixels: before downloading anything else, the page's HTML is read and
  tracking pixels are identified (tiny or hidden images, third-party images
  inside <noscript>, third-party images with beacon/track/pixel-style URLs).
  Those exact URLs are refused, so they are never requested. After the
  download, any image that turns out to be 3x3 or smaller, or a few hundred
  bytes, is deleted and its tag removed as a backstop.
  Scrub: every saved HTML file has scripts, iframes, embeds, event handlers,
  meta refreshes, preconnect/prefetch hints, and any remaining remote
  src/srcset/stylesheet references removed.
  Read: a tiny server bound to 127.0.0.1 only serves the files with a
  Content-Security-Policy that forbids the page from loading anything from
  anywhere but that local server. Even if the scrubber missed something,
  the browser will refuse to make the request. Links to other pages are
  routed through the server: the target is fetched and saved, and the local
  copy opens instead of the live web.
  Live: with --live, the page's own downloaded scripts and stylesheets are
  kept so it renders as designed, but there is no egress: remote URLs inside
  CSS and JS text are pointed at an inert placeholder, the CSP only allows
  the local server and the injected shim, and the shim no-ops sendBeacon,
  WebSocket, EventSource and persistent storage. Links clicked from a live
  page are fetched with --live too.

Requires wget (Mac: brew install wget) and Python 3.8+. Standard library only.
"""

import argparse
import datetime
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import (parse_qs, quote, unquote, urldefrag, urlencode,
                          urljoin, urlparse)

CACHE = Path(os.environ.get("OFFREAD_DIR", str(Path.home() / "OfflineReader")))
MANIFEST = CACHE / "manifest.json"
DEFAULT_PORT = 8787

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")

# The page may load only from the local server. No scripts, no frames,
# no fetch/XHR/websockets, no form posts.
CSP = ("default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
       "font-src 'self' data:; media-src 'self'; script-src 'none'; connect-src 'none'; "
       "frame-src 'none'; child-src 'none'; worker-src 'none'; object-src 'none'; "
       "manifest-src 'none'; form-action 'none'; base-uri 'none'")

# Same sandbox for --live pages, except local (and the injected inline)
# scripts may run. Every other egress channel stays shut.
CSP_LIVE = ("default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "font-src 'self' data:; media-src 'self'; script-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; frame-src 'none'; child-src 'none'; worker-src 'none'; "
            "object-src 'none'; manifest-src 'none'; form-action 'none'; base-uri 'none'")

# Injected at the top of --live pages: closes the tracking APIs that CSP
# can't reach (sendBeacon ignores CSP in some engines) and makes storage
# non-persistent so nothing can be stashed for a later page to exfiltrate.
SHIM = """<script>(function(){
if (navigator.sendBeacon) navigator.sendBeacon = function(){ return false; };
window.WebSocket = function(){ throw new Error("offread: no egress"); };
window.EventSource = function(){ throw new Error("offread: no egress"); };
try {
  var mk = function(){ var d = {};
    return {getItem:function(k){return k in d ? d[k] : null;},
            setItem:function(k,v){d[k]=String(v);}, removeItem:function(k){delete d[k];},
            clear:function(){d={};}, key:function(){return null;}, get length(){return 0;}}; };
  var ls = mk(), ss = mk();  // one store each per page load: works within the
  Object.defineProperty(window, "localStorage",   {get: function(){ return ls; }});  // page, gone after
  Object.defineProperty(window, "sessionStorage", {get: function(){ return ss; }});
} catch (e) {}
})();</script>"""

# Remote urls inside CSS and JS text are pointed at this inert 1x1 GIF.
BLANK = ("data:image/gif;base64,"
         "R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==")

# Hosts wget will refuse to touch while fetching requisites. Add to taste.
AD_HOSTS = [
    "doubleclick", "googlesyndication", "googleadservices", "adservice",
    "adnxs", "taboola", "outbrain", "criteo", "amazon-adsystem",
    "scorecardresearch", "quantserve", "moatads", "pubmatic",
    "rubiconproject", "openx", "casalemedia", "google-analytics",
    "googletagmanager", "hotjar", "chartbeat", "facebook.com/tr",
    "connect.facebook.net", "adsrvr", "bidswitch", "smartadserver", "teads",
    "sharethrough", "yieldmo", "3lift", "indexww", "media.net", "permutive",
    "segment.io", "newrelic", "nr-data", "quantcount", "krxd", "bluekai",
    "demdex", "everesttech", "mathtag", "omtrdc", "2mdn", "adform",
    "lijit", "sonobi", "gumgum", "zemanta", "mgid", "revcontent",
]


def ere_escape(s):
    """Escape a literal string for wget's POSIX extended regex."""
    return re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", s)


AD_REGEX = "|".join(ere_escape(h) for h in AD_HOSTS)


# ---------------------------------------------------------------- pixels

# URL words that mark a third-party image as a beacon rather than a picture.
TRACK_URL_RE = re.compile(
    r"pixel|beacon|track|collect|analytics|impression|/imp[/?]|1x1|spacer|"
    r"/b\.gif|/p\.gif|/t\.gif|/e\.gif|/__utm|utm\.gif|/ping|/event|/log[/?]|"
    r"/hit[/?]|/count|counter|stat[s]?\.|/view[/?]|fbevents|/tr[/?]",
    re.I,
)
HIDDEN_STYLE_RE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|"
    r"(?:width|height)\s*:\s*(?:[0-3](?:\.\d+)?px\b|0(?![\d.]))",
    re.I,
)
TINY_BYTES = 300
IMAGE_EXTS = {".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".ico", ".avif", ".svg", ""}


def base_domain(host):
    parts = (host or "").lower().strip(".").split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in (
            "co", "com", "org", "net", "gov", "ac", "edu", "ne", "or"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def tiny_or_hidden(attrs):
    """True if an <img>'s own markup says it is a pixel or invisible."""
    for k in ("width", "height"):
        m = re.match(r"\s*(\d+)", attrs.get(k, "") or "")
        if m and int(m.group(1)) <= 3:
            return True
    return bool(HIDDEN_STYLE_RE.search(attrs.get("style", "") or ""))


class PixelFinder(HTMLParser):
    """Scan the raw page and collect absolute URLs of tracking pixels."""

    def __init__(self, page_url):
        super().__init__(convert_charrefs=True)
        self.base = page_url
        self.site = base_domain(urlparse(page_url).hostname)
        self.noscript = 0
        self.pixels = set()

    def handle_starttag(self, tag, attrs):
        d = {k.lower(): (v or "") for k, v in attrs}
        if tag == "base" and d.get("href"):
            self.base = urljoin(self.base, d["href"])
        elif tag == "noscript":
            self.noscript += 1
        elif tag == "img" and d.get("src"):
            src = d["src"].strip()
            if src.lower().startswith("data:"):
                return
            url = urljoin(self.base, src)
            third = base_domain(urlparse(url).hostname) != self.site
            if (tiny_or_hidden(d)
                    or (self.noscript and third)
                    or (third and TRACK_URL_RE.search(url))):
                self.pixels.add(url)

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        if tag == "noscript" and self.noscript:
            self.noscript -= 1


def _jpeg_size(head):
    """(width, height) from a JPEG's SOF marker, or None."""
    i = 2
    while i + 4 <= len(head):
        if head[i] != 0xFF:
            i += 1
            continue
        marker = head[i + 1]
        if marker == 0xDA:  # start of scan: entropy data, stop looking
            return None
        if marker in (0x01, 0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2  # standalone markers carry no length
            continue
        seg_len = int.from_bytes(head[i + 2:i + 4], "big")
        if seg_len < 2:
            return None
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):  # SOF, not DHT/DAC
            if i + 9 > len(head):
                return None
            h = int.from_bytes(head[i + 5:i + 7], "big")
            w = int.from_bytes(head[i + 7:i + 9], "big")
            return w, h
        i += 2 + seg_len
    return None


def _webp_size(head):
    """(width, height) from a WebP RIFF header, or None."""
    chunk, payload = head[12:16], head[20:]  # FourCC, then 4-byte chunk size
    if chunk == b"VP8X" and len(payload) >= 10:
        return (int.from_bytes(payload[4:7], "little") + 1,
                int.from_bytes(payload[7:10], "little") + 1)
    if chunk == b"VP8L" and len(payload) >= 5 and payload[0] == 0x2F:
        bits = int.from_bytes(payload[1:5], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8 " and len(payload) >= 10 and payload[3:6] == b"\x9d\x2a\x01":
        return (int.from_bytes(payload[6:8], "little") & 0x3FFF,
                int.from_bytes(payload[8:10], "little") & 0x3FFF)
    return None


def image_is_tiny(path):
    """Backstop after download: is this file actually a pixel?"""
    try:
        size = path.stat().st_size
        if size <= TINY_BYTES and path.suffix.lower() != ".svg":
            return True
        with open(path, "rb") as f:
            head = f.read(65536)
    except OSError:
        return False
    dims = None
    if head[:6] in (b"GIF87a", b"GIF89a"):
        dims = (int.from_bytes(head[6:8], "little"), int.from_bytes(head[8:10], "little"))
    elif head[:8] == b"\x89PNG\r\n\x1a\n":
        dims = (int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big"))
    elif head[:2] == b"\xff\xd8":
        dims = _jpeg_size(head)
    elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        dims = _webp_size(head)
    elif head[:4] == b"\x00\x00\x01\x00" and len(head) >= 8:  # ICO directory entry
        dims = (head[6] or 256, head[7] or 256)
    return dims is not None and dims[0] <= 3 and dims[1] <= 3

INJECT = (
    '<meta charset="utf-8">'
    f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
    '<meta http-equiv="x-dns-prefetch-control" content="off">'
    '<meta name="referrer" content="no-referrer">'
)

INJECT_LIVE = (
    '<meta charset="utf-8">'
    f'<meta http-equiv="Content-Security-Policy" content="{CSP_LIVE}">'
    '<meta http-equiv="x-dns-prefetch-control" content="off">'
    '<meta name="referrer" content="no-referrer">'
    + SHIM
)


# ---------------------------------------------------------------- scrubber

DROP_WITH_CONTENT = {"iframe", "object", "frame", "frameset",
                     "applet", "template", "noembed", "portal"}
DROP_VOID = {"embed", "base", "param"}
LOAD_ATTRS = {"src", "srcset", "poster", "data", "background", "lowsrc",
              "dynsrc", "imagesrcset", "xlink:href"}
BAD_RELS = {"preconnect", "dns-prefetch", "prefetch", "preload", "prerender",
            "modulepreload", "manifest", "pingback", "import"}
STRIP_ATTRS = {"ping", "formaction", "action", "integrity", "nonce",
               "crossorigin", "attributionsrc"}

# url(...) and @import targets inside CSS text.
CSS_URL_RE = re.compile(
    r"url\(\s*(['\"]?)(?:https?:)?//[^)'\"]+\1\s*\)", re.I)
CSS_IMPORT_RE = re.compile(
    r"@import\s+(['\"])(?:https?:)?//[^'\"]+\1", re.I)

# Absolute or protocol-relative URLs inside JavaScript text.
JS_URL_RE = re.compile(r"(?:https?:)?//[A-Za-z0-9][^\s'\"<>\\]*")


def is_remote(value):
    v = (value or "").strip().lower()
    return "://" in v or v.startswith("//")


def scrub_css_text(css):
    """Point every remote CSS url()/import at the inert placeholder."""
    css = CSS_URL_RE.sub(f'url("{BLANK}")', css)
    return CSS_IMPORT_RE.sub(f'@import "{BLANK}"', css)


def scrub_js_text(js):
    """Point absolute URLs in script text at about:blank."""
    return JS_URL_RE.sub("about:blank", js)


def scrub_asset(path):
    """Defang a saved CSS or JS file in place (live mode only)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    ext = path.suffix.lower()
    if ext == ".css":
        text = scrub_css_text(text)
    elif ext in (".js", ".mjs"):
        text = scrub_js_text(text)
    path.write_text(text, encoding="utf-8")


class Scrubber(HTMLParser):
    def __init__(self, here=None, live=False):
        super().__init__(convert_charrefs=False)
        self.here = here  # directory of the HTML file being scrubbed
        self.live = live  # keep local scripts/styles; defang inline content
        self.out = []
        self.skip = 0
        self.saw_doctype = False
        self.in_title = False
        self.title = ""
        self.in_style = False
        self.in_script = False
        self.script_src = None
        self.text = []  # visible text, used to tell if a page changed

    def _clean(self, tag, attrs):
        """Return cleaned attrs, or None to drop the tag entirely."""
        if tag in DROP_VOID:
            return None
        d = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            equiv = d.get("http-equiv", "").lower()
            if "charset" in d or equiv in ("refresh", "content-type", "set-cookie",
                                           "content-security-policy"):
                return None
        if tag == "link":
            rels = set(d.get("rel", "").lower().split())
            if rels & BAD_RELS or is_remote(d.get("href")):
                return None
        if tag == "img":
            if tiny_or_hidden(d) or is_remote(d.get("src")):
                return None
            if self.here is not None and not self._local_ok(d.get("src", "")):
                return None
        kept = []
        for k, v in attrs:
            k = k.lower()
            val = v or ""
            low = val.strip().lower()
            if k.startswith("on") or k in STRIP_ATTRS:
                continue
            if low.startswith(("javascript:", "vbscript:", "data:text/html")):
                continue
            if k in LOAD_ATTRS and is_remote(val):
                continue
            if k == "style" and ("url(" in low or "@import" in low):
                val = scrub_css_text(val)  # remote urls become an inert placeholder
            if k == "href" and tag not in ("a", "area") and is_remote(val):
                continue
            kept.append((k, v if k != "style" else val))
        return kept

    def _local_ok(self, src):
        """An <img> pointing at a local file that was refused or deleted goes."""
        s = (src or "").strip()
        if not s or s.lower().startswith("data:") or is_remote(s):
            return True  # data: is inert; remote src is stripped separately
        rel = unquote(re.split(r"[?#]", s, maxsplit=1)[0])
        return (self.here / rel).exists()

    @staticmethod
    def _render(tag, attrs, selfclose=False):
        parts = [tag]
        for k, v in attrs:
            parts.append(k if v is None else f'{k}="{html.escape(v, quote=True)}"')
        return "<" + " ".join(parts) + (" />" if selfclose else ">")

    def handle_decl(self, decl):
        if self.skip:
            return
        self.out.append(f"<!{decl}>")
        if decl.lower().startswith("doctype") and not self.saw_doctype:
            self.saw_doctype = True
            self.out.append(INJECT_LIVE if self.live else INJECT)

    def handle_starttag(self, tag, attrs):
        if self.skip:
            if tag in DROP_WITH_CONTENT or (tag == "script" and not self.live):
                self.skip += 1
            return
        if tag == "script":
            if not self.live:
                self.skip = 1
                return
            d = {k.lower(): (v or "") for k, v in attrs}
            if is_remote(d.get("src")):
                return  # remote script: wget never saved it; drop the tag
        if tag in DROP_WITH_CONTENT:
            self.skip = 1
            return
        cleaned = self._clean(tag, attrs)
        if cleaned is None:
            return
        if tag == "title":
            self.in_title = True
        elif tag == "style":
            self.in_style = True
        elif tag == "script":
            self.in_script = True
            self.script_src = next((v for k, v in cleaned if k == "src"), None)
        self.out.append(self._render(tag, cleaned))

    def handle_startendtag(self, tag, attrs):
        if self.skip or tag in DROP_WITH_CONTENT:
            return
        if tag == "script":
            if not self.live:
                return
            d = {k.lower(): (v or "") for k, v in attrs}
            if is_remote(d.get("src")):
                return
        cleaned = self._clean(tag, attrs)
        if cleaned is not None:
            self.out.append(self._render(tag, cleaned, selfclose=True))

    def handle_endtag(self, tag):
        if self.skip:
            if tag in DROP_WITH_CONTENT or (tag == "script" and not self.live):
                self.skip -= 1
            return
        if tag in DROP_WITH_CONTENT or tag in DROP_VOID:
            return
        if tag == "title":
            self.in_title = False
        elif tag == "style":
            self.in_style = False
        elif tag == "script":
            self.in_script = False
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_style:
            data = scrub_css_text(data)
        elif self.live and self.in_script and not self.script_src:
            data = scrub_js_text(data)
        self.out.append(data)
        if self.in_title:
            self.title += data
        elif not self.in_style and not self.in_script:
            self.text.append(data)

    def handle_entityref(self, name):
        if not self.skip:
            self.out.append(f"&{name};")
            if self.in_title:
                self.title += html.unescape(f"&{name};")

    def handle_charref(self, name):
        if not self.skip:
            self.out.append(f"&#{name};")
            if self.in_title:
                self.title += html.unescape(f"&#{name};")

    # Comments (incl. IE conditional comments), PIs and CDATA are dropped.
    def handle_comment(self, data): pass
    def handle_pi(self, data): pass
    def unknown_decl(self, data): pass


def scrub_file(path, live=False):
    """Scrub one saved HTML file in place. Returns (title, text_hash)."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    s = Scrubber(here=path.parent, live=live)
    s.feed(text)
    s.close()
    out = "".join(s.out)
    if not s.saw_doctype:
        out = "<!DOCTYPE html>" + (INJECT_LIVE if live else INJECT) + out
    path.write_text(out, encoding="utf-8")
    words = " ".join("".join(s.text).split())
    return " ".join(s.title.split()), hashlib.sha256(words.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- storage
#
# manifest.json holds two lists:
#   pages:     every snapshot ever saved (number = position, 1-based, stable)
#              {url, title, saved, path, hash, bookmark, group, archived}
#   bookmarks: links you want kept fresh (number = position, 1-based)
#              {id, url, name, first_party, allow, added, checked, current}
# Snapshots live in pages/<snapshot>/ while current and move to
# archive/<snapshot>/ once a newer copy replaces them.

def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def load_state():
    st = {"pages": [], "bookmarks": []}
    if MANIFEST.exists():
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        if isinstance(data, list):          # older single-list format
            st["pages"] = data
        else:
            st.update(data)
    for p in st["pages"]:
        p.setdefault("archived", False)
        p.setdefault("bookmark", None)
        p.setdefault("group", None)
        p.setdefault("hash", "")
        p.setdefault("live", False)
    return st


def save_state(st):
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
    os.replace(tmp, MANIFEST)
    write_index(st)


def page_num(st, page):
    return st["pages"].index(page) + 1


def find_page(st, path):
    for p in st["pages"]:
        if p["path"] == path:
            return p
    return None


def archive_page(page):
    """Move a snapshot folder from pages/ to archive/ and mark it archived."""
    parts = page["path"].split("/")
    if parts[0] == "pages":
        src = CACHE / "pages" / parts[1]
        dst = CACHE / "archive" / parts[1]
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.exists():
            shutil.move(str(src), str(dst))
        page["path"] = "/".join(["archive"] + parts[1:])
    page["archived"] = True


STYLE = (
    "<style>"
    ":root{--fg:#1d1d1f;--mute:#6e6e73;--line:#e5e5ea;--bg:#fff;--acc:#0a58ca}"
    "@media (prefers-color-scheme:dark){:root{--fg:#f2f2f7;--mute:#98989f;"
    "--line:#2c2c2e;--bg:#141414;--acc:#6ea8fe}}"
    "body{font:16px/1.5 -apple-system,system-ui,'Segoe UI',sans-serif;max-width:780px;"
    "margin:2em auto;padding:0 16px;color:var(--fg);background:var(--bg)}"
    "a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}"
    "h2{font-size:1.05em;margin-top:2.2em;padding-bottom:.3em;border-bottom:1px solid var(--line)}"
    "ol,ul{padding-left:1.6em}li{margin:.7em 0}"
    "small{color:var(--mute);word-break:break-all}"
    "details{margin:.4em 0}summary{cursor:pointer;color:var(--mute)}"
    ".n{color:var(--mute);font-variant-numeric:tabular-nums}"
    "</style>"
)


def write_index(st):
    pages, marks = st["pages"], st["bookmarks"]
    h = html.escape
    out = ["<!DOCTYPE html>", INJECT, "<title>Offline reader</title>", STYLE,
           "<h1>Offline reader</h1>"]

    if marks:
        out.append("<h2>Bookmarks</h2><ol>")
        for bn, b in enumerate(marks, 1):
            cur = find_page(st, b.get("current"))
            old = [p for p in pages if p["bookmark"] == b["id"] and p["archived"]]
            label = h(b["name"] or (cur and cur["title"]) or b["url"])
            link = f'<a href="{quote(cur["path"])}">{label}</a>' if cur else label
            meta = []
            if cur:
                meta.append(f"saved {h(cur['saved'])}")
            if b.get("checked"):
                meta.append(f"checked {h(b['checked'])}")
            if old:
                meta.append(f'<a href="#arch-{b["id"]}">{len(old)} older</a>')
            out.append(f'<li value="{bn}">{link}<br><small>{" &middot; ".join(meta)}'
                       f'<br>{h(b["url"])}</small></li>')
        out.append("</ol>")

    loose = [p for p in pages if not p["archived"] and p["bookmark"] is None]
    if loose:
        out.append("<h2>Saved pages</h2><ul>")
        for p in reversed(loose):
            out.append(f'<li><span class="n">#{page_num(st, p)}</span> '
                       f'<a href="{quote(p["path"])}">{h(p["title"] or p["url"])}</a><br>'
                       f'<small>{h(p["saved"])} &middot; {h(p["url"])}</small></li>')
        out.append("</ul>")

    arch = [p for p in pages if p["archived"]]
    if arch:
        out.append("<h2>Archive</h2>")
        groups = {}
        for p in arch:
            groups.setdefault((p["bookmark"], p["group"] or p["url"]), []).append(p)
        for (bid, name), items in groups.items():
            anchor = f' id="arch-{bid}"' if bid is not None else ""
            out.append(f"<details{anchor}><summary>{h(name)} ({len(items)})</summary><ul>")
            for p in reversed(items):
                out.append(f'<li><span class="n">#{page_num(st, p)}</span> '
                           f'<a href="{quote(p["path"])}">{h(p["saved"])}</a> '
                           f'<small>{h(p["title"])}</small></li>')
            out.append("</ul></details>")

    if not (marks or loose or arch):
        out.append("<p>Nothing saved yet.</p>")
    (CACHE / "index.html").write_text("".join(out), encoding="utf-8")


# ---------------------------------------------------------------- fetch

WGET_COMMON = ["--no-cookies", "-e", "robots=off", "--timeout=20", "--tries=2",
               "--user-agent", UA]


def find_pixels(url, env):
    """Fetch only the page's HTML (nothing else) and list its tracking pixels."""
    r = subprocess.run(["wget", "-q", "-O", "-", *WGET_COMMON, url],
                       env=env, capture_output=True)
    if r.returncode != 0 or not r.stdout:
        return set()
    try:
        text = r.stdout.decode("utf-8")
    except UnicodeDecodeError:
        text = r.stdout.decode("cp1252", errors="replace")
    pf = PixelFinder(url)
    try:
        pf.feed(text)
        pf.close()
    except Exception:
        pass
    return pf.pixels


def snapshot(url, first_party=False, allow=(), live=False):
    """Download, de-pixel and scrub one page into pages/<new folder>.
    Returns a page record (not yet added to the manifest) or None."""
    if not shutil.which("wget"):
        sys.exit("wget not found. Mac: brew install wget   Windows: winget install JernejSimoncic.Wget")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    host = urlparse(url).hostname or "page"
    dest = CACHE / "pages" / f"{stamp}-{host}"
    k = 1
    while dest.exists() or (CACHE / "archive" / dest.name).exists():
        k += 1
        dest = CACHE / "pages" / f"{stamp}-{k}-{host}"
    dest.mkdir(parents=True)
    env = dict(os.environ, LC_ALL="C")
    print(f"fetching {url}")

    pixels = find_pixels(url, env)
    reject = AD_REGEX
    if pixels:
        reject += "|" + "|".join("^" + ere_escape(p) + "$" for p in sorted(pixels))
        print(f"  refusing {len(pixels)} tracking pixel(s)")

    cmd = [
        "wget",
        "--page-requisites", "--convert-links", "--adjust-extension",
        "--span-hosts", "--no-verbose", *WGET_COMMON,
        "--reject", "*.json,*.wasm,*.swf" + ("" if live else ",*.js,*.mjs"),
        "--reject-regex", "(" + reject + ")",
        "--restrict-file-names=windows",
        "-P", str(dest),
    ]
    if first_party:
        cmd += ["--domains", ",".join([base_domain(host), *allow])]
    cmd.append(url)
    r = subprocess.run(cmd, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    log = r.stdout + r.stderr

    m = re.search(r'-> "(.+?)"', log)
    if not m:
        shutil.rmtree(dest, ignore_errors=True)
        print(log[-2000:], file=sys.stderr)
        print(f"  failed: could not save {url}", file=sys.stderr)
        return None
    main = Path(m.group(1))
    if not main.is_absolute():
        main = Path.cwd() / main
    main = main.resolve()

    # Backstop: anything that downloaded but is really a pixel gets deleted.
    # The scrubber then drops <img> tags that point at missing files.
    removed = 0
    for f in list(dest.rglob("*")):
        if (f.is_file() and f.resolve() != main.resolve()
                and os.path.splitext(f.name.split("@")[0])[1].lower() in IMAGE_EXTS
                and image_is_tiny(f)):
            f.unlink()
            removed += 1
    if removed:
        print(f"  removed {removed} pixel-sized image(s)")

    title, digest = "", ""
    for f in dest.rglob("*"):
        if f.suffix.lower() in (".html", ".htm") and f.is_file():
            t, d = scrub_file(f, live)
            if f.resolve() == main.resolve():
                title, digest = t, d
        elif live and f.suffix.lower() in (".css", ".js", ".mjs") and f.is_file():
            scrub_asset(f)

    return {
        "url": url,
        "title": title,
        "saved": now(),
        "path": main.relative_to(CACHE.resolve()).as_posix(),
        "hash": digest,
        "bookmark": None,
        "group": None,
        "archived": False,
        "live": live,
    }


def discard(page):
    folder = CACHE / page["path"].split("/")[0] / page["path"].split("/")[1]
    shutil.rmtree(folder, ignore_errors=True)


def refresh(st, b, force=False):
    """Re-fetch one bookmark. Returns 'new', 'changed', 'same' or 'failed'."""
    page = snapshot(b["url"], b.get("first_party", False), b.get("allow", []),
                    b.get("live", False))
    b["checked"] = now()
    if page is None:
        return "failed"
    cur = find_page(st, b.get("current"))
    if cur and not force and page["hash"] and page["hash"] == cur["hash"]:
        discard(page)
        print("  unchanged, kept the copy you have")
        return "same"
    page["bookmark"] = b["id"]
    page["group"] = b["name"] or page["title"] or b["url"]
    st["pages"].append(page)
    if cur:
        archive_page(cur)
        print(f"  updated; previous copy moved to the archive")
    else:
        print(f"  saved: {page['title'] or b['url']}")
    b["current"] = page["path"]
    return "changed" if cur else "new"


# ---------------------------------------------------------------- serve

# Recent /go fetch failures: (url, live) -> epoch. Retrying within FAIL_TTL
# seconds returns the failure page again without another doomed wget run.
FAILURES = {}
FAIL_TTL = 60


class Handler(SimpleHTTPRequestHandler):
    _fetch_lock = threading.Lock()

    # matches <a href="https://..."> in double quotes, single quotes, or bare
    A_RE = re.compile(r'(<a\b[^>]*?\bhref=)(["\'])(https?://[^"\']+)\2', re.I)
    A_RE_BARE = re.compile(r'(<a\b[^>]*?\bhref=)(https?://[^\s>]+)', re.I)
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(CACHE), **k)

    def end_headers(self):
        live = getattr(self, "_live", False)
        self.send_header("Content-Security-Policy", CSP_LIVE if live else CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-DNS-Prefetch-Control", "off")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *a):
        pass

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/go":
                return self._go(parsed)
            if parsed.path.lower().endswith((".html", ".htm")):
                return self._serve_rewritten()
            super().do_GET()
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser moved on mid-response; nothing to do

    def _serve_rewritten(self):
        """Serve a saved page with its live-web links pointed back at /go."""
        path = Path(self.translate_path(self.path))
        if not path.is_file():
            return super().do_GET()
        text = path.read_text(encoding="utf-8")
        self._live = '<script src=' in text  # only live pages keep scripts
        extra = "&live=1" if self._live else ""
        text = self.A_RE.sub(
            lambda m: f'{m.group(1)}{m.group(2)}/go?url={quote(m.group(3), safe="")}{extra}{m.group(2)}',
            text)
        text = self.A_RE_BARE.sub(
            lambda m: f'{m.group(1)}"/go?url={quote(m.group(2), safe="")}{extra}"',
            text)
        self._send(text)

    def _send(self, body, status=200, location=None):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(data)

    def _page(self, body, status=200):
        self._send("<!DOCTYPE html>" + INJECT + "<title>offread</title>" + STYLE + body,
                   status=status)

    def _redirect(self, page):
        self._send("", status=303, location="/" + quote(page["path"]))

    def _go(self, parsed):
        """Catch a click to the live web: save the page, serve the copy."""
        q = parse_qs(parsed.query)
        url = urldefrag(q.get("url", [""])[0])[0]
        live = "live" in q
        if urlparse(url).scheme not in ("http", "https"):
            return self._page("<p>Bad link.</p>", status=400)
        st = load_state()
        have = next((p for p in st["pages"] if not p["archived"] and p["url"] == url
                     and bool(p.get("live")) == live), None)
        if have:
            return self._redirect(have)
        if "run" not in q:
            nxt = "/go?" + urlencode({"url": url, "run": "1", **({"live": "1"} if live else {})})
            return self._page(
                f'<meta http-equiv="refresh" content="0;url={nxt}">'
                f"<p>Fetching <b>{html.escape(url)}</b> &mdash; "
                f"this can take a few seconds&hellip;</p>")
        with Handler._fetch_lock:
            st = load_state()  # another click may have saved it meanwhile
            have = next((p for p in st["pages"]
                         if not p["archived"] and p["url"] == url
                         and bool(p.get("live")) == live), None)
            if not have:
                fail_at = FAILURES.get((url, live), 0)
                if time.time() - fail_at < FAIL_TTL:
                    print(f"  still failing (cached): {url}", file=sys.stderr)
                    return self._fail_page(url)
                have = snapshot(url, live=live)
                if have is None:
                    FAILURES[(url, live)] = time.time()
                    return self._fail_page(url)
                st["pages"].append(have)
                save_state(st)
        self._redirect(have)

    def _fail_page(self, url):
        return self._page(
            "<p>Could not save that page &mdash; the site refused or timed out. "
            "Details are in the server terminal. Trying again within a minute "
            "won't refetch.</p>"
            f'<p><a href="{html.escape(url, quote=True)}">'
            "Open the live version</a></p>", status=502)


def serve(port, open_path=None):
    CACHE.mkdir(parents=True, exist_ok=True)
    write_index(load_state())
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    target = f"http://127.0.0.1:{port}/" + (quote(open_path) if open_path else "index.html")
    print(f"reading at {target}  (Ctrl-C to stop)")
    webbrowser.open(target)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------- cli

def pick(seq, nums, what):
    out = []
    for n in nums:
        if not 1 <= n <= len(seq):
            sys.exit(f"no {what} #{n}")
        out.append(seq[n - 1])
    return out


def main():
    ap = argparse.ArgumentParser(description="Fetch pages with wget and read them offline.")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def fetch_opts(p):
        p.add_argument("--first-party", action="store_true",
                       help="only fetch images/CSS/fonts from the page's own domain")
        p.add_argument("--allow", action="append", default=[], metavar="DOMAIN",
                       help="extra domain to allow with --first-party (e.g. a CDN)")
        p.add_argument("--live", action="store_true",
                       help="keep the page's own scripts/styles running, with all "
                            "outgoing requests blocked (trackers can't phone home)")

    g = sub.add_parser("get", help="save a page once")
    g.add_argument("urls", nargs="+")
    g.add_argument("--open", action="store_true", help="open the last page when done")
    fetch_opts(g)

    mk = sub.add_parser("mark", help="bookmark links and save them now")
    mk.add_argument("urls", nargs="+")
    mk.add_argument("--name", help="label for the bookmark (one URL only)")
    fetch_opts(mk)

    sub.add_parser("marks", help="list bookmarks")

    up = sub.add_parser("update", help="re-fetch bookmarks; old copies go to the archive")
    up.add_argument("n", nargs="*", type=int, help="bookmark numbers (default: all)")
    up.add_argument("--force", action="store_true", help="keep a new copy even if unchanged")

    um = sub.add_parser("unmark", help="remove bookmarks (saved copies are kept)")
    um.add_argument("n", nargs="+", type=int)

    ls = sub.add_parser("list", help="list saved pages")
    ls.add_argument("--all", action="store_true", help="include the archive")

    rd = sub.add_parser("read", help="open a saved page (or the index)")
    rd.add_argument("n", nargs="?", type=int)
    rd.add_argument("-b", "--bookmark", action="store_true",
                    help="N is a bookmark number; open its latest copy")

    sub.add_parser("serve", help="run the local reader and open the index")
    args = ap.parse_args()
    st = load_state()

    if args.cmd == "get":
        last = None
        for u in args.urls:
            page = snapshot(u, args.first_party, args.allow, args.live)
            if page:
                st["pages"].append(page)
                save_state(st)
                last = page
                print(f"  saved #{len(st['pages'])}: {page['title'] or u}")
        if args.open and last:
            serve(args.port, last["path"])

    elif args.cmd == "mark":
        if args.name and len(args.urls) > 1:
            sys.exit("--name works with one URL at a time")
        for u in args.urls:
            b = next((x for x in st["bookmarks"] if x["url"] == u), None)
            if b:
                print(f"already bookmarked as #{st['bookmarks'].index(b) + 1}; options updated")
            else:
                ids = [x["id"] for x in st["bookmarks"]] + \
                      [p["bookmark"] for p in st["pages"] if p["bookmark"] is not None]
                b = {"id": max(ids, default=0) + 1, "url": u, "name": None,
                     "added": now(), "checked": None, "current": None}
                st["bookmarks"].append(b)
            if args.name:
                b["name"] = args.name
            b["first_party"] = args.first_party
            b["allow"] = args.allow
            b["live"] = args.live
            if not b["current"]:
                refresh(st, b)
            save_state(st)

    elif args.cmd == "marks":
        if not st["bookmarks"]:
            print("no bookmarks yet. Add one with: mark URL")
        for n, b in enumerate(st["bookmarks"], 1):
            cur = find_page(st, b.get("current"))
            old = sum(1 for p in st["pages"] if p["bookmark"] == b["id"] and p["archived"])
            label = b["name"] or (cur and cur["title"]) or b["url"]
            when = cur["saved"] if cur else "never saved"
            print(f"{n:4}  {when:16}  {label}" + (f"  [{old} archived]" if old else ""))

    elif args.cmd == "update":
        marks = pick(st["bookmarks"], args.n, "bookmark") if args.n else st["bookmarks"]
        tally = {"new": 0, "changed": 0, "same": 0, "failed": 0}
        for b in marks:
            tally[refresh(st, b, args.force)] += 1
            save_state(st)
        print(f"\n{tally['changed'] + tally['new']} updated, {tally['same']} unchanged, "
              f"{tally['failed']} failed")

    elif args.cmd == "unmark":
        for b in pick(st["bookmarks"], args.n, "bookmark"):
            st["bookmarks"].remove(b)
            cur = find_page(st, b.get("current"))
            if cur:
                cur["bookmark"] = None  # latest copy stays, under Saved pages
            print(f"removed bookmark: {b['name'] or b['url']}")
        save_state(st)

    elif args.cmd == "list":
        for n, p in enumerate(st["pages"], 1):
            if p["archived"] and not args.all:
                continue
            tag = " [archived]" if p["archived"] else (" [bookmark]" if p["bookmark"] else "")
            print(f"{n:4}  {p['saved']}  {p['title'] or p['url']}{tag}")

    elif args.cmd == "read":
        path = None
        if args.n:
            if args.bookmark:
                b = pick(st["bookmarks"], [args.n], "bookmark")[0]
                path = b.get("current") or sys.exit("that bookmark hasn't been saved yet")
            else:
                path = pick(st["pages"], [args.n], "page")[0]["path"]
        serve(args.port, path)

    elif args.cmd == "serve":
        serve(args.port)


if __name__ == "__main__":
    main()
