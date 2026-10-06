"""测试包初始化。

并发/恢复用例会在同一进程内创建多个指向同一日志的 EventStore（模拟独立进程），
其锁文件句柄在进程退出时统一回收；屏蔽该场景下预期的 ResourceWarning。
"""

import warnings

warnings.filterwarnings("ignore", message="unclosed file", category=ResourceWarning)
