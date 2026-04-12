import os
import re
import uuid
import glob
import json
import subprocess
import threading
import base64
import logging
from urllib.parse import parse_qs, urlparse
from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

COOKIES_FILE = os.path.join(os.path.dirname(__file__), "cookies.txt")
COOKIES_FROM_BROWSER = os.environ.get("YTDLP_COOKIES_FROM_BROWSER", "").strip()
YTDLP_PROXY = os.environ.get("YTDLP_PROXY", "").strip()
YTDLP_USER_AGENT = os.environ.get("YTDLP_USER_AGENT", "").strip()

# If YTDLP_COOKIES env var is set (base64 encoded cookies.txt), write it to file
if os.environ.get("YTDLP_COOKIES"):
    try:
        decoded = base64.b64decode(os.environ["YTDLP_COOKIES"])
        with open(COOKIES_FILE, "wb") as f:
            f.write(decoded)
        logger.info("Loaded cookies from YTDLP_COOKIES environment variable")
    except Exception as e:
        logger.error(f"Failed to decode YTDLP_COOKIES: {e}")

if COOKIES_FROM_BROWSER:
    logger.info(f"Using browser cookies via YTDLP_COOKIES_FROM_BROWSER={COOKIES_FROM_BROWSER}")
if YTDLP_PROXY:
    logger.info("Using yt-dlp proxy from YTDLP_PROXY")


def get_cookie_args():
    """Return cookie arguments for yt-dlp.

    Priority:
    1) YTDLP_COOKIES_FROM_BROWSER (local/runtime browser profile)
    2) cookies.txt file
    """
    if COOKIES_FROM_BROWSER:
        return ["--cookies-from-browser", COOKIES_FROM_BROWSER]
    if os.path.isfile(COOKIES_FILE):
        return ["--cookies", COOKIES_FILE]
    return []


def get_ytdlp_network_args():
    """Return network-related yt-dlp args from environment."""
    args = []
    if YTDLP_PROXY:
        args += ["--proxy", YTDLP_PROXY]
    if YTDLP_USER_AGENT:
        args += ["--user-agent", YTDLP_USER_AGENT]
    return args


def is_youtube_url(url):
    """Check if a URL is a YouTube URL."""
    youtube_patterns = [
        r"(https?://)?(www\.)?youtube\.com/watch",
        r"(https?://)?(www\.)?youtube\.com/shorts",
        r"(https?://)?(www\.)?youtube\.com/embed",
        r"(https?://)?youtu\.be/",
        r"(https?://)?m\.youtube\.com/watch",
    ]
    return any(re.match(pattern, url) for pattern in youtube_patterns)


def normalize_video_url(url):
    """Normalize known URL variants to improve extractor compatibility."""
    url = (url or "").strip()
    if not url:
        return url

    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        path = parsed.path or ""

        # youtu.be/<id> -> youtube watch URL
        if "youtu.be" in host:
            video_id = path.strip("/").split("/")[0]
            if video_id:
                return f"https://www.youtube.com/watch?v={video_id}"

        if "youtube.com" in host or "m.youtube.com" in host:
            # /shorts/<id> -> watch
            if path.startswith("/shorts/"):
                video_id = path.split("/shorts/", 1)[1].split("/")[0]
                if video_id:
                    return f"https://www.youtube.com/watch?v={video_id}"

            # /embed/<id> -> watch
            if path.startswith("/embed/"):
                video_id = path.split("/embed/", 1)[1].split("/")[0]
                if video_id:
                    return f"https://www.youtube.com/watch?v={video_id}"

            # Keep watch URLs canonical
            if path == "/watch":
                qs = parse_qs(parsed.query or "")
                video_id = (qs.get("v") or [""])[0]
                if video_id:
                    return f"https://www.youtube.com/watch?v={video_id}"
    except Exception:
        # If parsing fails, use original input.
        return url

    return url


