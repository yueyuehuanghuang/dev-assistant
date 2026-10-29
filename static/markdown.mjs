// A small Markdown subset rendered with DOM nodes, never raw HTML.
export function safeLink(value) {
  if (/^\/knowledge\/[0-9a-f-]{36}\/chunks\/[0-9a-f-]{36}$/.test(value)) return value;
  if (/^\/memory\/[0-9a-f-]{36}\/(?:items\/[0-9a-f-]{36}|turns\/[1-9][0-9]*)$/.test(value)) return value;
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

export function inlineTokens(text) {
  const expression = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\([^\s)]+\))/g;
  const tokens = []; let start = 0;
  for (const match of text.matchAll(expression)) {
    if (match.index > start) tokens.push({type: 'text', text: text.slice(start, match.index)});
    const value = match[0];
    if (value.startsWith('`')) tokens.push({type: 'code', text: value.slice(1, -1)});
    else if (value.startsWith('**')) tokens.push({type: 'strong', text: value.slice(2, -2)});
    else {
      const link = /^\[([^\]]+)\]\((.+)\)$/.exec(value);
      const href = safeLink(link[2]);
      tokens.push(href ? {type: 'a', text: link[1], href} : {type: 'text', text: value});
    }
    start = match.index + value.length;
  }
  if (start < text.length) tokens.push({type: 'text', text: text.slice(start)});
  return tokens;
}

export function markdownBlocks(text) {
  const blocks = []; let code = null;
  for (const line of text.replace(/\r\n?/g, '\n').split('\n')) {
    if (/^\s*```/.test(line)) {
      if (code !== null) { blocks.push({type: 'pre', text: code.join('\n')}); code = null; }
      else code = [];
      continue;
    }
    if (code !== null) { code.push(line); continue; }
    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    const list = /^\s*(?:[-*+] |\d+\. )(.+)$/.exec(line);
    if (heading) blocks.push({type: 'h' + heading[1].length, text: heading[2]});
    else if (list) blocks.push({type: 'li', text: list[1]});
    else if (line.trim()) blocks.push({type: 'p', text: line});
  }
  if (code !== null) blocks.push({type: 'pre', text: code.join('\n')});
  return blocks;
}

export function renderMarkdown(container, text) {
  container.replaceChildren(); container.classList.add('markdown');
  let list = null;
  for (const block of markdownBlocks(text)) {
    const node = document.createElement(block.type);
    if (block.type === 'pre') {
      const code = document.createElement('code'); code.textContent = block.text; node.append(code);
    } else {
      for (const token of inlineTokens(block.text)) {
        if (token.type === 'text') node.append(document.createTextNode(token.text));
        else {
          const child = document.createElement(token.type); child.textContent = token.text;
          if (token.href) { child.href = token.href; child.target = '_blank'; child.rel = 'noopener noreferrer'; }
          node.append(child);
        }
      }
    }
    if (block.type === 'li') {
      if (!list) { list = document.createElement('ul'); container.append(list); }
      list.append(node);
    } else { list = null; container.append(node); }
  }
}

export function renderSources(container, sources = []) {
  container.replaceChildren(); container.hidden = !sources.length;
  if (!sources.length) return;
  const title = document.createElement('small'); title.textContent = '本轮工具来源'; container.append(title);
  for (const source of sources) {
    const href = safeLink(source.url);
    if (!href) continue;
    const link = document.createElement('a'); link.href = href; link.textContent = source.label;
    link.target = '_blank'; link.rel = 'noopener noreferrer'; container.append(link);
  }
}
