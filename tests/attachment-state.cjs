// Deterministic tests of the real composer logic, without a browser or model.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const elements = new Map();
function element(key) {
  if (!elements.has(key)) elements.set(key, {content: 'test-token', innerHTML: '', textContent: '', value: '',
    style: {}, dataset: {}, classList: {toggle() {}, add() {}, remove() {}},
    querySelector: s => element(key + s), querySelectorAll: () => [], addEventListener() {}, focus() {}});
  return elements.get(key);
}
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const chats = {A: {id:'A', title:'Chat A', docs:[], params:{}, messages:[]},
  B: {id:'B', title:'Chat B', docs:[], params:{}, messages:[]}};
let finishImport;
let gate = new Promise(resolve => finishImport = resolve);
let importCount = 0;
const context = {
  console, setTimeout, clearTimeout, requestAnimationFrame: f => f(),
  localStorage: {getItem: () => null}, MD: {esc, render: esc, splitThink: s => ['',s,false]},
  icon: () => '×',
  Vault: {onChange() {}},
  document: {querySelector: element, querySelectorAll: () => [], addEventListener() {}},
  XMLHttpRequest: class {
    constructor() { this.upload = {}; }
    open(method, url) { this.url = url; }
    setRequestHeader() {}
    async send(file) {
      if (file.readError) { this.onerror(); return; }
      importCount++;
      this.upload.onprogress({lengthComputable:true,loaded:file.size/2,total:file.size});
      await gate;
      this.status = file.filename === 'broken.pdf' || file.name === 'broken.pdf' ? 400 : 200;
      this.response = this.status === 400 ? {error:'Unreadable PDF'} : {name:file.name};
      this.onload();
    }
  },
  FileReader: class {readAsDataURL(file) {setImmediate(() => {
    if (file.readError) this.onerror(); else {this.result = 'data:application/pdf;base64,eA=='; this.onload();}
  });}},
  fetch: async (url, options) => {
    const b = JSON.parse(options.body || '{}');
    if (url.endsWith('/import')) {
      importCount++;
      await gate;
      if (b.filename === 'broken.pdf') return {ok:false,json:async () => ({error:'Unreadable PDF'})};
      return {ok:true,json:async () => ({name:b.filename.replace('.pdf','.md')})};
    }
    const c = chats[url.split('/')[3]];
    if (options.method === 'DELETE') c.docs = c.docs.filter(n => n !== b.name);
    else {c.docs = [...new Set(c.docs.concat(b.name))]; (c.doc_meta ||= {})[b.name] = {filename:b.filename,size:b.size};}
    return {ok:true,json:async () => ({chat:JSON.parse(JSON.stringify(c))})};
  }
};
context.window = {innerHeight:850,addEventListener() {}};
const source = fs.readFileSync(path.join(__dirname,'..','vscode_assistant','static','app.js'),'utf8');
vm.runInNewContext(source.slice(0,source.indexOf('  (async function start() {')) +
  'window.test = {S, attachDocs, renderAttachments, messageHTML, wire, send, waitPercent, assistantBody, noteFirstText};})();', context);
const t = context.window.test;
const file = name => ({name,size:123,type:'application/pdf'});
const tick = () => new Promise(resolve => setImmediate(resolve));
const html = () => element('#attachments').innerHTML;
async function remove(selector, dataset) {
  await element('#attachments').onclick({target:{closest:s => s === selector ? {dataset} : null}});
}
(async () => {
  t.S.chat = chats.A;
  t.S.settings = {last_model:'ollama::test'};
  t.wire();
  const upload = t.attachDocs([file('Homework.pdf')]);
  await tick(); await tick();
  assert(html().includes('Uploading PDF'));
  assert(html().includes('Homework.pdf'));
  assert(element('#sendBtn').disabled);
  await t.send(); // direct Enter/submit calls must be blocked as well
  t.S.chat = chats.B; t.renderAttachments();
  assert(!html().includes('Homework'));
  finishImport(); await upload;
  assert.deepEqual(chats.A.docs, ['Homework.pdf']); assert.deepEqual(chats.B.docs, []);
  t.S.chat = chats.A; t.renderAttachments();
  assert(html().includes('Ready · original PDF preserved'));
  assert(html().includes('Open PDF'));
  assert(!element('#sendBtn').disabled);
  await remove('[data-doc-rm]', {docRm:'Homework.pdf'});
  assert(!html().includes('Homework'));
  await t.attachDocs([file('broken.pdf')]);
  assert(html().includes('Upload failed')); assert(html().includes('Unreadable PDF'));
  await remove('[data-upload-rm]', {uploadRm:String(t.S.uploads[0].id)});
  assert(!html().includes('broken.pdf'));
  await t.attachDocs([{...file('unreadable.pdf'),readError:true}]);
  assert(html().includes('Connection interrupted'));
  assert(!element('#sendBtn').disabled);
  t.S.uploads = [];
  gate = new Promise(resolve => finishImport = resolve);
  const cancelled = t.attachDocs([file('cancelled.pdf')]);
  await tick(); await tick();
  await remove('[data-upload-rm]', {uploadRm:String(t.S.uploads[0].id)});
  finishImport(); await cancelled;
  assert(!chats.A.docs.includes('cancelled.pdf'));
  assert(!html().includes('cancelled.pdf'));
  const message = t.messageHTML({role:'user',content:'Review this',documents:[{filename:'<img src=x>.pdf',size:123}]},0,false);
  assert(message.includes('&lt;img src=x&gt;.pdf')); assert(!message.includes('<img src=x>'));
  assert.equal(importCount,3);
  assert.equal(t.waitPercent(0, 45), 0);
  assert(t.waitPercent(10, 45) > 0);
  assert(t.waitPercent(20, 45) > t.waitPercent(10, 45));
  assert.equal(t.waitPercent(10000, 45), 95);
  assert(t.waitPercent(10, 12) > t.waitPercent(10, 45));
  t.S.started.A = Date.now() - 1000;
  const pending = t.assistantBody({model:'ollama::test',content:''},true);
  assert(pending.includes('wait-progress') && pending.includes('Est.'));
  assert(!t.assistantBody({model:'ollama::test',content:'Answer'},true).includes('wait-progress'));
  assert(t.S.waitHistory.size > 0);
  assert(!t.assistantBody({content:''},false).includes('wait-progress'));
  console.log('Attachment state checks passed: processing, send lock, chat isolation, ready, removal, failures, cancellation, escaping.');
})().catch(e => {console.error(e);process.exitCode=1;});
