// The password vault. Everything secret stays in this page:
// - the key is derived from the master password with PBKDF2-SHA256 (600,000 rounds, random salt)
//   and kept as a non-extractable Web Crypto key;
// - the list of logins is encrypted with AES-256-GCM (fresh random IV on every save);
// - only the encrypted blob is sent to the local server, which writes it to vault.json.
// The master password itself is never stored or sent anywhere.
(function () {
  "use strict";
  const ITERATIONS = 600000;
  const AUTO_LOCK_MS = 15 * 60 * 1000;
  const enc = new TextEncoder(), dec = new TextDecoder();
  const b64 = buf => btoa(String.fromCharCode(...new Uint8Array(buf)));
  const unb64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));

  let api = null;            // (method, path, body) => Promise, set by init()
  let blob = null;           // the encrypted vault as stored, or null if none exists yet
  let key = null;            // CryptoKey while unlocked
  let salt = null, iterations = ITERATIONS;
  let items = null;          // [{id, site, username, password, updated}] while unlocked
  let timer = null;
  const listeners = [];

  async function deriveKey(password, saltBytes, iters) {
    const base = await crypto.subtle.importKey("raw", enc.encode(password), "PBKDF2", false, ["deriveKey"]);
    return crypto.subtle.deriveKey({ name: "PBKDF2", hash: "SHA-256", salt: saltBytes, iterations: iters },
      base, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
  }

  async function seal() {
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const data = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key,
      enc.encode(JSON.stringify({ items })));
    blob = { iterations, salt: b64(salt), iv: b64(iv), data: b64(data) };
    await api("PUT", "vault", { vault: blob });
  }

  function touch() {
    clearTimeout(timer);
    timer = setTimeout(lock, AUTO_LOCK_MS);
  }

  function changed() { listeners.forEach(fn => { try { fn(); } catch (_) { /* ignore */ } }); }

  function lock() {
    clearTimeout(timer);
    key = null;
    items = null;
    changed();
  }

  // Normalise "https://www.GitHub.com/login" or "github.com" to a bare host name.
  function siteOf(s) {
    s = String(s || "").trim().toLowerCase();
    if (!s) return "";
    try { return new URL(s.includes("://") ? s : "https://" + s).hostname.replace(/^www\./, ""); }
    catch (_) { return ""; }
  }

  // Same host, or a subdomain of a real domain name; IP addresses must match exactly and a bare
  // suffix like "com" never matches. (The server checks this again before filling anything.)
  function matchesHost(site, host) {
    site = String(site || "").toLowerCase().replace(/^\.+|\.+$/g, "");
    host = String(host || "").toLowerCase();
    if (!site || !host) return false;
    if (host === site) return true;
    const isIp = /^[\d.]+$/.test(host) || host.includes(":");
    return !isIp && site.includes(".") && !/^[\d.]+$/.test(site) && host.endsWith("." + site);
  }

  window.Vault = {
    siteOf, matchesHost,
    onChange(fn) { listeners.push(fn); },
    async init(apiFn) {
      api = apiFn;
      const r = await api("GET", "vault");
      blob = r.exists ? r.vault : null;
      changed();
    },
    get exists() { return !!blob; },
    get unlocked() { return !!key; },
    async create(password) {
      if (String(password).length < 10) throw new Error("Use at least 10 characters.");
      salt = crypto.getRandomValues(new Uint8Array(16));
      iterations = ITERATIONS;
      key = await deriveKey(password, salt, iterations);
      items = [];
      await seal();
      touch();
      changed();
    },
    async unlock(password) {
      if (!blob) throw new Error("There's no vault yet.");
      salt = unb64(blob.salt);
      iterations = blob.iterations || ITERATIONS;
      const k = await deriveKey(password, salt, iterations);
      let plain;
      try {
        plain = await crypto.subtle.decrypt({ name: "AES-GCM", iv: unb64(blob.iv) }, k, unb64(blob.data));
      } catch (_) {
        throw new Error("Wrong master password.");
      }
      key = k;
      items = JSON.parse(dec.decode(plain)).items || [];
      touch();
      changed();
    },
    lock,
    list() {
      if (!key) return [];
      touch();
      return items.slice().sort((a, b) => a.site.localeCompare(b.site) || a.username.localeCompare(b.username));
    },
    forHost(host) {
      if (!key) return [];
      touch();
      return items.filter(it => matchesHost(it.site, host));
    },
    async save(entry) {
      if (!key) throw new Error("Unlock the vault first.");
      const site = siteOf(entry.site);
      if (!site) throw new Error("Enter the website, like github.com.");
      if (!entry.password) throw new Error("Enter the password.");
      const it = { id: entry.id || b64(crypto.getRandomValues(new Uint8Array(9))), site,
                   username: String(entry.username || ""), password: String(entry.password), updated: Date.now() };
      const i = items.findIndex(x => x.id === it.id);
      if (i >= 0) items[i] = it; else items.push(it);
      await seal();
      touch();
      changed();
      return it;
    },
    async remove(id) {
      if (!key) throw new Error("Unlock the vault first.");
      items = items.filter(x => x.id !== id);
      await seal();
      changed();
    },
    async changePassword(password) {
      if (!key) throw new Error("Unlock the vault first.");
      if (String(password).length < 10) throw new Error("Use at least 10 characters.");
      salt = crypto.getRandomValues(new Uint8Array(16));
      iterations = ITERATIONS;
      key = await deriveKey(password, salt, iterations);
      await seal();
      touch();
      changed();
    },
    async destroy() {
      await api("DELETE", "vault");
      blob = null;
      lock();
    },
    generate(length) {
      const chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%^&*-_=+";
      const out = [], buf = new Uint32Array(length || 20);
      crypto.getRandomValues(buf);
      for (const n of buf) out.push(chars[n % chars.length]);
      return out.join("");
    },
  };

  window.addEventListener("pagehide", lock);
})();
