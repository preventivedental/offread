const STORAGE_KEY = "offread:snapshots";
const CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; base-uri 'none'; form-action 'none'; frame-src 'none'; connect-src 'none'; script-src 'none'";

const DROP_WITH_CONTENT = new Set(["iframe", "object", "frame", "frameset", "applet", "template", "noembed", "portal"]);
const DROP_VOID = new Set(["embed", "base", "param"]);
const LOAD_ATTRS = new Set(["src", "srcset", "poster", "data", "background", "lowsrc", "dynsrc", "imagesrcset", "xlink:href"]);
const BAD_RELS = new Set(["preconnect", "dns-prefetch", "prefetch", "preload", "prerender", "modulepreload", "manifest", "pingback", "import"]);
const STRIP_ATTRS = new Set(["ping", "formaction", "action", "integrity", "nonce", "crossorigin", "attributionsrc"]);
const BLANK_PIXEL = "data:image/gif;base64,R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==";

function getParams() {
  return new URLSearchParams(location.search);
}

function escapeText(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function isRemote(value) {
  const text = String(value || "").trim().toLowerCase();
  return text.includes("://") || text.startsWith("//");
}

function scrubCssText(css) {
  return String(css)
    .replace(/url\(\s*(['"]?)(?:https?:)?\/\/[^)'"\s]+\1\s*\)/gi, `url("${BLANK_PIXEL}")`)
    .replace(/@import\s+(['"])(?:https?:)?\/\/[^'"]+\1/gi, `@import "${BLANK_PIXEL}"`);
}

function absolutizeUrl(rawUrl, baseUrl) {
  try {
    return new URL(rawUrl, baseUrl).href;
  } catch {
    return rawUrl;
  }
}

function sanitizeElement(element, baseUrl) {
  const tag = element.tagName.toLowerCase();

  if (DROP_WITH_CONTENT.has(tag) || DROP_VOID.has(tag)) {
    element.remove();
    return;
  }

  if (tag === "meta") {
    const httpEquiv = (element.getAttribute("http-equiv") || "").toLowerCase();
    if (element.hasAttribute("charset") || ["refresh", "content-type", "set-cookie", "content-security-policy"].includes(httpEquiv)) {
      element.remove();
      return;
    }
  }

  if (tag === "link") {
    const rels = (element.getAttribute("rel") || "").toLowerCase().split(/\s+/).filter(Boolean);
    const href = element.getAttribute("href") || "";
    if (rels.some((rel) => BAD_RELS.has(rel)) || isRemote(href)) {
      element.remove();
      return;
    }
  }

  if (tag === "img") {
    const src = element.getAttribute("src") || "";
    if (!src || isRemote(src)) {
      element.setAttribute("src", BLANK_PIXEL);
      element.setAttribute("data-offread-src", src || "");
    }
    element.removeAttribute("srcset");
    element.removeAttribute("imagesrcset");
    element.removeAttribute("crossorigin");
  }

  if (tag === "source") {
    element.removeAttribute("srcset");
    element.removeAttribute("imagesrcset");
    element.removeAttribute("media");
    element.removeAttribute("type");
  }

  if (tag === "style") {
    element.textContent = scrubCssText(element.textContent || "");
  }

  for (const attr of Array.from(element.attributes)) {
    const name = attr.name.toLowerCase();
    const value = attr.value || "";
    const low = value.trim().toLowerCase();

    if (name.startsWith("on") || STRIP_ATTRS.has(name)) {
      element.removeAttribute(attr.name);
      continue;
    }

    if (low.startsWith("javascript:") || low.startsWith("vbscript:") || low.startsWith("data:text/html")) {
      element.removeAttribute(attr.name);
      continue;
    }

    if (name === "href") {
      if (tag === "a" || tag === "area") {
        if (value && !value.startsWith("#")) {
          element.setAttribute("data-offread-href", absolutizeUrl(value, baseUrl));
          element.setAttribute("href", "#");
        }
      } else if (isRemote(value)) {
        element.removeAttribute(attr.name);
      }
      continue;
    }

    if (LOAD_ATTRS.has(name) && isRemote(value)) {
      element.removeAttribute(attr.name);
      continue;
    }

    if (name === "style" && (low.includes("url(") || low.includes("@import"))) {
      element.setAttribute("style", scrubCssText(value));
    }
  }
}

function sanitizeSnapshot(snapshot) {
  const parser = new DOMParser();
  const doc = parser.parseFromString(snapshot.html || "", "text/html");
  const baseUrl = snapshot.url || doc.baseURI || location.href;

  Array.from(doc.querySelectorAll("*")).forEach((element) => sanitizeElement(element, baseUrl));

  const title = (doc.title || snapshot.title || snapshot.url || "Saved page").trim();
  const headStyles = Array.from(doc.head ? doc.head.querySelectorAll("style") : [])
    .map((style) => `<style>${scrubCssText(style.textContent || "")}</style>`)
    .join("");
  const body = doc.body ? doc.body.innerHTML : "";

  return {
    title,
    html: `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${CSP}">${headStyles}</head><body>${body}</body></html>`,
  };
}

async function getSnapshots() {
  const result = await browser.storage.local.get(STORAGE_KEY);
  return Array.isArray(result[STORAGE_KEY]) ? result[STORAGE_KEY] : [];
}

function findSnapshot(snapshots, id) {
  return snapshots.find((snapshot) => snapshot.id === id) || snapshots[0] || null;
}

function renderList(snapshots, activeId) {
  const list = document.getElementById("snapshot-list");
  list.innerHTML = "";

  if (!snapshots.length) {
    list.innerHTML = `<p class="meta">No snapshots yet. Click the toolbar button on any page to save one.</p>`;
    return;
  }

  for (const snapshot of snapshots) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `snapshot-item${snapshot.id === activeId ? " is-active" : ""}`;
    button.innerHTML = `
      <div class="title">${escapeText(snapshot.title || snapshot.url || "Saved page")}</div>
      <div class="meta">${escapeText(snapshot.savedAt || "")}</div>
      <div class="meta">${escapeText(snapshot.url || "")}</div>
    `;
    button.addEventListener("click", () => loadSnapshot(snapshot.id));
    list.appendChild(button);
  }
}

async function loadSnapshot(id) {
  const snapshots = await getSnapshots();
  const snapshot = findSnapshot(snapshots, id);
  const title = document.getElementById("page-title");
  const meta = document.getElementById("page-meta");
  const frame = document.getElementById("snapshot-frame");

  if (!snapshot) {
    title.textContent = "Saved page";
    meta.textContent = "No snapshot available";
    frame.srcdoc = `<!doctype html><meta charset="utf-8"><p style="font:16px system-ui,sans-serif">No snapshot found.</p>`;
    renderList([], null);
    return;
  }

  const safe = sanitizeSnapshot(snapshot);
  title.textContent = safe.title;
  meta.textContent = `${snapshot.savedAt || ""}  ${snapshot.url || ""}`.trim();
  frame.srcdoc = safe.html;
  renderList(snapshots, snapshot.id);
}

function resnapshotCurrentTab() {
  window.close();
}

document.getElementById("resnap").addEventListener("click", resnapshotCurrentTab);

(async () => {
  const params = getParams();
  const id = params.get("id");
  const snapshots = await getSnapshots();
  renderList(snapshots, id);
  await loadSnapshot(id);
})();