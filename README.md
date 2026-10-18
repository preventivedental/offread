# offread

A small command-line tool that fetches a page with wget, strips the worst tracking and active content, saves a local copy, and serves it through a local reader with a restrictive CSP.

It is designed for reading a page later without leaving your browser exposed to the live web, not for replacing a real browser or a full web-automation stack.

## Quick start

```bash
python3 offread.py get https://example.com --open
python3 offread.py mark https://example.org
python3 offread.py update
python3 offread.py read
```

## Command summary

```bash
python3 offread.py get URL [URL ...]     fetch and save one or more pages
    --open                open the last page when done
    --first-party         only pull images/CSS/fonts from the page's own domain
    --allow DOMAIN        extra domain allowed with --first-party (repeatable)
    --live                keep the page's own scripts/styles, but block egress

python3 offread.py mark URL [URL ...]    bookmark links and save them now
    --name LABEL          label for the bookmark (one URL at a time)
    (--first-party / --allow / --live work here too and are remembered)

python3 offread.py marks                 list bookmarks
python3 offread.py update [N ...]        refresh bookmarks; old copies move to archive
python3 offread.py unmark N [N ...]      remove bookmarks, keep saved copies
python3 offread.py list [--all]          show saved pages; --all includes archive
python3 offread.py read [N]              open a saved page in the browser
python3 offread.py read -b N             open a bookmark's latest copy
python3 offread.py serve                 run the local reader only
```

Global flag:

```bash
python3 offread.py --port 8787 serve
```

Pages are stored under `~/OfflineReader` by default, or under `OFFREAD_DIR` if set.

## What it does

### Fetch and save

- Uses `wget` to pull the target page and its page-level requisites.
- Refuses JavaScript files by filename suffix (`.js`, `.mjs`, etc.) unless you explicitly use live mode.
- Uses a built-in blocklist for common ad/tracker hosts and refuses known tracking-pixel patterns before downloading the page.
- Tries to catch small tracking pixels and strips their `<img>` tags after download.
- Saves each fetched copy under a timestamped directory under `pages/` and keeps bookmark history under `archive/` when refreshed.

### Scrub HTML

The saved HTML is rewritten to remove or neutralize the most common tracking and active content:

- removes scripts, iframes, embeds, objects, event handlers, meta refreshes, and some preload hints
- drops remote `src`, `srcset`, `poster`, `data`, `background`, and stylesheet references
- strips `javascript:`/`vbscript:` and similar active URL schemes
- rewrites CSS `url(...)` and `@import` references to a blank placeholder
- strips many remote references from fetched CSS/JS as part of the live-mode safety layer

This is a best-effort sanitizer, not a browser DOM engine.

### Read locally with restrictions

- The local reader runs on `127.0.0.1` only.
- Each served page gets a strict Content-Security-Policy so the page is only allowed to load from the local reader.
- Links from saved pages are routed through the local `/go` handler, which fetches the target and then opens the sanitized local copy instead of the live page.
- This means the browser is not allowed to reach the original site on a saved-offline page unless you explicitly click a live link or open the original URL directly outside the reader.

### Live mode

`--live` keeps the page's own downloaded scripts and stylesheets, while still blocking outbound network access:

- remote URLs in CSS and JS are rewritten to inert placeholders
- the page gets a shim that blocks `sendBeacon`, `WebSocket`, `EventSource`, and persistent storage access
- the CSP still allows only the local reader and the injected script shim

This is useful for pages whose layout or content relies on page scripts, but it is still not browser-equivalent safety.

## What it does not do

This tool is intentionally narrow, and it is easy to overestimate what it guarantees.

- It is not a full browser. It does not emulate a browser's JavaScript runtime, plugin environment, or login/session state.
- It does not guarantee that every tracker or malicious script is removed. It is a sanitizer and a policy gate, not a full browser sandbox.
- It does not bypass bot checks or Cloudflare-type challenges. If a site requires a browser challenge, `wget` may fail and the page may not be saved.
- It does not preserve user authentication state unless you explicitly give `wget` a cookie file via `OFFREAD_WGET_COOKIES`.
- It does not guarantee the saved page is a faithful copy of the live site. Some JS-heavy, CSS-heavy, or anti-bot pages will be incomplete or visually different.
- It does not provide anonymity or privacy by itself. A page fetch is still a real request from your IP to the source site.
- It is not a secure, general-purpose web proxy, and it is not designed to be exposed beyond localhost.

## Reading modes

### Default mode

The default mode strips active content aggressively. You get a simpler, safer, text-first copy.

Good for:

- regular articles and text-heavy pages
- anything you do not trust or do not want to execute
- reducing tracking and remote content before reading

Expect:

- missing JS widgets and interactive elements
- plainer layout and styling
- some pages to look less like the live site

### Live mode (`--live`)

Live mode keeps the page's own scripts and styles, so it behaves closer to the real site.

Good for:

- pages whose content is hidden behind script-based layout or rendering
- image-heavy pages or galleries that depend on local JS/CSS to read correctly

Risk:

- the site is still executing code in your browser
- hostile or buggy scripts may still consume CPU or cause annoyance, even though network egress is blocked
- use this only for pages whose JavaScript you are willing to run in a constrained browser environment

## OPSEC: do's and don'ts

### Do

- Use this behind a VPN, SOCKS proxy, or other network egress control if you want the page fetch to be less directly attributable to your home IP.
- Prefer the default stripped mode for anything sensitive, untrusted, or not worth running in a browser.
- Use `--first-party` when the page depends on a CDN but you want to keep the archive local and smaller.
- Put `OFFREAD_DIR` on encrypted storage if the saved pages are sensitive; the content, URLs, and timestamps are stored as ordinary local files.
- Keep the local reader bound to `127.0.0.1` only; do not put it behind a reverse proxy, port forward, or remote tunnel.
- Treat the local directory as browsing history: page list, archive, and bookmark metadata are all real evidence of what you fetched.

### Don't

- Do not assume a fetch is a browser-like request. `wget` is not a browser, and its TLS/HTTP fingerprint will differ from a browser session.
- Do not trust blocklists as a guarantee. The ad/tracker and pixel filters are useful heuristics, not a perfect defense.
- Do not expose the reader outside localhost; other local processes can already browse the saved index and can trigger fetches if they can reach the service.
- Do not open raw saved HTML directly from the filesystem and assume the CSP or rewritten links are active; use the local reader instead.
- Do not sync the `OfflineReader` directory to a cloud folder, shared repo, or backup that you do not want to leak browsing history.
- Do not assume a page is safe just because it was sanitized. The tool reduces obvious remote behavior, but it is not a certificate of trust.
- Do not treat `--live` as safe-by-default. It is intentionally less restrictive, and it still runs the page's code in your browser.

## Requirements

- Python 3.8+
- `wget` installed
  - macOS: `brew install wget`
  - Windows: `winget install JernejSimoncic.Wget`

## License

MIT
