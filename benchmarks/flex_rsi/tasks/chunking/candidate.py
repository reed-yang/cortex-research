"""Deliberately simple original starter; stdin request -> stdout response.

Only this file is editable. Offsets are UTF-8 byte offsets in the input.
"""
import json
import re
import sys


def chunk(document, max_bytes):
    raw = document.encode('utf-8')
    heads = list(re.finditer(rb'^## +([^\n]+)\n', raw, re.M))
    result = []
    for i, head in enumerate(heads):
        section = head[1].decode().strip()
        if section.casefold() in ('references', 'bibliography'):
            continue
        start = head.end()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(raw)
        # Starter leaves oversized bodies intact and does not parse fences.
        if raw[start:end].strip():
            result.append(dict(id=section, section=section, start=start, end=end))
    return result


if __name__ == '__main__':
    request = json.load(sys.stdin)
    print(json.dumps(chunk(request['document'], request['max_bytes'])))
