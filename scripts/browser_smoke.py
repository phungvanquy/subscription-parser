#!/usr/bin/env python3
"""Exercise the dashboard with synthetic providers; never change live accounts."""
from contextlib import contextmanager, suppress
import copy
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from urllib.parse import urljoin
import pyotp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import uvicorn
from playwright.sync_api import sync_playwright, expect
from subscription_parser import common, connectivity, publication
from subscription_parser.app import create_app
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


def read_clipboard(page):
    if os.environ.get('PARSER_TEST_ENGINE', 'chromium') == 'chromium':
        return page.evaluate('navigator.clipboard.readText()')
    # Firefox/WebKit do not support Playwright's clipboard-read permission.
    # Await the real clipboard write; record its text only after it succeeds.
    return page.evaluate('window.__testClipboardWrite')


def provider_details(row):
    return row.locator('xpath=following-sibling::tr[1]')


def check_server_details(page, checks):
    """Inspect actual cached settings without leaving the filtered library."""
    first = page.locator('#server-rows .server-open').first
    first.focus()
    page.keyboard.press('Enter')
    dialog = page.locator('#server-dialog')
    expect(dialog).to_be_visible()
    expect(page.locator('#server-detail-name')).to_have_text('Berlin')
    expect(page.locator('#server-detail-fields')).to_contain_text('example.org')
    expect(page.locator('#server-detail-position')).to_have_text('1 of 2 servers')
    expect(page.locator('#server-detail-previous')).to_be_disabled()
    expect(page.locator('#server-config-text')).to_contain_text('01234567-89ab-4cde-8fab-0123456789ab')
    original = page.locator('#server-config-text').inner_text()
    page.locator('#server-config-copy').click()
    expect(page.locator('#server-config-feedback')).to_have_text('Configuration copied')
    assert read_clipboard(page) == original
    with page.expect_download() as download:
        page.locator('#server-config-download').click()
    assert Path(download.value.path()).read_text() == original
    page.locator('#server-config-format').select_option('mihomo')
    expect(page.locator('#server-config-text')).to_contain_text('proxies:')
    page.evaluate('loadState()')
    expect(page.locator('#server-config-format')).to_have_value('mihomo')
    page.locator('#server-detail-next').click()
    expect(page.locator('#server-detail-name')).to_have_text('Tokyo')
    expect(page.locator('#server-config-text')).to_contain_text('synthetic-password')
    expect(page.locator('#server-detail-next')).to_be_disabled()
    page.locator('#server-detail-previous').click()
    expect(page.locator('#server-config-text')).to_have_text(original)
    checks.append('server inspector shows original and client formats; clipboard, download, navigation and polling work')
    for width in (1440, 768, 390, 320):
        page.set_viewport_size({'width': width, 'height': 900 if width > 640 else 844})
        assert dialog.evaluate('el => el.scrollWidth <= el.clientWidth')
        assert page.locator('#server-detail-body').evaluate('el => el.scrollWidth <= el.clientWidth')
        expect(page.locator('#server-detail-next')).to_be_in_viewport()
        if os.environ.get('PARSER_TEST_SCREENSHOTS') and width in (1440, 390):
            page.screenshot(path=os.environ['PARSER_TEST_SCREENSHOTS']+f'/server-detail-{width}.png')
    page.keyboard.press('Escape')
    expect(dialog).not_to_be_visible()
    expect(first).to_be_focused()
    expect(page.locator('#server-config-text')).to_have_text('')
    page.locator('#server-search').fill('Tokyo')
    page.locator('#server-rows .server-open').click()
    expect(page.locator('#server-detail-position')).to_have_text('1 of 1 servers')
    expect(page.locator('#server-detail-previous')).to_be_disabled()
    expect(page.locator('#server-detail-next')).to_be_disabled()
    page.locator('[data-close=server-dialog]').click()
    expect(page.locator('#server-search')).to_have_value('Tokyo')
    page.locator('#server-search').fill('')
    page.set_viewport_size({'width': 1440, 'height': 1000})
    checks.append('server inspector fits 320–1440px, restores keyboard focus and preserves filters')
    pattern = '**/api/servers/*'
    def unavailable(route):
        route.fulfill(status=404, json={'detail': 'Server no longer available'})
    page.route(pattern, unavailable)
    first.click()
    expect(page.locator('#server-detail-message')).to_have_text('Server no longer available')
    expect(page.locator('#server-detail-content')).not_to_be_visible()
    page.unroute(pattern, unavailable)
    page.locator('#server-detail-retry').click()
    expect(page.locator('#server-config-text')).to_have_text(original)
    page.keyboard.press('Escape')
    pending = []
    def delayed(route):
        pending.append(route)
    page.route(pattern, delayed)
    first.click()
    page.wait_for_timeout(100)
    page.locator('#server-detail-next').click()
    page.wait_for_timeout(100)
    assert len(pending) == 2
    pending[1].fulfill(response=pending[1].fetch())
    expect(page.locator('#server-config-text')).to_contain_text('synthetic-password')
    pending[0].fulfill(response=pending[0].fetch())
    expect(page.locator('#server-detail-name')).to_have_text('Tokyo')
    expect(page.locator('#server-config-text')).to_contain_text('synthetic-password')
    page.unroute(pattern, delayed)
    page.keyboard.press('Escape')
    checks.append('server inspector handles unavailable servers, retries and out-of-order requests safely')


def check_subscription_server_policy(page, manager, checks):
    """Exercise persistent selection, endpoint edits, resets and explicit relinking."""
    original_fetcher = manager.fetcher
    page.locator('.nav-item[data-page=exports]').click()
    page.locator('#add-subscription').click()
    page.locator('#subscription-name').fill('Custom endpoint test')
    page.locator('#subscription-all').check()
    page.locator('#save-subscription').click()
    expect(page.locator('#subscription-dialog')).not_to_be_visible()
    expect(page.locator('#subscription-detail-name')).to_have_text('Custom endpoint test')
    identifier = page.evaluate('selectedSubscription')
    token = manager.store.subscription(identifier)['token']
    url = urljoin(page.url, '/sub/' + token + '/hiddify.json')
    baseline = manager.snapshot['exports']['hiddify.json']
    try:
        page.locator('#customize-subscription').click()
        expect(page.locator('#policy-content')).to_be_visible()
        page.locator('#policy-mode').select_option('selected')
        page.locator('#policy-available-search').fill('Berlin')
        page.locator('.policy-available-row').filter(has_text='Berlin').first.locator('[data-add]').click()
        record = page.locator('.policy-record').first
        editor = page.locator('.policy-record-editor')
        editor.locator('[data-field=address]').fill('relay.example.org')
        editor.locator('[data-field=port]').fill('8443')
        editor.locator('[data-field=name]').fill('Custom <Berlin>')
        page.evaluate('loadState()')
        expect(editor.locator('[data-field=address]')).to_have_value('relay.example.org')
        for width in (1440, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1000 if width > 640 else 844})
            assert page.locator('#policy-dialog').evaluate('el => el.scrollWidth <= el.clientWidth')
            assert record.evaluate('el => el.scrollWidth <= el.clientWidth')
            expect(page.locator('#policy-save')).to_be_in_viewport()
            if os.environ.get('PARSER_TEST_SCREENSHOTS') and width in (1440, 390):
                page.screenshot(path=os.environ['PARSER_TEST_SCREENSHOTS'] + f'/server-policy-{width}.png')
        page.set_viewport_size({'width': 1440, 'height': 1000})
        page.locator('#policy-save').click()
        expect(page.locator('#policy-dialog')).not_to_be_visible()
        assert page.request.get(url).status == 503
        manager.begin_check(reuse_recent=True); manager.wait_for_idle(15)
        page.evaluate('loadState()')
        expect(page.locator('#subscription-published')).to_have_text('1')
        nodes = page.request.get(url).json()['outbounds']
        assert len(nodes) == 1 and nodes[0]['server'] == 'relay.example.org' and nodes[0]['server_port'] == 8443
        assert 'Custom <Berlin>' in nodes[0]['tag']
        assert manager.snapshot['exports']['hiddify.json'] == baseline
        saved_id = manager.store.server_policy(identifier)['records'][0]['id']
        page.locator('#customize-subscription').click()
        expect(page.locator('.policy-record')).to_have_count(1)
        page.locator('.policy-record [data-edit]').click()
        expect(editor).to_contain_text('Provider: example.org')
        page.locator('[data-reset=address]').click()
        expect(page.locator('[data-field=address]')).to_have_value('')
        page.keyboard.press('Escape')
        page.locator('#customize-subscription').click()
        page.locator('.policy-record [data-edit]').click()
        expect(page.locator('[data-field=address]')).to_have_value('relay.example.org')
        page.keyboard.press('Escape')
        checks.append('subscription server editor preserves drafts during polling, resets fields and fits 320–1440px')

        def changed_source(*args):
            body, metadata = original_fetcher(*args)
            document = json.loads(body)
            document[0]['outbounds'][0]['server_port'] = 444
            return json.dumps(document).encode(), metadata
        manager.fetcher = changed_source
        manager.refresh()
        page.evaluate('loadState()')
        page.locator('#customize-subscription').click()
        expect(page.locator('.policy-record .status-pill')).to_have_text('Source unavailable')
        page.locator('.policy-record [data-edit]').click()
        expect(page.locator('[data-field=address]')).to_have_value('relay.example.org')
        page.locator('.policy-suggestion').first.click()
        expect(editor).to_contain_text('Provider: 444')
        page.locator('#policy-save').click()
        expect(page.locator('#policy-dialog')).not_to_be_visible()
        manager.begin_check(reuse_recent=True); manager.wait_for_idle(15)
        assert manager.store.server_policy(identifier)['records'][0]['id'] == saved_id
        nodes = page.request.get(url).json()['outbounds']
        assert nodes[0]['server'] == 'relay.example.org' and nodes[0]['server_port'] == 8443
        page.evaluate('loadState()')
        page.locator('#customize-subscription').click()
        expect(page.locator('.policy-record')).to_have_count(1)
        page.locator('[data-selected]').uncheck()
        page.locator('#policy-save').click()
        expect(page.locator('#policy-dialog')).not_to_be_visible()
        assert page.request.get(url).status == 503
        assert manager.store.server_policy(identifier)['records'][0]['address'] == 'relay.example.org'
        checks.append('custom endpoints publish only after checks; changed sources require relinking and retain edits when deselected')
    finally:
        manager.fetcher = original_fetcher
        if page.locator('#policy-dialog').is_visible():
            page.keyboard.press('Escape')
        manager.change_subscription(identifier=identifier, delete=True)
        manager.refresh()
        page.evaluate('loadState()')
        page.evaluate('showSubscriptionList()')


