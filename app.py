import os
import shutil
from flask import Flask, render_template, request, jsonify, send_from_directory
import yt_dlp

app = Flask(__name__)

DOWNLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "downloads")
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)


def get_base_ydl_opts():
    opts = {
        "quiet": True,
        "nocheckcertificate": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 10,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        },
    }

    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        opts["ffmpeg_location"] = os.path.dirname(ffmpeg_path)

    return opts


def clean_youtube_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""

    # Handle direct text searches (e.g. "mvp") by prepending ytsearch1:
    if not url.startswith("http://") and not url.startswith("https://"):
        return f"ytsearch1:{url}"

    # Clean playlist parameters from individual video links
    if "youtube.com/watch" in url:
        if "&list=" in url:
            url = url.split("&list=")[0]
        if "?list=" in url and "&v=" in url:
            video_id = url.split("v=")[1].split("&")[0]
            url = f"https://www.youtube.com/watch?v={video_id}"
    elif "youtu.be/" in url and "?list=" in url:
        url = url.split("?list=")[0]

    return url


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/process", methods=["POST"])
def process():
    """
    Single-shot endpoint: takes a URL (or search term) and a mode
    ('video' or 'audio'), downloads + merges server-side, and returns
    the filename ready to be pulled from /files/<filename>.
    """
    raw_url = request.form.get("url", "").strip()
    mode = request.form.get("mode", "video")  # 'video' or 'audio'

    if not raw_url:
        return jsonify({"error": "URL or search term is required"}), 400

    target_url = clean_youtube_url(raw_url)
    ydl_opts = get_base_ydl_opts()

    if mode == "audio":
        ydl_opts.update(
            {
                "format": "bestaudio/best",
                "outtmpl": os.path.join(DOWNLOAD_FOLDER, "%(title)s.%(ext)s"),
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
            }
        )
    else:
        ydl_opts.update(
            {
                "format": "bestvideo[height<=1080]+bestaudio/best",
                "format_sort": ["res:1080", "codec:h264", "size"],
                "outtmpl": os.path.join(DOWNLOAD_FOLDER, "%(title)s.%(ext)s"),
                "merge_output_format": "mp4",
            }
        )

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(target_url, download=True)

            if "entries" in info and len(info["entries"]) > 0:
                info = info["entries"][0]

            filename = ydl.prepare_filename(info)
            filename = os.path.splitext(filename)[0] + (
                ".mp3" if mode == "audio" else ".mp4"
            )
            base_name = os.path.basename(filename)

            return jsonify(
                {
                    "success": True,
                    "title": info.get("title", "Unknown Title"),
                    "thumbnail": info.get("thumbnail", ""),
                    "filename": base_name,
                }
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/files/<path:filename>")
def serve_file(filename):
    return send_from_directory(DOWNLOAD_FOLDER, filename, as_attachment=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
