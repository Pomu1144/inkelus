// Monaco's web workers start here (a file of our own, since the page only runs its own scripts).
self.MonacoEnvironment = { baseUrl: "/static/vendor/monaco/" };
importScripts("/static/vendor/monaco/vs/base/worker/workerMain.js");
