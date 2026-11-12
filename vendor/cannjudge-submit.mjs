import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

export const SUBMIT_URL = 'https://cannjudge.cn/ccpc/ladder_2026_changchun/cosine_similarity/submit';
const projectDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
export const sha256 = data => createHash('sha256').update(data).digest('hex');
export const normalizeSource = source => source.replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n');

export function durationToUs(value) {
    const match = String(value).trim().match(/^(\d+(?:\.\d*)?|\.\d+)(?:\s*)(ns|[uµμ]s|ms|s)$/i);
    if (!match) return NaN;
    const factors = { ns: 0.001, us: 1, 'µs': 1, 'μs': 1, ms: 1000, s: 1000000 };
    const result = Number(match[1]) * factors[match[2].toLowerCase()];
    return Number.isFinite(result) ? result : NaN;
}

export function optionsFromArgs(args) {
    const result = { source: path.join(projectDir, 'kernel.asc') };
    for (let i = 0; i < args.length; i++) {
        if (args[i] === '--source' && args[i + 1]) result.source = path.resolve(args[++i]);
        else if (args[i] === '--login-only') result.loginOnly = true;
        else if (args[i] === '--dry-run') result.dryRun = true;
        else if (args[i] === '--show-browser') result.showBrowser = true;
        else throw new Error(`Unknown or incomplete argument: ${args[i]}`);
    }
    return result;
}

export function parseResult(text, rows, url) {
    const status = text.match(/提交状态[：:]\s*([^\r\n]+)/)?.[1]?.trim() ?? '';
    const submissionId = text.match(/提交\s*ID[：:]\s*(\d+)/i)?.[1] ?? null;
    const count = text.match(/通过[：:]\s*(\d+)\s*\/\s*(\d+)/);
    const toNumber = value => { const clean = String(value).replace(/[^\d.+-]/g, ''); return clean ? Number(clean) : NaN; };
    const cases = rows.filter(cells => cells.length >= 5 && /^\d+$/.test(cells[0].trim())).map(cells => ({
        index: Number(cells[0]), status: cells[1].trim(),
        errorPercent: toNumber(cells[2]), durationUs: durationToUs(cells[3]),
        bestUs: durationToUs(cells[4]),
        durationText: cells[3].trim(), bestText: cells[4].trim(),
    }));
    const pending = /running|pending|queue|compil(?:e|ing)|judging|waiting|运行中|排队|评测中|编译中/i.test(status)
        && !/error|fail|失败|错误/i.test(status);
    const terminal = !pending && /pass|accepted|fail|error|wrong|limit|timeout|rejected|通过|失败|错误|超时/i.test(status);
    const passed = /^(pass|accepted|通过)$/i.test(status) && !!count && Number(count[2]) > 0
        && Number(count[1]) === Number(count[2]) && cases.length === Number(count[2])
        && cases.every(item => /^pass$/i.test(item.status) && item.errorPercent === 0 && item.durationUs > 0);
    return { url, submissionId, status, terminal, passed,
        passedCases: count ? Number(count[1]) : 0, totalCases: count ? Number(count[2]) : 0, cases };
}

export function cooldownSeconds(text) {
    if (!/频繁|冷却|稍后|间隔|等待|cooldown|too many|rate limit|wait/i.test(text)) return null;
    const seconds = text.match(/(\d+)\s*(?:秒|seconds?\b|s\b)/i);
    const minutes = text.match(/(\d+)\s*(?:分钟|minutes?\b)/i);
    const value = seconds ? Number(seconds[1]) : minutes ? Number(minutes[1]) * 60 : null;
    return value > 0 && value <= 600 ? value : null;
}

export function compareCases(current, baseline) {
    if (!baseline || current.length !== baseline.length || !current.length) return null;
    const rows = current.map((entry, i) => {
        const old = typeof baseline[i] === 'number' ? baseline[i] : baseline[i].durationUs;
        if (!(old > 0) || !(entry.durationUs > 0)) throw new Error('Invalid timing in comparison.');
        return { index: entry.index, oldUs: old, newUs: entry.durationUs,
            changePercent: (entry.durationUs / old - 1) * 100 };
    });
    return { rows, geometricSpeedup: Math.exp(rows.reduce((sum, r) => sum + Math.log(r.oldUs / r.newUs), 0) / rows.length) };
}

