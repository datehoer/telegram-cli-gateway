# Table

可横向滚动的表格，宽表在手机上不撑破页面。

- 结构：`.rp-table-wrap` > `table.rp-table`。数字列在 `th`/`td` 上加 `.num`，右对齐并使用等宽数字。
- 调用方提供：GFM 表格渲染出的 `<table>`；对齐方式由 Markdown 的 `:--:` 决定。
- 表头 `surface-raised` 底；行线用 `border`，不要做斑马纹。
- 状态列（完成/失败）写文字，不要只用颜色块。
