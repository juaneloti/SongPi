import os
import threading
import time
from flask import Flask, render_template, send_from_directory, jsonify
from flask_socketio import SocketIO, emit
from shazamio import Shazam
import pyaudio
import wave
import json
from PIL import Image
import requests

app = Flask(__name__)
socketio = SocketIO(app)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')
HISTORY_PATH = os.path.join(os.path.dirname(__file__), 'song_history.log')
COVER_CACHE_DIR = os.path.join(os.path.dirname(__file__), 'cover_cache')

os.makedirs(COVER_CACHE_DIR, exist_ok=True)

# Load config
def load_config():
    with open(CONFIG_PATH, 'r') as f:
        return json.load(f)

config = load_config()

# Song recognition logic
def record_audio(filename, duration=5, rate=44100, channels=1, chunk=1024, device_index=None):
    p = pyaudio.PyAudio()
    stream = p.open(format=pyaudio.paInt16,
                    channels=channels,
                    rate=rate,
                    input=True,
                    frames_per_buffer=chunk,
                    input_device_index=device_index)
    frames = []
    for _ in range(0, int(rate / chunk * duration)):
        try:
            data = stream.read(chunk, exception_on_overflow=False)
        except IOError as e:
            print(f"[Audio Warning] Input overflowed: {e}")
            data = b'\x00' * chunk * 2  # silence
        frames.append(data)
    stream.stop_stream()
    stream.close()
    p.terminate()
    wf = wave.open(filename, 'wb')
    wf.setnchannels(channels)
    wf.setsampwidth(p.get_sample_size(pyaudio.paInt16))
    wf.setframerate(rate)
    wf.writeframes(b''.join(frames))
    wf.close()

async def recognize_song(audio_path):
    shazam = Shazam()
    out = await shazam.recognize(audio_path)
    return out

# Song state
data = {
    'current': None,
    'history': []
}

def save_history(song):
    with open(HISTORY_PATH, 'a') as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {song['artist']} | {song['title']}\n")

def cache_cover(url, song_id):
    path = os.path.join(COVER_CACHE_DIR, f"{song_id}.jpg")
    if not os.path.exists(path):
        r = requests.get(url)
        if r.status_code == 200:
            with open(path, 'wb') as f:
                f.write(r.content)
    return f"/cover_cache/{song_id}.jpg"

# Background recognition thread
def recognition_loop():
    while True:
        audio_file = 'temp.wav'
        record_audio(audio_file, duration=config.get('record_seconds', 5),
                     rate=config.get('sample_rate', 44100),
                     channels=config.get('channels', 1),
                     chunk=config.get('chunk_size', 1024),
                     device_index=config.get('device_index'))
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(recognize_song(audio_file))
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
            if not data['current'] or (song['title'] != data['current']['title'] or song['artist'] != data['current']['artist']):
                data['current'] = song
                data['history'].insert(0, song)
                data['history'] = data['history'][:config.get('history_max', 10)]
                save_history(song)
                socketio.emit('song_update', song)
        time.sleep(config.get('interval', 10))

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