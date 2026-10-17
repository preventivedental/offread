const STORAGE_KEY = "offread:snapshots";
const INTERNAL_PREFIX = browser.runtime.getURL("");

function isInternalUrl(url) {
  return typeof url === "string" && url.startsWith(INTERNAL_PREFIX);
}

function snapshotId() {
  return `snap_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
}

async function loadSnapshots() {
  const result = await browser.storage.local.get(STORAGE_KEY);
  return Array.isArray(result[STORAGE_KEY]) ? result[STORAGE_KEY] : [];
}

async function saveSnapshots(snapshots) {
  await browser.storage.local.set({ [STORAGE_KEY]: snapshots });
}

async function storeSnapshot(snapshot) {
  const snapshots = await loadSnapshots();
  const entry = {
    id: snapshotId(),
    url: snapshot.url,
    title: snapshot.title,
    savedAt: new Date().toISOString(),
    html: snapshot.html,
  };
  const next = [entry, ...snapshots.filter((item) => item.url !== entry.url)].slice(0, 100);
  await saveSnapshots(next);
  return entry.id;
}

async function fetchSnapshot(url) {
  const response = await fetch(url, { credentials: "omit", redirect: "follow" });
  const html = await response.text();
  return storeSnapshot({
    url: response.url || url,
    title: response.url || url,
    html,
  });
}

browser.webRequest.onBeforeRequest.addListener(
  async (details) => {
    if (details.type !== "main_frame" || !/^https?:/i.test(details.url) || isInternalUrl(details.url)) {
      return undefined;
    }
    try {
      const id = await fetchSnapshot(details.url);
      return { redirectUrl: browser.runtime.getURL(`viewer.html?id=${encodeURIComponent(id)}`) };
    } catch (error) {
      console.error("offread fetch failed", error);
      return { redirectUrl: browser.runtime.getURL("viewer.html") };
    }
  },
  { urls: ["<all_urls>"], types: ["main_frame"] },
  ["blocking"]
);

browser.browserAction.onClicked.addListener(() => {
  browser.tabs.create({ url: browser.runtime.getURL("viewer.html") });
});

browser.runtime.onMessage.addListener((message) => {
  if (!message || typeof message.type !== "string") {
    return undefined;
  }
  if (message.type === "offread:list") {
    return loadSnapshots();
  }
  if (message.type === "offread:get" && typeof message.id === "string") {
    return loadSnapshots().then((snapshots) => snapshots.find((item) => item.id === message.id) || null);
  }
  if (message.type === "offread:store" && message.snapshot && typeof message.snapshot === "object") {
    return storeSnapshot(message.snapshot);
  }
  return undefined;
});