import test from 'node:test';
import assert from 'node:assert/strict';
import {safeLink, inlineTokens, markdownBlocks, renderMarkdown, renderSources} from '../static/markdown.mjs';

test('unsafe protocols and credentials are never links', () => {
  for (const url of ['javascript:alert(1)', 'data:text/html,x', '//evil.test', 'https://user:pass@host.test']) assert.equal(safeLink(url), null);
  assert.equal(safeLink('https://github.com/a/b#L1'), 'https://github.com/a/b#L1');
});
test('knowledge source links accept only scoped local chunk paths', () => {
  const id = '12345678-1234-1234-1234-123456789abc';
  const source = `/knowledge/${id}/chunks/${id}`;
  assert.equal(safeLink(source), source);
  for (const path of ['/knowledge/../../etc/passwd', '//evil.test/knowledge/x', source + '?redirect=https://evil.test']) assert.equal(safeLink(path), null);
  assert.equal(inlineTokens(`[doc](${source})`)[0].href, source);
});
test('memory sources reject arbitrary local paths and accept scoped turns', () => {
  const id = '12345678-1234-1234-1234-123456789abc';
  assert.equal(safeLink(`/memory/${id}/turns/1`), `/memory/${id}/turns/1`);
  assert.equal(safeLink(`/memory/${id}/items/${id}`), `/memory/${id}/items/${id}`);
  for (const path of ['/memory/../../etc/passwd', `/memory/${id}/turns/0`, `/memory/${id}/turns/1?redirect=evil`]) assert.equal(safeLink(path), null);
});
test('raw HTML remains text; inline code, bold and links tokenize', () => {
  const tokens = inlineTokens('<script>alert(1)</script> **bold** `code` [file](https://github.com/a/b)');
  assert.equal(tokens[0].type, 'text');
  assert.ok(tokens.some(t => t.type === 'strong'));
  assert.ok(tokens.some(t => t.type === 'code'));
  assert.ok(tokens.some(t => t.type === 'a'));
  assert.equal(inlineTokens('[x](javascript:alert)')[0].type, 'text');
});
test('code fences preserve literal markup and headings/lists parse', () => {
  const blocks = markdownBlocks('# Title\n- item\n```python\n<script>\n**literal**\n```');
  assert.deepEqual(blocks.map(b => b.type), ['h1', 'li', 'pre']);
  assert.equal(blocks[2].text, '<script>\n**literal**');
});

test('DOM renderer creates safe nodes, code blocks, lists and source anchors', () => {
  class Node {
    constructor(tag = '') { this.tag = tag; this.children = []; this.classList = {add() {}}; }
    append(...nodes) { this.children.push(...nodes); }
    replaceChildren(...nodes) { this.children = nodes; }
    set innerHTML(value) { throw new Error('Raw HTML must never be used'); }
  }
  const original = globalThis.document;
  globalThis.document = {createElement: tag => new Node(tag), createTextNode: text => ({tag: 'text', textContent: text})};
  try {
    const body = new Node();
    renderMarkdown(body, '# Heading\n- **bold**\n```\n<script>\n```\n[x](javascript:bad)');
    assert.deepEqual(body.children.map(n => n.tag), ['h1', 'ul', 'pre', 'p']);
    assert.equal(body.children[2].children[0].textContent, '<script>');
    assert.equal(body.children[3].children[0].tag, 'text');
    const sources = new Node();
    renderSources(sources, [{label: '<img onerror=bad>', url: 'https://github.com/a/b'}, {label: 'unsafe', url: 'javascript:bad'}]);
    assert.equal(sources.children.filter(n => n.tag === 'a').length, 1);
    const link = sources.children.find(n => n.tag === 'a');
    assert.equal(link.textContent, '<img onerror=bad>');
    assert.equal(link.rel, 'noopener noreferrer');
    renderSources(sources, []); assert.equal(sources.hidden, true);
  } finally { globalThis.document = original; }
});
