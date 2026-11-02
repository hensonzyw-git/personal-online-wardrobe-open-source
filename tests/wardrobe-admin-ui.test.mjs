// Execute the actual UI module against a minimal DOM and controlled fetch promises.
// This covers interaction state, not browser layout, focus, or Nginx authentication.
import assert from 'node:assert/strict';
import { test } from 'node:test';

class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.parentElement = null; this.handlers = {}; this.hidden = false; this.disabled = false; this.open = false; this.value = ''; this.textContent = ''; this.dataset = {}; }
  append(...children) {
    if (this.tagName === 'select' && !this.children.length && children.length) this.value = children[0].value;
    for (const child of children) {
      if (child.parentElement) child.parentElement.children.splice(child.parentElement.children.indexOf(child), 1);
      child.parentElement = this; this.children.push(child);
    }
  }
  replaceChildren(...children) { for (const child of this.children) child.parentElement = null; this.children = []; this.append(...children); }
  replaceWith(replacement) {
    const parent = this.parentElement;
    if (!parent) return;
    if (replacement.parentElement) replacement.parentElement.children.splice(replacement.parentElement.children.indexOf(replacement), 1);
    parent.children.splice(parent.children.indexOf(this), 1, replacement);
    this.parentElement = null; replacement.parentElement = parent;
  }
  addEventListener(name, callback) { (this.handlers[name] ??= []).push(callback); }
  dispatch(name) { for (const callback of this.handlers[name] ?? []) callback({preventDefault() {}}); }
  get options() { return this.children; }
  showModal() { this.open = true; }
  close() { this.open = false; this.dispatch('close'); }
  focus() {}
}

const gid1 = '00000000-0000-0000-0000-000000000011';
const gid2 = '00000000-0000-0000-0000-000000000012';
const garment = (id, name) => ({id, name, category: '测试分类', state: 'active', revision: 1, created_at: 1800000000, roles: ['upper'], availability: 'unknown', annotation_fields: {}, image_references: []});
const first = garment(gid1, '<script>synthetic text</script>');
const second = garment(gid2, '合成衣物二');
const response = (data, status = 200, code = null) => ({ok: status === 200, status, json: async () => code ? {error: {code}} : {data}});
const tick = () => new Promise(resolve => setImmediate(resolve));

await test('UI detail loading clears prior write target; stale response cannot overwrite current detail', async () => {
  const elements = new Map();
  const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  get('state').value = 'active'; get('order').value = 'desc';
  globalThis.document = {getElementById: get, createElement: tag => new Element(tag), activeElement: new Element('button')};
  globalThis.Option = class extends Element { constructor(text, value) { super('option'); this.textContent = text; this.value = value; } };
  const originalInterval = globalThis.setInterval;
  globalThis.setInterval = () => 1;
  const pending = [];
  const calls = [];
  globalThis.fetch = async (path, options) => {
    calls.push([path, options]);
    if (path.endsWith('/api/session')) return {ok: true, status: 200, json: async () => ({csrf_token: 'synthetic-csrf'})};
    if (path.includes('/api/categories?')) return response({items: ['测试分类'], next_cursor: null, has_more: false, catalog_revision: 1});
    if (path.includes('/api/garments?')) return response({items: [first, second], next_cursor: null, has_more: false, catalog_revision: 1, server_now: 1800000000});
    return new Promise(resolve => pending.push({path, resolve}));
  };
  try {
    await import('../ops/wardrobe-admin/ui/admin.js?interaction-test'); await tick();
    assert.equal(get('cards').children.length, 2);
    const card1 = get('cards').children[0];
    const card2 = get('cards').children[1];
    assert.equal(card1.children[1].textContent, '<script>synthetic text</script>');
    card1.children.at(-1).dispatch('click');
    assert.equal(get('delete').disabled, true);
    pending.shift().resolve(response({garment: first})); await tick();
    assert.equal(get('detail-title').textContent, first.name);
    assert.equal(get('delete').disabled, false);
    get('close-detail').onclick();
    card2.children.at(-1).dispatch('click');
    assert.equal(get('delete').disabled, true);
    get('delete').onclick();
    assert.equal(get('confirm').open, false, 'old target must not remain writable while the new detail is loading');
    pending.shift().resolve(response({}, 404, 'NOT_FOUND')); await tick();
    get('delete').onclick(); assert.equal(get('confirm').open, false);
    get('close-detail').onclick();
    card1.children.at(-1).dispatch('click');
    const stale = pending.shift();
    get('close-detail').onclick();
    card2.children.at(-1).dispatch('click');
    const current = pending.shift();
    current.resolve(response({garment: second})); await tick();
    stale.resolve(response({garment: first})); await tick();
    assert.equal(get('detail-title').textContent, second.name);
    get('delete').onclick();
    assert.equal(get('confirm').open, true);
    assert.match(get('confirm-title').textContent, /合成衣物二/);
    get('confirm-no').onclick();
    const nameBox = get('editor').children.find(box => box.children[0].textContent === '名称');
    const nameInput = nameBox.children[2].children[0];
    nameInput.value = '待保存的新名称'; nameInput.dispatch('input');
    get('save').onclick(); get('confirm-yes').onclick();
    const conflictWrite = pending.shift();
    assert.ok(conflictWrite.path.endsWith(gid2), conflictWrite.path + ' ' + get('detail-status').textContent);
    conflictWrite.resolve(response({}, 409, 'REVISION_CONFLICT')); await tick();
    pending.shift().resolve(response({}, 404, 'NOT_FOUND')); await tick();
    assert.equal(nameInput.value, '待保存的新名称', 'conflict must preserve unsaved input');
    assert.equal(get('save').disabled, true, 'failed conflict refresh must block another stale write');
    get('close-detail').onclick();
    card2.children.at(-1).dispatch('click'); pending.shift().resolve(response({garment: second})); await tick();
    const nextName = get('editor').children.find(box => box.children[0].textContent === '名称').children[2].children[0];
    nextName.value = '冻结正文'; nextName.dispatch('input');
    get('save').onclick(); get('confirm-yes').onclick();
    const originalCall = calls.at(-1);
    pending.shift().resolve(response({}, 503)); await tick();
    assert.equal(get('retry').hidden, false);
    nextName.value = '后来输入不能替换冻结正文'; nextName.dispatch('input');
    get('retry').onclick(); get('confirm-yes').onclick();
    const replayCall = calls.at(-1);
    assert.equal(replayCall[1].headers['Idempotency-Key'], originalCall[1].headers['Idempotency-Key']);
    assert.equal(replayCall[1].body, originalCall[1].body);
    pending.shift().resolve(response({revision: 2, state: 'active', receipt_id: 'synthetic-receipt'})); await tick();
    pending.shift().resolve(response({garment: {...second, revision: 2, name: '冻结正文'}})); await tick(); await tick();
    assert.match(get('detail-status').textContent, /写入已成功/);
    assert.equal(get('retry').hidden, true);
    assert.ok(calls.every(([path]) => path.startsWith('/wardrobe-admin/')));
  } finally {
    globalThis.setInterval = originalInterval;
    delete globalThis.document; delete globalThis.Option; delete globalThis.fetch;
  }
});

