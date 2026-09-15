"""Fixed, network-free publisher child. Receives only a parent-prepared manifest."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from .files import PublicationConflict, digest, directory, exclusive_rename, inventory, parts, read_file, update_file
from .sandbox import apply_profile, publisher_profile, verify_no_delete


def publish(task: dict) -> dict:
    root, stage = Path(task['root']), Path(task['stage'])
    if len(parts(task['paper_dir'])) != 1:
        raise PublicationConflict('invalid_paper_directory')
    for name, value in task['files'].items():
        parts(name)
        if Path(name).name.casefold() == 'notes.md' and value['action'] == 'update':
            raise PublicationConflict('notes_update_forbidden')
    target = root / task['paper_dir']
    creates = [root / name for name in task['creates']]
    updates = [target / name for name, value in task['files'].items() if value['action'] == 'update']
    apply_profile(publisher_profile(root, stage, creates, updates))
    verify_no_delete(root)
    with directory(root) as root_fd:
        identity = os.fstat(root_fd)
        if [identity.st_dev, identity.st_ino] != task['root_identity']:
            raise PublicationConflict('root_identity_changed')
        if task['new_directory']:
            if not target.exists():
                if inventory(stage / 'new') != {name: value['new'] for name, value in task['files'].items()}:
                    raise PublicationConflict('staged_content_changed')
                exclusive_rename(stage / 'new', target)
            for name, value in task['files'].items():
                if digest(read_file(target, name)) != value['new']:
                    raise PublicationConflict('new_directory_conflict')
        else:
            with directory(target) as target_fd:
                identity = os.fstat(target_fd)
                if [identity.st_dev, identity.st_ino] != task['target_identity']:
                    raise PublicationConflict('paper_identity_changed')
            for relative in task['directories']:
                path = target / relative
                if not path.exists():
                    for name, value in task['files'].items():
                        if name.startswith(relative + '/'):
                            if digest(read_file(stage / 'directories', name)) != value['new']:
                                raise PublicationConflict('staged_content_changed')
                    exclusive_rename(stage / 'directories' / relative, path)
            for name, value in task['files'].items():
                action = value['action']
                if action == 'preserve':
                    continue
                try:
                    current = digest(read_file(target, name))
                except FileNotFoundError:
                    current = None
                if current == value['new']:
                    continue
                if action == 'create' and current is None:
                    if digest(read_file(stage / 'new', name)) != value['new']:
                        raise PublicationConflict('staged_content_changed')
                    exclusive_rename(stage / 'new' / name, target / name)
                elif action == 'update' and current == value['old']:
                    data = read_file(stage / 'new', name)
                    if digest(data) != value['new']:
                        raise PublicationConflict('staged_content_changed')
                    update_file(target, name, value['old'], data)
                else:
                    # Unknown partial writes or external edits require reconciliation.
                    # Both complete versions remain available outside the library.
                    raise PublicationConflict('content_conflict')
                if digest(read_file(target, name)) != value['new']:
                    raise PublicationConflict('publication_verification_failed')
        if [root.stat().st_dev, root.stat().st_ino] != task['root_identity']:
            raise PublicationConflict('root_identity_changed')
    with directory(target) as fd:
        info = os.fstat(fd)
    return {'state': 'published', 'target_identity': [info.st_dev, info.st_ino]}


def main() -> int:
    try:
        task = json.load(sys.stdin)
        result = publish(task)
    except (OSError, ValueError) as error:
        result = {'state': 'conflict' if isinstance(error, PublicationConflict) else 'failed',
                  'failure': str(error) if isinstance(error, PublicationConflict) else type(error).__name__}
    print(json.dumps(result))
    return 0 if result['state'] == 'published' else 1


if __name__ == '__main__':
    raise SystemExit(main())
