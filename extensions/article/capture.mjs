import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { ArticleError, archiveLookups, isArchive, isWsj, qualityFailure } from './article.mjs';

const require = createRequire(import.meta.url);
const EXTENSION = path.join(import.meta.dirname, 'vendor', 'bpc');
const NAVIGATION_MS = 20_000;
const RUN_MS = 110_000;

// Runs in the page before site scripts or BPC can remove the original overlay.
export function observePaywall() {
  window.__tgArticleSawPaywall = false;
  const observer = new MutationObserver(() => {
    if (document.querySelector('.snippet-promotion,div[id*="-snippet-overlay"]')) {
      window.__tgArticleSawPaywall = true;
      observer.disconnect();
    }
  });
  observer.observe(document, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'id'] });
}

export function inspectDocument({ readabilitySource, status }) {
  const body = document.body?.innerText || '';
  const title = document.title;
  const frameChallenge = [...document.querySelectorAll('iframe[src]')].some(frame => {
    try { return /captcha-delivery\.com|challenges\.cloudflare\.com/.test(new URL(frame.src).hostname); } catch { return false; }
  });
  const challenge = frameChallenge || /^(?:just a moment|access denied|attention required)/i.test(title)
    || /verify (?:that )?you are human|enable javascript and (?:cookies|disable)|checking (?:your )?browser/i.test(body.slice(0, 1200));
  const visible = node => node.getClientRects().length && getComputedStyle(node).display !== 'none';
  const paywall = [...document.querySelectorAll('.snippet-promotion,div[id*="-snippet-overlay"],[data-testid="paywall"],.paywall-overlay')].some(visible);
  const archiveNodes = [...document.querySelectorAll('#bpc_archive')];
  const archiveRestored = archiveNodes.some(node => /Full article text fetched from/i.test(node.textContent));
  const archiveLink = archiveRestored ? archiveNodes.flatMap(node => [...node.querySelectorAll('a[href]')])
    .find(node => /^https:\/\/archive\.(?:is|ph|fo|li|md|vn)\/[^?#]{5}$/.test(node.href))?.href || '' : '';
  const bpcFailure = !!document.querySelector('#bpc_fail,#bpc_nofix')
    || archiveNodes.some(node => /Try for full article|no article content found|fetch blocked|failed to load/i.test(node.textContent));
  const listing = document.querySelector('.TEXT-BLOCK a[href]');
  let snapshotUrl = '';
  if (listing) {
    try {
      const url = new URL(listing.href);
      if (/^archive\.(?:is|ph|fo|li|md|vn|today)$/.test(url.hostname)
          && (/^\/[A-Za-z0-9]{5}$/.test(url.pathname) || /^\/\d{4}\./.test(url.pathname))) snapshotUrl = url.href;
    } catch { /* A malformed history link is not an article. */ }
  }
  const archiveListing = !!listing && !document.querySelector('article') && !document.querySelector('#CONTENT');
  const clone = document.cloneNode(true);
  clone.querySelectorAll('[id^="bpc_"],script:not([type="application/ld+json"]),style,iframe,form,button,nav,aside').forEach(node => node.remove());
  if (/^archive\./.test(location.hostname)) clone.querySelectorAll('#HEADER,#FOOTER,#DIV_TIMESTAMP').forEach(node => node.remove());
  // Compile the locally installed library, not any code supplied by the page.
  const Reader = new Function(`${readabilitySource}\n;return Readability;`)();
  const article = new Reader(clone, { charThreshold: 500, maxElemsToParse: 60_000 }).parse();
  const template = document.createElement('template');
  template.innerHTML = article?.content || '';
  const paragraphCount = [...template.content.querySelectorAll('p,blockquote,li')].filter(node => node.textContent.trim().length >= 20).length;
  return { status, title, challenge, paywall, bpcFailure, archiveRestored, archiveLink,
    sawPaywall: !!window.__tgArticleSawPaywall, archiveListing, snapshotUrl, article, paragraphCount };
}

async function inspect(page, readabilitySource, status) {
  return page.evaluate(inspectDocument, { readabilitySource, status });
}

async function readPage(context, url, readabilitySource, deadline) {
  const page = await context.newPage();
  try {
    const response = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: Math.min(NAVIGATION_MS, Math.max(1, deadline - Date.now())) });
    const status = response?.status() || 0;
    let snapshot = await inspect(page, readabilitySource, status);
    if (snapshot.challenge || status >= 400) return { snapshot, retrievedUrl: page.url() };
    let previous = 0;
    let stableSince = Date.now();
    const settleUntil = Math.min(deadline, Date.now() + 12_000);
    const earliest = Date.now() + 3000;
    while (Date.now() < settleUntil) {
      await page.waitForTimeout(500);
      snapshot = await inspect(page, readabilitySource, status);
      const length = snapshot.article?.textContent?.length || 0;
      if (length !== previous) { previous = length; stableSince = Date.now(); }
      if (snapshot.challenge || snapshot.bpcFailure || snapshot.archiveListing) break;
      if (!snapshot.paywall && length >= 500 && Date.now() >= earliest && Date.now() - stableSince >= 1500) break;
    }
    return { snapshot, retrievedUrl: page.url() };
  } finally { await page.close(); }
}

