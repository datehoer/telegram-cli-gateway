## 阅读页设计（Relay Pages）

Bot 长回复发布成的网页必须使用 `relay-pages/` 下的设计系统：

- 先读 `relay-pages/DESIGN.md`（视觉与内容规则），用到哪个组件再读 `relay-pages/components/<Name>.md`。
- 页面只引入 `relay-pages/tokens.css` 和 `relay-pages/relay-pages.css`，使用 `rp-` 前缀的类名；不要写死颜色、字号、间距，一律用 tokens.css 里的变量。
- 页面骨架以 `relay-pages/example.html` 为准：`.rp-page > article.rp-card > 页头 → 目录 → .rp-prose → 页脚`。
- Markdown 渲染后必须做 HTML 消毒；`> [!NOTE|TIP|WARNING|CAUTION]` 映射到 Callout，代码块映射到 CodeBlock，表格包 `.rp-table-wrap`。
- 改动设计请改 `tokens.json` 并重新生成 `tokens.css`，不要直接在组件里覆盖。
