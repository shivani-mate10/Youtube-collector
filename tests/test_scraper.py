import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from yt_dlp.networking import Response
from yt_dlp.networking.exceptions import HTTPError

from collector.enrich import scraper


def http_error(status, headers=None):
    return HTTPError(Response(io.BytesIO(), 'https://example.com', headers or {}, status=status))


class ScraperTests(unittest.TestCase):
    def test_ids(self):
        expected = 'Ywtya3SHMvU'
        for value in (expected, f'https://youtu.be/{expected}', f'https://www.youtube.com/watch?v={expected}&list=ignored', f'https://youtube.com/shorts/{expected}'):
            self.assertEqual(scraper.video_id(value), expected)
        for value in ('../escape', 'https://evil.com/watch?v=Ywtya3SHMvU', 'bad'):
            with self.assertRaises(ValueError):
                scraper.video_id(value)

    def test_caption_selection(self):
        info = {'subtitles': {'en-US': [{'ext': 'json3', 'url': 'https://example.com/manual'}]},
                'automatic_captions': {'en': [{'ext': 'json3', 'url': 'https://example.com/auto'}]}}
        self.assertEqual(scraper.caption_track(info)[:2], ('en-US', 'subtitles'))
        info['subtitles'] = {}
        self.assertEqual(scraper.caption_track(info)[1], 'automatic_captions')
        info['automatic_captions']['en'][0]['url'] += '?tlang=en'
        self.assertIsNotNone(scraper.caption_track(info))

    def test_native_captions_preferred_to_translation(self):
        info = {'subtitles': {'en-hi': [{'ext': 'json3', 'url': 'https://example.com/?lang=hi&tlang=en'}]},
                'automatic_captions': {'en-orig': [{'ext': 'json3', 'url': 'https://example.com/?lang=en'}]}}
        self.assertEqual(scraper.caption_track(info)[0], 'en-orig')

    def test_translated_caption_output_and_empty_rejection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = {'files': {}}
            info = {'automatic_captions': {'en': [{'ext': 'json3', 'url': 'https://example.com/?lang=hi&tlang=en'}]}}
            data = json.dumps({'events': [{'tStartMs': 0, 'dDurationMs': 1000, 'segs': [{'utf8': 'English translation'}]}]}).encode()
            with patch.object(scraper, 'fetch', return_value=data):
                scraper.save_captions(None, info, root, manifest)
            self.assertTrue(manifest['captions']['translated'])
            self.assertEqual(manifest['captions']['source_language'], 'hi')
            self.assertEqual((root / 'transcript.txt').read_text(), 'English translation\n')
            for bad in (b'', b'{}', b'<html>error</html>'):
                with patch.object(scraper, 'fetch', return_value=bad), self.assertRaises(ValueError):
                    scraper.save_captions(None, info, root, {'files': {}})

    def test_age_metadata(self):
        self.assertEqual(scraper.age_metadata({})['restriction_status'], 'unknown')
        self.assertEqual(scraper.age_metadata({'age_limit': 18})['minimum_age'], 18)
        self.assertEqual(scraper.age_metadata({'age_limit': 0})['restriction_status'], 'not_reported')
        self.assertIsNone(scraper.age_metadata({'age_limit': 0})['content_suitability_rating'])

    def test_caption_segments(self):
        self.assertEqual(scraper.caption_segments({'events': [{}, {'tStartMs': 100, 'dDurationMs': 200, 'segs': [{'utf8': 'Hello '}, {'utf8': 'world'}]}]}),
                         [{'start_ms': 100, 'end_ms': 300, 'text': 'Hello world'}])

    def test_retry_and_circuit_breaker(self):
        sleep = Mock()
        policy = scraper.NetworkPolicy(0, 0, sleep=sleep)
        send = Mock(side_effect=[http_error(429, {'Retry-After': '120'}), 'ok'])
        self.assertEqual(policy.call(send), 'ok')
        self.assertTrue(any(call.args[0] >= 120 for call in sleep.call_args_list))
        with self.assertRaises(scraper.BatchStopped):
            policy.call(Mock(side_effect=http_error(429)))
        untouched = Mock()
        with self.assertRaises(scraper.BatchStopped):
            policy.call(untouched)
        untouched.assert_not_called()

    def test_retry_limit(self):
        policy = scraper.NetworkPolicy(0, 0, sleep=Mock())
        send = Mock(side_effect=lambda: (_ for _ in ()).throw(http_error(503)))
        with self.assertRaises(HTTPError):
            policy.call(send)
        self.assertEqual(send.call_count, 4)

    def test_storyboard_partial_sheet_and_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sheet = Image.new('RGB', (4, 4), 'red')
            data = io.BytesIO()
            sheet.save(data, 'PNG')
            board = {'format_id': 'sb0', 'protocol': 'mhtml', 'width': 2, 'height': 2, 'columns': 2, 'rows': 2, 'fps': 0.5,
                     'fragments': [{'url': 'https://example.com/sheet'}]}
            info = {'duration': 6, 'formats': [board]}
            manifest = {'files': {}}
            with patch.object(scraper, 'fetch', return_value=data.getvalue()) as fetch:
                scraper.save_previews(None, info, root, manifest)
                scraper.save_previews(None, info, root, manifest)
                self.assertEqual(fetch.call_count, 1)
            index = json.loads((root / 'previews/index.json').read_text())
            self.assertEqual([frame['timestamp_ms'] for frame in index], [0, 2000, 4000])
            self.assertEqual(len(list((root / 'previews').glob('*.jpg'))), 3)
            self.assertEqual(index[2]['crop'], [0, 2, 2, 2])

    def test_existing_directory_skips_without_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'Ywtya3SHMvU').mkdir()
            ydl = Mock()
            self.assertEqual(scraper.process_video(ydl, 'Ywtya3SHMvU', root)['status'], 'skipped')
            ydl.extract_info.assert_not_called()

    def test_duplicate_id_and_url_processed_once(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(scraper, 'PacedYoutubeDL'), patch.object(scraper, 'process_video', return_value={'status': 'skipped'}) as process:
            self.assertEqual(scraper.main(['Ywtya3SHMvU', 'https://youtu.be/Ywtya3SHMvU', '--output', folder]), 0)
            self.assertEqual(process.call_count, 1)

    def test_integrity_and_atomic_write_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = {'files': {}}
            scraper.record(root, manifest, 'data.txt', b'good')
            with patch.object(scraper.os, 'replace', side_effect=OSError('interrupted')):
                with self.assertRaises(OSError):
                    scraper.atomic_bytes(root / 'data.txt', b'bad')
            self.assertTrue(scraper.valid_file(root, manifest, 'data.txt'))
            (root / 'data.txt').write_text('corrupt')
            self.assertFalse(scraper.valid_file(root, manifest, 'data.txt'))

    def test_missing_assets_and_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            ydl = Mock()
            ydl.policy.stopped = False
            ydl.extract_info.return_value = {'id': 'Ywtya3SHMvU'}
            self.assertEqual(scraper.process_video(ydl, 'Ywtya3SHMvU', Path(folder))['status'], 'incomplete')
            manifest = json.loads((Path(folder) / 'Ywtya3SHMvU/manifest.json').read_text())
            self.assertEqual(manifest['captions']['status'], 'missing')
            scraper.process_video(ydl, 'Ywtya3SHMvU', Path(folder), resume=True)
            self.assertEqual(ydl.extract_info.call_count, 2)

    def test_proxy_and_shared_policy(self):
        policy = scraper.NetworkPolicy(0, 0, sleep=Mock())
        with scraper.PacedYoutubeDL(policy, 'socks5://localhost:9999') as ydl:
            self.assertEqual(ydl.params['proxy'], 'socks5://localhost:9999')
            with patch.object(scraper.yt_dlp.YoutubeDL, 'urlopen', return_value='response'):
                self.assertEqual(ydl.urlopen('https://example.com'), 'response')

    def test_expired_caption_url_refresh_and_failure_isolation(self):
        with tempfile.TemporaryDirectory() as folder:
            ydl = Mock()
            ydl.policy.stopped = False
            ydl.extract_info.return_value = {'id': 'Ywtya3SHMvU'}
            with patch.object(scraper, 'save_captions', side_effect=[http_error(403), ValueError('bad captions')]) as captions, patch.object(scraper, 'save_previews') as previews:
                scraper.process_video(ydl, 'Ywtya3SHMvU', Path(folder))
            self.assertEqual(captions.call_count, 2)
            self.assertEqual(ydl.extract_info.call_count, 2)
            previews.assert_called_once()
            manifest = json.loads((Path(folder) / 'Ywtya3SHMvU/manifest.json').read_text())
            self.assertEqual(manifest['captions']['status'], 'failed')

    def test_resume_complete_avoids_network(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'Ywtya3SHMvU'
            root.mkdir()
            manifest = {'schema_version': 1, 'id': root.name, 'status': 'complete', 'extraction_revision': 2, 'files': {}}
            scraper.record(root, manifest, 'data.txt', b'complete')
            ydl = Mock()
            self.assertTrue(scraper.process_video(ydl, root.name, root.parent, resume=True)['cached'])
            ydl.extract_info.assert_not_called()

    def test_bot_challenge_stops_batch(self):
        policy = scraper.NetworkPolicy()
        with self.assertRaises(scraper.BatchStopped):
            scraper.QuietLogger(policy).error("Sign in to confirm you’re not a bot")
        self.assertTrue(policy.stopped)


if __name__ == '__main__':
    unittest.main()
