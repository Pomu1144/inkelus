monaco-editor 0.52.2 (min build). The TypeScript/JavaScript language service and translations are left out;
vs/language/typescript/tsMode.js is a small stand-in so Monaco does not try to load it. TypeScript help
comes from typescript-language-server instead (see tsls.py).
