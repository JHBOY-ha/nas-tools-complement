// Run with: node tests/test_media_library_history.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
let grid = {};
const elements = new Map();
const requests = [];
const timers = [];
const confirmations = [];
let saves = 0;
let failures = 0;
function $(selector) {
  if (!elements.has(selector)) elements.set(selector, {
    value: '', content: '',
    val() { return this.value; },
    prop() { return this; },
    removeClass() { return this; },
    one(event, callback) { this.hidden = callback; return this; },
    html(value) { this.content = value; return this; },
    text(value) { this.content = value; return this; }
  });
  return elements.get(selector);
}
$.ajax = options => requests.push(options);
const listeners = [];
const window = {history: {state: {extra: {other_view: 'preserved'}}},
  addEventListener(name, callback) { listeners.push({name, callback}); }};
const ctx = vm.createContext({$, window, document: {getElementById: () => grid, querySelectorAll: () => []},
  NProgress: {start() {}, done() {}},
  show_fail_modal() { failures++; },
  show_confirm_modal(message, confirm) { confirmations.push({message, confirm}); },
  hide_confirm_modal() {},
  window_history(_, extra) { saves++; window.history.state.extra = extra; },
  setTimeout(callback) { timers.push(callback); }
});
vm.runInContext(fs.readFileSync('web/static/js/media-library.js', 'utf8'), ctx);
ctx.library_view_instance = {};
ctx.load_library_items(3);
const first = requests[0];
// Leaving the library invalidates every callback, including history writes.
grid = null;
first.success({code: -1});
first.error();
first.complete();
assert.equal(saves, 0);
assert.equal(failures, 0);
assert.equal(window.history.state.extra.other_view, 'preserved');
// A restored library has the same IDs, but a different DOM and instance token.
grid = {};
ctx.library_view_instance = {};
ctx.library_items_loading = false;
ctx.load_library_items(2);
first.complete();
assert.equal(ctx.library_items_loading, true);
assert.equal(saves, 0);
const second = requests[1];
ctx.load_library_items(4); // queue a page while this instance is loading
second.complete();
assert.equal(saves, 1);
assert.equal(window.history.state.extra.other_view, 'preserved');
assert.equal(window.history.state.extra.library_view.page, 2);
assert.equal(timers.length, 1);
// A deferred pending page must not start a request in the next view.
grid = {};
ctx.library_view_instance = {};
timers[0]();
assert.equal(requests.length, 2);
// Reinitializing even the same DOM invalidates callbacks from its previous owner.
ctx.library_items_loading = false;
ctx.load_library_items(1);
ctx.library_view_instance = {};
requests[2].complete();
assert.equal(saves, 1);
// popstate switches history before the navigation animation replaces the DOM.
ctx.library_items_loading = false;
ctx.library_view_instance = {};
ctx.load_library_items(5);
const duringNavigation = requests[3];
assert.equal(listeners.length, 1);
assert.equal(listeners[0].name, 'popstate');
listeners[0].callback();
duringNavigation.success({code: -1});
duringNavigation.error();
duringNavigation.complete();
assert.equal(saves, 1);
assert.equal(failures, 0);
ctx.load_library_items(6);
assert.equal(requests.length, 4);
// Reloading scripts on a new page must not accumulate global event listeners.
vm.runInContext(fs.readFileSync('web/static/js/media-library.js', 'utf8'), ctx);
assert.equal(listeners.length, 1);
// A failed load keeps the page the user still sees instead of the requested one.
grid = {};
ctx.library_view_instance = {};
ctx.library_items_loading = false;
ctx.library_page = 3;
ctx.load_library_items(4);
const failed = requests[requests.length - 1];
failed.success({code: -1, msg: 'boom'});
assert.equal(ctx.library_page, 3);
failed.complete();
ctx.load_library_items(5);
requests[requests.length - 1].error();
assert.equal(ctx.library_page, 3);
requests[requests.length - 1].complete();
// A page past the end is clamped by the server; the client follows the returned page.
ctx.load_library_items(9);
const clamped = requests[requests.length - 1];
clamped.success({code: 0, items: [], total: 20, page: 2, library_total: 20});
assert.equal(ctx.library_page, 2);
assert.match($('#library_items_grid').content, /没有符合条件的媒体/);
clamped.complete();
ctx.load_library_items(1);
const empty_library = requests[requests.length - 1];
empty_library.success({code: 0, items: [], total: 0, page: 1, library_total: 0});
assert.match($('#library_items_grid').content, /字幕库还没有媒体/);
empty_library.complete();

