// Runtime base-URL detection so the site works both as:
//   (a) raw files served at e.g. /cod-kmap/web/  → data lives under ./public/
//   (b) a Vite-built artifact at /cod-kmap/      → Vite flattens public/ into root, data lives at ./
//
// Using document.baseURI (= the URL of index.html) plus a heuristic on
// location.pathname. Exposed as a URL string ending in "/".

const servedFromWebDir = /\/web\/(?:index\.html)?$/.test(location.pathname)
  || location.pathname.endsWith('/web/');

export const DATA_BASE = new URL(
  servedFromWebDir ? './public/' : './',
  document.baseURI,
).href;
