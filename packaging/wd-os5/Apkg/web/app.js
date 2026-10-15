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

    const SUPPORTED = ['en', 'it', 'de', 'fr', 'es', 'pt', 'zh', 'ru', 'nl', 'pl', 'ja'];
    // Native language names for the selector, mirroring the desktop client's
    // SUPPORTED_LANGUAGES so the two front-ends present the same list.
    const LANGUAGE_NAMES = {
        en: 'English',
        it: 'Italiano',
        de: 'Deutsch',
        fr: 'Français',
        es: 'Español',
        pt: 'Português (Brasil)',
        zh: '中文（简体）',
        ru: 'Русский',
        nl: 'Nederlands',
        pl: 'Polski',
        ja: '日本語',
    };
    const CHECKBOX_KEYS = [
        'HSYNC_P2P_REQUIRE_PAIRING',
        'HSYNC_UPNP_ENABLED',
        'HSYNC_BOOTNODE_TOFU',
        'HSYNC_SERVER_TOFU',
        'HSYNC_P2P_PEER_DOWNLOAD', 'HSYNC_P2P_ALLOW_UNAUTHENTICATED_WRITES',
        'HSYNC_P2P_ALLOW_UNAUTHENTICATED',
        'HSYNC_IGNORE_DELETE',
        'HSYNC_NO_COMPRESSION', 'HSYNC_DELTA_ENABLED', 'HSYNC_VERBOSE',
        'HSYNC_AUTOTUNE',
        'HSYNC_ALLOW_DELETIONS',
    ];

    // Fields whose values are interpolated into the generated connect command;
    // editing any of them must re-render the section.
    const CONNECT_FIELDS = ['HSYNC_BUCKET', 'HSYNC_P2P_GROUP', 'HSYNC_P2P_KEY', 'HSYNC_BOOTNODE', 'HSYNC_BOOTNODE_FINGERPRINT', 'HSYNC_BOOTNODE_TOFU', 'HSYNC_BOOTNODE_TRUST', 'HSYNC_BOOTNODE_PORTS'];

    function isConnectField(id) {
        return CONNECT_FIELDS.includes(id);
    }

    // Placeholder shown for the local folder on the *other* device (unknowable
    // from the NAS) and the fixed mask used for the LAN shared key in the
    // generated connect command (fixed length so it does not leak the key length).
    const PATH_PLACEHOLDER = '<local-folder>';
    const KEY_MASK = '••••••••';

    // Reveal state + last computed unmasked command, preserved across re-renders
    // so typing in another field does not re-mask an already-revealed key.
    let keyRevealed = false;
    let connectUnmasked = '';

    // ID lookups use getElementById: querySelector can miss nodes while the
    // WD dashboard is still injecting the fragment (jQuery .load / remotewd).
    function $(sel) {
        if (sel.charAt(0) === '#' && /^#[A-Za-z][\w-]*$/.test(sel)) {
            return document.getElementById(sel.slice(1));
        }
        const root = document.getElementById('hsync-app');
        try {
            return (root || document).querySelector(sel);
        } catch (_) {
            return null;
        }
    }
    function $$ (sel) {
        const root = document.getElementById('hsync-app');
        try {
            return Array.from((root || document).querySelectorAll(sel));
        } catch (_) {
            return [];
        }
    }

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
        // Re-render the connect command so the reveal-toggle label tracks the
        // new language (applyDict resets it to the data-i18n default).
        renderConnectCommand();
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
        if (!sel) return;
        SUPPORTED.forEach((code) => {
            const opt = document.createElement('option');
            opt.value = code;
            opt.textContent = LANGUAGE_NAMES[code] || code.toUpperCase();
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
                // Firefox throws / logs if <input pattern> is not valid under
                // the unicodeSets (`v`) flag; never let that abort applyConfig.
                try { el.value = cfg[key] || ''; } catch (_) { /* ignore */ }
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
        const modeEl = $('#HSYNC_MODE');
        const mode = modeEl ? modeEl.value : 'p2p';
        const p2p_section = $('#hsync-p2p-options');
        const srv = $('#hsync-server-options');
        if (p2p_section) p2p_section.hidden = (mode !== 'p2p');
        if (srv) srv.hidden = (mode !== 'server');
        renderConnectCommand();
    }

    // -----------------------------------------------------------------
    // "Connect another device" command
    // -----------------------------------------------------------------
    // Builds the hsync command to run on a second device to join the sync
    // group coordinated by this NAS. Only the connection-matching fields are
    // emitted (bucket, mode, group, p2p key); per-device sync-behaviour flags
    // (ignore-delete, compression, ...) are intentionally omitted — they
    // do not need to match between peers. The NAS role is also omitted: LAN
    // master election resolves the remote device's role automatically regardless
    // of the NAS role (auto/master/slave/never-master).
    function buildConnectCommand() {
        const bucketEl = $('#HSYNC_BUCKET');
        const bucket = (bucketEl && bucketEl.value.trim())
            || (bucketEl && bucketEl.placeholder)
            || '<bucket>';
        const groupEl = $('#HSYNC_P2P_GROUP');
        const keyEl = $('#HSYNC_P2P_KEY');
        const group = groupEl ? groupEl.value.trim() : '';
        const key = keyEl ? keyEl.value : '';

        // POSIX single-quote every interpolated value so the generated command
        // is safe to paste into a shell regardless of its contents (no $ /
        // backtick / " expansion, no < > redirection from the placeholder).
        let cmd = 'hsync p2p -b ' + shellQuote(bucket)
            + ' -p ' + shellQuote(PATH_PLACEHOLDER);
        if (group) cmd += ' --group ' + shellQuote(group);

        // Cross-network discovery: the other device must use the same bootnode,
        // so it travels as an env prefix (HOST[:PORT], no scheme), together
        // with the optional fingerprint pin / trust-on-first-use.
        const bootnodeEl = $('#HSYNC_BOOTNODE');
        const bootnode = bootnodeEl ? bootnodeEl.value.trim() : '';
        const fpEl = $('#HSYNC_BOOTNODE_FINGERPRINT');
        const fp = fpEl ? fpEl.value.trim() : '';
        const tofuEl = $('#HSYNC_BOOTNODE_TOFU');
        const tofu = !!tofuEl && tofuEl.checked;
        const trustEl = $('#HSYNC_BOOTNODE_TRUST');
        const trust = trustEl ? trustEl.value : '';
        const portsEl = $('#HSYNC_BOOTNODE_PORTS');
        const ports = portsEl ? portsEl.value.trim() : '';
        let envPrefix = '';
        if (bootnode) envPrefix += 'HSYNC_BOOTNODE=' + shellQuote(bootnode) + ' ';
        if (fp) envPrefix += 'HSYNC_BOOTNODE_FINGERPRINT=' + shellQuote(fp) + ' ';
        if (tofu) envPrefix += 'HSYNC_BOOTNODE_TOFU=true ';
        if (trust) envPrefix += 'HSYNC_BOOTNODE_TRUST=' + shellQuote(trust) + ' ';
        if (ports) envPrefix += 'HSYNC_BOOTNODE_PORTS=' + shellQuote(ports) + ' ';
        if (envPrefix) cmd = envPrefix + cmd;

        const showKey = key.length > 0;
        const masked = showKey ? ('HSYNC_P2P_KEY=' + shellQuote(KEY_MASK) + ' ' + cmd) : cmd;
        const unmasked = showKey ? ('HSYNC_P2P_KEY=' + shellQuote(key) + ' ' + cmd) : cmd;
        return { masked, unmasked, showKey };
    }

    // POSIX shell single-quoting: wrap in '...' and escape embedded ' as '\''.
    function shellQuote(v) {
        return "'" + String(v).replace(/'/g, "'\\''") + "'";
    }

    function renderConnectCommand() {
        const cmdEl = $('#hsync-connect-command');
        if (!cmdEl) return;

        const modeEl = $('#HSYNC_MODE');
        const isP2p = !!modeEl && modeEl.value === 'p2p';

        // Resolve the command text first: it is the core of this section, so
        // a missing optional element (note/button) can never leave it empty.
        let showKey = false;
        if (isP2p) {
            const built = buildConnectCommand();
            connectUnmasked = built.unmasked;
            showKey = built.showKey;
            cmdEl.hidden = false;
            // textarea uses .value; a <div>/<code> uses textContent. Set both
            // so the command is visible regardless of which tag the markup uses.
            const text = keyRevealed ? built.unmasked : built.masked;
            cmdEl.value = text;
            cmdEl.textContent = text;
        } else {
            connectUnmasked = '';
            cmdEl.hidden = true;
            cmdEl.value = '';
            cmdEl.textContent = '';
        }

        const noteP2p = $('#hsync-connect-note-p2p');
        const noteServer = $('#hsync-connect-note-server');
        if (noteLan) noteP2p.hidden = !isLan;
        if (noteServer) noteServer.hidden = isLan;

        const copyBtn = $('#hsync-btn-copy');
        const revealBtn = $('#hsync-btn-reveal-key');
        if (copyBtn) copyBtn.hidden = !isLan;
        if (revealBtn) {
            revealBtn.hidden = !isLan || !showKey;
            revealBtn.textContent = keyRevealed
                ? t('action.hide_key', 'Hide key')
                : t('action.reveal_key', 'Show key');
        }
    }

    async function copyConnectCommand() {
        if (!connectUnmasked) return;
        try {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                await navigator.clipboard.writeText(connectUnmasked);
            } else {
                const ta = document.createElement('textarea');
                ta.value = connectUnmasked;
                ta.style.position = 'fixed';
                ta.style.opacity = '0';
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
            }
            toast(t('action.copy_done', 'Copied'), 'ok');
        } catch (e) {
            toast(t('toast.save_error', 'Failed') + ': ' + e.message, 'error');
        }
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
        if (!b) return;
        b.classList.remove('hsync-badge-running', 'hsync-badge-stopped', 'hsync-badge-unknown');
        if (running === true)       { b.classList.add('hsync-badge-running'); b.textContent = t('status.running', 'running'); }
        else if (running === false) { b.classList.add('hsync-badge-stopped'); b.textContent = t('status.stopped', 'stopped'); }
        else                        { b.classList.add('hsync-badge-unknown'); b.textContent = t('status.unknown', 'unknown'); }
    }

    function toast(msg, kind) {
        const el = $('#hsync-toast');
        if (!el) return;
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
        const view = $('#hsync-log-view');
        const modal = $('#hsync-log-modal');
        if (view) view.textContent = '';
        logOffset = -1;                 // first poll requests the tail window
        if (modal) modal.hidden = false;
        pollLog();                      // immediate first paint
        startLog();
    }

    function closeLog() {
        stopLog();
        const modal = $('#hsync-log-modal');
        if (modal) modal.hidden = true;
    }

    // -----------------------------------------------------------------
    // Wiring
    // -----------------------------------------------------------------
    // The dashboard injects this HTML via jQuery .load() after its own
    // DOMContentLoaded has already fired, so listening for that event
    // would never run init(). The script is placed at the end of the
    // body, so all referenced elements already exist when it runs.
    async function init() {
        // The dashboard may insert the fragment without the #hsync-app wrapper
        // (jQuery .load('index.html #hsync-app') injects children only). Proceed
        // as long as the config form or the connect-command node is present.
        if (!$('#hsync-app') && !$('#hsync-config-form') && !$('#hsync-connect-command')) {
            return;
        }

        buildLangSelect();
        await loadLang(pickLang());

        try { applyConfig(await loadConfig()); }
        catch (e) { toast(t('toast.load_error', 'Failed to load configuration') + ': ' + e.message, 'error'); }
        renderConnectCommand();
        // The dashboard (and remotewd's injectedScript) can replace nodes after
        // first paint; re-apply so the command survives that pass.
        [50, 200, 1000].forEach((ms) => setTimeout(renderConnectCommand, ms));

        // Delegate on document: the WD dashboard / remotewd can re-inject input
        // nodes after init(), which would orphan per-element listeners. A
        // delegated listener survives node replacement. Listen to input,
        // change and keyup: old WebKit builds (OS5 dashboard) can miss `input`
        // events on some fields (autofill, password managers, select helpers).
        document.addEventListener('input', (e) => {
            if (isConnectField(e.target.id)) renderConnectCommand();
        });
        document.addEventListener('change', (e) => {
            if (e.target.id === 'HSYNC_MODE') { syncModeVisibility(); return; }
            if (isConnectField(e.target.id)) renderConnectCommand();
        });
        document.addEventListener('keyup', (e) => {
            if (isConnectField(e.target.id)) renderConnectCommand();
        });

        const copyBtn = $('#hsync-btn-copy');
        if (copyBtn) copyBtn.addEventListener('click', copyConnectCommand);
        const revealBtn = $('#hsync-btn-reveal-key');
        if (revealBtn) revealBtn.addEventListener('click', () => {
            keyRevealed = !keyRevealed;
            renderConnectCommand();
        });

        const saveBtn = $('#hsync-btn-save');
        if (saveBtn) saveBtn.addEventListener('click', async () => {
            try {
                await saveConfig(collectConfig());
                toast(t('toast.saved', 'Configuration saved'), 'ok');
                // Re-sync the connect command with what config_set.php actually
                // persisted: the backend normalizes values (bucket fallback,
                // boolean true/false, empty invalid numerics), so rebuild the
                // command from the reloaded config after every successful save.
                try { applyConfig(await loadConfig()); }
                catch (_) { /* keep the current form values */ }
                renderConnectCommand();
            } catch (e) {
                toast(t('toast.save_error', 'Failed to save') + ': ' + e.message, 'error');
            }
        });

        function bind(id, handler) {
            const el = $('#' + id);
            if (el) el.addEventListener('click', handler);
        }
        bind('hsync-btn-start',   async () => { try { await service('start');   toast(t('toast.started', 'Service started'), 'ok'); } catch (e) { toast(e.message, 'error'); } refreshStatus(); });
        bind('hsync-btn-stop',    async () => { try { await service('stop');    toast(t('toast.stopped', 'Service stopped'), 'ok'); } catch (e) { toast(e.message, 'error'); } refreshStatus(); });
        bind('hsync-btn-restart', async () => { try { await service('restart'); toast(t('toast.restarted', 'Service restarted'), 'ok'); } catch (e) { toast(e.message, 'error'); } refreshStatus(); });
        bind('hsync-btn-log', openLog);
        bind('hsync-log-close', closeLog);
        bind('hsync-log-clear', () => { const v = $('#hsync-log-view'); if (v) v.textContent = ''; });

        refreshStatus();
        setInterval(refreshStatus, 10000);
    }

    let boots = 0;
    function boot() {
        if (!$('#hsync-app') && !$('#hsync-config-form') && !$('#hsync-connect-command')) {
            if (boots++ < 20) setTimeout(boot, 50);
            return;
        }
        init();
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }
})();
