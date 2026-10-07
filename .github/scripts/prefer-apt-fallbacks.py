#!/usr/bin/env python3
"""Try the runner's Ubuntu fallback mirrors before its stalled Azure mirror."""

from pathlib import Path
import re
import sys
from urllib.parse import urlsplit


def prefer_fallbacks(text):
    """Retain endpoints, metadata and priority ties while moving Azure last."""
    lines = text.splitlines(keepends=True)
    entries = []
    for index, line in enumerate(lines):
        fields = line.split()
        if not fields or fields[0].startswith('#'):
            continue
        priority = next((int(field.removeprefix('priority:')) for field in fields[1:]
                         if field.startswith('priority:')), float('inf'))
        azure = urlsplit(fields[0]).hostname == 'azure.archive.ubuntu.com'
        entries.append((index, azure, priority))
    if not any(azure for _, azure, _ in entries) or all(azure for _, azure, _ in entries):
        return text
    # Missing priority is tried last by apt. Rank groups explicitly so an
    # implicit fallback still precedes Azure, without changing ties or order.
    ranks = {group: rank for rank, group in enumerate(
        sorted({(azure, priority) for _, azure, priority in entries}), start=1)}
    for index, azure, priority in entries:
        line = lines[index]
        tag = f'priority:{ranks[azure, priority]}'
        if re.search(r'(?<!\S)priority:\d+', line):
            lines[index] = re.sub(r'(?<!\S)priority:\d+', tag, line)
        else:
            body = line.rstrip('\r\n')
            lines[index] = body + '\t' + tag + line[len(body):]
    return ''.join(lines)


def main():
    """Update only an existing runner mirror list, logging its before/after state."""
    path = Path(sys.argv[1] if len(sys.argv) > 1 else '/etc/apt/apt-mirrors.txt')
    if not path.is_file():
        print(f'APT mirror list absent; unchanged: {path}')
        return
    before = path.read_bytes().decode('utf-8')
    after = prefer_fallbacks(before)
    print('APT mirrors before:\n' + before)
    if after != before:
        path.write_bytes(after.encode('utf-8'))
    print('APT mirrors after:\n' + after)


if __name__ == '__main__':
    main()
