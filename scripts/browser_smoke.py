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
from subscription_parser import common, connectivity
from subscription_parser.app import create_app
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


def main():
    os.umask(0o077)
    checks = []
    with tempfile.TemporaryDirectory(prefix="parser-browser-") as temporary:
        root = Path(temporary)
        seen = []
        failed_nodes = set()
        def fetch(url, hwid, user_agent, timeout):
            seen.append(hwid)
            return json.dumps([{'outbounds': [
                {'type': 'vless', 'tag': 'Berlin', 'server': 'example.org', 'server_port': 443,
                 'uuid': '01234567-89ab-4cde-8fab-0123456789ab'},
                {'type': 'shadowsocks', 'tag': 'Tokyo', 'server': 'example.net', 'server_port': 443,
                 'method': 'aes-128-gcm', 'password': 'synthetic-password'}]},
                {'outbounds': [{'protocol': 'loopback', 'tag': 'routing-helper',
                                'settings': {'inboundTag': 'route-again'}}]}]).encode(), {'fetched_at': common.now()}
        admin = 'synthetic-browser-admin-key'
        with patch.dict(os.environ, {'PARSER_ADMIN_TOKEN': admin}):
            def checker(entries, root, **kwargs):
                return connectivity.check_entries(entries, root, **kwargs,
                    tester=lambda node, *args: {'status': 'failed', 'reason': 'HTTPS check failed'}
                    if node.name in failed_nodes else {'status': 'passed', 'latency_ms': 12})
            app = create_app(root, start_scheduler=False, fetcher=fetch, checker=checker)
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

                page.locator('.nav-item[data-page=devices]').click()
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

                page.locator('.nav-item[data-page=providers]').click()
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
                page.locator('.nav-item[data-page=overview]').click()
                expect(page.locator('#provider-rows tr')).to_have_count(2)
                page.locator('.nav-item[data-page=servers]').click()
                page.locator('#check-servers').click()
                expect(page.locator('#connection-summary')).to_contain_text('2 of 2 nodes included',timeout=15000)
                expect(page.locator('#server-rows')).to_contain_text('Passed')
                expect(page.locator('#server-total')).to_have_text('2')
                expect(page.locator('#protocol-filter')).not_to_contain_text('LOOPBACK')
                expect(page.locator('#connection-summary')).to_contain_text('0 excluded')
                assert len(seen)==2
                checks.append('cached connection checks publish passing nodes without another provider fetch')
                page.locator('.nav-item[data-page=overview]').click()
                expect(page.locator('#stat-servers')).to_have_text('2', timeout=15000)
                assert seen==['saved-browser-hwid','second-browser-hwid']
                checks.append('explicit per-link devices, test-before-save and reuse of tested responses')

                page.locator('.nav-item[data-page=devices]').click()
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

                page.locator('.nav-item[data-page=providers]').click()
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
                page.locator('.nav-item[data-page=providers]').click()
                no_device=page.locator('#provider-rows tr').filter(has_text='No device provider')
                expect(no_device).to_contain_text('No device ID')
                page.once('dialog',lambda dialog:dialog.accept())
                no_device.locator('[data-action=delete]').click()
                expect(page.locator('#provider-rows tr')).to_have_count(2)
                checks.append('explicit no-device subscription and removal')

                page.locator('.nav-item[data-page=servers]').click()
                page.locator('#protocol-filter').select_option('shadowsocks')
                expect(page.locator('#server-rows tr')).to_have_count(1)
                expect(page.locator('#server-rows')).to_contain_text('Tokyo')
                page.locator('#server-search').fill('not-present')
                expect(page.locator('#server-rows')).to_contain_text('No servers match')
                checks.append('server search and protocol filters')

                page.locator('#server-search').fill('')
                page.locator('#protocol-filter').select_option('')
                failed_nodes.add('Tokyo')
                fetch_count = len(seen)
                page.locator('#check-servers').click()
                expect(page.locator('#connection-summary')).to_contain_text('1 of 2 nodes included',timeout=15000)
                page.locator('#connection-filter').select_option('excluded')
                expect(page.locator('#server-rows tr')).to_have_count(1)
                expect(page.locator('#server-rows')).to_contain_text('Tokyo')
                expect(page.locator('#server-rows')).to_contain_text('Failed')
                expect(page.locator('#connection-summary')).to_contain_text('1 excluded')
                page.locator('#connection-filter').select_option('')
                if os.environ.get('PARSER_TEST_SCREENSHOTS'):
                    page.screenshot(path=os.environ['PARSER_TEST_SCREENSHOTS']+'/servers-desktop.png',full_page=True)
                    page.set_viewport_size({'width':390,'height':844})
                    assert page.evaluate('document.body.scrollWidth <= innerWidth')
                    page.screenshot(path=os.environ['PARSER_TEST_SCREENSHOTS']+'/servers-mobile.png',full_page=True)
                    page.set_viewport_size({'width':1440,'height':1000})
                failed_nodes.clear()
                page.locator('#check-servers').click()
                expect(page.locator('#connection-summary')).to_contain_text('2 of 2 nodes included',timeout=15000)
                assert len(seen) == fetch_count
                checks.append('show failed nodes, filter excluded results and recover without fetching providers')

                page.locator('.nav-item[data-page=exports]').click()
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

                legacy_link = v2board_link
                # The earlier replacement intentionally invalidated Demo 1's
                # cache. Refresh the synthetic providers before scoping feeds.
                page.locator('.nav-item[data-page=providers]').click()
                page.locator('#refresh-button').click()
                expect(page.locator('#provider-rows tr').first.locator('.server-count')).to_have_text('2', timeout=15000)
                page.locator('.nav-item[data-page=exports]').click()
                expect(page.locator('#add-subscription')).to_be_enabled(timeout=15000)
                subscription_fetches = len(seen)
                providers = manager.store.config()['providers']
                page.locator('#add-subscription').click()
                page.locator('#subscription-name').fill('My phone <private>')
                page.locator('#save-subscription').click()
                expect(page.locator('#subscription-error')).to_contain_text('Choose one or more')
                page.locator('#subscription-provider-choices input[value="'+providers[0]['id']+'"]').check()
                page.locator('#save-subscription').click()
                expect(page.locator('#subscription-dialog')).not_to_be_visible()
                expect(page.locator('#subscription-total')).to_have_text('2')
                phone_id = page.locator('#subscription-list [aria-current=true]').get_attribute('data-subscription-id')
                expect(page.locator('#subscription-summary')).to_contain_text('2 of 2 servers included')
                page.locator('[data-copy="combined.json"]').click()
                phone_link = page.evaluate('navigator.clipboard.readText()')
                phone_archive = page.request.get(phone_link).json()
                assert {source for node in phone_archive['servers'] for source in node['sources']} == {providers[0]['name']}
                page.locator('#edit-subscription').click()
                page.locator('#subscription-provider-choices input[value="'+providers[1]['id']+'"]').check()
                page.locator('#save-subscription').click()
                expect(page.locator('#subscription-dialog')).not_to_be_visible()
                expect(page.locator('#subscription-providers-summary')).to_contain_text(providers[1]['name'])
                assert {source for node in page.request.get(phone_link).json()['servers'] for source in node['sources']} == {p['name'] for p in providers}
                checks.append('create scoped subscriptions, require a provider and edit selections without changing links')

                page.locator('#add-subscription').click()
                page.locator('#subscription-name').fill('Family')
                page.locator('#subscription-all').check()
                expect(page.locator('#subscription-provider-choices input').first).to_be_disabled()
                page.locator('#save-subscription').click()
                expect(page.locator('#subscription-dialog')).not_to_be_visible()
                family_id = page.locator('#subscription-list [aria-current=true]').get_attribute('data-subscription-id')
                expect(page.locator('#subscription-total')).to_have_text('3')
                page.locator('[data-copy="v2board.txt"]').click()
                family_link = page.evaluate('navigator.clipboard.readText()')
                assert family_link != legacy_link
                page.locator('#subscription-search').fill('my phone')
                expect(page.locator('#subscription-list > li')).to_have_count(1)
                page.locator('#subscription-search').fill('no such subscription')
                expect(page.locator('#subscriptions-no-results')).to_be_visible()
                page.locator('#clear-subscription-search').click()
                expect(page.locator('#subscription-list > li')).to_have_count(3)
                expect(page.locator('#subscription-search')).to_be_focused()
                page.locator('[data-subscription-id="'+phone_id+'"]').click()
                expect(page.locator('#subscription-detail-name')).to_have_text('My phone <private>')
                page.locator('#show-subscription-urls').check()
                expect(page.locator('.export-url input').first).to_have_value(phone_link.replace('combined.json','hiddify.json'))
                page.locator('#show-subscription-urls').uncheck()
                expect(page.locator('.export-url').first).not_to_be_visible()
                page.locator('#subscription-menu summary').click()
                page.keyboard.press('Escape')
                expect(page.locator('#subscription-menu summary')).to_be_focused()
                expect(page.locator('#rotate-links')).not_to_be_visible()
                checks.append('search subscriptions, recover from empty results, reveal URLs and dismiss actions with the keyboard')
                page.once('dialog', lambda dialog: dialog.accept())
                page.locator('#subscription-menu summary').click()
                page.locator('#rotate-links').click()
                expect(page.locator('#toast')).to_contain_text('New subscription links')
                assert page.request.get(phone_link).status == 404
                assert page.request.get(family_link).status == 200
                assert page.request.get(legacy_link).status == 200
                page.set_viewport_size({'width':390,'height':844})
                assert page.evaluate('document.body.scrollWidth <= innerWidth')
                page.locator('#back-to-subscriptions').click()
                expect(page.locator('#subscription-list')).to_be_visible()
                expect(page.locator('#subscription-detail')).not_to_be_visible()
                if os.environ.get('PARSER_TEST_SCREENSHOTS'):
                    page.screenshot(path=os.environ['PARSER_TEST_SCREENSHOTS']+'/subscriptions-list-mobile.png',full_page=True)
                page.locator('[data-subscription-id="'+phone_id+'"]').focus()
                page.keyboard.press('Enter')
                expect(page.locator('#subscription-detail-name')).to_be_focused()
                expect(page.locator('#subscription-list')).not_to_be_visible()
                page.locator('#edit-subscription').click()
                expect(page.locator('#subscription-name')).to_have_value('My phone <private>')
                page.locator('#subscription-provider-search').fill(providers[1]['name'])
                expect(page.locator('.provider-choice:visible')).to_have_count(1)
                page.locator('#clear-selected-providers').click()
                expect(page.locator('#subscription-selection-summary')).to_contain_text('0 providers selected')
                page.locator('#select-active-providers').click()
                expect(page.locator('#subscription-selection-summary')).to_contain_text('2 providers selected')
                page.locator('#subscription-provider-search').fill('')
                assert page.evaluate('document.body.scrollWidth <= innerWidth')
                if os.environ.get('PARSER_TEST_SCREENSHOTS'):
                    page.screenshot(path=os.environ['PARSER_TEST_SCREENSHOTS']+'/subscriptions-dialog-mobile.png',full_page=True)
                page.locator('[data-close=subscription-dialog]').first.click()
                if os.environ.get('PARSER_TEST_SCREENSHOTS'):
                    page.screenshot(path=os.environ['PARSER_TEST_SCREENSHOTS']+'/subscriptions-mobile.png',full_page=True)
                page.set_viewport_size({'width':1440,'height':1000})
                if os.environ.get('PARSER_TEST_SCREENSHOTS'):
                    page.screenshot(path=os.environ['PARSER_TEST_SCREENSHOTS']+'/subscriptions-desktop.png',full_page=True)
                page.once('dialog', lambda dialog: dialog.accept())
                page.locator('#subscription-menu summary').click()
                page.locator('#delete-subscription').click()
                expect(page.locator('[data-subscription-id=default]')).to_have_attribute('aria-current','true')
                expect(page.locator('#delete-subscription')).to_be_disabled()
                expect(page.locator('#subscription-total')).to_have_text('2')
                page.locator('[data-subscription-id="'+family_id+'"]').click()
                assert page.request.get(family_link).status == 200
                assert len(seen) == subscription_fetches
                checks.append('independent subscription links, rotation, deletion, safe names and mobile controls without extra fetches')
                checks.append('mobile list and detail navigation, keyboard selection and searchable provider choices')

                page.locator('.nav-item[data-page=providers]').click()
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

                page.locator('.nav-item[data-page=devices]').click()
                expect(second.locator('[data-device-action=delete]')).to_be_enabled()
                page.once('dialog',lambda dialog:dialog.accept())
                second.locator('[data-device-action=delete]').click()
                expect(second).to_have_count(0)
                checks.append('delete a device only after its subscription is removed')

                page.locator('.nav-item[data-page=settings]').click()
                page.locator('#refresh-interval').select_option('900')
                page.locator('#schedule-form button').click()
                expect(page.locator('#toast')).to_contain_text('saved')
                assert manager.store.config()['refresh_seconds'] == 900
                checks.append('persist refresh schedule')
                page.locator('#check-interval').select_option('900')
                page.locator('#check-schedule-form button').click()
                expect(page.locator('#toast')).to_contain_text('Connection check schedule saved')
                assert manager.store.config()['check_seconds'] == 900
                checks.append('persist independent connection-check schedule')

                page.locator('.nav-item[data-page=activity]').click()
                expect(page.locator('#log-output')).to_contain_text('Subscription job completed')
                expect(page.locator('#log-retention')).to_contain_text('4 MiB maximum')
                manager.logs.write('provider_failed', provider_id='browser-test', reason='provider returned HTTP 429')
                page.locator('#log-level').select_option('warning')
                expect(page.locator('#log-output')).to_contain_text('provider returned HTTP 429')
                expect(page.locator('#log-output')).not_to_contain_text('[INFO]')
                manager.logs.write('request_failed', error=RuntimeError('browser-private-log-secret'))
                page.locator('#log-level').select_option('error')
                expect(page.locator('#log-output')).to_contain_text('Application request failed')
                expect(page.locator('#log-output')).not_to_contain_text('[WARNING]')
                expect(page.locator('#log-output')).not_to_contain_text('browser-private-log-secret')
                page.locator('#refresh-logs').click()
                expect(page.locator('#refresh-logs')).to_be_enabled()
                if os.environ.get('PARSER_TEST_SCREENSHOTS'):
                    page.screenshot(path=os.environ['PARSER_TEST_SCREENSHOTS']+'/logs-desktop.png',full_page=True)
                page.set_viewport_size({'width':390,'height':844})
                assert page.evaluate('document.body.scrollWidth <= innerWidth')
                if os.environ.get('PARSER_TEST_SCREENSHOTS'):
                    page.screenshot(path=os.environ['PARSER_TEST_SCREENSHOTS']+'/logs-mobile.png',full_page=True)
                page.set_viewport_size({'width':1440,'height':1000})
                checks.append('private debug history, severity filters, manual refresh and mobile log view')

                page.locator('.nav-item[data-page=overview]').click()
                assert page.evaluate('document.body.scrollWidth <= innerWidth')
                page.set_viewport_size({'width':390, 'height':844})
                assert page.evaluate('document.body.scrollWidth <= innerWidth')
                for name in ('overview','providers','devices','servers','exports','activity','settings'):
                    button = page.locator('.nav-item[data-page='+name+']')
                    expect(button).to_be_visible()
                    assert button.bounding_box()['width'] >= 44
                page.locator('.nav-item[data-page=activity]').click()
                expect(page.locator('#logs-section')).to_be_visible()
                checks.append('all seven labeled navigation actions remain accessible on mobile')
                page.locator('.nav-item[data-page=devices]').click()
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
