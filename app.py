import os
import re
import time
import uuid
import shutil
import zipfile
import threading
from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)

DOWNLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "downloads")
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

VIDEO_QUALITIES = [1080, 720, 480, 360, 240]
AUDIO_QUALITIES = [320, 256, 192, 128, 64]

# In-memory job tracking for progress polling: job_id -> dict
JOBS = {}

FILE_MAX_AGE_SECONDS = 3600       # auto-delete files older than 1 hour
CLEANUP_INTERVAL_SECONDS = 300    # check every 5 minutes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_base_ydl_opts():
    opts = {
        "quiet": True,
        "nocheckcertificate": True,
        "socket_timeout": 30,
        "retries": 10,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
        # Bypasses YouTube's bot wall by stripping out flagged android_sdkless endpoints
        "extractor_args": {
            "youtube": {
                "player_client": ["default", "-android_sdkless"]
            }
        },
    }

    # Optional Proxy support via environment variable on Render
    proxy_url = os.environ.get("PROXY_URL", "").strip()
    if proxy_url:
        opts["proxy"] = proxy_url

    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        opts["ffmpeg_location"] = os.path.dirname(ffmpeg_path)
    return opts


def is_playlist_url(url: str) -> bool:
    return "list=" in url


def clean_single_video_url(url: str) -> str:
    """Strip playlist params so a single video downloads, not the whole list."""
    url = url.strip()
    if "youtube.com/watch" in url:
        if "&list=" in url:
            url = url.split("&list=")[0]
        if "?list=" in url and "&v=" in url:
            video_id = url.split("v=")[1].split("&")[0]
            url = f"https://www.youtube.com/watch?v={video_id}"
    elif "youtu.be/" in url and "?list=" in url:
        url = url.split("?list=")[0]
    return url


def clean_youtube_url(url: str, allow_playlist: bool = False) -> str:
    url = url.strip()
    if not url:
        return ""
    if not url.startswith("http://") and not url.startswith("https://"):
        return f"ytsearch1:{url}"
    if allow_playlist and is_playlist_url(url):
        return url
    return clean_single_video_url(url)


FRIENDLY_ERROR_RULES = [
    (r"Private video", "This video is private and can't be downloaded."),
    (r"Video unavailable", "This video is unavailable — it may have been removed."),
    (r"Sign in to confirm your age|age.?restrict", "This video is age-restricted and can't be fetched without sign-in."),
    (r"Sign in to confirm you.?re not a bot|not a bot", "YouTube is blocking this server as a suspected bot. Try again shortly."),
    (r"This live event|is a live stream|live_status", "Live streams can't be downloaded until they've finished."),
    (r"members-only|join this channel", "This video is for channel members only."),
    (r"copyright", "This video is blocked due to a copyright claim."),
    (r"Unsupported URL", "That doesn't look like a valid YouTube link or search term."),
    (r"Unable to extract|Failed to extract", "YouTube's page format changed and the current tool version couldn't read it. Try again — this is often temporary."),
]


def friendly_error(raw_message: str) -> str:
    for pattern, friendly in FRIENDLY_ERROR_RULES:
        if re.search(pattern, raw_message, re.IGNORECASE):
            return friendly
    return "Something went wrong fetching that video. Double check the link and try again."


def cleanup_old_files():
    while True:
        try:
            now = time.time()
            for name in os.listdir(DOWNLOAD_FOLDER):
                path = os.path.join(DOWNLOAD_FOLDER, name)
                if os.path.isfile(path) and (now - os.path.getmtime(path)) > FILE_MAX_AGE_SECONDS:
                    os.remove(path)
        except Exception:
            pass
        time.sleep(CLEANUP_INTERVAL_SECONDS)


threading.Thread(target=cleanup_old_files, daemon=True).start()


def make_progress_hook(job_id):
    def hook(d):
        job = JOBS.get(job_id)
        if job is None:
            return
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            if total:
                job["percent"] = min(95, int(downloaded / total * 95))
            job["status"] = "downloading"
        elif d["status"] == "finished":
            job["percent"] = 97
            job["status"] = "merging"
    return hook


def download_single(url, mode, quality, job_id):
    ydl_opts = get_base_ydl_opts()
    ydl_opts["noplaylist"] = True
    ydl_opts["progress_hooks"] = [make_progress_hook(job_id)]

    if mode == "audio":
        ydl_opts.update({
            "format": "bestaudio/best",
            "outtmpl": os.path.join(DOWNLOAD_FOLDER, "%(title)s.%(ext)s"),
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": quality,
            }],
        })
    else:
        ydl_opts.update({
            "format": f"bestvideo[height<={quality}]+bestaudio/best",
            "format_sort": [f"res:{quality}", "codec:h264", "size"],
            "outtmpl": os.path.join(DOWNLOAD_FOLDER, "%(title)s.%(ext)s"),
            "merge_output_format": "mp4",
        })

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception:
        # Secondary fallback client options if default client fails
        fallback_opts = dict(ydl_opts)
        fallback_opts["format"] = "best" if mode == "video" else "bestaudio/best"
        fallback_opts.pop("format_sort", None)
        fallback_opts["extractor_args"] = {
            "youtube": {
                "player_client": ["tv_embedded", "mweb"]
            }
        }
        with yt_dlp.YoutubeDL(fallback_opts) as ydl:
            info = ydl.extract_info(url, download=True)

    if "entries" in info and len(info["entries"]) > 0:
        info = info["entries"][0]
    filename = ydl.prepare_filename(info)
    filename = os.path.splitext(filename)[0] + (".mp3" if mode == "audio" else ".mp4")
    return os.path.basename(filename)


