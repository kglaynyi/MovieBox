/* Dependency-free DOM-model tests, not a substitute for visual browser QA. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'Backend/fastapi/templates/tools.html'), 'utf8');
const source = fs.readFileSync(path.join(root, 'Backend/fastapi/static/gdrive_tools.js'), 'utf8');
assert(html.indexOf('/static/gdrive_tools.js') < html.indexOf('initGDriveTools();'));
assert(!html.includes('async function startGDriveScan'), 'Drive functions must not be nested in renderScan');

class Node {
    constructor(tag='div') { this.tag=tag; this.children=[]; this.listeners={}; this.style={}; this.dataset={}; this.hidden=false; this.disabled=false; this.checked=false; this._text=''; this.classList={add(){},remove(){},toggle(){}}; }
    set textContent(value) { this._text=String(value); this.children=[]; }
    get textContent() { return this._text + this.children.map(n=>n.textContent).join(''); }
    append(...nodes) { this.children.push(...nodes); }
    replaceChildren(...nodes) { this._text=''; this.children=[...nodes]; }
    addEventListener(event, callback) { this.listeners[event]=callback; }
    querySelectorAll(selector) {
        const found=[];
        for (const child of this.children) {
            if (selector==='input[data-path]' && child.tag==='input' && child.dataset.path) found.push(child);
            found.push(...child.querySelectorAll(selector));
        }
        return found;
    }
    async event(type) { if (this.listeners[type]) await this.listeners[type]({target:this}); }
}

async function test() {
    const nodes = new Map([...html.matchAll(/id="([^"]+)"/g)].map(m=>[m[1],new Node()]));
    const doc = {getElementById(id){ assert(nodes.has(id), 'Missing real template ID: '+id); return nodes.get(id); },createElement(tag){return new Node(tag);}};
    let saved=[], status={status:'idle',counters:{}}, startCount=0, browseError=false;
    const network=[], intervals=new Map(); let seq=0;
    const fetch = async (url, options) => {
        network.push([url,options.method]);
        let body=options.body ? JSON.parse(options.body) : null, data={}, ok=true;
        if (url.endsWith('/gdrive-folders/config')) data={source_type:'gdi_js',root_path:'/0:/',selected_folders:saved};
        else if (url.endsWith('/gdrive-folders/browse')) {
            if (browseError) { ok=false; data={detail:'Index login is required.'}; }
            else if (body.path==='/0:/') data={path:body.path,folders:body.page_token ? [{name:'More',path:'/0:/More/'}] : [{name:'Movies',path:'/0:/Movies/'},{name:'Anime',path:'/0:/Anime/'}], next_page_token:body.page_token ? null : 'page2', page_index:body.page_index};
            else data={path:body.path,folders:[],next_page_token:null,page_index:0};
        } else if (url.endsWith('/gdrive-folders/selection')) { saved=body.folders; data={selected_folders:saved}; }
        else if (url.endsWith('/gdrive-scan/start')) { assert(saved.length); startCount++; status={status:'running',is_running:true,phase:'discovery',discovery_pages:2,discovery_files:4,counters:{}}; data={ok:true}; }
        else if (url.endsWith('/gdrive-scan/cancel')) { status={status:'cancelled',is_running:false,counters:{}}; data={ok:true}; }
        else if (url.endsWith('/gdrive-scan/status')) data={data:status};
        else assert.fail('Unexpected endpoint '+url);
        return {ok,status:ok?200:400,redirected:false,json:async()=>data};
    };
    const context={window:{}, document:doc, fetch, setTimeout, clearTimeout, AbortController, console,
        setInterval(fn){const id=++seq; intervals.set(id,fn); return id;},clearInterval(id){intervals.delete(id);},confirm:()=>true};
    vm.createContext(context); vm.runInContext(source,context);
    const controls=context.window, get=id=>nodes.get(id), settle=()=>new Promise(setImmediate);
    for (const name of ['initGDriveTools','startGDriveScan','confirmGDriveRescan','cancelGDriveScan','pollGDriveScan']) assert.equal(typeof controls[name],'function');
    controls.initGDriveTools(); await settle();
    assert.equal(get('gdi-controls').hidden,false);
    await controls.startGDriveScan('scan');
    assert.equal(startCount,0);
    assert.match(get('gd-scan-error').textContent,/Choose at least one folder/);
    await get('gdi-connect').event('click'); await settle();
    let checks=get('gdi-folders').querySelectorAll('input[data-path]');
    assert.equal(checks.length,2);
    for(const check of checks) { check.checked=true; await check.event('change'); }
    await get('gdi-save').event('click');
    assert.deepEqual(saved,['/0:/Movies/','/0:/Anime/']);
    assert.equal(get('gdi-saved').textContent,'Saved');
    await get('gdi-more').event('click'); await settle();
    assert.equal(get('gdi-folders').querySelectorAll('input[data-path]').length,3);
    assert.equal(get('gdi-more').hidden,true);
    await controls.startGDriveScan('scan');
    assert.equal(startCount,1);
    assert.match(get('gd-scan-current').textContent,/Finding videos.*4 videos/);
    assert.equal(get('gd-scan-cancel-btn').disabled,false);
    await controls.cancelGDriveScan(); await controls.pollGDriveScan();
    assert.equal(get('gd-scan-current').textContent,'Stopped.');
    assert.equal(intervals.size,0);
    // Reinitialize page state with persisted selections.
    controls.initGDriveTools(); await settle();
    assert.match(get('gdi-selected').textContent,/Movies/);
    await get('gdi-connect').event('click'); await settle();
    const open=get('gdi-folders').children[0].children[1];
    await open.event('click'); await settle();
    assert.equal(get('gdi-path').textContent,'/0:/Movies/');
    await get('gdi-up').event('click'); await settle();
    assert.equal(get('gdi-path').textContent,'/0:/');
    browseError=true;
    await get('gdi-connect').event('click'); await settle();
    assert.match(get('gdi-error').textContent,/login is required/);
    assert.equal(saved.length,2);
    assert(network.every(([url])=>url.startsWith('/api/admin/tools/')));
    console.log('PASS: global handlers, no-selection guard, browse, paging, multi-select, save/reload, start/stop, folder navigation, visible errors.');
}
test().catch(error=>{console.error(error);process.exitCode=1;});
