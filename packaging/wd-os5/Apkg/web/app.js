/*
 * HSYNC web UI controller.
 *
 * - Loads / persists configuration through config_get.cgi and config_set.cgi.
 * - Drives the daemon (start/stop/restart/status) through service.cgi.
 * - Lightweight i18n: JSON dictionaries under i18n/, language selectable
 *   from the header and remembered in localStorage.
 *
 * No external dependencies; runs on any modern browser.
 */
(function () {
    'use strict';

    const SUPPORTED = ['en', 'it', 'de', 'es', 'fr'];
    const CHECKBOX_KEYS = [
        'HSYNC_LAN_PERSISTENT', 'HSYNC_LAN_HTTPS',
        'HSYNC_WATCH', 'HSYNC_IGNORE_DELETE',
        'HSYNC_NO_COMPRESSION', 'HSYNC_VERBOSE',
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
            const r = await fetch(`i18n/${lang}.json`, { cache: 'no-store' });
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
        const sel = $('#lang-select');
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
        if (cfg.__VERSION__) $('#app-version').textContent = cfg.__VERSION__;
        syncModeVisibility();
    }

    function collectConfig() {
        const out = {};
        $$('#config-form [name]').forEach((el) => {
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
        $('#lan-options').hidden    = (mode !== 'lan');
        $('#server-options').hidden = (mode !== 'server');
    }

    // -----------------------------------------------------------------
    // Backend calls
    // -----------------------------------------------------------------
    async function loadConfig() {
        const r = await fetch('config_get.cgi', { cache: 'no-store' });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
    }

    async function saveConfig(cfg) {
        const body = new URLSearchParams();
        Object.keys(cfg).forEach((k) => body.append(k, cfg[k] == null ? '' : cfg[k]));
        const r = await fetch('config_set.cgi', {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: body.toString(),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
    }

    async function service(action) {
        const r = await fetch(`service.cgi?action=${encodeURIComponent(action)}`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
    }

    function setStatus(running) {
        const b = $('#status-badge');
        b.classList.remove('badge-running', 'badge-stopped', 'badge-unknown');
        if (running === true)       { b.classList.add('badge-running'); b.textContent = t('status.running', 'running'); }
        else if (running === false) { b.classList.add('badge-stopped'); b.textContent = t('status.stopped', 'stopped'); }
        else                        { b.classList.add('badge-unknown'); b.textContent = t('status.unknown', 'unknown'); }
    }

    function toast(msg, kind) {
        const el = $('#toast');
        el.textContent = msg;
        el.className = kind || '';
        el.hidden = false;
        setTimeout(() => { el.hidden = true; }, 4000);
    }

    async function refreshStatus() {
        try { setStatus((await service('status')).running); }
        catch (_) { setStatus(null); }
    }

    // -----------------------------------------------------------------
    // Wiring
    // -----------------------------------------------------------------
    document.addEventListener('DOMContentLoaded', async () => {
        buildLangSelect();
        await loadLang(pickLang());

        try { applyConfig(await loadConfig()); }
        catch (e) { toast(t('toast.load_error', 'Failed to load configuration') + ': ' + e.message, 'error'); }

        $('#HSYNC_MODE').addEventListener('change', syncModeVisibility);

        $('#config-form').addEventListener('submit', async (ev) => {
            ev.preventDefault();
            try {
                await saveConfig(collectConfig());
                toast(t('toast.saved', 'Configuration saved'), 'ok');
            } catch (e) {
                toast(t('toast.save_error', 'Failed to save') + ': ' + e.message, 'error');
            }
        });

        $('#btn-start')  .addEventListener('click', async () => { try { await service('start');   toast(t('toast.started', 'Service started'), 'ok'); } catch (e) { toast(e.message, 'error'); } refreshStatus(); });
        $('#btn-stop')   .addEventListener('click', async () => { try { await service('stop');    toast(t('toast.stopped', 'Service stopped'), 'ok'); } catch (e) { toast(e.message, 'error'); } refreshStatus(); });
        $('#btn-restart').addEventListener('click', async () => { try { await service('restart'); toast(t('toast.restarted', 'Service restarted'), 'ok'); } catch (e) { toast(e.message, 'error'); } refreshStatus(); });

        refreshStatus();
        setInterval(refreshStatus, 10000);
    });
})();