def check_large_server_policy(page, checks):
    """Bound the rendered list and keep drafts across filters, pages and bulk actions."""
    page.locator('.nav-item[data-page=exports]').click()
    page.evaluate("showSubscription('default')")
    records = []
    for index in range(1000):
        source = {'name': f'Server {index:04} <private>', 'protocol': 'vless',
                  'address': f'server-{index:04}.example.org', 'port': 443}
        records.append({'id': f'saved-{index}', 'provider_id': f'provider-{index % 4}',
            'provider_name': f'Provider {index % 4}', 'source_identity': f'{index:064x}',
            'source': source, 'selected': index % 5 != 0, 'address': None, 'port': None,
            'name': None, 'source_present': index % 4 != 0,
            'status': 'unavailable' if index % 4 == 0 else 'working', 'checks': {}, 'suggestions': []})
    candidates = [{'provider_id': 'provider-1', 'provider_name': 'Provider 1',
                   'source_identity': f'{index + 2000:064x}',
                   'source': {'name': f'Available {index:03}', 'protocol': 'vless',
                              'address': f'available-{index}.example.org', 'port': 443}}
                  for index in range(50)]
    fixture = {'revision': 0, 'all_servers': False, 'records': records, 'available': candidates,
               'available_total': len(candidates), 'offset': 0, 'limit': 50, 'max_records': 1000}
    writes = []
    fail_save = [False]
    def response(route):
        if route.request.method == 'PUT':
            writes.append(route.request.post_data_json)
            if fail_save[0]:
                route.fulfill(status=409, json={'detail': 'Wait for the current connection check'})
            else:
                route.fulfill(json={'revision': 1})
        else:
            route.fulfill(json=fixture)
    pattern = '**/api/subscriptions/default/servers*'
    page.route(pattern, response)
    try:
        page.locator('#customize-subscription').click()
        rows = page.locator('.policy-record')
        expect(rows).to_have_count(25)
        expect(page.locator('#policy-saved-count')).to_have_text('1000 saved · 800 selected')
        expect(page.locator('.policy-record-editor')).to_have_count(0)
        expect(page.locator('#policy-available-panel')).not_to_be_visible()
        assert len(page.locator('#policy-records input').all()) == 25
        page.locator('#policy-saved-page-select').select_option('39')
        expect(rows.first).to_contain_text('Server 0975')
        page.locator('#policy-saved-previous').click()
        expect(rows.first).to_contain_text('Server 0950')
        page.locator('#policy-saved-page-select').select_option('0')
        rows.nth(1).locator('[data-edit]').click()
        page.locator('[data-field=address]').fill('my-relay.example.org')
        page.locator('[data-field=port]').fill('8443')
        page.locator('[data-field=name]').fill('My <saved> server')
        page.evaluate('loadState()')
        expect(page.locator('[data-field=address]')).to_have_value('my-relay.example.org')
        page.locator('#policy-saved-next').click()
        expect(page.locator('.policy-record-editor')).to_have_count(0)
        page.locator('#policy-saved-search').fill('my-relay')
        expect(rows).to_have_count(1)
        expect(rows.first).to_contain_text('My <saved> server')
        rows.first.locator('[data-edit]').click()
        expect(page.locator('[data-field=port]')).to_have_value('8443')
        page.locator('[data-done]').click()
        expect(page.locator('.policy-record-editor')).to_have_count(0)
        expect(rows.first.locator('[data-edit]')).to_be_focused()
        page.locator('#policy-saved-search').fill('')
        page.locator('#policy-provider-filter').select_option('provider-0')
        expect(page.locator('#policy-exclude-matches')).to_have_text('Exclude 250 matches')
        page.locator('#policy-exclude-matches').click()
        expect(page.locator('#policy-saved-count')).to_have_text('1000 saved · 600 selected')
        page.locator('#policy-status-filter').select_option('unavailable')
        expect(page.locator('#policy-saved-page')).to_contain_text('of 250 matches')
        page.locator('#policy-include-matches').click()
        expect(page.locator('#policy-saved-count')).to_have_text('1000 saved · 850 selected')
        page.locator('#policy-provider-filter').select_option('')
        page.locator('#policy-status-filter').select_option('overrides')
        expect(rows).to_have_count(1)
        expect(rows.first).to_contain_text('My <saved> server')
        page.locator('#policy-status-filter').select_option('')
        page.locator('#policy-mode').select_option('all')
        expect(page.locator('#policy-bulk-selection')).not_to_be_visible()
        expect(page.locator('[data-selected]')).to_have_count(0)
        page.locator('#policy-mode').select_option('selected')
        page.locator('#policy-tab-saved').focus()
        page.keyboard.press('ArrowRight')
        expect(page.locator('#policy-tab-available')).to_be_focused()
        expect(page.locator('#policy-available-panel')).to_be_visible()
        expect(page.locator('#policy-add-page')).to_be_disabled()
        page.keyboard.press('Home')
        expect(page.locator('#policy-tab-saved')).to_be_focused()
        expect(page.locator('#policy-saved-panel')).to_be_visible()
        checks.append('1000 saved servers render only 25 compact rows; filters, page jumps and scoped bulk actions preserve edits')

        for width in (1440, 768, 390, 320):
            page.set_viewport_size({'width': width, 'height': 1000 if width > 640 else 844})
            page.locator('#policy-tab-saved').click()
            dialog = page.locator('#policy-dialog')
            assert dialog.evaluate('el => el.scrollWidth <= el.clientWidth'), width
            assert page.locator('.policy-editor-body').evaluate('el => el.scrollWidth <= el.clientWidth'), width
            heights = rows.evaluate_all('rows => rows.map(row => row.getBoundingClientRect().height)')
            assert max(heights) <= (132 if width <= 640 else 85), (width, heights)
            expect(page.locator('#policy-save')).to_be_in_viewport()
            page.locator('.policy-editor-body').evaluate('el => el.scrollTop = el.scrollHeight')
            expect(page.locator('#policy-tab-available')).to_be_in_viewport()
            expect(page.locator('#policy-saved-search')).to_be_in_viewport()
            expect(page.locator('#policy-saved-next')).to_be_in_viewport()
            if os.environ.get('PARSER_TEST_SCREENSHOTS') and width in (1440, 390):
                page.locator('#policy-saved-panel').scroll_into_view_if_needed()
                page.screenshot(path=os.environ['PARSER_TEST_SCREENSHOTS'] + f'/server-policy-1000-{width}.png')
        page.set_viewport_size({'width': 1440, 'height': 1000})
        page.locator('#policy-saved-page-select').select_option('0')
        rows.nth(1).locator('[data-edit]').click()
        page.locator('[data-field=port]').fill('70000')
        page.locator('#policy-tab-available').click()
        page.locator('#policy-save').click()
        expect(page.locator('#policy-saved-panel')).to_be_visible()
        expect(page.locator('#policy-error')).to_contain_text('between 1 and 65535')
        page.locator('[data-field=port]').click()
        page.locator('#policy-saved-next').click()
        page.locator('#policy-save').click()
        expect(page.locator('#policy-error')).to_contain_text('between 1 and 65535')
        expect(page.locator('[data-field=port]')).to_have_value('70000')
        page.locator('[data-field=port]').fill('8443')
        fail_save[0] = True
        page.locator('#policy-save').click()
        expect(page.locator('#policy-error')).to_contain_text('Wait for the current connection check')
        expect(page.locator('[data-field=address]')).to_have_value('my-relay.example.org')
        fail_save[0] = False
        page.locator('#policy-save').click()
        expect(page.locator('#policy-dialog')).not_to_be_visible()
        saved = writes[-1]['records']
        assert len(saved) == 1000 and sum(row['selected'] for row in saved) == 850
        assert saved[1]['address'] == 'my-relay.example.org' and saved[1]['port'] == 8443
        assert saved[1]['name'] == 'My <saved> server'
        assert all(row['selected'] for index, row in enumerate(saved) if index % 4 == 0)
        assert all(row['selected'] == records[index]['selected'] for index, row in enumerate(saved) if index % 4 != 0)
        expect(page.locator('#policy-records')).to_be_empty()
        checks.append('large-list editor fits 320–1440px, keeps paging controls visible and retains drafts after invalid ports or failed saves')

        fixture['records'] = []
        page.locator('#customize-subscription').click()
        expect(page.locator('#policy-available-panel')).to_be_visible()
        page.locator('#policy-add-page').click()
        expect(page.locator('#policy-saved-panel')).to_be_visible()
        expect(rows).to_have_count(25)
        expect(page.locator('#policy-saved-count')).to_have_text('50 saved · 50 selected')
        page.locator('#policy-tab-available').click()
        expect(page.locator('.policy-available-row')).to_have_count(0)
        expect(page.locator('#policy-add-page')).to_be_disabled()
        page.locator('#policy-save').click()
        expect(page.locator('#policy-dialog')).not_to_be_visible()
        assert len(writes[-1]['records']) == 50
        assert len({row['source_identity'] for row in writes[-1]['records']}) == 50
        checks.append('available-server page can be added in one action without duplicate bindings or exceeding the saved-server limit')
    finally:
        if page.locator('#policy-dialog').is_visible():
            page.keyboard.press('Escape')
        page.unroute(pattern, response)
        page.set_viewport_size({'width': 1440, 'height': 1000})
        page.evaluate('showSubscriptionList()')


