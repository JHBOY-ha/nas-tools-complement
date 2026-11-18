// Run with node tests/test_opensubtitles_settings_ui.js and Playwright WebKit.
const {webkit} = require('playwright');
const {spawnSync} = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '..');
const rendered = spawnSync('python3', ['-c', `
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('web/templates'), autoescape=True)
print(env.get_template('setting/subtitle.html').render(Config={'subtitle': {}}))
`], {cwd: root, encoding: 'utf8'});
assert.equal(rendered.status, 0, rendered.stderr);

(async () => {
  const browser = await webkit.launch({headless: true});
  try {
    const page = await browser.newPage();
    await page.addStyleTag({path: path.join(root, 'web/static/css/tabler.min.css')});
    await page.addScriptTag({path: path.join(root, 'web/static/js/jquery-3.3.1.min.js')});
    await page.addScriptTag({path: path.join(root, 'web/static/js/util.js')});
    await page.addScriptTag({path: path.join(root, 'web/static/js/tabler.min.js')});
    await page.evaluate(() => {
      window.savedLimits = [];
      window.ajax_post = (command, data) => savedLimits.push(data['subtitle.opensubtitles.auto_download_limit']);
    });
    await page.evaluate(html => $('body').html(html), rendered.stdout);
    const open = page.getByRole('link', {name: '配置 OpenSubtitles'});
    await open.click();
    const input = page.getByLabel(/自动下载上限/);
    const save = page.locator('#opensubtitles_save_btn');
    await input.fill('');
    await input.pressSequentially('1e');
    assert.equal(await input.evaluate(el => el.validity.badInput), true);
    await save.click();
    assert.deepEqual(await page.evaluate(() => savedLimits), []);
    assert.equal(await input.evaluate(el => el.classList.contains('is-invalid')), true);
    if (process.env.TEST_ARTIFACT_DIR) {
      fs.mkdirSync(process.env.TEST_ARTIFACT_DIR, {recursive: true});
      await page.screenshot({path: path.join(process.env.TEST_ARTIFACT_DIR, 'invalid-quota.png')});
    }
    await input.fill('');
    await save.click();
    await input.waitFor({state: 'hidden'});
    await open.click();
    await input.fill('5');
    await save.click();
    assert.deepEqual(await page.evaluate(() => savedLimits), ['', '5']);
    console.log('OpenSubtitles quota settings reject incomplete numbers and accept blank and integer inputs.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
