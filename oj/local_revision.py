"""Literal revisions against one immutable, hash-pinned local source snapshot."""
import hashlib
import json

from pydantic import BaseModel, ConfigDict, StrictStr


MAX_EDITS = 128


class LiteralEdit(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, hide_input_in_errors=True)

    old: StrictStr
    new: StrictStr


def apply_edits(source: bytes, expected_base_sha256: str, edits: list[LiteralEdit],
                max_source_bytes: int) -> str:
    """Resolve every match on the original bytes, then assemble once.

    Array order determines error indices, never changes the matching snapshot.
    Searching from start+1 detects overlapping occurrences within a single old.
    Replacements cannot supply or erase a later edit's match.
    """
    if (not isinstance(expected_base_sha256, str) or len(expected_base_sha256) != 64
            or any(c not in '0123456789abcdefABCDEF' for c in expected_base_sha256)):
        raise ValueError('expected_base_sha256 must contain exactly 64 hexadecimal characters')
    actual = hashlib.sha256(source).hexdigest()
    expected = expected_base_sha256.lower()
    if actual != expected:
        raise ValueError(json.dumps(dict(error='base_sha256_mismatch',
            actual_base_sha256=actual, expected_base_sha256=expected,
            source_bytes=len(source)), separators=(',', ':')))
    if len(source) > max_source_bytes:
        raise ValueError('Base source too large')
    if not 1 <= len(edits) <= MAX_EDITS:
        raise ValueError(f'edits must contain 1..{MAX_EDITS} literal edits')
    spans, payload_bytes, final_bytes = [], 0, len(source)
    for index, edit in enumerate(edits):
        old, new = edit.old.encode('utf-8'), edit.new.encode('utf-8')
        if not old:
            raise ValueError(f'edits[{index}].old must not be empty')
        payload_bytes += len(old) + len(new)
        if payload_bytes > 2 * max_source_bytes:
            raise ValueError('Literal edit payload too large')
        start = source.find(old)
        if start < 0 or source.find(old, start + 1) >= 0:
            raise ValueError(f'edits[{index}].old must occur exactly once in the base source')
        end = start + len(old)
        for other_start, other_end, _, other_index in spans:
            if start < other_end and other_start < end:
                raise ValueError(f'edits[{index}] overlaps edits[{other_index}] in the base source')
        spans.append((start, end, new, index))
        final_bytes += len(new) - len(old)
    if final_bytes > max_source_bytes:
        raise ValueError('Revised source too large')
    chunks, offset = [], 0
    for start, end, new, _ in sorted(spans):
        chunks.extend((source[offset:start], new))
        offset = end
    chunks.append(source[offset:])
    return b''.join(chunks).decode('utf-8')
