/* Subtitle task center: running/finished groups, friendly times, compact finished rows and the radius scale. */
const {firefox} = require('playwright');
const fs = require('fs'), path = require('path'), assert = require('assert');
const root = path.resolve(__dirname, '..');
const evidenceDir = process.env.TEST_ARTIFACT_DIR || '/tmp/nas-tools-subtitle-task-center';

const minutesAgo = minutes => new Date(Date.now() - minutes * 60000).toISOString();
const daysAgoAt = (days, hours, minutes) => {
  const date = new Date();
  date.setDate(date.getDate() - days);
  date.setHours(hours, minutes, 0, 0);
  return date.toISOString();
};
const moviePath = '/volume1/media/movie/外语电影/爱在午夜降临前 (2013)/爱在午夜降临前 (2013).mkv';
const tasks = [
  {task_id: 'run', type: 'audit', status: 'running', server: 'jellyfin', cancellable: true,
    created_at: minutesAgo(10), started_at: minutesAgo(9),
    payload: {subcategory: '外语电影', mode: 'linked'},
    progress: {phase: 'enumerating', message: '正在枚举字幕范围', elapsed_seconds: 572}},
  {task_id: 'queue', type: 'upload', status: 'queued', queue_position: 1, server: 'emby', cancellable: true,
    created_at: minutesAgo(1), payload: {media_file: '/volume1/media/movie/外语电影/爱在黎明破晓前 (1995)/爱在黎明破晓前 (1995).mkv'},
    progress: {phase: 'queued'}},
  {task_id: 'done', type: 'upload', status: 'succeeded', server: 'jellyfin',
    created_at: daysAgoAt(1, 23, 9), finished_at: daysAgoAt(1, 23, 10),
    payload: {media_file: moviePath}, progress: {phase: 'completed', message: '已发布 1 个字幕', elapsed_seconds: 2},
    result: {results: [{filename: '爱在午夜降临前 (2013).chs.srt', success: true, data: {}}]}, items: []},
  {task_id: 'fail', type: 'repair', status: 'failed', server: 'plex',
    created_at: daysAgoAt(2, 0, 10), finished_at: daysAgoAt(2, 0, 11),
    payload: {media_file: '/volume1/media/tv/欧美剧/某剧/Season 1/某剧 S01E02.mkv'},
    progress: {phase: 'normalize', elapsed_seconds: 5}, error: '字幕编码无法识别', result: {}, items: []},
];

