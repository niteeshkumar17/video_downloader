# ClipDown — Video Downloader

A self-hosted video and audio downloader with a clean, modern web UI. Paste links from YouTube, TikTok, Instagram, Twitter/X, and 1000+ other sites — download as MP4 or MP3.

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

## UptimeRobot Keep-Alive

To reduce Render cold starts, configure UptimeRobot to ping this lightweight endpoint:

- URL: `https://YOUR-DOMAIN/api/health` (or `https://YOUR-DOMAIN/healthz`)
- Method: `GET`
- Interval: every `5` minutes
- Timeout: `30` seconds
- Expected body contains: `"status":"ok"`

Note: keep-alive helps with idle spin-down. It does not fix YouTube datacenter IP trust/bot-check restrictions.

## YouTube Notes (Important)

- This app does not bypass YouTube access controls.
- Some YouTube videos require a valid logged-in session (cookies).
- Local run option: set `YTDLP_COOKIES_FROM_BROWSER` (example: `chrome`) so yt-dlp reads cookies from your browser profile.
- Server option: provide fresh exported cookies via `YTDLP_COOKIES` (base64-encoded `cookies.txt`).
- Cloud/datacenter note: YouTube may still block requests from some server IP ranges even with cookies.
- Optional mitigation: set `YTDLP_PROXY` to a trusted residential/ISP proxy endpoint.
- If you see `Login with OAuth is no longer supported`, refresh/export cookies again and retry.
- Never commit real cookie files to git.

## Terabox Notes

- Terabox links are handled through Terabox web APIs (not yt-dlp generic extraction).
- Some links return `need verify` / `need verify_v2` until you provide a logged-in Terabox session cookie.
- You can provide this via either:
	- `TERABOX_COOKIE` environment variable (full cookie header string), or
	- `video_downloader/terabox_cookies.txt` file (same cookie header string).
- The app will also try to read Terabox cookies from `cookies.txt` if present in Netscape format.

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
