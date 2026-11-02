const ROOT = '/wardrobe-admin';
const level = ['low', 'medium', 'high'];
export const LABEL_SPEC = {
  brand: ['品牌', 'text', 100], model: ['款式型号', 'text', 100], subcategory: ['细分类', 'text', 100], primary_color: ['主色', 'text', 60],
  secondary_colors: ['辅色', 'list', 5], color_distribution: ['颜色分布', 'text', 300], lightness: ['明度', ['light', 'medium', 'dark']], saturation: ['饱和度', level],
  pattern: ['图案', 'list', 5], pattern_density: ['图案密度', level], visual_focus: ['视觉焦点', 'text', 300], material: ['材质', 'text', 300],
  composition: ['成分', 'text', 300], texture: ['纹理', 'text', 200], thickness: ['厚度', ['thin', 'medium', 'thick', 'very_thick']], warmth: ['保暖度', level],
  breathability: ['透气度', level], windproof: ['防风', 'boolean'], waterproof: ['防水', 'boolean'], silhouette: ['轮廓', 'text', 100],
  length: ['衣长', 'text', 100], sleeve: ['袖型', 'text', 100], neckline: ['领型', 'text', 100], closure: ['开合', 'text', 100],
  fit: ['版型', 'text', 100], shoe_shape: ['鞋型', 'text', 100], style: ['风格', 'list', 5], formality: ['正式度', ['casual', 'smart_casual', 'formal']],
  occasions: ['场合', 'list', 8], layering: ['叠穿', 'list', 5], seasons: ['季节', 'list', 4], weather_notes: ['天气补充', 'text', 300],
  size: ['尺码', 'text', 100], fit_feedback: ['上身反馈', 'text', 500], preference: ['个人偏好', 'text', 500], notes: ['备注', 'text', 1000],
};
export function formatTime(seconds) {
  return seconds == null ? '未知' : new Intl.DateTimeFormat('zh-CN', {timeZone: 'UTC', dateStyle: 'medium', timeStyle: 'medium'}).format(new Date(seconds * 1000)) + ' UTC';
}
export function parseValue(spec, raw) {
  if (spec[1] === 'boolean') {
    if (!['true', 'false'].includes(raw)) throw new Error('请选择是或否');
    return raw === 'true';
  }
  if (spec[1] === 'list') {
    const values = raw === '' ? [] : raw.split('\n');
    if (values.length > spec[2]) throw new Error(`最多 ${spec[2]} 项，每行一项`);
    return values;
  }
  if (Array.isArray(spec[1]) && !spec[1].includes(raw)) throw new Error('请选择有效值');
  if (spec[1] === 'text' && raw.length > spec[2]) throw new Error(`最多 ${spec[2]} 个字符`);
  return raw;
}
const messages = {401: '登录信息已失效，请重新使用现有账密登录', 403: '无权操作或来源校验失败', 409: '记录或目录已变化，请刷新后重新确认', 422: '输入不合法，请检查字段', 429: '请求过于频繁，请稍后重试', 502: '衣橱服务暂不可用，写入结果可能未知', 503: '后台服务授权或数据服务暂不可用，请维护后重试', 504: '衣橱服务超时，写入结果可能未知'};
const codes = {REVISION_CONFLICT: '记录已被修改，请对照最新值重新确认', RESTORE_UNAVAILABLE: '恢复期已结束或图片不满足恢复条件', CSRF_INVALID: '安全令牌已过期', CURSOR_STALE: '目录已变化，请从首页重新加载', ASSET_UNAVAILABLE: '图片不可用', PREVIEW_BUSY: '预览暂不可用，请稍后重试'};

