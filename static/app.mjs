import {renderMarkdown, renderSources} from './markdown.mjs';
import {consumeSSE} from './stream.mjs';

const $ = id => document.getElementById(id);
const storageKey = 'dev-assistant.conversation';
let conversationId = null;
let controller = null;
let busy = false;
let knowledgeId = null;
let memoryProject = null;
try { conversationId = localStorage.getItem(storageKey); } catch {}
try { knowledgeId = localStorage.getItem('dev-assistant.knowledge'); } catch {}

function saveKnowledge(id) {
  knowledgeId = id || null;
  try { id ? localStorage.setItem('dev-assistant.knowledge', id) : localStorage.removeItem('dev-assistant.knowledge'); } catch {}
  $('knowledge-select').value = knowledgeId || '';
}
async function loadKnowledge() {
  try {
    const response = await fetch('/api/knowledge');
    if (!response.ok) throw new Error();
    const data = await response.json();
    for (const kb of data.knowledge_bases) {
      const option = document.createElement('option'); option.value = kb.id; option.textContent = kb.name;
      $('knowledge-select').append(option);
    }
    if (knowledgeId && !data.knowledge_bases.some(kb => kb.id === knowledgeId)) saveKnowledge(null);
    else saveKnowledge(knowledgeId);
    if (!data.embedding.configured) $('knowledge-hint').textContent = '混合问答需要先配置 Embedding 并构建索引';
  } catch { $('knowledge-hint').textContent = '暂时无法加载知识库'; }
}
$('knowledge-select').addEventListener('change', () => saveKnowledge($('knowledge-select').value));

function saveId(id) {
  conversationId = id;
  try { id ? localStorage.setItem(storageKey, id) : localStorage.removeItem(storageKey); } catch {}
  $('session-id').textContent = id || '尚未开始';
  $('memory-link').href = id ? `/memory?conversation=${encodeURIComponent(id)}` : '/memory';
}
function showMemory(state) {
  memoryProject = state?.memory_project || null;
  $('memory-project').value = memoryProject || '';
  if (state) $('memory-status').textContent = `已归档 ${state.archived_turns} 轮 · 上下文约 ${state.context_meta?.estimated_input_tokens || 0} tokens · 待确认记忆 ${state.pending_count || 0} 条`;
}
function setBusy(value) {
  busy = value;
  $('send').disabled = value;
  $('new-chat').disabled = value;
  $('new-chat-mobile').disabled = value;
  $('message').disabled = value;
  $('stop').hidden = !value;
  $('knowledge-select').disabled = value || Boolean(conversationId);
  $('memory-project').disabled = value || Boolean(conversationId);
  document.querySelectorAll('[data-prompt]').forEach(button => button.disabled = value);
}
function scrollDown() {
  $('conversation').scrollTop = $('conversation').scrollHeight;
}
function addMessage(role, content = '') {
  $('welcome').hidden = true;
  const article = document.createElement('article');
  article.className = `message ${role}`;
  const label = document.createElement('div');
  label.className = 'message-label';
  label.textContent = role === 'user' ? 'YOU' : 'DEV ASSISTANT';
  const trace = document.createElement('div');
  trace.className = 'trace'; trace.hidden = true;
  const body = document.createElement('div');
  body.className = 'message-body'; body.textContent = content;
  article.append(label, trace, body);
  $('messages').append(article);
  const sources = document.createElement('div'); sources.className = 'sources'; sources.hidden = true;
  article.append(sources);
  if (role === 'assistant' && content) renderMarkdown(body, content);
  scrollDown();
  return {article, body, trace, sources};
}
function note(message, text) {
  const node = document.createElement('div');
  node.className = 'error-note'; node.textContent = text;
  message.article.append(node);
}

async function restore() {
  if (!conversationId) return;
  setBusy(true); $('stop').hidden = true;
  $('messages').replaceChildren();
  $('status').textContent = '正在恢复上次对话……';
  try {
    const response = await fetch(`/conversations/${encodeURIComponent(conversationId)}`);
    if (response.status === 404 || response.status === 422) {
      saveId(null);
      $('status').textContent = '上次会话已不可用，可以开始新对话。';
      return;
    }
    if (!response.ok) throw new Error('暂时无法恢复对话，请刷新重试。');
    const history = await response.json();
    saveKnowledge(history.knowledge_base_id);
    showMemory(history.memory_state);
    $('messages').replaceChildren();
    for (const message of history.messages) {
      const node = addMessage(message.role, message.content);
      renderSources(node.sources, message.sources);
    }
    $('status').textContent = '已恢复最近的对话，可以继续提问。';
  } catch (error) { $('status').textContent = error.message; }
  finally { setBusy(false); }
}

function newChat() {
  if (busy) return;
  saveId(null); $('messages').replaceChildren(); $('welcome').hidden = false;
  $('knowledge-select').disabled = false;
  $('memory-project').disabled = false;
  $('memory-status').textContent = '新会话将使用上方项目与知识库范围；未分组时仅限本会话。';
  $('message').value = ''; $('status').textContent = '新会话已准备好。'; $('message').focus();
  document.querySelector('.sidebar').classList.remove('show-mobile');
  $('history-mobile').setAttribute('aria-expanded', 'false');
  void loadSessions();
}
$('new-chat').addEventListener('click', newChat);
$('new-chat-mobile').addEventListener('click', newChat);
$('stop').addEventListener('click', () => controller?.abort());
$('message').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault(); $('chat-form').requestSubmit();
  }
});
document.querySelectorAll('[data-prompt]').forEach(button => button.addEventListener('click', () => {
  if (!busy) { $('message').value = button.dataset.prompt; $('chat-form').requestSubmit(); }
}));

