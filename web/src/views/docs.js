// docs.js — Docs view: sub-tabs for Methods / References / Bibliography

import { DATA_BASE as BASE } from '../config.js';

const PAGES = [
  { key: 'methods',      label: 'Methods',      file: 'METHODS.md' },
  { key: 'references',   label: 'References',   file: 'REFERENCES.md' },
  { key: 'bibliography', label: 'Bibliography', file: 'BIBLIOGRAPHY.md' },
];

let _container = null;
let _activeKey = 'methods';
const _cache = {};
let _wired = false;

function mdToHtml(md) {
  const lines = md.split('\n');
  const out = [];
  let inCode = false;
  let codeLines = [];
  let inTable = false;
  let tableLines = [];

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    if (line.startsWith('```')) {
      if (inCode) {
        out.push('<pre><code>' + codeLines.map(escHtml).join('\n') + '</code></pre>');
        codeLines = [];
        inCode = false;
      } else {
        inCode = true;
      }
      continue;
    }
    if (inCode) { codeLines.push(line); continue; }

    if (line.startsWith('|')) {
      tableLines.push(line);
      inTable = true;
      continue;
    } else if (inTable) {
      out.push(renderTable(tableLines));
      tableLines = [];
      inTable = false;
    }

    if (/^#{1,6}\s/.test(line)) {
      const m = line.match(/^(#{1,6})\s+(.*)/);
      const level = m[1].length;
      out.push(`<h${level}>${inlinesMd(m[2])}</h${level}>`);
      continue;
    }

    if (/^---+$/.test(line.trim())) { out.push('<hr>'); continue; }

    if (/^[-*]\s/.test(line)) {
      out.push(`<li>${inlinesMd(line.replace(/^[-*]\s/, ''))}</li>`);
      continue;
    }

    if (line.trim() === '') { out.push(''); continue; }

    out.push(`<p>${inlinesMd(line)}</p>`);
  }

  if (inTable) out.push(renderTable(tableLines));
  if (inCode) out.push('<pre><code>' + codeLines.map(escHtml).join('\n') + '</code></pre>');

  const html = out.join('\n');
  return html.replace(/(<li>.*?<\/li>\n?)+/gs, (m) => `<ul>${m}</ul>`);
}

function renderTable(lines) {
  const rows = lines.filter((l) => !/^\|[-| ]+\|/.test(l));
  if (!rows.length) return '';
  const [head, ...body] = rows;
  const thCells = parseCells(head).map((c) => `<th>${inlinesMd(c)}</th>`).join('');
  const tbRows = body.map((r) => {
    const tds = parseCells(r).map((c) => `<td>${inlinesMd(c)}</td>`).join('');
    return `<tr>${tds}</tr>`;
  }).join('');
  return `<table class="md-table"><thead><tr>${thCells}</tr></thead><tbody>${tbRows}</tbody></table>`;
}

function parseCells(line) {
  return line.replace(/^\||\|$/g, '').split('|').map((s) => s.trim());
}

function inlinesMd(s) {
  return escHtml(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

function escHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function tabBar() {
  return `<div class="docs-tabs" role="tablist">
    ${PAGES.map((p) => `
      <button class="docs-tab ${p.key === _activeKey ? 'active' : ''}" data-key="${p.key}" role="tab">
        ${escHtml(p.label)}
      </button>
    `).join('')}
  </div>`;
}

async function loadPage(key) {
  if (_cache[key]) return _cache[key];
  const page = PAGES.find((p) => p.key === key);
  if (!page) throw new Error(`Unknown docs page: ${key}`);
  const res = await fetch(`${BASE}${page.file}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const md = await res.text();
  _cache[key] = mdToHtml(md);
  return _cache[key];
}

async function renderActive() {
  if (!_container) return;
  const body = _container.querySelector('.docs-body');
  if (!body) return;
  body.innerHTML = '<p style="color:var(--c-muted)">Loading…</p>';
  try {
    body.innerHTML = await loadPage(_activeKey);
  } catch (e) {
    const file = PAGES.find((p) => p.key === _activeKey)?.file ?? _activeKey;
    body.innerHTML = `<p style="color:#c00">Failed to load ${escHtml(file)}: ${escHtml(e.message)}</p>`;
  }
}

function wireTabs() {
  _container.querySelectorAll('.docs-tab').forEach((btn) => {
    btn.addEventListener('click', () => {
      const k = btn.dataset.key;
      if (!k || k === _activeKey) return;
      _activeKey = k;
      _container.querySelectorAll('.docs-tab').forEach((b) => {
        b.classList.toggle('active', b.dataset.key === _activeKey);
      });
      renderActive();
    });
  });
}

export async function initDocsView(container) {
  _container = container;
  if (!_wired) {
    _container.innerHTML = `${tabBar()}<div class="docs-body"></div>`;
    wireTabs();
    _wired = true;
  } else {
    // Already rendered once; make sure the tab bar reflects _activeKey
    _container.querySelectorAll('.docs-tab').forEach((b) => {
      b.classList.toggle('active', b.dataset.key === _activeKey);
    });
  }
  renderActive();
}
