import random
import time
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass
class HumorConfig:
    humor_rate: float = 0.2
    min_gap_seconds: int = 180
    min_length: int = 6
    max_length: int = 600
    # Leave empty to disable keyword blocking entirely.
    block_keywords: Tuple[str, ...] = ()


def should_add_humor(
    user_text: str,
    last_humor_ts: Optional[float],
    cfg: HumorConfig,
) -> bool:
    text = user_text.lower().strip()
    if not text:
        return False
    if len(text) < cfg.min_length or len(text) > cfg.max_length:
        return False
    if cfg.block_keywords and any(k in text for k in cfg.block_keywords):
        return False
    if last_humor_ts and (time.time() - last_humor_ts) < cfg.min_gap_seconds:
        return False
    return random.random() < cfg.humor_rate

