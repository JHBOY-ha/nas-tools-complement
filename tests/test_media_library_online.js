// Run with: node tests/test_media_library_online.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
function $(selector) {
  if (selector && typeof selector === 'object' && selector.val) { return selector; }
  if (!elements.has(selector)) {
    const element = {value: '', content: '', properties: {}, values: {}, events: {},
      length: String(selector).startsWith('#library_online_member_') ? 0 : 1,
      val(value) { if (value === undefined) return this.value; this.value = value; return this; },
      text(value) { this.content = value; return this; },
      html(value) { this.content = value; return this; },
      empty() { this.content = ''; return this; },
      append() { return this; },
      prop(key, value) {
        if (value === undefined) return this.properties[key];
        this.properties[key] = value;
        // Model jQuery's class selection when the download disables every row.
        if (selector === '.library-online-download') {
          elements.forEach(node => { if (node.downloadButton) node.properties[key] = value; });
        }
        return this;
      },
      data(key, value) { if (value === undefined) return this.values[key]; this.values[key] = value; return this; },
      one(event, callback) { this.events[event] = callback; return this; },
      trigger(event) { this.lastEvent = event; return this; },
      modal(state) {
        const event = state === 'hide' ? 'hidden.bs.modal' : 'shown.bs.modal';
        const callback = this.events[event];
        delete this.events[event];
        if (callback) callback();
        return this;
      }};
    elements.set(selector, element);
  }
  return elements.get(selector);
}
let ajax;
const requests = [];
$.ajax = options => { ajax = options; requests.push(options); };
const ctx = vm.createContext({$, console, Set, document: {}, window: {}});
vm.runInContext(fs.readFileSync('web/static/js/media-library.js', 'utf8'), ctx);
ctx.library_current_series_id = 'show';
ctx.library_current_series_title = 'Example';
ctx.library_items_cache = {show: {original_title: 'Original Example', year: '2024'}};
ctx.library_episodes_cache = [
  {season: 1, episode: 2, title: 'A', path: '/media/S01E02.mkv', can_upload: true},
  {season: 2, episode: 1, title: 'B', path: '/media/S02E01.mkv', can_upload: true},
  {season: 2, episode: 2, title: 'C', path: '/media/S02E02.mkv', can_upload: true}
];
$('#library_episode_season').val('2');
$('#library_episode_number').val('2');
ctx.filter_library_episodes(false);
const html = $('#index_library_episodes_body').content;
assert.ok(html.includes('open_library_online_episode(2)'));
assert.ok(!html.includes('open_library_online_episode(0)'));
assert.ok(!html.includes('open_library_online_episode(1)'));
$('#library_online_provider').val('thunder');
ctx.open_library_online_episode(2);
const payload = JSON.parse(ajax.data);
assert.equal(payload.media_path, '/media/S02E02.mkv');
assert.equal(payload.keyword, 'Example Original Example 2024 S02E02');
assert.equal(payload.media.query_edited, false);
assert.equal(payload.media.season, 2);
assert.equal(payload.media.episode, 2);
assert.equal(payload.media.original_title, 'Original Example');
assert.ok($('#library_online_media').content.includes('第 2 季 第 2 集'));
assert.ok($('#library_online_target_path').content.includes('/media/S02E02.mkv'));
// A late response for a previous query must not overwrite the current episode's results.
const previous = ajax;
ctx.open_library_online_episode(0);
previous.success({code: 0, items: [{name: 'stale'}]});
assert.equal(ctx.library_online_results.length, 0);
assert.equal(JSON.parse(ajax.data).media_path, '/media/S01E02.mkv');
// Editing keywords preserves year and release terms without changing the destination.
$('#library_online_keyword').val('Original Example 2023 S01E02 1080p BluRay');
ctx.search_library_online_subtitles();
const edited = JSON.parse(ajax.data);
assert.equal(edited.keyword, 'Original Example 2023 S01E02 1080p BluRay');
assert.equal(edited.media.query_edited, true);
assert.equal(edited.media_path, '/media/S01E02.mkv');
ctx.reset_library_online_keyword();
assert.equal($('#library_online_keyword').val(), 'Example Original Example 2024 S01E02');
assert.equal(ctx.library_online_keyword_for({title: 'Alien', original_title: 'alien', year: 1979}), 'Alien 1979');
assert.equal(ctx.library_online_keyword_for({title: '星际穿越', original_title: 'Interstellar', year: 2014}), '星际穿越 Interstellar 2014');
assert.equal(ctx.library_online_keyword_for({title: 'Only title'}), 'Only title');
console.log('Episode binding, default keywords, manual editing and reset checks passed');

