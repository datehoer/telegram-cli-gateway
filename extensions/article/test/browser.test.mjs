import test from 'node:test';
import assert from 'node:assert/strict';
import { capture } from '../capture.mjs';

const sourceUrl = 'https://www.wsj.com/tech/ai/test-article-12345678';
const paragraph = 'This original fixture explains a practical engineering decision, including its observations, costs, and measurable results. It supplies enough continuous prose for a reader-mode extractor to identify the article.';
const html = `<html><head><title>Original Test Article</title><meta name="author" content="Fixture Author"></head><body><nav>Navigation links</nav><article><h1>Original Test Article</h1>${Array.from({ length: 8 }, (_, i) => `<p>Paragraph ${i + 1}. ${paragraph}</p>`).join('')}<h2>Final section</h2><p>${paragraph}</p></article></body></html>`;

test('real Chromium plus BPC can extract a normal page without retaining its navigation', { timeout: 30_000 }, async () => {
  const input = { url: 'https://article-fixture.invalid/story', archive: false };
  const result = await capture(input, () => {}, {
    prepareContext: context => context.route('**/*', route => route.request().url() === input.url
      ? route.fulfill({ status: 200, contentType: 'text/html', body: html }) : route.abort()),
  });
  assert.equal(result.method, 'direct');
  assert.equal(result.snapshot.article.title, 'Original Test Article');
  assert.ok(result.snapshot.paragraphCount >= 8);
  assert.doesNotMatch(result.snapshot.article.textContent, /Navigation links/);
});

test('WSJ challenge falls back through archive history to a snapshot, preserving source provenance', { timeout: 30_000 }, async () => {
  const result = await capture({ url: sourceUrl, archive: false }, () => {}, {
    prepareContext: context => context.route('**/*', route => {
      const url = route.request().url();
      if (url === sourceUrl) return route.fulfill({ status: 401, contentType: 'text/html', body: '<html><head><title>wsj.com</title></head><body><iframe src="https://geo.captcha-delivery.com/test"></iframe></body></html>' });
      if (url === 'https://archive.ph/abcde') return route.fulfill({ status: 200, contentType: 'text/html', body: html });
      if (url.startsWith('https://archive.ph/https://www.wsj.com/')) return route.fulfill({ status: 200, contentType: 'text/html', body: '<html><body><div class="TEXT-BLOCK"><a href="https://archive.ph/abcde">Saved article</a></div></body></html>' });
      return route.abort();
    }),
  });
  assert.equal(result.method, 'archive');
  assert.equal(result.retrievedUrl, 'https://archive.ph/abcde');
  assert.equal(result.attempts[0].status, 401);
  assert.equal(result.attempts[0].challenge, true);
  assert.equal(result.snapshot.article.title, 'Original Test Article');
});

test('challenge on original and both archive hosts fails instead of exporting challenge text', { timeout: 30_000 }, async () => {
  await assert.rejects(capture({ url: sourceUrl, archive: false }, () => {}, {
    prepareContext: context => context.route('**/*', route => route.fulfill({ status: 403, contentType: 'text/html', body: '<html><head><title>Access Denied</title></head><body>Verify you are human.</body></html>' })),
  }), error => error.code === 'unavailable' && error.attempts.length === 3 && error.attempts.every(attempt => attempt.challenge));
});
