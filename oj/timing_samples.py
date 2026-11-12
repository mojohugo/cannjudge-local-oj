"""Bounded, read-only access to independently replayed LOCAL timing samples."""
import json
import math
import os
from pathlib import Path
import re
import stat
import statistics

from remote.launch_metrics import metric_digest

SIDECAR = 'timing-samples-v1.json'
MAX_BYTES = 2 * 1024 * 1024
PHASES = ('prevalidation', 'warmup', 'measured')


def phase_summary(entry):
    if not isinstance(entry, dict):
        return None
    values = entry.get('task_sums_us')
    indices = entry.get('logical_launch_indices')
    counts = entry.get('task_counts')
    if (not isinstance(values, list) or not values or len(values) > 10000
            or not isinstance(indices, list) or not isinstance(counts, list)
            or len(values) != len(indices) or len(values) != len(counts)
            or any(type(v) not in (float, int) or not math.isfinite(v) or v <= 0 for v in values)
            or any(type(i) is not int or i < 0 for i in indices)
            or indices != sorted(set(indices))
            or any(type(n) is not int or n < 1 for n in counts)):
        return None
    return dict(task_sums_us=values, logical_launch_indices=indices, task_counts=counts,
                sample_count=len(values), min_us=min(values), max_us=max(values),
                spread_us=max(values) - min(values))


def validated_samples(phases, metric):
    """Cross-check stored phase lists against the original immutable measurements."""
    if not isinstance(phases, dict) or not isinstance(metric, dict):
        return None
    out = {phase: phase_summary(phases.get(phase)) for phase in PHASES}
    measured = out['measured']
    steady = metric.get('phase_metrics', {}).get('steady_state', {})
    if (measured is None or steady.get('available') is not True or steady.get('phase') != 'measured'
            or measured['sample_count'] != steady.get('invocations')
            or sum(measured['task_counts']) != steady.get('kernel_count')):
        return None
    try:
        for field, computed in [('mean_us', statistics.fmean(measured['task_sums_us'])),
                                ('median_us', statistics.median(measured['task_sums_us']))]:
            saved = steady[field]
            if type(saved) not in (int, float) or not math.isfinite(saved) or not math.isclose(
                    computed, saved, rel_tol=1e-12, abs_tol=1e-6):
                return None
        first = metric.get('phase_metrics', {}).get('first_invocation', {})
        pre = out['prevalidation']
        if pre is not None and (first.get('available') is not True or first.get('phase') != 'prevalidation'
                or pre['logical_launch_indices'][0] != first.get('logical_launch_index')
                or pre['task_counts'][0] != first.get('kernel_count')
                or not math.isclose(pre['task_sums_us'][0], first['value_us'], rel_tol=1e-12, abs_tol=1e-6)):
            return None
        all_indices = [i for phase in PHASES if out[phase] for i in out[phase]['logical_launch_indices']]
        if all_indices != sorted(set(all_indices)):
            return None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return out


def load_samples(row, state_dir):
    """No SSH, SDK, writes or fallback from averages. Missing evidence stays null."""
    if state_dir is None or not re.fullmatch('[0-9a-f]{32}', str(row.get('id', ''))):
        return {}
    directory = Path(state_dir) / 'submissions' / row['id']
    path = directory / SIDECAR
    try:
        # Do not follow a sidecar or submission-directory symlink.
        if directory.is_symlink():
            return {}
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                return {}
            data = handle.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            return {}
        saved = json.loads(data)
        result = row['result']
        if (saved.get('schema_version') != 1 or saved.get('submission_id') != row['id']
                or saved.get('source_sha256') != row.get('sha256')
                or row.get('sha256') != result.get('source_sha256')
                or saved.get('result_sha256') != metric_digest(result)):
            return {}
        group = result.get('metrics', {}).get('PipeUtilization', {})
        protocol = group.get('measurement_protocol', {})
        if (group.get('status') != 'available' or protocol.get('exact') is not True
                or protocol.get('invocation_mapping') != 'recorded_device_launch_counts'
                or protocol.get('task_order') != 'single_stream_decimal_start_time'):
            return {}
        cases = saved.get('cases', {})
        return {m['index']: validated_samples(cases.get(str(m['index'])), m) for m in group.get('cases', [])}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return {}
