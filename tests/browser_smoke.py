"""Exercise the simplified workspace against an explicitly started demo server.

Example: .venv/bin/python tests/browser_smoke.py http://127.0.0.1:8081
"""

import argparse
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def assert_workspace_fits(page, label):
    """Primary camera actions stay in the viewport, even with a long control tab."""
    result = page.evaluate("""() => {
        const selectors = ['#camera-gallery', '[data-camera-socket]', '#capture-selected',
                           '#capture-all', '#capture-format', '#apply-controls',
                           '#open-setup', '#open-captures', '#stop-button'];
        const outside = selectors.filter(selector => {
            const el = document.querySelector(selector);
            if (!el) return true;
            const r = el.getBoundingClientRect();
            return r.width <= 0 || r.height <= 0 || r.left < -1 || r.top < -1 ||
                   r.right > innerWidth + 1 || r.bottom > innerHeight + 1;
        });
        return {outside, width: document.documentElement.scrollWidth,
                height: document.documentElement.scrollHeight, viewport: [innerWidth, innerHeight]};
    }""")
    assert not result['outside'], (label, result)
    assert result['width'] <= result['viewport'][0] + 1, (label, result)
    assert result['height'] <= result['viewport'][1] + 1, (label, result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url')
    parser.add_argument('--chromium', default='/usr/bin/chromium')
    parser.add_argument('--output', type=Path, default=Path('test-results'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.chromium, headless=True,
                                   args=['--no-sandbox', '--disable-dev-shm-usage'])
        context = browser.new_context(viewport={'width': 1366, 'height': 768})
        status = context.request.get(args.url + '/api/status').json()
        assert status['demo'], 'Browser smoke test must run against --demo, never real hardware.'
        context.request.post(args.url + '/api/stop', data={})
        errors = []
        page = context.new_page()
        page.on('pageerror', lambda exc: errors.append(str(exc)))
        page.goto(args.url, wait_until='domcontentloaded')
        expect(page).to_have_title('OAK FFC TEST')
        expect(page.locator('#demo-banner')).to_be_visible()
        expect(page.locator('.camera-config')).to_have_count(3, timeout=20000)
        expect(page.locator('#setup-dialog')).to_be_visible()
        expect(page.locator('#start-button')).to_be_visible()
        expect(page.locator('#stop-button')).to_be_hidden()

        # Native dialogs close on Escape and return focus to their launcher.
        page.keyboard.press('Escape')
        expect(page.locator('#setup-dialog')).not_to_be_visible()
        page.locator('#open-setup').click()
        expect(page.locator('#setup-dialog')).to_be_visible()
        page.locator('[data-preset="2"]').click()
        camera_a = page.locator('.camera-config[data-socket="CAM_A"]')
        camera_d = page.locator('.camera-config[data-socket="CAM_D"]')
        camera_a.locator('.resolution-select').select_option('1080p')
        camera_d.locator('.resolution-select').select_option('4k')
        camera_a.locator('.fps-input').fill('7')
        camera_d.locator('.fps-input').fill('11')
        expect(page.locator('#setup-start-button')).to_be_disabled()
        expect(page.locator('#warnings')).to_contain_text('matching sensor resolutions')
        expect(page.locator('#match-resolutions')).to_be_enabled()
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Setup horizontal overflow'
        page.locator('#match-resolutions').click()
        for camera, fps in ((camera_a, '7'), (camera_d, '11')):
            expect(camera.locator('.resolution-select')).to_have_value('4k')
            expect(camera.locator('.fps-input')).to_have_value(fps)
        expect(page.locator('#warnings')).not_to_contain_text('need matching sensor resolutions')
        expect(page.locator('#setup-start-button')).to_be_enabled()
        for camera in (camera_a, camera_d):
            camera.locator('.resolution-select').select_option('1080p')
            camera.locator('.fps-input').fill('10')
        page.set_viewport_size({'width': 1366, 'height': 768})

        # Keep configuration edits for an unselected camera through other sessions.
        camera_b = page.locator('.camera-config[data-socket="CAM_B"]')
        camera_b.locator('.camera-select').check()
        camera_b.locator('.resolution-select').select_option('4k')
        camera_b.locator('.fps-input').fill('13')
        camera_b.locator('.camera-select').uncheck()
        expect(camera_b.locator('.resolution-select')).to_be_disabled()
        expect(camera_b.locator('.fps-input')).to_be_disabled()

        # A startup error must be readable and dismissible above the modal.
        page.route('**/api/start', lambda route: route.fulfill(
            status=409, json={'error': 'Synthetic startup failure for UI verification'}), times=1)
        page.locator('#setup-start-button').click()
        expect(page.locator('#error-banner')).to_contain_text('Synthetic startup failure')
        page.locator('#dismiss-error').click()
        expect(page.locator('#error-banner')).to_be_hidden()
        expect(page.locator('#setup-dialog')).to_be_visible()

        for count in (1, 2, 3):
            if not page.locator('#setup-dialog').is_visible():
                page.locator('#open-setup').click()
            if count > 1:
                expect(camera_b.locator('.camera-select')).not_to_be_checked()
                expect(camera_b.locator('.resolution-select')).to_have_value('4k')
                expect(camera_b.locator('.fps-input')).to_have_value('13')
            page.locator(f'[data-preset="{count}"]').click()
            if count == 3:
                page.locator('#raw-enabled').check()
            page.locator('#setup-start-button').click()
            expect(page.locator('#pipeline-state')).to_have_text('Running', timeout=20000)
            expect(page.locator('#setup-dialog')).not_to_be_visible()
            expect(page.locator('#start-button')).to_be_hidden()
            expect(page.locator('#stop-button')).to_be_visible()
            expect(page.locator('.camera-card')).to_have_count(count)
            page.wait_for_function("document.querySelectorAll('.preview.has-frame').length === " + str(count))
            page.wait_for_function("[...document.querySelectorAll('.camera-image')].every(i => i.naturalWidth > 0)")
            assert_workspace_fits(page, f'{count} cameras, desktop')
            if count == 3:
                running_status = context.request.get(args.url + '/api/status').json()
                running_b = next(c for c in running_status['cameras'] if c['socket'] == 'CAM_B')
                assert running_b['resolution'] == '4k', running_b
                assert running_b['requested_fps'] == 13, running_b
            if count < 3:
                page.locator('#stop-button').click()
                expect(page.locator('#pipeline-state')).to_have_text('Stopped')
                expect(page.locator('#start-button')).to_be_visible()
                expect(page.locator('#stop-button')).to_be_hidden()

        def tab(name):
            page.locator(f'[data-control-tab="{name}"]').click()

        def camera(socket):
            page.locator(f'button[data-camera-socket="{socket}"]').click()

        def advanced(name, opened=True):
            details = page.locator(f'#panel-{name} details.advanced-controls')
            if details.evaluate('(element) => element.open') != opened:
                details.locator('summary').first.click()
            if opened:
                expect(details).to_have_attribute('open', '')
            else:
                expect(details).not_to_have_attribute('open', '')
            return details

        def apply():
            with page.expect_response(lambda response: '/api/controls/' in response.url) as response:
                page.locator('#apply-controls').click()
            assert response.value.status == 200, response.value.json()
            expect(page.locator('#notice')).to_contain_text('Settings applied', timeout=10000)
            page.wait_for_function("!document.body.classList.contains('busy')")
            return response.value.request.post_data_json

        def controls(socket='CAM_A'):
            state = context.request.get(args.url + '/api/status').json()
            return next(c['controls'] for c in state['cameras'] if c['socket'] == socket)

        # Keep the default workspace quiet without concealing frame health.
        for name in ('exposure', 'color', 'image'):
            details = page.locator(f'#panel-{name} details.advanced-controls')
            expect(details).to_have_count(1)
            expect(details).not_to_have_attribute('open', '')
        expect(page.locator('#toggle-details')).to_have_attribute('aria-pressed', 'false')
        for metadata in page.locator('.camera-metadata').all():
            expect(metadata).to_be_hidden()
        expect(page.locator('.camera-telemetry').first).to_be_visible()
        page.locator('#toggle-details').click()
        expect(page.locator('#toggle-details')).to_have_attribute('aria-pressed', 'true')
        for metadata in page.locator('.camera-metadata').all():
            expect(metadata).to_be_visible()
        page.locator('#toggle-details').click()
        expect(page.locator('.camera-metadata').first).to_be_hidden()

        page.locator('#view-selected').click()
        expect(page.locator('.camera-card:visible')).to_have_count(1)
        camera('CAM_D')
        expect(page.locator('.camera-card:visible')).to_have_attribute('data-socket', 'CAM_D')
        page.locator('#view-all').click()
        expect(page.locator('.camera-card:visible')).to_have_count(3)

        camera('CAM_A')
        tab('exposure')
        expect(page.locator('#manual-exposure-fields')).to_be_hidden()
        advanced('exposure')
        page.locator('[name="auto_exposure_limit_us"]').fill('5000')
        page.locator('[name="exposure_lock"]').check()
        apply()
        assert controls()['auto_exposure_limit_us'] == 5000
        assert controls()['exposure_lock'] is True
        assert controls('CAM_D')['exposure_lock'] is False

        # Keep an unapplied edit while changing cameras and receiving status polls.
        page.locator('#exposure-mode').select_option('manual')
        expect(page.locator('#manual-exposure-fields')).to_be_visible()
        expect(page.locator('[name="exposure_lock"]')).not_to_be_checked()
        page.locator('#exposure-number').fill('5000')
        page.locator('#iso-number').fill('200')
        camera('CAM_D')
        expect(page.locator('#exposure-mode')).to_have_value('auto')
        expect(page.locator('#manual-exposure-fields')).to_be_hidden()
        camera('CAM_A')
        page.wait_for_timeout(1800)
        expect(page.locator('#exposure-mode')).to_have_value('manual')
        expect(page.locator('#manual-exposure-fields')).to_be_visible()
        expect(page.locator('#exposure-number')).to_have_value('5000')

        # A rejected controls request retains its draft for a successful retry.
        manual_changes = {'exposure_mode': 'manual', 'exposure_us': 5000, 'iso': 200}
        page.route('**/api/controls/CAM_A', lambda route: route.fulfill(
            status=409, json={'error': 'Synthetic controls failure for UI verification'}), times=1)
        with page.expect_response(lambda response: '/api/controls/CAM_A' in response.url) as failed:
            page.locator('#apply-controls').click()
        assert failed.value.status == 409
        assert failed.value.request.post_data_json == manual_changes
        expect(page.locator('#error-banner')).to_contain_text('Synthetic controls failure')
        page.wait_for_function("!document.body.classList.contains('busy')")
        expect(page.locator('#reset-controls')).to_be_enabled()
        expect(page.locator('#control-dirty')).to_contain_text('Unapplied changes')
        assert controls()['exposure_mode'] == 'auto'
        assert controls()['exposure_lock'] is True
        assert controls()['exposure_us'] == 10000
        assert controls()['iso'] == 400
        camera('CAM_D')
        expect(page.locator('#exposure-mode')).to_have_value('auto')
        camera('CAM_A')
        page.wait_for_timeout(1800)
        expect(page.locator('#exposure-mode')).to_have_value('manual')
        expect(page.locator('#exposure-number')).to_have_value('5000')
        expect(page.locator('#iso-number')).to_have_value('200')
        page.locator('#dismiss-error').click()
        assert apply() == manual_changes
        expect(page.locator('#reset-controls')).to_be_disabled()
        expect(page.locator('#control-dirty')).to_have_text('Settings apply to this camera only.')
        assert controls()['exposure_mode'] == 'manual'
        assert controls()['exposure_lock'] is False
        assert controls()['exposure_us'] == 5000
        assert controls()['iso'] == 200
        assert controls('CAM_D')['exposure_mode'] == 'auto'

        tab('color')
        expect(page.locator('#manual-white-balance-fields')).to_be_hidden()
        page.locator('#white-balance-mode').select_option('daylight')
        expect(page.locator('#manual-white-balance-fields')).to_be_hidden()
        expect(page.locator('[name="white_balance_lock"]')).to_be_disabled()
        apply()
        assert controls()['white_balance_mode'] == 'daylight'
        page.locator('#white-balance-mode').select_option('auto')
        page.locator('[name="white_balance_lock"]').check()
        apply()
        assert controls()['white_balance_lock'] is True
        page.locator('#white-balance-mode').select_option('manual')
        expect(page.locator('#manual-white-balance-fields')).to_be_visible()
        expect(page.locator('[name="white_balance_lock"]')).not_to_be_checked()
        page.locator('#white-balance-number').fill('5600')
        apply()
        assert controls()['white_balance_kelvin'] == 5600
        assert controls()['white_balance_lock'] is False

        tab('focus')
        expect(page.locator('#manual-focus-fields')).to_be_hidden()
        page.locator('#focus-mode').select_option('manual')
        expect(page.locator('#manual-focus-fields')).to_be_visible()
        page.locator('#focus-number').fill('155')
        apply()
        assert controls()['focus'] == 155
        tab('image')
        page.locator('[name="effect_mode"]').select_option('mono')
        apply()
        assert controls()['effect_mode'] == 'mono'
        assert controls('CAM_D')['effect_mode'] == 'off'
        advanced('image')
        page.locator('[name="luma_denoise"]').fill('2')
        assert apply() == {'luma_denoise': 2}
        assert controls()['luma_denoise'] == 2
        advanced('image', opened=False)
        page.locator('[name="brightness"]').fill('2')
        expect(page.locator('#reset-controls')).to_be_enabled()
        page.locator('#reset-controls').click()
        expect(page.locator('[name="brightness"]')).to_have_value('0')

        # A second client can change settings without an unrelated draft undoing them.
        camera('CAM_D')
        remote = context.request.post(args.url + '/api/controls/CAM_D', data={'brightness': 3})
        assert remote.ok
        expect(page.locator('[name="brightness"]')).to_have_value('3', timeout=10000)
        tab('color')
        advanced('color')
        page.locator('[name="saturation"]').fill('2')
        camera('CAM_A')
        remote = context.request.post(args.url + '/api/controls/CAM_D', data={'brightness': 4})
        assert remote.ok
        page.wait_for_timeout(1800)
        camera('CAM_D')
        assert apply() == {'saturation': 2}
        assert controls('CAM_D')['brightness'] == 4

        # A repeated one-shot focus request is explicit, not a side effect of Apply.
        camera('CAM_A')
        tab('focus')
        page.locator('#focus-mode').select_option('auto')
        expect(page.locator('#manual-focus-fields')).to_be_hidden()
        assert apply() == {'focus_mode': 'auto'}
        with page.expect_response(lambda response: '/api/controls/' in response.url) as response:
            page.locator('#trigger-autofocus').click()
        assert response.value.status == 200
        assert response.value.request.post_data_json == {'focus_mode': 'auto'}
        expect(page.locator('#notice')).to_contain_text('Autofocus requested')
        page.wait_for_function("!document.body.classList.contains('busy')")

        # Invalid edits in another tab are brought into view before validation.
        tab('exposure')
        page.locator('#exposure-number').fill('')
        tab('color')
        page.locator('#apply-controls').click()
        expect(page.locator('[data-control-tab="exposure"]')).to_have_attribute('aria-selected', 'true')
        expect(page.locator('#exposure-number')).to_be_focused()
        page.locator('#reset-controls').click()

        # A collapsed advanced section must open when it contains an invalid edit.
        page.locator('#exposure-mode').select_option('auto')
        expect(page.locator('#manual-exposure-fields')).to_be_hidden()
        details = advanced('exposure')
        page.locator('[name="auto_exposure_limit_us"]').fill('100001')
        advanced('exposure', opened=False)
        tab('color')
        page.locator('#apply-controls').click()
        expect(page.locator('[data-control-tab="exposure"]')).to_have_attribute('aria-selected', 'true')
        expect(details).to_have_attribute('open', '')
        expect(page.locator('[name="auto_exposure_limit_us"]')).to_be_focused()
        assert controls()['exposure_mode'] == 'manual'
        assert controls()['auto_exposure_limit_us'] == 5000
        page.locator('#reset-controls').click()
        expect(page.locator('#manual-exposure-fields')).to_be_visible()

        # The primary actions remain accessible across viewport sizes and tabs.
        for width, height in ((1366, 768), (1024, 768), (800, 600), (390, 844)):
            page.set_viewport_size({'width': width, 'height': height})
            for name in ('exposure', 'color', 'focus', 'image'):
                tab(name)
                assert_workspace_fits(page, f'{width}x{height} {name}')
            camera('CAM_D')
            expect(page.locator('#control-camera')).to_have_value('CAM_D')
            camera('CAM_A')
            page.screenshot(path=str(args.output / f'workspace-{width}x{height}.png'))

        # Very short screens may scroll, but must never clip working controls.
        for width, height in ((844, 390), (390, 600), (683, 384)):
            page.set_viewport_size({'width': width, 'height': height})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Short viewport horizontal overflow'
            for selector in ('#apply-controls', '#capture-selected', '#open-setup'):
                page.locator(selector).scroll_into_view_if_needed()
                expect(page.locator(selector)).to_be_in_viewport(ratio=1)
        page.set_viewport_size({'width': 1366, 'height': 768})

        page.locator('#capture-format').select_option('png')
        page.locator('#capture-all').click()
        expect(page.locator('#notice')).to_contain_text('3 PNG captures saved', timeout=20000)
        page.locator('#capture-format').select_option('raw')
        page.locator('#capture-selected').click()
        expect(page.locator('#notice')).to_contain_text('1 RAW capture saved', timeout=20000)
        expect(page.locator('#error-banner')).to_be_hidden()
        page.locator('#open-captures').click()
        expect(page.locator('#captures-dialog')).to_be_visible()
        with page.expect_download() as download:
            page.locator('#capture-list a').filter(has_text='image.raw').first.click()
        download.value.save_as(args.output / 'browser-capture.raw')
        page.locator('#close-captures').click()
        expect(page.locator('#captures-dialog')).not_to_be_visible()
        expect(page.locator('#open-captures')).to_be_focused()
        page.locator('#stop-button').click()
        expect(page.locator('#pipeline-state')).to_have_text('Stopped')
        expect(page.locator('#start-button')).to_be_visible()
        expect(page.locator('#stop-button')).to_be_hidden()
        page.locator('#open-setup').click()
        page.locator('#scan-button').click()
        expect(page.locator('#notice')).to_contain_text('3 cameras discovered')
        page.locator('#close-setup').click()
        assert not errors, errors
        browser.close()
    print('Browser smoke passed: compact layouts, automatic/manual visibility, advanced controls, metadata toggle, dialogs, retained setup configurations, camera drafts and failed-request retry, extended controls, captures/downloads, and restart.')


if __name__ == '__main__':
    main()
