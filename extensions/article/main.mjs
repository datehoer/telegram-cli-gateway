#!/usr/bin/env node
import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { ArticleError, articleMarkdown, outputName, parseInput, singleLine } from './article.mjs';
import { capture } from './capture.mjs';

export const HELP = '用法：/article <URL> [--archive]\n提取可读正文为 Markdown；WSJ 原页失败时自动尝试已有存档。\n遇到验证码、付费预览或存档无法访问时会明确报错。';

export async function run(argv = process.argv.slice(2), environment = process.env, dependencies = {}) {
  const input = parseInput(argv, environment);
  if (!input) { console.log(HELP); return 0; }
  let Turndown;
  try { ({ default: Turndown } = await import('turndown')); } catch {
    throw new ArticleError('missing_dependencies', '文章扩展依赖未安装，请在扩展目录运行 npm ci。');
  }
  const result = await (dependencies.capture || capture)(input, text => console.log(`@@PROGRESS ${singleLine(text)}`));
  const article = result.snapshot.article;
  const converter = new Turndown({ headingStyle: 'atx', codeBlockStyle: 'fenced' });
  converter.remove(['script', 'style', 'iframe', 'form', 'button']);
  converter.addRule('unsafeLinks', {
    filter: node => node.nodeName === 'A' && /^(?:javascript|data|file):/i.test(node.getAttribute('href') || ''),
    replacement: content => content,
  });
  const markdown = converter.turndown(article.content);
  const metadata = {
    status: 'extracted', sourceUrl: input.url,
    retrievedUrl: result.snapshot.archiveLink || result.retrievedUrl,
    retrievedAt: new Date().toISOString(), method: result.method,
    title: singleLine(article.title), byline: singleLine(article.byline),
    paragraphCount: result.snapshot.paragraphCount,
    characterCount: article.textContent.trim().length,
    wordCount: article.textContent.trim().split(/\s+/).length,
    bpcVersion: '4.4.6.0', attempts: result.attempts,
  };
  const directory = path.resolve(environment.TG_WORKDIR || '/srv/projects/artifacts');
  await fs.mkdir(directory, { recursive: true });
  const stem = outputName(input.url);
  const output = path.join(directory, `${stem}.md`);
  const info = path.join(directory, `${stem}.json`);
  const text = articleMarkdown(article, markdown, metadata);
  // Publish only fully written files after the capture quality checks pass.
  await fs.writeFile(`${output}.tmp`, text, { encoding: 'utf8', mode: 0o600 });
  await fs.rename(`${output}.tmp`, output);
  await fs.writeFile(info, `${JSON.stringify(metadata, null, 2)}\n`, { encoding: 'utf8', mode: 0o600 });
  console.log(`已提取：${metadata.title}\n正文：${metadata.wordCount} 词 · ${metadata.paragraphCount} 段\n来源：${result.method === 'archive' ? '存档副本' : '网页正文'}\n文件：${output}`);
  return 0;
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  run().then(code => { process.exitCode = code; }).catch(error => {
    console.error(error instanceof ArticleError ? error.message : '文章提取失败，未交付正文文件。');
    process.exitCode = 1;
  });
}
