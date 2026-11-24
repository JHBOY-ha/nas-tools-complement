// Run with: node tests/test_douban_covers.js (no network or browser dependencies).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const elements = new Map();
const nothing = Symbol('nothing');
const context = vm.createContext({
  LitElement: class {}, nothing, observeState: base => base, cardState: {},
  NormalCardPlaceholder: {render_placeholder() {}},
  window: {customElements: {define: (name, element) => elements.set(name, element)}},
  html: (strings, ...values) => ({strings, values}),
});
for (const file of ['utility/utility.js', 'card/normal/index.js', 'custom/img/index.js']) {
  const code = fs.readFileSync(path.join(__dirname, '../web/static/components', file), 'utf8');
  vm.runInContext(code.replace(/^import .*$/gm, '').replaceAll('export class ', 'class '), context);
}

// Exercise the bindings and handlers returned by the actual Lit components.
function binding(component, attribute) {
  const template = component.render();
  const index = template.strings.findIndex(text => text.endsWith(' ' + attribute + '='));
  assert.notEqual(index, -1, attribute);
  return template.values[index];
}

const Card = elements.get('normal-card');
const card = new Card();
const original = 'https://img9.doubanio.com/view/photo/s_ratio_poster/public/p42.jpg?size=1&sig=a%2Fb';
card.image = original;
card.tmdb_id = 'DB:42';
card.lazy = '1';
assert.equal(binding(card, 'src'), nothing);
card.lazy = '0';
assert.equal(binding(card, 'src'), original);
binding(card, '@error')();
assert.equal(binding(card, 'src'), '/douban/subject/42/preview');
binding(card, '@load')();
assert.equal(card._placeholder, false);
binding(card, '@error')();
const noImage = '../static/img/no-image.png';
assert.equal(binding(card, 'src'), noImage);
binding(card, '@error')();
assert.equal(binding(card, 'src'), noImage);
assert.equal(card.image, original);

card.tmdb_id = 'DB:43';
card.image = '';
card.willUpdate(new Map([['tmdb_id', 'DB:42'], ['image', original]]));
assert.equal(binding(card, 'src'), '/douban/subject/43/preview');
for (const id of ['123', 'BG:42', 'DB:bad', 'DB:4/2']) {
  card.tmdb_id = id;
  card.image = 'https://image.tmdb.org/t/p/w500/poster.jpg';
  card.willUpdate(new Map([['tmdb_id', undefined]]));
  assert.equal(binding(card, 'src'), card.image);
  binding(card, '@error')();
  assert.equal(binding(card, 'src'), noImage);
}

const CustomImg = elements.get('custom-img');
const img = new CustomImg();
img.img_src = original;
assert.equal(binding(img, 'src'), original);
img.lazy = '1';
assert.equal(binding(img, 'src'), '');
console.log('PASS: original URLs, lazy loading, Douban preview, terminal fallback and reused cards');
