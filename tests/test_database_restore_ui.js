/* Existing backup/restore handlers and shared modals, exercised in real Chrome. */
const {chromium} = require('playwright');
const fs = require('fs'), path = require('path'), assert = require('assert');
const root = path.resolve(__dirname, '..');
const service = fs.readFileSync(path.join(root, 'web/templates/service.html'), 'utf8');
const handlers = service.slice(service.indexOf('  // 备份\n'), service.indexOf('  //名称识别测试'));
const navigation = fs.readFileSync(path.join(root, 'web/templates/navigation.html'), 'utf8');
const feedback = navigation.slice(navigation.indexOf('  function show_success_modal('), navigation.indexOf('</script>', navigation.indexOf('  function show_success_modal(')));
(async () => {
  const browser = await chromium.launch({headless:true, ...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {})});
  try {
    const page = await browser.newPage({viewport:{width:1000,height:760}, acceptDownloads:true});
    let restores=0, backupMode='ok', restoreMode='ok';
    const errors=[];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('http://db.test/**', async route => {
      const url=new URL(route.request().url());
      if (url.pathname==='/') return route.fulfill({contentType:'text/html; charset=utf-8', body:`<!doctype html><html lang="zh-CN"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/css/tabler.min.css"><body><main class="container-xl"><button id="backup_btn" class="btn btn-primary">备份当前配置</button><button id="restore_btn" class="btn btn-outline-primary">恢复配置</button></main>
${['success','fail'].map(kind=>`<div id="system-${kind}-modal" class="modal" tabindex="-1" aria-labelledby="system_${kind}_message"><div class="modal-dialog"><div class="modal-content"><div class="modal-body" id="system_${kind}_message"></div><div class="modal-footer"><button id="system_${kind}_modal_btn" class="btn btn-primary" data-bs-dismiss="modal">关闭</button></div></div></div></div>`).join('')}
<script src="/static/js/jquery-3.3.1.min.js"></script><script src="/static/js/tabler.min.js"></script><script src="/static/js/nprogress.js"></script><script src="/static/js/util.js"></script><script>window.backup_dropzone={files:[{name:'backup.zip'}]}; window.FailureCount=0; ${feedback}; ${handlers};</script></body></html>`});
      if (url.pathname==='/do') {
        restores+=1;
        await new Promise(resolve=>setTimeout(resolve,200));
        if (restoreMode==='offline') return route.abort('connectionfailed');
        return route.fulfill({json:restoreMode==='ok' ? {code:0, restart_required:true, msg:'已暂存'} : {code:1,msg:'<img src=x onerror=alert(1)>备份校验失败'}});
      }
      if (url.pathname==='/backup') {
        await new Promise(resolve=>setTimeout(resolve,150));
        if (backupMode==='offline') return route.abort('connectionfailed');
        if (backupMode==='error') return route.fulfill({status:503,body:'备份失败'});
        return route.fulfill({body:'disposable zip fixture',headers:{'Content-Type':'application/zip','Content-Disposition':'attachment; filename="snapshot.zip"'}});
      }
      const file=path.join(root,'web',url.pathname);
      return fs.existsSync(file) ? route.fulfill({path:file}) : route.fulfill({status:404});
    });
    await page.goto('http://db.test/');
    await page.locator('#restore_btn').click();
    await page.waitForFunction(()=>document.getElementById('restore_btn').disabled);
    assert.equal(await page.locator('#restore_btn').getAttribute('aria-busy'),'true');
    await page.evaluate(()=>document.getElementById('restore_btn').click());
    await page.locator('#system-success-modal.show').waitFor();
    assert.equal(restores,1);
    assert((await page.locator('#system_success_message').textContent()).includes('重启后离线恢复生效'));
    assert.equal(await page.locator('#restore_btn').isDisabled(),false);
    await page.locator('#system_success_modal_btn').click();
    restoreMode='error';
    await page.locator('#restore_btn').click();
    await page.locator('#system-fail-modal.show').waitFor();
    assert.equal(await page.locator('#system_fail_message img').count(),0);
    assert.equal(await page.locator('#restore_btn').isDisabled(),false);
    await page.locator('#system_fail_modal_btn').click();
    // The legacy failure callback reopens its owning dialog. Close it through
    // Bootstrap, including its backdrop, before exercising the sibling backup.
    await page.waitForFunction(()=>!document.body.classList.contains('modal-open'));
    const download=page.waitForEvent('download');
    await page.locator('#backup_btn').click();
    assert.equal((await download).suggestedFilename(),'snapshot.zip');
    await page.waitForFunction(()=>!document.getElementById('backup_btn').disabled);
    backupMode='error';
    await page.locator('#backup_btn').click();
    await page.locator('#system-fail-modal.show').waitFor();
    await page.waitForFunction(()=>!document.getElementById('backup_btn').disabled);
    await page.locator('#system_fail_modal_btn').click();
    backupMode='offline';
    await page.locator('#backup_btn').click();
    await page.locator('#system-fail-modal.show').waitFor();
    await page.waitForFunction(()=>!document.getElementById('backup_btn').disabled);
    await page.setViewportSize({width:390,height:740});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.keyboard.press('Escape');
    assert.deepEqual(errors,[]);
    console.log('PASS: staged restore, duplicate guard, safe failure text, backup download/error/offline, busy reset, narrow layout');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exit(1);});
