"""sculptor 健壮性验收测试（对应群聊落地方案 8 条硬门槛中的关键项）。

覆盖：
  P0-1 稳定ID：相同内容 -> 相同 ID；approve/reject 可按 ID 精确引用。
  P0-2 事务写入：写前 hash 校验失败（磁盘已被外部修改）-> 拒绝写入，原文件不变。
  P0-3 状态机：代码块内的 '## [待确认]' 不被误判为区块边界。
  兼容：原有按行号 approve/reject 流程仍可用（test_sculpt.py 已覆盖）。
"""

import os
import sys
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sculptor import identity, storage, parser  # noqa: E402

SCULPT = ROOT / "scripts" / "sculpt.py"


# ---------- P0-1 稳定ID ----------

def test_stable_id_is_idempotent():
    a = identity.compute_id("经验", "计算密集任务优先 Rust")
    b = identity.compute_id("经验", "计算密集任务优先 Rust")
    assert a == b
    assert a.startswith("P-") and len(a) == 10
    assert identity.is_id(a)


def test_stable_id_differs_by_content():
    a = identity.compute_id("经验", "规则A")
    b = identity.compute_id("经验", "规则B")
    assert a != b


# ---------- P0-3 状态机解析 ----------

def test_code_fence_not_treated_as_block():
    text = (
        "## [已生效]\n\n- 暂空\n\n"
        "## [待确认]\n\n- [经验]：正常提案\n\n"
        "代码块里的伪提案不应被解析：\n"
        "```\n"
        "## [待确认]\n"
        "- [经验]：这是代码里的示例，不是真提案\n"
        "```\n"
    )
    blocks = parser.split_blocks(text)
    names = [n for n, _ in blocks]
    # 只有两个真实区块
    assert names == ["[已生效]", "[待确认]"], names
    waiting = parser.find_block(blocks, "[待确认]")
    items = parser.iter_items("[待确认]", waiting)
    assert len(items) == 1, [i.raw for i in items]
    assert "正常提案" in items[0].raw


# ---------- P0-2 事务写入 ----------

def test_atomic_write_basic(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("hello", encoding="utf-8")
    h = storage.sha256_text("hello")
    storage.atomic_write(f, "world", expected_hash=h)
    assert f.read_text(encoding="utf-8") == "world"


def test_atomic_write_fail_closed_on_external_change(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("original", encoding="utf-8")
    h = storage.sha256_text("original")
    # 模拟外部在「读」与「写」之间改了文件
    f.write_text("tampered", encoding="utf-8")
    with pytest.raises(storage.ConcurrentModification):
        storage.atomic_write(f, "new", expected_hash=h)
    # 原文件未被覆盖，仍是外部改后的内容
    assert f.read_text(encoding="utf-8") == "tampered"
    # 不留临时文件
    assert not f.with_name(f.name + ".tmp").exists()


# ---------- 端到端：按 ID 与按行号 approve ----------

def _run(*args, cwd=None):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(SCULPT), *args],
        capture_output=True, text=True, cwd=cwd, encoding="utf-8", env=env,
    )


def _write_agents(tmp_path, waiting_items):
    body = "\n".join(f"- {it}" for it in waiting_items)
    content = (
        "## [已生效]\n\n- 暂空\n\n"
        f"## [待确认]\n\n{body}\n"
    )
    p = tmp_path / "AGENTS.md"
    p.write_text(content, encoding="utf-8")
    return p


def test_approve_by_id(tmp_path):
    p = _write_agents(tmp_path, ["[经验]：提案一", "[经验]：提案二"])
    r = _run("review", "--path", str(tmp_path))
    assert r.returncode == 0
    # review 应输出 P-xxxx ID
    assert "P-" in r.stdout
    # 取第一个提案的 ID
    pid = next(tok for tok in r.stdout.split() if identity.is_id(tok))
    r = _run("approve", pid, "--path", str(tmp_path))
    assert r.returncode == 0, r.stderr
    content = p.read_text(encoding="utf-8")
    assert "提案一" in content
    # 提案一已离开 [待确认] 进入 [已生效] 区间
    active_idx = content.index("[已生效]")
    waiting_idx = content.index("[待确认]")
    assert active_idx < waiting_idx
    assert "提案一" in content[active_idx:waiting_idx]


