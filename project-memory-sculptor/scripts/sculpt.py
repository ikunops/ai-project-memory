#!/usr/bin/env python3
"""project-memory-sculptor 辅助工具（健壮内核版）

CLI 表面与旧版完全一致：
  python sculpt.py status [--path <AGENTS.md 目录>]
  python sculpt.py propose -c "类别" -r "规则内容" [-e "证据"] [-s "使用条件"] [--write]
  python sculpt.py review [--path <AGENTS.md 目录>]
  python sculpt.py approve <行号 | P-xxxx> [--dry-run] [--path ...]
  python sculpt.py reject  <行号 | P-xxxx> [--purge] [--dry-run] [--path ...]
  python sculpt.py amend   <行号 | P-xxxx> [-c 新类别] [-r 新规则] [-e 新证据] [-s 新条件] [--dry-run]

改进（详见 sculptor/ 包）：
  P0-1 稳定ID：approve/reject 支持内容派生的 P-xxxx ID，不再依赖易漂移的行号。
  P0-2 事务写入：所有改写走 storage.atomic_write（锁 + fsync + os.replace + 写前 hash 校验）。
  P0-3 状态机解析：代码块内的 '## [xxx]' 不再被误判为区块边界。
  归档层：reject 默认归档到 docs/archived-rules.md（--purge 才彻底删除）。
  amend：按 ID/行号编辑 [待确认] 提案（未指定字段保留原值）。
  --dry-run：预览变更 diff，不写盘。
"""
import argparse
import difflib
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

# 让 scripts/ 同级的 sculptor/ 包可被 import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sculptor import identity, storage, parser  # noqa: E402

WAITING = "[待确认]"
ACTIVE = "[已生效]"


def find_agents(path: str, allow_missing: bool = False) -> Path:
    p = Path(path or ".")
    f = p if p.is_file() else p / "AGENTS.md"
    if not f.is_file() and not allow_missing:
        sys.exit(f"AGENTS.md not found at {f}")
    return f


def _read_blocks(p: Path):
    text = storage.read_text(p)
    return text, parser.split_blocks(text)


def _real_items(p: Path, block_name: str):
    _, blocks = _read_blocks(p)
    body = parser.find_block(blocks, block_name)
    if body is None:
        return []
    return parser.iter_items(block_name, body)


# ---------- 提案行构建/解析 ----------

def _build_line(category: str, rule: str, evidence: str = "", condition: str = "") -> str:
    """按标准格式构建提案行（与 propose 输出一致）。"""
    meta = []
    if condition:
        meta.append(f"使用条件：{condition}")
    if evidence:
        meta.append(f"证据：{evidence}")
    suffix = "（" + "；".join(meta) + "）" if meta else ""
    return f"- [{category}]：{rule}{suffix}"


def _split_rule(rule: str):
    """'规则内容（证据：X；使用条件：Y）' -> (规则, 证据, 条件)。

    规则正文里的括号不受影响（非贪婪匹配 + 尾部锚定）。
    """
    m = re.match(r"^(.*?)（(.*)）$", rule)
    if not m:
        return rule, "", ""
    body, meta = m.group(1), m.group(2)
    evid = cond = ""
    for part in meta.split("；"):
        if part.startswith("证据："):
            evid = part[3:]
        elif part.startswith("使用条件："):
            cond = part[5:]
    return body, evid, cond


# ---------- 归档层 ----------

def _archive_path(f: Path) -> Path:
    return f.parent / "docs" / "archived-rules.md"


def _archive_entry(target, raw: str) -> str:
    ts = datetime.now().isoformat(timespec="seconds")
    return (
        f"## 归档 {ts}\n\n"
        f"- 提案 ID：{target.id}\n"
        f"- 原区块：[待确认]\n\n"
        f"{raw.strip()}\n"
    )


