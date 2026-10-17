# offread

Fetch web pages with wget, scrub them, and read them offline.

## Usage

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

Pages live in `~/OfflineReader` (override with `OFFREAD_DIR`): current copies
in `pages/`, replaced bookmark copies in `archive/`.

## What it does

**Fetch:** wget pulls the page plus its images/CSS/fonts. JavaScript files are
refused by extension (.js/.mjs/...), cookies are off, and common ad/tracker
hosts are refused. A script served from an extension-less URL would still
download, but the scrubber removes its tag and the reader's CSP blocks it.

**Pixels:** before downloading anything else, the page's HTML is read and
tracking pixels are identified (tiny or hidden images, third-party images
inside `<noscript>`, third-party images with beacon/track/pixel-style URLs).
Those exact URLs are refused, so they are never requested. After the download,
any image that turns out to be 3x3 or smaller, or a few hundred bytes, is
deleted and its tag removed as a backstop.

**Scrub:** every saved HTML file has scripts, iframes, embeds, event handlers,
meta refreshes, preconnect/prefetch hints, and any remaining remote
src/srcset/stylesheet references removed.

**Read:** a tiny server bound to 127.0.0.1 only serves the files with a
Content-Security-Policy that forbids the page from loading anything from
anywhere but that local server. Even if the scrubber missed something, the
browser will refuse to make the request. Links to other pages are routed
through the server: the target is fetched and saved, and the local copy opens
instead of the live web.

**Live mode (`--live`):** the page's own downloaded scripts and stylesheets
are kept so it renders and behaves as designed — accordions, lightboxes,
fonts, layout JS all work. But there is no egress: remote URLs inside saved
CSS/JS are repointed to an inert placeholder, a shim injected into the page
no-ops `sendBeacon`, `WebSocket`, `EventSource` and persistent storage, and
the CSP only ever allows the local server. Links clicked from a live page are
fetched with `--live` too.

## Reading modes: what to expect

**Default (fully stripped).** Use for anything you don't trust, and as the
everyday default.

- You get the article: text, images, and the page's own downloaded CSS for
  basic styling. No JavaScript at all.
- Nothing on the page can contact anything — no trackers, no beacons, no
  exceptions. This is the airtight mode.
- Expect some pages to look plainer than the live site, and JS-driven
  widgets (comment sections, live tickers, expandable galleries) to be inert
  or missing.

**Live (`--live`).** Use for pages from outlets you broadly trust where the
stripped version is unreadable — heavy layout JS, paywalled sites that hide
content behind scripts, image galleries.

- The page renders and behaves much like the real thing.
- Trackers still can't phone home: every outbound channel is blocked at
  three layers (wget refused known trackers at fetch time, the scrubber
  repointed URLs in the saved files, and CSP + the shim block the rest at
  read time).
- The caveat: you are now *executing code written by the site* in your
  browser. It can't send data out, but a hostile (not merely nosey) script
  can still waste CPU, mine crypto in a loop, or make the tab annoying.
  If you wouldn't run the site's app, don't read it in live mode.

Both modes store pages the same way and can be mixed: plain and live copies
of the same URL coexist, and bookmarks remember which mode you chose.

## Requirements

wget (Mac: `brew install wget`; Windows: `winget install JernejSimoncic.Wget`)
and Python 3.8+. Standard library only.

## License

MIT
