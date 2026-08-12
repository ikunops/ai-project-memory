"""sculptor — 项目记忆雕刻师的健壮内核。

提供三个不变量：
  P0-1 稳定身份：提案以内容派生的 P-xxxx ID 引用，不再依赖易漂移的行号。
  P0-2 事务写入：文件锁 + 临时文件 fsync + os.replace 原子替换 + 写前 hash 校验。
  P0-3 状态机解析：代码块内的 '## [xxx]' 不被误判为区块边界。

零三方依赖，可在任意 Python 3.8+ 环境直接运行。
"""

__all__ = ["identity", "storage", "parser"]
