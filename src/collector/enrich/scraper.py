#!/usr/bin/env python3
"""Sequential, resumable English-caption and storyboard extraction."""
import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlparse

import PIL
from PIL import Image
import yt_dlp
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.version import __version__ as YTDLP_VERSION


class BatchStopped(Exception):
    pass


def safe_error(error):
    # Never persist signed asset URLs, proxy credentials, or extractor query strings.
    return re.sub(r'https?://\S+|socks\w*://\S+', '[URL]', str(error))[:800]


class NetworkPolicy:
    def __init__(self, minimum=1, maximum=2, sleep=time.sleep, clock=time.monotonic):
        self.minimum, self.maximum = minimum, maximum
        self.sleep, self.clock = sleep, clock
        self.next_request = 0
        self.throttled = False
        self.stopped = False

    def stop(self):
        self.stopped = True
        raise BatchStopped('Batch stopped by YouTube throttling or bot challenge; resume later.')

    def call(self, send):
        for attempt in range(4):
            if self.stopped:
                self.stop()
            self.sleep(max(0, self.next_request - self.clock()))
            self.next_request = self.clock() + random.uniform(self.minimum, self.maximum)
            try:
                return send()
            except HTTPError as error:
                if error.status == 429:
                    if self.throttled:
                        error.close()
                        self.stop()
                    self.throttled = True
                    retry = error.response.headers.get('Retry-After', '')
                    try:
                        delay = float(retry)
                    except ValueError:
                        try:
                            delay = parsedate_to_datetime(retry).timestamp() - time.time()
                        except (TypeError, ValueError, OverflowError):
                            delay = 60
                    error.close()
                    self.sleep(max(60, delay) + random.uniform(0, 5))
                elif error.status >= 500 and attempt < 3:
                    error.close()
                    self.sleep(2 ** (attempt + 1) + random.random())
                else:
                    raise
            except (TransportError, TimeoutError, OSError):
                if attempt == 3:
                    raise
                self.sleep(2 ** (attempt + 1) + random.random())
        self.stop()


class QuietLogger:
    def __init__(self, policy):
        self.policy = policy

    def debug(self, message):
        pass

    def warning(self, message):
        print(safe_error(message), file=sys.stderr)

    def error(self, message):
        normalized = message.lower().replace('’', "'")
        if any(s in normalized for s in ('confirm you’re not a bot', "confirm you're not a bot", 'confirm you are not a bot')):
            self.policy.stop()
        self.warning(message)


class PacedYoutubeDL(yt_dlp.YoutubeDL):
    def __init__(self, policy, proxy=None):
        self.policy = policy
        super().__init__({
            'skip_download': True, 'noplaylist': True, 'quiet': True,
            'socket_timeout': 30, 'retries': 0, 'extractor_retries': 0,
            'fragment_retries': 0, 'ignore_no_formats_error': True,
            'proxy': proxy, 'logger': QuietLogger(policy),
            'js_runtimes': {name: {'path': path} for name in ('deno', 'node')
                            if (path := shutil.which(name))},
            'writeautomaticsub': True, 'subtitleslangs': ['en.*'],
        })

    def urlopen(self, req):
        return self.policy.call(lambda: super(PacedYoutubeDL, self).urlopen(req))


def video_id(value):
    if re.fullmatch(r'[A-Za-z0-9_-]{11}', value):
        return value
    parsed = urlparse(value)
    host = (parsed.hostname or '').lower()
    candidate = ''
    if parsed.scheme in ('http', 'https'):
        if host == 'youtu.be':
            candidate = parsed.path.strip('/')
        elif host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'):
            parts = parsed.path.strip('/').split('/')
            if parsed.path == '/watch':
                candidate = parse_qs(parsed.query).get('v', [''])[0]
            elif len(parts) == 2 and parts[0] in ('shorts', 'embed', 'live'):
                candidate = parts[1]
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', candidate):
        raise ValueError(f'Invalid video ID or URL: {safe_error(value)}')
    return candidate