export async function capture(input, progress = () => {}, options = {}) {
  let chromium;
  try { ({ chromium } = await import('playwright-core')); } catch {
    throw new ArticleError('missing_dependencies', '文章扩展依赖未安装，请在扩展目录运行 npm ci。');
  }
  const readabilitySource = await fs.readFile(require.resolve('@mozilla/readability/Readability.js'), 'utf8');
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), 'tg-article-'));
  let context;
  const deadline = Date.now() + (options.runMs || RUN_MS);
  const attempts = [];
  const cleanup = async () => {
    await context?.close().catch(() => {});
    await fs.rm(profile, { recursive: true, force: true });
  };
  const interrupt = () => { cleanup().finally(() => process.exit(143)); };
  process.once('SIGTERM', interrupt);
  process.once('SIGINT', interrupt);
  try {
    progress('启动文章读取浏览器…');
    context = await chromium.launchPersistentContext(profile, {
      headless: true, channel: 'chromium', bypassCSP: true,
      ...(options.executablePath || process.env.ARTICLE_CHROMIUM_EXECUTABLE ? { executablePath: options.executablePath || process.env.ARTICLE_CHROMIUM_EXECUTABLE } : {}),
      args: [`--disable-extensions-except=${EXTENSION}`, `--load-extension=${EXTENSION}`],
      timeout: 20_000,
    });
    const worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker', { timeout: 10_000 });
    await worker.evaluate(async () => {
      await chrome.storage.local.set({ optInUpdate: false, optInShown: true, customShown: true });
    });
    await context.addInitScript(observePaywall);
    if (options.prepareContext) await options.prepareContext(context);

    const tryRead = async url => {
      try {
        const result = await readPage(context, url, readabilitySource, deadline);
        attempts.push({ url, status: result.snapshot.status, challenge: result.snapshot.challenge,
          failure: qualityFailure(result.snapshot, input.url) });
        return result;
      } catch (error) {
        attempts.push({ url, failure: error.name === 'TimeoutError' ? '页面读取超时。' : '页面无法访问。' });
        return null;
      }
    };

    progress('读取原始文章并等待正文处理…');
    let result = await tryRead(input.url);
    if (result && !qualityFailure(result.snapshot, input.url)) {
      return { ...result, method: isArchive(input.url) || result.snapshot.archiveRestored ? 'archive' : 'direct', attempts };
    }
    if (!isArchive(input.url) && (isWsj(input.url) || input.archive)) {
      progress('原页未提供可读正文，查找已有存档…');
      for (const url of archiveLookups(input.url)) {
        if (Date.now() >= deadline) break;
        result = await tryRead(url);
        if (result?.snapshot.snapshotUrl && result.snapshot.snapshotUrl !== result.retrievedUrl) {
          result = await tryRead(result.snapshot.snapshotUrl);
        }
        if (result && !qualityFailure(result.snapshot, result.retrievedUrl)) return { ...result, method: 'archive', attempts };
      }
    }
    const last = attempts.at(-1)?.failure || '没有获取到可读正文。';
    const error = new ArticleError('unavailable', `未获取正文：${last}${attempts[0]?.challenge ? ' 原页要求人机验证。' : ''}`);
    error.attempts = attempts;
    throw error;
  } catch (error) {
    if (error instanceof ArticleError) throw error;
    if (/Executable doesn't exist/.test(error.message)) {
      throw new ArticleError('missing_browser', 'Chromium 未安装，请在扩展目录运行 npm run install-browser。');
    }
    throw new ArticleError('browser_failure', '文章读取浏览器未能完成处理，请检查浏览器安装和网络。');
  } finally {
    process.removeListener('SIGTERM', interrupt);
    process.removeListener('SIGINT', interrupt);
    await cleanup();
  }
}