export async function acquireLock(lockPath) {
    for (let attempt = 0; attempt < 2; attempt++) {
        try {
            const handle = await fs.open(lockPath, 'wx');
            await handle.writeFile(JSON.stringify({ pid: process.pid, startedAt: new Date().toISOString() }));
            return async () => { await handle.close(); await fs.unlink(lockPath).catch(() => {}); };
        } catch (error) {
            if (error.code !== 'EEXIST') throw error;
            let previous;
            try { previous = JSON.parse(await fs.readFile(lockPath, 'utf8')); }
            catch { throw new Error('Another submit process is starting. Please wait.'); }
            try { process.kill(previous.pid, 0); }
            catch (checkError) {
                if (checkError.code === 'ESRCH') { await fs.unlink(lockPath); continue; }
                throw checkError;
            }
            throw new Error(`Another submit is active (PID ${previous.pid}). No second submission was sent.`);
        }
    }
    throw new Error('Cannot acquire the submission lock.');
}

export function loadPlaywright() {
    const require = createRequire(import.meta.url);
    const candidates = [process.env.CANNJUDGE_NODE_MODULES,
        path.join(projectDir, '.tools', 'cannjudge-submit', 'node_modules')].filter(Boolean);
    for (const directory of candidates) {
        try { return require(path.join(directory, 'playwright')); }
        catch (error) { if (error.code !== 'MODULE_NOT_FOUND') throw error; }
    }
    try { return require('playwright'); }
    catch { throw new Error('Playwright is missing. Run npm ci in the repository root.'); }
}

async function waitForEditor(page, timeoutMs) {
    const end = Date.now() + timeoutMs;
    const editor = page.getByRole('textbox', { name: 'Ascend C 代码编辑器', exact: true });
    while (Date.now() < end) {
        if (page.isClosed()) throw new Error('Login window was closed. Nothing was submitted.');
        if (await editor.isVisible().catch(() => false)) return true;
        const origin = new URL(page.url()).origin;
        if (origin === new URL(SUBMIT_URL).origin && /\/home(?:[/?#]|$)/.test(page.url())) {
            await page.goto(SUBMIT_URL, { waitUntil: 'domcontentloaded' });
        }
        await sleep(1000);
    }
    return false;
}

export async function fillAndVerify(page, context, source, origin = new URL(SUBMIT_URL).origin) {
    const editor = page.getByRole('textbox', { name: 'Ascend C 代码编辑器', exact: true });
    // Monaco's rendered text overlays its textarea, so focus the input directly.
    await editor.focus();
    await editor.press('ControlOrMeta+A');
    // Use the site's own Copy Current File control to verify its submitted model.
    // The clipboard is read only after this explicit copy of the source we supplied.
    await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin });
    try {
        console.log('Pasting source into the editor...');
        await bounded(page.evaluate(text => navigator.clipboard.writeText(text), source), 10000, 'writing source clipboard');
        await editor.press('ControlOrMeta+V');
        console.log('Checking the editor source...');
        await bounded(page.evaluate(marker => navigator.clipboard.writeText(marker), `cannjudge-verification-${Date.now()}`), 10000, 'clearing verification clipboard');
        await page.getByRole('button', { name: '⧉', exact: true }).click();
        let copied = '';
        for (let i = 0; i < 20; i++) {
            copied = await bounded(page.evaluate(() => navigator.clipboard.readText()), 10000, 'reading copied source');
            if (normalizeSource(copied) === source) return;
            await sleep(100);
        }
        throw new Error('Editor content does not match the local source. Nothing was submitted.');
    } finally { await context.clearPermissions(); }
}

async function bounded(action, ms, label) {
    let timer;
    try {
        return await Promise.race([action, new Promise((_, reject) => {
            timer = setTimeout(() => reject(new Error(`Timed out ${label}. Nothing further was submitted.`)), ms);
        })]);
    } finally { clearTimeout(timer); }
}

export async function archivedSource(page, context) {
    await page.getByRole('button', { name: 'kernel.asc', exact: true }).click();
    await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: new URL(SUBMIT_URL).origin });
    try {
        const marker = `cannjudge-archive-${Date.now()}`;
        await bounded(page.evaluate(text => navigator.clipboard.writeText(text), marker), 10000, 'preparing archive verification');
        await page.getByRole('button', { name: '复制代码', exact: true }).click();
        for (let i = 0; i < 20; i++) {
            const copied = await bounded(page.evaluate(() => navigator.clipboard.readText()), 10000, 'reading archived source');
            if (copied !== marker && copied.includes('run_kernel')) return normalizeSource(copied);
            await sleep(100);
        }
        throw new Error('Cannot verify the source stored in the submission record.');
    } finally { await context.clearPermissions(); }
}