def test_reject_by_index(tmp_path):
    p = _write_agents(tmp_path, ["[经验]：坏提案"])
    r = _run("reject", "1", "--path", str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "坏提案" not in p.read_text(encoding="utf-8")


def test_concurrent_approve_no_lost_update(tmp_path):
    """两进程先后 approve 同文件不同提案，结果都不丢。"""
    p = _write_agents(tmp_path, ["[经验]：提案A", "[经验]：提案B"])
    r1 = _run("approve", "1", "--path", str(tmp_path))
    r2 = _run("approve", "1", "--path", str(tmp_path))  # 此时原第2条已变成第1条
    assert r1.returncode == 0 and r2.returncode == 0, (r1.stderr, r2.stderr)
    content = p.read_text(encoding="utf-8")
    assert "提案A" in content and "提案B" in content


def test_external_edit_then_approve_fails_closed(tmp_path):
    """外部在「我读取之后」改了文件：写前 hash 校验失败，原文件不被覆盖。

    复现跨进程场景：进程 A 读到 hash=H，进程 B 改成 H'；A 再写时带
    expected=H 应被拒绝（Fail-Closed），磁盘保留 B 的修改而非 A 的过期内容。
    """
    p = _write_agents(tmp_path, ["[经验]：提案A", "[经验]：提案B"])
    before = storage.sha256_text(p.read_text(encoding="utf-8"))
    # 模拟另一进程在 A「读」之后改了文件
    changed = p.read_text(encoding="utf-8").replace(
        "[经验]：提案B", "[经验]：提案B（已手改）"
    )
    p.write_text(changed, encoding="utf-8")
    with pytest.raises(storage.ConcurrentModification):
        storage.atomic_write(p, "x", expected_hash=before)
    # 写被拒绝：磁盘仍是外部改后的内容，未被 "x" 覆盖
    assert p.read_text(encoding="utf-8") == changed


# ---------- v2：标题感知 / 嵌套围栏 / propose 落盘 ----------

def test_plain_headers_not_swallowed():
    """'## 简介' 等普通标题是独立区块，重写后位置不变。"""
    text = (
        "# AGENTS.md — demo\n\n"
        "## 简介\n\n这是项目说明。\n\n"
        "## [已生效]\n\n- 暂空\n\n"
        "## [待确认]\n\n- [经验]：提案X\n"
    )
    blocks = parser.split_blocks(text)
    names = [n for n, _ in blocks]
    # 一级/二级普通标题均为独立区块，不被吞并
    assert names == ["# AGENTS.md — demo", "## 简介", "[已生效]", "[待确认]"], names
    # 重写后 preamble 与普通标题原样保留、顺序不变
    out = parser.dump(blocks)
    assert "# AGENTS.md — demo" in out
    assert out.index("## 简介") < out.index("## [已生效]")
    assert "这是项目说明。" in out


def test_approve_keeps_plain_headers_in_place(tmp_path):
    """真实流程：approve 后 '## 简介' 仍位于 [已生效] 之前。"""
    content = (
        "# AGENTS.md — demo\n\n"
        "## 简介\n\n这是项目说明。\n\n"
        "## [已生效]\n\n- 暂空\n\n"
        "## [待确认]\n\n- [经验]：提案X\n"
    )
    p = tmp_path / "AGENTS.md"
    p.write_text(content, encoding="utf-8")
    r = _run("approve", "1", "--path", str(tmp_path))
    assert r.returncode == 0, r.stderr
    out = p.read_text(encoding="utf-8")
    assert out.index("## 简介") < out.index("## [已生效]")
    assert "提案X" in out[out.index("[已生效]"):out.index("[待确认]")]


def test_nested_code_fence():
    """README 里展示 markdown 示例（围栏内再开围栏）不被翻转状态机。"""
    text = (
        "## [待确认]\n\n- [经验]：真提案\n\n"
        "示例：\n"
        "```\n"
        "## [已生效]\n\n```\n"   # 内层示例围栏（等长，用于展示）
        "```\n"                   # 外层围栏闭合
        "```\n"                   # 残留？不——上面闭合后这里重新开启，再闭合
        "\n- [经验]：还是真提案\n"
    )
    blocks = parser.split_blocks(text)
    waiting = parser.find_block(blocks, "[待确认]")
    items = parser.iter_items("[待确认]", waiting)
    # 两条提案都应被识别，且不产生伪区块
    names = [n for n, _ in blocks]
    assert names == ["[待确认]"], names
    assert len(items) == 2, [i.raw for i in items]


def test_propose_write_creates_file(tmp_path):
    """propose --write 在 AGENTS.md 不存在时创建标准结构并写入。"""
    r = _run("propose", "-c", "技术选型", "-r", "优先 Rust",
             "-e", "低内存", "--write", "--path", str(tmp_path))
    assert r.returncode == 0, r.stderr
    p = tmp_path / "AGENTS.md"
    assert p.is_file()
    content = p.read_text(encoding="utf-8")
    assert "[已生效]" in content and "[待确认]" in content
    assert "优先 Rust" in content
    assert "# AGENTS.md" in content


def test_propose_write_appends_and_dedups(tmp_path):
    """propose --write 追加到已有 [待确认]；重复内容拒绝重复写入。"""
    p = _write_agents(tmp_path, ["[经验]：提案A"])
    r = _run("propose", "-c", "经验", "-r", "提案A", "--write",
             "--path", str(tmp_path))
    assert r.returncode != 0  # 重复提案被拒绝
    assert "已存在相同提案" in r.stdout + r.stderr
    # 文件未被破坏，仍只有一条
    items = parser.iter_items("[待确认]", parser.find_block(
        parser.split_blocks(p.read_text(encoding="utf-8")), "[待确认]"))
    assert len(items) == 1

    r = _run("propose", "-c", "经验", "-r", "提案B", "--write",
             "--path", str(tmp_path))
    assert r.returncode == 0, r.stderr
    content = p.read_text(encoding="utf-8")
    assert "提案A" in content and "提案B" in content


# ---------- 归档层 / amend / --dry-run / review 增强 ----------

def test_reject_archives_to_docs(tmp_path):
    """reject 默认归档到 docs/archived-rules.md，AGENTS.md 中移除。"""
    p = _write_agents(tmp_path, ["[经验]：坏提案（证据：测试）"])
    r = _run("reject", "1", "--path", str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "坏提案" not in p.read_text(encoding="utf-8")
    archive = tmp_path / "docs" / "archived-rules.md"
    assert archive.is_file()
    content = archive.read_text(encoding="utf-8")
    assert "坏提案" in content
    assert "提案 ID：P-" in content
    assert "原区块：[待确认]" in content
    # 二次归档追加而非覆盖
    r = _run("propose", "-c", "经验", "-r", "提案B", "--write",
             "--path", str(tmp_path))
    assert r.returncode == 0, r.stderr
    r = _run("reject", "1", "--path", str(tmp_path))
    assert r.returncode == 0, r.stderr
    content2 = archive.read_text(encoding="utf-8")
    assert content2.count("## 归档") == 2
    assert "提案B" in content2


def test_reject_purge_no_archive(tmp_path):
    """--purge 彻底删除，不产生归档文件。"""
    p = _write_agents(tmp_path, ["[经验]：临时提案"])
    r = _run("reject", "1", "--purge", "--path", str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "临时提案" not in p.read_text(encoding="utf-8")
    assert not (tmp_path / "docs" / "archived-rules.md").exists()


def test_amend_keeps_unspecified_fields(tmp_path):
    """amend 只改指定字段，证据/条件/类别保留原值。"""
    p = _write_agents(tmp_path, ["[经验]：旧规则（证据：原证据；使用条件：原条件）"])
    r = _run("amend", "1", "-r", "新规则", "--path", str(tmp_path))
    assert r.returncode == 0, r.stderr
    content = p.read_text(encoding="utf-8")
    assert "新规则" in content
    assert "原证据" in content and "原条件" in content
    assert "旧规则" not in content


def test_amend_category_and_dry_run(tmp_path):
    """amend 可改类别；--dry-run 只预览不写盘。"""
    p = _write_agents(tmp_path, ["[经验]：规则A（证据：E1）"])
    before = p.read_text(encoding="utf-8")
    r = _run("amend", "1", "-c", "技术选型", "--dry-run", "--path", str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "[技术选型]" in r.stdout and "-" in r.stdout  # diff 输出
    assert p.read_text(encoding="utf-8") == before  # 未写盘

    r = _run("amend", "1", "-c", "技术选型", "--path", str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "[技术选型]：规则A" in p.read_text(encoding="utf-8")


def test_approve_dry_run_no_write(tmp_path):
    """approve --dry-run 不改变文件。"""
    p = _write_agents(tmp_path, ["[经验]：提案A"])
    before = p.read_text(encoding="utf-8")
    r = _run("approve", "1", "--dry-run", "--path", str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert p.read_text(encoding="utf-8") == before


def test_review_marks_duplicate_with_active(tmp_path):
    """[待确认] 与 [已生效] 同内容时 review 标记重复。

    正常路径下 CLI 查重会阻止重复追加，此场景只能由外部手工编辑产生，
    因此直接改文件模拟（绕过 CLI 写入）。
    """
    p = _write_agents(tmp_path, ["[经验]：重复提案"])
    r = _run("approve", "1", "--path", str(tmp_path))
    assert r.returncode == 0, r.stderr
    content = p.read_text(encoding="utf-8").rstrip() + "\n- [经验]：重复提案\n"
    p.write_text(content, encoding="utf-8")
    r = _run("review", "--path", str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "与 [已生效] 重复" in r.stdout


# ---------- init / --auto 双模式 / 碰撞检测 / 编码 ----------

def test_init_creates_standard_structure(tmp_path):
    """init 创建含 [项目家风]/[安全红线] 的标准结构。"""
    r = _run("init", "--path", str(tmp_path))
    assert r.returncode == 0, r.stderr
    p = tmp_path / "AGENTS.md"
    assert p.is_file()
    content = p.read_text(encoding="utf-8")
    for section in ("[项目家风", "[已生效]", "[待确认]", "[安全红线"):
        assert section in content, section


def test_init_refuses_to_overwrite(tmp_path):
    """已存在 AGENTS.md 时 init 拒绝覆盖。"""
    p = _write_agents(tmp_path, ["[经验]：既有提案"])
    before = p.read_text(encoding="utf-8")
    r = _run("init", "--path", str(tmp_path))
    assert r.returncode != 0
    assert "已存在" in r.stderr
    assert p.read_text(encoding="utf-8") == before


def test_propose_auto_requires_evidence_and_condition(tmp_path):
    """--auto 直接入 [已生效]，缺证据或条件时拒绝（三原则强校验）。"""
    r = _run("propose", "-c", "经验", "-r", "规则X", "--write", "--auto",
             "--path", str(tmp_path))
    assert r.returncode != 0
    assert "证据" in r.stderr and "使用条件" in r.stderr

    r = _run("propose", "-c", "经验", "-r", "规则X",
             "-e", "证据Y", "-s", "条件Z", "--write", "--auto",
             "--path", str(tmp_path))
    assert r.returncode == 0, r.stderr
    content = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    active_idx = content.index("[已生效]")
    waiting_idx = content.index("[待确认]")
    assert "规则X" in content[active_idx:waiting_idx], "auto 提案应进入 [已生效]"
    assert "规则X" not in content[waiting_idx:], "不应出现在 [待确认]"


def test_review_marks_id_collision(tmp_path):
    """[待确认] 内同 ID 出现多次时 review 标记冲突。"""
    content = (
        "## [已生效]\n\n- 暂空\n\n"
        "## [待确认]\n\n"
        "- [经验]：同内容提案\n"
        "- [经验]：同内容提案\n"
    )
    p = tmp_path / "AGENTS.md"
    p.write_text(content, encoding="utf-8")
    r = _run("review", "--path", str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "同 ID 出现 2 次" in r.stdout


def test_gbk_file_roundtrip(tmp_path):
    """GBK 编码的 AGENTS.md 可读可改，重写后为 UTF-8 且内容一致。"""
    p = tmp_path / "AGENTS.md"
    text = "## [已生效]\n\n- 暂空\n\n## [待确认]\n\n- [经验]：中文提案\n"
    p.write_bytes(text.encode("gbk"))
    r = _run("review", "--path", str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "中文提案" in r.stdout
    r = _run("approve", "1", "--path", str(tmp_path))
    assert r.returncode == 0, r.stderr
    content = p.read_text(encoding="utf-8")  # 重写后应能按 UTF-8 读
    assert "中文提案" in content


def test_storage_read_text_utf8_bom(tmp_path):
    """带 BOM 的 UTF-8 文件读取正常且不残留 BOM 字符。"""
    f = tmp_path / "bom.md"
    f.write_bytes("# 标题\n".encode("utf-8-sig"))
    assert storage.read_text(f) == "# 标题\n"
    assert not storage.read_text(f).startswith("\ufeff")


# ---------- 审查回归：dry-run 纯净 / 代码块空行保留 ----------

def test_reject_dry_run_does_not_archive(tmp_path):
    """reject --dry-run 只预览：不写归档、不改主文件。"""
    p = _write_agents(tmp_path, ["[经验]：提案A"])
    before = p.read_text(encoding="utf-8")
    r = _run("reject", "1", "--dry-run", "--path", str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "-- [经验]：提案A" in r.stdout  # diff 中可见将被删除（带 - 前缀）
    assert p.read_text(encoding="utf-8") == before
    assert not (tmp_path / "docs" / "archived-rules.md").exists()


def test_propose_preserves_code_block_blank_lines(tmp_path):
    """[待确认] 内代码块中的空行在 propose 追加后保留（不误删）。"""
    content = (
        "## [已生效]\n\n- 暂空\n\n"
        "## [待确认]\n\n- [经验]：提案A\n\n"
        "示例：\n\n"
        "```\n\n  保留此行空行\n\n```\n"
    )
    p = tmp_path / "AGENTS.md"
    p.write_text(content, encoding="utf-8")
    r = _run("propose", "-c", "经验", "-r", "提案B", "--write",
             "--path", str(tmp_path))
    assert r.returncode == 0, r.stderr
    out = p.read_text(encoding="utf-8")
    assert "  保留此行空行" in out
    assert "```\n\n  保留此行空行\n\n```" in out  # 代码块内空行原样保留
    assert "提案B" in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