def check_large_provider_list(page, checks):
    """Exercise pagination and live updates with 200 synthetic sources."""
    fixture = page.evaluate('state')
    source, node = fixture['providers'][0], fixture['inventory'][0]
    fixture.update(providers=[], inventory=[], busy=False, refresh_running=False,
                   check_running=False, check_queued=False, last_error=None,
                   revision='synthetic-provider-list')
    for index in range(1, 201):
        kind = index % 4
        provider = {**source, 'id': f'large-{index}', 'name': f'Provider {index:03}',
                    'host': f'provider-{index}.example', 'enabled': kind != 0,
                    'status': ('disabled', 'fresh', 'cached', 'unavailable')[kind],
                    'count': 20, 'fetched_count': 200, 'server_limit': 20,
                    'selection_pending': index == 1,
                    'error': 'Provider returned HTTP 429. ' + 'Retry later. ' * 30 if kind == 2 else None}
        if index == 1:
            provider['name'] += ' · A very long provider name with more detail than the row can display'
            provider['host'] = 'long-host-' * 25 + '.example'
        fixture['providers'].append(provider)
        if kind:
            for server in range(20):
                clients = node['clients'] if kind in (1, 2) and server < 15 else []
                fixture['inventory'].append({**node, 'id': f'large-node-{index}-{server}',
                    'name': f'Server {index}-{server}', 'sources': [provider['name']], 'clients': clients,
                    'connectivity': {core: {'status': 'passed' if clients else 'failed', 'latency_ms': 12}
                                     for core in ('hiddify', 'mihomo', 'xray')}})
    fixture['total'] = len(fixture['inventory'])
    fixture['published_total'] = sum(bool(n['clients']) for n in fixture['inventory'])
    def state_response(route):
        route.fulfill(json=fixture)
    page.route('**/api/state*', state_response)
    try:
        page.set_viewport_size({'width': 1440, 'height': 1000})
        page.evaluate('loadState()')
        page.locator('.nav-item[data-page=providers]').click()
        rows = page.locator('#provider-rows .provider-row')
        expect(rows).to_have_count(25)
        expect(page.locator('#provider-footer')).to_have_text('1–25 of 200 providers')
        expect(page.locator('#provider-previous')).to_be_disabled()
        assert max(rows.evaluate_all('rows => rows.map(row => row.getBoundingClientRect().height)')) <= 64
        first = rows.first
        first.locator('.provider-expand').focus()
        page.keyboard.press('Enter')
        expect(provider_details(first)).to_be_visible()
        expect(provider_details(first)).to_contain_text('200 fetched · 20 selected · random limit 20')
        expect(provider_details(first)).to_contain_text('previous selection stays active')
        page.evaluate('loadState(true)')
        expect(first.locator('.provider-expand')).to_be_focused()
        expect(first.locator('.provider-expand')).to_have_attribute('aria-expanded', 'true')
        first.locator('.provider-expand').click()
        if os.environ.get('PARSER_TEST_SCREENSHOTS'):
            page.screenshot(path=os.environ['PARSER_TEST_SCREENSHOTS']+'/providers-200-desktop.png')
        page.locator('#provider-next').click()
        expect(page.locator('#provider-footer')).to_have_text('26–50 of 200 providers')
        expect(rows.first).to_contain_text('Provider 026')
        rows.first.locator('.provider-actions summary').click()
        page.evaluate('loadState(true)')
        expect(rows.first.locator('[data-action=edit]')).to_be_visible()
        expect(rows.first.locator('[data-action=edit]')).to_have_attribute('data-id', 'large-26')
        expect(page.locator('#provider-page-label')).to_have_text('2 / 8')
        page.keyboard.press('Escape')
        page.locator('#provider-search').fill('pRoViDeR 199')
        expect(rows).to_have_count(1)
        expect(rows.first).to_contain_text('Provider 199')
        expect(page.locator('#provider-page-label')).to_have_text('1 / 1')
        page.locator('#provider-search').fill('provider-198.example')
        expect(rows).to_have_count(1)
        rows.first.locator('.provider-working').click()
        expect(page.locator('#provider-filter')).to_have_value('large-198')
        expect(page.locator('#connection-filter')).to_have_value('passing')
        expect(page.locator('#server-rows tr')).to_have_count(15)
        page.locator('.nav-item[data-page=providers]').click()
        expect(page.locator('#provider-search')).to_have_value('provider-198.example')
        page.locator('#provider-search').fill('does-not-exist')
        expect(page.locator('#providers-no-results')).to_be_visible()
        expect(page.locator('#providers-empty')).not_to_be_visible()
        page.locator('#clear-provider-filters').click()
        expect(page.locator('#provider-search')).to_be_focused()
        expect(rows).to_have_count(25)
        checks.append('200-provider search, page navigation, empty search, working-server drill-down and polling preserve controls')

        for status, total in (('enabled', 150), ('passing', 100), ('attention', 100), ('paused', 50)):
            page.locator('#provider-status-filter').select_option(status)
            expect(page.locator('#provider-footer')).to_have_text(f'1–25 of {total} providers · 200 total')
        expect(rows.first.locator('.provider-check-note')).to_have_text('Paused')
        page.locator('#provider-status-filter').select_option('')
        page.locator('#provider-page-size').select_option('50')
        expect(rows).to_have_count(50)
        for _ in range(3):
            page.locator('#provider-next').click()
        expect(page.locator('#provider-footer')).to_have_text('151–200 of 200 providers')
        expect(page.locator('#provider-next')).to_be_disabled()
        saved_providers = fixture['providers']
        fixture['providers'] = saved_providers[:49]
        page.evaluate('loadState()')
        expect(page.locator('#provider-footer')).to_have_text('1–49 of 49 providers')
        expect(page.locator('#provider-page-label')).to_have_text('1 / 1')
        fixture['providers'] = saved_providers
        page.evaluate('loadState()')
        page.locator('#provider-page-size').select_option('100')
        expect(rows).to_have_count(100)
        page.locator('#provider-page-size').select_option('25')
        checks.append('provider status filters, configurable page size and page clamping after live removal')

        for width in (320, 390, 768, 1000, 1440):
            page.set_viewport_size({'width': width, 'height': 1000})
            assert page.evaluate('document.body.scrollWidth <= innerWidth')
            assert page.locator('#provider-table-wrap').evaluate('el => el.scrollWidth <= el.clientWidth')
            heights = rows.evaluate_all('rows => rows.map(row => row.getBoundingClientRect().height)')
            assert max(heights) <= (104 if width <= 760 else 68), (width, heights)
            first.locator('.provider-expand').click()
            expect(provider_details(first)).to_be_visible()
            assert page.evaluate('document.body.scrollWidth <= innerWidth')
            first.locator('.provider-expand').click()
            rows.nth(1).locator('.provider-expand').click()
            expect(provider_details(rows.nth(1)).locator('.provider-fetch-error')).to_be_visible()
            assert page.evaluate('document.body.scrollWidth <= innerWidth')
            rows.nth(1).locator('.provider-expand').click()
            first.locator('.provider-actions summary').click()
            expect(first.locator('[data-action=edit]')).to_be_in_viewport()
            assert page.evaluate('document.body.scrollWidth <= innerWidth')
            page.keyboard.press('Escape')
            if width == 390 and os.environ.get('PARSER_TEST_SCREENSHOTS'):
                page.locator('#providers-section').scroll_into_view_if_needed()
                page.screenshot(path=os.environ['PARSER_TEST_SCREENSHOTS']+'/providers-200-mobile.png')
        fixture['providers'], fixture['inventory'] = [], []
        page.evaluate('loadState()')
        expect(page.locator('#providers-empty')).to_be_visible()
        expect(page.locator('#provider-toolbar')).not_to_be_visible()
        expect(page.locator('#provider-list-footer')).not_to_be_visible()
        checks.append('compact provider row heights, long names/errors, expandable details and menus fit desktop, tablet and mobile')
    finally:
        page.unroute('**/api/state*', state_response)
        page.evaluate("""async () => {
            $('provider-search').value='';$('provider-status-filter').value='';$('provider-page-size').value='25';
            providerPage=0;expandedProviders.clear();await loadState();
        }""")
        page.set_viewport_size({'width': 1440, 'height': 1000})