$('chat-form').addEventListener('submit', async event => {
  event.preventDefault();
  const text = $('message').value.trim();
  if (!text || busy) return;
  addMessage('user', text);
  const assistant = addMessage('assistant');
  const tools = new Map();
  let completed = false, failed = false;
  controller = new AbortController(); setBusy(true); $('message').value = '';
  try {
    const response = await fetch('/chat/stream', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text, conversation_id: conversationId, knowledge_base_id: knowledgeId,
        memory_project: $('memory-project').value.trim() || null}), signal: controller.signal,
    });
    if (!response.ok) {
      const error = await response.json();
      throw new Error(typeof error.detail === 'string' ? error.detail : '输入不合法，请检查后重试。');
    }
    await consumeSSE(response.body, (type, data) => {
      if (type === 'status') {
        $('status').textContent = data.message;
        if (data.agent && !assistant.trace.querySelector(`[data-agent="${data.agent}"]`)) {
          assistant.trace.hidden = false;
          const row = document.createElement('div'); row.className = 'trace-row';
          row.dataset.agent = data.agent;
          row.textContent = data.message;
          assistant.trace.append(row);
        }
        if (data.reset_answer) assistant.body.textContent = '';
      } else if (type === 'delta') {
        assistant.body.textContent += data.text;
      } else if (type === 'tool_start') {
        assistant.trace.hidden = false;
        const row = document.createElement('div'); row.className = 'trace-row';
        row.textContent = `◌ 正在执行 ${data.name}……`;
        tools.set(data.id, row); assistant.trace.append(row);
        $('status').textContent = data.name === 'search_knowledge' ? '正在混合检索知识库……' : ['search_memory','search_history','read_history'].includes(data.name) ? '正在检索已确认记忆或历史原文……' : '正在读取 GitHub 仓库资料……';
      } else if (type === 'tool_end') {
        const row = tools.get(data.id);
        if (row) { row.textContent = `${data.ok ? '✓' : '×'} ${data.message}`; row.classList.toggle('failed', !data.ok); }
        $('status').textContent = data.message;
      } else if (type === 'done') {
        completed = true; saveId(data.conversation_id);
        saveKnowledge(data.knowledge_base_id);
        showMemory(data.memory_state);
        renderMarkdown(assistant.body, data.answer);
        renderSources(assistant.sources, data.sources);
        void loadSessions();
        $('status').textContent = '回答完成 · 对话已保存';
      } else if (type === 'error') {
        failed = true; note(assistant, `本轮未保存：${data.message}`);
        $('status').textContent = '本轮未完成，可以重新发送。';
      }
      scrollDown();
    });
    if (!completed && !failed) throw new Error('连接中断，请刷新确认已保存的对话后重试。');
  } catch (error) {
    if (!completed && !failed) {
      const message = error.name === 'AbortError'
        ? '已停止接收。未完成的回答不会保存；若恰好完成，请刷新确认。'
        : error.message;
      note(assistant, message); $('status').textContent = message;
    }
  } finally {
    controller = null; setBusy(false); $('message').focus(); scrollDown();
  }
});

saveId(conversationId);
await loadKnowledge();
await restore();
await loadSessions();


async function loadSessions(append = false) {
  const status = $('sessions-status');
  try {
    const offset = append ? Number($('more-sessions').dataset.offset || 0) : 0;
    const response = await fetch(`/conversations?limit=20&offset=${offset}`);
    if (!response.ok) throw new Error('Unable to load conversations');
    const data = await response.json();
    if (!append) $('sessions-list').replaceChildren();
    for (const item of data.conversations) {
      const button = document.createElement('button');
      button.className = 'session-link'; button.textContent = item.title;
      button.title = item.updated_at ? new Date(item.updated_at).toLocaleString() : item.title;
      button.classList.toggle('selected', item.conversation_id === conversationId);
      button.addEventListener('click', async () => {
        if (busy) return;
        document.querySelector('.sidebar').classList.remove('show-mobile');
        $('history-mobile').setAttribute('aria-expanded', 'false');
        saveId(item.conversation_id); await restore(); await loadSessions();
      });
      $('sessions-list').append(button);
    }
    $('more-sessions').hidden = data.next_offset === null;
    $('more-sessions').dataset.offset = data.next_offset ?? '';
    status.textContent = data.conversations.length || append ? '' : '暂无已保存会话';
  } catch { status.textContent = '暂时无法加载会话列表'; }
}
$('more-sessions').addEventListener('click', () => { if (!busy) void loadSessions(true); });
$('history-mobile').addEventListener('click', () => {
  const shown = document.querySelector('.sidebar').classList.toggle('show-mobile');
  $('history-mobile').setAttribute('aria-expanded', String(shown));
});
document.querySelector('main').addEventListener('click', event => {
  if (event.target.closest('#history-mobile')) return;
  document.querySelector('.sidebar').classList.remove('show-mobile');
  $('history-mobile').setAttribute('aria-expanded', 'false');
});