for (const previewFails of [false, true]) {
  await test(`UI original image remains visible and retryable after preview ${previewFails ? 'failure' : 'success'}`, async () => {
    const elements = new Map();
    const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
    get('state').value = 'active'; get('order').value = 'desc';
    const item = {...first, image_references: [{preview_url: '/wardrobe-admin/images/synthetic/preview', content_url: '/wardrobe-admin/images/synthetic/content', mime: 'image/png'}]};
    globalThis.document = {getElementById: get, createElement: tag => new Element(tag), activeElement: new Element('button')};
    globalThis.Option = class extends Element { constructor(text, value) { super('option'); this.textContent = text; this.value = value; } };
    const originalInterval = globalThis.setInterval;
    globalThis.setInterval = () => 1;
    globalThis.fetch = async path => {
      if (path.endsWith('/api/session')) return response({csrf_token: 'synthetic-csrf'});
      if (path.includes('/api/categories?')) return response({items: [], has_more: false, next_cursor: null, catalog_revision: 1});
      if (path.includes('/api/garments?')) return response({items: [item], has_more: false, next_cursor: null, catalog_revision: 1});
      return response({garment: item});
    };
    try {
      await import(`../ops/wardrobe-admin/ui/admin.js?image-test-${previewFails}`); await tick();
      get('cards').children[0].children.at(-1).dispatch('click'); await tick();
      const wrapper = get('record').children[0];
      const image = wrapper.children.find(child => child.tagName === 'img');
      const open = wrapper.children.find(child => child.tagName === 'button');
      assert.equal(image.src, item.image_references[0].preview_url);
      if (previewFails) {
        image.dispatch('error');
        assert.equal(wrapper.children.some(child => child.tagName === 'img'), false);
        assert.match(wrapper.children[0].textContent, /图片不可用/);
      }
      open.dispatch('click');
      assert.equal(image.src, item.image_references[0].content_url);
      assert.equal(wrapper.children.filter(child => child.tagName === 'img').length, 1, 'original must be attached to the visible detail');
      assert.equal(wrapper.children.some(child => child.tagName === 'p'), false, 'stale image error must be removed');
      image.dispatch('error');
      assert.match(wrapper.children[0].textContent, /图片不可用/);
      assert.equal(open.parentElement, wrapper, 'failed original must retain the retry control');
      open.dispatch('click');
      assert.equal(image.parentElement, wrapper, 'retry after an original failure must reattach the image');
      assert.equal(image.src, item.image_references[0].content_url);
      assert.equal(wrapper.children.filter(child => child.tagName === 'img').length, 1);
    } finally {
      globalThis.setInterval = originalInterval;
      delete globalThis.document; delete globalThis.Option; delete globalThis.fetch;
    }
  });
}
