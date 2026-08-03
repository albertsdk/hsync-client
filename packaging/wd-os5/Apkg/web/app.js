/*
 * HSYNC web UI controller.
 *
 * - Loads / persists configuration through config_get.php and config_set.php.
 * - Drives the daemon (start/stop/restart/status) through service.php.
 * - Lightweight i18n: JSON dictionaries under i18n/, language selectable
 *   from the header and remembered in localStorage.
 *
 * No external dependencies; runs on any modern browser.
 */
(function () {
    'use strict';

    // Absolute base path: the dashboard injects this HTML via jQuery .load()
    // into a page served from a different URL, so relative fetch() calls
    // would resolve against the dashboard origin instead of /apps/hsync/.
    const BASE = '/apps/hsync';

    const SUPPORTED = ['en', 'it', 'de', 'es', 'fr'];
    const CHECKBOX_KEYS = [
        'HSYNC_LAN_PERSISTENT', 'HSYNC_LAN_REQUIRE_PAIRING',
        'HSYNC_WATCH', 'HSYNC_IGNORE_DELETE',
        'HSYNC_NO_COMPRESSION', 'HSYNC_DELTA_ENABLED', 'HSYNC_VERBOSE',
        'HSYNC_AUTOTUNE',
    ];

    const $  = (sel) => document.querySelector(sel);
    const $$ = (sel) => Array.from(document.querySelectorAll(sel));

    let dict = {};

    // -----------------------------------------------------------------
    // i18n
    // -----------------------------------------------------------------
    function pickLang() {
        const stored = localStorage.getItem('hsync.lang');
        if (stored && SUPPORTED.includes(stored)) return stored;
        const nav = (navigator.language || 'en').slice(0, 2).toLowerCase();
        return SUPPORTED.includes(nav) ? nav : 'en';
    }

    async function loadLang(lang) {
        try {
            const r = await fetch(`${BASE}/i18n/${lang}.json`, { cache: 'no-store' });
            dict = await r.json();
        } catch (_) {
            dict = {};
        }
        document.documentElement.lang = lang;
        applyDict();
    }

    function t(key, fallback) {
        return dict[key] || fallback || key;
    }

    function applyDict() {
        $$('[data-i18n]').forEach((el) => {
            const key = el.getAttribute('data-i18n');
            if (dict[key]) el.textContent = dict[key];
        });
    }

    function buildLangSelect() {
        const sel = $('#hsync-lang-select');
        SUPPORTED.forEach((code) => {
            const opt = document.createElement('option');
            opt.value = code;
            opt.textContent = code.toUpperCase();
            sel.appendChild(opt);
        });
        sel.value = pickLang();
        sel.addEventListener('change', () => {
            localStorage.setItem('hsync.lang', sel.value);
            loadLang(sel.value);
        });
    }

    // -----------------------------------------------------------------
    // Form ⇄ config
    // -----------------------------------------------------------------
    function applyConfig(cfg) {
        Object.keys(cfg).forEach((key) => {
            const el = document.getElementById(key);
            if (!el) return;
            if (el.type === 'checkbox') {
                el.checked = (cfg[key] === 'true' || cfg[key] === '1');
            } else {
                el.value = cfg[key] || '';
            }
        });
        if (cfg.__VERSION__) $('#hsync-app-version').textContent = cfg.__VERSION__;
        syncModeVisibility();
    }

    function collectConfig() {
        const out = {};
        $$('#hsync-config-form [name]').forEach((el) => {
            if (el.type === 'checkbox') {
                out[el.name] = el.checked ? 'true' : 'false';
            } else {
                out[el.name] = el.value;
            }
        });
        // Normalize booleans we know about (defensive).
        CHECKBOX_KEYS.forEach((k) => { if (!(k in out)) out[k] = 'false'; });
        return out;
    }

    function syncModeVisibility() {
        const mode = $('#HSYNC_MODE').value;
        $('#hsync-lan-options').hidden    = (mode !== 'lan');
        $('#hsync-server-options').hidden = (mode !== 'server');
    }

    // -----------------------------------------------------------------
    // Backend calls
    // -----------------------------------------------------------------
    async function loadConfig() {
        const r = await fetch(`${BASE}/config_get.php`, { cache: 'no-store' });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
    }

    async function saveConfig(cfg) {
        const body = new URLSearchParams();
        Object.keys(cfg).forEach((k) => body.append(k, cfg[k] == null ? '' : cfg[k]));
        const r = await fetch(`${BASE}/config_set.php`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: body.toString(),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
    }

    async function service(action) {
        const r = await fetch(`${BASE}/service.php?action=${encodeURIComponent(action)}`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
    }

    function setStatus(running) {
        const b = $('#hsync-status-badge');
        b.classList.remove('hsync-badge-running', 'hsync-badge-stopped', 'hsync-badge-unknown');
        if (running === true)       { b.classList.add('hsync-badge-running'); b.textContent = t('status.running', 'running'); }
        else if (running === false) { b.classList.add('hsync-badge-stopped'); b.textContent = t('status.stopped', 'stopped'); }
        else                        { b.classList.add('hsync-badge-unknown'); b.textContent = t('status.unknown', 'unknown'); }
    }

    function toast(msg, kind) {
        const el = $('#hsync-toast');
        el.textContent = msg;
        el.className = kind ? ('hsync-toast-' + kind) : '';
        el.hidden = false;
        setTimeout(() => { el.hidden = true; }, 4000);
    }

    async function refreshStatus() {
        try { setStatus((await service('status')).running); }
        catch (_) { setStatus(null); }
    }

    // -----------------------------------------------------------------
    // Live log viewer (incremental polling)
    // -----------------------------------------------------------------
    // The backend returns only the bytes appended since the byte offset we
    // last received, so a short poll feels real-time without re-transferring
    // the whole file. A negative offset asks the server for the tail window.
    const LOG_POLL_MS = 2000;
    let logOffset = -1;
    let logTimer  = null;

    async function pollLog() {
        const q = logOffset >= 0 ? `?offset=${logOffset}` : '';
        let j;
        try {
            const r = await fetch(`${BASE}/log_get.php${q}`, { cache: 'no-store' });
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            j = await r.json();
        } catch (e) {
            stopLog();
            toast(t('toast.log_error', 'Failed to load log') + ': ' + e.message, 'error');
            return;
        }
        logOffset = j.offset;
        if (j.data) {
            const v = $('#hsync-log-view');
            v.textContent += j.data;
            if ($('#hsync-log-follow').checked) v.scrollTop = v.scrollHeight;
        }
    }

    function startLog() {
        if (logTimer === null) logTimer = setInterval(pollLog, LOG_POLL_MS);
    }

    function stopLog() {
        if (logTimer !== null) { clearInterval(logTimer); logTimer = null; }
    }

    function openLog() {
        $('#hsync-log-view').textContent = '';
        logOffset = -1;                 // first poll requests the tail window
        $('#hsync-log-modal').hidden = false;
        pollLog();                      // immediate first paint
        startLog();
    }

    function closeLog() {
        stopLog();
        $('#hsync-log-modal').hidden = true;
    }

    // -----------------------------------------------------------------
    // Wiring
    // -----------------------------------------------------------------
    // The dashboard injects this HTML via jQuery .load() after its own
    // DOMContentLoaded has already fired, so listening for that event
    // would never run init(). The script is placed at the end of the
    // body, so all referenced elements already exist when it runs.
    async function init() {
        if (!$('#hsync-app')) return; // not in DOM yet (defensive)

        buildLangSelect();
        await loadLang(pickLang());

        try { applyConfig(await loadConfig()); }
        catch (e) { toast(t('toast.load_error', 'Failed to load configuration') + ': ' + e.message, 'error'); }

        $('#HSYNC_MODE').addEventListener('change', syncModeVisibility);

        $('#hsync-btn-save').addEventListener('click', async () => {
            try {
                await saveConfig(collectConfig());
                toast(t('toast.saved', 'Configuration saved'), 'ok');
            } catch (e) {
                toast(t('toast.save_error', 'Failed to save') + ': ' + e.message, 'error');
            }
        });

        $('#hsync-btn-start')  .addEventListener('click', async () => { try { await service('start');   toast(t('toast.started', 'Service started'), 'ok'); } catch (e) { toast(e.message, 'error'); } refreshStatus(); });
        $('#hsync-btn-stop')   .addEventListener('click', async () => { try { await service('stop');    toast(t('toast.stopped', 'Service stopped'), 'ok'); } catch (e) { toast(e.message, 'error'); } refreshStatus(); });
        $('#hsync-btn-restart').addEventListener('click', async () => { try { await service('restart'); toast(t('toast.restarted', 'Service restarted'), 'ok'); } catch (e) { toast(e.message, 'error'); } refreshStatus(); });

        $('#hsync-btn-log')   .addEventListener('click', openLog);
        $('#hsync-log-close') .addEventListener('click', closeLog);
        $('#hsync-log-clear') .addEventListener('click', () => { $('#hsync-log-view').textContent = ''; });

        refreshStatus();
        setInterval(refreshStatus, 10000);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
