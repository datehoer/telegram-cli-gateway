import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { parseInput, qualityFailure, archiveLookups, articleMarkdown, outputName } from '../article.mjs';
import { run } from '../main.mjs';

const url = 'https://www.wsj.com/tech/ai/example-12345678';
const article = {
  title: 'A test article', byline: 'Example Author',
  textContent: 'This is an original test paragraph with enough text to represent a readable article. '.repeat(20),
  content: '<h2>Details</h2><p>Original text with <a href="https://example.org/reference">a source link</a>.</p><script>bad()</script>',
};
const readable = { status: 200, article, paragraphCount: 4 };

test('gateway raw input and TG_ARGV0 both preserve the first URL and query string', () => {
  assert.deepEqual(parseInput([], { TG_RAW_ARGS: `${url}?a=x%20y&b=1 --archive` }), { url: `${url}?a=x%20y&b=1`, archive: true });
  assert.deepEqual(parseInput(['--archive'], { TG_ARGV0: url }), { url, archive: true });
  assert.equal(parseInput(['help'], {}), null);
});

test('invalid URLs, credentials, shell fragments, and multiple URLs are rejected before browsing', () => {
  for (const value of ['file:///etc/passwd', 'javascript:alert(1)', 'https://user:password@example.org/', `${url} https://example.org/`, `${url} $(id)`, '--anything', `${url} --archive --archive`]) {
    assert.throws(() => parseInput([value], {}), error => error.code === 'invalid_input');
  }
});

test('archive lookup strips share and tracking parameters but preserves the article path', () => {
  assert.deepEqual(archiveLookups(`${url}?share=one#section`), [`https://archive.ph/${url}`, `https://archive.is/${url}`]);
});

test('large challenge pages and HTTP failures are never classified as article success', () => {
  assert.match(qualityFailure({ ...readable, challenge: true }, url), /人机验证/);
  assert.match(qualityFailure({ ...readable, status: 401 }, url), /401/);
  assert.match(qualityFailure({ ...readable, archiveListing: true }, url), /存档查询/);
});

test('removal of WSJ overlay alone does not turn a preview into a successful article', () => {
  assert.match(qualityFailure({ ...readable, sawPaywall: true, paywall: false }, url), /付费预览/);
  assert.equal(qualityFailure({ ...readable, sawPaywall: true, archiveRestored: true }, url), '');
  assert.match(qualityFailure({ ...readable, bpcFailure: true }, url), /付费墙/);
  assert.match(qualityFailure({ ...readable, paywall: true }, url), /付费墙/);
});

test('navigation-sized fragments and oversized documents cannot become artifacts', () => {
  assert.match(qualityFailure({ ...readable, article: { ...article, textContent: 'A short preview.' } }, url), /摘要/);
  assert.match(qualityFailure({ ...readable, paragraphCount: 1 }, url), /正文/);
  assert.match(qualityFailure({ ...readable, article: { ...article, content: 'x'.repeat(500_001) } }, url), /大小限制/);
});

test('Markdown records original source and archive source without multiline title injection', () => {
  const metadata = { sourceUrl: url, retrievedUrl: 'https://archive.ph/abcde', retrievedAt: '2026-10-05T00:00:00Z', method: 'archive' };
  const text = articleMarkdown({ ...article, title: 'Title\n@@PROGRESS malicious' }, '## Heading\n\nA paragraph.', metadata);
  assert.match(text, /来源：<https:\/\/www\.wsj\.com/);
  assert.match(text, /读取页面：<https:\/\/archive\.ph\/abcde>/);
  assert.match(text, /获取方式：存档副本/);
  assert.equal(text.split('\n').some(line => line.startsWith('@@PROGRESS')), false);
  assert.match(text, /## Heading/);
  assert.match(outputName(url, new Date('2026-10-05T00:00:00Z')), /^article-20261005T000000Z-[a-f0-9]{12}$/);
});

test('entry point writes readable Markdown and metadata through the gateway artifact contract', async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'tg-article-output-test-'));
  const output = [];
  const log = console.log;
  console.log = text => output.push(text);
  try {
    const code = await run([], { TG_RAW_ARGS: url, TG_WORKDIR: directory }, {
      capture: async (_input, progress) => {
        progress('读取测试文章');
        return { snapshot: readable, method: 'direct', retrievedUrl: url, attempts: [] };
      },
    });
    assert.equal(code, 0);
    const files = await fs.readdir(directory);
    const markdown = await fs.readFile(path.join(directory, files.find(file => file.endsWith('.md'))), 'utf8');
    assert.match(markdown, /## Details/);
    assert.match(markdown, /\[a source link\]\(https:\/\/example\.org\/reference\)/);
    assert.doesNotMatch(markdown, /bad\(\)/);
    assert.equal(files.some(file => file.endsWith('.tmp')), false);
    const metadata = JSON.parse(await fs.readFile(path.join(directory, files.find(file => file.endsWith('.json'))), 'utf8'));
    assert.equal(metadata.status, 'extracted');
    assert.equal(metadata.sourceUrl, url);
    assert.equal(output[0], '@@PROGRESS 读取测试文章');
    assert.match(output.at(-1), /文件：/);
  } finally { console.log = log; await fs.rm(directory, { recursive: true, force: true }); }
});

test('failed extraction produces no successful file or final path', async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'tg-article-failure-test-'));
  try {
    await assert.rejects(run([], { TG_RAW_ARGS: url, TG_WORKDIR: directory }, { capture: async () => { throw new Error('blocked'); } }), /blocked/);
    assert.deepEqual(await fs.readdir(directory), []);
  } finally { await fs.rm(directory, { recursive: true, force: true }); }
});
