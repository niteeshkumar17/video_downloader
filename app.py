import os
import re
import uuid
import glob
import json
import hashlib
import subprocess
import threading
import base64
import logging
import time
import random
from urllib.parse import parse_qs, urlparse, quote
import requests
from flask import Flask, request, jsonify, send_file, render_template, redirect
from flask_cors import CORS

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
PREVIEW_DIR = os.path.join(DOWNLOAD_DIR, "previews")
os.makedirs(PREVIEW_DIR, exist_ok=True)

COOKIES_FILE = os.path.join(os.path.dirname(__file__), "cookies.txt")
COOKIES_FROM_BROWSER = os.environ.get("YTDLP_COOKIES_FROM_BROWSER", "").strip()
YTDLP_PROXY = os.environ.get("YTDLP_PROXY", "").strip()
YTDLP_USER_AGENT = os.environ.get("YTDLP_USER_AGENT", "").strip()
TERABOX_COOKIE_FILE = os.path.join(os.path.dirname(__file__), "terabox_cookies.txt")
TERABOX_COOKIE = os.environ.get("TERABOX_COOKIE", "").strip()
TERABOX_USER_AGENT = (
    os.environ.get("TERABOX_USER_AGENT", "").strip()
    or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0"
)
TERABOX_HOST_HINTS = (
    "terabox.com",
    "terabox.app",
    "dm.terabox.app",
    "1024terabox.com",
    "1024tera.com",
    "nephobox.com",
    "freeterabox.com",
    "www.freeterabox.com",
    "4funbox.com",
    "mirrobox.com",
    "momerybox.com",
    "tibibox.com",
    "dm.1024terabox.com",
)


class TeraboxExternalDownloadRequired(Exception):
    """Raised when Terabox blocks server-side download but browser-session download may still work."""

    def __init__(self, dlink, filename):
        super().__init__("Terabox requires browser-session download")
        self.dlink = dlink
        self.filename = filename

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


def is_terabox_url(url):
    """Check if a URL belongs to Terabox-like share domains."""
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return False
    return any(domain in host for domain in TERABOX_HOST_HINTS)


def extract_terabox_surl(url):
    """Extract surl token from Terabox-style links."""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""

    qs = parse_qs(parsed.query or "")
    surl = (qs.get("surl") or qs.get("shorturl") or [""])[0].strip()
    if surl:
        return surl

    path = parsed.path or ""
    m = re.search(r"/s/([^/?#]+)", path)
    if m:
        return m.group(1).strip()
    return ""


def normalize_video_url(url):
    """Normalize known URL variants to improve extractor compatibility."""
    url = (url or "").strip()
    if not url:
        return url

    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        path = parsed.path or ""

        # Terabox variants → canonical share URL on www.terabox.app using /s/ format
        # IMPORTANT: Using /s/SURL causes the redirect to strip the regional '1' prefix,
        # giving surl=X6gwi_... instead of 1X6gwi_..., which is required for /share/list errno=0.
        if any(hint in host for hint in TERABOX_HOST_HINTS):
            surl = extract_terabox_surl(url)
            if surl:
                scheme = parsed.scheme if parsed.scheme in ("http", "https") else "https"
                return f"{scheme}://www.terabox.app/s/{quote(surl, safe='')}"

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


