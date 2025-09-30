import os
import threading
import time
import logging
import hashlib
import random
import json
from flask import Flask, render_template, send_from_directory, jsonify
from flask_socketio import SocketIO, emit
from shazamio import Shazam
import pyaudio
import wave
from PIL import Image  # noqa: F401 (PIL may be used elsewhere / template expectations)
import requests

# --------------------------------------------------
# Logging setup
# --------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("songpi")

app = Flask(__name__)
socketio = SocketIO(app)

BASE_DIR = os.path.dirname(__file__)
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
HISTORY_PATH = os.path.join(BASE_DIR, 'song_history.log')
COVER_CACHE_DIR = os.path.join(BASE_DIR, 'cover_cache')

os.makedirs(COVER_CACHE_DIR, exist_ok=True)

def load_config():
    """Load JSON config but tolerate lines with inline comments starting with #.

    Standard json does not allow comments; the provided config includes one.
    We strip anything after a # (unless inside quotes – kept simple: assume no # in string literals).
    """
    try:
        with open(CONFIG_PATH, 'r') as f:
            raw_lines = f.readlines()
        cleaned = []
        for line in raw_lines:
            # crude comment stripper – if # appears before a quote pair ends, strip
            if '#' in line:
                hash_index = line.find('#')
                # keep content before '#'
                line = line[:hash_index]
            if line.strip():
                cleaned.append(line)
        text = ''.join(cleaned)
        return json.loads(text)
    except Exception as e:
        logger.warning(f"Failed to load config.json cleanly ({e}); using defaults where needed.")
        return {}

config = load_config()

# --------------------------------------------------
# Rate limiting / pacing configuration (with fallbacks)
# --------------------------------------------------
MIN_INTERVAL_UNKNOWN = config.get('min_interval_unknown', config.get('interval', 10))
HOLD_TIME_AFTER_MATCH = config.get('hold_time_after_match', 300)  # seconds to pause after a successful match
BACKOFF_BASE_SECONDS = config.get('backoff_base_seconds', 5)
BACKOFF_MAX_SECONDS = config.get('backoff_max_seconds', 180)
MAX_FAILURE_BEFORE_PAUSE = config.get('max_failure_before_pause', 5)
PAUSE_AFTER_MAX_FAILURES = config.get('pause_after_max_failures', 120)
JITTER_SECONDS = config.get('jitter_seconds', 5)

logger.info(
    "Pacing config: min_interval_unknown=%ss hold_after_match=%ss backoff_base=%ss backoff_max=%ss max_fail_before_pause=%s pause_after_max_failures=%ss jitter=%ss",
    MIN_INTERVAL_UNKNOWN, HOLD_TIME_AFTER_MATCH, BACKOFF_BASE_SECONDS, BACKOFF_MAX_SECONDS,
    MAX_FAILURE_BEFORE_PAUSE, PAUSE_AFTER_MAX_FAILURES, JITTER_SECONDS
)

# Song recognition logic
def record_audio(filename, duration=5, rate=44100, channels=1, chunk=1024, device_index=None):
    """Capture audio into a WAV file.

    If the audio device is unavailable or overflows, we still produce a silent file
    so that the recognition loop can continue gracefully.
    """
    frames = []
    p = None
    stream = None
    try:
        p = pyaudio.PyAudio()
        stream = p.open(format=pyaudio.paInt16,
                        channels=channels,
                        rate=rate,
                        input=True,
                        frames_per_buffer=chunk,
                        input_device_index=device_index)
        for _ in range(0, int(rate / chunk * duration)):
            try:
                data = stream.read(chunk, exception_on_overflow=False)
            except IOError as e:
                logger.warning(f"Audio overflow or read issue: {e}; inserting silence chunk")
                data = b'\x00' * chunk * 2  # silence
            frames.append(data)
    except Exception as e:
        logger.error(f"Audio capture failed ({e}); generating silence.")
        # generate silence about 'duration' seconds
        silence_frames = int(rate / chunk * duration)
        frames = [b'\x00' * chunk * 2] * silence_frames
    finally:
        try:
            if stream:
                stream.stop_stream()
                stream.close()
        except Exception:
            pass
        try:
            if p:
                p.terminate()
        except Exception:
            pass

    try:
        wf = wave.open(filename, 'wb')
        wf.setnchannels(channels)
        wf.setsampwidth(pyaudio.PyAudio().get_sample_size(pyaudio.paInt16))  # might create a short-lived PyAudio
        wf.setframerate(rate)
        wf.writeframes(b''.join(frames))
        wf.close()
    except Exception as e:
        logger.error(f"Failed to write WAV file {filename}: {e}")

async def recognize_song(audio_path):
    shazam = Shazam()
    return await shazam.recognize(audio_path)

# Song state
data = {
    'current': None,
    'history': []
}

# Internal pacing / recognition state
recognition_state = {
    'attempt': 0,
    'last_attempt': 0.0,
    'last_success': 0.0,
    'failure_count': 0,
    'last_audio_sig': None,  # sha1 of last wav content
    'current_track_id': None,
}

def save_history(song):
    try:
        with open(HISTORY_PATH, 'a') as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {song['artist']} | {song['title']}\n")
    except Exception as e:
        logger.warning(f"Failed writing history: {e}")

def cache_cover(url, song_id):
    path = os.path.join(COVER_CACHE_DIR, f"{song_id}.jpg")
    if not os.path.exists(path):
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                with open(path, 'wb') as f:
                    f.write(r.content)
            else:
                logger.info(f"Cover fetch non-200 ({r.status_code}) for song_id={song_id}")
        except Exception as e:
            logger.warning(f"Cover fetch failed ({e}) for song_id={song_id}")
    return f"/cover_cache/{song_id}.jpg"

