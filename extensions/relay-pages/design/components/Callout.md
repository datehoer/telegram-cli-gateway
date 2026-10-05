# Callout

提示框，对应 GitHub 风格的 `> [!NOTE]`、`> [!TIP]`、`> [!WARNING]`、`> [!CAUTION]`。

- 映射：NOTE → 默认（`accent-soft`，标签「提示」）；TIP → `.ok`（「完成/建议」）；WARNING → `.warn`（「注意」）；CAUTION → `.danger`（「警告」）。
- 结构：`.rp-callout[.warn|.danger|.ok]` > `.rp-callout-label`（16px 线性图标 + 文字）+ 正文。
- 永远带文字标签，颜色只是辅助。底色铺满，不加左侧色条。
- Bot 的「需要人工处理」类信息放在 warn/danger 提示框里，并尽量放在正文开头。
