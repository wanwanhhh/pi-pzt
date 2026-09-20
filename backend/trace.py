"""位置曲线的环形缓冲：遥测轮询线程往里写，HTTP 只读。

**不另开设备轮询**：设备查询约 32 ms/条（DLL 通道上限约 31 条/秒），
多一个轮询者就是从真正在动的那件事上抢带宽（server.Telemetry 的注释：唯一的状态轮询者）。
曲线与界面读数因此永远同源 —— 内存里的一份样本，两处显示。

按**时间**裁剪而不是按条数：扫描中遥测会从 10 Hz 降到 2 Hz，
按条数裁的话缓冲会在降频时莫名其妙地只剩几秒。
"""
from __future__ import annotations

import threading
from collections import deque

# 一条样本：(时刻 time.time()，位置 µm，目标 µm)
Sample = tuple[float, float, float]


class TraceBuffer:
    """保留最近 span_s 秒的 (时刻, 位置, 目标)。"""

    def __init__(self, span_s: float) -> None:
        self.span_s = span_s
        self._items: deque[Sample] = deque()
        # 锁在**类里面**，不外包给调用者：读窗口是 for 迭代这个 deque，
        # 写侧 append/popleft，两者不互斥时 CPython 会抛
        # RuntimeError: deque mutated during iteration（实测约 0.01%/次读）。
        # 把这个契约留在类里，调用者怎么写都不会踩。
        self._lock = threading.Lock()

    def add(self, ts: float, position: float, target: float) -> None:
        with self._lock:
            self._items.append((ts, position, target))
            cutoff = ts - self.span_s
            while self._items and self._items[0][0] < cutoff:
                self._items.popleft()

    def window(self, from_ts: float, to_ts: float) -> list[Sample]:
        """闭区间 [from_ts, to_ts]。

        两端都用绝对时刻：记录结束后前端还会再取一次，那次可能迟到一分钟
        （浏览器把后台标签的定时器压到约 1 次/分钟），有了 to 这个上界，
        迟到的取数拿到的仍是原来那一段，不会把后面的样本也吞进来。
        from_ts > to_ts 时返回空 —— 轮询线程卡住、锚点比样本还新就是这种情况，
        不该报错。
        """
        with self._lock:
            return [it for it in self._items if from_ts <= it[0] <= to_ts]

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
