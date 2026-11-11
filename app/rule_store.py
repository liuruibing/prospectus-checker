from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.rule_compiler import _is_plain_chinese_rule, load_compiled_execution_plan


RULE_ROOT = Path(__file__).resolve().parents[1] / "rules"
RULE_SCOPES = {
    "investment": "投资组合核对",
    "product_summary": "产品资料概要核对",
}
MAX_RULE_BYTES = 512_000


class RuleConflictError(ValueError):
    pass


def _scope_dir(scope: str) -> Path:
    if scope not in RULE_SCOPES:
        raise FileNotFoundError("规则目录不存在")
    directory = RULE_ROOT / scope
    if directory.is_symlink() or directory.resolve().parent != RULE_ROOT.resolve():
        raise FileNotFoundError("规则目录不可用")
    return directory


def _rule_path(scope: str, filename: str) -> Path:
    if not filename or Path(filename).name != filename or "/" in filename or "\\" in filename:
        raise FileNotFoundError("规则文件不存在")
    if Path(filename).suffix.lower() != ".md":
        raise FileNotFoundError("仅支持 Markdown 规则文件")
    directory = _scope_dir(scope)
    path = directory / filename
    if path.is_symlink() or not path.is_file() or path.resolve().parent != directory.resolve():
        raise FileNotFoundError("规则文件不存在")
    return path


def _file_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _title(scope: str, filename: str, content: str) -> str:
    heading = re.search(r"^#\s+(.+?)\s*#*\s*$", content, re.MULTILINE)
    if heading:
        return heading.group(1).strip()
    return Path(filename).stem.replace("_", " ")


def list_rules() -> list[dict[str, Any]]:
    entries = []
    for scope, label in RULE_SCOPES.items():
        directory = _scope_dir(scope)
        for path in sorted(directory.glob("*.md"), key=lambda value: value.name.casefold()):
            if path.is_symlink() or not path.is_file():
                continue
            content = path.read_text(encoding="utf-8")
            entries.append({
                "scope": scope,
                "scope_label": label,
                "filename": path.name,
                "title": _title(scope, path.name, content),
                "sha256": _file_hash(content),
                "updated_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
                "characters": len(content),
            })
    return entries


def get_rule(scope: str, filename: str) -> dict[str, Any]:
    path = _rule_path(scope, filename)
    content = path.read_text(encoding="utf-8")
    return {
        "scope": scope,
        "scope_label": RULE_SCOPES[scope],
        "filename": path.name,
        "title": _title(scope, path.name, content),
        "content": content,
        "sha256": _file_hash(content),
        "updated_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
    }


def _validate_summary_rules(scope: str, filename: str, content: str) -> None:
    del scope, filename
    if not _is_plain_chinese_rule(content):
        raise ValueError("规则须使用中文 Markdown 标题和正文，不接受参数文件或旧格式")
    sections: dict[str, list[str]] = {}
    active = None
    for line in content.splitlines():
        if line.startswith("## "):
            active = line[3:].strip()
            if not active or active in sections:
                raise ValueError("规则分区标题不能为空或重复")
            sections[active] = []
        elif active:
            sections[active].append(line)
    if not sections or any(not "\n".join(lines).strip() for lines in sections.values()):
        raise ValueError("每个规则分区都需要填写说明内容")


def save_rule(scope: str, filename: str, content: str, expected_sha256: str) -> dict[str, Any]:
    path = _rule_path(scope, filename)
    if not isinstance(content, str) or not content.strip():
        raise ValueError("规则内容不能为空")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_RULE_BYTES:
        raise ValueError("规则文件不能超过 512 KB")
    if "\x00" in content:
        raise ValueError("规则内容包含无效字符")
    current = path.read_text(encoding="utf-8")
    current_sha256 = _file_hash(current)
    if current_sha256 != expected_sha256:
        raise RuleConflictError("文件已被其他操作更新，请重新载入后再保存")
    _validate_summary_rules(scope, filename, content)
    if not filename.startswith("common_") and load_compiled_execution_plan(path, content) is None:
        raise ValueError("中文规则尚未生成当前版本的通用执行计划，修改尚未生效")

    original_mode = stat.S_IMODE(path.stat().st_mode)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temp_name = stream.name
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, original_mode)
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
    return get_rule(scope, filename)
