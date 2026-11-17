// Run with Node and Playwright installed. Exercises the real composer functions
// against a controlled import API; no model server is required.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(require.resolve('playwright', {paths: [process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES || process.cwd()]}));

(async () => {
  const browser = await chromium.launch({headless: true,
    ...(process.env.CHROMIUM_PATH ? {executablePath: process.env.CHROMIUM_PATH} : {}), args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1100, height: 850}});
    const root = path.join(__dirname, '..', 'vscode_assistant', 'static');
    const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8')
      .replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '').replace(/<link\b[^>]*>/g, '');
    await page.setContent(html);
    await page.addStyleTag({content: fs.readFileSync(path.join(root, 'app.css'), 'utf8')});
    await page.evaluate(() => {
      const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
      window.MD = {esc, render: esc, splitThink: s => ['', s, false]};
      window.icon = () => '<span aria-hidden="true">×</span>';
      window.Vault = {onChange() {}};
    });
    const source = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
    await page.addScriptTag({content: source.slice(0, source.indexOf('  (async function start() {')) +
      'window.__attachments = {S, attachDocs, renderAttachments, messageHTML, wire, send};\n})();'});
    await page.evaluate(() => {
      const t = window.__attachments;
      t.S.chat = {id: 'chatA', title: 'Chat A', docs: [], params: {}, messages: []};
      t.S.settings = {last_model: 'ollama::test'};
      window.chats = {chatA: t.S.chat, chatB: {id: 'chatB', docs: [], params: {}, messages: []}};
      window.importCount = 0;
      window.importGate = new Promise(resolve => window.finishImport = resolve);
      window.fetch = async (url, options) => {
        const body = JSON.parse(options.body || '{}');
        if (url.endsWith('/import')) {
          window.importCount++;
          await window.importGate;
          if (body.filename === 'broken.pdf') return {ok: false, json: async () => ({error: 'Unreadable PDF'})};
          return {ok: true, json: async () => ({name: body.filename.replace('.pdf', '.md')})};
        }
        const id = url.split('/')[3];
        const c = window.chats[id];
        if (options.method === 'DELETE') c.docs = c.docs.filter(n => n !== body.name);
        else {
          c.docs = [...new Set(c.docs.concat(body.name))];
          (c.doc_meta ||= {})[body.name] = {filename: body.filename, size: body.size};
        }
        return {ok: true, json: async () => ({chat: JSON.parse(JSON.stringify(c))})};
      };
      t.wire();
      window.upload = t.attachDocs([new File(['sample'], 'Homework.pdf', {type: 'application/pdf'})]);
    });
    await page.waitForFunction(() => document.querySelector('#attachments').textContent.includes('Uploading and processing'));
    assert(await page.locator('#sendBtn').isDisabled());
    assert((await page.locator('#attachments').innerText()).includes('Homework.pdf'));
    // Switching chats while an import is in flight must not redirect its attachment.
    await page.evaluate(() => { window.__attachments.S.chat = window.chats.chatB; window.__attachments.renderAttachments(); });
    assert.equal(await page.locator('#attachments .document-card').count(), 0);
    await page.evaluate(async () => { window.finishImport(); await window.upload; });
    assert.deepEqual(await page.evaluate(() => window.chats.chatA.docs), ['Homework.md']);
    assert.deepEqual(await page.evaluate(() => window.chats.chatB.docs), []);
    await page.evaluate(() => { window.__attachments.S.chat = window.chats.chatA; window.__attachments.renderAttachments(); });
    assert((await page.locator('#attachments').innerText()).includes('Ready · attached to this chat'));
    assert(await page.locator('#sendBtn').isEnabled());
    await page.locator('#attachments [data-doc-rm]').click();
    await page.waitForFunction(() => window.chats.chatA.docs.length === 0);
    assert.equal(await page.locator('#attachments .document-card').count(), 0);
    // Errors stay visible rather than disappearing into a temporary toast.
    await page.evaluate(async () => {
      await window.__attachments.attachDocs([new File(['bad'], 'broken.pdf', {type: 'application/pdf'})]);
    });
    assert((await page.locator('#attachments').innerText()).includes('Unreadable PDF'));
    assert(await page.locator('#sendBtn').isEnabled());
    await page.locator('#attachments [data-upload-rm]').click();
    assert.equal(await page.locator('#attachments .document-card').count(), 0);
    // Sent-message filenames are escaped, including names that resemble HTML.
    const message = await page.evaluate(() => window.__attachments.messageHTML({role: 'user', content: 'Review this',
      documents: [{filename: '<img src=x>.pdf', size: 123}]}, 0, false));
    assert(message.includes('&lt;img src=x&gt;.pdf'));
    assert(!message.includes('<img src=x>'));
    // Keep a visual fixture of ready, processing, and failed attachment states.
    await page.evaluate(() => {
      const t = window.__attachments;
      t.S.chat.docs = ['Homework.md'];
      t.S.uploads = [{id: 10, chat: 'chatA', filename: 'Course_readings.pdf', size: 830000, status: 'Uploading and processing…', pending: true},
        {id: 11, chat: 'chatA', filename: 'broken.pdf', size: 2500, status: 'Upload failed', pending: false, error: 'Unreadable PDF. Try uploading another copy.'}];
      t.renderAttachments();
    });
    if (process.env.ATTACHMENT_SCREENSHOT) await page.locator('#composer').screenshot({path: process.env.ATTACHMENT_SCREENSHOT});
    console.log('Attachment UI checks passed: processing, send lock, chat isolation, ready, removal, errors, escaping.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
