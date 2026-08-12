"""P0-3 状态机解析器（v2：标题感知 + 嵌套围栏）。

v1 的缺陷（本版修复）：
  1. 只认 '## [xxx]' 为边界 —— 文档里 '## 简介' 等普通标题会被吞进相邻
     区块，approve/reject 重写文件时被悄悄移动位置；
  2. 文件头（'# AGENTS.md' 等第一区块之前的内容）被直接丢弃；
  3. 嵌套代码围栏（README 里展示 markdown 示例）会翻转状态机。

本版规则：
  - 三种区块：preamble（name=None，原样保留）、非方括号标题区块
    （name=标题行原文，原样保留）、方括号区块（name='[xxx]'）。
  - 代码围栏按长度跟踪：开启 fence 记下长度，闭合 fence 需 >= 开启长度
    （GitHub Flavored Markdown 语义），支持嵌套展示示例。
"""

import re

from . import identity

HEADER_RE = identity.HEADER_RE
ITEM_RE = identity.ITEM_RE

# 代码围栏：至少 3 个反引号（不处理波浪号围栏，AGENTS.md 惯例用反引号）
FENCE_RE = re.compile(r"^\s*(`{3,})")
# 任意 Markdown 标题（# ~ ######）
ANY_HEADER_RE = re.compile(r"^\s*(#{1,6}\s+\S.*)$")


class Proposal:
    """一条提案。id 由内容派生，跨进程稳定。"""

    __slots__ = ("id", "category", "rule", "raw", "block", "index")

    def __init__(self, pid, category, rule, raw, block, index):
        self.id = pid
        self.category = category
        self.rule = rule
        self.raw = raw          # 原始整行文本（含前缀 "- "）
        self.block = block      # 所属区块名，如 "[待确认]"
        self.index = index      # 在所属区块内的 0-based 序号

    def __repr__(self):
        return f"<Proposal {self.id} {self.category!r}>"


def _is_plain_header(stripped: str):
    """非方括号的 Markdown 标题行，原样返回标题行（含 # 前缀）。"""
    m = ANY_HEADER_RE.match(stripped)
    if not m:
        return None
    header = m.group(1)
    if identity.HEADER_RE.match(header):
        return None  # 方括号区块交给方括号分支
    return header


def split_blocks(text: str):
    """返回 [(name, [正文行...]), ...]。

    name 语义：
      - None            -> preamble（第一区块之前的内容），dump 原样输出
      - "[xxx]"         -> 方括号区块（[已生效]/[待确认]/[安全红线...]）
      - "## 简介" 等     -> 非方括号标题区块，dump 原样输出标题行本身
    正文行不含区块头那一行。代码块内的标题/提案不会被当作真实结构。
    """
    blocks = []
    cur_name = None
    cur_lines = []
    fence_len = 0  # 0 = 在代码块外；>0 = 当前代码块开启 fence 的长度
    for line in text.split("\n"):
        stripped = line.strip()
        fm = FENCE_RE.match(stripped)
        if fm:
            fl = len(fm.group(1))
            if fence_len == 0:
                fence_len = fl
            elif fl >= fence_len:
                fence_len = 0
            if cur_name is not None:
                cur_lines.append(line)
            elif blocks and blocks[0][0] is None:
                blocks[0][1].append(line)
            else:
                blocks.append((None, [line]))
            continue
        if fence_len == 0:
            m = HEADER_RE.match(stripped)
            if m:
                if cur_name is not None:
                    blocks.append((cur_name, cur_lines))
                cur_name = m.group(1)
                cur_lines = []
                continue
            h = _is_plain_header(stripped)
            if h:
                if cur_name is not None:
                    blocks.append((cur_name, cur_lines))
                cur_name = h
                cur_lines = []
                continue
        if cur_name is not None:
            cur_lines.append(line)
        elif blocks and blocks[0][0] is None:
            blocks[0][1].append(line)
        else:
            blocks.append((None, [line]))
    if cur_name is not None:
        blocks.append((cur_name, cur_lines))
    return blocks


def iter_items(block_name: str, body_lines):
    """从某区块正文行中抽取提案，逐条附稳定 ID。

    与 split_blocks 同样感知代码块：代码围栏内部的 '- [类别]：...'
    行不会被当作真实提案（避免示例/文档被误提取）。
    """
    items = []
    fence_len = 0
    for i, line in enumerate(body_lines):
        s = line.strip()
        fm = FENCE_RE.match(s)
        if fm:
            fl = len(fm.group(1))
            if fence_len == 0:
                fence_len = fl
            elif fl >= fence_len:
                fence_len = 0
            continue
        if fence_len:
            continue
        m = ITEM_RE.match(line)
        if not m:
            continue
        category, rule = m.group(1), m.group(2)
        pid = identity.compute_id(category, rule)
        items.append(Proposal(pid, category, rule, line, block_name, i))
    return items


def find_block(blocks, name: str):
    for bn, lines in blocks:
        if bn == name:
            return lines
    return None


def dump(blocks) -> str:
    """blocks -> 文本。方括号区块 header 与正文间保留一个空行。

    name=None 或非方括号标题区块按原始行原样输出，保证任何重写
    （approve/reject）都不会移动文件其它部分的位置。
    """
    out = []
    for name, lines in blocks:
        if name is None:
            out.extend(lines)
            continue
        if name.startswith("#"):
            out.append(name)
            if lines and lines[0].strip() != "":
                out.append("")
            out.extend(lines)
            continue
        out.append(f"## {name}")
        if lines:
            if lines[0].strip() != "":
                out.append("")
            out.extend(lines)
        else:
            out.append("")
    return "\n".join(out)
