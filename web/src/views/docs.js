// docs.js — Docs view: renders METHODS.md as styled HTML

import { DATA_BASE as BASE } from '../config.js';
let _container = null;
let _cachedHtml = null;

function mdToHtml(md) {
  // Escape HTML first, then apply markdown patterns
  const lines = md.split('\n');
  const out = [];
  let inCode = false;
  let codeLines = [];
  let inTable = false;
  let tableLines = [];

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // Code block fence
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

    // Table row detection
    if (line.startsWith('|')) {
      tableLines.push(line);
      inTable = true;
      continue;
    } else if (inTable) {
      out.push(renderTable(tableLines));
      tableLines = [];
      inTable = false;
    }

    // Headings
    if (/^#{1,6}\s/.test(line)) {
      const m = line.match(/^(#{1,6})\s+(.*)/);
      const level = m[1].length;
      out.push(`<h${level}>${inlinesMd(m[2])}</h${level}>`);
      continue;
    }

    // HR
    if (/^---+$/.test(line.trim())) { out.push('<hr>'); continue; }

    // List items
    if (/^[-*]\s/.test(line)) {
      out.push(`<li>${inlinesMd(line.replace(/^[-*]\s/, ''))}</li>`);
      continue;
    }

    // Blank line
    if (line.trim() === '') { out.push(''); continue; }

    // Paragraph
    out.push(`<p>${inlinesMd(line)}</p>`);
  }

  if (inTable) out.push(renderTable(tableLines));
  if (inCode) out.push('<pre><code>' + codeLines.map(escHtml).join('\n') + '</code></pre>');

  // Wrap consecutive <li> in <ul>
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

export async function initDocsView(container) {
  _container = container;
  if (_cachedHtml) {
    _container.innerHTML = _cachedHtml;
    return;
  }
  _container.innerHTML = '<p style="padding:24px;color:var(--c-muted)">Loading documentation…</p>';
  try {
    const res = await fetch(`${BASE}METHODS.md`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const md = await res.text();
    _cachedHtml = `<div class="docs-body">${mdToHtml(md)}</div>`;
    _container.innerHTML = _cachedHtml;
  } catch (e) {
    _container.innerHTML = `<p style="padding:24px;color:#c00">Failed to load METHODS.md: ${escHtml(e.message)}</p>`;
  }
}
