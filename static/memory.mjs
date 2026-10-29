const $ = id => document.getElementById(id);
let selected = new URLSearchParams(location.search).get('conversation'), busy = false, offset = 0;
const labels = {pending: '待确认', approved: '已确认', rejected: '已拒绝', archived: '已撤回'};
async function api(url, options = {}) {
  const response = await fetch(url, options); const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '操作失败，请检查输入。');
  return body;
}
async function action(work) {
  if (busy) return;
  busy = true; document.querySelectorAll('button,select').forEach(n => n.disabled = true);
  try { await work(); } catch (error) { $('memory-message').textContent = error.message; }
  finally { busy = false; document.querySelectorAll('button,select').forEach(n => n.disabled = false); }
}
async function transcript(reset = false) {
  if (reset) { offset = 0; $('memory-transcript').replaceChildren(); }
  const data = await api(`/conversations/${selected}/transcript?offset=${offset}&limit=10`);
  for (const turn of data.turns) {
    const card = document.createElement('details'); card.className = 'kb-result';
    const summary = document.createElement('summary'); summary.textContent = `第 ${turn.turn_number} 轮 · ${turn.created_at}`;
    card.append(summary);
    for (const message of turn.messages) {
      const text = document.createElement('pre'); text.className = 'memory-summary'; text.textContent = `${message.role}: ${message.content}`; card.append(text);
    }
    $('memory-transcript').append(card);
  }
  offset = data.next_offset; $('memory-more').hidden = offset === null;
}
async function refresh() {
  if (!selected) return;
  const data = await api(`/api/conversations/${selected}/memory`);
  $('memory-workspace').hidden = false;
  $('scope-label').textContent = data.memory_project ? `项目：${data.memory_project}${data.knowledge_base_id ? '（同时限定当前知识库）' : ''}` : data.knowledge_base_id ? '当前知识库范围（可跨会话召回已确认记忆）' : '仅本会话（未指定项目或知识库）';
  $('summary-status').textContent = `归档 ${data.archived_turns} 轮；摘要是有上限的原文摘录，可能遗漏信息，并非已确认事实。`;
  $('memory-summary').textContent = data.summary.length ? data.summary.map((e,i) => `${i+1}. 用户摘录：${e.user_excerpt}\n助手摘录：${e.answer_excerpt}`).join('\n\n') : '尚未产生跨窗口摘要。';
  $('memory-items').replaceChildren();
  const approved = data.items.filter(i => i.status === 'approved');
  for (const item of data.items) {
    const card = document.createElement('article'); card.className = 'kb-result';
    const label = document.createElement('strong'); label.textContent = `${labels[item.status]} · ${item.kind} · v${item.version}`;
    const content = document.createElement('p'); content.textContent = item.content;
    const quote = document.createElement('small'); quote.textContent = `出处：${item.source_quote}`;
    card.append(label, content, quote);
    const replace = document.createElement('select');
    if (item.status === 'pending') {
      const none = document.createElement('option'); none.value = ''; none.textContent = '不替换旧记忆'; replace.append(none);
      for (const old of approved) { const option = document.createElement('option'); option.value = old.id; option.textContent = `替换：${old.content.slice(0,60)}`; replace.append(option); }
      card.append(replace);
    }
    for (const [name, text] of item.status === 'pending' ? [['approve','确认'],['reject','拒绝']] : item.status === 'approved' ? [['archive','撤回']] : [['reopen','重新提交待确认']]) {
      const button = document.createElement('button'); button.textContent = text;
      button.addEventListener('click', () => action(async () => {
        await api(`/api/conversations/${selected}/memories/${item.id}/actions`, {method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({action: name, version: item.version, supersedes_id: name === 'approve' ? replace.value || null : null})});
        await refresh();
      })); card.append(button);
    }
    $('memory-items').append(card);
  }
  if (!data.items.length) $('memory-items').textContent = '暂无候选。可手动提交，或在聊天中明确说“记住：……”；成功保存后生成待确认候选。';
  $('memory-message').textContent = `待确认 ${data.pending_count} 条；只有已确认记忆才会进入后续上下文及长期召回。`;
}
$('memory-propose').addEventListener('submit', event => {
  event.preventDefault(); void action(async () => {
    await api(`/api/conversations/${selected}/memories`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({content: $('memory-content').value.trim(), kind: $('memory-kind').value})});
    $('memory-content').value = ''; await refresh();
  });
});
$('memory-more').addEventListener('click', () => action(() => transcript()));
$('memory-conversation').addEventListener('change', () => action(async () => { selected = $('memory-conversation').value; if (selected) { await refresh(); await transcript(true); } else $('memory-workspace').hidden = true; }));
try {
  const data = await api('/conversations?limit=50');
  for (const c of data.conversations) { const option = document.createElement('option'); option.value = c.conversation_id; option.textContent = c.title; $('memory-conversation').append(option); }
  if (selected) {
    if (![...$('memory-conversation').options].some(o => o.value === selected)) { const option = document.createElement('option'); option.value = selected; option.textContent = '当前会话'; $('memory-conversation').append(option); }
    $('memory-conversation').value = selected; await refresh(); await transcript(true);
  }
} catch (error) { $('memory-message').textContent = error.message; }