def normalize_ytdlp_error(error_message, is_youtube=False):
    """Convert raw yt-dlp errors into short actionable messages."""
    message = (error_message or "").strip()
    if not message:
        return "Unknown error"

    if is_youtube:
        lowered = message.lower()
        youtube_auth_markers = [
            "login with oauth is no longer supported",
            "use --cookies-from-browser or --cookies",
            "sign in to confirm",
            "confirm your age",
            "confirm you're not a bot",
        ]
        if any(marker in lowered for marker in youtube_auth_markers):
            return (
                "YouTube requires a valid logged-in session for this video. "
                "Use fresh browser cookies (set YTDLP_COOKIES_FROM_BROWSER=chrome) "
                "or provide a fresh cookies.txt via YTDLP_COOKIES. "
                "On cloud/datacenter IPs, YouTube may still block requests; use a residential egress or set YTDLP_PROXY."
            )

    return message.split("\n")[-1]


def build_ytdlp_strategies(url):
    """Build retry strategies for yt-dlp args."""
    cookie_args = get_cookie_args()
    if is_youtube_url(url):
        youtube_overrides = [
            [],
            ["--extractor-args", "youtube:player_client=web_safari"],
            ["--extractor-args", "youtube:player_client=android"],
        ]

        strategies = []
        if cookie_args:
            strategies.extend([cookie_args + override for override in youtube_overrides])
        strategies.extend(youtube_overrides)
        return strategies

    return [cookie_args] if cookie_args else [[]]


jobs = {}


def cleanup_old_files():
    """Remove downloaded files older than 30 minutes to save disk space on Render."""
    import time
    threshold = time.time() - 1800  # 30 minutes
    for f in glob.glob(os.path.join(DOWNLOAD_DIR, "*")):
        try:
            if os.path.getmtime(f) < threshold:
                os.remove(f)
        except OSError:
            pass


# Clients to try in order — WEB triggers auto PO token generation via nodejs
PYTUBE_CLIENTS = ['WEB', 'WEB_EMBED', 'ANDROID']


def _create_youtube(url, client=None):
    """Create a pytubefix YouTube object with the given client."""
    from pytubefix import YouTube
    if client:
        return YouTube(url, client=client)
    return YouTube(url)


# ── pytubefix-based YouTube handlers ────────────────────────────────────────

def pytube_get_info(url):
    """Fetch video info using pytubefix with PO token via auto client rotation."""
    last_error = None
    for client in PYTUBE_CLIENTS:
        try:
            logger.info(f"pytubefix: trying client={client} for info")
            yt = _create_youtube(url, client=client)

            # Build quality options from available streams
            formats = []
            seen_heights = set()

            # Get adaptive video streams for high quality options
            for stream in yt.streams.filter(adaptive=True, only_video=True).order_by("resolution").desc():
                height = stream.resolution  # e.g. "1080p"
                if height and height not in seen_heights:
                    seen_heights.add(height)
                    height_int = int(height.replace("p", ""))
                    formats.append({
                        "id": f"pytube_{stream.itag}",
                        "label": height,
                        "height": height_int,
                    })

            # Also include progressive streams as fallback
            for stream in yt.streams.filter(progressive=True).order_by("resolution").desc():
                height = stream.resolution
                if height and height not in seen_heights:
                    seen_heights.add(height)
                    height_int = int(height.replace("p", ""))
                    formats.append({
                        "id": f"pytube_prog_{stream.itag}",
                        "label": f"{height}",
                        "height": height_int,
                    })

            formats.sort(key=lambda x: x["height"], reverse=True)

            result = {
                "title": yt.title or "",
                "thumbnail": yt.thumbnail_url or "",
                "duration": yt.length,
                "uploader": yt.author or "",
                "formats": formats,
            }
            logger.info(f"pytubefix: client={client} succeeded for info")
            return result
        except Exception as e:
            last_error = e
            logger.warning(f"pytubefix: client={client} failed for info: {e}")
    raise Exception(f"All pytubefix clients failed. Last error: {last_error}")


