# offread Firefox add-on

This folder contains a minimal Firefox extension that intercepts top-level browser navigations, snapshots the requested page, strips executable and network-bearing markup, and renders the saved copy inside an extension page.

## What it does

- Runs persistently in the background.
- Intercepts top-level `http://` and `https://` navigations before the live page loads.
- Uses `storage` plus web request interception; it does not request history or browsing-wide access.
- Saves the current page HTML locally, then renders a sanitized copy inside `viewer.html`.
- Removes scripts, iframes, embeds, remote links, form actions, and other obvious egress points.
- Rewrites external images to a blank data URI so the viewer never needs to fetch them.

## Load it in Firefox

1. Open `about:debugging#/runtime/this-firefox`.
2. Click `Load Temporary Add-on...`.
3. Select `manifest.json` from this folder.
4. Type a web address into the address bar. The extension will fetch it, save it, and open the internal viewer instead of the live site.

## Notes

- This is a simple snapshot reader, not a full mirror of the original page.
- It intentionally monitors only top-level navigations, not arbitrary page activity.
- If you want the Python fetch/scrub pipeline wired into Firefox instead, that would need a native-host bridge.