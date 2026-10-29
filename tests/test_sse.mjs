import test from 'node:test';
import assert from 'node:assert/strict';
import {consumeSSE} from '../static/stream.mjs';

test('SSE parser handles split UTF-8, CRLF and multiple frames', async () => {
  const bytes = new TextEncoder().encode(': heartbeat\r\n\r\nevent: delta\r\ndata: {"text":"你好"}\r\n\r\nevent: done\ndata: {"answer":"你好"}\n\n');
  const body = new ReadableStream({start(controller) {
    for (let index = 0; index < bytes.length; index++) controller.enqueue(bytes.slice(index, index + 1));
    controller.close();
  }});
  const events = [];
  await consumeSSE(body, (event, data) => events.push([event, data]));
  assert.deepEqual(events, [['delta', {text: '你好'}], ['done', {answer: '你好'}]]);
});

test('incomplete SSE frame raises instead of silently finishing', async () => {
  const body = new ReadableStream({start(controller) {
    controller.enqueue(new TextEncoder().encode('event: delta\ndata: {"text":'));
    controller.close();
  }});
  await assert.rejects(consumeSSE(body, () => {}));
});
