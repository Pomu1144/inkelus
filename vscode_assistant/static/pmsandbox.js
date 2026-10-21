// Runs Postman-style pre-request and test scripts with a pm.* API that matches Postman's for everyday use:
// pm.environment / collectionVariables / globals / variables / iterationData, pm.request, pm.response,
// pm.test, pm.expect (a chai subset), pm.info, pm.setNextRequest, plus the old postman.* and tests[] forms.
//
// It runs in a Web Worker in the app window (no access to the page, cookies or your files), or under
// Node for command-line runs:  node pmsandbox.js < context.json
// Input: {script, scopes: {globals, collection, environment, data, local}, request, response?, info}
// Output: {scopes, request, tests: [{name, passed, error}], console: [..], error, nextRequest, skip}
(function () {
  "use strict";

  function typeOf(v) {
    if (v === null) return "null";
    if (Array.isArray(v)) return "array";
    if (v instanceof RegExp) return "regexp";
    if (v instanceof Date) return "date";
    return typeof v;
  }
  function show(v) {
    try { return typeof v === "string" ? JSON.stringify(v) : JSON.stringify(v) === undefined ? String(v) : JSON.stringify(v); }
    catch (_) { return String(v); }
  }
  function deepEqual(a, b) {
    if (a === b) return true;
    if (typeof a !== typeof b || a === null || b === null || typeof a !== "object") return Number.isNaN(a) && Number.isNaN(b);
    if (Array.isArray(a) !== Array.isArray(b)) return false;
    const ka = Object.keys(a), kb = Object.keys(b);
    if (ka.length !== kb.length) return false;
    return ka.every(k => deepEqual(a[k], b[k]));
  }

  // ── expect: the chai BDD style Postman scripts use ─────────────────────
  function Assertion(obj, flags) {
    this._obj = obj;
    this._flags = flags || {};
  }
  const words = ["to", "be", "been", "is", "that", "which", "and", "has", "have", "with", "at", "of", "same", "but", "does", "still"];
  words.forEach(w => Object.defineProperty(Assertion.prototype, w, { get() { return this; } }));
  Object.defineProperty(Assertion.prototype, "not", { get() { this._flags.not = !this._flags.not; return this; } });
  Object.defineProperty(Assertion.prototype, "deep", { get() { this._flags.deep = true; return this; } });
  Object.defineProperty(Assertion.prototype, "nested", { get() { this._flags.nested = true; return this; } });
  Object.defineProperty(Assertion.prototype, "own", { get() { return this; } });
  Object.defineProperty(Assertion.prototype, "any", { get() { this._flags.any = true; return this; } });
  Object.defineProperty(Assertion.prototype, "all", { get() { this._flags.all = true; return this; } });
  Assertion.prototype._assert = function (ok, msg, negMsg) {
    if (this._flags.not ? ok : !ok) throw new Error(this._flags.not ? negMsg : msg);
    return this;
  };
  const getters = {
    ok() { return this._assert(!!this._obj, "expected " + show(this._obj) + " to be truthy", "expected " + show(this._obj) + " to be falsy"); },
    true() { return this._assert(this._obj === true, "expected " + show(this._obj) + " to be true", "expected value not to be true"); },
    false() { return this._assert(this._obj === false, "expected " + show(this._obj) + " to be false", "expected value not to be false"); },
    null() { return this._assert(this._obj === null, "expected " + show(this._obj) + " to be null", "expected value not to be null"); },
    undefined() { return this._assert(this._obj === undefined, "expected " + show(this._obj) + " to be undefined", "expected value not to be undefined"); },
    NaN() { return this._assert(Number.isNaN(this._obj), "expected " + show(this._obj) + " to be NaN", "expected value not to be NaN"); },
    exist() { return this._assert(this._obj !== null && this._obj !== undefined, "expected value to exist", "expected " + show(this._obj) + " not to exist"); },
    empty() {
      const o = this._obj;
      const empty = o == null ? true : typeof o === "string" || Array.isArray(o) ? o.length === 0 : typeof o === "object" ? Object.keys(o).length === 0 : false;
      return this._assert(empty, "expected " + show(o) + " to be empty", "expected " + show(o) + " not to be empty");
    },
  };
  Object.keys(getters).forEach(k => Object.defineProperty(Assertion.prototype, k, { get: getters[k], configurable: true }));
  const A = Assertion.prototype;
  A.a = A.an = function (type) {
    const t = typeOf(this._obj);
    return this._assert(t === String(type).toLowerCase(), "expected " + show(this._obj) + " to be a " + type + " (it's a " + t + ")", "expected " + show(this._obj) + " not to be a " + type);
  };
  A.equal = A.equals = A.eq = function (v) {
    const ok = this._flags.deep ? deepEqual(this._obj, v) : this._obj === v;
    return this._assert(ok, "expected " + show(this._obj) + " to equal " + show(v), "expected " + show(this._obj) + " not to equal " + show(v));
  };
  A.eql = function (v) { return this._assert(deepEqual(this._obj, v), "expected " + show(this._obj) + " to deeply equal " + show(v), "expected " + show(this._obj) + " not to deeply equal " + show(v)); };
  A.include = A.includes = A.contain = A.contains = function (v) {
    const o = this._obj;
    let ok;
    if (typeof o === "string") ok = o.indexOf(v) >= 0;
    else if (Array.isArray(o)) ok = o.some(x => this._flags.deep ? deepEqual(x, v) : x === v);
    else if (o && typeof o === "object" && v && typeof v === "object") ok = Object.keys(v).every(k => deepEqual(o[k], v[k]));
    else ok = false;
    return this._assert(ok, "expected " + show(o) + " to include " + show(v), "expected " + show(o) + " not to include " + show(v));
  };
  A.property = function (name, val) {
    const o = this._obj;
    let has, got;
    if (this._flags.nested) {
      got = String(name).split(/\.|\[(\d+)\]/).filter(Boolean).reduce((c, k) => (c == null ? undefined : c[k]), o);
      has = got !== undefined;
    } else { has = o != null && Object.prototype.hasOwnProperty.call(Object(o), name); got = has ? o[name] : undefined; }
    if (arguments.length > 1) {
      const ok = has && (this._flags.deep ? deepEqual(got, val) : got === val);
      this._assert(ok, "expected " + show(o) + " to have property " + show(name) + " of " + show(val) + (has ? ", but got " + show(got) : ""), "expected property " + show(name) + " not to be " + show(val));
    } else this._assert(has, "expected " + show(o) + " to have property " + show(name), "expected " + show(o) + " not to have property " + show(name));
    return new Assertion(got, { not: false });
  };
  A.keys = A.key = function () {
    const want = Array.isArray(arguments[0]) ? arguments[0] : Array.from(arguments);
    const have = Object.keys(this._obj || {});
    const ok = this._flags.any ? want.some(k => have.includes(k)) : want.every(k => have.includes(k)) && (this._flags.all ? have.length === want.length : true);
    return this._assert(ok, "expected " + show(have) + " to have keys " + show(want), "expected keys " + show(want) + " to be absent");
  };
  A.length = A.lengthOf = function (n) {
    const len = this._obj == null ? undefined : this._obj.length !== undefined ? this._obj.length : Object.keys(this._obj).length;
    return this._assert(len === n, "expected length " + len + " to be " + n, "expected length not to be " + n);
  };
  const cmp = (name, fn, word) => { A[name] = function (n) { return this._assert(fn(this._obj, n), "expected " + show(this._obj) + " to be " + word + " " + n, "expected " + show(this._obj) + " not to be " + word + " " + n); }; };
  cmp("above", (a, b) => a > b, "above"); cmp("gt", (a, b) => a > b, "above"); cmp("greaterThan", (a, b) => a > b, "above");
  cmp("below", (a, b) => a < b, "below"); cmp("lt", (a, b) => a < b, "below"); cmp("lessThan", (a, b) => a < b, "below");
  cmp("least", (a, b) => a >= b, "at least"); cmp("gte", (a, b) => a >= b, "at least"); cmp("most", (a, b) => a <= b, "at most"); cmp("lte", (a, b) => a <= b, "at most");
  A.within = function (lo, hi) { return this._assert(this._obj >= lo && this._obj <= hi, "expected " + show(this._obj) + " to be within " + lo + ".." + hi, "expected value outside " + lo + ".." + hi); };
  A.match = A.matches = function (re) { return this._assert(re.test(String(this._obj)), "expected " + show(this._obj) + " to match " + re, "expected " + show(this._obj) + " not to match " + re); };
  A.oneOf = function (list) { return this._assert(list.some(x => this._flags.deep ? deepEqual(x, this._obj) : x === this._obj), "expected " + show(this._obj) + " to be one of " + show(list), "expected " + show(this._obj) + " not to be one of " + show(list)); };
  A.instanceOf = A.instanceof = function (C) { return this._assert(this._obj instanceof C, "expected value to be an instance of " + (C && C.name), "expected value not to be an instance of " + (C && C.name)); };
  A.string = function (s) { return this.include(s); };
  A.members = function (list) {
    const o = this._obj || [];
    const ok = list.length === o.length && list.every(x => o.some(y => deepEqual(x, y)));
    return this._assert(ok, "expected " + show(o) + " to have the same members as " + show(list), "expected different members");
  };
  A.satisfy = function (fn) { return this._assert(!!fn(this._obj), "expected " + show(this._obj) + " to satisfy the function", "expected value not to satisfy the function"); };
  A.throw = function () {
    let threw = false;
    try { this._obj(); } catch (_) { threw = true; }
    return this._assert(threw, "expected the function to throw", "expected the function not to throw");
  };
  // pm.response.to.have.status(200) style, when the object is the response
  A.status = function (code) {
    const r = this._obj;
    const ok = typeof code === "number" ? r.code === code : String(r.status).toLowerCase() === String(code).toLowerCase();
    return this._assert(ok, "expected response to have status " + code + " but got " + r.code, "expected response not to have status " + code);
  };
  A.header = function (name, val) {
    const r = this._obj;
    const got = r.headers.get(name);
    if (arguments.length > 1) return this._assert(got === val, "expected header " + name + " to be " + show(val) + " but got " + show(got), "expected header " + name + " not to be " + show(val));
    return this._assert(got !== undefined, "expected response to have header " + name, "expected response not to have header " + name);
  };
  A.body = function (val) {
    const r = this._obj;
    if (arguments.length === 0) return this._assert(r.text().length > 0, "expected response to have a body", "expected response to have no body");
    const ok = typeof val === "string" ? r.text() === val : deepEqual(r.json(), val);
    return this._assert(ok, "expected response body to be " + show(val), "expected response body not to be " + show(val));
  };
  A.jsonBody = function (path, val) {
    const r = this._obj;
    let j;
    try { j = r.json(); } catch (_) { return this._assert(false, "expected the response to be JSON", "expected the response not to be JSON"); }
    if (arguments.length === 0) return this;
    const got = String(path).split(/\.|\[(\d+)\]/).filter(Boolean).reduce((c, k) => (c == null ? undefined : c[k]), j);
    if (arguments.length === 1) return this._assert(got !== undefined, "expected JSON body to have " + path, "expected JSON body not to have " + path);
    return this._assert(deepEqual(got, val), "expected JSON " + path + " to be " + show(val) + " but got " + show(got), "expected JSON " + path + " not to be " + show(val));
  };
  A.responseTime = function () { return new Assertion(this._obj.responseTime, this._flags); };
  ["success", "clientError", "serverError", "error", "info", "redirection"].forEach(name => {
    const range = { info: 1, success: 2, redirection: 3, clientError: 4, serverError: 5 }[name];
    Object.defineProperty(A, name, { get() {
      const c = this._obj.code, cls = Math.floor(c / 100);
      const ok = name === "error" ? cls === 4 || cls === 5 : cls === range;
      return this._assert(ok, "expected response to be " + name + " but got " + c, "expected response not to be " + name);
    } });
  });
  ["withBody", "json"].forEach(name => Object.defineProperty(A, name, { get() {
    if (name === "json") { try { this._obj.json(); } catch (_) { return this._assert(false, "expected the response to be JSON", "expected the response not to be JSON"); } }
    return this;
  } }));
  // "ok" on a response means a 2xx status, like Postman
  const okGetter = Object.getOwnPropertyDescriptor(A, "ok").get;
  Object.defineProperty(A, "ok", { get() {
    if (this._obj && this._obj.__isResponse) return this._assert(Math.floor(this._obj.code / 100) === 2, "expected response to be OK (2xx) but got " + this._obj.code, "expected response not to be OK");
    return okGetter.call(this);
  } });

  function expect(v, msg) { const a = new Assertion(v); a._msg = msg; return a; }

  // ── variables ─────────────────────────────────────────────────────────
  function VarScope(obj, name) {
    this._v = obj;
    this._name = name;
  }
  VarScope.prototype.get = function (k) { return this._v[k]; };
  VarScope.prototype.set = function (k, v) { this._v[k] = v === undefined || v === null ? v : typeof v === "string" ? v : typeof v === "object" ? JSON.stringify(v) : String(v); };
  VarScope.prototype.unset = function (k) { delete this._v[k]; };
  VarScope.prototype.has = function (k) { return Object.prototype.hasOwnProperty.call(this._v, k); };
  VarScope.prototype.clear = function () { Object.keys(this._v).forEach(k => delete this._v[k]); };
  VarScope.prototype.toObject = function () { return Object.assign({}, this._v); };
  VarScope.prototype.replaceIn = function (s) { return replaceIn(s, this._v); };

  function replaceIn(s, vars) {
    return String(s == null ? "" : s).replace(/\{\{\s*([\w.$-]+)\s*\}\}/g, (m, k) => (vars[k] !== undefined ? vars[k] : m));
  }

  // ── headers: a small version of Postman's HeaderList ──────────────────
  function HeaderList(list) { this._l = list; }
  HeaderList.prototype.get = function (k) { const h = this._l.find(x => x.key.toLowerCase() === String(k).toLowerCase() && x.enabled !== false); return h ? h.value : undefined; };
  HeaderList.prototype.has = function (k) { return this.get(k) !== undefined; };
  HeaderList.prototype.add = function (h) { if (typeof h === "string") { const i = h.indexOf(":"); h = { key: h.slice(0, i).trim(), value: h.slice(i + 1).trim() }; } this._l.push({ key: h.key, value: String(h.value), enabled: true }); };
  HeaderList.prototype.upsert = function (h) { const e = this._l.find(x => x.key.toLowerCase() === h.key.toLowerCase()); if (e) { e.value = String(h.value); e.enabled = true; } else this.add(h); };
  HeaderList.prototype.remove = function (k) { const lk = String(k).toLowerCase(); for (let i = this._l.length - 1; i >= 0; i--) if (this._l[i].key.toLowerCase() === lk) this._l.splice(i, 1); };
  HeaderList.prototype.toObject = function () { const o = {}; this._l.forEach(h => { if (h.enabled !== false) o[h.key] = h.value; }); return o; };
  HeaderList.prototype.each = function (fn) { this._l.forEach(h => fn({ key: h.key, value: h.value })); };
  HeaderList.prototype.all = function () { return this._l.map(h => ({ key: h.key, value: h.value })); };
  HeaderList.prototype.count = function () { return this._l.length; };

  // ── one script run ────────────────────────────────────────────────────
  async function run(ctx) {
    const out = { tests: [], console: [], error: null, nextRequest: undefined, skip: false };
    const sc = ctx.scopes || {};
    const scopes = { globals: Object.assign({}, sc.globals), collection: Object.assign({}, sc.collection),
      environment: Object.assign({}, sc.environment), data: Object.assign({}, sc.data), local: Object.assign({}, sc.local) };
    const req = ctx.request || {};
    req.headers = req.headers || [];
    const merged = () => Object.assign({}, scopes.globals, scopes.collection, scopes.environment, scopes.data, scopes.local);
    const pending = [];

    const variables = {
      get: k => merged()[k], set: (k, v) => { scopes.local[k] = typeof v === "string" ? v : JSON.stringify(v); },
      has: k => merged()[k] !== undefined, unset: k => { delete scopes.local[k]; },
      toObject: () => merged(), replaceIn: s => replaceIn(s, merged()),
    };
    const urlObj = {
      toString: () => req.url,
      getHost: () => { try { return new URL(replaceIn(req.url, merged())).hostname; } catch (_) { return ""; } },
      getPath: () => { try { return new URL(replaceIn(req.url, merged())).pathname; } catch (_) { return ""; } },
      query: { add: q => { req.params = req.params || []; req.params.push({ key: q.key, value: String(q.value), enabled: true }); },
               upsert: q => { req.params = req.params || []; const e = req.params.find(p => p.key === q.key); if (e) e.value = String(q.value); else req.params.push({ key: q.key, value: String(q.value), enabled: true }); },
               remove: k => { req.params = (req.params || []).filter(p => p.key !== k); },
               get: k => ((req.params || []).find(p => p.key === k) || {}).value,
               toObject: () => Object.fromEntries((req.params || []).filter(p => p.enabled !== false).map(p => [p.key, p.value])) },
    };
    const pmRequest = {
      get url() { return urlObj; }, set url(v) { req.url = String(v); },
      get method() { return req.method; }, set method(v) { req.method = String(v).toUpperCase(); },
      headers: new HeaderList(req.headers),
      addHeader: h => new HeaderList(req.headers).add(h),
      removeHeader: k => new HeaderList(req.headers).remove(k),
      get body() {
        const b = req.body || {};
        return { mode: b.mode === "json" || b.mode === "text" ? "raw" : b.mode, raw: b.text || "", toString: () => b.text || "",
          update: v => { req.body = Object.assign({}, b, { text: typeof v === "string" ? v : JSON.stringify(v), mode: b.mode === "none" || !b.mode ? "json" : b.mode }); } };
      },
      set body(v) { req.body = Object.assign({}, req.body, { text: typeof v === "string" ? v : (v && v.raw) || JSON.stringify(v) }); },
      get name() { return (ctx.info || {}).requestName; },
    };
    let pmResponse;
    if (ctx.response) {
      const r = ctx.response;
      const hl = new HeaderList((r.headers || []).map(([k, v]) => ({ key: k, value: v })));
      let parsed;
      pmResponse = {
        __isResponse: true, code: r.status, status: r.reason || "", responseTime: r.ms, responseSize: r.size,
        headers: hl, text: () => r.body || "",
        json: () => { if (parsed === undefined) parsed = JSON.parse(r.body); return parsed; },
        cookies: { get: n => ((r.cookies || []).find(c => c.name === n) || {}).value, has: n => (r.cookies || []).some(c => c.name === n), toObject: () => Object.fromEntries((r.cookies || []).map(c => [c.name, c.value])) },
      };
      Object.defineProperty(pmResponse, "to", { get: () => expect(pmResponse) });   // fresh each time, so .not doesn't stick
    }
    const log = level => function () { out.console.push({ level, text: Array.from(arguments).map(a => typeof a === "string" ? a : show(a)).join(" ") }); };
    const sandboxConsole = { log: log("log"), info: log("info"), warn: log("warn"), error: log("error"), debug: log("debug") };
    const globals = new VarScope(scopes.globals, "globals");
    const pm = {
      // with no environment selected, pm.environment reads and writes globals so chained values aren't lost
      environment: ctx.noEnvironment ? globals : new VarScope(scopes.environment, "environment"),
      collectionVariables: new VarScope(scopes.collection, "collection"),
      globals,
      iterationData: Object.assign(new VarScope(scopes.data, "data"), { set() { throw new Error("iterationData is read-only"); } }),
      variables,
      request: pmRequest,
      response: pmResponse,
      info: Object.assign({ eventName: ctx.response ? "test" : "prerequest", iteration: 0, iterationCount: 1, requestName: "", requestId: "" }, ctx.info || {}),
      cookies: pmResponse ? pmResponse.cookies : { get: () => undefined, has: () => false, toObject: () => ({}) },
      test(name, fn) {
        const record = (passed, err) => out.tests.push({ name: String(name), passed, error: err ? String(err.message || err) : "" });
        try {
          const r = fn && fn();
          if (r && typeof r.then === "function") pending.push(r.then(() => record(true), e => record(false, e)));
          else record(true);
        } catch (e) { record(false, e); }
      },
      expect,
      setNextRequest(name) { out.nextRequest = name === null ? null : String(name); },
      execution: { setNextRequest(name) { out.nextRequest = name === null ? null : String(name); }, skipRequest() { out.skip = true; } },
      sendRequest(_r, cb) {
        const e = new Error("pm.sendRequest isn't supported here yet; put that call in its own request before this one.");
        out.console.push({ level: "warn", text: e.message });
        if (cb) cb(e);
        return Promise.reject(e);
      },
      visualizer: { set() { out.console.push({ level: "info", text: "pm.visualizer isn't shown in this app." }); } },
    };
    pm.test.skip = name => out.tests.push({ name: String(name), passed: true, skipped: true, error: "" });
    // the older postman.* / tests[] style
    const tests = {};
    const postman = {
      setEnvironmentVariable: (k, v) => pm.environment.set(k, v), getEnvironmentVariable: k => pm.environment.get(k),
      clearEnvironmentVariable: k => pm.environment.unset(k), setGlobalVariable: (k, v) => pm.globals.set(k, v),
      getGlobalVariable: k => pm.globals.get(k), clearGlobalVariable: k => pm.globals.unset(k), setNextRequest: n => pm.setNextRequest(n),
      getResponseHeader: k => pmResponse && pmResponse.headers.get(k),
    };
    const legacy = ctx.response ? {
      responseBody: ctx.response.body || "", responseCode: { code: ctx.response.status, name: ctx.response.reason || "" },
      responseTime: ctx.response.ms, responseHeaders: Object.fromEntries((ctx.response.headers || []).map(h => [h[0], h[1]])),
    } : {};
    const env = Object.assign({ pm, postman, tests, console: sandboxConsole, environment: ctx.noEnvironment ? scopes.globals : scopes.environment, globals: scopes.globals,
      data: scopes.data, request: { url: req.url, method: req.method, headers: new HeaderList(req.headers).toObject(), data: (req.body || {}).text || "" },
      require: name => { throw new Error("require('" + name + "') isn't available in this app's scripts."); },
      btoa: s => (typeof btoa === "function" ? btoa(s) : Buffer.from(String(s), "binary").toString("base64")),
      atob: s => (typeof atob === "function" ? atob(s) : Buffer.from(String(s), "base64").toString("binary")) }, legacy);
    try {
      await exec(String(ctx.script || ""), env);
      await Promise.all(pending);
    } catch (e) {
      out.error = (e && e.message) || String(e);
    }
    Object.keys(tests).forEach(k => out.tests.push({ name: k, passed: !!tests[k], error: tests[k] ? "" : "tests[\"" + k + "\"] was false" }));
    out.scopes = scopes;
    out.request = req;
    return out;
  }

  // run the user's code with only the sandbox's names in scope
  let exec = async function (code, env) {
    const names = Object.keys(env);
    const fn = new Function(...names, '"use strict";\nreturn (async () => {\n' + code + "\n})();");
    return fn(...names.map(n => env[n]));
  };

  // Web Worker: one message in, one message out
  if (typeof self !== "undefined" && typeof self.postMessage === "function" && typeof window === "undefined") {
    self.onmessage = async e => {
      const res = await run(e.data);
      self.postMessage(JSON.parse(JSON.stringify(res)));
    };
  }
  // Node: JSON on stdin, JSON on stdout
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { run };
    if (typeof require !== "undefined" && require.main === module) {
      const vm = require("vm");
      exec = async function (code, env) {
        const ctxObj = vm.createContext(Object.assign({ JSON, Math, Date, Object, Array, String, Number, Boolean, RegExp, Promise, Error, parseInt, parseFloat, isNaN, encodeURIComponent, decodeURIComponent }, env));
        return vm.runInContext('(async () => {\n"use strict";\n' + code + "\n})()", ctxObj, { timeout: 5000 });
      };
      let input = "";
      process.stdin.on("data", d => (input += d));
      process.stdin.on("end", async () => {
        const res = await run(JSON.parse(input));
        process.stdout.write(JSON.stringify(res));
      });
    }
  }
})();
