"""REST 接口的数据模型。

行程范围只有连上设备才知道，所以这里只做类型与有限性校验。
对着软限位的校验在 scanner.start()（扫描起终点）和 pi_stage.clamp()（手动移动）里做。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .config import DEFAULT_SETTLE_MS, MAX_VELOCITY, SETTLE_MS_RANGE


class _Model(BaseModel):
    # NaN / inf 会让限位夹取失效，必须在入口挡掉
    model_config = ConfigDict(allow_inf_nan=False)


class MoveRequest(_Model):
    target_um: float


class JogRequest(_Model):
    delta_um: float


class ServoRequest(_Model):
    on: bool


class VelocityRequest(_Model):
    velocity: float = Field(gt=0, le=MAX_VELOCITY)


class ScanRequest(_Model):
    name: str = Field(default="", max_length=200)
    start_um: float
    stop_um: float
    count: int = Field(ge=2, le=100_000)
    settle_ms: int = Field(
        default=DEFAULT_SETTLE_MS, ge=SETTLE_MS_RANGE[0], le=SETTLE_MS_RANGE[1]
    )


class ScanControl(_Model):
    """暂停 / 继续 / 中止三个独立动作，一次只做一个。"""

    action: str = Field(pattern="^(pause|resume|abort)$")
