"""Read-only projection of the local dispatch queue and existing worker messages.

Remote OS locks expose no FIFO position. Do not infer one from submission age,
recent listings, cache keys, or completed evaluation timing metadata.
"""
import re

from .store import TERMINAL


def queue_fields(row, ahead_count='unknown'):
    status = row['status']
    message = row['result'].get('message', '')
    if status == 'queued':
        return dict(queue_stage='worker_slot', ahead_count=ahead_count)
    if status in TERMINAL:
        return dict(queue_stage='finished', ahead_count=0)
    stage = 'unknown'
    # Reuse only coordinator-authored status messages, paired with their status.
    # Changed/older messages fall back to unknown without altering the protocol.
    if status == 'compiling' and isinstance(message, str):
        if re.fullmatch(r'等待同缓存键编译，最多 [0-9]+ 秒；等待不占编译预算或 NPU', message):
            stage = 'compile_cache_single_flight'
        elif (message.startswith('独立编译 kernel.asc')
              or re.fullmatch(r'CPU 编译中，墙钟硬上限 [0-9]+ 秒；未占用 NPU', message)
              or message == '编译缓存命中'):
            stage = 'compiling'
    elif status == 'waiting' and message == '编译和数据已准备好，等待独占 NPU':
        stage = 'npu'
    elif status in {'connecting', 'generating', 'running'}:
        stage = status
    return dict(queue_stage=stage, ahead_count='unknown')
