#!/usr/bin/env python3
"""Exercise the dashboard with synthetic providers; never change live accounts."""
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import uvicorn
from playwright.sync_api import sync_playwright, expect
from subscription_parser import common
from subscription_parser.app import create_app
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


def main():
    os.umask(0o077)
    checks = []
    with tempfile.TemporaryDirectory(prefix="parser-browser-") as temporary:
        root = Path(temporary)
        seen = []
        def fetch(url, hwid, user_agent, timeout):
            seen.append(hwid)
            return json.dumps({'outbounds': [
                {'type': 'vless', 'tag': 'Berlin', 'server': 'example.org', 'server_port': 443,
                 'uuid': '01234567-89ab-4cde-8fab-0123456789ab'},
                {'type': 'shadowsocks', 'tag': 'Tokyo', 'server': 'example.net', 'server_port': 443,
                 'method': 'aes-128-gcm', 'password': 'synthetic-password'}]}).encode(), {'fetched_at': common.now()}
        admin = 'synthetic-browser-admin-key'
        with patch.dict(os.environ, {'PARSER_ADMIN_TOKEN': admin}):
            app = create_app(root, start_scheduler=False, fetcher=fetch)
        manager = app.state.manager
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, access_log=False, log_level='error'))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(.05)
        if not server.started:
            raise RuntimeError('test server did not start')
        try:
            with sync_playwright() as browser_api:
                options = {'headless': True, 'args': ['--no-sandbox']}
                if os.environ.get('PARSER_TEST_BROWSER'):
                    options['executable_path'] = os.environ['PARSER_TEST_BROWSER']
                browser = browser_api.chromium.launch(**options)
                context = browser.new_context(viewport={'width':1440, 'height':1000}, permissions=['clipboard-read', 'clipboard-write'])
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{port}')
                page.locator('#admin-key').fill('wrong-admin-key')
                page.locator('#login-form button').click()
                expect(page.locator('#login-error')).to_contain_text('incorrect')
                page.locator('#admin-key').fill(admin)
                page.locator('#login-form button').click()
                expect(page.locator('#workspace')).to_be_visible()
                checks.append('admin login and incorrect-key handling')

                page.locator('[data-page=devices]').click()
                page.locator('#add-hwid').click()
                page.locator('#hwid-name').fill('Browser device')
                page.locator('#hwid-value').fill('saved-browser-hwid')
                page.locator('#hwid-form button[type=submit]').click()
                expect(page.locator('#hwid-dialog')).not_to_be_visible()
                expect(page.locator('#hwid-rows')).to_contain_text('Browser device')
                checks.append('save a fixed HWID without exposing its value')

                page.locator('#add-hwid').click()
                page.locator('#hwid-name').fill('Second device')
                page.locator('#hwid-value').fill('second-browser-hwid')
                page.locator('#hwid-form button[type=submit]').click()
                expect(page.locator('#hwid-dialog')).not_to_be_visible()

                page.locator('#add-provider').click()
                page.locator('#provider-name').fill('Demo')
                page.locator('#provider-url').fill('https://one.example/sub\nhttps://two.example/sub')
                expect(page.locator('#assignment-0')).to_have_value('')
                expect(page.locator('#assignment-1')).to_have_value('')
                expect(page.locator('#save-provider')).to_be_disabled()
                identities={item['name']:item['id'] for item in manager.store.hwids()}
                page.locator('#assignment-0').select_option(identities['Browser device'])
                page.locator('#assignment-1').select_option(identities['Second device'])
                page.locator('#test-provider').click()
                expect(page.locator('#provider-test-result')).to_contain_text('Link 2: fetch passed',timeout=15000)
                assert manager.store.config()['providers']==[]
                assert seen==['saved-browser-hwid','second-browser-hwid']
                page.locator('#save-provider').click()
                expect(page.locator('#provider-dialog')).not_to_be_visible()
                page.locator('[data-page=overview]').click()
                expect(page.locator('#provider-rows tr')).to_have_count(2)
                expect(page.locator('#stat-servers')).to_have_text('2', timeout=15000)
                assert seen==['saved-browser-hwid','second-browser-hwid']
                checks.append('explicit per-link devices, test-before-save and reuse of tested responses')

                page.locator('[data-page=devices]').click()
                first=page.locator('[data-device="'+identities['Browser device']+'"]')
                second=page.locator('[data-device="'+identities['Second device']+'"]')
                expect(first).to_contain_text('Demo 1')
                expect(second).to_contain_text('Demo 2')
                expect(first.locator('[data-device-action=delete]')).to_be_disabled()
                first.locator('[data-device-action=rename]').click()
                page.locator('#device-new-name').fill('My renamed device')
                page.locator('#device-rename-form button[type=submit]').click()
                expect(page.locator('#device-rename-dialog')).not_to_be_visible()
                expect(first).to_contain_text('My renamed device')
                assert manager.store.hwid_value(identities['Browser device'])=='saved-browser-hwid'
                first.locator('[data-device-action=reveal]').click()
                expect(page.locator('#device-revealed-value')).to_have_value('saved-browser-hwid')
                page.locator('#copy-revealed-device').click()
                expect(page.locator('#toast')).to_contain_text('copied')
                assert page.evaluate('navigator.clipboard.readText()')=='saved-browser-hwid'
                page.locator('#device-reveal-dialog [data-close]').first.click()
                expect(page.locator('#device-revealed-value')).to_have_value('')
                checks.append('device usage, rename without ID changes and private reveal/copy/hide')

                first.locator('[data-device-action=replace]').click()
                expect(page.locator('#device-replace-usage')).to_contain_text('Demo 1')
                expect(page.locator('#device-replace-usage')).not_to_contain_text('Demo 2')
                expect(page.locator('#device-replace-confirm')).not_to_be_checked()
                page.locator('#device-replacement-value').fill('replacement-browser-hwid')
                page.locator('#device-replace-confirm').check()
                page.locator('#device-replace-form button[type=submit]').click()
                expect(page.locator('#device-replace-dialog')).not_to_be_visible()
                assert manager.store.hwid_value(identities['Browser device'])=='replacement-browser-hwid'
                assert len(seen)==2
                checks.append('explicit replacement lists affected subscriptions and makes no provider request')

                if os.environ.get('PARSER_TEST_SCREENSHOTS'):
                    page.screenshot(path=os.environ['PARSER_TEST_SCREENSHOTS']+'/devices-desktop.png',full_page=True)

                page.locator('#add-provider').click()
                page.locator('#provider-name').fill('No device provider')
                page.locator('#provider-url').fill('https://no-device.example/sub')
                expect(page.locator('#provider-hwid')).to_have_value('')
                page.locator('#provider-hwid').select_option('__none__')
                page.locator('#test-provider').click()
                expect(page.locator('#provider-test-result')).to_contain_text('fetch passed',timeout=15000)
                page.locator('#save-provider').click()
                expect(page.locator('#provider-dialog')).not_to_be_visible()
                assert seen[-1] is None and len(seen)==3
                page.locator('[data-page=providers]').click()
                no_device=page.locator('#provider-rows tr').filter(has_text='No device provider')
                expect(no_device).to_contain_text('No device ID')
                page.once('dialog',lambda dialog:dialog.accept())
                no_device.locator('[data-action=delete]').click()
                expect(page.locator('#provider-rows tr')).to_have_count(2)
                checks.append('explicit no-device subscription and removal')

                page.locator('[data-page=servers]').click()
                page.locator('#protocol-filter').select_option('shadowsocks')
                expect(page.locator('#server-rows tr')).to_have_count(1)
                expect(page.locator('#server-rows')).to_contain_text('Tokyo')
                page.locator('#server-search').fill('not-present')
                expect(page.locator('#server-rows')).to_contain_text('No servers match')
                checks.append('server search and protocol filters')

                page.locator('[data-page=exports]').click()
                page.locator('[data-copy="hiddify.json"]').click()
                expect(page.locator('#toast')).to_contain_text('copied')
                copied = page.evaluate('navigator.clipboard.readText()')
                response = page.request.get(copied)
                assert response.status == 200
                assert len(response.json()['outbounds']) == 2
                checks.append('copy a usable subscription link and download combined JSON')

                page.locator('[data-copy="v2board.txt"]').click()
                expect(page.locator('#toast')).to_contain_text('copied')
                v2board_link = page.evaluate('navigator.clipboard.readText()')
                response = page.request.get(v2board_link)
                assert response.status == 200
                assert response.headers['content-type'].startswith('text/plain')
                assert {line.split('://', 1)[0] for line in response.text().splitlines()} == {'vless', 'ss'}
                checks.append('copy and fetch V2Board URI subscription')

                page.locator('[data-page=providers]').click()
                page.locator('[data-action=edit]').first.click()
                page.locator('#provider-name').fill('Renamed provider')
                page.locator('#save-provider').click()
                expect(page.locator('#provider-dialog')).not_to_be_visible()
                expect(page.locator('#provider-rows')).to_contain_text('Renamed provider')
                expect(page.locator('#refresh-button')).to_be_enabled(timeout=15000)
                paused_provider=page.locator('#provider-rows tr').filter(has_text='Demo 2')
                paused_provider.locator('[data-action=toggle]').click()
                expect(paused_provider).to_contain_text('Paused')
                page.once('dialog', lambda dialog: dialog.accept())
                paused_provider.locator('[data-action=delete]').click()
                expect(page.locator('#provider-rows tr')).to_have_count(1)
                checks.append('edit, pause and delete providers')

                page.locator('[data-page=devices]').click()
                expect(second.locator('[data-device-action=delete]')).to_be_enabled()
                page.once('dialog',lambda dialog:dialog.accept())
                second.locator('[data-device-action=delete]').click()
                expect(second).to_have_count(0)
                checks.append('delete a device only after its subscription is removed')

                page.locator('[data-page=settings]').click()
                page.locator('#refresh-interval').select_option('900')
                page.locator('#schedule-form button').click()
                expect(page.locator('#toast')).to_contain_text('saved')
                assert manager.store.config()['refresh_seconds'] == 900
                checks.append('persist refresh schedule')

                page.locator('[data-page=overview]').click()
                assert page.evaluate('document.body.scrollWidth <= innerWidth')
                page.set_viewport_size({'width':390, 'height':844})
                assert page.evaluate('document.body.scrollWidth <= innerWidth')
                page.locator('[data-page=devices]').click()
                expect(page.locator('#devices-section')).to_be_visible()
                if os.environ.get('PARSER_TEST_SCREENSHOTS'):
                    page.screenshot(path=os.environ['PARSER_TEST_SCREENSHOTS']+'/devices-mobile.png',full_page=True)
                page.locator('#add-hwid').click()
                expect(page.locator('#hwid-dialog')).to_be_visible()
                assert page.evaluate('document.body.scrollWidth <= innerWidth')
                page.locator('[data-close=hwid-dialog]').first.click()
                checks.append('desktop and mobile layout, navigation and dialogs')
                page.locator('#logout-button').click()
                expect(page.locator('#login-view')).to_be_visible()
                checks.append('sign out')
                assert not errors, errors
                browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
    report = {'tested_at':common.now(), 'checks':checks, 'passed':len(checks), 'page_errors':[]}
    common.atomic_write(ROOT / '.parser-state/browser-check.json', common.json_text(report))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