if (typeof document !== 'undefined') {
  const $ = id => document.getElementById(id);
  const node = (tag, text) => { const result = document.createElement(tag); if (text !== undefined) result.textContent = text; return result; };
  let csrf = '', listCursor = null, categoryCursor = null, listRevision = null, categoryRevision = null, record = null, revision = null, pending = null, latestConflict = null, busy = false, previousFocus = null, pageEpoch = 0, detailEpoch = 0, serverClockOffset = 0;
  const fields = new Map();
  const serverNow = () => Math.floor((Date.now() + serverClockOffset) / 1000);
  const display = value => value === null || value === undefined ? '未知' : JSON.stringify(value);
  const viewQuery = () => new URLSearchParams({state: $('state').value});
  async function api(path, options = {}) {
    let response;
    try { response = await fetch(ROOT + path, {credentials: 'same-origin', cache: 'no-store', ...options}); }
    catch { throw Object.assign(new Error('网络读取失败，写入结果可能未知；请保留原操作编号重试'), {unknown: true}); }
    let payload;
    try { payload = await response.json(); } catch { payload = {}; }
    if (!response.ok) {
      const code = payload.error?.code;
      throw Object.assign(new Error(codes[code] || messages[response.status] || '请求失败，请稍后重试'), {status: response.status, code});
    }
    return payload.data || payload;
  }
  async function session() { csrf = (await api('/api/session')).csrf_token; }
  function imageFor(item, large = false) {
    if (item.state === 'deleted') return node('p', '已删除，恢复后可查看图片');
    const ref = item.image_references?.[0];
    if (!ref) return node('p', '无图');
    if (item.image_access_status === 'missing') return node('p', '图片不可用');
    const image = node('img');
    image.src = ref.preview_url;
    image.alt = item.name || '衣物预览';
    image.loading = 'lazy';
    const unavailable = node('p', '图片不可用或预览暂不可用');
    image.addEventListener('error', () => image.replaceWith(unavailable));
    if (!large) return image;
    const wrapper = node('div'); wrapper.append(image);
    const open = node('button', '查看原图'); open.type = 'button';
    if (['image/heic', 'image/heif'].includes(ref.mime)) wrapper.append(node('p', '原图为 HEIC／HEIF，浏览器可能无法显示，请使用预览。'));
    open.addEventListener('click', () => { unavailable.replaceWith(image); image.src = ref.content_url; }); wrapper.append(open); return wrapper;
  }
  function card(item) {
    const card = node('article');
    card.append(imageFor(item), node('h2', item.name), node('p', item.category ?? '未分类'), node('p', '入库时间：' + formatTime(item.created_at)));
    if (item.state === 'deleted') card.dataset.expires = String(item.restore_until);
    if (item.state === 'deleted') card.append(node('p', '恢复截止：' + formatTime(item.restore_until)));
    const open = node('button', '查看详情'); open.type = 'button';
    open.addEventListener('click', () => openDetail(item.id)); card.append(open); return card;
  }
  async function loadList(reset = true) {
    const epoch = pageEpoch;
    if (reset) { listCursor = null; listRevision = null; $('cards').replaceChildren(); }
    const query = viewQuery(); query.set('sort', 'created_at'); query.set('order', $('order').value);
    if ($('category').value) query.set('category', $('category').value);
    if (listCursor) query.set('cursor', listCursor);
    $('more').disabled = true; $('status').textContent = '正在加载衣物…';
    try {
      const data = await api('/api/garments?' + query);
      if (epoch !== pageEpoch) return;
      if (listRevision !== null && data.catalog_revision !== listRevision) throw Object.assign(new Error(codes.CURSOR_STALE), {code: 'CURSOR_STALE'});
      if (Number.isInteger(data.server_now)) serverClockOffset = data.server_now * 1000 - Date.now();
      listRevision = data.catalog_revision; listCursor = data.next_cursor;
      for (const item of data.items) $('cards').append(card(item));
      $('more').hidden = !data.has_more;
      $('status').textContent = $('cards').children.length ? `已加载 ${$('cards').children.length} 件${data.has_more ? '，可继续翻页' : '，当前查询已读完'}` : '当前查询没有衣物';
    } catch (error) {
      if (epoch !== pageEpoch) return;
      if (error.code === 'CURSOR_STALE') { listCursor = null; listRevision = null; $('cards').replaceChildren(); $('more').hidden = true; }
      $('status').textContent = error.message + '。可点击重新加载';
    } finally { $('more').disabled = false; }
  }
  async function loadCategories(reset = true) {
    const epoch = pageEpoch;
    const selected = $('category').value;
    if (reset) { categoryCursor = null; categoryRevision = null; $('category').replaceChildren(new Option('全部分类', '')); if (selected) { $('category').append(new Option(selected, selected)); $('category').value = selected; } }
    const query = viewQuery(); query.set('limit', '100'); if (categoryCursor) query.set('cursor', categoryCursor);
    try {
      const data = await api('/api/categories?' + query);
      if (epoch !== pageEpoch) return;
      if (categoryRevision !== null && data.catalog_revision !== categoryRevision) throw new Error('分类目录已变化，请重新加载');
      categoryRevision = data.catalog_revision;
      for (const category of data.items) if (![...$('category').options].some(option => option.value === category)) $('category').append(new Option(category, category));
      categoryCursor = data.next_cursor; $('more-categories').hidden = !data.has_more;
      $('more-categories').textContent = '加载更多分类';
    } catch (error) {
      $('status').textContent = error.message; $('more-categories').hidden = false; $('more-categories').textContent = '重试分类加载';
    }
  }
  function addField(key, title, spec, value, nullable = true, source = '') {
    const box = node('fieldset'); box.append(node('legend', title));
    const mode = node('select'); mode.id = 'mode-' + key;
    mode.append(new Option('未修改', 'keep'), new Option('设置值', 'set'));
    if (nullable) mode.append(new Option('明确置未知（null）', 'null'));
    const modeLabel = node('label', '修改方式'); modeLabel.htmlFor = mode.id; modeLabel.append(mode); box.append(modeLabel);
    let input;
    if (Array.isArray(spec[1]) || spec[1] === 'boolean') {
      input = node('select');
      input.append(new Option('请选择', ''));
      for (const option of spec[1] === 'boolean' ? ['true', 'false'] : spec[1]) input.append(new Option(option, option));
      input.value = value == null ? '' : String(value);
    } else {
      input = node(spec[1] === 'list' || spec[2] > 100 ? 'textarea' : 'input');
      if (spec[1] === 'text') input.maxLength = spec[2];
      input.value = Array.isArray(value) ? value.join('\n') : value ?? '';
    }
    input.id = 'value-' + key; const valueLabel = node('label', spec[1] === 'list' ? `每行一项，空白表示空数组；最多 ${spec[2]} 项` : '字段值');
    valueLabel.htmlFor = input.id; valueLabel.append(input); box.append(valueLabel);
    if (source) box.append(node('small', source));
    input.addEventListener('input', () => { mode.value = 'set'; updateSave(); });
    mode.addEventListener('change', updateSave);
    fields.set(key, {mode, input, spec, original: value}); $('editor').append(box);
  }
  function collectPatch() {
    const patch = {}, labels = {};
    for (const [key, field] of fields) {
      if (field.mode.value === 'keep') continue;
      const value = field.mode.value === 'null' ? null : parseValue(field.spec, field.input.value);
      if (JSON.stringify(value) === JSON.stringify(field.original)) continue;
      if (key.startsWith('annotation_fields.')) labels[key.split('.')[1]] = value;
      else patch[key] = value;
    }
    if (Object.keys(labels).length) patch.annotation_fields = labels;
    if ('name' in patch && (!patch.name?.trim() || patch.name.length > 200)) throw new Error('名称需为 1–200 个字符，不能全为空白');
    if ('category' in patch && patch.category !== null && (!patch.category.trim() || patch.category.length > 100)) throw new Error('分类需为 1–100 个字符，或明确置未知');
    if (patch.roles?.some(value => !['upper', 'lower', 'footwear', 'outerwear', 'base_layer', 'accessory', 'one_piece'].includes(value))) throw new Error('roles 中存在不支持的值');
    return patch;
  }
  function updateSave() {
    try { $('save').disabled = busy || pending !== null || latestConflict !== null || !Object.keys(collectPatch()).length; }
    catch { $('save').disabled = false; }
  }
  function populateRecord(item) {
    record = item; revision = item.revision; fields.clear(); $('record').replaceChildren(); $('editor').replaceChildren();
    $('record').append(imageFor(item, true), node('p', `当前版本：${item.revision}；入库时间：${formatTime(item.created_at)}`));
    if (item.state === 'deleted') $('record').append(node('p', '删除时间：' + formatTime(item.deleted_at) + '；恢复截止：' + formatTime(item.restore_until)));
    $('record').append(node('h3', '只读商品来源与货号'), node('pre', JSON.stringify({identity_fields: item.identity_fields, product_reference: item.product_reference, product_evidence_summary: item.product_evidence_summary, product_evidence: item.product_evidence}, null, 2)));
    const active = item.state === 'active';
    $('save').hidden = $('delete').hidden = $('editor').hidden = !active; $('restore').hidden = active;
    $('restore').disabled = !active && item.restore_until <= serverNow();
    if (active) {
      addField('name', '名称', ['名称', 'text', 200], item.name, false);
      addField('category', '分类', ['分类', 'text', 100], item.category);
      addField('roles', '穿搭位置', ['穿搭位置', 'list', 8], item.roles, false);
      addField('availability', '可用状态', ['可用状态', ['available', 'unavailable', 'unknown']], item.availability, false);
      for (const [key, spec] of Object.entries(LABEL_SPEC)) {
        const field = item.annotation_fields[key];
        addField('annotation_fields.' + key, spec[0], spec, field?.value ?? null, true, `来源：${field?.source_kind ?? 'unknown'}；核实：${field?.verification_state ?? 'unknown'}；证据：${JSON.stringify(field?.evidence ?? [])}`);
      }
    }
    $('detail-title').textContent = item.name; updateSave();
  }
  async function openDetail(id) {
    const epoch = ++detailEpoch; record = null; revision = null; fields.clear();
    $('save').disabled = $('delete').disabled = $('restore').disabled = true;
    previousFocus = document.activeElement; pending = null; latestConflict = null; $('conflict').textContent = ''; $('accept-revision').hidden = $('retry').hidden = true;
    if (!$('detail').open) $('detail').showModal();
    $('detail-status').textContent = '读取详情…'; $('record').replaceChildren(); $('editor').replaceChildren(); $('save').disabled = true;
    try { const data = await api('/api/garments/' + id + '?' + viewQuery()); if (epoch !== detailEpoch || !$('detail').open) return; populateRecord(data.garment); $('delete').disabled = false; $('restore').disabled = data.garment.state === 'deleted' && data.garment.restore_until <= serverNow(); $('detail-status').textContent = ''; }
    catch (error) { if (epoch === detailEpoch) $('detail-status').textContent = error.message; }
  }
  function confirmation(title, description, action) {
    $('confirm-title').textContent = title; $('confirm-body').textContent = description; $('confirm').showModal();
    $('confirm-yes').onclick = () => { $('confirm').close(); action(); };
  }
  function diff(patch) {
    const result = {};
    for (const [key, value] of Object.entries(patch)) {
      if (key === 'annotation_fields') {
        for (const [label, newValue] of Object.entries(value)) result['标注.' + label] = {原值: record.annotation_fields[label]?.value ?? null, 新值: newValue};
      } else result[key] = {原值: record[key], 新值: value};
    }
    return JSON.stringify(result, null, 2);
  }
  function newAction(operation, patch) {
    if (!record || busy || pending) return;
    pending = Object.freeze({key: crypto.randomUUID(), id: record.id, operation, revision, body: JSON.stringify({base_revision: revision, ...(patch ? {patch} : {})})});
    perform();
  }
  async function perform() {
    if (busy || !pending) return;
    busy = true; updateSave(); $('delete').disabled = $('restore').disabled = $('retry').disabled = true;
    const frozen = pending;
    try {
      let data;
      const send = () => api('/api/garments/' + frozen.id + (frozen.operation === 'update' ? '' : '/' + frozen.operation), {method: frozen.operation === 'update' ? 'PATCH' : 'POST', headers: {'Content-Type': 'application/json', 'Idempotency-Key': frozen.key, 'X-CSRF-Token': csrf}, body: frozen.body});
      try { data = await send(); }
      catch (error) { if (error.code !== 'CSRF_INVALID') throw error; await session(); data = await send(); }
      pending = null; $('retry').hidden = true;
      $('detail-status').textContent = `写入已成功，版本 ${data.revision}，回执 ${data.receipt_id}。`;
      const state = data.state === 'deleted' ? 'deleted' : 'active';
      try {
        const fresh = await api('/api/garments/' + frozen.id + '?state=' + state); populateRecord(fresh.garment);
        await loadList(true); await loadCategories(true);
      } catch { $('detail-status').textContent += ' 重新读取失败，请手动刷新核对；不要重复创建操作。'; }
    } catch (error) {
      $('detail-status').textContent = error.message;
      if (error.status === 409 && error.code === 'REVISION_CONFLICT') {
        pending = null;
        try {
          latestConflict = (await api('/api/garments/' + frozen.id + '?state=' + record.state)).garment;
          $('conflict').textContent = '未提交的输入已保留。最新记录：\n' + JSON.stringify(latestConflict, null, 2);
          $('accept-revision').hidden = false;
        } catch { latestConflict = true; $('accept-revision').hidden = true; $('conflict').textContent = '无法读取最新记录，请保留当前输入并重新读取详情。'; }
      } else if (error.status && error.status < 500 && error.status !== 429) { pending = null; }
      $('retry').hidden = pending === null;
    } finally { busy = false; $('delete').disabled = $('restore').disabled = $('retry').disabled = false; updateSave(); }
  }
  $('filters').addEventListener('submit', event => event.preventDefault());
  $('editor').addEventListener('submit', event => event.preventDefault());
  $('state').addEventListener('change', () => { pageEpoch++; $('category').value = ''; loadCategories(true); loadList(true); });
  for (const id of ['category', 'order']) $(id).addEventListener('change', () => { pageEpoch++; loadList(true); });
  $('reload').onclick = () => { pageEpoch++; loadCategories(true); loadList(true); };
  $('more').onclick = () => loadList(false);
  $('more-categories').onclick = () => loadCategories(categoryCursor === null);
  $('close-detail').onclick = () => { if (!busy) $('detail').close(); };
  $('detail').addEventListener('close', () => { detailEpoch++; previousFocus?.focus(); });
  $('detail').addEventListener('cancel', event => { if (busy) event.preventDefault(); });
  $('confirm-no').onclick = () => $('confirm').close();
  $('save').onclick = () => {
    if (!record || busy || pending || latestConflict) return;
    try { const patch = collectPatch(); if (!Object.keys(patch).length) return; confirmation('确认保存：' + record.name, `版本 ${revision}\n${diff(patch)}`, () => newAction('update', patch)); }
    catch (error) { $('detail-status').textContent = error.message; }
  };
  $('delete').onclick = () => {
    if (!record || busy || pending) return;
    confirmation('确认软删除：' + record.name, `当前版本 ${revision}。删除后不再用于目录和推荐；保留 30×86400 秒恢复期，不物理销毁。${!record.image_references?.length || record.image_access_status === 'missing' ? '该衣物无图或原图缺失，现行恢复规则可能使其无法恢复。' : ''}`, () => newAction('delete'));
  };
  $('restore').onclick = async () => {
    if (!record || busy || pending) return;
    try { const fresh = await api('/api/garments/' + record.id + '?state=deleted'); if (fresh.garment.revision !== revision) { $('detail-status').textContent = '记录已变化，请重新打开详情'; return; } confirmation('确认恢复：' + record.name, `当前版本 ${revision}；截止 ${formatTime(record.restore_until)}。恢复由服务端再次检查期限和原图。`, () => newAction('restore')); }
    catch (error) { $('detail-status').textContent = error.message; }
  };
  $('accept-revision').onclick = () => {
    if (!latestConflict || latestConflict === true) return;
    const next = latestConflict;
    confirmation('重新确认最新版本', '保留输入并以最新记录作为对照。采用版本 ' + next.revision + ' 后仍需点击查看修改并保存。', () => {
      revision = next.revision; record = next;
      for (const [key, field] of fields) field.original = key.startsWith('annotation_fields.') ? next.annotation_fields[key.split('.')[1]]?.value ?? null : next[key];
      latestConflict = null; $('accept-revision').hidden = true; $('conflict').textContent = '已由本人选择最新版本，未自动保存。'; updateSave();
    });
  };
  $('retry').onclick = () => { if (pending) confirmation('重试原未决操作', `目标 ${pending.id}；版本 ${pending.revision}；动作 ${pending.operation}。保持原编号与正文，不使用后来表单输入。`, perform); };
  // Displayed recycle-bin cards expire without relying on a browser's time zone.
  setInterval(() => {
    if ($('state').value === 'deleted' && [...$('cards').children].some(card => Number(card.dataset.expires) <= serverNow())) loadList(true);
    if (record?.state === 'deleted' && record.restore_until <= serverNow()) { $('restore').disabled = true; $('detail-status').textContent = '恢复期已结束，请刷新回收站'; }
  }, 1000);
  session().catch(error => { $('status').textContent = error.message; });
  loadCategories(true); loadList(true);
}
