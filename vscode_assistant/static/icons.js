// Line icons for the whole app (16×16, drawn with the current text colour), instead of emoji and symbol characters.
// icon("play") returns an <svg>; <i data-icon="play"></i> in the page is replaced on load.
(function () {
  "use strict";
  const P = {
    plus: '<path d="M8 3v10M3 8h10"/>',
    close: '<path d="M4 4l8 8M12 4l-8 8"/>',
    check: '<path d="M3 8.5l3.2 3L13 4.5"/>',
    cross: '<path d="M4.5 4.5l7 7M11.5 4.5l-7 7"/>',
    circle: '<circle cx="8" cy="8" r="4.5"/>',
    dot: '<circle cx="8" cy="8" r="3" fill="currentColor" stroke="none"/>',
    "chevron-right": '<path d="M6 3.5L10.5 8 6 12.5"/>',
    "chevron-down": '<path d="M3.5 6L8 10.5 12.5 6"/>',
    "chevron-up": '<path d="M3.5 10L8 5.5 12.5 10"/>',
    "chevron-left": '<path d="M10 3.5L5.5 8 10 12.5"/>',
    edit: '<path d="M10.5 2.5l3 3-7.5 7.5H3v-3z"/>',
    tool: '<path d="M10.2 2.3a3.2 3.2 0 0 0-3.9 4.1L2.5 10.2a1.4 1.4 0 0 0 2 2l3.8-3.8a3.2 3.2 0 0 0 4.1-3.9l-2 2-1.8-.4-.4-1.8z"/>',
    spark: '<path d="M8 1.8l1.4 3.6 3.6 1.4-3.6 1.4L8 11.8 6.6 8.2 3 6.8l3.6-1.4z M12.6 10.8l.6 1.5 1.5.6-1.5.6-.6 1.5-.6-1.5-1.5-.6 1.5-.6z"/>',
    bolt: '<path d="M9 1.5L3.5 9H8l-1 5.5L12.5 7H8z"/>',
    refresh: '<path d="M13 8a5 5 0 1 1-1.5-3.5M13 2.5v3h-3"/>',
    undo: '<path d="M5.5 3.5L2.5 6.5l3 3M2.5 6.5H10a3.5 3.5 0 0 1 0 7H7"/>',
    play: '<path d="M5 3.2v9.6L12.5 8z"/>',
    stop: '<rect x="4" y="4" width="8" height="8" rx="1.2"/>',
    clear: '<path d="M6 3.5h7.5v9H6L2.5 8z M8 6.5l3 3M11 6.5l-3 3"/>',
    folder: '<path d="M1.8 4h4.5l1.5 1.6h6.4v7.4H1.8z"/>',
    "folder-plus": '<path d="M1.8 4h4.5l1.5 1.6h6.4v7.4H1.8z M8 7.3v4M6 9.3h4"/>',
    file: '<path d="M4 1.8h5l3 3v9.4H4z M9 1.8v3h3"/>',
    "file-plus": '<path d="M4 1.8h5l3 3v9.4H4z M9 1.8v3h3 M8 7.5v4.5M5.8 9.7h4.4"/>',
    image: '<rect x="2" y="3" width="12" height="10" rx="1.5"/><path d="M2.5 11.5l3.5-3.5 3 3 1.8-1.8 2.7 2.7"/><circle cx="10.5" cy="6" r="1"/>',
    paperclip: '<path d="M12.5 7.5l-5 5a3 3 0 0 1-4.2-4.2l5.6-5.6a2 2 0 0 1 2.8 2.8L6.2 11a1 1 0 0 1-1.4-1.4l4.6-4.6"/>',
    book: '<path d="M3 1.5h8v11H3z M5 4.5h4 M5 7h4 M13 4v10.5H5"/>',
    code: '<path d="M5.5 4.5L2 8l3.5 3.5M10.5 4.5L14 8l-3.5 3.5"/>',
    sidebar: '<rect x="1.8" y="2.5" width="12.4" height="11" rx="1.5"/><path d="M6 2.5v11"/>',
    branch: '<path d="M5 2.5v11M5 10.5c0-3 6-2 6-5.5M11 2.5v3"/><circle cx="5" cy="2.5" r="1.3"/><circle cx="5" cy="13.5" r="1.3"/><circle cx="11" cy="4" r="1.3"/>',
    ruler: '<path d="M2 11.5L11.5 2l2.5 2.5L4.5 14z M5 9.5l1.2 1.2M7 7.5l1.6 1.6M9 5.5l1.2 1.2"/>',
    trash: '<path d="M3 4.5h10M6.5 4.5v-2h3v2M4.5 4.5l.7 9h5.6l.7-9"/>',
    copy: '<path d="M5.5 5.5h7v8h-7z M3.5 10.5v-8h7"/>',
    external: '<path d="M9 2.5h4.5V7M13.5 2.5L7.5 8.5M11.5 9.5v4H2.5v-9h4"/>',
    link: '<path d="M6.8 9.2l2.4-2.4M7.5 4.6l1.2-1.2a2.6 2.6 0 0 1 3.7 3.7l-1.2 1.2M8.5 11.4l-1.2 1.2a2.6 2.6 0 0 1-3.7-3.7l1.2-1.2"/>',
  };
  window.icon = function (name, cls) {
    return '<svg class="i' + (cls ? " " + cls : "") + '" viewBox="0 0 16 16" aria-hidden="true">' + (P[name] || "") + "</svg>";
  };
  window.fillIcons = function (root) {
    (root || document).querySelectorAll("i[data-icon]").forEach(el => { el.outerHTML = window.icon(el.dataset.icon, el.className); });
  };
  window.fillIcons();   // loaded at the end of <body>, so the page above is already there
})();