def pytube_download(job_id, url, format_choice, format_id):
    """Download a YouTube video using pytubefix with auto PO token via client rotation."""
    last_error = None
    for client in PYTUBE_CLIENTS:
        try:
            logger.info(f"pytubefix: trying client={client} for download")
            result = _pytube_download_with_client(job_id, url, format_choice, format_id, client)
            logger.info(f"pytubefix: client={client} succeeded for download")
            return result
        except Exception as e:
            last_error = e
            logger.warning(f"pytubefix: client={client} failed for download: {e}")
            # Clean up partial files before trying next client
            for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}*")):
                try:
                    os.remove(f)
                except OSError:
                    pass
    raise Exception(f"All pytubefix clients failed. Last error: {last_error}")


def _pytube_download_with_client(job_id, url, format_choice, format_id, client):
    """Internal download function using a specific pytubefix client."""
    yt = _create_youtube(url, client=client)
    output_path = DOWNLOAD_DIR

    if format_choice == "audio":
        # Download audio only as mp3
        stream = yt.streams.get_audio_only()
        if not stream:
            raise Exception("No audio stream available")
        out_file = stream.download(output_path=output_path, filename=f"{job_id}_audio")
        # Convert to mp3 using ffmpeg
        mp3_file = os.path.join(output_path, f"{job_id}.mp3")
        cmd = ["ffmpeg", "-y", "-i", out_file, "-vn", "-acodec", "libmp3lame", "-ab", "192k", mp3_file]
        subprocess.run(cmd, capture_output=True, timeout=120)
        # Remove original
        try:
            os.remove(out_file)
        except OSError:
            pass
        return mp3_file, yt.title

    elif format_id and format_id.startswith("pytube_prog_"):
        # Progressive stream (has audio+video in one file)
        itag = int(format_id.replace("pytube_prog_", ""))
        stream = yt.streams.get_by_itag(itag)
        if not stream:
            raise Exception(f"Stream with itag {itag} not found")
        out_file = stream.download(output_path=output_path, filename=f"{job_id}")
        # Rename to .mp4 if needed
        base, ext = os.path.splitext(out_file)
        if ext != ".mp4":
            mp4_file = f"{base}.mp4"
            os.rename(out_file, mp4_file)
            return mp4_file, yt.title
        return out_file, yt.title

    elif format_id and format_id.startswith("pytube_"):
        # Adaptive stream — need to download video + audio separately and merge
        itag = int(format_id.replace("pytube_", ""))
        video_stream = yt.streams.get_by_itag(itag)
        if not video_stream:
            raise Exception(f"Video stream with itag {itag} not found")

        audio_stream = yt.streams.get_audio_only()
        if not audio_stream:
            raise Exception("No audio stream available for merging")

        # Download both
        video_file = video_stream.download(output_path=output_path, filename=f"{job_id}_video")
        audio_file = audio_stream.download(output_path=output_path, filename=f"{job_id}_audio")

        # Merge with ffmpeg
        merged_file = os.path.join(output_path, f"{job_id}.mp4")
        cmd = [
            "ffmpeg", "-y",
            "-i", video_file,
            "-i", audio_file,
            "-c:v", "copy", "-c:a", "aac",
            "-movflags", "+faststart",
            merged_file
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        # Cleanup temp files
        for f in [video_file, audio_file]:
            try:
                os.remove(f)
            except OSError:
                pass

        if result.returncode != 0:
            raise Exception(f"FFmpeg merge failed: {result.stderr[:200]}")

        return merged_file, yt.title

    else:
        # Default: get highest resolution progressive, or adaptive + merge
        stream = yt.streams.get_highest_resolution()
        if stream:
            out_file = stream.download(output_path=output_path, filename=f"{job_id}")
            base, ext = os.path.splitext(out_file)
            if ext != ".mp4":
                mp4_file = f"{base}.mp4"
                os.rename(out_file, mp4_file)
                return mp4_file, yt.title
            return out_file, yt.title
        else:
            raise Exception("No suitable stream found")


# ── yt-dlp based handlers ──────────────────────────────────────────────────

def ytdlp_get_info(url):
    """Fetch video info using yt-dlp (with retry strategies for YouTube)."""
    youtube = is_youtube_url(url)
    strategies = build_ytdlp_strategies(url)

    last_error = "Unknown error"
    for extra_args in strategies:
        cmd = ["yt-dlp", "--no-playlist", "--no-warnings", "-j"] + get_ytdlp_network_args() + extra_args + [url]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode == 0:
                info = json.loads(result.stdout)
                # Build quality options
                best_by_height = {}
                for f in info.get("formats", []):
                    height = f.get("height")
                    if height and f.get("vcodec", "none") != "none":
                        tbr = f.get("tbr") or 0
                        if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                            best_by_height[height] = f

                formats = []
                for height, f in best_by_height.items():
                    formats.append({
                        "id": f["format_id"],
                        "label": f"{height}p",
                        "height": height,
                    })
                formats.sort(key=lambda x: x["height"], reverse=True)

                return {
                    "title": info.get("title", ""),
                    "thumbnail": info.get("thumbnail", ""),
                    "duration": info.get("duration"),
                    "uploader": info.get("uploader", ""),
                    "formats": formats,
                }
            else:
                stderr = (result.stderr or result.stdout or "").strip()
                last_error = normalize_ytdlp_error(stderr, youtube)
                logger.warning(f"yt-dlp strategy failed: {last_error}")
        except subprocess.TimeoutExpired:
            last_error = "Timed out fetching video info"
        except Exception as e:
            last_error = str(e)

    raise Exception(normalize_ytdlp_error(last_error, youtube))


def run_download(job_id, url, format_choice, format_id):
    job = jobs[job_id]
    youtube = is_youtube_url(url)

    # ── Try pytubefix first for YouTube URLs ────────────────────────────────
    if youtube:
        try:
            logger.info(f"[{job_id}] Attempting pytubefix download for YouTube URL")
            filepath, title = pytube_download(job_id, url, format_choice, format_id)

            if os.path.isfile(filepath):
                job["status"] = "done"
                job["file"] = filepath
                ext = os.path.splitext(filepath)[1]
                title = title or job.get("title", "")
                if title:
                    safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:80].strip()
                    job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(filepath)
                else:
                    job["filename"] = os.path.basename(filepath)
                logger.info(f"[{job_id}] pytubefix download successful")
                return
            else:
                logger.warning(f"[{job_id}] pytubefix: file not found after download")
        except Exception as e:
            logger.warning(f"[{job_id}] pytubefix download failed: {e}, falling back to yt-dlp")
            # Clean up any partial files
            for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}*")):
                try:
                    os.remove(f)
                except OSError:
                    pass

    # ── Fallback to yt-dlp ──────────────────────────────────────────────────
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    # Build strategy list for yt-dlp
    base_cmd = ["yt-dlp", "--no-playlist", "--no-warnings"] + get_ytdlp_network_args() + ["-o", out_template]

    format_args = []
    if format_choice == "audio":
        format_args = ["-x", "--audio-format", "mp3"]
    elif format_id and not format_id.startswith("pytube_"):
        format_args = ["-f", f"{format_id}+bestaudio/{format_id}/bestvideo+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        format_args = ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]

    strategies = build_ytdlp_strategies(url)

    last_error = "Unknown error"
    for extra_args in strategies:
        # Clean up any partial files from previous attempt
        for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*")):
            try:
                os.remove(f)
            except OSError:
                pass

        cmd = base_cmd + format_args + extra_args + [url]
        logger.info(f"[{job_id}] Trying yt-dlp: {' '.join(cmd)}")

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
                if files:
                    if format_choice == "audio":
                        target = [f for f in files if f.endswith(".mp3")]
                        chosen = target[0] if target else files[0]
                    else:
                        target = [f for f in files if f.endswith(".mp4")]
                        chosen = target[0] if target else files[0]

                    for f in files:
                        if f != chosen:
                            try:
                                os.remove(f)
                            except OSError:
                                pass

                    job["status"] = "done"
                    job["file"] = chosen
                    ext = os.path.splitext(chosen)[1]
                    title = job.get("title", "").strip()
                    if title:
                        safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:80].strip()
                        job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
                    else:
                        job["filename"] = os.path.basename(chosen)
                    logger.info(f"[{job_id}] yt-dlp download successful")
                    return
                else:
                    last_error = "Download completed but no file was found"
            else:
                stderr = (result.stderr or result.stdout or "").strip()
                last_error = normalize_ytdlp_error(stderr, youtube)
                logger.warning(f"[{job_id}] yt-dlp strategy failed: {last_error}")
        except subprocess.TimeoutExpired:
            last_error = "Download timed out (5 min limit)"
        except Exception as e:
            last_error = str(e)

    # All strategies failed
    job["status"] = "error"
    job["error"] = normalize_ytdlp_error(last_error, youtube)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/healthz")