def parse_netscape_cookie_header(cookie_file, domain_hints):
    """Read Netscape cookie file and return Cookie header string for matching domains."""
    if not os.path.isfile(cookie_file):
        return ""

    try:
        with open(cookie_file, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except OSError:
        return ""

    return parse_netscape_cookie_text(content, domain_hints)


def parse_netscape_cookie_text(cookie_text, domain_hints):
    """Parse Netscape cookie content and return Cookie header string."""
    cookies = {}
    if not cookie_text:
        return ""

    for raw_line in cookie_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split("\t")
        if len(parts) < 7:
            continue

        domain = (parts[0] or "").lower()
        if not any(hint in domain for hint in domain_hints):
            continue

        name = parts[5].strip()
        value = parts[6].strip()
        if name:
            cookies[name] = value

    if not cookies:
        return ""
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def get_terabox_cookie_header():
    """Return Terabox cookie header from env/file/cookies.txt."""
    if TERABOX_COOKIE:
        # TERABOX_COOKIE can be either a header string or Netscape content.
        parsed = parse_netscape_cookie_text(TERABOX_COOKIE, TERABOX_HOST_HINTS)
        if parsed:
            return parsed
        return TERABOX_COOKIE

    if os.path.isfile(TERABOX_COOKIE_FILE):
        try:
            with open(TERABOX_COOKIE_FILE, "r", encoding="utf-8", errors="ignore") as f:
                value = f.read().strip()
            if value:
                parsed = parse_netscape_cookie_text(value, TERABOX_HOST_HINTS)
                if parsed:
                    return parsed
                return value
        except OSError:
            pass

    # Try the default cookies file first.
    parsed_default = parse_netscape_cookie_header(COOKIES_FILE, TERABOX_HOST_HINTS)
    if parsed_default:
        return parsed_default

    # Auto-discover commonly exported Terabox cookie files in project folder.
    project_dir = os.path.dirname(__file__)
    for pattern in ("*terabox*cookies*.txt", "*1024terabox*cookies*.txt"):
        for candidate in glob.glob(os.path.join(project_dir, pattern)):
            parsed = parse_netscape_cookie_header(candidate, TERABOX_HOST_HINTS)
            if parsed:
                return parsed

    return ""


def extract_terabox_dp_logid(html):
    """Extract the real dp-logid from Terabox share page HTML.

    Terabox validates this server-side, so generating a random one fails.
    Falls back to a generated value only when absent.
    """
    html = html or ""
    # Pattern: dp-logid=<value>& (encoded in URLs on page)
    m = re.search(r"dp-logid=([\w%+.\-]+)(?:&|\b)", html)
    if m:
        from urllib.parse import unquote
        return unquote(m.group(1)).strip()
    # Fallback: window.dplogid or similar JS assignments
    m = re.search(r'(?:dplogid|dp_logid|dp-logid)["\']?\s*[:=]\s*["\']([\w.\-]+)["\']', html, re.I)
    if m:
        return m.group(1).strip()
    # Last resort: generate a plausible-looking one
    session_id = random.randint(100000, 999999)
    user_id = f"00{random.randint(10000000, 99999999)}"
    return f"{session_id}{user_id}0001"


def extract_terabox_template_data(html):
    """Extract templateData JSON blob from share page."""
    m = re.search(r"var\s+templateData\s*=\s*(\{.*?\});\s*</script>", html or "", re.S)
    if not m:
        return {}

    try:
        return json.loads(m.group(1))
    except Exception:
        return {}


def extract_terabox_js_token(html):
    """Extract jsToken from share page using multiple fallback patterns."""
    html = html or ""

    # Pattern 1: URL-encoded fn call
    m = re.search(r"fn%28%22([A-F0-9a-f]+)%22%29", html)
    if m:
        return m.group(1).upper()

    # Pattern 2: window.jsToken assignment
    m = re.search(r"window\.jsToken\s*=\s*[\"']([A-F0-9a-f]+)[\"']", html)
    if m:
        return m.group(1).upper()

    # Pattern 3: jsToken inside JSON payload
    m = re.search(r'"jsToken"\s*:\s*"([A-F0-9a-f]{16,})"', html)
    if m:
        return m.group(1).upper()

    # Pattern 4: var jsToken = "..."
    m = re.search(r'var\s+jsToken\s*=\s*["\']([A-F0-9a-f]{16,})["\']', html, re.I)
    if m:
        return m.group(1).upper()

    # Pattern 5: fn("HEX") un-encoded
    m = re.search(r'fn\(["\']([A-F0-9a-f]{16,})["\']\)', html)
    if m:
        return m.group(1).upper()

    return ""


def normalize_terabox_error(error_data):
    """Map Terabox API errors into short actionable messages."""
    if not isinstance(error_data, dict):
        return "Unable to read Terabox response"

    code = error_data.get("errno")
    if code is None:
        code = error_data.get("code")
    try:
        code_int = int(code)
    except (TypeError, ValueError):
        code_int = code

    msg = (error_data.get("errmsg") or error_data.get("error_msg") or "").strip()

    if code_int == -3:
        return (
            "Terabox requires a logged-in session for this link. "
            "Set TERABOX_COOKIE with your browser session cookies and retry."
        )
    if code_int in (9019, 9013):
        return (
            "Terabox triggered a CAPTCHA/verification check. "
            "Open the link in browser, complete verification, then export fresh cookies and set TERABOX_COOKIE."
        )
    if code_int in (31045, 31001):
        return (
            "Terabox blocked the server-side download path for this request. "
            "The direct link may still work in a browser session."
        )

    if code_int in (460020, 400210):
        return (
            "Terabox requires verification for this link right now. "
            "Add a logged-in Terabox cookie via TERABOX_COOKIE or terabox_cookies.txt and retry."
        )
    if code_int == 400141:
        return (
            "This Terabox/1024tera share currently requires verification or an extraction code. "
            "Open the link in browser, complete verification (and code if prompted), then export fresh cookies and retry."
        )
    if code_int in (105, 2):
        return "This Terabox link looks invalid, expired, or protected."
    if msg:
        return f"Terabox error (errno={code_int}): {msg}"
    return f"Failed to fetch Terabox file info (errno={code_int})"


def terabox_is_dir(value):
    """Normalize various isdir value encodings used by Terabox APIs."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


def is_terabox_family_host(host):
    """Return True when host belongs to Terabox/1024tera family."""
    host = (host or "").lower()
    return any(hint in host for hint in TERABOX_HOST_HINTS)


def is_terabox_api_host(host):
    """Return True for Terabox-like hosts that serve API endpoints."""
    h = (host or "").strip().lower()
    if not h:
        return False
    # d./dm. hosts are CDN/download hosts and should be skipped for API calls.
    if h.startswith("d.") or h.startswith("dm."):
        return False
    return is_terabox_family_host(h)


def pick_terabox_file(items):
    """Pick a downloadable file from Terabox list response."""
    if not isinstance(items, list) or not items:
        return None

    for item in items:
        if not terabox_is_dir(item.get("isdir")):
            return item

    for item in items:
        children = item.get("children")
        if isinstance(children, list):
            for child in children:
                if not terabox_is_dir(child.get("isdir")):
                    return child

    return None


def resolve_terabox_dlink(session, hosts_to_try, base_api_params, api_headers, surl, fs_id):
    """Resolve direct download link via shorturlinfo + sharedownload."""
    if not fs_id:
        raise Exception("Terabox file id is missing")

    fs_id_str = str(fs_id)
    shorturl_candidates = [surl]
    if surl and not surl.startswith("1"):
        shorturl_candidates.append(f"1{surl}")

    short_data = None
    last_error_data = None
    last_network_error = None

    # Step 1: get sign/timestamp/shareid from shorturlinfo.
    for host in hosts_to_try:
        for shorturl_value in shorturl_candidates:
            params = dict(base_api_params)
            params.update({"shorturl": shorturl_value})
            req_headers = dict(api_headers)
            req_headers["Origin"] = f"https://{host}"

            try:
                rr = session.get(f"https://{host}/api/shorturlinfo", params=params, headers=req_headers, timeout=30)
                data = rr.json()
            except requests.RequestException as e:
                last_network_error = e
                continue
            except ValueError:
                continue

            last_error_data = data

            share_id = data.get("shareid") or data.get("share_id")
            uk = data.get("uk")
            sign = data.get("sign")
            timestamp = data.get("timestamp")

            # errno -3 also returns usable fields for share download flow.
            if share_id and uk and sign and timestamp:
                short_data = {
                    "share_id": str(share_id),
                    "uk": str(uk),
                    "sign": str(sign),
                    "timestamp": str(timestamp),
                }
                break

        if short_data:
            break

    if not short_data:
        if not last_error_data and last_network_error:
            raise Exception(f"Terabox network error: {last_network_error}")
        raise Exception(normalize_terabox_error(last_error_data))

    # Step 2: request sharedownload with discovered signature fields.
    download_params = dict(base_api_params)
    download_params.update({
        "fid_list": f"[{fs_id_str}]",
        "primaryid": short_data["share_id"],
        "uk": short_data["uk"],
        "sign": short_data["sign"],
        "timestamp": short_data["timestamp"],
        "product": "share",
    })

    for host in hosts_to_try:
        req_headers = dict(api_headers)
        req_headers["Origin"] = f"https://{host}"

        try:
            rr = session.get(
                f"https://{host}/api/sharedownload",
                params=download_params,
                headers=req_headers,
                timeout=30,
            )
            data = rr.json()
        except requests.RequestException as e:
            last_network_error = e
            continue
        except ValueError:
            continue

        last_error_data = data
        if data.get("errno") == 0 and isinstance(data.get("list"), list) and data.get("list"):
            for item in data.get("list") or []:
                dlink = item.get("dlink") or item.get("downloadlink") or item.get("download_link")
                if dlink:
                    return dlink

    if not last_error_data and last_network_error:
        raise Exception(f"Terabox network error: {last_network_error}")
    raise Exception(normalize_terabox_error(last_error_data))


def terabox_get_info(url):
    """Fetch Terabox info using its web APIs."""
    normalized_url = normalize_video_url(url)
    surl = extract_terabox_surl(normalized_url)
    if not surl:
        raise Exception("Invalid Terabox share URL")

    parsed_input = urlparse(normalized_url)
    input_scheme = parsed_input.scheme if parsed_input.scheme in ("http", "https") else "https"
    input_host = (parsed_input.netloc or "").lower()
    input_origin = f"{input_scheme}://{input_host}" if input_host else "https://www.terabox.com"

    hosts_to_try = []

    def add_host(host):
        h = (host or "").strip().lower()
        if h and is_terabox_api_host(h) and h not in hosts_to_try:
            hosts_to_try.append(h)

    add_host(input_host)
    # dm.terabox.app is the actual serving domain Terabox redirects to — try it first
    for host in (
        "dm.terabox.app",
        "www.terabox.app",
        "www.terabox.com",
        "www.1024terabox.com",
        "1024terabox.com",
        "www.1024tera.com",
        "1024tera.com",
    ):
        add_host(host)

    cookie_header = get_terabox_cookie_header()

    session = requests.Session()
    # Seed the session cookie jar with the user-provided Terabox cookies.
    # This lets the session send them automatically (like a real browser) on ALL
    # subsequent API requests to Terabox domains, including share/list.
    if cookie_header:
        for kv in cookie_header.split(";"):
            kv = kv.strip()
            if "=" in kv:
                k, _, v = kv.partition("=")
                for terabox_domain in (".terabox.com", ".terabox.app", ".1024terabox.com", ".1024tera.com"):
                    session.cookies.set(k.strip(), v.strip(), domain=terabox_domain)

    share_headers = {
        "User-Agent": TERABOX_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }
    if cookie_header:
        share_headers["Cookie"] = cookie_header

    share_resp = session.get(normalized_url, headers=share_headers, timeout=30, allow_redirects=True)
    share_resp.raise_for_status()

    parsed_share = urlparse(share_resp.url)
    share_scheme = parsed_share.scheme if parsed_share.scheme in ("http", "https") else "https"
    share_host = (parsed_share.netloc or "").lower()
    share_origin = f"{share_scheme}://{share_host}" if share_host else input_origin
    # Always add the redirect host first — it's the authoritative serving domain for this share
    if share_host and share_host not in hosts_to_try:
        hosts_to_try.insert(0, share_host)
    elif share_host and share_host in hosts_to_try:
        hosts_to_try.remove(share_host)
        hosts_to_try.insert(0, share_host)

    # Re-extract surl from the REDIRECTED URL (e.g. dm.terabox.app/sharing/link?surl=X6gwi_...)
    # The original short URL has a regional prefix (e.g. '1X6gwi_...') that must be stripped.
    redirected_surl = extract_terabox_surl(share_resp.url)
    if redirected_surl and redirected_surl != surl:
        logger.info(f"Terabox surl updated after redirect: {surl!r} → {redirected_surl!r}")
        surl = redirected_surl

    html = share_resp.text or ""
    lower_html = html.lower()
    if (
        "needverify" in lower_html
        or "need_verify" in lower_html
        or "verify-code" in lower_html
        or "accessdenied" in lower_html
        or ("captcha" in lower_html and "jstoken" not in lower_html)
    ):
        raise Exception(
            "Terabox is showing a verification/CAPTCHA page for this link. "
            "Open the URL in browser, complete verification, export your cookies, "
            "and set TERABOX_COOKIE."
        )

    js_token = extract_terabox_js_token(html)
    if not js_token:
        raise Exception("Could not read Terabox access token from share page")

    # Extract the real dp-logid that Terabox set in the page (server validates it)
    dp_logid = extract_terabox_dp_logid(html)

    template_data = extract_terabox_template_data(html)
    uk = template_data.get("uk")
    bdstoken = template_data.get("bdstoken")

    # Try extracting direct file/dlink info from the embedded page data
    # Terabox embeds file info (fs_id, filename, dlink, sign) directly in the HTML
    page_file_list = template_data.get("list") or template_data.get("fileList") or []
    if not page_file_list and template_data.get("shareid"):
        # Try extracting file info from the file info structures in templateData
        for key in ("file", "fileInfo", "video", "videoInfo"):
            if template_data.get(key):
                item = template_data[key]
                if isinstance(item, list):
                    page_file_list = item
                elif isinstance(item, dict):
                    page_file_list = [item]
                break

    # Also search for dlink/play_url/vlist directly in the raw HTML
    raw_dlink = None
    for pattern in (
        r'"dlink"\s*:\s*"([^"]{40,})"',
        r'"play_url"\s*:\s*"([^"]{40,})"',
        r'"download_addr"\s*:\s*"([^"]{40,})"',
    ):
        m = re.search(pattern, html)
        if m:
            raw_dlink = m.group(1).replace("\\u0026", "&").replace("\\/", "/")
            break

    if raw_dlink and not page_file_list:
        # We found a direct link in the page — use it immediately
        fname_m = re.search(r'"server_filename"\s*:\s*"([^"]+)"', html) or re.search(r'"filename"\s*:\s*"([^"]+)"', html)
        fname = fname_m.group(1) if fname_m else "terabox_file"
        thumb_m = re.search(r'"url3"\s*:\s*"([^"]+)"', html)
        thumb = thumb_m.group(1).replace("\\/", "/") if thumb_m else ""
        logger.info(f"Terabox: found dlink directly in page HTML for {fname!r}")
        return {
            "title": fname, "thumbnail": thumb,
            "duration": None, "uploader": "Terabox",
            "formats": [{"id": "terabox_direct", "label": "Original", "height": 1}],
            "terabox_dlink": raw_dlink, "terabox_filename": fname,
            "terabox_referer": share_resp.url,
        }

    # Also try to extract bdstoken directly from HTML if templateData missed it
    if not bdstoken:
        m = re.search(r'bdstoken["\']?\s*[:=,{]\s*["\']([A-Za-z0-9_\-]{8,})["\']', html)
        if m:
            bdstoken = m.group(1)

    logger.info(f"Terabox tokens — jsToken={js_token[:12]}… dp_logid={dp_logid} bdstoken={bdstoken}")

    list_params = {
        "app_id": "250528",
        "web": "1",
        "channel": "dubox",
        "clienttype": "0",
        "jsToken": js_token,
        "dp-logid": dp_logid,
        "page": "1",
        "num": "20",
        "by": "name",
        "order": "asc",
        "site_referer": share_resp.url,
        "shorturl": surl,
        "root": "1",
    }
    if bdstoken:
        list_params["bdstoken"] = str(bdstoken)

    api_headers = {
        "User-Agent": TERABOX_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "DNT": "1",
        "Referer": share_resp.url,
        "Origin": share_origin,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "X-Requested-With": "XMLHttpRequest",
    }
    # Do NOT set Cookie explicitly — the session cookie jar handles it automatically
    # (mimics a real browser session which is what Terabox checks for)

    list_response = None
    last_error_data = None
    last_network_error = None

    # --- Try 0: Bare call using just the surl (no auth, matches TeraDL mode 3 style) ---
    for host in ("dm.terabox.app", "www.terabox.app", "www.terabox.com", "1024terabox.com"):
        try:
            bare_r = session.get(
                f"https://{host}/share/list",
                params={"app_id": "250528", "shorturl": surl, "root": "1"},
                headers={
                    "user-agent": TERABOX_USER_AGENT,
                    "Referer": f"https://{host}/sharing/link?surl={surl}",
                },
                timeout=20,
            )
            bare_data = bare_r.json()
            logger.info(f"Bare /share/list [{host}] → errno={bare_data.get('errno')} len={len(bare_data.get('list') or [])}")
            if bare_data.get("errno") == 0 and bare_data.get("list"):
                list_response = bare_data
                break
        except Exception as e:
            logger.warning(f"Bare share/list [{host}]: {e}")

    for host in hosts_to_try:
        if list_response:
            break
        endpoint = f"https://{host}/share/list"
        try:
            r = session.get(endpoint, params=list_params, headers=api_headers, timeout=30)
        except requests.RequestException as e:
            last_network_error = e
            continue
        try:
            data = r.json()
        except ValueError:
            continue
        if data.get("errno") == 0 and isinstance(data.get("list"), list) and data.get("list"):
            list_response = data
            break
        logger.warning(f"Terabox /share/list [{host}] → errno={data.get('errno')} errmsg={data.get('errmsg') or data.get('error_msg')} raw_keys={list(data.keys())}")
        last_error_data = data

    # Fallback path: /share/list returned errno=105.
    # Strategy:
    #   1. Call /api/shorturlinfo — it often returns errno=0 with all the sign fields + fid
    #   2. Use those to call /api/sharedownload (POST with fid_list JSON) or Terabox's GET sharedownload
    #   3. If that fails, try HNN worker API as last resort
    if not list_response:
        si_shareid = si_uk = si_sign = si_ts = si_fid = si_fname = None

        # Step 1: call shorturlinfo (try all hosts)
        for host in hosts_to_try:
            try:
                si_r = session.get(
                    f"https://{host}/api/shorturlinfo",
                    params={"app_id": "250528", "web": "1", "channel": "dubox",
                            "clienttype": "0", "jsToken": js_token,
                            "dp-logid": dp_logid, "shorturl": surl, "root": "1"},
                    headers=api_headers, timeout=20
                )
                si_data = si_r.json()
                last_error_data = si_data
                logger.info(f"shorturlinfo [{host}] → errno={si_data.get('errno')} shareid={si_data.get('shareid')} fid={si_data.get('fid')}")
                if si_data.get("shareid") and si_data.get("uk"):
                    si_shareid = str(si_data["shareid"])
                    si_uk = str(si_data["uk"])
                    si_sign = str(si_data.get("sign") or "")
                    si_ts = str(si_data.get("timestamp") or "")
                    si_fid = si_data.get("fid")
                    si_fname = (si_data.get("dir") or "").rstrip("/").split("/")[-1] or None
                    break
            except Exception as e:
                last_network_error = e
                continue

        # Step 2: if we have shareid+uk, get file list; for folder shares (fid=0) list content
        if si_shareid and si_uk:
            # Attempt to get files: first try bare no-auth /share/list with 1+surl
            for host in ("dm.terabox.app", "www.terabox.app", "www.terabox.com", "1024terabox.com"):
                try:
                    bare2_r = session.get(
                        f"https://{host}/share/list",
                        params={"app_id": "250528", "shorturl": surl, "root": "1"},
                        headers={
                            "user-agent": TERABOX_USER_AGENT,
                            "Referer": share_resp.url,
                        },
                        timeout=20,
                    )
                    b2 = bare2_r.json()
                    logger.info(f"Fallback bare /share/list [{host}] → errno={b2.get('errno')} len={len(b2.get('list') or [])}")
                    if b2.get("errno") == 0 and b2.get("list"):
                        list_response = b2
                        break
                    for item in (b2.get("list") or []):
                        if item.get("fs_id") and not int(item.get("isdir", 0)):
                            si_fid = item["fs_id"]
                            si_fname = item.get("server_filename")
                            break
                except Exception:
                    pass
                if list_response or si_fid:
                    break
            fid_str = str(si_fid)

            # Build base params (no cookie needed for this path)
            sd_base = {
                "app_id": "250528", "web": "1", "channel": "dubox", "clienttype": "0",
                "jsToken": js_token, "dp-logid": dp_logid,
                "primaryid": si_shareid, "uk": si_uk,
                "sign": si_sign, "timestamp": si_ts,
                "product": "share", "nozip": "0",
            }

            for host in hosts_to_try:
                req_h = dict(api_headers)
                req_h["Host"] = host
                req_h["Origin"] = f"https://{host}"

                # Try GET with fid_list param
                try:
                    sd_params = dict(sd_base)
                    sd_params["fid_list"] = f"[{fid_str}]"
                    sd_r = session.get(f"https://{host}/api/sharedownload",
                                       params=sd_params, headers=req_h, timeout=30)
                    sd_data = sd_r.json()
                    logger.info(f"/api/sharedownload GET [{host}] → errno={sd_data.get('errno')} list={sd_data.get('list')}")
                    if sd_data.get("errno") == 0 and sd_data.get("list"):
                        for item in sd_data["list"]:
                            dlink = item.get("dlink") or item.get("downloadlink") or item.get("download_link")
                            if dlink:
                                fname = si_fname or item.get("server_filename") or "terabox_file"
                                return {
                                    "title": fname, "thumbnail": "",
                                    "duration": None, "uploader": "Terabox",
                                    "formats": [{"id": "terabox_direct", "label": "Original", "height": 1}],
                                    "terabox_dlink": dlink, "terabox_filename": fname,
                                    "terabox_referer": share_resp.url,
                                }
                    last_error_data = sd_data
                except Exception as e:
                    last_network_error = e

                # Try POST with JSON body
                try:
                    post_body = {
                        "app_id": "250528", "web": "1", "channel": "dubox",
                        "jsToken": js_token, "primaryid": si_shareid, "uk": si_uk,
                        "sign": si_sign, "timestamp": si_ts,
                        "product": "share", "fid_list": f"[{fid_str}]",
                    }
                    sd_r2 = session.post(f"https://{host}/api/sharedownload",
                                         json=post_body, headers=req_h, timeout=30)
                    sd_data2 = sd_r2.json()
                    logger.info(f"/api/sharedownload POST [{host}] → errno={sd_data2.get('errno')} list={sd_data2.get('list')}")
                    if sd_data2.get("errno") == 0 and sd_data2.get("list"):
                        for item in sd_data2["list"]:
                            dlink = item.get("dlink") or item.get("downloadlink") or item.get("download_link")
                            if dlink:
                                fname = si_fname or item.get("server_filename") or "terabox_file"
                                return {
                                    "title": fname, "thumbnail": "",
                                    "duration": None, "uploader": "Terabox",
                                    "formats": [{"id": "terabox_direct", "label": "Original", "height": 1}],
                                    "terabox_dlink": dlink, "terabox_filename": fname,
                                    "terabox_referer": share_resp.url,
                                }
                    last_error_data = sd_data2
                except Exception as e:
                    last_network_error = e

        # Step 3: HNN worker API fallback (may be Cloudflare-protected)
        if not list_response and si_shareid and si_uk and si_sign and si_fid:
            hnn_base = "https://terabox.hnn.workers.dev/api"
            hnn_h = {
                "accept-language": "en-US,en;q=0.9",
                "referer": "https://terabox.hnn.workers.dev/",
                "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin",
                "user-agent": TERABOX_USER_AGENT,
            }
            try:
                payload = {"shareid": si_shareid, "uk": si_uk, "sign": si_sign,
                           "timestamp": si_ts, "fs_id": str(si_fid)}
                dl_r = session.post(f"{hnn_base}/get-download", json=payload, headers=hnn_h, timeout=25)
                dl_data = dl_r.json()
                logger.info(f"HNN get-download → keys={list(dl_data.keys())}")
                dlink = dl_data.get("downloadLink") or dl_data.get("download_link") or dl_data.get("dlink")
                if dlink:
                    fname = si_fname or "terabox_file"
                    return {
                        "title": fname, "thumbnail": "", "duration": None, "uploader": "Terabox",
                        "formats": [{"id": "terabox_direct", "label": "Original", "height": 1}],
                        "terabox_dlink": dlink, "terabox_filename": fname,
                        "terabox_referer": share_resp.url,
                    }
            except Exception as e:
                logger.warning(f"HNN fallback failed: {e}")

        if not list_response:
            if last_error_data:
                raise Exception(normalize_terabox_error(last_error_data))
            if last_network_error:
                raise Exception(f"Terabox network error: {last_network_error}")
            raise Exception("Failed to fetch Terabox file info — please check the link or try again later")

    if not list_response:
        hnn_base = "https://terabox.hnn.workers.dev/api"

    if not list_response:
        raise Exception("Failed to fetch Terabox file info (no response from any API host)")

    file_item = pick_terabox_file(list_response.get("list") or [])
    if not file_item:
        raise Exception("No downloadable file found in this Terabox share")

    # Some Terabox/1024tera hosts omit dlink in /share/list and require /api/sharedownload.
    dlink = file_item.get("dlink") or ""
    if not dlink:
        base_api_params = {
            "app_id": "250528",
            "web": "1",
            "channel": "dubox",
            "clienttype": "0",
            "jsToken": js_token,
            "dp-logid": dp_logid,
        }
        dlink = resolve_terabox_dlink(
            session=session,
            hosts_to_try=hosts_to_try,
            base_api_params=base_api_params,
            api_headers=api_headers,
            surl=surl,
            fs_id=file_item.get("fs_id"),
        )

    file_name = file_item.get("server_filename") or "terabox_file"
    thumbs = file_item.get("thumbs") or {}
    thumbnail = thumbs.get("url3") or thumbs.get("url2") or thumbs.get("url1") or ""

    return {
        "title": file_name,
        "thumbnail": thumbnail,
        "duration": None,
        "uploader": "Terabox",
        "formats": [{"id": "terabox_direct", "label": "Original", "height": 1}],
        "terabox_dlink": dlink,
        "terabox_filename": file_name,
        "terabox_referer": share_resp.url,
    }


def terabox_download(job_id, url, format_choice):
    """Resolve Terabox dlink and store it for direct browser download."""
    info = terabox_get_info(url)
    dlink = info.get("terabox_dlink")
    if not dlink:
        raise Exception("Terabox direct link is missing")

    original_name = (info.get("terabox_filename") or info.get("title") or "terabox_file").strip()
    ext = os.path.splitext(original_name)[1].lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,6}", ext or ""):
        ext = ".mp4"

    headers = {
        "User-Agent": TERABOX_USER_AGENT,
        "Referer": info.get("terabox_referer") or normalize_video_url(url),
    }
    cookie_header = get_terabox_cookie_header()

    # Use a session with cookies for Terabox CDN domains
    dl_session = requests.Session()
    if cookie_header:
        headers["Cookie"] = cookie_header
        for kv in cookie_header.split(";"):
            kv = kv.strip()
            if "=" in kv:
                k, _, v = kv.partition("=")
                for d in (".terabox.com", ".terabox.app", ".1024terabox.com",
                          ".1024tera.com", "dm-d.terabox.app", ".dm-d.terabox.app"):
                    dl_session.cookies.set(k.strip(), v.strip(), domain=d)

    # Quick HEAD check to verify the dlink is accessible before handing to browser
    try:
        head_r = dl_session.head(dlink, headers=headers, timeout=15, allow_redirects=True)
        final_url = head_r.url  # follow any redirects to get final CDN URL
        if head_r.status_code == 200:
            logger.info(f"[{job_id}] Terabox dlink HEAD OK → {final_url[:80]}")
            # Store dlink for direct browser download — no server-side buffering
            return None, original_name, final_url
        elif head_r.status_code in (401, 403):
            raise TeraboxExternalDownloadRequired(dlink=dlink, filename=original_name)
        else:
            logger.warning(f"[{job_id}] Terabox HEAD {head_r.status_code}, falling back to proxy download")
            final_url = dlink
    except TeraboxExternalDownloadRequired:
        raise
    except Exception as e:
        logger.warning(f"[{job_id}] Terabox HEAD failed: {e}, trying proxy download")
        final_url = dlink

    # Fallback: server-side proxy download (for short/small files where HEAD failed)
    temp_path = os.path.join(DOWNLOAD_DIR, f"{job_id}{ext}")
    with dl_session.get(final_url, headers=headers, stream=True, timeout=90, allow_redirects=True) as r:
        response_host = (urlparse(r.url or final_url).netloc or "").lower()
        is_terabox_host = is_terabox_family_host(response_host)

        if r.status_code >= 400:
            error_text = ""
            try:
                error_text = (r.text or "")[:500]
            except Exception:
                error_text = ""
            is_user_not_exists = (
                ("error_code" in error_text and "31045" in error_text)
                or "user not exists" in error_text.lower()
            )
            if r.status_code in (401, 403) and (is_user_not_exists or is_terabox_host):
                raise TeraboxExternalDownloadRequired(dlink=dlink, filename=original_name)
            raise Exception(f"Terabox download failed (HTTP {r.status_code})")

        with open(temp_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)

    if format_choice == "audio":
        mp3_path = os.path.join(DOWNLOAD_DIR, f"{job_id}.mp3")
        cmd = ["ffmpeg", "-y", "-i", temp_path, "-vn", "-acodec", "libmp3lame", "-ab", "192k", mp3_path]
        convert = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if convert.returncode != 0:
            raise Exception("Failed to convert Terabox file to MP3")
        try:
            os.remove(temp_path)
        except OSError:
            pass
        temp_path = mp3_path
        original_name = f"{os.path.splitext(original_name)[0] or 'audio'}.mp3"

    return temp_path, original_name, None




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
preview_jobs = {}
preview_jobs_lock = threading.Lock()
PREVIEW_TTL_SECONDS = 1800
CLEANUP_INTERVAL_SECONDS = 120
last_cleanup_ts = 0


def cleanup_old_files(force=False):
    """Remove downloaded files older than TTL; throttled to reduce request latency."""
    global last_cleanup_ts
    now = time.time()
    if not force and (now - last_cleanup_ts) < CLEANUP_INTERVAL_SECONDS:
        return

    last_cleanup_ts = now
    threshold = now - PREVIEW_TTL_SECONDS
    for root, _, files in os.walk(DOWNLOAD_DIR):
        for name in files:
            file_path = os.path.join(root, name)
            try:
                if os.path.getmtime(file_path) < threshold:
                    os.remove(file_path)
            except OSError:
                pass


def get_preview_id(url):
    """Build a stable preview id for a URL to enable cache reuse."""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def get_preview_paths(preview_id):
    """Return expected preview asset paths for a preview id."""
    return (
        os.path.join(PREVIEW_DIR, f"{preview_id}.mp4"),
        os.path.join(PREVIEW_DIR, f"{preview_id}.jpg"),
    )


def preview_assets_exist(preview_id):
    """Check whether both preview video and thumbnail are already available."""
    preview_video, preview_thumb = get_preview_paths(preview_id)
    return os.path.isfile(preview_video) and os.path.isfile(preview_thumb)


def get_preview_payload(preview_id):
    """Build standard preview response payload."""
    return {
        "preview_id": preview_id,
        "thumbnail": f"/api/preview/thumb/{preview_id}",
        "preview_video": f"/api/preview/video/{preview_id}",
    }


def get_preview_job(preview_id):
    """Read preview job metadata in a thread-safe way."""
    with preview_jobs_lock:
        return preview_jobs.get(preview_id)


def set_preview_job(preview_id, status, error=None):
    """Update preview job metadata in a thread-safe way."""
    with preview_jobs_lock:
        preview_jobs[preview_id] = {
            "status": status,
            "error": error,
            "updated_at": time.time(),
        }


def generate_preview_assets(preview_id, url):
    """Generate a 5 second preview clip and thumbnail for a video URL."""
    preview_video, preview_thumb = get_preview_paths(preview_id)
    raw_template = os.path.join(PREVIEW_DIR, f"{preview_id}_raw.%(ext)s")
    raw_pattern = os.path.join(PREVIEW_DIR, f"{preview_id}_raw.*")

    if preview_assets_exist(preview_id):
        return preview_id

    youtube = is_youtube_url(url)
    last_error = "Unable to fetch preview media"

    for old_raw in glob.glob(raw_pattern):
        try:
            os.remove(old_raw)
        except OSError:
            pass

    try:
        strategies = build_ytdlp_strategies(url)
        for extra_args in strategies:
            cmd = [
                "yt-dlp",
                "--no-playlist",
                "--no-warnings",
                "--force-overwrites",
                "--download-sections", "*0-5",
                "-f", "best[ext=mp4][height<=360]/best[height<=360]/best[ext=mp4]/best",
                "-o", raw_template,
            ] + get_ytdlp_network_args() + extra_args + [url]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                break

            stderr = (result.stderr or result.stdout or "").strip()
            last_error = normalize_ytdlp_error(stderr, youtube)
        else:
            raise Exception(last_error)

        raw_files = glob.glob(raw_pattern)
        if not raw_files:
            raise Exception("Preview download did not produce a file")
        raw_file = raw_files[0]

        raw_ext = os.path.splitext(raw_file)[1].lower()
        if raw_ext == ".mp4":
            try:
                if os.path.isfile(preview_video):
                    os.remove(preview_video)
                os.replace(raw_file, preview_video)
            except OSError as e:
                raise Exception(f"Failed to store preview video: {e}")
        else:
            clip_cmd = [
                "ffmpeg", "-y",
                "-i", raw_file,
                "-t", "5",
                "-vf", "scale='min(640,iw)':-2",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-movflags", "+faststart",
                "-an",
                preview_video,
            ]
            clip_result = subprocess.run(clip_cmd, capture_output=True, text=True, timeout=90)
            if clip_result.returncode != 0:
                raise Exception("Failed to create preview video")

        thumb_cmd = [
            "ffmpeg", "-y",
            "-ss", "0.8",
            "-i", preview_video,
            "-frames:v", "1",
            "-q:v", "4",
            preview_thumb,
        ]
        thumb_result = subprocess.run(thumb_cmd, capture_output=True, text=True, timeout=45)
        if thumb_result.returncode != 0:
            raise Exception("Failed to create preview thumbnail")

        if not preview_assets_exist(preview_id):
            raise Exception("Preview assets are incomplete")
    except Exception:
        for path in [preview_video, preview_thumb]:
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
        raise
    finally:
        for raw_file in glob.glob(raw_pattern):
            try:
                os.remove(raw_file)
            except OSError:
                pass

    return preview_id


def generate_preview_assets_job(preview_id, url):
    """Background task that generates preview assets and updates job state."""
    try:
        generate_preview_assets(preview_id, url)
        set_preview_job(preview_id, "done")
    except Exception as e:
        normalized_error = normalize_ytdlp_error(str(e), is_youtube_url(url))
        logger.warning(f"Preview generation failed for {url}: {normalized_error}")
        set_preview_job(preview_id, "error", normalized_error)


def is_valid_preview_id(preview_id):
    """Validate preview IDs to prevent path traversal."""
    return re.fullmatch(r"[a-f0-9]{16}", preview_id or "") is not None


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

    # ── Terabox direct flow ──────────────────────────────────────────────────
    if is_terabox_url(url):
        try:
            logger.info(f"[{job_id}] Attempting Terabox direct download")
            filepath, original_name, direct_url = terabox_download(job_id, url, format_choice)
            job["status"] = "done"

            if direct_url:
                # Fast path: redirect browser directly to Terabox CDN — no server buffering
                job["external_url"] = direct_url
                ext = os.path.splitext(original_name)[1] or ".mp4"
                title = job.get("title", "").strip()
                if title:
                    safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:80].strip()
                    job["filename"] = f"{safe_title}{ext}" if safe_title else original_name
                else:
                    job["filename"] = "".join(c for c in (original_name or "") if c not in r'\/:*?"<>|').strip() or f"download{ext}"
                logger.info(f"[{job_id}] Terabox: direct browser download → {direct_url[:80]}")
                return

            # Slow path: file was downloaded to local disk
            job["file"] = filepath
            ext = os.path.splitext(filepath)[1]
            title = job.get("title", "").strip()
            if title:
                safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:80].strip()
                job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(filepath)
            else:
                cleaned = "".join(c for c in (original_name or "") if c not in r'\/:*?"<>|').strip()
                job["filename"] = cleaned or os.path.basename(filepath)

            logger.info(f"[{job_id}] Terabox direct download successful")
            return
        except TeraboxExternalDownloadRequired as e:
            logger.info(f"[{job_id}] Falling back to browser-session Terabox download")
            job["status"] = "done"
            job["external_url"] = e.dlink

            ext = os.path.splitext(e.filename or "")[1]
            if not ext:
                ext = ".mp4" if format_choice != "audio" else ".mp3"

            title = job.get("title", "").strip()
            if title:
                safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:80].strip()
                job["filename"] = f"{safe_title}{ext}" if safe_title else (e.filename or f"download{ext}")
            else:
                cleaned = "".join(c for c in (e.filename or "") if c not in r'\/:*?"<>|').strip()
                job["filename"] = cleaned or f"download{ext}"
            return
        except Exception as e:
            logger.warning(f"[{job_id}] Terabox direct download failed: {e}")
            job["status"] = "error"
            job["error"] = str(e)
            return

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


@app.route("/api/proxy-video")
def proxy_video():
    """Resolve Terabox dlink to its final signed CDN URL and redirect.

    Terabox dlinks redirect to a CDN URL with auth baked into query params
    (e.g. ?bkt=...). Once resolved, the CDN URL works directly in the browser
    without any Cookie/Referer headers — so we just 302-redirect there.
    """
    raw_url = request.args.get("u", "").strip()
    if not raw_url:
        return jsonify({"error": "Missing url param"}), 400

    from urllib.parse import unquote
    video_url = unquote(raw_url)

    # Security: only proxy Terabox-family URLs
    allowed_hosts = ("terabox.app", "terabox.com", "1024tera", "data.terabox",
                     "dm-d.terabox", "terabox.hnn", "d.pcs.baidu")
    parsed_host = urlparse(video_url).netloc.lower()
    if not any(h in parsed_host for h in allowed_hosts):
        return jsonify({"error": "URL not allowed"}), 403

    cookie_header = get_terabox_cookie_header()
    headers = {
        "User-Agent": TERABOX_USER_AGENT,
        "Referer": "https://www.terabox.app/",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header

    try:
        # Follow redirects to get the final signed CDN URL
        r = requests.head(video_url, headers=headers, timeout=15, allow_redirects=True)
        final_url = r.url  # The CDN URL with auth in query params
        if r.status_code in (200, 206):
            logger.info(f"proxy-video redirect → {final_url[:80]}")
            return redirect(final_url, code=302)
    except Exception as e:
        logger.warning(f"proxy-video HEAD failed: {e}")

    # Fallback: stream through server if redirect didn't work
    try:
        upstream = requests.get(video_url, headers=headers, stream=True,
                                timeout=30, allow_redirects=True)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    resp_headers = {
        "Content-Type": upstream.headers.get("Content-Type", "video/mp4"),
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
    }
    if "Content-Length" in upstream.headers:
        resp_headers["Content-Length"] = upstream.headers["Content-Length"]
    if "Content-Range" in upstream.headers:
        resp_headers["Content-Range"] = upstream.headers["Content-Range"]

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=256 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return app.response_class(generate(),
                              status=upstream.status_code if upstream.status_code in (200, 206) else 200,
                              headers=resp_headers)


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

    # ── Terabox dedicated extractor ───────────────────────────────────────────
    if is_terabox_url(url):
        try:
            logger.info(f"Fetching info via Terabox API for: {url}")
            info = terabox_get_info(url)
            return jsonify(info)
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    # ── Fallback to yt-dlp (or primary for non-YouTube) ─────────────────────
    try:
        logger.info(f"Fetching info via yt-dlp for: {url}")
        info = ytdlp_get_info(url)
        return jsonify(info)
    except Exception as e:
        return jsonify({"error": normalize_ytdlp_error(str(e), is_youtube_url(url))}), 400


@app.route("/api/preview", methods=["POST"])
def get_preview():
    """Kick off preview generation and return quickly for instant-feel UI."""
    cleanup_old_files()

    data = request.json or {}
    url = normalize_video_url(data.get("url", ""))
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    preview_id = get_preview_id(url)
    payload = get_preview_payload(preview_id)

    if preview_assets_exist(preview_id):
        return jsonify({"status": "ready", **payload})

    should_start_job = False
    with preview_jobs_lock:
        existing_job = preview_jobs.get(preview_id)
        if not existing_job or existing_job.get("status") != "processing":
            preview_jobs[preview_id] = {
                "status": "processing",
                "error": None,
                "updated_at": time.time(),
            }
            should_start_job = True

    if should_start_job:
        thread = threading.Thread(target=generate_preview_assets_job, args=(preview_id, url), daemon=True)
        thread.start()

    return jsonify({"status": "processing", **payload})


@app.route("/api/preview/status/<preview_id>")
def get_preview_status(preview_id):
    if not is_valid_preview_id(preview_id):
        return jsonify({"error": "Invalid preview id"}), 400

    payload = get_preview_payload(preview_id)

    if preview_assets_exist(preview_id):
        return jsonify({"status": "ready", **payload})

    job = get_preview_job(preview_id)
    if not job:
        return jsonify({"status": "not-found", **payload})

    if job.get("status") == "error":
        return jsonify({"status": "error", "error": job.get("error"), **payload})

    return jsonify({"status": "processing", **payload})


@app.route("/api/preview/video/<preview_id>")
def get_preview_video(preview_id):
    if not is_valid_preview_id(preview_id):
        return jsonify({"error": "Invalid preview id"}), 400

    preview_video, _ = get_preview_paths(preview_id)
    if not os.path.isfile(preview_video):
        return jsonify({"error": "Preview video not found"}), 404

    return send_file(preview_video, mimetype="video/mp4")


@app.route("/api/preview/thumb/<preview_id>")
def get_preview_thumb(preview_id):
    if not is_valid_preview_id(preview_id):
        return jsonify({"error": "Invalid preview id"}), 400

    _, preview_thumb = get_preview_paths(preview_id)
    if not os.path.isfile(preview_thumb):
        return jsonify({"error": "Preview image not found"}), 404

    return send_file(preview_thumb, mimetype="image/jpeg")


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
        "external_url": job.get("external_url"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    if job.get("external_url"):
        return redirect(job["external_url"], code=302)
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


@app.route("/api/debug/terabox", methods=["POST"])
def debug_terabox():
    """Debug endpoint to diagnose Terabox extraction step-by-step."""
    import traceback
    data = request.json or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    result = {
        "url": url,
        "is_terabox": is_terabox_url(url),
        "normalized_url": normalize_video_url(url),
        "surl": extract_terabox_surl(normalize_video_url(url)),
        "terabox_cookie_set": bool(get_terabox_cookie_header()),
        "terabox_cookie_header_preview": (get_terabox_cookie_header() or "")[:80] + "…" if get_terabox_cookie_header() else "",
    }

    if not result["is_terabox"]:
        result["note"] = "URL is not recognized as a Terabox URL."
        return jsonify(result)

    try:
        info = terabox_get_info(url)
        result["status"] = "success"
        result["title"] = info.get("title")
        result["terabox_dlink_preview"] = (info.get("terabox_dlink") or "")[:100] + "…"
        result["thumbnail"] = info.get("thumbnail")
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        result["traceback"] = traceback.format_exc()[-800:]

    return jsonify(result)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "0.0.0.0")
    app.run(host=host, port=port, threaded=True)