def _append_archive(f: Path, entry: str) -> None:
    """把归档条目追加到 docs/archived-rules.md（事务写入）。

    先写归档、后改主文件：归档成功而主文件失败只产生多出的记录，
    反过来则会造成提案丢失（Fail-Safe 偏向不丢数据）。
    """
    ap = _archive_path(f)
    if ap.exists():
        old = storage.read_text(ap)
        new_text = old.rstrip() + "\n\n" + entry.rstrip() + "\n"
        expected = storage.sha256_text(old)
    else:
        ap.parent.mkdir(parents=True, exist_ok=True)
        new_text = "# 已归档规则（被否决/过期/移除的提案）\n\n" + entry.rstrip() + "\n"
        expected = None
    storage.atomic_write(ap, new_text, expected_hash=expected)


# ---------- 区块变换 ----------

def _remove_from_waiting(blocks, target):
    new_blocks = []
    for name, lines in blocks:
        if name == WAITING:
            lines = list(lines)
            del lines[target.index]
            new_blocks.append((name, lines))
        else:
            new_blocks.append((name, lines))
    return new_blocks


def _append_waiting(blocks, line):
    """向 [待确认] 追加一行；区块不存在时创建标准结构。"""
    return _append_block(blocks, line, WAITING)


def _new_file_text(f: Path, line: str) -> str:
    """AGENTS.md 不存在时创建最小标准结构。"""
    name = f.parent.name or "项目"
    return (
        f"# AGENTS.md — {name}\n\n"
        "## [已生效]\n\n- 暂空\n\n"
        f"## [待确认]\n\n{line}\n"
    )


def cmd_init(args):
    """创建标准 AGENTS.md（含 [项目家风]/[已生效]/[待确认]/[安全红线]）。

    优先复制 templates/AGENTS.template.md（单一事实来源），模板缺失时
    回退最小结构。已存在则拒绝覆盖（不猜、不破坏）。
    """
    f = find_agents(args.path, allow_missing=True)
    if f.exists():
        sys.exit(f"{f} 已存在，不覆盖（可用 status 查看现状）")
    template = Path(__file__).resolve().parent.parent / "templates" / "AGENTS.template.md"
    if template.is_file():
        text = template.read_text(encoding="utf-8").replace("<项目名>", f.parent.name or "项目")
    else:
        text = _new_file_text(f, "- 暂空")
    storage.atomic_write(f, text, expected_hash=None)
    print(f"已创建标准结构: {f}")


def _trim_edges(lines: list) -> list:
    """移除区块正文首尾的空行，保留中间（含代码块内）的空行。"""
    start = 0
    end = len(lines)
    while start < end and lines[start].strip() == "":
        start += 1
    while end > start and lines[end - 1].strip() == "":
        end -= 1
    return lines[start:end]


def _append_block(blocks, line, block_name):
    """向指定区块追加一行；区块不存在时补齐标准结构。"""
    new_blocks = []
    appended = False
    for name, body in blocks:
        if name == block_name and not appended:
            body = _trim_edges(body) + [line]
            appended = True
        new_blocks.append((name, body))
    if not appended:
        other = WAITING if block_name == ACTIVE else ACTIVE
        if parser.find_block(new_blocks, other) is None:
            new_blocks.append((other, ["- 暂空"]))
        new_blocks.append((block_name, [line]))
    return new_blocks


# ---------- 事务应用 ----------

def _print_diff(old: str, new: str) -> None:
    for line in difflib.unified_diff(
        old.splitlines(), new.splitlines(), lineterm="", n=2
    ):
        print(line)


def _apply(path: Path, mutate, dry_run: bool = False):
    """读 -> mutate(blocks)->新文本；dry_run 只打印 diff；否则事务写。"""
    text, blocks = _read_blocks(path)
    new_text = mutate(blocks)
    if dry_run:
        print(f"[dry-run] 预览 {path} 的变更：")
        _print_diff(text, new_text)
        return None
    storage.atomic_write(path, new_text, expected_hash=storage.sha256_text(text))
    return new_text


# ---------- 命令 ----------

def cmd_status(args):
    f = find_agents(args.path)
    _, blocks = _read_blocks(f)
    print(f"AGENTS.md: {f}")
    if not blocks:
        print("未发现标准区块（[已生效]/[待确认]）。建议运行: sculpt.py init")
        return
    found = False
    for name, body in blocks:
        if not name.startswith("["):
            continue
        found = True
        n = len(parser.iter_items(name, body))
        print(f"  {name}: {n} 条")
    if not found:
        print("文件存在但无 [已生效]/[待确认] 区块。可用: sculpt.py init 或 propose --write 开始沉淀")