(async () => {
  const browser = await firefox.launch({headless: true, ...(process.env.FIREFOX_PATH ? {executablePath: process.env.FIREFOX_PATH} : {})});
  try {
    for (const width of [1280, 390]) {
      const page = await browser.newPage({viewport: {width, height: 900}});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.route('http://tasks.test/**', route => {
        const url = new URL(route.request().url());
        if (url.pathname === '/') return route.fulfill({contentType: 'text/html; charset=utf-8', body: `<!doctype html>
          <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
          <link rel="stylesheet" href="/static/css/tabler.min.css"><link rel="stylesheet" href="/static/css/style.css">
          <body><button class="btn" type="button" data-subtitle-task-center>任务中心</button>
          <script src="/static/js/jquery-3.3.1.min.js"></script><script src="/static/js/tabler.min.js"></script>
          <script src="/static/js/subtitle-tasks.js"></script><script>SubtitleTasks.init();</script>`});
        if (url.pathname.startsWith('/static/')) return route.fulfill({path: path.join(root, 'web', url.pathname)});
        if (url.pathname === '/subtitle/tasks') return route.fulfill({json: {code: 0, data: {items: tasks}}});
        if (url.pathname === '/subtitle/tasks/settings') return route.fulfill({json: {code: 0, data: {}}});
        const detail = tasks.find(task => url.pathname === `/subtitle/tasks/${task.task_id}`);
        return detail ? route.fulfill({json: {code: 0, data: detail}}) : route.fulfill({status: 404, body: ''});
      });
      await page.goto('http://tasks.test/');
      await page.click('[data-subtitle-task-center]');
      await page.locator('#subtitle-task-center-modal.show').waitFor();
      await page.locator('.subtitle-task-row').first().waitFor();

      const view = await page.evaluate(() => {
        const radius = el => getComputedStyle(el).borderTopLeftRadius;
        const text = el => el.innerText.replace(/\s+/g, ' ').trim();
        return {
          groups: [...document.querySelectorAll('.subtitle-task-group-title')].map(text),
          cards: [...document.querySelectorAll('#subtitle-task-center-list article.card')].map(el => el.dataset.subtitleTaskId),
          rows: [...document.querySelectorAll('.subtitle-task-row')].map(el => el.dataset.subtitleTaskId),
          meta: [...document.querySelectorAll('article .subtitle-task-meta')].map(text),
          separator: getComputedStyle(document.querySelector('.subtitle-task-meta > *'), '::before').content,
          rowTitle: text(document.querySelector('.subtitle-task-row .text-break')),
          rowTitleHint: document.querySelector('.subtitle-task-row .text-break').title,
          rowTimes: [...document.querySelectorAll('.subtitle-task-row time')].filter(el => el.offsetParent).map(text),
          failText: text(document.querySelector('[data-subtitle-task-id="fail"]')),
          cancel: [...document.querySelectorAll('.subtitle-task-cancel')].map(el => ({
            danger: el.classList.contains('btn-outline-danger'), height: Math.round(el.getBoundingClientRect().height),
            pill: getComputedStyle(el).borderRadius.split(' ').every(corner => parseFloat(corner) >= el.offsetHeight / 2)})),
          indeterminateWidth: Math.round(document.querySelector('.progress-bar-indeterminate').getBoundingClientRect().width),
          serverBadges: [...document.querySelectorAll('#subtitle-task-center-list .badge')].filter(el => /JELLYFIN|Jellyfin/.test(el.textContent)).length,
          alerts: document.querySelectorAll('#subtitle-task-center-modal .alert').length,
          note: text(document.querySelector('.subtitle-task-footer-note')),
          radii: {
            modal: radius(document.querySelector('#subtitle-task-center-modal .modal-content')),
            card: radius(document.querySelector('#subtitle-task-center-list article.card')),
            badge: radius(document.querySelector('#subtitle-task-center-list .badge')),
          },
          statusWidths: [...document.querySelectorAll('.subtitle-task-row-status')].map(el => Math.round(el.getBoundingClientRect().width)),
          metaFirstOffset: [...document.querySelectorAll('.subtitle-task-meta')].map(meta => {
            const first = [...meta.children].find(child => child.offsetParent);
            return first ? Math.round(first.getBoundingClientRect().left - meta.getBoundingClientRect().left) : null;
          }),
          overflow: document.documentElement.scrollWidth > window.innerWidth,
        };
      });

      assert.deepEqual(view.groups, ['进行中 · 2', '最近完成']);
      assert.deepEqual(view.cards.sort(), ['queue', 'run']);
      assert.deepEqual(view.rows, ['done', 'fail']);
      // The “·” separators are ::before content, so innerText shows the parts only.
      assert.match(view.meta.find(line => line.startsWith('Jellyfin')), /^Jellyfin 枚举目录 (今天|昨天) \d\d:\d\d 开始 已运行 9分32秒$/);
      assert.equal(view.separator, '"·"');
      assert.match(view.meta.find(line => line.startsWith('Emby')), /(今天|昨天) \d\d:\d\d 提交/);
      assert.equal(view.rowTitle, '手动上传 · 爱在午夜降临前 (2013).mkv', 'finished rows show the file name only');
      assert.equal(view.rowTitleHint, moviePath, 'the full path stays available on hover');
      const twoDaysAgo = new Date();
      twoDaysAgo.setDate(twoDaysAgo.getDate() - 2);
      assert.deepEqual(view.rowTimes, ['昨天 23:10', `${twoDaysAgo.getMonth() + 1}月${twoDaysAgo.getDate()}日 00:11`]);
      assert.match(view.failText, /字幕编码无法识别/, 'compact rows keep the failure reason');
      assert.ok(view.cancel.length === 2 && view.cancel.every(button => !button.danger && button.height === 28 && button.pill));
      assert.equal(view.indeterminateWidth, 0, 'unknown progress must not fill the bar');
      assert.equal(view.serverBadges, 0, 'server names are plain text, not badges');
      assert.equal(view.alerts, 0);
      assert.match(view.note, /^关闭窗口不会停止后台任务/);
      assert.deepEqual(view.radii, {modal: '16px', card: '12px', badge: '4px'});
      assert.equal(new Set(view.statusWidths).size, 1, 'finished-row status badges share one width');
      assert.ok(view.metaFirstOffset.every(offset => offset < 0), 'the leading separator of each line is clipped');
      assert.equal(view.overflow, false);
      assert.deepEqual(errors, []);
      fs.mkdirSync(evidenceDir, {recursive: true});
      await page.screenshot({path: path.join(evidenceDir, `task-center-${width}.png`)});
      await page.close();
    }
    console.log('Task center groups running and finished tasks with friendly times and the shared radius scale.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
