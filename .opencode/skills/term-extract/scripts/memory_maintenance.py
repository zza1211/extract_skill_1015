"""本地服务存活期间的轻量维护；不调用模型，不修改规则正文或发布集合。"""
from __future__ import annotations

import threading


def start(root, interval_seconds=60):
    stop = threading.Event()

    def maintain():
        from evolution_store import scan_conflicts
        while not stop.is_set():
            try:
                # 口径/候选变化立即检查，否则默认每七天一次。
                scan_conflicts(root, force=False)
            except (OSError, ValueError, TypeError, KeyError):
                # 历史记录不完整时不终止HTTP服务；手动扫描会返回具体错误。
                pass
            stop.wait(interval_seconds)

    worker = threading.Thread(target=maintain, name="term-memory-maintenance", daemon=True)
    worker.start()
    return stop
