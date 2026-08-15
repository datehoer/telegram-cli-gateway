from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Attachment:
    path: Path
    name: str
    mime_type: str
    is_image: bool

