# SongPi - Docker Version (Web GUI)

This version of SongPi replaces the Tkinter desktop GUI with a web-based interface, making it suitable for running in a Docker container and accessing via a browser.

## Features
- Automatic song recognition using Shazamio
- Web-based GUI (Flask + SocketIO)
- Displays current song info, cover art, and song history
- Designed for containerized deployment (Docker)

## How to Run
1. Build and run the Docker container (see Dockerfile and docker-compose.yml)
2. Access the web interface at `http://localhost:5000` (or the mapped port)

## Configuration
- Edit `config.json` for audio and app settings
- Audio input must be available to the container (see Docker docs for device passthrough)

---

This directory contains the web-based version of SongPi. The main app entry point is `app.py`.