# Background recognition thread
def recognition_loop():
    """Continuous recognition loop with pacing, backoff, and logging."""
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    while True:
        now = time.time()

        # HOLD phase: skip recognition after a successful match for HOLD_TIME_AFTER_MATCH
        if recognition_state['current_track_id'] and recognition_state['last_success'] > 0:
            elapsed_since_success = now - recognition_state['last_success']
            if elapsed_since_success < HOLD_TIME_AFTER_MATCH:
                remaining = HOLD_TIME_AFTER_MATCH - elapsed_since_success
                sleep_time = min(remaining, MIN_INTERVAL_UNKNOWN)
                logger.debug(
                    f"Skip recognition (holding current track) remaining_hold=%.1fs sleep=%.1fs",
                    remaining, sleep_time
                )
                time.sleep(sleep_time)
                continue

        audio_file = 'temp.wav'
        record_audio(
            audio_file,
            duration=config.get('record_seconds', 5),
            rate=config.get('sample_rate', 44100),
            channels=config.get('channels', 1),
            chunk=config.get('chunk_size', 1024),
            device_index=config.get('device_index')
        )

        # Hash audio to detect duplicates
        try:
            with open(audio_file, 'rb') as f:
                audio_bytes = f.read()
            audio_sig = hashlib.sha1(audio_bytes).hexdigest()
        except Exception as e:
            logger.error(f"Failed reading audio file for hashing: {e}")
            audio_sig = None

        # Duplicate skip: if identical to last and last attempt very recent
        if (audio_sig is not None and audio_sig == recognition_state['last_audio_sig'] and
                (now - recognition_state['last_attempt']) < MIN_INTERVAL_UNKNOWN):
            logger.debug("Skipping duplicate audio within min interval.")
            time.sleep(MIN_INTERVAL_UNKNOWN)
            continue

        recognition_state['attempt'] += 1
        recognition_state['last_attempt'] = now
        recognition_state['last_audio_sig'] = audio_sig

        # Run recognition
        result = None
        error = None
        try:
            result = loop.run_until_complete(recognize_song(audio_file))
        except Exception as e:
            error = e

        next_sleep = MIN_INTERVAL_UNKNOWN
        status = 'unknown'

        if error:
            recognition_state['failure_count'] += 1
            status = 'error'
            if recognition_state['failure_count'] >= MAX_FAILURE_BEFORE_PAUSE:
                next_sleep = PAUSE_AFTER_MAX_FAILURES
            else:
                next_sleep = min(
                    BACKOFF_BASE_SECONDS * (2 ** (recognition_state['failure_count'] - 1)),
                    BACKOFF_MAX_SECONDS
                )
            logger.warning(
                f"[Recognize] attempt={recognition_state['attempt']} status=error failures={recognition_state['failure_count']} "
                f"sleep={next_sleep}s err={error}"
            )
        else:
            if result and 'track' in result:
                track = result['track']
                song_id = track.get('key', str(int(time.time())))
                song = {
                    'title': track.get('title', 'Unknown'),
                    'artist': track.get('subtitle', 'Unknown'),
                    'cover_url': track['images']['coverart'] if 'images' in track and 'coverart' in track['images'] else None,
                    'song_id': song_id
                }
                if song['cover_url']:
                    song['cover_path'] = cache_cover(song['cover_url'], song_id)
                else:
                    song['cover_path'] = None

                is_new = (not data['current']) or (
                    song['title'] != data['current']['title'] or song['artist'] != data['current']['artist']
                )
                recognition_state['failure_count'] = 0
                recognition_state['last_success'] = time.time()
                recognition_state['current_track_id'] = song_id
                status = 'match' if is_new else 'duplicate_match'

                if is_new:
                    data['current'] = song
                    data['history'].insert(0, song)
                    data['history'] = data['history'][:config.get('history_max', 10)]
                    save_history(song)
                    socketio.emit('song_update', song)
                next_sleep = MIN_INTERVAL_UNKNOWN  # immediate hold enforced at loop top
                logger.info(
                    f"[Recognize] attempt={recognition_state['attempt']} status={status} title=\"{song['title']}\" artist=\"{song['artist']}\" sleep={next_sleep}s"
                )
            else:
                # No track recognized
                recognition_state['failure_count'] += 1
                status = 'no_match'
                # Mild backoff after a few misses
                if recognition_state['failure_count'] > 2:
                    next_sleep = min(
                        BACKOFF_BASE_SECONDS * (2 ** (recognition_state['failure_count'] - 3)),
                        BACKOFF_MAX_SECONDS
                    )
                logger.info(
                    f"[Recognize] attempt={recognition_state['attempt']} status=no_match failures={recognition_state['failure_count']} sleep={next_sleep}s"
                )

        # Add jitter
        jitter = random.uniform(0, JITTER_SECONDS)
        next_sleep_with_jitter = next_sleep + jitter
        logger.debug(
            f"Sleeping for {next_sleep_with_jitter:.2f}s (base={next_sleep}s jitter={jitter:.2f}s) status={status}"
        )
        time.sleep(next_sleep_with_jitter)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/cover_cache/<filename>')
def cover_cache(filename):
    return send_from_directory(COVER_CACHE_DIR, filename)

@app.route('/api/current')
def api_current():
    return jsonify(data['current'])

@app.route('/api/history')
def api_history():
    return jsonify(data['history'])


# Start recognition thread on import (so it works with Gunicorn)
threading.Thread(target=recognition_loop, daemon=True).start()

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)