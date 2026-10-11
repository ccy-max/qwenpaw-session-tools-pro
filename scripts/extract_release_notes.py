#!/usr/bin/env python3
"""从 CHANGELOG.md 提取指定版本的段落，用作 GitHub Release 说明。

用法：python3 scripts/extract_release_notes.py v3.0.3
版本号可带可不带 v 前缀；CHANGELOG.md 中以 `## vX.Y.Z` 为段落标题，
提取该标题到下一个 `## ` 标题（或文件尾）之间的内容。

找不到对应版本时打印全文并返回非零，让 CI 立刻暴露问题。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"


def extract(version: str) -> str | None:
    text = CHANGELOG.read_text(encoding="utf-8")
    pattern = re.compile(
        r"(^## " + re.escape(version) + r"\b.*?)(?=^## |\Z)",
        re.M | re.S,
    )
    m = pattern.search(text)
    return m.group(1).strip() if m else None


def main() -> int:
    version = sys.argv[1].lstrip("v") if len(sys.argv) > 1 else ""
    if not version:
        print("用法: extract_release_notes.py <版本号>", file=sys.stderr)
        return 1
    notes = extract(version) or extract("v" + version)
    if not notes:
        print(f"❌ CHANGELOG.md 中未找到 {version} 段落", file=sys.stderr)
        return 1
    print(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