def download_playlist(url, mode, quality, job_id):
    ydl_opts = get_base_ydl_opts()
    ydl_opts["noplaylist"] = False
    ydl_opts["progress_hooks"] = [make_progress_hook(job_id)]

    batch_id = uuid.uuid4().hex[:8]
    template = os.path.join(DOWNLOAD_FOLDER, f"pl_{batch_id}_%(playlist_index)s_%(title)s.%(ext)s")

    if mode == "audio":
        ydl_opts.update({
            "format": "bestaudio/best",
            "outtmpl": template,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": quality,
            }],
        })
    else:
        ydl_opts.update({
            "format": f"bestvideo[height<={quality}]+bestaudio/best",
            "format_sort": [f"res:{quality}", "codec:h264", "size"],
            "outtmpl": template,
            "merge_output_format": "mp4",
        })

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        playlist_title = info.get("title", "playlist")

    matched = [
        os.path.join(DOWNLOAD_FOLDER, f)
        for f in os.listdir(DOWNLOAD_FOLDER)
        if f.startswith(f"pl_{batch_id}_")
    ]

    zip_name = f"{playlist_title}.zip".replace("/", "-")
    zip_path = os.path.join(DOWNLOAD_FOLDER, zip_name)
    with zipfile.ZipFile(zip_path, "w") as zf:
        for filepath in matched:
            zf.write(filepath, arcname=os.path.basename(filepath))
            os.remove(filepath)

    return zip_name


def run_download_job(job_id, url, mode, quality, playlist):
    try:
        if playlist:
            filename = download_playlist(url, mode, quality, job_id)
        else:
            filename = download_single(url, mode, quality, job_id)
        JOBS[job_id] = {"status": "finished", "percent": 100, "filename": filename}
    except Exception as e:
        JOBS[job_id] = {"status": "error", "percent": 0, "error": friendly_error(str(e))}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/info", methods=["POST"])
def info_route():
    raw_url = request.form.get("url", "").strip()
    want_playlist = request.form.get("playlist", "false") == "true"

    if not raw_url:
        return jsonify({"error": "URL or search term is required"}), 400

    playlist_available = is_playlist_url(raw_url) and raw_url.startswith("http")
    target_url = clean_youtube_url(raw_url, allow_playlist=want_playlist and playlist_available)
    ydl_opts = get_base_ydl_opts()
    ydl_opts["noplaylist"] = not (want_playlist and playlist_available)
    ydl_opts["extract_flat"] = want_playlist and playlist_available

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(target_url, download=False)

            if want_playlist and playlist_available and "entries" in info:
                entries = [e.get("title", "Untitled") for e in info["entries"] if e]
                return jsonify({
                    "success": True,
                    "is_playlist": True,
                    "playlist_title": info.get("title", "Playlist"),
                    "entry_titles": entries[:50],
                    "entry_count": len(entries),
                    "url": raw_url,
                    "video_qualities": VIDEO_QUALITIES,
                    "audio_qualities": AUDIO_QUALITIES,
                })

            if "entries" in info and len(info["entries"]) > 0:
                info = info["entries"][0]

            return jsonify({
                "success": True,
                "is_playlist": False,
                "title": info.get("title", "Unknown Title"),
                "thumbnail": info.get("thumbnail", ""),
                "video_id": info.get("id", ""),
                "url": info.get("webpage_url", raw_url),
                "playlist_available": playlist_available,
                "video_qualities": VIDEO_QUALITIES,
                "audio_qualities": AUDIO_QUALITIES,
            })
    except Exception as e:
        return jsonify({"error": friendly_error(str(e))}), 500


@app.route("/start-download", methods=["POST"])
def start_download():
    raw_url = request.form.get("url", "").strip()
    mode = request.form.get("mode", "video")
    quality = request.form.get("quality", "").strip()
    playlist = request.form.get("playlist", "false") == "true"

    if not raw_url or not quality:
        return jsonify({"error": "Missing URL or quality"}), 400

    target_url = clean_youtube_url(raw_url, allow_playlist=playlist)
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"status": "starting", "percent": 0}

    thread = threading.Thread(
        target=run_download_job,
        args=(job_id, target_url, mode, quality, playlist),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/progress/<job_id>")
def progress(job_id):
    job = JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "Unknown job"}), 404
    return jsonify(job)


@app.route("/files/<path:filename>", methods=["GET"])
def serve_file(filename):
    return send_from_directory(DOWNLOAD_FOLDER, filename, as_attachment=True)


@app.route("/files/<path:filename>", methods=["DELETE"])
def delete_file(filename):
    path = os.path.join(DOWNLOAD_FOLDER, filename)
    try:
        if os.path.isfile(path):
            os.remove(path)
            return jsonify({"success": True})
        return jsonify({"error": "File not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
