// Run with: node tests/test_media_library_cards.js
// Card actions, upload blocking reasons, status counters, fill queue and drop handling.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const store = new Map();
function node(selector) {
  if (!store.has(selector)) {
    const element = {
      selector, value: '', content: '', props: {}, classes: new Set(),
      val(v) { if (v === undefined) return this.value; this.value = v; return this; },
      text(v) { if (v === undefined) return this.content; this.content = String(v); return this; },
      html(v) { if (v === undefined) return this.content; this.content = String(v); return this; },
      prop(k, v) { if (v === undefined) return this.props[k]; this.props[k] = v; return this; },
      addClass(c) { this.classes.add(c); return this; },
      removeClass(c) { this.classes.delete(c); return this; },
      toggleClass(c, on) { (on ? this.classes.add(c) : this.classes.delete(c)); return this; },
      attr() { return this; }, empty() { this.content = ''; return this; },
      trigger() { return this; },
      modal(state) { this.modalState = state; return this; }
    };
    store.set(selector, element);
  }
  return store.get(selector);
}
const $ = selector => node(selector);
$.ajax = () => {};
const windowListeners = {};
const context = {
  $, console, Set, Array, JSON, Math, Number, String, Object, parseInt,
  document: {getElementById: () => null},
  window: {addEventListener(type, callback) { (windowListeners[type] = windowListeners[type] || []).push(callback); }},
  NProgress: {start() {}, done() {}},
  SubtitleTasks: {formatBytes: v => `${v} B`, randomRequestId: () => 'id'},
  show_fail_modal(message) { context.failure = message; }
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('web/static/js/media-library.js', 'utf8'), context);

const movie = (overrides = {}) => Object.assign({
  id: 'm1', title: 'Movie', media_type: 'movie', media_type_name: '电影', category: '外语电影',
  year: '2024', linked: true, target_path: '/media/Movie.mkv', can_upload: true,
  subtitle_status: 'missing_chinese', subtitle_label: '缺中文字幕', poster_url: '/library/image/m1'
}, overrides);
const card = item => context.library_item_card(item, 0);
const primaryLabel = html => html.match(/class="btn btn-sm (btn-[a-z-]+) flex-fill"[^>]*>([^<]+)</);

// Each state gets one primary action; the rest sits in the always-visible menu.
let html = card(movie());
let primary = primaryLabel(html);
assert.equal(primary[2], '找字幕');
assert.equal(primary[1], 'btn-primary');
assert.ok(html.includes('open_library_online_movie(&quot;m1&quot;)'));
assert.ok(html.includes('aria-label="更多操作：Movie"'));
assert.ok(html.includes('data-bs-toggle="dropdown"'));
const menu = html.slice(html.indexOf('dropdown-menu'));
assert.ok(menu.includes('打开详情') && menu.includes('上传字幕'));
assert.ok(!menu.includes('>在线字幕<'), 'the primary action must not be repeated in the menu');

html = card(movie({subtitle_status: 'has_chinese_external', subtitle_audit_status: 'warning',
  subtitle_audit_label: '外挂字幕语言需规范', subtitle_audit_badge: 'bg-yellow-lt'}));
primary = primaryLabel(html);
assert.equal(primary[2], '修复');
assert.ok(html.includes('repair_library_subtitles(&quot;m1&quot;, this)'));

html = card(movie({subtitle_status: 'has_chinese_internal'}));
assert.equal(primaryLabel(html)[2], '管理');
assert.ok(!html.includes('二次处理'));

// A movie that cannot take uploads explains why instead of an unexplained disabled button.
html = card(movie({linked: false, target_path: '', can_upload: false}));
assert.match(html, /<button[^>]*flex-fill"\s+disabled[^>]*title="没有整理记录/);
assert.ok(html.includes('disabled>上传字幕<span class="d-block small text-muted">没有整理记录'));
assert.equal(context.library_upload_block_reason(movie({linked: true, target_path: '', can_upload: false})), '整理记录缺少目标媒体文件');
assert.ok(html.includes('不可上传：没有整理记录'), 'the reason is visible without hovering');
assert.equal(context.library_upload_block_label(movie({can_upload: false})), '媒体文件不存在');
assert.ok(!card(movie()).includes('不可上传'));
assert.match(context.library_upload_block_reason(movie({can_upload: false})), /不存在或已移动/);
assert.equal(context.library_upload_block_reason(movie()), '');
assert.equal(context.library_upload_block_reason({media_type: 'tv', can_upload: false}), '');

// Series open the drawer's episode list; coverage is visible and accessible.
html = card({id: '/library/metadata/5', title: 'Show', media_type: 'tv', media_type_name: '电视剧',
  category: '欧美剧', subtitle_status: 'unknown', subtitle_label: '已覆盖 2/4 集', episode_total: 4, episode_covered: 2});
