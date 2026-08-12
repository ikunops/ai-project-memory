import os
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCULPT = SKILL_ROOT / "scripts" / "sculpt.py"


def run(*args, cwd=None):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(SCULPT), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        encoding="utf-8",
        env=env,
    )


def test_propose_format():
    r = run("propose", "-c", "选型", "-r", "规则X", "-e", "证据Y", "-s", "条件Z")
    assert r.returncode == 0
    out = r.stdout
    assert "[选型]" in out and "证据" in out and "条件" in out


def test_propose_requires_rule():
    r = run("propose", "-c", "选型")
    assert r.returncode != 0


def test_full_flow(tmp_path):
    agents = tmp_path / "AGENTS.md"
    agents.write_text(
        "## [已生效]\n\n- 暂空\n\n## [待确认]\n\n- [经验]：待审提案（证据：测试）\n",
        encoding="utf-8",
    )
    r = run("review", "--path", str(tmp_path))
    assert "待审提案" in r.stdout
    r = run("approve", "1", "--path", str(tmp_path))
    assert r.returncode == 0
    content = agents.read_text(encoding="utf-8")
    assert "待审提案" in content and "[已生效]" in content
    active_idx = content.index("[已生效]")
    waiting_idx = content.index("[待确认]")
    assert active_idx < waiting_idx, "已生效必须在待确认之前"
    assert "待审提案" in content[active_idx:waiting_idx], "提案必须提升到已生效区块内"
    r = run("status", "--path", str(tmp_path))
    assert "1 条" in r.stdout


def test_reject(tmp_path):
    agents = tmp_path / "AGENTS.md"
    agents.write_text(
        "## [已生效]\n\n- 暂空\n\n## [待确认]\n\n- [经验]：坏提案\n",
        encoding="utf-8",
    )
    r = run("reject", "1", "--path", str(tmp_path))
    assert r.returncode == 0
    assert "坏提案" not in agents.read_text(encoding="utf-8")
