/* GDI-JS folder picker and Drive scan controls. No credentials in this page. */
(() => {
    'use strict';
    const base = '/api/admin/tools/';
    let config = null, current = '', selected = new Set(), dirty = false;
    let nextToken = null, pageIndex = 0, browsing = false, timer = null, starting = false;
    let configReady = null, loadedFolders = [];
    let polling = false, statusEpoch = 0;
    const el = id => document.getElementById(id);
    const text = (id, value) => { el(id).textContent = value; };
    function error(id, message) {
        const node = el(id);
        node.textContent = message || '';
        node.hidden = !message;
        node.style.display = message ? '' : 'none';
    }
    async function api(path, method = 'GET', body) {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 35000);
        try {
            const response = await fetch(base + path, {
                method, signal: controller.signal,
                headers: body === undefined ? {} : {'Content-Type': 'application/json'},
                body: body === undefined ? undefined : JSON.stringify(body),
            });
            if (response.status === 401 || response.redirected) throw new Error('Admin session expired. Sign in again.');
            let data;
            try { data = await response.json(); }
            catch (_) { throw new Error('Server did not return JSON. Check your login and app logs.'); }
            if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Request failed. Check the app logs.');
            return data;
        } catch (exc) {
            if (exc.name === 'AbortError') throw new Error('Request timed out. Check the index connection and try again.');
            throw exc;
        } finally { clearTimeout(timeout); }
    }
    function renderSelection() {
        const box = el('gdi-selected');
        box.replaceChildren();
        if (!selected.size) box.textContent = 'No folders selected.';
        for (const path of selected) {
            const row = document.createElement('div');
            row.className = 'flex items-center justify-between gap-2 mt-2';
            const label = document.createElement('span');
            label.textContent = decodeURIComponent(path);
            label.style.overflowWrap = 'anywhere';
            const remove = document.createElement('button');
            remove.type = 'button'; remove.className = 'btn'; remove.textContent = 'Remove';
            remove.addEventListener('click', () => { selected.delete(path); changed(); });
            row.append(label, remove); box.append(row);
        }
        el('gdi-current').checked = selected.has(current);
        text('gdi-saved', dirty ? 'Unsaved changes' : 'Saved');
        for (const node of el('gdi-folders').querySelectorAll('input[data-path]')) node.checked = selected.has(node.dataset.path);
    }
    function changed() { dirty = true; renderSelection(); }
    async function loadConfig() {
        config = await api('gdrive-folders/config');
        el('gdi-controls').hidden = config.source_type !== 'gdi_js';
        if (config.source_type !== 'gdi_js') {
            text('gdi-hint', 'To browse your index folders, choose GDI-JS under Settings → Google Drive Source and save its URL/login.');
            return;
        }
        current = config.root_path;
        selected = new Set(config.selected_folders || []);
        text('gdi-hint', 'Browse folders, tick the ones to scan, and save. You can add more folders later.');
        text('gdi-path', decodeURIComponent(current));
        el('gdi-up').disabled = true;
        renderSelection();
    }
    function renderFolders() {
        const box = el('gdi-folders');
        box.replaceChildren();
        if (!loadedFolders.length) box.textContent = nextToken ? 'No subfolders on this page. Load more to continue.' : 'No subfolders. Select this folder above to scan its videos.';
        for (const item of loadedFolders) {
            const row = document.createElement('div');
            row.className = 'flex items-center justify-between gap-2';
            row.style.cssText = 'padding:12px 0;border-bottom:1px solid var(--border)';
            const label = document.createElement('label');
            label.className = 'flex items-center gap-2'; label.style.minWidth = '0';
            const check = document.createElement('input');
            check.type = 'checkbox'; check.dataset.path = item.path; check.checked = selected.has(item.path);
            check.addEventListener('change', () => {
                if (check.checked) selected.add(item.path); else selected.delete(item.path);
                changed();
            });
            const name = document.createElement('span'); name.textContent = item.name; name.style.overflowWrap = 'anywhere';
            const open = document.createElement('button');
            open.type = 'button'; open.className = 'btn'; open.textContent = 'Open';
            open.addEventListener('click', () => browse(item.path));
            label.append(check, name); row.append(label, open); box.append(row);
        }
    }
    async function ready() {
        await configReady;
        if (!config || config.source_type !== 'gdi_js') {
            error('gdi-error', 'Save a valid GDI-JS source in Settings, then reload this page.');
            return false;
        }
        return true;
    }
    async function browse(path, more = false) {
        if (!await ready() || browsing) return;
        path = path || config.root_path;
        browsing = true;
        el('gdi-connect').disabled = true; el('gdi-more').disabled = true;
        error('gdi-error', '');
        text('gdi-hint', 'Connecting and loading folders…');
        try {
            const page = await api('gdrive-folders/browse', 'POST', {
                path, page_token: more ? nextToken : null, page_index: more ? pageIndex + 1 : 0,
            });
            current = page.path;
            nextToken = page.next_page_token;
            pageIndex = page.page_index;
            if (!more) loadedFolders = [];
            const seen = new Set(loadedFolders.map(f => f.path));
            loadedFolders.push(...page.folders.filter(f => !seen.has(f.path)));
            text('gdi-path', decodeURIComponent(current));
            el('gdi-up').disabled = current === config.root_path;
            el('gdi-more').hidden = !nextToken;
            text('gdi-hint', 'Connected. Choose folders below; selecting a folder includes all its subfolders.');
            renderFolders(); renderSelection();
        } catch (exc) { error('gdi-error', exc.message); text('gdi-hint', 'Could not load folders. Check the message below.'); }
        finally { browsing = false; el('gdi-connect').disabled = false; el('gdi-more').disabled = false; }
    }
    async function saveSelection() {
        if (!await ready()) return;
        const result = await api('gdrive-folders/selection', 'PUT', {folders: [...selected]});
        selected = new Set(result.selected_folders || []);
        dirty = false; renderSelection();
    }
    function stopPolling() { if (timer) clearInterval(timer); timer = null; }
    function startPolling() { if (!timer) timer = setInterval(pollGDriveScan, 1500); }
    function renderGDriveScan(s) {
        const status = s.status || 'idle';
        const pill = el('gd-scan-status-pill');
        pill.className = 'status-pill status-' + status; pill.textContent = status;
        const counters = s.counters || {};
        for (const [id, key] of Object.entries({'gd-processed':'processed', 'gd-indexed':'indexed', 'gd-dup':'skipped_dup', 'gd-meta':'skipped_meta', 'gd-nonvid':'skipped_nonvid', 'gd-errors':'errors'})) text(id, counters[key] || 0);
        text('gd-scan-elapsed', s.elapsed || '0s');
        const bar = el('gd-scan-bar');
        el('gd-scan-start-btn').disabled = starting || !!s.is_running;
        el('gd-rescan-btn').disabled = starting || !!s.is_running;
        el('gd-scan-cancel-btn').disabled = !s.is_running;
        if (s.is_running) {
            bar.classList.toggle('indeterminate', !s.has_progress);
            bar.style.width = s.has_progress ? (s.progress || 0) + '%' : '';
            text('gd-scan-current', s.phase === 'discovery'
                ? `Finding videos · ${s.discovery_pages || 0} pages · ${s.discovery_files || 0} videos`
                : (s.has_progress ? `Indexing · ${s.current_id || 0}/${s.current_target_id || 0}` : `Indexing · ${s.current_id || 0} videos processed`));
            error('gd-scan-error', ''); startPolling();
        } else {
            bar.classList.remove('indeterminate'); bar.style.width = status === 'completed' ? '100%' : '0%';
            text('gd-scan-current', s.resumable ? 'Paused — Start Scan resumes the saved page.' : ({completed:'Scan complete.', cancelled:'Stopped.', error:'Scan failed.'}[status] || 'No scan running'));
            error('gd-scan-error', s.error || ''); stopPolling();
        }
    }
    async function pollGDriveScan() {
        if (polling) return;
        polling = true;
        const epoch = statusEpoch;
        try { const result = await api('gdrive-scan/status'); if (epoch === statusEpoch) renderGDriveScan(result.data || {}); }
        catch (exc) { if (epoch === statusEpoch) error('gd-scan-error', exc.message); }
        finally { polling = false; }
    }
    async function startGDriveScan(mode) {
        if (starting) return;
        starting = true; el('gd-scan-start-btn').disabled = true; el('gd-rescan-btn').disabled = true;
        error('gd-scan-error', '');
        try {
            await configReady;
            if (!config) throw new Error('Source settings could not be loaded. Reload this page and try again.');
            if (config?.source_type === 'gdi_js') {
                if (!selected.size) throw new Error('Choose at least one folder above before scanning.');
                if (dirty) await saveSelection();
            }
            statusEpoch++;
            await api('gdrive-scan/start', 'POST', {mode});
            starting = false;
            startPolling();
            await pollGDriveScan();
        } catch (exc) { error('gd-scan-error', exc.message); }
        finally {
            starting = false;
            if (!timer) { el('gd-scan-start-btn').disabled = false; el('gd-rescan-btn').disabled = false; }
        }
    }
    async function cancelGDriveScan() {
        try { await api('gdrive-scan/cancel', 'POST'); text('gd-scan-current', 'Stop requested; finishing the current request…'); }
        catch (exc) { error('gd-scan-error', exc.message); }
    }
    function confirmGDriveRescan() {
        if (confirm('Refresh videos in your saved folders? Existing media is kept if discovery fails; missing files are not deleted.')) startGDriveScan('rescan');
    }
    function initGDriveTools() {
        configReady = loadConfig().catch(exc => { error('gdi-error', exc.message); text('gdi-hint', 'Could not load source settings.'); });
        el('gdi-connect').addEventListener('click', () => browse(current));
        el('gdi-root').addEventListener('click', async () => { if (await ready()) await browse(config.root_path); });
        el('gdi-up').addEventListener('click', async () => {
            if (!await ready()) return;
            const parent = current.replace(/[^/]+\/$/, '');
            if (parent.startsWith(config.root_path)) browse(parent);
        });
        el('gdi-more').addEventListener('click', () => browse(current, true));
        el('gdi-current').addEventListener('change', async event => {
            if (!await ready()) return;
            if (event.target.checked) selected.add(current); else selected.delete(current);
            changed();
        });
        el('gdi-save').addEventListener('click', async () => {
            el('gdi-save').disabled = true;
            try { await saveSelection(); error('gdi-error', ''); }
            catch (exc) { error('gdi-error', exc.message); }
            finally { el('gdi-save').disabled = false; }
        });
        pollGDriveScan();
    }
    Object.assign(window, {initGDriveTools, startGDriveScan, confirmGDriveRescan, cancelGDriveScan, pollGDriveScan});
})();
