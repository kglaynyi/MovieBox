/* Offline real-browser smoke test. Requires Playwright and Chromium. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {spawnSync} = require('node:child_process');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const rendered = spawnSync(path.join(root, '.venv/bin/python'), ['-c', `
import asyncio, os
os.environ.update(API_ID='12345', API_HASH='0'*32, BOT_TOKEN='12345:dummy', OWNER_ID='12345', DATABASE='mongodb://localhost:27017,mongodb://localhost:27017')
from starlette.requests import Request
from Backend.fastapi.routes.template_routes import tools_page
request = Request({'type':'http', 'method':'GET', 'path':'/tools', 'headers':[], 'session':{'authenticated':True,'username':'admin'}})
print(asyncio.run(tools_page(request)).body.decode())
`], {cwd: root, encoding: 'utf8', maxBuffer: 4 * 1024 * 1024});
assert.equal(rendered.status, 0, rendered.stderr);
assert(rendered.stdout.includes('gdi-browser'));
const inlineScripts = [...rendered.stdout.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)].map(match => match[1]);
// Compile every rendered script, including the existing Tools script.
for (const source of inlineScripts) new (require('node:vm').Script)(source);

(async () => {
    let selected = [], scan = {status:'idle', counters:{}}, saveCalls = 0, startCalls = 0, failures = false;
    const pageErrors = [];
    const server = http.createServer((req, res) => {
        if (req.url.startsWith('/static/')) {
            const file = path.join(root, 'Backend/fastapi', req.url.split('?')[0]);
            if (fs.existsSync(file)) { res.setHeader('Content-Type', file.endsWith('.js') ? 'application/javascript' : 'text/css'); res.end(fs.readFileSync(file)); return; }
        }
        res.setHeader('Content-Type', 'text/html'); res.end(rendered.stdout);
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const origin = 'http://127.0.0.1:' + server.address().port;
    const browser = await chromium.launch({headless:true, executablePath:process.env.CHROMIUM_PATH || undefined, args:['--no-sandbox']});
    try {
        const page = await browser.newPage({viewport:{width:390, height:844}});
        page.on('pageerror', error => pageErrors.push(error.message));
        await page.route('**/*', async route => {
            const req = route.request(), url = new URL(req.url());
            if (url.origin !== origin) return route.abort(); // No external network.
            if (!url.pathname.startsWith('/api/')) return route.continue();
            let data = {status:'success', data:{status:'idle', counters:{}}, channels:[], session:null};
            let status = 200;
            if (url.pathname.endsWith('/gdrive-folders/config')) data = {source_type:'gdi_js', root_path:'/0:/', selected_folders:selected};
            else if (url.pathname.endsWith('/gdrive-folders/browse')) {
                const payload = req.postDataJSON();
                if (failures) { status = 400; data = {detail:'Index requires login. Save the index credentials in Settings.'}; }
                else if (payload.path === '/0:/') data = {path:'/0:/', folders:[{name:'Movies',path:'/0:/Movies/'},{name:'Anime',path:'/0:/Anime/'}], next_page_token:null,page_index:0};
                else data = {path:payload.path, folders:[{name:'Season 1',path:payload.path+'Season%201/'}],next_page_token:null,page_index:0};
            } else if (url.pathname.endsWith('/gdrive-folders/selection')) {
                selected = req.postDataJSON().folders; saveCalls++;
                data = {selected_folders:selected};
            } else if (url.pathname.endsWith('/gdrive-scan/start')) {
                assert(selected.length, 'No whole-drive scan without selection');
                startCalls++; scan = {status:'running', is_running:true,phase:'discovery',discovery_pages:2,discovery_files:4,counters:{},elapsed:'2s'};
                data = {ok:true};
            } else if (url.pathname.endsWith('/gdrive-scan/cancel')) {
                scan = {status:'cancelled',is_running:false,counters:{}}; data = {ok:true};
            } else if (url.pathname.endsWith('/gdrive-scan/status')) data = {data:scan};
            await route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
        });
        await page.goto(origin + '/tools');
        await page.waitForFunction(() => !document.getElementById('gdi-controls').hidden);
        assert.equal(await page.evaluate(() => typeof window.startGDriveScan), 'function');
        assert.equal(await page.evaluate(() => typeof window.pollGDriveScan), 'function');
        await page.locator('#gd-scan-start-btn').click();
        await page.getByText('Choose at least one folder above before scanning.', {exact:true}).waitFor();
        assert.equal(startCalls, 0);
        await page.locator('#gdi-connect').click();
        await page.locator('input[data-path="/0:/Movies/"]').check();
        await page.locator('input[data-path="/0:/Anime/"]').check();
        await page.locator('#gdi-save').click();
        await page.waitForFunction(() => document.getElementById('gdi-saved').textContent === 'Saved');
        assert.equal(saveCalls, 1);
        assert.deepEqual(selected, ['/0:/Movies/', '/0:/Anime/']);
        if (process.env.UI_SCREENSHOT) await page.locator('#gdi-browser').screenshot({path:process.env.UI_SCREENSHOT});
        await page.locator('#gd-scan-start-btn').click();
        await page.waitForFunction(() => document.getElementById('gd-scan-current').textContent.includes('Finding videos'));
        assert.equal(startCalls, 1);
        await page.locator('#gd-scan-cancel-btn').click();
        await page.getByText('Stopped.', {exact:true}).waitFor();
        await page.reload();
        await page.waitForFunction(() => document.getElementById('gdi-selected').textContent.includes('/0:/Movies/'));
        await page.locator('#gdi-connect').click();
        await page.locator('#gdi-folders button').first().click();
        await page.waitForFunction(() => document.getElementById('gdi-path').textContent === '/0:/Movies/');
        await page.locator('#gdi-up').click();
        await page.waitForFunction(() => document.getElementById('gdi-path').textContent === '/0:/');
        failures = true;
        await page.locator('#gdi-connect').click();
        await page.locator('#gdi-error').getByText('Index requires login.', {exact:false}).waitFor();
        assert.equal(selected.length, 2, 'An API failure must preserve saved selection');
        assert.deepEqual(pageErrors, [], 'No page JavaScript errors');
        console.log('PASS: mobile folder browse, multi-select, save/reload, start, stop, navigation, safe errors, global handlers.');
    } finally {
        await browser.close();
        await new Promise(resolve => server.close(resolve));
    }
})().catch(error => { console.error(error); process.exit(1); });
