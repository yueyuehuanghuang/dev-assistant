const $ = id => document.getElementById(id);
let selected = null, polling = null, locked = false;
const states = {pending: '尚未构建向量索引', stale: '文档已变化，需要更新向量索引', indexing: '正在生成向量…', ready: '向量索引已就绪', error: '索引失败', interrupted: '上次索引已中断'};

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '请求失败，请检查输入或稍后重试。');
  return result;
}
function message(text, error = false) { $('kb-message').textContent = text; $('kb-message').classList.toggle('error-note', error); }
function lock(value) {
  locked = value;
  document.querySelectorAll('button').forEach(b => b.disabled = value);
}
async function action(work) {
  if (locked) return;
  lock(true);
  try { await work(); } catch (error) { message(error.message, true); }
  finally { lock(false); }
}
async function refreshBases() {
  const data = await api('/api/knowledge');
  $('embedding-status').textContent = data.embedding.configured ? `${data.embedding.location === 'local' ? '本机' : '远程'} Embedding：${data.embedding.model} · ${data.embedding.dimensions} 维` : 'Embedding 未配置 · 可先上传和测试关键词检索';
  $('kb-list').replaceChildren();
  for (const kb of data.knowledge_bases) {
    const button = document.createElement('button'); button.className = 'session-link';
    button.classList.toggle('selected', kb.id === selected);
    button.textContent = `${kb.name} · ${kb.document_count} 文档`;
    button.addEventListener('click', () => action(() => select(kb.id)));
    $('kb-list').append(button);
  }
}
async function refreshIndex() {
  if (!selected) return;
  const id = selected;
  const kb = await api(`/api/knowledge/${id}`);
  if (id !== selected) return;
  $('kb-title').textContent = kb.name;
  $('kb-index-status').textContent = (states[kb.index_state] || kb.index_state) + (kb.index_error ? `：${kb.index_error}` : '');
  clearTimeout(polling);
  if (kb.index_state === 'indexing') polling = setTimeout(() => refreshIndex().catch(e => message(e.message, true)), 1500);
}
async function refreshDocuments() {
  const data = await api(`/api/knowledge/${selected}/documents`);
  $('kb-documents').replaceChildren();
  if (!data.documents.length) { $('kb-documents').textContent = '还没有文档，先上传一份项目资料。'; return; }
  for (const documentInfo of data.documents) {
    const row = document.createElement('div'); row.className = 'kb-document';
    const label = document.createElement('span'); label.textContent = `${documentInfo.name} · ${documentInfo.chunk_count} 片段 · ${documentInfo.character_count} 字${documentInfo.archived ? ' · 已归档' : ''}`;
    const button = document.createElement('button'); button.textContent = documentInfo.archived ? '恢复' : '归档';
    button.addEventListener('click', () => action(async () => {
      await api(`/api/knowledge/${selected}/documents/${documentInfo.id}/${documentInfo.archived ? 'restore' : 'archive'}`, {method: 'POST'});
      $('kb-results').replaceChildren();
      await refreshDocuments(); await refreshIndex(); await refreshBases();
      message('文档状态已更新，请重新构建向量索引。归档内容不会进入新的检索结果。');
    }));
    row.append(label, button); $('kb-documents').append(row);
  }
}
async function select(id) {
  clearTimeout(polling); selected = id;
  $('kb-workspace').hidden = false; $('kb-results').replaceChildren();
  await refreshIndex(); await refreshDocuments(); await refreshBases();
  message('资料已加载。可上传文档、更新向量索引或测试检索。');
}
$('kb-create').addEventListener('submit', event => {
  event.preventDefault();
  void action(async () => {
    const kb = await api('/api/knowledge', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name: $('kb-name').value.trim()})});
    $('kb-name').value = ''; await select(kb.id);
  });
});
$('kb-upload').addEventListener('submit', event => {
  event.preventDefault();
  void action(async () => {
    const file = $('kb-file').files[0];
    if (!file || file.size > 1_000_000) throw new Error('请选择不超过 1 MB 的文档。');
    const form = new FormData(); form.append('file', file);
    const result = await api(`/api/knowledge/${selected}/documents`, {method: 'POST', body: form});
    $('kb-file').value = ''; $('kb-results').replaceChildren();
    await refreshDocuments(); await refreshIndex(); await refreshBases();
    message(result.duplicate ? (result.archived ? '内容已存在且已归档，可点击“恢复”。' : '相同内容已存在，本次未重复导入。') : `上传成功，生成 ${result.chunk_count} 个片段。下一步构建向量索引。`);
  });
});
$('reindex').addEventListener('click', () => action(async () => {
  await api(`/api/knowledge/${selected}/reindex`, {method: 'POST'});
  message('索引任务已提交，页面会自动更新状态；可离开页面，后端继续执行。');
  await refreshIndex();
}));
$('kb-search').addEventListener('submit', event => {
  event.preventDefault();
  void action(async () => {
    const result = await api(`/api/knowledge/${selected}/search`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({query: $('kb-query').value.trim(), mode: $('kb-mode').value, top_k: 5})});
    $('kb-results').replaceChildren();
    message(`检索模式：${result.retrieval_mode} · ${result.total_matches} 个候选。${result.note}`);
    if (!result.results.length) { $('kb-results').textContent = '没有命中。可改写关键词、检查文档或校准向量阈值，不能据此生成确定结论。'; return; }
    for (const item of result.results) {
      const card = document.createElement('article'); card.className = 'kb-result';
      const link = document.createElement('a'); link.href = item.url; link.textContent = `${item.name} · 第 ${item.start_line}–${item.end_line} 行`; link.target = '_blank'; link.rel = 'noopener noreferrer';
      const meta = document.createElement('small'); meta.textContent = `分数 ${Number(item.score).toFixed(4)}${item.vector_score != null ? ` · 余弦 ${Number(item.vector_score).toFixed(4)}` : ''}`;
      const content = document.createElement('pre'); content.textContent = item.content;
      card.append(link, meta, content); $('kb-results').append(card);
    }
  });
});
$('use-kb').addEventListener('click', () => {
  try { localStorage.setItem('dev-assistant.knowledge', selected); localStorage.removeItem('dev-assistant.conversation'); } catch {}
  location.assign('/');
});
try { await refreshBases(); } catch (error) { message(error.message, true); }