// A successful reload publishes fresh records before updating an already-open drawer.
const oldMovie = {id: 'm1', media_type: 'movie', title: 'Movie', can_upload: true,
  target_path: '/media/Movie.mkv', subtitle_status: 'missing_chinese', subtitle_label: '缺中文字幕'};
ctx.library_drawer_item = oldMovie;
ctx.load_library_items(1);
const updatedList = requests[requests.length - 1];
updatedList.success({code: 0, page: 1, total: 1, items: [Object.assign({}, oldMovie, {
  subtitle_status: 'has_chinese_external', subtitle_label: '已有外挂中文字幕'
})]});
assert.equal(ctx.library_drawer_item.subtitle_status, 'has_chinese_external');
assert.match($('#library_drawer_overview').content, /已有外挂中文字幕/);
updatedList.complete();

// A script reload can reuse a preview counter, but cannot reuse its page ownership.
$('#index_upload_subtitle_target_path').value = '/media/A.mkv';
ctx.library_upload_preview_generation = 2;
ctx.request_library_upload_preview([{name: 'old.chs.srt'}]);
const oldPreview = requests[requests.length - 1];
vm.runInContext(fs.readFileSync('web/static/js/media-library.js', 'utf8'), ctx);
grid = {};
ctx.library_view_instance = {};
ctx.library_upload_preview_generation = 2;
$('#index_upload_subtitle_target_path').value = '/media/B.mkv';
ctx.request_library_upload_preview([{name: 'new.eng.srt'}]);
const newPreview = requests[requests.length - 1];
newPreview.success({code: 0, items: [{target: 'B.eng.srt', language: 'eng'}]});
const currentPreview = $('[data-library-preview="0"]').content;
assert.match(currentPreview, /B.eng.srt/);
oldPreview.success({code: 0, items: [{target: 'A.zh-CN.srt', language: 'zh-CN'}]});
assert.equal($('[data-library-preview="0"]').content, currentPreview);
// Reinitializing the same DOM, or changing the target without changing the counter, is stale too.
$('#index_upload_subtitle_target_path').value = '/media/C.mkv';
newPreview.success({code: 0, items: [{target: 'wrong-target.srt'}]});
assert.equal($('[data-library-preview="0"]').content, currentPreview);
$('#index_upload_subtitle_target_path').value = '/media/B.mkv';
ctx.library_view_instance = {};
newPreview.success({code: 0, items: [{target: 'wrong-view.srt'}]});
assert.equal($('[data-library-preview="0"]').content, currentPreview);

// Ordinary menu navigation removes the grid without resetting the instance token.
const queueResponse = {code: 0, total: 1, items: [oldMovie]};
ctx.start_library_fill();
const departedFill = requests[requests.length - 1];
grid = null;
const priorFailures = failures;
departedFill.success(queueResponse);
departedFill.error();
assert.equal(confirmations.length, 0);
assert.equal(failures, priorFailures);
grid = {};
ctx.library_view_instance = {};
ctx.library_fill_loading = true;
departedFill.complete();
assert.equal(ctx.library_fill_loading, true, 'old completion must not clear a new view loading flag');

// Confirmation and the delayed modal-hidden continuation both retain page ownership.
ctx.library_fill_loading = false;
ctx.start_library_fill();
const confirmedFill = requests[requests.length - 1];
confirmedFill.success(queueResponse);
confirmedFill.complete();
assert.equal(confirmations.length, 1);
grid = null;
confirmations[0].confirm();
assert.equal(ctx.library_fill, null);
grid = {};
ctx.start_library_fill();
const delayedFill = requests[requests.length - 1];
delayedFill.success(queueResponse);
delayedFill.complete();
confirmations[1].confirm();
assert.equal(ctx.library_fill.queue.length, 1);
grid = null;
const requestCount = requests.length;
$('#system-confirm-modal').hidden();
assert.equal(requests.length, requestCount, 'leaving during modal close must not start subtitle search');
console.log('Media library history lifecycle tests passed');
