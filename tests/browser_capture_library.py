"""Verify capture browsing and bulk actions using an owned temporary demo server.

Build the UI first, then run:
.venv/bin/python tests/browser_capture_library.py --url http://127.0.0.1:8082
The URL identifies the loopback port to bind; an existing server is never used.
"""

from contextlib import contextmanager
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from urllib.parse import urlsplit
import zipfile
import argparse

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server, WSGIRequestHandler

# Running this file directly places tests/, rather than the project, on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oak_camera.app import create_app


class QuietRequestHandler(WSGIRequestHandler):
    def log_request(self, code="-", size="-"):
        pass


def synthetic_capture(store, index):
    """Use real encoders so thumbnail tests exercise the production image path."""
    import cv2
    import numpy as np

    fmt, extension = [('jpeg', 'jpg'), ('png', 'png'), ('tiff', 'tiff'),
                      ('bmp', 'bmp'), ('raw', 'raw')][index % 5]
    if fmt == 'raw':
        data = bytes([index % 256]) * 400
    else:
        image = np.zeros((16, 24, 3), dtype=np.uint8)
        image[:, :, index % 3] = 80 + index % 175
        success, encoded = cv2.imencode('.' + extension, image)
        assert success, f'Could not encode synthetic {fmt} fixture'
        data = encoded.tobytes()
    record = store.save(['CAM_A', 'CAM_B', 'CAM_D'][index % 3], fmt,
                        {'extension': extension, 'data': data,
                         'metadata': {'width': 24, 'height': 16, 'synthetic_test': True}},
                        demo=True)
    # Include a UTC timestamp whose local calendar date changes in Asia/Tokyo.
    created_at = ('2024-04-06T16:30:00+00:00' if index == 0 else
                  f'2024-04-{6 + index % 3:02d}T12:{index:02d}:00+00:00')
    record['created_at'] = created_at
    record['metadata']['created_at'] = created_at
    folder = store.root / record['id']
    (folder / 'metadata.json').write_text(json.dumps(record['metadata']))
    (folder / 'record.json').write_text(json.dumps(record))
    return record