def check_large_subscription_list(page, checks):
    """Browse a large directory while live updates and detail navigation run."""
    fixture = page.evaluate('state')
    sample = fixture['subscriptions'][0]
    fixture.update(subscriptions=[], busy=False, refresh_running=False,
                   check_running=False, check_queued=False,
                   revision='synthetic-subscription-list')
    for index in range(200):
        kind = index % 4
        included = 200 - index if kind == 0 else 0
        fixture['subscriptions'].append({**sample,
            'id': 'default' if index == 0 else f'large-sub-{index}',
            'name': f'Subscription {index:03}',
            'all_providers': index == 0,
            'provider_ids': [] if kind == 3 else [fixture['providers'][0]['id']],
            'ready': kind != 1, 'published_total': included, 'total': 250,
            'counts': {key: included for key in sample['counts']},
            'generated_at': f'2026-01-01T00:{index // 60:02}:{index % 60:02}Z'})
    fixture['subscriptions'][1]['name'] += ' <private> ' + 'LongName' * 6

    def state_response(route):
        route.fulfill(json=fixture)

    page.route('**/api/state*', state_response)
    try:
        page.set_viewport_size({'width': 1440, 'height': 1000})
        page.evaluate('loadState()')
        page.locator('.nav-item[data-page=exports]').click()
        rows = page.locator('#subscription-list .subscription-row')
        expect(rows).to_have_count(25)
        expect(page.locator('#subscription-search-summary')).to_have_text('1–25 of 200 subscriptions')
        expect(page.locator('#subscription-previous')).to_be_disabled()
        expect(page.locator('#subscription-detail')).not_to_be_visible()
        page.locator('#subscription-next').click()
        expect(page.locator('#subscription-search-summary')).to_have_text('26–50 of 200 subscriptions')
        link = rows.nth(15).locator('.subscription-open')
        link.focus()
        scroll = page.evaluate('scrollY')
        page.keyboard.press('Enter')
        expect(page.locator('#subscription-detail-name')).to_have_text('Subscription 040')
        expect(page.locator('#subscription-detail-name')).to_be_focused()
        expect(page.locator('#subscription-list')).not_to_be_visible()
        page.locator('#subscription-menu summary').click()
        page.evaluate('loadState(true)')
        expect(page.locator('#rotate-links')).to_be_visible()
        page.keyboard.press('Escape')
        page.locator('#back-to-subscriptions').click()
        expect(link).to_be_focused()
        expect(page.locator('#subscription-page-label')).to_have_text('2 / 8')
        assert abs(page.evaluate('scrollY') - scroll) <= 2
        fixture['subscriptions'][40]['published_total'] -= 1
        page.evaluate('loadState(true)')
        expect(link).to_be_focused()
        expect(rows.nth(15).locator('.subscription-server-cell')).to_contain_text('159 included')
        checks.append('200-subscription pagination, keyboard detail navigation and return scroll/focus survive live updates')

        page.locator('#subscription-search').fill('sUbScRiPtIoN 199')
        expect(rows).to_have_count(1)
        expect(rows.first).to_contain_text('Subscription 199')
        expect(page.locator('#subscription-page-label')).to_have_text('1 / 1')
        page.locator('#subscription-search').fill(fixture['providers'][1]['name'])
        expect(rows).to_have_count(1)
        expect(rows.first.locator('.subscription-open')).to_have_attribute('data-subscription-id', 'default')
        page.locator('#subscription-search').fill('no such subscription')
        expect(page.locator('#subscriptions-no-results')).to_be_visible()
        expect(page.locator('#subscription-next')).to_be_disabled()
        expect(page.locator('#subscription-previous')).to_be_disabled()
        page.locator('#clear-subscription-search').click()
        expect(page.locator('#subscription-search')).to_be_focused()
        for status in ('ready', 'pending', 'empty', 'paused'):
            page.locator('#subscription-status-filter').select_option(status)
            expect(page.locator('#subscription-search-summary')).to_have_text('1–25 of 50 subscriptions · 200 total')
        page.locator('#subscription-search').fill('Subscription 199')
        expect(rows).to_have_count(1)
        page.locator('.nav-item[data-page=overview]').click()
        page.locator('.nav-item[data-page=exports]').click()
        expect(page.locator('#subscription-search')).to_have_value('Subscription 199')
        expect(page.locator('#subscription-status-filter')).to_have_value('paused')
        page.locator('#subscription-search').fill('Subscription 198')
        expect(page.locator('#subscriptions-no-results')).to_be_visible()
        page.locator('#clear-subscription-search').click()
        expect(page.locator('#subscription-status-filter')).to_have_value('')
        checks.append('subscription name/provider search, all-provider matching, status filters, empty results and navigation preserve filters')

        for sort, name in (('name-desc', 'Subscription 199'), ('servers', 'Subscription 000'),
                           ('updated', 'Subscription 199'), ('name', 'Subscription 000')):
            page.locator('#subscription-sort').select_option(sort)
            expect(rows.first.locator('.subscription-open > span').first).to_have_text(name)
        page.locator('#subscription-page-size').select_option('50')
        expect(rows).to_have_count(50)
        for _ in range(3):
            page.locator('#subscription-next').click()
        expect(page.locator('#subscription-search-summary')).to_have_text('151–200 of 200 subscriptions')
        expect(page.locator('#subscription-next')).to_be_disabled()
        saved = fixture['subscriptions']
        fixture['subscriptions'] = saved[:49]
        page.evaluate('loadState()')
        expect(rows).to_have_count(49)
        expect(page.locator('#subscription-search-summary')).to_have_text('1–49 of 49 subscriptions')
        expect(page.locator('#subscription-page-label')).to_have_text('1 / 1')
        fixture['subscriptions'] = saved
        page.evaluate('loadState()')
        page.locator('#subscription-page-size').select_option('100')
        expect(rows).to_have_count(100)
        page.locator('#subscription-page-size').select_option('25')
        rows.nth(1).locator('.subscription-view').click()
        fixture['subscriptions'] = [s for s in saved if s['id'] != 'large-sub-1']
        page.evaluate('loadState(true)')
        expect(page.locator('#subscription-detail')).not_to_be_visible()
        expect(page.locator('#subscription-list')).to_be_visible()
        expect(rows.first.locator('.subscription-open')).to_be_focused()
        fixture['subscriptions'] = saved
        page.evaluate('loadState()')
        checks.append('subscription sorting, 25/50/100 rows, page clamping and return to list when an open subscription is removed')

        for width in (320, 390, 768, 1000, 1440):
            page.set_viewport_size({'width': width, 'height': 1000})
            page.evaluate('window.scrollTo(0,0)')
            assert page.evaluate('document.body.scrollWidth <= innerWidth'), width
            assert page.locator('#subscription-table-wrap').evaluate('el => el.scrollWidth <= el.clientWidth'), width
            heights = rows.evaluate_all('rows => rows.map(row => row.getBoundingClientRect().height)')
            assert max(heights) <= (110 if width <= 760 else 76), (width, heights)
            if width in (390, 1440) and os.environ.get('PARSER_TEST_SCREENSHOTS'):
                page.screenshot(path=f"{os.environ['PARSER_TEST_SCREENSHOTS']}/subscriptions-200-{width}.png")
            rows.nth(1).locator('.subscription-open').focus()
            page.keyboard.press('Enter')
            expect(page.locator('#subscription-detail-name')).to_contain_text('<private>')
            expect(page.locator('#subscription-detail-name')).to_be_focused()
            assert page.evaluate('document.body.scrollWidth <= innerWidth'), width
            page.locator('#show-subscription-urls').check()
            expect(page.locator('.export-url').first).to_be_visible()
            assert page.evaluate('document.body.scrollWidth <= innerWidth'), width
            page.locator('#show-subscription-urls').uncheck()
            page.locator('#back-to-subscriptions').click()
            expect(rows.nth(1).locator('.subscription-open')).to_be_focused()
        checks.append('compact subscription rows, long names and details fit 320–1440px screens without horizontal scrolling')
    finally:
        page.unroute('**/api/state*', state_response)
        page.evaluate("""async () => {
            $('subscription-search').value='';$('subscription-status-filter').value='';
            $('subscription-sort').value='name';$('subscription-page-size').value='25';
            subscriptionPage=0;selectedSubscription='default';subscriptionListScroll=0;
            $('subscriptions-layout').classList.remove('show-detail');await loadState();
        }""")
        page.set_viewport_size({'width': 1440, 'height': 1000})


