# Button

按钮：胶囊形，主次两种只差颜色，大小两档，阅读页上的所有可点击操作都用它。

- 结构：`<button class="rp-btn">` 或 `<a class="rp-btn">`（跳转用 `<a>`，如「返回对话」）。主按钮加 `.rp-btn-primary`；代码块工具栏里用小号 `.rp-btn-sm`。
- 尺寸：默认 36px 高、14px 字；小号 28px 高、13px 字，用伪元素把点击区域扩到 44px；在窄容器（≤520px）的页脚里，按钮拉满并升到 44px 高，方便单手点。
- 颜色：主按钮 `accent` 底 + `on-accent` 字，悬停 `accent-hover`；次按钮透明底 + `border-strong` 描边 + `ink` 字，悬停 `accent-soft` 底 + `accent` 描边。焦点环 2px `accent`，偏移 2px。
- 状态：复制成功加 `.is-done`（`ok` 色描边和文字、勾选图标、文案「已复制」），1.5 秒后移除；不可用用 `disabled`（`<a>` 用 `aria-disabled="true"`）。
- 图标可选，放在文字前，16px（小号 14px），`currentColor`。只有图标没有文字时必须加 `aria-label`。
- 并排的按钮必须同一尺寸；每页只有一个主按钮（「返回对话」）。
