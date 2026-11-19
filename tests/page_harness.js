// Runs the canvas page's own script under Node with a stand-in for the browser, against a real test server,
// and drives its saving logic. No browser is involved. Used by tests/test_page.py:
//   node page_harness.js <playbook.html> <http://127.0.0.1:PORT>
// Prints one JSON object with what happened at each step; the Python test asserts on it.
'use strict';
const fs = require('fs');
const vm = require('vm');

const [pagePath, base, deadPort] = process.argv.slice(2);
const html = fs.readFileSync(pagePath, 'utf8');
let code = html.slice(html.indexOf('<script>') + 8, html.lastIndexOf('</script>'));
const tail = 'boot();\n})();';
if (!code.includes(tail)) throw new Error('the page script no longer ends with boot()');
code = code.replace(tail, 'globalThis.__pb = {S, api, touchFlow, flush, saveDoc, deleteDoc, poll, view, cleanFlow, dirty, unsaved, fails, retryAt, saving, ' +
  'undoStack, beginChange, detachNode, addNodeFromPart, partUse, nodeById, renderFlow, renderLegend, renderBar, renderInspector, renderPage, ACT, addNode, ' +
  'fit, fitKey, cam, normFlow, kindOf, pendingWork}; globalThis.__booted = boot();\n})();');

// ---- a stand-in for the DOM: every element accepts anything and remembers what it was given
class El {
  constructor(id) {
    this.id = id; this.hidden = id === '#modal' || id === '#menu' || id === '#toast' || id === '#insp';
    this.dataset = {}; this.style = {}; this.children = []; this.innerHTML = ''; this.textContent = ''; this.value = '';
    this.offsetWidth = 252; this.offsetHeight = 96; this.clientWidth = 1200; this.clientHeight = 800; this.width = 352; this.height = 232;
    this.offsetParent = null; this.scrollTop = 0;
    this.classList = {add() {}, remove() {}, toggle() {}, contains() { return false; }};
  }
  addEventListener(type, fn) { (this.listeners = this.listeners || {})[type] = fn; } removeEventListener() {} setAttribute() {} focus() {} select() {} blur() {} click() {} setPointerCapture() {}
  querySelector(s) { return els(this.id + ' ' + s); }
  querySelectorAll() { return []; }
  closest() { return null; }
  contains() { return false; }
  getBoundingClientRect() { return {left: 0, top: 0, right: 1200, bottom: 800, width: 1200, height: 800}; }
  getContext() { return new Proxy({}, {get: () => () => {}, set: () => true}); }
}
const cache = new Map();
function els(sel) { if (!cache.has(sel)) cache.set(sel, new El(sel)); return cache.get(sel); }
const toasts = [];
const toastEl = els('#toast');
Object.defineProperty(toastEl, 'innerHTML', {get() { return this._h || ''; }, set(v) { this._h = v; if (v) toasts.push(v.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim()); }});