@contextmanager
def checked_browser(checks):
    engine = os.environ.get('PARSER_TEST_ENGINE', 'chromium')
    if engine not in ('chromium', 'firefox', 'webkit'):
        raise ValueError('PARSER_TEST_ENGINE must be chromium, firefox or webkit')
    artifacts = Path(os.environ.get('PARSER_TEST_ARTIFACTS', ROOT / 'artifacts/browser' / engine))
    artifacts.mkdir(parents=True, exist_ok=True)
    screenshots = Path(os.environ.setdefault('PARSER_TEST_SCREENSHOTS', str(artifacts / 'screenshots')))
    screenshots.mkdir(parents=True, exist_ok=True)
    errors = []
    status = 'failed'
    browser = context = page = None
    with sync_playwright() as browser_api:
        try:
            options = {'headless': True}
            if engine == 'chromium':
                options['args'] = ['--no-sandbox']
            if os.environ.get('PARSER_TEST_BROWSER'):
                options['executable_path'] = os.environ['PARSER_TEST_BROWSER']
            browser = getattr(browser_api, engine).launch(**options)
            context = browser.new_context(viewport={'width': 1440, 'height': 1000},
                permissions=['clipboard-read', 'clipboard-write'] if engine == 'chromium' else [])
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
            if engine != 'chromium':
                context.add_init_script('''
                    const write = navigator.clipboard.writeText.bind(navigator.clipboard);
                    navigator.clipboard.writeText = text => {
                        window.__testClipboardWrite = write(text).then(() => String(text));
                        return window.__testClipboardWrite.then(() => undefined);
                    };
                ''')
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            yield page, errors
            status = 'passed'
        except BaseException:
            if page:
                with suppress(Exception):
                    page.screenshot(path=str(artifacts / 'failure.png'), full_page=True, timeout=5000)
            raise
        finally:
            if context:
                with suppress(Exception):
                    context.tracing.stop(path=str(artifacts / 'trace.zip') if status == 'failed' else None)
            if browser:
                browser.close()
            report = {'tested_at': common.now(), 'browser': engine, 'status': status,
                      'checks': checks, 'passed': len(checks), 'page_errors': errors,
                      'clipboard_check': 'native-readback' if engine == 'chromium' else 'successful-write-capture'}
            common.atomic_write(artifacts / 'report.json', common.json_text(report))
            print(json.dumps(report, indent=2))