@contextmanager
def isolated_demo_server(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost'}
            or not parsed.port or parsed.path not in {'', '/'} or parsed.query
            or parsed.fragment or parsed.username or parsed.password):
        raise ValueError('--url must be an HTTP loopback URL with an explicit unused port.')
    with TemporaryDirectory(prefix='oak-capture-library-') as temporary:
        app = create_app(demo=True, capture_dir=Path(temporary) / 'captures')
        store = app.extensions['capture_store']
        backend = app.extensions['camera_backend']
        try:
            records = [synthetic_capture(store, index) for index in range(56)]
            # make_server fails on an occupied port; never fall back to an existing app.
            server = make_server(parsed.hostname, parsed.port, app, threaded=True,
                                 request_handler=QuietRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield store, records
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
        finally:
            backend.close()


def assert_no_horizontal_overflow(page, label):
    result = page.evaluate("""() => {
        const dialog = document.querySelector('#captures-dialog');
        const box = dialog.getBoundingClientRect();
        return {document: document.documentElement.scrollWidth, viewport: innerWidth,
                left: box.left, right: box.right,
                dialog: dialog.scrollWidth, content: dialog.clientWidth};
    }""")
    assert result['document'] <= result['viewport'] + 1, (label, result)
    assert result['left'] >= -1 and result['right'] <= result['viewport'] + 1, (label, result)
    assert result['dialog'] <= result['content'] + 1, (label, result)



def assert_library_controls_fit(page, label):
    result = page.evaluate("""() => {
        const ids = ['#toggle-capture-filters', '#refresh-captures',
            '#download-selected-captures', '#delete-selected-captures',
            '#download-all-captures', '#delete-all-captures'];
        const outside = ids.filter(selector => {
            const r = document.querySelector(selector).getBoundingClientRect();
            return r.left < -1 || r.top < -1 || r.right > innerWidth + 1
                || r.bottom > innerHeight + 1 || r.width <= 0 || r.height <= 0;
        });
        const gallery = document.querySelector('.capture-library-gallery');
        const scroller = document.querySelector('.capture-library-browser') || gallery;
        return {outside, galleryHeight: gallery.getBoundingClientRect().height,
                scrollAreaHeight: scroller.clientHeight, scrollHeight: scroller.scrollHeight,
                viewport: [innerWidth, innerHeight]};
    }""")
    assert not result['outside'], (label, result)
    assert result['galleryHeight'] > 20 and result['scrollAreaHeight'] > 20, (label, result)
    assert result['scrollHeight'] > result['scrollAreaHeight'], (label, result)


def shown_ids(page):
    return page.locator('.capture-card').evaluate_all(
        '(cards) => cards.map(card => card.dataset.captureId)')


def verify_zip(download, output, records, store):
    destination = output / download.suggested_filename
    download.save_as(destination)
    assert destination.suffix == '.zip', destination
    expected = {f"{record['id']}/{file['name']}" for record in records
                for file in record['files']}
    with zipfile.ZipFile(destination) as archive:
        assert set(archive.namelist()) == expected, (archive.namelist(), expected)
        for name in expected:
            assert archive.read(name) == (store.root / name).read_bytes(), name
        assert all(not name.endswith('/record.json') for name in archive.namelist())
    return destination


def exercise(url, chromium, output, store, records):
    by_id = {record['id']: record for record in records}
    all_ids = set(by_id)
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium, headless=True,
                                            args=['--no-sandbox', '--disable-dev-shm-usage'])
        context = browser.new_context(viewport={'width': 1366, 'height': 768},
                                      timezone_id='Asia/Tokyo', locale='en-US',
                                      accept_downloads=True)
        status = context.request.get(url + '/api/status').json()
        assert status['demo'] is True
        assert status['capture_count'] == len(records) > 50
        page = context.new_page()
        page.on('pageerror', lambda exc: errors.append(str(exc)))
        pending_library_requests = []
        page.route('**/api/captures', lambda route: pending_library_requests.append(route), times=1)
        broken_thumbnail = next(record for record in records if record['format'] == 'png')
        page.route('**/captures/' + broken_thumbnail['id'] + '/thumbnail',
                   lambda route: route.fulfill(status=404, body='Synthetic missing thumbnail'))
        page.goto(url, wait_until='domcontentloaded')
        expect(page.locator('#demo-banner')).to_be_visible(timeout=20000)
        expect(page.locator('#setup-dialog')).to_be_visible(timeout=20000)
        page.keyboard.press('Escape')
        page.locator('#open-captures').click()
        expect(page.locator('#captures-dialog')).to_be_visible()
        expect(page.locator('#capture-library-loading')).to_be_visible()
        expect(page.locator('#download-all-captures')).to_be_disabled()
        expect(page.locator('#delete-all-captures')).to_be_disabled()
        assert pending_library_requests, 'Library open must request the full saved library'
        pending_library_requests[0].fulfill(status=409, json={'error': 'Synthetic library load failure'})
        expect(page.locator('#error-banner')).to_contain_text('Synthetic library load failure')
        expect(page.locator('#download-all-captures')).to_be_disabled()
        expect(page.locator('#delete-all-captures')).to_be_disabled()
        page.locator('#close-captures').click()
        page.locator('#open-captures').click()
        expect(page.locator('#error-banner')).to_be_hidden()
        expect(page.locator('#capture-results-count')).to_contain_text(str(len(records)))
        expect(page.locator('.capture-card').first).to_be_visible()
        assert_no_horizontal_overflow(page, 'desktop library')
        page.screenshot(path=str(output / 'desktop-library.png'))
        first_page = set(shown_ids(page))
        expect(page.locator('#next-capture-page')).to_be_enabled()
        page.locator('#next-capture-page').click()
        expect(page.locator('.capture-card')).to_have_count(len(records) - len(first_page))
        second_page = set(shown_ids(page))
        assert first_page.isdisjoint(second_page) and first_page | second_page == all_ids
        page.locator('#previous-capture-page').click()
        expect(page.locator('.capture-card')).to_have_count(len(first_page))
        expect(page.locator('#previous-capture-page')).to_be_disabled()

        page.locator('#toggle-capture-filters').click()
        for filter_name in ('date', 'format', 'category', 'camera'):
            expect(page.locator(f'#capture-{filter_name}-filter')).to_be_visible()

        def reset_filters():
            page.locator('#capture-date-filter').fill('')
            for name in ('format', 'category', 'camera'):
                page.locator(f'#capture-{name}-filter').select_option('')

        def assert_filtered(expected):
            expected_ids = {record['id'] for record in expected}
            expect(page.locator('#capture-results-count')).to_contain_text(str(len(expected)))
            actual = shown_ids(page)
            assert actual and set(actual) <= expected_ids, (actual, expected_ids)
            page.locator('#select-shown-captures').click()
            expect(page.locator('#capture-selection-count')).to_contain_text(str(len(expected)))
            # Verify selection by the actual POST body, including off-page captures.
            page.route('**/api/captures/download?prepare=1', lambda route: route.fulfill(
                status=409, json={'error': 'Synthetic download failure'}), times=1)
            with page.expect_response(lambda response: '/api/captures/download?prepare=1' in response.url) as failed:
                page.locator('#download-selected-captures').click()
            assert failed.value.status == 409
            assert set(failed.value.request.post_data_json['ids']) == expected_ids
            expect(page.locator('#error-banner')).to_contain_text('Synthetic download failure')
            expect(page.locator('#capture-selection-count')).to_contain_text(str(len(expected)))
            page.locator('#dismiss-error').click()
            page.locator('#clear-capture-selection').click()

        assert_filtered(records)
        page.locator('#capture-date-filter').fill('2024-04-07')
        local_date_records = [record for record in records if record['created_at'].startswith('2024-04-07')]
        local_date_records.append(records[0])
        assert_filtered(local_date_records)
        assert records[0]['id'] in shown_ids(page), 'Date filtering must use browser local calendar day'
        reset_filters()
        page.locator('#capture-format-filter').select_option('raw')
        raw_records = [record for record in records if record['format'] == 'raw']
        assert_filtered(raw_records)
        expect(page.locator('.capture-card img')).to_have_count(0)
        expect(page.locator('.capture-preview-placeholder')).to_have_count(len(raw_records))
        reset_filters()
        page.locator('#capture-category-filter').select_option('processed')
        assert_filtered([record for record in records if record['format'] != 'raw'])
        reset_filters()
        page.locator('#capture-camera-filter').select_option('CAM_B')
        assert_filtered([record for record in records if record['socket'] == 'CAM_B'])
        reset_filters()

        # Grouping changes headings without losing records, including each metadata axis.
        for group in ('format', 'category', 'camera', 'date'):
            page.locator('#capture-group-by').select_option(group)
            expect(page.locator('#capture-group-by')).to_have_value(group)
            assert shown_ids(page), group
            expect(page.locator('#capture-results-count')).to_contain_text('56')
            for section in page.locator('.capture-library-group').all():
                key = section.get_attribute('data-group-key')
                expect(section.locator('.capture-group-heading h3')).not_to_have_text('')
                ids = section.locator('.capture-card').evaluate_all(
                    '(cards) => cards.map(card => card.dataset.captureId)')
                for capture_id in ids:
                    record = by_id[capture_id]
                    if group == 'format':
                        expected_key = record['format']
                    elif group == 'category':
                        expected_key = 'raw' if record['format'] == 'raw' else 'processed'
                    elif group == 'camera':
                        expected_key = record['socket']
                    else:
                        expected_key = '2024-04-07' if record is records[0] else record['created_at'][:10]
                    assert key == expected_key, (group, key, record)

        page.locator('#select-all-captures').click()
        expect(page.locator('#capture-selection-count')).to_contain_text('56')
        page.locator('#capture-format-filter').select_option('raw')
        page.locator('#capture-category-filter').select_option('processed')
        expect(page.locator('.capture-card')).to_have_count(0)
        expect(page.locator('#select-shown-captures')).to_be_disabled()
        expect(page.locator('#capture-selection-count')).to_contain_text('56')
        reset_filters()
        page.locator('#clear-capture-selection').click()
        page.locator('#capture-format-filter').select_option('png')
        broken_card = page.locator(f'.capture-card[data-capture-id="{broken_thumbnail["id"]}"]')
        expect(broken_card.locator('.capture-preview-placeholder')).to_be_visible()
        page.wait_for_function("""() => [...document.querySelectorAll('.capture-card img')]
            .some(image => image.complete && image.naturalWidth > 0)""")
        visible = shown_ids(page)
        subset = [by_id[capture_id] for capture_id in visible[:2]]
        for record in subset:
            page.locator(f'.capture-card[data-capture-id="{record["id"]}"] input[type=checkbox]').check()
        expect(page.locator('#capture-selection-count')).to_contain_text('2')
        with page.expect_download() as download:
            page.locator('#download-selected-captures').click()
        verify_zip(download.value, output, subset, store)
        expect(page.locator('#capture-selection-count')).to_contain_text('2')
        # All-download includes records outside the current PNG filter and pagination.
        with page.expect_download() as download:
            page.locator('#download-all-captures').click()
        verify_zip(download.value, output, records, store)
        assert set(record['id'] for record in store.list(limit=None)) == all_ids

        # Cancel keeps every file. A failed delete keeps selection for a retry.
        page.locator('#delete-selected-captures').click()
        expect(page.locator('#capture-delete-confirmation')).to_be_visible()
        page.locator('#cancel-capture-delete').click()
        expect(page.locator('#capture-delete-confirmation')).to_be_hidden()
        expect(page.locator('#delete-selected-captures')).to_be_focused()
        assert all((store.root / record['id'] / 'metadata.json').is_file() for record in records)
        page.route('**/api/captures/delete', lambda route: route.fulfill(
            status=507, json={'error': 'Synthetic disk deletion failure'}), times=1)
        page.locator('#delete-selected-captures').click()
        page.locator('#confirm-capture-delete').click()
        expect(page.locator('#error-banner')).to_contain_text('Synthetic disk deletion failure')
        expect(page.locator('#capture-selection-count')).to_contain_text('2')
        assert set(record['id'] for record in store.list(limit=None)) == all_ids
        page.locator('#dismiss-error').click()
        if not page.locator('#capture-delete-confirmation').is_visible():
            page.locator('#delete-selected-captures').click()

        def partial_delete(route):
            removed = store.delete([subset[0]['id']])
            remaining = store.list(limit=None)
            route.fulfill(status=507, json={
                'error': 'Synthetic partial deletion', 'deleted': removed,
                'captures': remaining, 'capture_count': len(remaining),
            })

        page.route('**/api/captures/delete', partial_delete, times=1)
        page.locator('#confirm-capture-delete').click()
        expect(page.locator('#error-banner')).to_contain_text('Synthetic partial deletion')
        expect(page.locator('#capture-selection-count')).to_contain_text('1 selected')
        expect(page.locator('#capture-delete-heading')).to_have_text('Permanently delete 1 image?')
        assert not (store.root / subset[0]['id']).exists()
        assert (store.root / subset[1]['id'] / 'metadata.json').is_file()
        page.locator('#dismiss-error').click()
        with page.expect_response(lambda response: '/api/captures/delete' in response.url) as deleted:
            page.locator('#confirm-capture-delete').click()
        assert deleted.value.status == 200
        assert deleted.value.request.post_data_json['ids'] == [subset[1]['id']]
        for record in subset:
            assert not (store.root / record['id']).exists(), 'Image and metadata folder must be removed'
        expect(page.locator('#capture-selection-count')).to_contain_text('0')
        expect(page.locator('#capture-results-count')).to_contain_text('54')

        # Keep filters, gallery scrolling, and actions reachable in short windows.
        for width, height, label in ((844, 390, 'desktop-short'),
                                     (390, 600, 'mobile-short'),
                                     (390, 568, 'mobile-small')):
            page.set_viewport_size({'width': width, 'height': height})
            assert_no_horizontal_overflow(page, label)
            assert_library_controls_fit(page, label)
            page.screenshot(path=str(output / f'{label}-library.png'))

        # Check the full modal and its deletion confirmation on a phone-sized viewport.
        page.set_viewport_size({'width': 390, 'height': 844})
        assert_no_horizontal_overflow(page, 'mobile library')
        page.screenshot(path=str(output / 'mobile-library.png'))
        page.locator('#delete-all-captures').click()
        expect(page.locator('#capture-delete-confirmation')).to_be_visible()
        assert_no_horizontal_overflow(page, 'mobile delete confirmation')
        page.screenshot(path=str(output / 'mobile-delete-confirmation.png'))
        # Another client adds a capture after the user begins confirming Delete all.
        new_record = synthetic_capture(store, 57)
        with page.expect_response(lambda response: '/api/captures/delete' in response.url) as deleted:
            page.locator('#confirm-capture-delete').click()
        assert deleted.value.status == 200
        payload = deleted.value.request.post_data_json
        assert set(payload['ids']) == all_ids - {record['id'] for record in subset}
        assert len(payload['ids']) == 54 > 50
        assert 'all' not in payload, 'Confirmation must freeze IDs rather than include future captures'
        remaining = store.list(limit=None)
        assert [record['id'] for record in remaining] == [new_record['id']], remaining
        for capture_id in payload['ids']:
            assert not (store.root / capture_id).exists()
        reset_filters()
        expect(page.locator('#capture-results-count')).to_contain_text('1')
        expect(page.locator('.capture-card')).to_have_count(1)
        page.locator('#delete-all-captures').click()
        page.locator('#confirm-capture-delete').click()
        expect(page.locator('.capture-card')).to_have_count(0)
        expect(page.locator('#download-all-captures')).to_be_disabled()
        expect(page.locator('#delete-all-captures')).to_be_disabled()
        assert store.list(limit=None) == []
        assert not errors, errors
        browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True, help='Unused HTTP loopback port for the isolated demo server')
    parser.add_argument('--chromium', default='/usr/bin/chromium')
    parser.add_argument('--output', type=Path, default=Path('test-results/capture-library'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with isolated_demo_server(args.url) as (store, records):
        exercise(args.url.rstrip('/'), args.chromium, args.output, store, records)
    print('Capture-library browser regression passed using temporary synthetic captures.')


if __name__ == '__main__':
    main()
