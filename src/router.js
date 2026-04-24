// router.js — Hash-based router for the multi-view SPA

let _routes = {};
let _currentPath = null;

function getHash() {
  const h = location.hash || '#/';
  // Expect format: #/path
  return h.startsWith('#') ? h.slice(1) : '/';
}

function navigate(path) {
  if (path === _currentPath) return;
  _currentPath = path;

  // Update active tab styling
  document.querySelectorAll('.tabs a[data-view]').forEach((a) => {
    a.classList.toggle('active', a.dataset.view === path);
  });

  // Hide/show sidebar for views that don't need it
  const noSidebar = (path === '/docs' || path === '/stats'
                  || path === '/network' || path === '/sql');
  document.body.classList.toggle('no-sidebar', noSidebar);

  // Call route handler
  const handler = _routes[path] || _routes['/'];
  if (handler) handler(path);
}

export function initRouter(routes) {
  _routes = routes;

  window.addEventListener('hashchange', () => {
    navigate(getHash());
  });

  // Initial route
  navigate(getHash());
}

export function currentPath() {
  return _currentPath;
}
