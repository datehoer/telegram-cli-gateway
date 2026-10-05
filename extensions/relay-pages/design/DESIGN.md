Relay Pages 是 Bot 长回复的阅读页：Bot 把 Markdown 发布成网页，在聊天里只回一句摘要和链接，用户点开后在这里舒服地读完。它的气质要和聊天客户端连续——天蓝、圆润、系统字体、干净的白底——但比聊天气泡更安静，更适合长文。

## 内容原则

- **页面是 Bot 回复的延伸，不是一个新产品。** 页头写清楚是哪个 Bot、什么时候发的；页脚永远有「返回对话」。
- **先结论，后细节。** 需要用户处理的事放在正文最前面，用 Callout（`warn` / `danger`）标出来；完整日志放到最后。
- **语气跟随 Bot 原文**，阅读页本身的界面文案简短、中文、不带感叹号和 emoji：「复制」「已复制」「返回对话」「7 天后过期」「查看原始 Markdown」。
- **聊天里的那条回复**：一句话摘要（≤60 字）+ 链接，必要时加一行关键数字。例：`部署完成，14 个任务中 2 个需要你处理 → https://pages.example.dev/p/k3x9a`。

## 视觉基础

**颜色。** 大部分面积是 `surface`（白 / 深蓝灰）上的 `ink`。`accent` 是唯一的交互色：链接、主按钮、目录当前项、焦点环。`sky` 是品牌天蓝，只做大面积装饰（头像底、封面、链接预览图），不要用作文字或细线。状态色 `ok` / `warn` / `danger` 只出现在 Callout 和状态文字上，各自配 `*-soft` 底，并且总是带文字标签。

**主题。** 浅色和深色两套，跟随系统；URL 参数 `?theme=dark|light` 优先。深色不是纯黑，`surface` 是深蓝灰，`surface-sunken` 更深一层。所有文字在两个主题的对应底色上都 ≥4.5:1；焦点环为 2px 实线 `accent`，偏移 2px，在所有面上 ≥3:1。

**字体。** 只用系统字体：`sans`（苹方 / 微软雅黑 / Noto Sans SC 等）和 `mono`。正文用 `body`（16/28，中文长文需要 1.75 行高）；标题 `page-title` / `h2` / `h3`；表格和提示框用 `small`；时间、用户名、语言标签用 `meta` 配 `ink-muted`；代码 `code`。手机端 `page-title` 降到 24/32，其余不变。

**布局。** 文章卡片 `rp-card` 最大宽 `measure`（720px，约 40 个汉字一行），桌面端居中，`radius-lg` 圆角 + `shadow-card`，内边距 `space-8`；640px 以下卡片铺满、去圆角阴影，左右 `space-4`。段落间距 `space-4`，块级元素间距 `space-6`，`##` 前 `space-8`。

**圆角。** `radius-sm` 给小东西（行内代码、目录项），`radius-md` 给正文里的块（代码块、表格、提示框、目录），`radius-lg` 只给文章卡片，`radius-full` 给头像和所有按钮。

**边框与阴影。** 分隔靠 `border` 发丝线和 `surface-raised` 底色，不靠阴影；整页只有文章卡片一处 `shadow-card`。可点击控件的描边用 `border-strong`。

**按钮。** 统一用 Button：胶囊形，默认 36px、小号 28px（点击区域 44px），手机页脚 44px；并排的按钮尺寸必须一致，只用颜色区分主次。

**动效。** 只有悬停底色变化和目录高亮切换，150ms 以内；不要入场动画——用户是来读字的。

## 图标

线性图标，16px，1.6px 描边，`currentColor`，只出现在 Callout 标签和少数按钮里。没有 Logo：页面上只写 Bot 自己的名字和头像，阅读页本身不露出品牌标识。不要使用 emoji 当图标。

## 用法速查

- 页面骨架：`.rp-page` > `article.rp-card` > PageHeader → Toc（标题 >3 个时）→ `.rp-prose` → PageFooter。
- Markdown 映射：`#` → `rp-title`；`##`/`###` → Prose 内标题；``` → CodeBlock；表格 → Table；`> [!NOTE|TIP|WARNING|CAUTION]` → Callout；`==text==` → `<mark>`。
- 链接预览：设置 `og:title`、`og:description`（正文前 120 字）、`og:site_name`（Bot 名）和 `theme-color`，聊天里的预览卡片才有内容。
- 样式全部来自 `components/bundle.css`（类名前缀 `rp-`）和 tokens.css 变量，不要在页面里写死颜色值。