def cmd_propose(args):
    if not args.rule:
        sys.exit("需要 -r/--rule 规则内容")
    if args.auto:
        args.write = True  # --auto 隐含 --write
    cat = args.category or "经验"
    line = _build_line(cat, args.rule, args.evidence, args.condition)
    if not args.write:
        print(line)
        return

    if args.auto and not (args.evidence and args.condition):
        sys.exit("--auto 直接写入 [已生效] 时必须同时提供 -e 证据 和 -s 使用条件（反过拟合三原则强校验）")
    target = ACTIVE if args.auto else WAITING

    f = find_agents(args.path, allow_missing=True)
    if f.is_file():
        text, blocks = _read_blocks(f)
        ids = set()
        for bn in (WAITING, ACTIVE):
            for it in _real_items(f, bn):
                ids.add(it.id)
        new_id = identity.compute_id(cat, args.rule)
        if new_id in ids:
            sys.exit(f"已存在相同提案（ID {new_id}），未重复追加：{line}")
        new_text = parser.dump(_append_block(blocks, line, target))
        expected = storage.sha256_text(text)
    else:
        if target == ACTIVE:
            name = f.parent.name or "项目"
            new_text = (
                f"# AGENTS.md — {name}\n\n"
                f"## [已生效]\n\n{line}\n\n"
                "## [待确认]\n\n- 暂空\n"
            )
        else:
            new_text = _new_file_text(f, line)
        expected = None
    storage.atomic_write(f, new_text, expected_hash=expected)
    print(f"已追加到 {f} 的 {target}：{line}")


def cmd_review(args):
    f = find_agents(args.path)
    items = _real_items(f, WAITING)
    if not items:
        print("[待确认] 区块为空或不存在")
        return
    active_ids = {it.id for it in _real_items(f, ACTIVE)}
    id_counts = Counter(it.id for it in items)
    print(f"共 {len(items)} 条待审查：")
    by_cat = {}
    for it in items:
        by_cat.setdefault(it.category, []).append(it)
    pos = {id(it): i + 1 for i, it in enumerate(items)}
    for cat, its in sorted(by_cat.items()):
        print(f"\n[{cat}]")
        for it in its:
            notes = []
            if it.id in active_ids:
                notes.append("与 [已生效] 重复")
            if id_counts[it.id] > 1:
                notes.append(f"同 ID 出现 {id_counts[it.id]} 次（内容可能重复）")
            tail = "  <-- " + "；".join(notes) if notes else ""
            print(f"  [{pos[id(it)]}] {it.id} {it.raw.strip()}{tail}")


def _resolve_target(token: str, items):
    if identity.is_id(token):
        for it in items:
            if it.id == token:
                return it
        sys.exit(f"ID 未找到: {token}")
    try:
        idx = int(token)
    except ValueError:
        sys.exit(f"无效目标: {token}（请用行号或 P-xxxx ID）")
    if not 1 <= idx <= len(items):
        sys.exit(f"索引无效: {token} (有效范围 1-{len(items)})")
    return items[idx - 1]


def _require_waiting(f) -> list:
    items = _real_items(f, WAITING)
    if not items:
        sys.exit("[待确认] 为空，无提案可操作")
    return items


def cmd_approve(args):
    f = find_agents(args.path)
    items = _require_waiting(f)
    target = _resolve_target(args.target, items)
    raw = target.raw

    def mutate(blocks):
        new_blocks = _remove_from_waiting(blocks, target)
        if parser.find_block(new_blocks, ACTIVE) is None:
            new_blocks.append((ACTIVE, ["", raw.strip()]))
        else:
            new_blocks = [
                (name, lines + [raw.strip()] if name == ACTIVE else lines)
                for name, lines in new_blocks
            ]
        return parser.dump(new_blocks)

    _apply(f, mutate, dry_run=args.dry_run)
    if not args.dry_run:
        print(f"已提升 [已生效] <- {raw.strip()}")


