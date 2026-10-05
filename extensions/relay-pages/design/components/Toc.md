# Toc

目录，标题多于 3 个时自动生成，放在页头下方、正文之前。

- 结构：`details.rp-toc` > `summary` + `ol`；三级标题的 `li` 加 `.lv3`。只收录 `##` 和 `###`。
- 当前阅读位置的链接加 `.is-active`（`accent-soft` 底 + `accent` 字），用 IntersectionObserver 更新。
- 手机端默认收起（去掉 `open`），桌面端默认展开。
