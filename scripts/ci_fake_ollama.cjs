// Test-only fetch hook for the installed one-shot candidate workflow.
// It never forwards an HTTP request to a model provider.
const fs = require('node:fs');
const log = process.env.CASTOR_FAKE_OLLAMA_LOG;
if (!log) throw new Error('CASTOR_FAKE_OLLAMA_LOG is required');
let chats = 0;
const digest = 'a'.repeat(64);
const reply = (value) => new Response(JSON.stringify(value), {
  status: 200, headers: {'content-type': 'application/json'},
});
globalThis.fetch = async (input, options = {}) => {
  const url = String(input);
  if (!url.startsWith('http://127.0.0.1:11434/')) {
    throw new Error(`fake Ollama refused URL: ${url}`);
  }
  const path = new URL(url).pathname;
  const method = options.method || 'GET';
  fs.appendFileSync(log, JSON.stringify({pid: process.pid, path, method}) + '\n');
  if (path === '/api/tags' && method === 'GET') {
    return reply({models: [{name: 'qwen3.5:9b', digest}]});
  }
  if (path === '/api/version' && method === 'GET') {
    return reply({version: '0.34.1'});
  }
  if (path === '/api/chat' && method === 'POST') {
    const request = JSON.parse(options.body);
    if (request.model !== 'qwen3.5:9b' || request.stream !== false || request.think !== false) {
      throw new Error('fake Ollama observed unexpected model policy');
    }
    chats++;
    if (chats > 3) throw new Error('fake Ollama interaction cap exceeded');
    const message = chats === 1
      ? {role: 'assistant', content: '', tool_calls: [{id: 'read-1', function: {name: 'castor_read_file', arguments: {path: 'hello.txt'}}}]}
      : chats === 2
        ? {role: 'assistant', content: '', tool_calls: [{id: 'edit-1', function: {name: 'castor_edit_file', arguments: {path: 'hello.txt', edits: [{oldText: 'hello', newText: 'fixed'}]}}}]}
        : {role: 'assistant', content: 'Done.'};
    return reply({model: 'qwen3.5:9b', done: true, done_reason: 'stop', message,
      prompt_eval_count: chats === 1 ? 7 : 9, eval_count: chats === 1 ? 5 : chats === 2 ? 8 : 2});
  }
  throw new Error(`fake Ollama refused request: ${method} ${path}`);
};