def main():
    os.umask(0o077)
    checks = []
    with tempfile.TemporaryDirectory(prefix="parser-browser-") as temporary:
        root = Path(temporary)
        seen = []
        failed_nodes = set()
        failed_checks = set()
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
                    tester=lambda node, core, *args: {'status': 'failed', 'reason': 'HTTPS check failed'}
                    if node.name in failed_nodes or (node.name, core) in failed_checks else {'status': 'passed', 'latency_ms': 12})
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
            with checked_browser(checks) as (page, errors):
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
                expect(page.locator('#provider-rows .provider-row')).to_have_count(2)
                expect(page.locator('#last-refresh')).to_contain_text('Providers downloaded')
                download_time = manager.snapshot['last_download']
                download_label = page.locator('#last-refresh').get_attribute('title')
                for row in page.locator('#provider-rows .provider-row').all():
                    expect(row.locator('.server-count')).to_have_text('2')
                    expect(row.locator('.provider-working')).to_have_text('0 working')
                    expect(row.locator('.provider-check-note')).to_have_text('Awaiting checks')
                page.locator('.nav-item[data-page=servers]').click()
                page.locator('#check-servers').click()
                expect(page.locator('#connection-summary')).to_contain_text('2 of 2 nodes included',timeout=15000)
                expect(page.locator('#server-rows')).to_contain_text('Passed')
                expect(page.get_by_role('columnheader', name='HIDDIFY CHECK')).to_have_count(1)
                expect(page.locator('#server-rows tr').first.locator('td').nth(5)).to_contain_text('Passed')
                expect(page.locator('#server-total')).to_have_text('2')
                expect(page.locator('#protocol-filter')).not_to_contain_text('LOOPBACK')
                expect(page.locator('#connection-summary')).to_contain_text('0 excluded')
                assert len(seen)==2
                check_server_details(page, checks)
                checks.append('cached connection checks publish passing nodes without another provider fetch')
                page.locator('.nav-item[data-page=overview]').click()
                expect(page.locator('#stat-servers')).to_have_text('2', timeout=15000)
                expect(page.locator('#last-refresh')).to_contain_text('Providers downloaded')
                expect(page.locator('#last-refresh')).to_have_attribute('title', download_label)
                expect(page.locator('#last-connection-check')).to_contain_text('Connections checked')
                expect(page.locator('#stat-next')).to_contain_text('Next download at')
                assert manager.snapshot['last_download'] == download_time
                assert manager.snapshot['last_check']
                checks.append('provider download and connection-check timestamps remain separate')
                assert seen==['saved-browser-hwid','second-browser-hwid']
                checks.append('explicit per-link devices, test-before-save and reuse of tested responses')

                for row in page.locator('#provider-rows .provider-row').all():
                    expect(row.locator('.provider-working')).to_have_text('2 working')
                    expect(provider_details(row).locator('.provider-client-counts strong')).to_have_text(['2'] * 5)
                    expect(provider_details(row).locator('.provider-client-counts')).not_to_be_visible()
                first_row = page.locator('#provider-rows .provider-row').first
                first_row.locator('.provider-expand').focus()
                page.keyboard.press('Enter')
                expect(provider_details(first_row).locator('.provider-client-counts')).to_be_visible()
                page.evaluate('loadState()')
                expect(provider_details(first_row).locator('.provider-client-counts')).to_be_visible()
                expect(first_row.locator('.provider-expand')).to_be_focused()
                first_row.locator('.provider-actions summary').focus()
                page.keyboard.press('Enter')
                page.keyboard.press('Tab')
                expect(first_row.locator('[data-action=refresh]')).to_be_focused()
                page.evaluate('loadState()')
                expect(first_row.locator('[data-action=refresh]')).to_be_focused()
                page.keyboard.press('Escape')
                expect(first_row.locator('.provider-actions summary')).to_be_focused()
                expect(first_row.locator('.action-menu-items')).not_to_be_visible()
                first_row.locator('.provider-actions summary').click()
                page.locator('#page-title').click()
                expect(first_row.locator('.action-menu-items')).not_to_be_visible()
                checks.append('provider details and actions stay usable through polling, keyboard navigation and outside clicks')
                saved_connectivity = copy.deepcopy(manager.snapshot['connectivity'])
                for cores in manager.snapshot['connectivity'].values():
                    for result in cores.values():
                        result['checked_ts'] = time.time() - 1201
                manager.snapshot['valid_until'] = time.time() - 1
                page.reload()
                expect(page.locator('#provider-rows .provider-row')).to_have_count(2)
                for row in page.locator('#provider-rows .provider-row').all():
                    expect(row.locator('.provider-working')).to_have_text('0 working')
                    expect(row.locator('.server-count')).to_have_text('2')
                    expect(provider_details(row).locator('.provider-client-counts strong')).to_have_text(['0'] * 5)
                    expect(row.locator('.provider-check-note')).to_have_text('Checks expired')
                manager.snapshot['connectivity'] = saved_connectivity
                manager.refresh(download=False, check=False)
                page.reload()
                expect(page.locator('#provider-rows .provider-row').first.locator('.provider-working')).to_have_text('2 working')
                page.locator('.nav-item[data-page=providers]').click()
                if os.environ.get('PARSER_TEST_SCREENSHOTS'):
                    page.screenshot(path=os.environ['PARSER_TEST_SCREENSHOTS']+'/providers-desktop.png',full_page=True)
                page.set_viewport_size({'width':390,'height':844})
                assert page.evaluate('document.body.scrollWidth <= innerWidth')
                assert page.locator('#providers-section .table-scroll').evaluate('el => el.scrollWidth <= el.clientWidth')
                if os.environ.get('PARSER_TEST_SCREENSHOTS'):
                    page.screenshot(path=os.environ['PARSER_TEST_SCREENSHOTS']+'/providers-mobile.png',full_page=True)
                first_row.locator('.provider-expand').click()
                expect(provider_details(first_row).locator('.provider-client-counts')).to_be_visible()
                first_row.locator('.provider-actions summary').click()
                expect(first_row.locator('[data-action=edit]')).to_be_in_viewport()
                assert page.evaluate('document.body.scrollWidth <= innerWidth')
                if os.environ.get('PARSER_TEST_SCREENSHOTS'):
                    page.screenshot(path=os.environ['PARSER_TEST_SCREENSHOTS']+'/providers-expanded-mobile.png',full_page=True)
                page.keyboard.press('Escape')
                first_row.locator('.provider-expand').click()
                page.locator('#provider-rows .provider-row').filter(has_text='Demo 2').locator('.provider-working').click()
                expect(page.locator('#page-title')).to_have_text('Server library')
                expect(page.locator('#provider-filter option:checked')).to_have_text('Demo 2')
                expect(page.locator('#connection-filter')).to_have_value('passing')
                expect(page.locator('#server-rows tr')).to_have_count(2)
                page.set_viewport_size({'width':1440,'height':1000})
                failed_checks.add(('Tokyo', 'xray'))
                page.locator('#check-servers').click()
                expect(page.locator('#check-servers')).to_be_enabled(timeout=15000)
                page.locator('.nav-item[data-page=providers]').click()
                for row in page.locator('#provider-rows .provider-row').all():
                    expect(row.locator('.provider-working')).to_have_text('2 working')
                    expect(provider_details(row).locator('.provider-client-counts strong')).to_have_text(['2', '2', '1', '2', '2'])
                failed_checks.clear()
                page.locator('.nav-item[data-page=servers]').click()
                page.locator('#provider-filter').select_option('')
                page.locator('#connection-filter').select_option('')
                page.locator('#check-servers').click()
                expect(page.locator('#server-rows tr').nth(1).locator('td').nth(4)).to_contain_text('Passed',timeout=15000)
                assert len(seen)==2
                checks.append('provider working totals, shared nodes, per-client results, pending/expired checks and mobile drill-down')

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
                assert read_clipboard(page)=='saved-browser-hwid'
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
                no_device=page.locator('#provider-rows .provider-row').filter(has_text='No device provider')
                expect(provider_details(no_device)).to_contain_text('No device ID')
                no_device.locator('.provider-actions summary').click()
                page.once('dialog',lambda dialog:dialog.accept())
                no_device.locator('[data-action=delete]').click()
                expect(page.locator('#provider-rows .provider-row')).to_have_count(2)
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
                page.locator('.nav-item[data-page=providers]').click()
                expect(page.locator('#provider-rows .provider-row').filter(has_text='Demo 2').locator('.provider-working')).to_have_text('1 working')
                expect(page.locator('#provider-rows .provider-row').filter(has_text='Demo 2').locator('.server-count')).to_have_text('2')
                page.locator('.nav-item[data-page=servers]').click()
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
                page.locator('#subscription-list [data-subscription-id=default]').click()
                page.locator('[data-copy="hiddify.json"]').click()
                expect(page.locator('#toast')).to_contain_text('copied')
                copied = read_clipboard(page)
                response = page.request.get(copied)
                assert response.status == 200
                assert len(response.json()['outbounds']) == 2
                checks.append('copy a usable subscription link and download combined JSON')

                page.locator('[data-copy="v2board.json"]').click()
                expect(page.locator('#toast')).to_contain_text('copied')
                native_link = read_clipboard(page)
                response = page.request.get(native_link)
                assert response.status == 200
                assert response.json()['format'] == 'subscription-parser'
                assert len(response.json()['nodes']) == 2
                checks.append('copy and fetch the native V2Board JSON subscription')

                page.locator('[data-copy="v2board.txt"]').click()
                expect(page.locator('#toast')).to_contain_text('copied')
                v2board_link = read_clipboard(page)
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
                expect(page.locator('#provider-rows .provider-row').first.locator('.server-count')).to_have_text('2', timeout=15000)
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
                phone_link = read_clipboard(page)
                phone_archive = page.request.get(phone_link).json()
                assert {source for node in phone_archive['servers'] for source in node['sources']} == {providers[0]['name']}
                page.locator('#back-to-subscriptions').click()
                page.locator('#subscription-search').fill('my phone')
                page.locator('#subscription-status-filter').select_option('ready')
                page.locator('[data-subscription-id="'+phone_id+'"]').click()
                page.locator('#edit-subscription').click()
                page.locator('#subscription-provider-choices input[value="'+providers[1]['id']+'"]').check()
                page.locator('#save-subscription').click()
                expect(page.locator('#subscription-dialog')).not_to_be_visible()
                expect(page.locator('#subscription-providers-summary')).to_contain_text(providers[1]['name'])
                assert {source for node in page.request.get(phone_link).json()['servers'] for source in node['sources']} == {p['name'] for p in providers}
                page.locator('#back-to-subscriptions').click()
                expect(page.locator('#subscription-search')).to_have_value('my phone')
                expect(page.locator('#subscription-status-filter')).to_have_value('ready')
                page.locator('#subscription-search').fill('no such subscription')
                page.locator('#clear-subscription-search').click()
                checks.append('create scoped subscriptions, require a provider and edit selections without changing links')

                page.locator('#add-subscription').click()
                page.locator('#subscription-name').fill('Family')
                page.locator('#subscription-all').check()
                expect(page.locator('#subscription-provider-choices input').first).to_be_disabled()
                page.locator('#save-subscription').click()
                expect(page.locator('#subscription-dialog')).not_to_be_visible()
                expect(page.locator('#subscription-total')).to_have_text('3')
                expect(page.locator('#subscription-detail-name')).to_have_text('Family')
                family_id = page.locator('#subscription-list [aria-current=true]').get_attribute('data-subscription-id')
                page.locator('[data-copy="v2board.txt"]').click()
                family_link = read_clipboard(page)
                assert family_link != legacy_link
                page.locator('#back-to-subscriptions').click()
                page.locator('#subscription-search').fill('my phone')
                expect(page.locator('#subscription-list .subscription-row')).to_have_count(1)
                page.locator('#subscription-search').fill('no such subscription')
                expect(page.locator('#subscriptions-no-results')).to_be_visible()
                page.locator('#clear-subscription-search').click()
                expect(page.locator('#subscription-list .subscription-row')).to_have_count(3)
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
                check_large_subscription_list(page, checks)
                check_subscription_server_policy(page, manager, checks)
                check_large_server_policy(page, checks)

                page.locator('.nav-item[data-page=providers]').click()
                limited_row = page.locator('#provider-rows .provider-row').filter(has_text='Demo 2')
                before_limit_fetches = len(seen)
                limited_row.locator('.provider-actions summary').click()
                limited_row.locator('[data-action=edit]').click()
                expect(page.locator('#provider-server-limit')).to_have_value('')
                page.locator('#provider-server-limit').fill('0')
                expect(page.locator('#save-provider')).to_be_disabled()
                page.locator('#provider-server-limit').fill('1')
                expect(page.locator('#save-provider')).to_be_enabled()
                page.locator('#save-provider').click()
                expect(page.locator('#provider-dialog')).not_to_be_visible()
                expect(limited_row.locator('.server-count')).to_have_text('1')
                expect(limited_row.locator('.provider-working')).to_have_text('1 working')
                expect(provider_details(limited_row).locator('.provider-sample-note')).to_have_text('2 fetched · 1 selected · random limit 1')
                assert len(seen) == before_limit_fetches
                page.reload()
                expect(limited_row.locator('.server-count')).to_have_text('1')
                page.set_viewport_size({'width':390, 'height':844})
                limited_row.locator('.provider-actions summary').click()
                limited_row.locator('[data-action=edit]').click()
                expect(page.locator('#provider-server-limit')).to_have_value('1')
                page.locator('#provider-server-limit').scroll_into_view_if_needed()
                expect(page.locator('#provider-server-limit')).to_be_in_viewport()
                expect(page.locator('#provider-server-limit-hint')).to_contain_text('new random selection')
                if os.environ.get('PARSER_TEST_SCREENSHOTS'):
                    page.screenshot(path=os.environ['PARSER_TEST_SCREENSHOTS']+'/provider-limit-mobile.png',full_page=True)
                page.locator('#provider-server-limit').fill('')
                page.locator('#save-provider').click()
                expect(page.locator('#provider-dialog')).not_to_be_visible()
                expect(limited_row.locator('.server-count')).to_have_text('2')
                expect(provider_details(limited_row).locator('.provider-sample-note')).to_have_text('2 fetched · 2 selected · all servers')
                assert len(seen) == before_limit_fetches
                page.set_viewport_size({'width':1440, 'height':1000})
                checks.append('per-provider random server limits validate, persist, show fetched/selected counts, and clear on mobile without another fetch')

                page.locator('.nav-item[data-page=providers]').click()
                page.locator('.provider-actions summary').first.click()
                page.locator('[data-action=edit]').first.click()
                page.locator('#provider-name').fill('Renamed provider')
                page.locator('#save-provider').click()
                expect(page.locator('#provider-dialog')).not_to_be_visible()
                expect(page.locator('#provider-rows')).to_contain_text('Renamed provider')
                expect(page.locator('#refresh-button')).to_be_enabled(timeout=15000)
                paused_provider=page.locator('#provider-rows .provider-row').filter(has_text='Demo 2')
                paused_provider.locator('.provider-actions summary').click()
                paused_provider.locator('[data-action=toggle]').click()
                expect(paused_provider).to_contain_text('Paused')
                expect(paused_provider.locator('.provider-working')).to_have_text('0 working')
                expect(provider_details(paused_provider).locator('.provider-client-counts strong')).to_have_text(['0'] * 5)
                paused_provider.locator('.provider-actions summary').click()
                page.once('dialog', lambda dialog: dialog.accept())
                paused_provider.locator('[data-action=delete]').click()
                expect(page.locator('#provider-rows .provider-row')).to_have_count(1)
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

                # Hold each phase to observe real API polling and page changes,
                # without contacting any external provider/server.
                extra = {'name': 'Unchanged provider', 'url': 'https://unchanged.example/sub', 'hwid_id': None}
                preview = manager.test_provider(extra)
                extra_provider = manager.change_provider({**extra, 'test_token': preview['test_token']})
                original_fetcher, original_checker, original_build = manager.fetcher, manager.checker, publication.build
                fetch_started, release_fetch = threading.Event(), threading.Event()
                check_started, release_check = threading.Event(), threading.Event()
                publish_started, release_publish = threading.Event(), threading.Event()
                reports = []
                def held_fetcher(*args, **kwargs):
                    fetch_started.set()
                    if not release_fetch.wait(45):
                        raise RuntimeError('synthetic provider fetch was not released')
                    return original_fetcher(*args, **kwargs)
                def held_checker(*args, **kwargs):
                    reports.append(kwargs['progress'])
                    check_started.set()
                    if not release_check.wait(45):
                        raise RuntimeError('synthetic provider check was not released')
                    return original_checker(*args, **kwargs)
                def held_build(*args, **kwargs):
                    publish_started.set()
                    if not release_publish.wait(45):
                        raise RuntimeError('synthetic publication was not released')
                    return original_build(*args, **kwargs)
                manager.fetcher = held_fetcher
                manager.checker = held_checker
                publishing = patch.object(publication, 'build', side_effect=held_build)
                publishing.start()
                before_fetch = len(seen)
                schedules = (manager.next_refresh, manager.next_check)
                try:
                    page.set_viewport_size({'width':1440, 'height':1000})
                    page.locator('.nav-item[data-page=overview]').click()
                    page.evaluate('loadState()')
                    active = page.locator('#provider-rows .provider-row').filter(has_text='Renamed provider')
                    inactive = page.locator('#provider-rows .provider-row').filter(has_text='Unchanged provider')
                    active.locator('.provider-actions summary').click()
                    active.locator('[data-action=refresh]').click()
                    assert fetch_started.wait(5)
                    expect(page.locator('#job-progress')).to_be_visible()
                    expect(page.locator('#job-progress-title')).to_have_text('Refreshing Renamed provider')
                    expect(page.locator('#job-progress-percent')).to_have_text('In progress')
                    assert page.locator('#job-progress-bar').get_attribute('aria-valuenow') is None
                    expect(active.locator('.provider-check-note')).to_have_text('Refreshing…')
                    expect(inactive.locator('.provider-check-note')).to_have_text('Ready')
                    expect(page.locator('.refresh-label')).to_have_text('Refresh all')
                    expect(page.locator('#refresh-button.spinning')).to_have_count(0)
                    page.evaluate('loadState(true)')
                    expect(active.locator('.provider-check-note')).to_have_text('Refreshing…')
                    expect(inactive.locator('.provider-check-note')).to_have_text('Ready')
                    page.reload()
                    expect(page.locator('#page-title')).to_have_text('Overview')
                    expect(active.locator('.provider-check-note')).to_have_text('Refreshing…')
                    expect(inactive.locator('.provider-check-note')).to_have_text('Ready')
                    release_fetch.set()
                    assert check_started.wait(5)
                    expect(page.locator('#job-progress-title')).to_have_text('Checking connections · Renamed provider')
                    expect(page.locator('#job-progress-detail')).to_contain_text('Preparing saved servers')
                    assert page.locator('#job-progress-bar').get_attribute('aria-valuenow') is None
                    reports[0](0, 6)
                    expect(page.locator('#job-progress-bar')).to_have_attribute('aria-valuenow', '0')
                    reports[0](2, 6)
                    expect(page.locator('#job-progress-detail')).to_contain_text('2 of 6 client checks processed')
                    expect(page.locator('#job-progress-percent')).to_have_text('33%')
                    expect(page.get_by_role('progressbar', name='Checking connections · Renamed provider')).to_have_attribute('aria-valuenow', '33')
                    expect(page.locator('#last-refresh')).to_contain_text('Providers downloaded')
                    expect(page.locator('#last-connection-check')).to_contain_text('Connections checked')
                    expect(page.locator('.refresh-label')).to_have_text('Refresh all')
                    expect(page.locator('#refresh-button.spinning')).to_have_count(0)
                    expect(active.locator('.provider-check-note')).to_have_text('Checking…')
                    expect(inactive.locator('.provider-check-note')).to_have_text('Ready')
                    assert len(seen) == before_fetch + 1
                    assert seen[-1] == 'replacement-browser-hwid'
                    for name in ('overview', 'servers', 'exports', 'activity', 'devices', 'settings', 'providers'):
                        page.locator('.nav-item[data-page='+name+']').click()
                        expect(page.locator('#job-progress')).to_be_visible()
                    page.reload()
                    expect(page.locator('#job-progress-detail')).to_contain_text('2 of 6 client checks processed')
                    page.locator('.nav-item[data-page=providers]').click()
                    for width in (320, 390, 768, 1440):
                        page.set_viewport_size({'width':width, 'height':1000})
                        expect(page.locator('#job-progress')).to_be_visible()
                        assert page.evaluate('document.body.scrollWidth <= innerWidth')
                        if width in (390, 1440) and os.environ.get('PARSER_TEST_SCREENSHOTS'):
                            page.screenshot(path=os.environ['PARSER_TEST_SCREENSHOTS']+f'/progress-{width}.png', full_page=True)
                    reports[0](5, 6)
                    expect(page.locator('#job-progress-percent')).to_have_text('83%')
                    release_check.set()
                    assert publish_started.wait(5)
                    expect(page.locator('#job-progress-title')).to_have_text('Updating subscriptions')
                    expect(page.locator('#job-progress-percent')).to_have_text('In progress')
                    assert page.locator('#job-progress-bar').get_attribute('aria-valuenow') is None
                finally:
                    release_fetch.set()
                    release_check.set()
                    release_publish.set()
                    manager.wait_for_idle(10)
                    manager.fetcher = original_fetcher
                    manager.checker = original_checker
                    publishing.stop()
                assert not manager.busy
                assert (manager.next_refresh, manager.next_check) == schedules
                expect(page.locator('#job-progress')).not_to_be_visible()
                expect(page.locator('#activity-rows')).to_contain_text('Renamed provider refreshed')
                expect(page.locator('#activity-rows .activity-row').first).to_contain_text('Connections checked')
                checks.append('live refresh/check progress follows every phase, survives navigation and reload, and fits mobile screens')

                # A standalone check uses the same progress display and clears
                # it on failure, without implying a new provider download.
                check_started.clear(); release_check.clear(); reports.clear()
                manager.checker = held_checker
                try:
                    page.locator('.nav-item[data-page=servers]').click()
                    page.locator('#check-servers').click()
                    assert check_started.wait(5)
                    reports[0](3, 6)
                    expect(page.locator('#job-progress-title')).to_have_text('Checking connections')
                    expect(page.locator('#job-progress-bar')).to_have_attribute('aria-valuenow', '50')
                    expect(page.locator('#connection-summary')).to_contain_text('3 of 6 client checks processed')
                    assert len(seen) == before_fetch + 1
                finally:
                    release_check.set()
                    manager.wait_for_idle(10)
                    manager.checker = original_checker
                expect(page.locator('#job-progress')).not_to_be_visible()
                with patch.object(manager, 'checker', side_effect=RuntimeError('synthetic checker failure')):
                    page.locator('#check-servers').click()
                    manager.wait_for_idle(10)
                    expect(page.locator('#global-error')).to_contain_text('connection check failed')
                    expect(page.locator('#job-progress')).not_to_be_visible()
                checks.append('standalone connection checks show live counts and failed work clears the progress indicator')

                # The hourly job can follow a manual refresh. Its rows identify
                # the schedule and stop refreshing as each download completes.
                fetch_started.clear(); release_fetch.clear()
                extra_started, release_extra = threading.Event(), threading.Event()
                def scheduled_fetcher(*args, **kwargs):
                    if args[0] == extra['url']:
                        extra_started.set()
                        if not release_extra.wait(45):
                            raise RuntimeError('synthetic scheduled fetch was not released')
                        return original_fetcher(*args, **kwargs)
                    return held_fetcher(*args, **kwargs)
                manager.fetcher = scheduled_fetcher
                try:
                    page.set_viewport_size({'width': 1440, 'height': 1000})
                    page.locator('.nav-item[data-page=overview]').click()
                    manager.next_refresh = 0
                    with patch.object(manager.stop, 'wait', side_effect=[False, True]):
                        manager.scheduler()
                    assert fetch_started.wait(5) and extra_started.wait(5)
                    page.evaluate('loadState(true)')
                    expect(page.locator('#job-progress-title')).to_have_text('Scheduled refresh · all providers')
                    expect(active.locator('.provider-check-note')).to_have_text('Scheduled refresh…')
                    expect(inactive.locator('.provider-check-note')).to_have_text('Scheduled refresh…')
                    release_fetch.set()
                    expect(active.locator('.provider-check-note')).not_to_have_text('Scheduled refresh…')
                    expect(inactive.locator('.provider-check-note')).to_have_text('Scheduled refresh…')
                    page.reload()
                    expect(page.locator('#job-progress-title')).to_have_text('Scheduled refresh · all providers')
                    expect(active.locator('.provider-check-note')).not_to_have_text('Scheduled refresh…')
                    expect(inactive.locator('.provider-check-note')).to_have_text('Scheduled refresh…')
                    for width in (320, 390, 768, 1440):
                        page.set_viewport_size({'width': width, 'height': 1000})
                        assert page.evaluate('document.body.scrollWidth <= innerWidth')
                finally:
                    release_fetch.set(); release_extra.set()
                    manager.wait_for_idle(10)
                    manager.fetcher = original_fetcher
                expect(page.locator('#job-progress')).not_to_be_visible()
                expect(page.locator('#activity-rows')).to_contain_text('Scheduled provider refresh completed')
                checks.append('scheduled refreshes have distinct overview labels and only unfinished providers stay refreshing after polling and reload')
                manager.change_provider(provider_id=extra_provider['id'], delete=True)
                page.evaluate('loadState()')
                checks.append('single-provider refresh keeps other rows idle, fetches only its saved device and preserves global schedules')

                # Both jobs remain visible and independently controllable.
                fetch_started.clear(); release_fetch.clear()
                check_started.clear(); release_check.clear(); reports.clear()
                manager.fetcher, manager.checker = held_fetcher, held_checker
                try:
                    page.locator('.nav-item[data-page=servers]').click()
                    page.locator('#check-servers').click()
                    assert check_started.wait(5)
                    reports[0](2, 6)
                    expect(page.locator('#refresh-button')).to_be_enabled()
                    page.locator('#refresh-button').click()
                    assert fetch_started.wait(5)
                    expect(page.locator('#refresh-progress')).to_be_visible()
                    expect(page.locator('#refresh-progress-title')).to_have_text('Refreshing all providers')
                    expect(page.locator('#job-progress-title')).to_have_text('Checking connections')
                    expect(page.locator('#job-progress-detail')).to_contain_text('2 of 6 client checks processed')
                    expect(page.locator('#refresh-button')).to_be_disabled()
                    expect(page.locator('#check-servers')).to_be_disabled()
                    page.reload()
                    expect(page.locator('#refresh-progress')).to_be_visible()
                    expect(page.locator('#job-progress')).to_be_visible()
                    for width in (320, 390, 768, 1440):
                        page.set_viewport_size({'width': width, 'height': 1000})
                        assert page.evaluate('document.body.scrollWidth <= innerWidth')
                    release_fetch.set()
                    manager.refresh_worker.join(10)
                    assert not manager.refresh_running and manager.check_running
                    expect(page.locator('#refresh-progress')).not_to_be_visible()
                    expect(page.locator('#refresh-button')).to_be_enabled()
                    expect(page.locator('#job-progress-title')).to_have_text('Checking connections')
                finally:
                    release_fetch.set(); release_check.set()
                    manager.wait_for_idle(10)
                    manager.fetcher, manager.checker = original_fetcher, original_checker
                assert not manager.busy
                checks.append('independent download and check controls, concurrent progress, reload and mobile layout')

                check_large_provider_list(page, checks)

                # Advance only the synthetic authentication clock, without
                # sleeping or touching real devices / production enrollment.
                auth_time = [time.time()]
                app.state.auth.clock = lambda: auth_time[0]
                page.set_viewport_size({'width':1440, 'height':1000})
                page.locator('.nav-item[data-page=settings]').click()
                expect(page.locator('#two-factor-status')).to_have_text('Not enabled')
                page.locator('#setup-two-factor').click()
                page.locator('#security-key').fill('wrong-key')
                page.locator('#security-submit').click()
                expect(page.locator('#security-dialog-error')).to_contain_text('incorrect admin key')
                page.locator('#security-key').fill(admin)
                page.locator('#security-submit').click()
                expect(page.locator('#security-enrollment')).to_be_visible()
                seed = page.locator('#security-secret').input_value()
                expect(page.locator('#security-key')).to_have_value('')
                assert page.locator('#security-qr').evaluate('img => img.complete && img.naturalWidth > 0')
                for width in (320, 390, 768, 1440):
                    page.set_viewport_size({'width':width, 'height':1000})
                    assert page.evaluate('document.body.scrollWidth <= innerWidth')
                if os.environ.get('PARSER_TEST_SCREENSHOTS'):
                    page.screenshot(path=os.environ['PARSER_TEST_SCREENSHOTS']+'/auth-setup-desktop.png',full_page=True)
                code = pyotp.TOTP(seed).at(auth_time[0])
                page.locator('#security-confirm-code').fill('000000' if code != '000000' else '111111')
                page.locator('#security-submit').click()
                expect(page.locator('#security-dialog-error')).to_contain_text('incorrect verification code')
                page.locator('#security-confirm-code').fill(code)
                page.locator('#security-submit').click()
                expect(page.locator('#recovery-dialog')).to_be_visible()
                recovery = page.locator('#recovery-codes').inner_text().splitlines()
                assert len(recovery) == 10
                expect(page.locator('#security-secret')).to_have_value('')
                page.locator('#copy-recovery').click()
                assert read_clipboard(page) == '\n'.join(recovery)
                for width in (320, 390, 1440):
                    page.set_viewport_size({'width':width, 'height':1000})
                    assert page.evaluate('document.body.scrollWidth <= innerWidth')
                page.locator('[data-close=recovery-dialog]').click()
                expect(page.locator('#recovery-codes')).to_have_text('')
                expect(page.locator('#two-factor-status')).to_have_text('Enabled')
                expect(page.locator('#security-reminder')).not_to_be_visible()
                checks.append('2FA setup verifies the key and code, renders a local QR and shows recovery codes once')

                page.locator('#logout-button').click()
                # A password manager or restored form can populate a hidden
                # field. It must never skip the prompt for the second factor.
                expect(page.locator('#login-factor')).not_to_be_visible()
                page.locator('#login-code').evaluate('(input, value) => input.value = value', code)
                page.locator('#admin-key').fill(admin)
                with page.expect_response('**/api/login') as response:
                    page.locator('#login-submit').click()
                assert response.value.request.post_data_json == {'token': admin, 'code': ''}
                assert response.value.json() == {'authenticated': False, 'requires_two_factor': True}
                expect(page.locator('#login-factor')).to_be_visible()
                expect(page.locator('#workspace')).not_to_be_visible()
                expect(page.locator('#login-code')).to_be_enabled()
                expect(page.locator('#login-code')).to_have_value('')
                expect(page.locator('#login-error')).to_have_text('')
                expect(page.locator('#login-factor')).to_contain_text('Admin key accepted')
                expect(page.locator('#login-submit')).to_contain_text('Verify')
                checks.append('hidden autofilled codes are ignored and the second-factor prompt is explicit')
                page.locator('#login-code').fill(code)
                page.locator('#login-submit').click()
                expect(page.locator('#login-error')).to_contain_text('verification code is incorrect')
                expect(page.locator('#login-error')).not_to_contain_text('admin key')
                auth_time[0] += 30
                page.locator('#login-code').fill(pyotp.TOTP(seed).at(auth_time[0]))
                page.locator('#login-submit').click()
                expect(page.locator('#workspace')).to_be_visible()
                expect(page.locator('#login-code')).to_have_value('')
                checks.append('two-step login requires a fresh authenticator code and rejects replay')

                auth_time[0] += 301
                page.locator('#replace-recovery').click()
                page.locator('#security-key').fill(admin)
                page.locator('#security-code').fill(pyotp.TOTP(seed).at(auth_time[0]))
                page.locator('#security-submit').click()
                expect(page.locator('#recovery-dialog')).to_be_visible()
                replacement_codes = page.locator('#recovery-codes').inner_text().splitlines()
                assert set(recovery).isdisjoint(replacement_codes)
                page.locator('[data-close=recovery-dialog]').click()
                page.locator('#logout-button').click()
                expect(page.locator('#login-code')).to_be_disabled()
                expect(page.locator('#login-submit')).to_contain_text('Open workspace')
                page.locator('#admin-key').fill(admin)
                page.locator('#login-submit').click()
                expect(page.locator('#login-factor')).to_be_visible()
                page.locator('#login-code').fill(replacement_codes[0])
                page.locator('#login-submit').click()
                expect(page.locator('#workspace')).to_be_visible()
                expect(page.locator('#two-factor-description')).to_contain_text('9 recovery codes remaining')
                checks.append('recovery replacement and one-use recovery login work through the dashboard')
                for width in (320, 390, 768, 1440):
                    page.set_viewport_size({'width':width, 'height':1000})
                    assert page.evaluate('document.body.scrollWidth <= innerWidth')
                    if os.environ.get('PARSER_TEST_SCREENSHOTS') and width in (390, 1440):
                        page.screenshot(path=os.environ['PARSER_TEST_SCREENSHOTS']+f'/security-{width}.png',full_page=True)

                page.locator('#disable-two-factor').click()
                page.locator('#security-key').fill(admin)
                page.locator('#security-code').fill('bad-code')
                page.locator('#security-submit').click()
                expect(page.locator('#security-dialog-error')).to_contain_text('incorrect')
                page.locator('#security-code').fill(replacement_codes[1])
                page.locator('#security-submit').click()
                expect(page.locator('#security-dialog')).not_to_be_visible()
                expect(page.locator('#two-factor-status')).to_have_text('Not enabled')
                checks.append('disabling 2FA requires both factors and updates account security status')

                page.locator('#logout-button').click()
                expect(page.locator('#login-view')).to_be_visible()
                checks.append('sign out')
                auth_time[0] += 301
                page.locator('#admin-key').fill('wrong-key')
                for _ in range(10):
                    with page.expect_response('**/api/login') as response:
                        page.locator('#login-submit').click()
                    assert response.value.status == 401
                page.locator('#login-submit').click()
                expect(page.locator('#login-error')).to_contain_text('too many authentication attempts')
                expect(page.locator('#login-error')).to_contain_text('seconds')
                checks.append('login throttling shows a clear retry time without opening the dashboard')
                assert not errors, errors
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == '__main__':
    main()
