# ReaderPage

完整阅读页示例：页头、目录、正文、提示框、表格、代码块、页脚的组合方式。

- 外层 `.rp-page`（`surface-sunken` 底）> `article.rp-card`（最大宽 `measure`，`radius-lg`，`shadow-card`）。
- 640px 以下卡片铺满屏幕、去圆角和阴影，左右留 `space-4`——从 Telegram 内置浏览器打开时就是这个状态。
- 块级元素之间统一 `space-6`。
- `<head>` 里设置 `og:title`（文章标题）、`og:description`（正文前 120 字）、`og:site_name`（Bot 名），让聊天里的链接预览有内容；设置 `<meta name="theme-color">` 为 `surface`。
- 根据 `prefers-color-scheme` 切换 `data-theme="dark"`；URL 带 `?theme=dark|light` 时以参数为准（Bot 可以把用户客户端的主题透传过来）。
