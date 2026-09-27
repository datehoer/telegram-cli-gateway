#!/usr/bin/env python3
"""示例直连命令：演示插件契约，不需要安装任何依赖。

契约要点：
- 参数从 argv 拿；网关不经过 shell，不会做二次解释。
- 环境变量 TG_* 提供上下文（这里只用 TG_WORKDIR 决定产物落在哪）。
- stdout 里以 `@@PROGRESS ` 开头的行是进度，网关会原地刷新同一条消息；
  其余 stdout 缓冲到最后一次性发出。
- stdout 里出现的、位于 ALLOWED_WORKDIRS 内的绝对路径会变成「发送文件」按钮。
- 退出码 0 表示成功，非 0 时 stderr 会展示给用户。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in {"-h", "--help"}:
        print("用法：/example <任意文本>")
        print("会在 TG_WORKDIR 下生成 example-output.txt，并演示进度行。")
        return 0

    text = " ".join(args)
    workdir = Path(os.environ.get("TG_WORKDIR", ".")).expanduser()
    workdir.mkdir(parents=True, exist_ok=True)
    target = (workdir / "example-output.txt").resolve()

    for step in range(1, 4):
        print(f"@@PROGRESS 第 {step}/3 步：处理「{text}」", flush=True)
        time.sleep(0.5)

    target.write_text(f"输入：{text}\n参数个数：{len(args)}\n", encoding="utf-8")

    print(f"收到 {len(args)} 个参数：{args}")
    print(f"已写入产物：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
