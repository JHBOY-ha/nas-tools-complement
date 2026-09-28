// Run with: node tests/test_media_library_history.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
let grid = {};
const elements = new Map();
const requests = [];
const timers = [];
let saves = 0;
let failures = 0;
function $(selector) {
  if (!elements.has(selector)) elements.set(selector, {
    value: '', content: '',
    val() { return this.value; },
    prop() { return this; },
    html(value) { this.content = value; return this; },
    text(value) { this.content = value; return this; }
  });
  return elements.get(selector);
}
$.ajax = options => requests.push(options);
const listeners = [];
const window = {history: {state: {extra: {other_view: 'preserved'}}},
  addEventListener(name, callback) { listeners.push({name, callback}); }};
const ctx = vm.createContext({$, window, document: {getElementById: () => grid},
  NProgress: {start() {}, done() {}},
  show_fail_modal() { failures++; },
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
console.log('Media library history lifecycle tests passed');
