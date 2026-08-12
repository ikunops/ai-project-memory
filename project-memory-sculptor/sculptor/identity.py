"""P0-1 稳定身份模型。

提案不以行号索引，而以「内容派生 ID」引用：
    compute_id(category, rule) -> "P-" + sha256(f"{category}:{rule}")[:8]

特点：
  - 幂等：相同 (类别, 规则) 永远得到相同 ID，跨进程、跨会话一致。
  - 内容寻址：外界无需维护计数器或 UUID 注册表，避免 ID 漂移/冲突。
  - 旧格式兼容：没有显式 ID 的现有条目，按其内容即时算出临时 ID，
    调用方可用该 ID 精确引用，无需先「升级」文件格式。
"""

import hashlib
import re

# 提案行：  "- [类别]：规则内容（证据：...；使用条件：...）"
ITEM_RE = re.compile(r"^\s*-\s*\[([^\]]+)\][：:]\s*(.*\S)\s*$")

# 区块头：  "## [已生效]" / "## [待确认]" / "## [安全红线 - ...]"
HEADER_RE = re.compile(r"^##\s*(\[[^\]]+\])")


def compute_id(category: str, rule: str) -> str:
    """由 (类别, 规则正文) 派生稳定 ID。"""
    norm = f"{category.strip()}:{rule.strip()}"
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()
    return "P-" + digest[:8]


def is_id(token: str) -> bool:
    """是否形如 P-xxxxxxxx 的稳定 ID。"""
    return bool(re.fullmatch(r"P-[0-9a-f]{8}", token or ""))