def cmd_reject(args):
    f = find_agents(args.path)
    items = _require_waiting(f)
    target = _resolve_target(args.target, items)
    raw = target.raw

    if args.dry_run:
        def mutate(blocks):
            return parser.dump(_remove_from_waiting(blocks, target))
        _apply(f, mutate, dry_run=True)
        return

    if not args.purge:
        _append_archive(f, _archive_entry(target, raw))

    def mutate(blocks):
        return parser.dump(_remove_from_waiting(blocks, target))

    _apply(f, mutate)
    if args.purge:
        print(f"已彻底删除: {raw.strip()}")
    else:
        print(f"已否决并归档到 {_archive_path(f)}: {raw.strip()}")


def cmd_amend(args):
    f = find_agents(args.path)
    items = _require_waiting(f)
    target = _resolve_target(args.target, items)
    if not (args.rule or args.category or args.evidence or args.condition):
        sys.exit("至少提供一项修改内容（-r / -c / -e / -s）")

    body, evid, cond = _split_rule(target.rule)
    new_rule = args.rule if args.rule is not None else body
    new_evid = args.evidence if args.evidence is not None else evid
    new_cond = args.condition if args.condition is not None else cond
    new_cat = args.category or target.category
    line = _build_line(new_cat, new_rule, new_evid, new_cond)
    old_line = target.raw.rstrip()

    def mutate(blocks):
        new_blocks = []
        for name, lines in blocks:
            if name == WAITING:
                lines = list(lines)
                lines[target.index] = line
                new_blocks.append((name, lines))
            else:
                new_blocks.append((name, lines))
        return parser.dump(new_blocks)

    _apply(f, mutate, dry_run=args.dry_run)
    if not args.dry_run:
        print(f"已修改:\n  旧: {old_line}\n  新: {line}")


def main():
    ap = argparse.ArgumentParser(description="项目记忆雕刻师辅助工具（健壮内核版）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status")
    p_status.add_argument("--path", default=".")

    p_init = sub.add_parser("init")
    p_init.add_argument("--path", default=".")

    p_propose = sub.add_parser("propose")
    p_propose.add_argument("--path", default=".")
    p_propose.add_argument("-c", "--category", default="经验")
    p_propose.add_argument("-r", "--rule", required=True)
    p_propose.add_argument("-e", "--evidence", default="")
    p_propose.add_argument("-s", "--condition", default="")
    p_propose.add_argument(
        "--write", action="store_true",
        help="直接写入目标 AGENTS.md 的 [待确认] 区块（事务写入 + 重复检测）",
    )
    p_propose.add_argument(
        "--auto", action="store_true",
        help="双模式-自动：直接写入 [已生效]（必须带 -e 证据 与 -s 使用条件）",
    )

    p_review = sub.add_parser("review")
    p_review.add_argument("--path", default=".")

    def _add_target_args(p):
        p.add_argument("target", help="行号（1-based）或 P-xxxx 稳定ID")
        p.add_argument("--path", default=".")
        p.add_argument(
            "--dry-run", action="store_true",
            help="只预览变更 diff，不写盘",
        )

    p_approve = sub.add_parser("approve")
    _add_target_args(p_approve)

    p_reject = sub.add_parser("reject")
    _add_target_args(p_reject)
    p_reject.add_argument(
        "--purge", action="store_true",
        help="彻底删除而不归档到 docs/archived-rules.md",
    )

    p_amend = sub.add_parser("amend")
    _add_target_args(p_amend)
    p_amend.add_argument("-c", "--category", default=None)
    p_amend.add_argument("-r", "--rule", default=None)
    p_amend.add_argument("-e", "--evidence", default=None)
    p_amend.add_argument("-s", "--condition", default=None)

    args = ap.parse_args()
    handlers = {
        "status": cmd_status,
        "init": cmd_init,
        "propose": cmd_propose,
        "review": cmd_review,
        "approve": cmd_approve,
        "reject": cmd_reject,
        "amend": cmd_amend,
    }
    handlers[args.cmd](args)


if __name__ == "__main__":
    main()
