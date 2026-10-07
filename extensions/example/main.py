#!/usr/bin/env python3
"""Example direct command: demonstrates the extension contract with no dependencies.

Contract highlights:
- Arguments come from argv; the gateway uses no shell and adds no second interpretation.
- TG_* environment variables provide context. This example writes its output to
  TG_WORKDIR and reports TG_LANGUAGE, the gateway's message language (en or zh).
- stdout lines starting with `@@PROGRESS ` are progress; the gateway updates the same
  message in place. Other stdout is buffered and sent at the end.
- Absolute paths on stdout inside ALLOWED_WORKDIRS become "send file" buttons.
- Exit code 0 means success; otherwise stderr is shown to the user.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in {"-h", "--help"}:
        print("Usage: /example <any text>")
        print("Writes example-output.txt to TG_WORKDIR and demonstrates progress lines.")
        return 0

    text = " ".join(args)
    workdir = Path(os.environ.get("TG_WORKDIR", ".")).expanduser()
    workdir.mkdir(parents=True, exist_ok=True)
    target = (workdir / "example-output.txt").resolve()

    for step in range(1, 4):
        print(f"@@PROGRESS Step {step}/3: processing “{text}”", flush=True)
        time.sleep(0.5)

    target.write_text(f"Input: {text}\nArgument count: {len(args)}\n", encoding="utf-8")

    print(f"Received {len(args)} arguments: {args}")
    print(f"Gateway language: {os.environ.get('TG_LANGUAGE', 'en')}")
    print(f"Wrote artifact: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
