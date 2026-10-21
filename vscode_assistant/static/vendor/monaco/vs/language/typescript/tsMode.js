// Stand-in for Monaco's own TypeScript support, which isn't shipped: the app's TypeScript help comes from
// typescript-language-server with the project's own TypeScript (see static/javaide.js and tsls.py).
define("vs/language/typescript/tsMode", ["exports"], function (exports) {
  "use strict";
  exports.setupTypeScript = function () { return function () { return Promise.reject(new Error("not available")); }; };
  exports.setupJavaScript = exports.setupTypeScript;
});
