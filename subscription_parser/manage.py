"""Local administration commands; credentials are never exposed through HTTP."""
import argparse
import json
import os
from pathlib import Path

from . import common
from .config import admin_key
from .storage import Store
from .auth import Auth
from .debug_log import DebugLog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['admin-key', 'migrate', 'check', 'reset-two-factor'])
    parser.add_argument('--confirm', action='store_true', help='confirm a local two-factor reset and session revocation')
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ.get('PARSER_DATA_DIR', '/data')))
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == 'admin-key':
            print(admin_key())
            return
        store = Store(args.data_dir)
        if args.command == 'reset-two-factor':
            if not args.confirm:
                raise common.SubscriptionError('reset requires --confirm; stop the app first, then enroll an authenticator again')
            with common.process_lock(store.directory / 'app.lock'):
                Auth(store, admin_key()).reset_factor()
                DebugLog(args.data_dir).write('auth_reset')
            print('Two-factor authentication reset. All sessions revoked. Sign in and enroll an authenticator again.')
            return
        with store.connection() as connection:
            if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or connection.execute('PRAGMA foreign_key_check').fetchall():
                raise common.SubscriptionError('database consistency check failed')
        print(json.dumps({'database': str(store.database_path), 'status': 'ok',
                          'hwids': len(store.hwids()), 'providers': len(store.config()['providers'])}))
    except common.SubscriptionError as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
