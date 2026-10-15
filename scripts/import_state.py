#!/usr/bin/env python3
"""Restore SQLite or legacy application data into an empty Docker data volume."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tarfile
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from subscription_parser import common
from subscription_parser.storage import SCHEMA_VERSION


def import_state(source: Path, destination: Path, uid=10001, gid=10001):
    source, destination = source.resolve(), destination.resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise common.SubscriptionError('source and destination must be separate directories')
    database = source / 'app.sqlite3'
    env_path = source / '.env'
    paths = []
    if database.exists() or database.is_symlink():
        if not database.is_file() or database.is_symlink():
            raise common.SubscriptionError('source database must be a regular file')
        for suffix in ('-wal', '-journal'):
            journal = source / ('app.sqlite3' + suffix)
            if journal.exists() and journal.stat().st_size:
                raise common.SubscriptionError('backup contains a SQLite journal; take a consistent backup first')
        try:
            connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
            try:
                if connection.execute('PRAGMA user_version').fetchone()[0] not in {1, 2, 3, SCHEMA_VERSION}:
                    raise common.SubscriptionError('unsupported backup database version')
                if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or connection.execute('PRAGMA foreign_key_check').fetchall():
                    raise common.SubscriptionError('backup database is inconsistent')
            finally:
                connection.close()
        except sqlite3.DatabaseError:
            raise common.SubscriptionError('backup database is invalid') from None
        paths.append(database)
    elif not env_path.is_file():
        raise common.SubscriptionError('source must contain app.sqlite3 or a legacy .env file')
    if env_path.exists() or env_path.is_symlink():
        if not env_path.is_file() or env_path.is_symlink():
            raise common.SubscriptionError('source must contain a regular .env file')
        common.load_environment(env_path, inherit_process=False)
        paths.append(env_path)
    state = source / '.parser-state'
    if state.is_symlink():
        raise common.SubscriptionError('source state directory must not be a symlink')
    for filename in ('providers.json', 'snapshot.json'):
        path = state / filename
        if path.exists() or path.is_symlink():
            if not path.is_file() or path.is_symlink():
                raise common.SubscriptionError('saved state must contain regular files')
            try:
                json.loads(common.read_limited(path))
            except ValueError:
                raise common.SubscriptionError('saved state contains invalid JSON') from None
            paths.append(path)
    cache = state / 'cache'
    if cache.is_symlink():
        raise common.SubscriptionError('cache directory must not be a symlink')
    if cache.is_dir():
        for path in cache.glob('*.json'):
            if path.is_symlink() or not path.is_file():
                raise common.SubscriptionError('cache must contain regular files')
            paths.append(path)
    archive = source / 'legacy-backup'
    if archive.is_symlink():
        raise common.SubscriptionError('legacy backup must not be a symlink')
    for name in ('environment.txt', 'providers.json'):
        path = archive / name
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file():
                raise common.SubscriptionError('legacy backup must contain regular files')
            paths.append(path)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (destination / '.parser-state').is_symlink():
        raise common.SubscriptionError('destination state directory must not be a symlink')
    with common.process_lock(destination / '.parser-state' / 'app.lock'):
        if any((destination / name).exists() or (destination / name).is_symlink() for name in ('.env', 'app.sqlite3', 'legacy-backup')) or any(
                path.name != 'app.lock' for path in (destination / '.parser-state').iterdir()):
            raise common.SubscriptionError('destination already contains application data; import refused')
        with tempfile.TemporaryDirectory(prefix='.import-', dir=destination) as temporary:
            staging = Path(temporary)
            for path in paths:
                target = staging / path.relative_to(source)
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                target.chmod(0o600)
            # Never copy the source lock: the destination keeps its own lock inode.
            for path in sorted(staging.rglob('*')):
                target = destination / path.relative_to(staging)
                if path.is_dir():
                    target.mkdir(mode=0o700, exist_ok=True)
                else:
                    os.replace(path, target)
        destination.chmod(0o700)
        if os.geteuid() == 0:
            for path in [destination, *destination.rglob('*')]:
                if not path.is_symlink():
                    os.chown(path, uid, gid)
    return len(paths)


def import_archive(stream, destination):
    """Read a host-created archive through stdin without privileged bind mounts."""
    with tempfile.TemporaryDirectory(prefix='parser-import-') as temporary:
        source = Path(temporary)
        size = 0
        with tarfile.open(fileobj=stream, mode='r|gz') as archive:
            for index, member in enumerate(archive):
                path = Path(member.name)
                if index >= 1000 or path.is_absolute() or '..' in path.parts or member.issym() or member.islnk():
                    raise common.SubscriptionError('invalid state archive member')
                if path.as_posix() in {'app.sqlite3-wal', 'app.sqlite3-journal'} and member.size:
                    raise common.SubscriptionError('backup contains a SQLite journal; take a consistent backup first')
                selected = path.as_posix() in {'.env', 'app.sqlite3', '.parser-state/providers.json', '.parser-state/snapshot.json',
                                               'legacy-backup/environment.txt', 'legacy-backup/providers.json'} or (
                    path.parent.as_posix() == '.parser-state/cache' and path.suffix == '.json')
                if not selected:
                    continue
                if not member.isfile() or member.size > common.MAX_BYTES:
                    raise common.SubscriptionError('invalid or oversized state archive file')
                size += member.size
                if size > 512 * 1024 * 1024:
                    raise common.SubscriptionError('state archive exceeds the size limit')
                target = source / path
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with archive.extractfile(member) as input_file, target.open('xb') as output:
                    shutil.copyfileobj(input_file, output)
                target.chmod(0o600)
        return import_state(source, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', help='source directory, or - for a tar.gz archive on stdin')
    parser.add_argument('--destination', type=Path, default=Path(os.environ.get('PARSER_DATA_DIR', '/data')))
    args = parser.parse_args()
    os.umask(0o077)
    try:
        count = import_archive(sys.stdin.buffer, args.destination) if args.source == '-' else import_state(Path(args.source), args.destination)
    except (common.SubscriptionError, OSError, tarfile.TarError) as error:
        parser.exit(1, (str(error) if isinstance(error, common.SubscriptionError) else 'import failed; check file permissions') + '\n')
    print(f'Imported {count} private files; existing HWIDs and application keys are preserved.')


if __name__ == '__main__':
    main()
