// Run with: node tests/test_douban_covers.js (no network or browser dependencies).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'web/static/components/utility/utility.js'), 'utf8');
const context = vm.createContext({URL, LitElement: class {}});
vm.runInContext(source.replace(/^import .*;$/m, '').replaceAll('export class ', 'class ')
  + '\nthis.Golbal = Golbal;', context);
const imageUrl = value => context.Golbal.poster_image_url(value);
const posterPath = '/view/photo/m_ratio_poster/public/p2936069048.webp';
const resize = 'imageView2/2/q/80/w/500/h/750/format/webp';
const expected = 'https://qnmob3.doubanio.com' + posterPath + '?' + resize;

for (const host of ['img1', 'img2', 'img3', 'img9', 'qnmob3']) {
  for (const scheme of ['https', 'http']) {
    assert.equal(imageUrl(`${scheme}://${host}.doubanio.com${posterPath}`), expected);
  }
}
assert.equal(imageUrl(expected), expected, 'normalization must be idempotent');
assert.equal(imageUrl('https://img3.doubanio.com' + posterPath + '?version=1#poster'),
  'https://qnmob3.doubanio.com' + posterPath + '?version=1&' + resize + '#poster');
assert.equal(imageUrl('http://img3.doubanio.com:80' + posterPath), expected);
assert.equal(imageUrl('https://img3.doubanio.com/view/photo/s_ratio_poster/public/p42.jpg'),
  'https://qnmob3.doubanio.com/view/photo/s_ratio_poster/public/p42.jpg?' + resize);
for (const value of [undefined, null, '', 'None', 'not a URL', '../static/img/no-image.png',
  'https://image.tmdb.org/t/p/w500/a.jpg', 'http://lain.bgm.tv/pic/cover/l/a.jpg',
  'https://img3.doubanio.com.evil.test' + posterPath,
  'https://evil.test/?url=https://img3.doubanio.com' + posterPath,
  'https://user:secret@img3.doubanio.com' + posterPath,
  'https://img3.doubanio.com:8443' + posterPath,
  'ftp://img3.doubanio.com' + posterPath,
  'https://img3.doubanio.com/icon/u123.jpg']) {
  assert.equal(imageUrl(value), value, 'unrelated or invalid source must be unchanged');
}
for (const component of ['card/normal', 'custom/img']) {
  const code = fs.readFileSync(path.join(root, `web/static/components/${component}/index.js`), 'utf8');
  assert(code.includes('referrerpolicy="no-referrer"'));
  assert(code.includes('Golbal.poster_image_url('));
  assert(code.includes('this.lazy == "1" ? "" :'), 'keep deferred image loading');
}
console.log('PASS: Douban direct CDN URLs, processing, idempotence, URL boundaries, components and lazy loading');
