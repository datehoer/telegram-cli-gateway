# Prose

Markdown 渲染结果的排版容器，所有正文元素都放在 `.rp-prose` 里。

- 调用方提供：渲染后的 HTML（GFM：表格、任务列表、删除线、脚注；`==高亮==` 转 `<mark>`）。渲染前必须做 HTML 消毒，Bot 输出不可信。
- 段落间距 `space-4`，`##` 上方 `space-8`；正文 `body`（16/28），不要再压小。
- 链接用 `accent` + 下划线，在新标签页打开外链并加 `rel="noopener"`。
- 代码块、表格、提示框不要直接用 `<pre>` / `<table>` / 引用，分别包成 CodeBlock、Table、Callout。
- 普通 `>` 引用保持低调：`border-strong` 竖线 + `ink-muted` 字；需要强调用 Callout。
