#!/usr/bin/env python3
"""project-memory-sculptor 辅助工具

用法:
  python sculpt.py status [--path <AGENTS.md 目录>]   检查记忆库状态
  python sculpt.py propose -c "类别" -r "规则内容" [-e "证据/理由"] [-s "使用条件"]
                                                    生成一条提案(打印, 不写入)
  python sculpt.py review [--path <AGENTS.md 目录>]  列出 [待确认] 全部提案
  python sculpt.py approve <索引> [--path <AGENTS.md 目录>]   提升第 N 条到 [已生效]
  python sculpt.py reject <索引> [--path <AGENTS.md 目录>]    删除第 N 条
"""
import argparse
import re
import sys
from pathlib import Path

WAITING = "[待确认]"
ACTIVE = "[已生效]"
REDLINE = "[安全红线"


def find_agents(path: str) -> Path:
    p = Path(path or ".")
    f = p if p.is_file() else p / "AGENTS.md"
    if not f.is_file():
        sys.exit(f"AGENTS.md not found at {f}")
    return f


HEADER_RE = re.compile(r"^##\s*(\[[^\]]+\])")


def read_blocks(f: Path):
    text = f.read_text(encoding="utf-8")
    lines = text.split("\n")
    blocks = {}
    cur = None
    for i, line in enumerate(lines):
        m = HEADER_RE.match(line.strip())
        if m:
            cur = m.group(1)
            blocks[cur] = {"start": i, "end": len(lines) - 1, "lines": []}
        elif cur is not None:
            blocks[cur]["lines"].append((i, line))
    return text, lines, blocks


def _real_items(lines):
    return [ln for _, ln in lines if ln.strip().startswith("- ") and "暂空" not in ln]


def cmd_status(args):
    f = find_agents(args.path)
    _, lines, blocks = read_blocks(f)
    print(f"AGENTS.md: {f}")
    print(f"总行数: {len(lines)}")
    if not blocks:
        print("未发现标准区块（[已生效]/[待确认]）。建议运行: sculpt.py init")
        return
    for name, b in blocks.items():
        items = _real_items(b["lines"])
        print(f"  {name}: {len(items)} 条")


def cmd_propose(args):
    if not args.rule:
        sys.exit("需要 -r/--rule 规则内容")
    cat = args.category or "经验"
    parts = [f"[{cat}]：{args.rule}"]
    if args.condition:
        parts.append(f"使用条件：{args.condition}")
    if args.evidence:
        parts.append(f"证据：{args.evidence}")
    rule = "（" + "；".join(parts[1:]) + "）" if len(parts) > 1 else ""
    print(f"- {parts[0]}{rule}")


def cmd_review(args):
    f = find_agents(args.path)
    text, _, blocks = read_blocks(f)
    if WAITING not in blocks:
        print("没有 [待确认] 区块，无待审查提案")
        return
    items = [(i, ln) for i, ln in blocks[WAITING]["lines"] if ln.strip().startswith("- ") and "暂空" not in ln]
    if not items:
        print("[待确认] 区块为空")
        return
    print(f"共 {len(items)} 条待审查：")
    for idx, (lineno, ln) in enumerate(items, 1):
        print(f"  [{idx}] (line {lineno + 1}) {ln.strip()}")


def _extract_items(blocks):
    return [(i, ln) for i, ln in blocks.get(WAITING, {}).get("lines", []) if ln.strip().startswith("- ") and "暂空" not in ln]


def cmd_approve(args):
    f = find_agents(args.path)
    text, lines, blocks = read_blocks(f)
    items = _extract_items(blocks)
    if not 1 <= args.index <= len(items):
        sys.exit(f"索引无效: {args.index} (有效范围 1-{len(items)})")
    target_line, content = items[args.index - 1]
    item = content.strip()
    lines[target_line] = ""

    if ACTIVE not in blocks:
        lines.append("")
        lines.append(f"## {ACTIVE}")
        lines.append("")
        lines.append(item)
        f.write_text("\n".join(lines), encoding="utf-8")
        print(f"已提升 [已生效] <- {item}")
        return

    block_start = blocks[ACTIVE]["start"]
    next_header = len(lines)
    for i in range(block_start + 1, len(lines)):
        if i != target_line and HEADER_RE.match(lines[i].strip()):
            next_header = i
            break

    insert_at = block_start + 1
    for i in range(block_start + 1, next_header):
        if i != target_line and lines[i].strip():
            insert_at = i + 1

    if target_line < insert_at:
        insert_at -= 1

    lines.insert(insert_at, item)
    f.write_text("\n".join(lines), encoding="utf-8")
    print(f"已提升 [已生效] <- {item}")


def cmd_reject(args):
    f = find_agents(args.path)
    text, lines, blocks = read_blocks(f)
    items = _extract_items(blocks)
    if not 1 <= args.index <= len(items):
        sys.exit(f"索引无效: {args.index} (有效范围 1-{len(items)})")
    target_line, content = items[args.index - 1]
    print(f"已删除: {content.strip()}")
    lines[target_line] = ""
    f.write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="项目记忆雕刻师辅助工具")
    ap.add_argument("--path", default=".", help="AGENTS.md 所在目录或文件")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status")
    p_status.add_argument("--path", default=".")

    p_propose = sub.add_parser("propose")
    p_propose.add_argument("-c", "--category", default="经验")
    p_propose.add_argument("-r", "--rule", required=True)
    p_propose.add_argument("-e", "--evidence", default="")
    p_propose.add_argument("-s", "--condition", default="")

    p_review = sub.add_parser("review")
    p_review.add_argument("--path", default=".")

    p_approve = sub.add_parser("approve")
    p_approve.add_argument("index", type=int)
    p_approve.add_argument("--path", default=".")

    p_reject = sub.add_parser("reject")
    p_reject.add_argument("index", type=int)
    p_reject.add_argument("--path", default=".")

    args = ap.parse_args()
    handlers = {
        "status": cmd_status,
        "propose": cmd_propose,
        "review": cmd_review,
        "approve": cmd_approve,
        "reject": cmd_reject,
    }
    handlers[args.cmd](args)


if __name__ == "__main__":
    main()