// Sparse history records must show the season/episode present in the screenshot's filename.
ctx.library_current_series_title = 'Re：从零开始的异世界生活';
ctx.library_items_cache.show = {original_title: 'Re:ゼロから始める異世界生活', year: 2016};
ctx.library_episodes_cache = [{season: '', episode: '', path: '/media/Season 4/Re：从零开始的异世界生活 - S04E17 - 第17集.mkv'}];
ctx.open_library_online_episode(0);
const recovered = JSON.parse(ajax.data);
assert.equal(recovered.media.season, 4);
assert.equal(recovered.media.episode, 17);
assert.ok(recovered.keyword.endsWith('2016 S04E17'));
assert.ok($('#library_online_media').content.includes('第 4 季 第 17 集'));
assert.equal(ctx.library_fill_episode_numbers({path: '/media/S00E02.mkv'}).season, 0);
console.log('Screenshot regression: missing metadata recovered as S04E17');

// OpenSubtitles reuses the selected series identity and displays its own source label.
ctx.library_items_cache.show.imdbid = 'tt12345';
$('#library_online_provider').val('opensubtitles');
ctx.open_library_online_episode(0);
assert.equal(JSON.parse(ajax.data).provider, 'opensubtitles');
assert.equal(JSON.parse(ajax.data).media.imdbid, 'tt12345');
ajax.success({code: 0, items: [{name: 'Show.S04E17', provider: 'opensubtitles', language: '简体中文', format: 'srt', ticket: 'signed'}]});
assert.ok($('#library_online_results').content.includes('OpenSubtitles'));
assert.ok(!$('#library_online_results').content.includes('Assrt ·'));

let confirmDownload;
ctx.show_confirm_modal = (message, callback) => {
  assert.ok(message.includes('一次账户下载配额'));
  confirmDownload = callback;
};
ctx.hide_confirm_modal = () => $('#system-confirm-modal').modal('hide');
const button = $('#online-download-button');
button.downloadButton = true;
let count = requests.length;
ctx.download_library_online_subtitle(0, button);
assert.equal(requests.length, count);
assert.equal(button.prop('disabled'), true);
// Cancel restores the exact search result, focus and button, without a download request.
$('#system-confirm-modal').modal('hide');
assert.equal(requests.length, count);
assert.equal(button.prop('disabled'), false);
assert.equal(button.lastEvent, 'focus');
ctx.download_library_online_subtitle(0, button);
confirmDownload();
assert.equal(requests.length, count + 1);
assert.equal(ajax.url, '/library/subtitle/download');
assert.deepEqual(JSON.parse(ajax.data), {ticket: 'signed', confirm_quota: true});
assert.equal(button.prop('disabled'), true);
ctx.download_library_online_subtitle(0, button);
assert.equal(requests.length, count + 1);

// A pending confirmation may not dispatch into results from a newer search.
button.prop('disabled', false);
ctx.download_library_online_subtitle(0, button);
ctx.library_online_generation += 1;
confirmDownload();
assert.equal(requests.length, count + 1);
console.log('OpenSubtitles source, quota confirmation, cancel, duplicate prevention and stale-confirmation checks passed');
