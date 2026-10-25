// router.js — Hash-based router for the multi-view SPA.
//
// Routes always begin with "#/" (e.g. "#/stats", "#/network"). Anything
// else in `location.hash` — most importantly "#area-xxxx" anchors used
// by the per-research-area TOC inside the Stats view — is an
// **in-page fragment** and must NOT trigger navigation. Without this
// guard, clicking a TOC entry would route to the unknown path
// "area-xxxx", fall through to the default '/' handler, and dump the
// user onto the Map tab.

let _routes = {};
let _currentPath = null;

function isRouteHash(h) {
  return typeof h === 'string' && h.startsWith('#/');
}

function getHash() {
  const h = location.hash || '#/';
  // Only "#/path" is a route. In-page fragments stay on whatever
  // route we're already on.
  if (!isRouteHash(h)) return _currentPath || '/';
  return h.slice(1);
}

function navigate(path) {
  if (path === _currentPath) return;
  _currentPath = path;

  // Top-level segment for tab highlighting + sidebar / handler lookup.
  // 'top' is a reserved global in browser context, so call it 'rootSeg'.
  const rootSeg = '/' + (path.split('/')[1] || '');

  // Update active tab styling. Sub-routes like '/people/<id>' light
  // up the parent tab '/people'. A retired route ('/team', '/scholars')
  // matches no tab for the instant before its handler redirects.
  document.querySelectorAll('.tabs a[data-view]').forEach((a) => {
    a.classList.toggle('active', a.dataset.view === rootSeg);
    // On a phone the tab strip scrolls sideways; keep the active tab in view.
    if (a.dataset.view === rootSeg && a.parentElement
        && a.parentElement.scrollWidth > a.parentElement.clientWidth) {
      a.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }
  });
  updateTabOverflow();

  // Hide/show sidebar for views that don't need it. Keep in step with the
  // `no-sidebar` class each route handler in main.js sets: this toggle
  // runs on every navigation, so a view missing from this list gets the
  // class stripped straight back off again.
  // '/team' and '/scholars' are retired routes that redirect to '/people';
  // they stay listed so the sidebar does not flash in during the redirect.
  const NO_SIDEBAR = new Set([
    '/docs', '/stats', '/network', '/sql', '/people',
    '/org', '/team', '/scholars', '/data',
  ]);
  const noSidebar = NO_SIDEBAR.has(rootSeg);
  document.body.classList.toggle('no-sidebar', noSidebar);

  // Call route handler. Try exact match first; if the path has
  // sub-segments (e.g. '/people/<id>'), fall back to the top-level
  // handler which is responsible for parsing its own sub-routes.
  const handler = _routes[path] || _routes[rootSeg] || _routes['/'];
  if (handler) handler(path);
}

// When the tab strip overflows (phone widths), fade the edge that has more
// tabs behind it so the hidden SQL / Stats / Docs tabs read as scrollable.
function updateTabOverflow() {
  const nav = document.querySelector('.tabs');
  if (!nav) return;
  const max = nav.scrollWidth - nav.clientWidth;
  nav.classList.toggle('tabs-more-right', max > 2 && nav.scrollLeft < max - 2);
  nav.classList.toggle('tabs-more-left', max > 2 && nav.scrollLeft > 2);
}

export function initRouter(routes) {
  _routes = routes;
  const nav = document.querySelector('.tabs');
  if (nav) {
    nav.addEventListener('scroll', updateTabOverflow, { passive: true });
    window.addEventListener('resize', updateTabOverflow, { passive: true });
  }

  window.addEventListener('hashchange', () => {
    // Ignore in-page anchor changes — the browser already scrolled
    // the document to the target element; we don't need to (and
    // mustn't) re-route.
    if (!isRouteHash(location.hash)) return;
    navigate(getHash());
  });

  // Initial route
  navigate(getHash());
}

export function currentPath() {
  return _currentPath;
}