primary = primaryLabel(html);
assert.equal(primary[2], '找字幕');
assert.ok(html.includes('open_library_drawer(&quot;/library/metadata/5&quot;, &quot;episodes&quot;)'));
assert.ok(html.includes('role="progressbar"') && html.includes('aria-valuenow="2"') && html.includes('width:50%'));
assert.ok(!html.includes('open_library_online_movie'));

// Poster is a labelled button; hostile titles are escaped everywhere they are interpolated.
html = card(movie({title: '"><img src=x onerror=alert(1)>'}));
assert.ok(html.includes('<button type="button" class="lit-library-card-poster'));
assert.ok(!html.includes('<img src=x'));
assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'));

// Status counters: tab labels never show a stale number.
context.library_render_summary({all: 1280, missing: 86, unknown: 0, problem: 12, ok: 968});
assert.equal(node('[data-library-count="all"]').content, (1280).toLocaleString('zh-CN'));
assert.equal(node('[data-library-count="unknown"]').content, '0');
context.library_render_summary(null);
assert.equal(node('[data-library-count="missing"]').content, '–');

// Audio language only applies to movies: series types reset and lock the control.
$('#library_filter_type').val('movie');
$('#library_filter_audio').val('eng');
context.update_library_audio_control();
assert.equal($('#library_filter_audio').val(), 'eng');
assert.equal($('#library_filter_audio').prop('disabled'), false);
$('#library_filter_type').val('tv');
context.update_library_audio_control();
assert.equal($('#library_filter_audio').val(), 'all');
assert.equal($('#library_filter_audio').prop('disabled'), true);
$('#library_filter_type').val('all');
$('#library_filter_audio').val('eng');
$('#library_filter_keyword').val(' Movie ');
assert.deepEqual(JSON.parse(JSON.stringify(context.library_summary_data())),
  {type: 'all', category: '', keyword: 'Movie', audio_language: 'eng'});
assert.equal(context.library_filter_data(2).audio_language, 'eng');

// Continuous fill walks a fixed queue, counts saves and skips, and ends cleanly.
const opened = [];
context.open_library_online_item = item => opened.push(item.id);
context.load_library_items = () => {};
context.library_view_instance = {};
context.library_page = 1;
context.library_fill = {queue: [{id: 'a'}, {id: 'b'}, {id: 'c'}], index: 0, saved: 0, skipped: 0, current_saved: false};
context.library_fill_open(0);
assert.deepEqual(opened, ['a']);
assert.equal($('#library_fill_next').prop('disabled'), true);
const fill = context.library_fill;
const first = fill.queue[0];
context.library_fill_note_saved(fill, first);
context.library_fill_note_saved(fill, first); // a repeated save of the same movie is counted once
assert.equal(context.library_fill.saved, 1);
assert.equal($('#library_fill_next').prop('disabled'), false);
context.library_fill_advance(true);
context.library_fill_advance(false); // skip b
assert.equal(context.library_fill.skipped, 1);
assert.deepEqual(opened, ['a', 'b', 'c']);
assert.equal($('#library_fill_skip').content, '跳过并结束');
context.library_fill_advance(false);
assert.equal(context.library_fill, null);
assert.match(context.library_refresh_notice, /补齐完成：保存 1 部，跳过 2 部/);
context.library_fill_exit(); // a second exit is harmless
context.library_fill_note_saved(fill, first);

// Drops are ignored outside the upload area, and only while the library page is alive.
const zone = {contains: target => target === 'inside', classList: {contains: () => false, toggle() {}, remove() {}},
  addEventListener() {}};
let zoneInDom = zone;
context.document.getElementById = id => (id === 'library_upload_dropzone' ? zoneInDom : null);
context.init_library_dropzone();
context.init_library_dropzone();
assert.equal(windowListeners.dragover.length, 1, 'window listeners are installed once');
assert.equal(windowListeners.drop.length, 1);
const drop = target => {
  const event = {target, dataTransfer: {types: ['Files'], dropEffect: 'copy'}, prevented: false,
    preventDefault() { this.prevented = true; }};
  windowListeners.drop[0](event);
  return event;
};
assert.equal(drop('outside').prevented, true);
assert.equal(drop('outside').dataTransfer.dropEffect, 'none');
assert.equal(drop('inside').prevented, false);
const text = {target: 'outside', dataTransfer: {types: ['text/plain']}, preventDefault() { this.prevented = true; }};
windowListeners.drop[0](text);
assert.equal(text.prevented, undefined, 'non-file drags are left alone');
zoneInDom = null; // left the library page
assert.equal(drop('outside').prevented, false);
zoneInDom = zone;
context.library_view_instance = null;
assert.equal(drop('outside').prevented, false);

console.log('Media library card, counter, fill and drop tests passed');
