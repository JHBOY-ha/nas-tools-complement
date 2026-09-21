const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const nodes = {};
function $(key) {
  if (!nodes[key]) {
    nodes[key] = {value: '', htmlValue: '', textValue: '', disabled: false, length: 0,
      val(v) { if (v === undefined) return this.value; this.value = v; return this; },
      text(v) { this.textValue = v; return this; },
      html(v) { this.htmlValue = v; return this; },
      empty() { this.htmlValue = ''; return this; },
      prop(k, v) { this[k] = v; return this; },
      toggleClass() { return this; }, modal() { return this; },
      each(fn) { (this.selected || []).forEach(value => fn.call({value})); }
    };
  }
  return nodes[key];
}
let pending, sent, calls = 0;
class FormData {
  constructor() { this.fields = {}; }
  append(k, v) { this.fields[k] = v; }
}
let root = {};
const context = {console, FormData, document: {getElementById: () => root}, library_current_series_id: 'series',
  library_items_cache: {series: {title: 'Show', server: 'plex'}}, library_default_media_server: 'emby',
  library_page: 1, load_library_items() {}, show_fail_modal(msg) { context.error = msg; },
  library_escape_html: s => String(s).replace(/</g, '&lt;'),
  SubtitleTasks: {randomRequestId: () => 'id', upload(data) { calls++; sent = data.fields;
    return new Promise((resolve, reject) => { pending = {resolve, reject}; }); }}
};
context.window = {jQuery: $};
vm.createContext(context);
vm.runInContext(fs.readFileSync('web/static/js/subtitle-season-pack.js', 'utf8'), context);
const api = context.window;
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  api.open_library_season_pack();
  assert(context.error.includes('选择具体'));
  $('#library_episode_season').value = '2';
  api.open_library_season_pack();
  $('#season_pack_file')[0] = {files: [{size: 1024, name: 'show.zip'}]};
  api.upload_library_season_pack(false);
  assert.equal(sent.upload_mode, 'season_preview');
  assert.equal(sent.item_id, 'series');
  assert.equal(sent.season, '2');
  assert.equal(sent.server, 'plex');
  api.upload_library_season_pack(false);
  assert.equal(calls, 1, 'duplicate requests must be blocked');
  pending.resolve({response: {plan_id: 'plan', rows: [
    {id: '0', name: '<E01>.srt', season_episode: 'S02E01', target: {path: '/show/E01.mkv'}},
    {id: '1', name: 'S01E02.srt', reason: '跨季', target: null}
  ]}});
  await flush();
  assert($('#season_pack_rows').htmlValue.includes('&lt;E01>'));
  assert($('#season_pack_rows').htmlValue.includes('data-unmatched'));
  const selection = $('#season_pack_rows input:checked:not(:disabled)');
  selection.length = 1;
  selection.selected = ['0'];
  api.upload_library_season_pack(true);
  assert.equal(sent.members, '["0"]');
  assert.equal(sent.plan_id, 'plan');
  assert.equal(sent.upload_mode, 'season_submit');
  pending.reject({message: 'queue full'});
  await flush();
  assert.equal($('#season_pack_message').textValue, 'queue full');
  assert.equal($('#season_pack_submit').disabled, false, 'retry must retain plan');
  api.upload_library_season_pack(true);
  pending.resolve({task_id: 'task'});
  await flush();
  assert.equal($('#season_pack_submit').disabled, true);
  assert($('#season_pack_message').textValue.includes('任务中心'));
  api.upload_library_season_pack(false);
  const before = $('#season_pack_rows').htmlValue;
  root = {};
  pending.resolve({response: {plan_id: 'stale', rows: [{name: 'stale'}]}});
  await flush();
  assert.equal($('#season_pack_rows').htmlValue, before, 'navigation must ignore stale callbacks');
  console.log('Season selection, preview escaping, bounded selection payload, retry and task submission passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
