"""Deterministic, lossless limits for upstream memory extraction requests."""
import hashlib

MAX_MESSAGES = 20
MAX_CHARACTERS = 20000


def split_messages(messages, max_messages=MAX_MESSAGES, max_characters=MAX_CHARACTERS):
    parts, current, size = [], [], 0
    for message in messages:
        content = message["content"]
        for offset in range(0, len(content), max_characters):
            segment = content[offset:offset + max_characters]
            if current and (len(current) >= max_messages or size + len(segment) > max_characters):
                parts.append(current)
                current, size = [], 0
            current.append({**message, "content": segment})
            size += len(segment)
    if current:
        parts.append(current)
    return parts


def part_request_id(user_id, request_id, fingerprint, index):
    identity = "\0".join(("partition-v1", user_id, request_id, fingerprint, str(index)))
    return "ldbd-part:" + hashlib.sha256(identity.encode()).hexdigest()
