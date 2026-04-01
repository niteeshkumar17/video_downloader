# ClipDown — Video Downloader

A self-hosted video and audio downloader with a clean, modern web UI. Paste links from YouTube, TikTok, Instagram, Twitter/X, and 1000+ other sites — download as MP4 or MP3.

Inspired by [ReClip](https://github.com/averygan/reclip).

## Features

- 🎬 Download videos from 1000+ supported sites (via [yt-dlp](https://github.com/yt-dlp/yt-dlp))
- 🎵 MP4 video or MP3 audio extraction
- 📊 Quality/resolution picker
- 📋 Bulk downloads — paste multiple URLs at once
- 🔗 Automatic URL deduplication
- 🎨 Premium dark blue UI — no frameworks, no build step
- 🐍 Single Python file backend (~160 lines)
- 🐳 Docker ready — deploy on Render, Railway, or any cloud

## Quick Start (Local)

### Prerequisites

- Python 3.9+
- ffmpeg (`choco install ffmpeg` on Windows, `brew install ffmpeg` on Mac)
- yt-dlp (`pip install yt-dlp`)

### Run Locally

```bash
pip install -r requirements.txt
python app.py
```

Open http://localhost:8899

### Run with Docker

```bash
docker build -t clipdown .
docker run -p 8899:8899 clipdown
```

## Deploy on Render

1. Create a new **Web Service** on [render.com](https://render.com)
2. Connect your GitHub repository
3. Set the **Root Directory** to `video_downloader`
4. Set **Environment** to **Docker**
5. Set the port to **8899**
6. Deploy!

The `Dockerfile` handles everything — Python, ffmpeg, yt-dlp, and gunicorn.

## Tech Stack

- **Backend**: Python + Flask
- **Frontend**: Vanilla HTML/CSS/JS (single file, no build step)
- **Download Engine**: [yt-dlp](https://github.com/yt-dlp/yt-dlp) + [ffmpeg](https://ffmpeg.org/)
- **Production Server**: Gunicorn
- **Containerization**: Docker

## Supported Sites

Anything [yt-dlp supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md), including:

YouTube, TikTok, Instagram, Twitter/X, Reddit, Facebook, Vimeo, Twitch, Dailymotion, SoundCloud, Loom, Streamable, Pinterest, Tumblr, Threads, LinkedIn, and many more.

## Disclaimer

This tool is intended for personal use only. Please respect copyright laws and the terms of service of the platforms you download from.

## License

MIT
