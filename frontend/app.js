/*
 * minify — frontend logic.
 *
 * SETUP: after `sam deploy`, paste the API base URL from the stack's
 * `ApiEndpoint` output here, then re-upload the frontend to the S3 bucket:
 *
 *     const API_BASE = "https://abc123.execute-api.us-east-1.amazonaws.com";
 */
const API_BASE = ""; // <-- set this after deploy (no trailing slash)

const form = document.getElementById("shorten-form");
const input = document.getElementById("url-input");
const aliasInput = document.getElementById("alias-input");
const expirySelect = document.getElementById("expiry-select");
const button = document.getElementById("shorten-btn");
const formError = document.getElementById("form-error");
const result = document.getElementById("result");
const shortLinkEl = document.getElementById("short-link");
const targetLine = document.getElementById("target-line");
const copyBtn = document.getElementById("copy-btn");
const historySection = document.getElementById("history-section");
const historyList = document.getElementById("history-list");
const clearHistory = document.getElementById("clear-history");

const HISTORY_KEY = "minify:history";
const HISTORY_MAX = 8;

function apiBase() {
  const stored = localStorage.getItem("minify:apiBase");
  const base = (stored || API_BASE).trim().replace(/\/+$/, "");
  return base || null;
}

function setBusy(busy) {
  button.disabled = busy;
  button.classList.toggle("loading", busy);
}

function showError(message) {
  formError.textContent = message;
  formError.hidden = false;
}

function hideError() {
  formError.hidden = true;
}

function looksLikeAlias(value) {
  return /^[A-Za-z0-9_-]{3,32}$/.test(value);
}

function looksLikeUrl(value) {
  try {
    const u = new URL(value);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch {
    return false;
  }
}

function loadHistory() {
  try {
    return JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
  } catch {
    return [];
  }
}

function saveHistory(entries) {
  localStorage.setItem(HISTORY_KEY, JSON.stringify(entries.slice(0, HISTORY_MAX)));
}

function renderHistory() {
  const entries = loadHistory();
  historySection.hidden = entries.length === 0;
  historyList.innerHTML = "";
  for (const entry of entries) {
    const li = document.createElement("li");

    const link = document.createElement("a");
    link.href = entry.shortUrl;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = entry.shortUrl.replace(/^https?:\/\//, "");

    const dest = document.createElement("span");
    dest.className = "dest";
    dest.title = entry.url;
    dest.textContent = entry.url.replace(/^https?:\/\//, "");

    li.append(link, dest);
    historyList.appendChild(li);
  }
}

function pushHistory(shortUrl, url) {
  const entries = loadHistory().filter((e) => e.shortUrl !== shortUrl);
  entries.unshift({ shortUrl, url });
  saveHistory(entries);
  renderHistory();
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  hideError();
  result.hidden = true;

  const raw = input.value.trim();
  if (!looksLikeUrl(raw)) {
    showError("That doesn't look like a valid http(s) URL.");
    input.focus();
    return;
  }

  const base = apiBase();
  if (!base) {
    showError("API not configured yet — set API_BASE in app.js after deploying.");
    return;
  }

  const alias = aliasInput.value.trim();
  if (alias && !looksLikeAlias(alias)) {
    showError("Alias must be 3–32 characters: letters, numbers, - or _.");
    aliasInput.focus();
    return;
  }

  const expiresIn = expirySelect.value ? Number(expirySelect.value) : undefined;

  setBusy(true);
  try {
    const payload = { url: raw };
    if (alias) payload.alias = alias;
    if (expiresIn) payload.expiresIn = expiresIn;
    const response = await fetch(`${base}/shorten`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.error || `Server returned ${response.status}.`);
    }

    shortLinkEl.href = data.shortUrl;
    shortLinkEl.textContent = data.shortUrl.replace(/^https?:\/\//, "");
    targetLine.textContent = `→ ${raw}`;
    if (data.expiresAt) {
      const label = expirySelect.options[expirySelect.selectedIndex].textContent;
      targetLine.textContent += ` · expires in ${label.toLowerCase()}`;
    }
    result.hidden = false;
    copyBtn.textContent = "Copy";
    copyBtn.classList.remove("copied");
    pushHistory(data.shortUrl, raw);
  } catch (err) {
    showError(err.message || "Something went wrong. Try again.");
  } finally {
    setBusy(false);
  }
});

copyBtn.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(shortLinkEl.href);
    copyBtn.textContent = "Copied";
    copyBtn.classList.add("copied");
    setTimeout(() => {
      copyBtn.textContent = "Copy";
      copyBtn.classList.remove("copied");
    }, 1600);
  } catch {
    // Clipboard API unavailable (non-secure context): select the link instead.
    const range = document.createRange();
    range.selectNode(shortLinkEl);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }
});

clearHistory.addEventListener("click", () => {
  localStorage.removeItem(HISTORY_KEY);
  renderHistory();
});

input.addEventListener("input", hideError);
aliasInput.addEventListener("input", hideError);
renderHistory();