@app.route("/api/health")
def health_check():
    """Lightweight health endpoint for uptime monitors."""
    return jsonify({
        "status": "ok",
        "service": "clipdown",
        "cookie_mode": "browser" if COOKIES_FROM_BROWSER else ("file" if os.path.isfile(COOKIES_FILE) else "none"),
    })


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = normalize_video_url(data.get("url", ""))
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    # ── Try pytubefix first for YouTube URLs ────────────────────────────────
    if is_youtube_url(url):
        try:
            logger.info(f"Fetching info via pytubefix for: {url}")
            info = pytube_get_info(url)
            return jsonify(info)
        except Exception as e:
            logger.warning(f"pytubefix info fetch failed: {e}, falling back to yt-dlp")

    # ── Fallback to yt-dlp (or primary for non-YouTube) ─────────────────────
    try:
        logger.info(f"Fetching info via yt-dlp for: {url}")
        info = ytdlp_get_info(url)
        return jsonify(info)
    except Exception as e:
        return jsonify({"error": normalize_ytdlp_error(str(e), is_youtube_url(url))}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    # Clean up old files before starting new download
    cleanup_old_files()

    data = request.json
    url = normalize_video_url(data.get("url", ""))
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"status": "downloading", "url": url, "title": title}

    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


@app.route("/api/debug", methods=["POST"])
def debug_info():
    """Debug endpoint to test pytubefix and yt-dlp on the server."""
    import traceback
    data = request.json or {}
    url = normalize_video_url(data.get("url", "https://www.youtube.com/watch?v=ZtmYzyY9hf0"))
    results = {"url": url, "pytubefix_clients": {}, "ytdlp": {}}
    results["cookie_mode"] = "browser" if COOKIES_FROM_BROWSER else ("file" if os.path.isfile(COOKIES_FILE) else "none")
    results["proxy_mode"] = "set" if YTDLP_PROXY else "none"
    results["user_agent_mode"] = "set" if YTDLP_USER_AGENT else "none"

    # Test each pytubefix client
    for client in PYTUBE_CLIENTS:
        try:
            yt = _create_youtube(url, client=client)
            title = yt.title
            streams_count = len(yt.streams)
            results["pytubefix_clients"][client] = {
                "status": "success",
                "title": title,
                "streams": streams_count,
            }
        except Exception as e:
            results["pytubefix_clients"][client] = {
                "status": "error",
                "error": str(e),
                "traceback": traceback.format_exc()[-500:],
            }

    # Test yt-dlp
    cmd = ["yt-dlp", "--no-playlist", "--no-warnings", "-j"] + get_ytdlp_network_args() + get_cookie_args() + [url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            info = json.loads(result.stdout)
            results["ytdlp"] = {"status": "success", "title": info.get("title")}
        else:
            results["ytdlp"] = {"status": "error", "stderr": result.stderr[-500:]}
    except Exception as e:
        results["ytdlp"] = {"status": "error", "error": str(e)}

    # Check node.js
    try:
        node_result = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=5)
        results["nodejs_version"] = node_result.stdout.strip()
    except Exception as e:
        results["nodejs_version"] = f"NOT FOUND: {e}"

    return jsonify(results)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "0.0.0.0")
    app.run(host=host, port=port)
