# ai-project-memory（项目记忆雕刻师 Skill）

把对话中出现的"采纳/纠正信号"沉淀为项目 AGENTS.md 的结构化记忆，供 AI 网关 Tier2 缓存与后续会话复用。与 [ai-agent-gateway](https://github.com/ikunops/ai-agent-gateway) 是两个独立项目：网关读记忆，本 Skill 写记忆。

## 安装

在 opencode 配置（`~/.config/opencode/opencode.jsonc`）中指向本仓库：

```jsonc
{
  "skills": {
    "paths": ["C:\\Users\\31807\\PycharmProjects\\ai-project-memory"]
  }
}
```

或复制 `project-memory-sculptor` 目录到 opencode skills 目录：

```
~/.config/opencode/skills/project-memory-sculptor/
```

重启 opencode 后生效。

## 使用

```bash
# 检查记忆库状态（区块结构/条数）
python project-memory-sculptor/scripts/sculpt.py status

# 生成格式化提案（打印，不写入）
python project-memory-sculptor/scripts/sculpt.py propose -c 技术选型 -r "计算密集任务优先考虑 Rust" -e "用户要求低内存" -s "仅限低内存场景"

# 列出全部 [待确认] 提案
python project-memory-sculptor/scripts/sculpt.py review

# 审查：提升第 2 条到 [已生效]
python project-memory-sculptor/scripts/sculpt.py approve 2

# 审查：否决删除第 1 条
python project-memory-sculptor/scripts/sculpt.py reject 1

# 指定目标项目
python project-memory-sculptor/scripts/sculpt.py status --path /path/to/project
```

## 核心机制

- **只追加 [待确认]**：Skill 永不直接修改 [已生效] 区块，人工审查后提升
- **反过拟合三原则**：记录决策上下文（为什么+使用条件）、被否决方案追加 [反例]、显式适用范围
- **状态优先**：任何操作先只读侦察 AGENTS.md 现状，不猜测
- **网关联动**：AGENTS.md 变化后网关 Tier2 缓存按文件哈希自动失效重读

## 结构

```
├── README.md
├── project-memory-sculptor/     # skill 本体（文件夹名 = skill 名）
│   ├── SKILL.md                      # skill 指令
│   ├── scripts/sculpt.py             # CLI 工具（status/propose/review/approve/reject）
│   ├── templates/AGENTS.template.md  # 标准 AGENTS.md 模板
│   ├── examples/proposals.md         # 正确/错误提案对照 + 自检清单
│   └── tests/test_sculpt.py          # 测试（4 个）
```

## 测试

```bash
python -m pytest project-memory-sculptor/tests -q
```