def atomic_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.part')
    with temporary.open('wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def atomic_json(path, data):
    atomic_bytes(path, (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode())


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def valid_file(root, manifest, relative):
    path = root / relative
    return path.is_file() and manifest['files'].get(relative) == checksum(path)


def record(root, manifest, relative, data, checkpoint=True):
    atomic_bytes(root / relative, data)
    manifest['files'][relative] = checksum(root / relative)
    if checkpoint:
        atomic_json(root / 'manifest.json', manifest)


def record_json(root, manifest, relative, data):
    record(root, manifest, relative, (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode())


def caption_track(info):
    candidates = []
    for source in ('subtitles', 'automatic_captions'):
        for language, formats in (info.get(source) or {}).items():
            if not re.fullmatch(r'en(?:[-_].+)?', language):
                continue
            for track in formats:
                if track.get('ext') != 'json3':
                    continue
                query = parse_qs(urlparse(track['url']).query)
                target = query.get('tlang', [''])[0]
                original = query.get('lang', [''])[0]
                translated = bool(target and target != original)
                if target and not re.fullmatch(r'en(?:[-_].+)?', target):
                    continue
                priority = 2 if translated else (0 if source == 'subtitles' else 1)
                candidates.append((priority, language != 'en', language, source, track))
    if not candidates:
        return None
    selected = min(candidates, key=lambda item: item[:3])
    return selected[2:]


def age_metadata(info):
    limit = info.get('age_limit')
    if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not math.isfinite(limit) or limit < 0:
        limit = None
    return {'minimum_age': limit,
            'restriction_status': 'unknown' if limit is None else ('restricted' if limit > 0 else 'not_reported'),
            'source': 'youtube_via_yt_dlp', 'content_suitability_rating': None,
            'note': 'An absent age restriction is not a child-suitability rating.'}


def caption_segments(raw):
    result = []
    for event in raw.get('events', []):
        text = ''.join(segment.get('utf8', '') for segment in event.get('segs', [])).strip()
        if not text:
            continue
        start = int(event.get('tStartMs', 0))
        result.append({'start_ms': start, 'end_ms': start + max(0, int(event.get('dDurationMs', 0))), 'text': text})
    return result


def storyboard(info):
    formats = [f for f in info.get('formats', []) if f.get('protocol') == 'mhtml' and f.get('fragments')]
    if not formats:
        return None
    selected = max(formats, key=lambda f: (f.get('width', 0) * f.get('height', 0), f.get('fps', 0)))
    duration, fps = info.get('duration'), selected.get('fps')
    if not duration or not fps or not all(selected.get(key) for key in ('rows', 'columns', 'width', 'height')):
        raise ValueError('Storyboard metadata lacks grid or timing information')
    return selected, round(duration * fps), 1000 / fps


def fetch(ydl, url):
    with ydl.urlopen(url) as response:
        data = response.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024:
        raise ValueError('Asset exceeds 32 MiB safety limit')
    return data


def save_captions(ydl, info, root, manifest):
    chosen = caption_track(info)
    if not chosen:
        manifest['captions'] = {'status': 'missing', 'reason': 'No English caption or English translation track available',
                                'available_languages': sorted(set(info.get('subtitles', {})) | set(info.get('automatic_captions', {})))}
        return
    language, source, track = chosen
    query = parse_qs(urlparse(track['url']).query)
    original = query.get('lang', [language])[0]
    target = query.get('tlang', [''])[0]
    translated = bool(target and target != original)
    details = {'language': 'en', 'track_language': language, 'source': source,
               'source_language': original, 'translated': translated,
               'translation_provider': 'youtube' if translated else None}
    raw_name = 'subtitles.raw.json'
    if manifest.get('caption_track') == details and valid_file(root, manifest, raw_name):
        raw = json.loads((root / raw_name).read_text())
    else:
        data = fetch(ydl, track['url'])
        if not data.strip():
            raise ValueError('YouTube returned an empty caption response; retry later with --resume')
        try:
            raw = json.loads(data)
        except (ValueError, UnicodeError) as error:
            raise ValueError('YouTube returned invalid caption JSON; retry later with --resume') from error
    if not isinstance(raw, dict):
        raise ValueError('Unexpected caption response structure')
    segments = caption_segments(raw)
    if not segments:
        raise ValueError('Caption track returned no text segments; retry later with --resume')
    manifest['caption_track'] = details
    record_json(root, manifest, raw_name, raw)
    record_json(root, manifest, 'subtitles.json', {**details, 'segments': segments})
    record(root, manifest, 'transcript.txt', ('\n'.join(s['text'] for s in segments) + '\n').encode())
    manifest['captions'] = {'status': 'complete', **details, 'segments': len(segments)}


def save_previews(ydl, info, root, manifest):
    selection = storyboard(info)
    if selection is None:
        manifest['previews'] = {'status': 'missing'}
        return
    board, count, interval = selection
    geometry = [board['format_id'], board['width'], board['height'], board['rows'], board['columns'], count, interval]
    if manifest.get('storyboard_geometry') != geometry:
        manifest['files'] = {k: v for k, v in manifest['files'].items() if not k.startswith(('storyboards/', 'previews/'))}
    manifest['storyboard_geometry'] = geometry
    width, height, columns, rows = board['width'], board['height'], board['columns'], board['rows']
    index = []
    for sheet_number, fragment in enumerate(board['fragments']):
        stem = f'storyboards/sheet_{sheet_number:04d}'
        cached = next((name for name in manifest['files'] if name.startswith(stem + '.') and valid_file(root, manifest, name)), None)
        data = (root / cached).read_bytes() if cached else fetch(ydl, fragment['url'])
        with Image.open(io.BytesIO(data)) as sheet:
            extension = {'JPEG': 'jpg', 'PNG': 'png', 'WEBP': 'webp'}.get(sheet.format)
            if not extension:
                raise ValueError('Unsupported storyboard image format')
            name = f'{stem}.{extension}'
            if not cached:
                record(root, manifest, name, data)
            sheet.load()
            for tile in range(min(rows * columns, count - len(index))):
                frame = len(index)
                x, y = tile % columns * width, tile // columns * height
                if x + width > sheet.width or y + height > sheet.height:
                    raise ValueError('Storyboard dimensions do not match advertised grid')
                timestamp = round(frame * interval)
                preview = f'previews/frame_{frame:06d}_{timestamp:09d}ms.jpg'
                if not valid_file(root, manifest, preview):
                    with sheet.crop((x, y, x + width, y + height)).convert('RGB') as crop:
                        output = io.BytesIO()
                        crop.save(output, format='JPEG', quality=90)
                        record(root, manifest, preview, output.getvalue(), checkpoint=False)
                index.append({'image': preview, 'timestamp_ms': timestamp, 'timing': 'estimated_from_duration_and_frame_count',
                              'sheet': name, 'crop': [x, y, width, height]})
            atomic_json(root / 'manifest.json', manifest)
    if len(index) != count:
        raise ValueError('Incomplete storyboard frame set')
    record_json(root, manifest, 'previews/index.json', index)
    manifest['previews'] = {'status': 'complete', 'frames': count, 'timing': 'estimated_from_duration_and_frame_count'}


def process_video(ydl, identifier, output, refresh=False, resume=False, skip=()):
    root = output / identifier
    if root.is_dir() and not refresh and not resume:
        return {'id': identifier, 'status': 'skipped', 'reason': 'folder_exists'}
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / 'manifest.json'
    manifest = {'schema_version': 1, 'id': identifier, 'files': {}, 'captions': {}, 'previews': {}}
    if manifest_path.exists() and not refresh:
        try:
            existing = json.loads(manifest_path.read_text())
            if existing.get('schema_version') == 1 and existing.get('id') == identifier:
                manifest = existing
        except (ValueError, OSError):
            pass
    if not refresh and manifest.get('status') == 'complete' and manifest.get('extraction_revision') == 2 and manifest['files'] and all(valid_file(root, manifest, name) for name in manifest['files']):
        return {'id': identifier, 'status': 'complete', 'cached': True}
    manifest.update(extraction_revision=2, status='in_progress', updated_at=datetime.now(timezone.utc).isoformat(), versions={'yt_dlp': YTDLP_VERSION, 'Pillow': PIL.__version__})
    atomic_json(manifest_path, manifest)
    try:
        info = ydl.extract_info(f'https://www.youtube.com/watch?v={identifier}', download=False)
        if not info or info.get('id') != identifier:
            raise ValueError('Extractor returned no matching video')
        metadata = {key: info.get(key) for key in ('id', 'title', 'duration', 'channel', 'channel_id', 'upload_date', 'webpage_url', 'age_limit')}
        metadata['age_rating'] = age_metadata(info)
        record_json(root, manifest, 'metadata.json', metadata)
        for component, action in (('captions', save_captions), ('previews', save_previews)):
            if component in skip:
                manifest[component] = {'status': 'skipped'}
                continue
            for attempt in range(2):
                try:
                    action(ydl, info, root, manifest)
                    break
                except BatchStopped:
                    raise
                except Exception as error:
                    if ydl.policy.stopped:
                        ydl.policy.stop()
                    if isinstance(error, HTTPError) and error.status == 403 and attempt == 0:
                        error.close()
                        info = ydl.extract_info(f'https://www.youtube.com/watch?v={identifier}', download=False)
                        continue
                    manifest[component] = {'status': 'failed', 'error': safe_error(error)}
                    break
            atomic_json(manifest_path, manifest)
        manifest['status'] = 'complete' if all(manifest[c].get('status') == 'complete' for c in ('captions', 'previews')) else 'incomplete'
    except (BatchStopped, KeyboardInterrupt):
        manifest['status'] = 'interrupted'
        raise
    except Exception as error:
        manifest.update(status='failed', error=safe_error(error))
        if ydl.policy.stopped:
            ydl.policy.stop()
    finally:
        atomic_json(manifest_path, manifest)
    return {'id': identifier, 'status': manifest['status']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('videos', nargs='*')
    parser.add_argument('--ids-file', type=Path)
    parser.add_argument('--output', type=Path, default=Path('youtube-data'))
    parser.add_argument('--proxy', default=os.environ.get('YOUTUBE_PROXY'))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--refresh', action='store_true', help='Re-extract even if the ID folder exists')
    mode.add_argument('--resume', action='store_true', help='Resume incomplete existing folders and validate completed output')
    parser.add_argument('--request-delay', type=float, nargs=2, default=(1, 2), metavar=('MIN', 'MAX'))
    parser.add_argument('--video-delay', type=float, nargs=2, default=(5, 10), metavar=('MIN', 'MAX'))
    args = parser.parse_args(argv)
    for pair in (args.request_delay, args.video_delay):
        if not 0 <= pair[0] <= pair[1] or not all(math.isfinite(n) for n in pair):
            parser.error('Delay ranges must be finite, nonnegative, and ordered')
    try:
        inputs = args.videos + ([line.strip() for line in args.ids_file.read_text().splitlines() if line.strip() and not line.lstrip().startswith('#')] if args.ids_file else [])
        identifiers = list(dict.fromkeys(video_id(value) for value in inputs))
    except (ValueError, OSError) as error:
        parser.error(safe_error(error))
    if not identifiers:
        parser.error('Pass at least one video ID/URL or --ids-file')
    args.output.mkdir(parents=True, exist_ok=True)
    # Prevent separate invocations from silently multiplying request rates.
    lock = args.output / '.scraper.lock'
    try:
        lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        parser.error(f'{lock} exists; another run may be active. Remove only if that process has stopped.')
    os.close(lock_fd)
    results, code = [], 0
    policy = NetworkPolicy(*args.request_delay)
    try:
        with PacedYoutubeDL(policy, args.proxy) as ydl:
            for position, identifier in enumerate(identifiers):
                will_fetch = args.refresh or args.resume or not (args.output / identifier).is_dir()
                if position and will_fetch:
                    time.sleep(random.uniform(*args.video_delay))
                print(f'[{position + 1}/{len(identifiers)}] {identifier}', flush=True)
                result = process_video(ydl, identifier, args.output, args.refresh, args.resume)
                results.append(result)
                print(result['status'], flush=True)
                atomic_json(args.output / 'batch-summary.json', {'results': results, 'pending': identifiers[len(results):]})
        code = 1 if any(r['status'] not in ('complete', 'skipped') for r in results) else 0
    except BatchStopped as error:
        print(safe_error(error), file=sys.stderr)
        code = 3
    except KeyboardInterrupt:
        code = 130
    finally:
        atomic_json(args.output / 'batch-summary.json', {'results': results, 'pending': identifiers[len(results):], 'exit_code': code})
        lock.unlink(missing_ok=True)
    return code


if __name__ == '__main__':
    sys.exit(main())
