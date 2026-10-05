import { createHash } from 'node:crypto';

export class ArticleError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'ArticleError';
    this.code = code;
  }
}

export function singleLine(value, limit = 250) {
  return String(value || '').replace(/[\x00-\x1f\x7f]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, limit);
}

export function parseInput(argv = [], environment = process.env) {
  // The gateway puts the first argument in TG_ARGV0, not argv. Prefer its
  // untouched input; never shell-parse a URL or interpolate it into a command.
  const raw = environment.TG_RAW_ARGS || [environment.TG_ARGV0, ...argv].filter(Boolean).join(' ');
  const parts = raw.trim().split(/\s+/).filter(Boolean);
  if (!parts.length || (parts.length === 1 && ['help', '--help', '-h'].includes(parts[0]))) return null;
  const archive = parts.includes('--archive');
  const urls = parts.filter(part => part !== '--archive');
  if (urls.length !== 1 || parts.filter(part => part === '--archive').length > 1) {
    throw new ArticleError('invalid_input', '用法：/article <一个 HTTP/HTTPS 网址> [--archive]');
  }
  let url;
  try { url = new URL(urls[0]); } catch {
    throw new ArticleError('invalid_input', '网址无效，请提供完整的 HTTP/HTTPS 网址。');
  }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.href.length > 4096) {
    throw new ArticleError('invalid_input', '只接受不含账号密码的 HTTP/HTTPS 网址。');
  }
  url.hash = '';
  return { url: url.href, archive };
}

export function isWsj(url) {
  const host = new URL(url).hostname.toLowerCase();
  return host === 'wsj.com' || host.endsWith('.wsj.com');
}

export function isArchive(url) {
  return /^archive\.(?:is|ph|fo|li|md|vn|today)$/.test(new URL(url).hostname.toLowerCase());
}

export function archiveLookups(url) {
  const original = new URL(url);
  original.search = '';
  original.hash = '';
  return ['archive.ph', 'archive.is'].map(host => `https://${host}/${original.href}`);
}

export function qualityFailure(snapshot, sourceUrl) {
  if (snapshot.challenge) return '页面要求人机验证，尚未获取正文。';
  if (snapshot.status >= 400) return `页面返回 HTTP ${snapshot.status}，尚未获取正文。`;
  if (snapshot.archiveListing) return '存档查询页没有提供可读取的文章副本。';
  if (snapshot.paywall || snapshot.bpcFailure) return '付费墙尚未处理成功，当前内容不能作为正文交付。';
  // BPC removes WSJ's overlay before an archive fetch finishes. Its absence
  // alone is not evidence that the truncated original was replaced.
  if (isWsj(sourceUrl) && snapshot.sawPaywall && !snapshot.archiveRestored) {
    return '仅取得 WSJ 的付费预览，尚未确认存档正文已加载。';
  }
  if (!snapshot.article?.content || snapshot.article.textContent.trim().length < 500 || snapshot.paragraphCount < 3) {
    return '没有找到足够的可读正文；可能是摘要、导航页或尚未加载完成。';
  }
  if (snapshot.article.content.length > 500_000) return '正文超过本扩展的大小限制。';
  return '';
}

export function outputName(url, now = new Date()) {
  const stamp = now.toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
  const key = createHash('sha256').update(url).digest('hex').slice(0, 12);
  return `article-${stamp}-${key}`;
}

export function articleMarkdown(article, markdown, metadata) {
  const title = singleLine(article.title || 'Untitled article').replace(/([\\`*_\[\]<>])/g, '\\$1');
  const lines = [`# ${title}`, '', `来源：<${metadata.sourceUrl}>`];
  if (metadata.retrievedUrl !== metadata.sourceUrl) lines.push(`读取页面：<${metadata.retrievedUrl}>`);
  if (article.byline) lines.push(`作者：${singleLine(article.byline)}`);
  if (article.publishedTime) lines.push(`发布时间：${singleLine(article.publishedTime)}`);
  lines.push(`提取时间：${metadata.retrievedAt}`, `获取方式：${metadata.method === 'archive' ? '存档副本' : '网页正文'}`);
  lines.push('', '---', '', markdown.trim(), '');
  return lines.join('\n');
}