export async function clickSubmit(page, log = console.log, delay = sleep) {
    for (let attempt = 0; attempt < 2; attempt++) {
        await page.getByRole('button', { name: '提交代码', exact: true }).click();
        const accepted = await page.waitForURL(/\/cosine_similarity\/submission\//, { timeout: 15000 }).then(() => true).catch(() => false);
        if (accepted) return page.url();
        const alerts = await page.getByRole('alert').allTextContents();
        let visible = alerts.join('\n').trim();
        if (!visible) {
            // CANNJudge also presents rate limits in a custom modal without role=alert.
            const body = await page.locator('body').innerText();
            visible = (body.match(/[^\r\n]*(?:提交过于频繁|冷却|请在|请等待|cooldown|rate limit)[^\r\n]*/gi) || []).join('\n');
        }
        const seconds = cooldownSeconds(visible);
        if (attempt === 0 && seconds !== null) {
            const acknowledge = page.getByRole('button', { name: '确定', exact: true });
            if (await acknowledge.isVisible()) await acknowledge.click();
            log(`Platform cooldown: waiting ${seconds + 2}s, then trying once.`);
            await delay((seconds + 2) * 1000);
            continue;
        }
        throw new Error(`No confirmed submission record. To avoid duplicates, automatic retry stopped. ${visible}`);
    }
}

export async function readResult(page) {
    const text = await page.getByRole('main').innerText();
    const rows = await page.getByRole('table').getByRole('row').evaluateAll(elements =>
        elements.map(row => Array.from(row.querySelectorAll('td')).map(cell => cell.innerText.trim())));
    return parseResult(text, rows, page.url());
}

export async function waitForResult(page, log = console.log, timeoutMs = 12 * 60 * 1000) {
    const end = Date.now() + timeoutMs;
    let previous = '';
    while (Date.now() < end) {
        const result = await readResult(page);
        if (result.status !== previous) { log(`Judge: ${result.status || 'waiting for status'}`); previous = result.status; }
        if (result.terminal && (!/^pass$/i.test(result.status) ||
            (result.totalCases > 0 && result.cases.length === result.totalCases))) return result;
        await sleep(5000);
    }
    throw new Error(`Timed out waiting for the accepted submission. Do not submit again; inspect ${page.url()}`);
}

export async function main(options) {
    const original = options.loginOnly ? Buffer.from('') : await fs.readFile(options.source);
    const source = normalizeSource(new TextDecoder('utf-8', { fatal: true }).decode(original));
    if (!options.loginOnly && (!source.includes('run_kernel') || !source.trim())) throw new Error('The source must contain run_kernel.');
    const identity = { sourcePath: options.source, sourceBytes: Buffer.byteLength(source),
        rawFileSha256: sha256(original), sourceSha256: sha256(source), submitUrl: SUBMIT_URL };
    if (!options.loginOnly) console.log(`Source: ${options.source}\nSHA-256: ${identity.sourceSha256}\nTarget: ${SUBMIT_URL}`);
    if (options.dryRun) { console.log('Dry run complete. No browser or submission was started.'); return 0; }

    const { chromium } = loadPlaywright();
    const appData = process.env.LOCALAPPDATA || path.join(os.homedir(), '.local', 'share');
    const stateDir = process.env.CANNJUDGE_BROWSER_STATE_DIR || path.join(appData, 'CANNJudgeSubmit');
    await fs.mkdir(stateDir, { recursive: true });
    const release = await acquireLock(path.join(stateDir, 'submit.lock'));
    let context;
    let recordDir;
    let page;
    let phase = 'login';
    try {
        const launch = async headed => {
            const ctx = await chromium.launchPersistentContext(path.join(stateDir, 'browser-profile'), {
                channel: process.env.CANNJUDGE_BROWSER_CHANNEL || (process.platform === 'win32' ? 'msedge' : undefined),
                headless: !headed, chromiumSandbox: process.env.CANNJUDGE_BROWSER_SANDBOX !== '0',
                viewport: { width: 1440, height: 1000 },
            });
            const tab = ctx.pages()[0] || await ctx.newPage();
            tab.setDefaultTimeout(15000);
            try { await tab.goto(SUBMIT_URL, { waitUntil: 'domcontentloaded', timeout: 60000 }); }
            catch (error) { await ctx.close(); throw error; }
            return { ctx, tab };
        };
        let current = await launch(!!options.showBrowser || !!options.loginOnly);
        context = current.ctx; page = current.tab;
        if (!await waitForEditor(page, 5000)) {
            if (!options.showBrowser && !options.loginOnly) {
                await context.close(); context = undefined;
                current = await launch(true); context = current.ctx; page = current.tab;
            }
            console.log('Please log in to CANNJudge in the tool browser. It will continue automatically.');
            const login = page.getByRole('link', { name: '前往登录', exact: true });
            if (await login.isVisible().catch(() => false)) await login.click();
            if (!await waitForEditor(page, 10 * 60 * 1000)) throw new Error('Login timed out. Nothing was submitted.');
        }
        if (options.loginOnly) { console.log('Login saved in the dedicated tool profile. No submission was made.'); return 0; }

        const cooldownPath = path.join(stateDir, 'last-accepted.json');
        let previousAcceptance;
        try { previousAcceptance = JSON.parse(await fs.readFile(cooldownPath, 'utf8')); } catch {}
        const remaining = previousAcceptance ? Date.parse(previousAcceptance.acceptedAt) + 182000 - Date.now() : 0;
        if (remaining > 0) {
            console.log(`Platform submission interval: waiting ${Math.ceil(remaining / 1000)}s.`);
            await sleep(remaining);
        }

        const stamp = new Date().toISOString().replace(/[:.]/g, '-');
        recordDir = path.join(projectDir, 'benchmarks', 'results', 'submissions', `${stamp}-${identity.sourceSha256.slice(0, 12)}`);
        await fs.mkdir(recordDir, { recursive: true });
        await fs.writeFile(path.join(recordDir, 'kernel.asc'), source);
        await fs.writeFile(path.join(recordDir, 'request.json'), JSON.stringify({ ...identity, startedAt: new Date().toISOString() }, null, 2));
        phase = 'editor';
        await fillAndVerify(page, context, source);
        console.log('Editor verified. Submitting...');
        phase = 'submitting';
        const url = await clickSubmit(page);
        const accepted = { url, acceptedAt: new Date().toISOString() };
        await fs.writeFile(path.join(recordDir, 'accepted.json'), JSON.stringify(accepted, null, 2));
        await fs.writeFile(cooldownPath, JSON.stringify(accepted, null, 2));
        console.log(`Accepted: ${url}`);
        phase = 'judging';
        const result = await waitForResult(page);
        const storedSource = await archivedSource(page, context);
        await fs.writeFile(path.join(recordDir, 'submitted-kernel.asc'), storedSource);
        const archivedSourceMatches = storedSource === source;
        let baseline;
        try {
            const value = JSON.parse(await fs.readFile(path.join(projectDir, 'benchmarks', 'results', 'online-baseline.json'), 'utf8'));
            baseline = value.duration_us || value.durationUs || value.cases?.map(item => item.duration_us ?? item.durationUs);
        } catch { /* Comparison is optional; the platform result is authoritative. */ }
        const report = { ...identity, ...result, archivedSourceSha256: sha256(storedSource), archivedSourceMatches, completedAt: new Date().toISOString(),
            comparison: result.passed ? compareCases(result.cases, baseline) : null };
        await fs.writeFile(path.join(recordDir, 'result.json'), JSON.stringify(report, null, 2));
        await page.screenshot({ path: path.join(recordDir, 'result.png'), fullPage: true });
        if (!result.passed) {
            const detail = page.locator('[data-action="show-status-msg"]').first();
            if (await detail.isVisible().catch(() => false)) {
                await detail.click();
                await fs.writeFile(path.join(recordDir, 'diagnostics.txt'), await page.locator('body').innerText());
                await page.screenshot({ path: path.join(recordDir, 'diagnostics.png'), fullPage: true });
            }
        }
        console.log(`\n${result.status}: ${result.passedCases}/${result.totalCases}\n${result.url}\nSaved: ${recordDir}`);
        console.table(result.cases.map(row => ({ case: row.index, result: row.status, us: row.durationUs, error: `${row.errorPercent}%` })));
        if (report.comparison) console.log(`Geometric speedup vs recorded baseline: ${report.comparison.geometricSpeedup.toFixed(4)}x (not the platform score).`);
        if (!archivedSourceMatches) console.error('ERROR: The platform archived different source. Do not attribute this result to the requested source.');
        return result.passed && archivedSourceMatches ? 0 : 1;
    } catch (error) {
        if (recordDir) {
            await fs.writeFile(path.join(recordDir, 'error.json'), JSON.stringify({ phase, message: error.message,
                url: page?.url(), at: new Date().toISOString() }, null, 2));
            if (page && !page.isClosed()) await page.screenshot({ path: path.join(recordDir, 'error.png') }).catch(() => {});
        }
        throw error;
    } finally {
        if (context) await context.close().catch(() => {});
        await release();
    }
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
    try { process.exitCode = await main(optionsFromArgs(process.argv.slice(2))); }
    catch (error) { console.error(`ERROR: ${error.message}`); process.exitCode = 1; }
}