let target = base;   // switched to a dead port to play "the server is down"
let calls = [];
const sandbox = {
  console, setTimeout, clearTimeout, URL, encodeURIComponent, decodeURIComponent, encodeURI, JSON, Math, Date, Set, Map, Promise, Object, Array, String, Number, Error, RegExp, Infinity,
  setInterval: () => 0, clearInterval: () => {}, requestAnimationFrame: () => 0, cancelAnimationFrame: () => {},
  fetch: (path, opt = {}) => { calls.push((opt.method || 'GET') + ' ' + path); return fetch(target + '/' + String(path).replace(/^\//, ''), opt); },
  document: {hidden: false, querySelector: els, querySelectorAll: () => [], addEventListener() {}, elementFromPoint: () => null, documentElement: els('html')},
  location: {hash: ''}, navigator: {clipboard: {writeText: async () => {}}}, localStorage: {getItem: () => null, setItem() {}},
  getComputedStyle: () => ({getPropertyValue: () => ''}), CSS: {escape: s => s}, innerWidth: 1200, innerHeight: 800,
};
sandbox.window = sandbox; sandbox.globalThis = sandbox; sandbox.addEventListener = () => {};
vm.createContext(sandbox);
vm.runInContext(code, sandbox, {filename: 'playbook-page.js'});

const pb = sandbox.__pb;
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function settled(id, ms = 8000) {   // wait until the page has nothing left to send for this flow
  const end = Date.now() + ms;
  while (Date.now() < end) { if (!pb.dirty.has(id) && !pb.saving[id]) return true; await sleep(25); }
  return false;
}
async function outside(method, path, body, headers = {}) {   // a change made by someone else (pb.py, another window)
  const tok = (await (await fetch(base + '/api/session')).json()).token;
  const r = await fetch(base + path, {method, headers: Object.assign({'X-Playbook-Token': tok, 'Content-Type': 'application/json'}, headers), body: body === undefined ? undefined : JSON.stringify(body)});
  return {status: r.status, json: await r.json()};
}
const disk = async () => (await (await fetch(base + '/api/all')).json());
const status = () => els('#status').dataset.k;

(async () => {
  const out = {};
  await sandbox.__booted;
  const S = pb.S;
  out.boot = {mode: S.mode, flows: Object.keys(S.flows).sort(), revs: Object.keys(S.revs.flows).sort(), status: status(), canWrite: S.canWrite};

  // 1. an ordinary edit: the page's token is the placeholder, so the first save must refresh it and try again
  calls = [];
  S.flows.welcome.summary = 'edited in the page';
  pb.touchFlow('welcome');
  out.edit = {settled: await settled('welcome'), calls: calls.slice(), status: status()};
  let d = await disk();
  out.edit.onDisk = d.data.flows.welcome.summary;
  out.edit.revMatches = d.revs.flows.welcome === S.revs.flows.welcome;
  out.edit.originKept = d.data.flows.welcome.nodes.find(n => n.id === 'welcome-rules').origin;
  out.edit.noEdgeIds = d.data.flows.welcome.edges.every(e => !('id' in e) && !('label' in e && e.label === ''));

  // 2. someone else changes the same flow; the page's next save is stale and must not overwrite it
  const theirs = JSON.parse(JSON.stringify(d.data.flows.welcome)); theirs.summary = 'changed outside';
  out.outsidePut = (await outside('PUT', '/api/flows/welcome', theirs, {'If-Match': d.revs.flows.welcome})).status;
  pb.beginChange('welcome');
  S.flows.welcome.summary = 'stale page copy';
  toasts.length = 0;
  pb.touchFlow('welcome');
  await settled('welcome');
  d = await disk();
  out.stale = {onDisk: d.data.flows.welcome.summary, inPage: S.flows.welcome.summary, revMatches: d.revs.flows.welcome === S.revs.flows.welcome,
    toast: toasts.join(' | '), undoLeft: pb.undoStack.filter(u => u.id === 'welcome').length, unsaved: [...pb.unsaved], dirty: [...pb.dirty]};

  // 3. a change to another flow is picked up by the poll, by revision
  const posting = JSON.parse(JSON.stringify(d.data.flows.posting)); posting.summary = 'posting changed outside';
  await outside('PUT', '/api/flows/posting', posting, {'If-Match': d.revs.flows.posting});
  await pb.poll();
  out.poll = {inPage: S.flows.posting.summary, stampMatches: (await disk()).stamp === S.stamp};

  // 4. a save the server refuses (the document is not valid) is not retried
  S.flows.welcome.nodes[0].kind = 'weird';
  calls = []; toasts.length = 0;
  pb.touchFlow('welcome');
  await settled('welcome');
  await sleep(1500);
  out.refused = {puts: calls.filter(c => c.startsWith('PUT')).length, unsaved: [...pb.unsaved], dirty: [...pb.dirty], status: status(), toast: toasts.join(' | ')};
  S.flows.welcome.nodes[0].kind = 'start';
  pb.touchFlow('welcome');
  await settled('welcome');
  out.refused.afterFix = {status: status(), unsaved: [...pb.unsaved]};

  // 5. the server goes away: the change is kept and retried less and less often, then lands when it is back
  target = 'http://127.0.0.1:' + (deadPort || 9);
  S.flows.welcome.summary = 'saved after the outage';
  calls = []; toasts.length = 0;
  pb.touchFlow('welcome');
  await sleep(4200);
  const tries = calls.filter(c => c.startsWith('PUT')).length;
  out.outage = {tries, fails: pb.fails.welcome, stillDirty: pb.dirty.has('welcome'), status: status(), toasts: toasts.length, waitMs: Math.round(pb.retryAt.welcome - Date.now())};
  target = base;
  out.outage.settled = await settled('welcome', 12000);
  out.outage.onDisk = (await disk()).data.flows.welcome.summary;
  out.outage.statusAfter = status();

  // 6. parts by reference
  S.view = 'flow'; S.flowId = 'posting';
  const ref = pb.nodeById(S.flows.posting, 'posting-readback');
  out.parts = {stored: Object.keys(ref).sort(), shown: pb.view(ref).title, kind: pb.view(ref).kind, uses: pb.partUse('part-read-back').length};
  pb.addNodeFromPart(S.parts['part-read-back'], 0, 400);
  const added = S.flows.posting.nodes[S.flows.posting.nodes.length - 1];
  out.parts.added = {keys: Object.keys(added).sort(), idOk: /^[a-z0-9]+(-[a-z0-9]+)*$/.test(added.id), cleaned: Object.keys(pb.cleanFlow(S.flows.posting).nodes.slice(-1)[0]).sort()};
  S.flows.posting.edges.push({id: 'posting-live>' + added.id, from: 'posting-live', to: added.id, type: 'flow', label: ''});
  pb.touchFlow('posting');
  out.parts.savedRef = await settled('posting');
  d = await disk();
  out.parts.onDiskRef = d.data.flows.posting.nodes.find(n => n.id === added.id);
  pb.detachNode(added);
  pb.touchFlow('posting');
  await settled('posting');
  d = await disk();
  out.parts.onDiskDetached = d.data.flows.posting.nodes.find(n => n.id === added.id);
  out.parts.status = status();

  // 7. an account saved through the page keeps a field the form does not know about
  const acct = Object.assign({}, S.accounts['x-sam'], {origin: 'kept by the page'}); delete acct.id;
  out.account = {saved: await pb.saveDoc('accounts', 'x-sam', acct)};
  out.account.onDisk = (await disk()).data.accounts['x-sam'].origin;
  const stale = Object.assign({}, acct, {purpose: 'stale'});
  const fresh = Object.assign({}, acct, {purpose: 'changed outside'});
  await outside('PUT', '/api/accounts/x-sam', fresh, {'If-Match': S.revs.accounts['x-sam']});
  out.account.staleSave = await pb.saveDoc('accounts', 'x-sam', stale);
  out.account.afterStale = S.accounts['x-sam'].purpose;

  // 8. the panels render without errors and show what they should (the HTML is only inspected as text)
  const insp = () => els('#insp').innerHTML;
  const has = (h, ...bits) => bits.map(b => h.includes(b));
  S.view = 'flow'; S.flowId = 'welcome'; S.editing = null; S.sel = new Set(['welcome-rules']);
  pb.renderFlow(); pb.renderLegend(); pb.renderBar(); pb.renderInspector();
  out.render = {board: has(els('#nodes').innerHTML, 'data-id="welcome-rules"', 'Read the rules', 'k-goto', 'Posting')};
  out.render.plain = has(insp(), 'Read the rules', 'Where this came from', 'always read the rules first', 'session:fixture', 'data-act="edit"', 'Helper scripts', 'Rules read', 'https://example.com/rules');
  S.editing = 'welcome-rules'; pb.renderInspector();
  out.render.form = has(insp(), 'id="nodeForm"', 'id="nf_body" rows="9" maxlength="1200"', 'Read every rule attached to this step.');
  S.editing = null; S.flowId = 'posting'; S.sel = new Set(['posting-readback']);
  pb.renderFlow(); pb.renderLegend(); pb.renderBar(); pb.renderInspector();
  out.render.partBoard = has(els('#nodes').innerHTML, 'Read the field back', 'This step comes from the parts library', 'k-check');
  out.render.part = has(insp(), 'Read the field back', 'From the part', 'data-act="part-edit"', 'data-act="detach"', 'Text matches the draft');
  out.render.partHasNoEditButton = !insp().includes('data-act="edit"');
  toasts.length = 0; pb.ACT.edit();
  out.render.editOnPart = {editing: S.editing, toast: toasts.join(' ')};
  pb.ACT.detach();
  const detached = pb.nodeById(S.flows.posting, 'posting-readback');
  out.render.detached = {part: 'part' in detached, title: detached.title, inspectorHasEdit: insp().includes('data-act="edit"')};
  await settled('posting');
  out.render.detachedOnDisk = (await disk()).data.flows.posting.nodes.find(n => n.id === 'posting-readback').title;
  out.pages = {};
  for (const v of ['home', 'accounts', 'folders', 'parts']) { S.view = v; pb.renderPage(); pb.renderBar(); out.pages[v] = els('#page').innerHTML.length; }
  S.view = 'parts'; pb.renderPage();
  out.pages.partsText = has(els('#page').innerHTML, 'Read the field back', 'Not used by any step yet.');
  S.view = 'accounts'; pb.renderPage();
  out.pages.accountsText = has(els('#page').innerHTML, '@sam', 'changed outside');
  out.status = status();

  // 9. the forms: open each one, and submit the account and folder forms the way a person would
  const modalHtml = () => els('#modalBody').innerHTML;
  const fill = values => { for (const [k, v] of Object.entries(values)) els('#modalBody #f_' + k).value = v; };
  const submit = () => els('#modalBody #mform').listeners.submit({preventDefault() {}});
  out.forms = {};
  S.view = 'flow'; S.flowId = 'welcome'; S.sel = new Set(); S.editing = null;
  pb.ACT['flow-settings'](); out.forms.flow = has(modalHtml(), 'Flow settings', '<option value="Rules &amp; QC"', 'value="Welcome"');
  pb.ACT['flow-new'](); out.forms.newFlow = has(modalHtml(), 'New flow', 'Create flow');
  pb.addNode('goto', 0, 600); out.forms.gotoPicker = has(modalHtml(), 'Which flow does it open?', '<option value="posting"', '<option value="map"');
  out.forms.gotoNotAddedYet = S.flows.welcome.nodes.length;
  pb.addNode('account', 0, 600); out.forms.accountPicker = has(modalHtml(), 'Which account?', '<option value="x-sam"');
  pb.ACT['part-edit']({dataset: {id: 'part-read-back'}}); out.forms.part = has(modalHtml(), 'Edit part', 'Read what is in the box before posting it.');
  pb.ACT['acct-edit']({dataset: {id: 'x-sam'}});
  out.forms.account = has(modalHtml(), 'Edit account', '<option value="X" selected', '<option value="active" selected');
  fill({platform: 'X', handle: '@sam', label: 'Edited in the form', owner: 'Sam', purpose: 'Edited purpose.', status: 'paused', flow: 'posting', pacing: '', login: '', url: 'https://x.com/sam', projects: 'posting', notes: 'One note'});
  submit();
  await sleep(600);
  let acctDisk = (await disk()).data.accounts['x-sam'];
  out.forms.accountSaved = {label: acctDisk.label, status: acctDisk.status, origin: acctDisk.origin, notes: acctDisk.notes, hasId: 'id' in acctDisk};
  S.folders['helper-scripts'].updatedAt = '2026-01-01T00:00:00Z';   // a field the folder form does not show
  pb.ACT['folder-edit']({dataset: {id: 'helper-scripts'}});
  out.forms.folder = has(modalHtml(), 'Edit folder', '<option value="scripts" selected');
  fill({label: 'Helper scripts', path: 'C:\\fixture\\scripts2', kind: 'scripts', note: 'Edited note'});
  submit();
  await sleep(600);
  const folderDisk = (await disk()).data.folders['helper-scripts'];
  out.forms.folderSaved = {path: folderDisk.path, note: folderDisk.note, updatedAt: folderDisk.updatedAt, hasId: 'id' in folderDisk};
  out.forms.status = status();

  // 10. a form whose save fails because the server is down stays open with what was typed, says so, and saves on the next try
  const modalEl = els('#modal'), bannerEl = els('#banner');
  pb.ACT['acct-edit']({dataset: {id: 'x-sam'}});
  fill({platform: 'X', handle: '@sam', label: 'Typed while the server was down', owner: 'Sam', purpose: 'Edited purpose.', status: 'paused', flow: 'posting', pacing: '', login: '', url: 'https://x.com/sam', projects: 'posting', notes: 'One note'});
  target = 'http://127.0.0.1:' + (deadPort || 9); toasts.length = 0;
  submit(); await sleep(600);
  out.offlineForm = {open: !modalEl.hidden, inPage: S.accounts['x-sam'].label, toast: toasts.join(' | '), banner: !bannerEl.hidden};
  target = base;
  submit(); await sleep(600);
  out.offlineForm.afterRetry = {open: !modalEl.hidden, onDisk: (await disk()).data.accounts['x-sam'].label, banner: !bannerEl.hidden};

  // 10b. the account changes elsewhere while its form is open: the save is refused, the newer version is loaded,
  // the form stays open with what was typed, and saving again replaces it
  pb.ACT['acct-edit']({dataset: {id: 'x-sam'}});
  fill({platform: 'X', handle: '@sam', label: 'Typed during a conflict', owner: 'Sam', purpose: 'Edited purpose.', status: 'paused', flow: 'posting', pacing: '', login: '', url: 'https://x.com/sam', projects: 'posting', notes: 'One note'});
  let dd = await disk();
  const theirAcct = JSON.parse(JSON.stringify(dd.data.accounts['x-sam'])); theirAcct.label = 'Changed by someone else'; theirAcct.origin = 'kept from their version';
  await outside('PUT', '/api/accounts/x-sam', theirAcct, {'If-Match': dd.revs.accounts['x-sam']});
  toasts.length = 0;
  submit(); await sleep(600);
  out.conflictForm = {open: !modalEl.hidden, inPage: S.accounts['x-sam'].label, onDisk: (await disk()).data.accounts['x-sam'].label, toast: toasts.join(' | ')};
  submit(); await sleep(600);
  dd = await disk();
  out.conflictForm.afterSaveAgain = {open: !modalEl.hidden, onDisk: dd.data.accounts['x-sam'].label, origin: dd.data.accounts['x-sam'].origin};

  // 11. unsaved work is known (it drives the warning before the tab closes)
  S.flows.welcome.summary = 'pending for a moment';
  pb.touchFlow('welcome');
  out.pending = {whileDirty: pb.pendingWork()};
  await settled('welcome');
  out.pending.afterSave = pb.pendingWork();

  // 12. kindOf is view(n).kind, without building the view
  const kinds = []; for (const f of Object.values(S.flows)) for (const n of f.nodes) kinds.push(pb.kindOf(n) === pb.view(n).kind);
  kinds.push(pb.kindOf({id: 'x', part: 'no-such-part', x: 0, y: 0}) === 'step', pb.kindOf({id: 'y', kind: 'weird', x: 0, y: 0}) === 'step');
  out.kindOf = kinds;

  // 13. fit: a flow that fits readably is shown whole; a very wide one opens readable from its left edge, and fitting again shows all of it
  S.view = 'flow'; S.flowId = 'posting'; delete S.cams.posting;
  out.fit = {small: pb.fit(), smallZ: pb.cam().z};
  S.flows.widefit = pb.normFlow('widefit', {name: 'Wide', group: 'Start here', icon: 'map', summary: '', order: 1, nodes: Array.from({length: 12}, (_, i) => ({id: 'w' + i, kind: 'step', title: 'S' + i, x: i * 340, y: 0})), edges: []});
  S.flowId = 'widefit';
  out.fit.wide = pb.fit(); out.fit.wideZ = pb.cam().z; out.fit.wideX = pb.cam().x;
  pb.fitKey(); out.fit.again = pb.cam().auto; out.fit.againZ = +pb.cam().z.toFixed(3);
  pb.fitKey(); out.fit.thirdZ = pb.cam().z;
  delete S.flows.widefit; delete S.cams.widefit; S.flowId = 'posting';

  // 14. deleting an account asks why, and the step that showed it on a canvas becomes an ordinary step with its text
  S.view = 'accounts'; toasts.length = 0;
  const del = pb.ACT['acct-del']({dataset: {id: 'x-sam'}});
  out.acctDel = {asked: has(modalHtml(), 'Delete the account “@sam”?', '1 step shows it on a canvas')};
  els('#modalBody #rWhy').value = 'Replaced by a new account';
  els('#modalBody #rform').listeners.submit({preventDefault() {}});
  await del; await settled('posting');
  d = await disk();
  const was = d.data.flows.posting.nodes.find(n => n.id === 'posting-account');
  out.acctDel.gone = !('x-sam' in d.data.accounts) && !('x-sam' in S.accounts);
  out.acctDel.node = {kind: was.kind, title: was.title, account: 'account' in was};
  out.acctDel.toast = toasts.join(' | ');
  out.acctDel.status = status();

  process.stdout.write(JSON.stringify(out));
  process.exit(0);
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
