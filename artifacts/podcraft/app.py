import os
import uuid
import threading
import json
import time
from pathlib import Path
from flask import Flask, request, jsonify, render_template, send_file, Response, stream_with_context
import requests as req_lib
import trafilatura

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "podcraft-secret-2025")

BASE_PATH = os.environ.get("BASE_PATH", "")


class PrefixMiddleware:
    def __init__(self, wsgi_app, prefix=""):
        self.app = wsgi_app
        self.prefix = prefix.rstrip("/")

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if self.prefix and path.startswith(self.prefix):
            environ["PATH_INFO"] = path[len(self.prefix):] or "/"
            environ["SCRIPT_NAME"] = self.prefix
        return self.app(environ, start_response)


if BASE_PATH:
    app.wsgi_app = PrefixMiddleware(app.wsgi_app, prefix=BASE_PATH)

OUTPUTS_DIR = os.path.join(os.path.dirname(__file__), "outputs")
os.makedirs(OUTPUTS_DIR, exist_ok=True)

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

VOICE_SAMPLES_DIR = os.path.join(os.path.dirname(__file__), "voice_samples")
os.makedirs(VOICE_SAMPLES_DIR, exist_ok=True)


ALLOWED_AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".wma", ".opus", ".webm", ".aiff"}


def get_job(job_id: str) -> dict | None:
    with JOBS_LOCK:
        return JOBS.get(job_id)


def set_job(job_id: str, data: dict):
    with JOBS_LOCK:
        JOBS[job_id] = data


def extract_text_from_url(url: str) -> str:
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        raise ValueError("Не удалось загрузить страницу по ссылке")
    text = trafilatura.extract(downloaded)
    if not text:
        raise ValueError("Не удалось извлечь текст со страницы")
    return text


def extract_text_from_pdf(file_bytes: bytes) -> str:
    from PyPDF2 import PdfReader
    from io import BytesIO
    reader = PdfReader(BytesIO(file_bytes))
    parts = []
    for page in reader.pages:
        extracted = page.extract_text()
        if extracted:
            parts.append(extracted)
    text = "\n".join(parts)
    if not text.strip():
        raise ValueError("Не удалось извлечь текст из PDF — возможно, это сканированный документ")
    return text


def extract_text_from_docx(file_bytes: bytes) -> str:
    from docx import Document
    from io import BytesIO
    doc = Document(BytesIO(file_bytes))
    text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    if not text.strip():
        raise ValueError("DOCX-файл не содержит текста")
    return text


def run_generation(job_id: str, text: str, style: str, host_voice: str,
                   guest_voice: str, duration: int, music_id: str,
                   language: str = "ru", podcast_mode: str = "video",
                   host_name: str = "Ведущий", guest_name: str = "Гость"):
    from generate_podcast import generate_podcast
    job_data = get_job(job_id)
    output_dir = os.path.join(OUTPUTS_DIR, job_id)
    try:
        generate_podcast(
            text=text,
            style=style,
            host_voice=host_voice,
            guest_voice=guest_voice,
            duration_minutes=duration,
            music_id=music_id,
            output_dir=output_dir,
            job_data=job_data,
            language=language,
            podcast_mode=podcast_mode,
            host_name=host_name,
            guest_name=guest_name,
            narrator_voice=job_data.get("narrator_voice", "sage"),
            narrator_name=job_data.get("narrator_name", "Диктор"),
            host_sample=job_data.get("host_sample"),
            guest_sample=job_data.get("guest_sample"),
            narrator_sample=job_data.get("narrator_sample"),
        )
    except Exception as e:
        job_data["status"] = "error"
        job_data["message"] = str(e)
        job_data["percent"] = 0


@app.route("/")
def index():
    from generate_podcast import ALL_VOICES, LANGUAGES, VOICE_PERSONALITIES, MUSIC_POOL, NARRATOR_PRESETS
    return render_template("index.html",
        base_path=BASE_PATH,
        all_voices=ALL_VOICES,
        languages=LANGUAGES,
        voice_personalities=VOICE_PERSONALITIES,
        music_pool=MUSIC_POOL,
        narrator_presets=NARRATOR_PRESETS,
    )


@app.route("/generate", methods=["POST"])
def generate():
    style = request.form.get("style", "cinematic")
    host_voice = request.form.get("host_voice", "alloy").strip()
    guest_voice = request.form.get("guest_voice", "nova").strip()
    duration = max(1, min(15, int(request.form.get("duration", 3))))
    music_id = request.form.get("music_id", "calm")
    language = request.form.get("language", "ru").strip()
    podcast_mode = request.form.get("podcast_mode", "video").strip()
    host_name = (request.form.get("host_name", "Ведущий") or "Ведущий").strip()[:40]
    guest_name = (request.form.get("guest_name", "Гость") or "Гость").strip()[:40]
    host_style = request.form.get("host_style", "").strip()
    guest_style = request.form.get("guest_style", "").strip()
    narrator_voice = (request.form.get("narrator_voice", "sage") or "sage").strip()
    narrator_name = (request.form.get("narrator_name", "Диктор") or "Диктор").strip()[:40]
    narrator_style = request.form.get("narrator_style", "").strip()
    host_description = request.form.get("host_description", "").strip()[:120]
    guest_description = request.form.get("guest_description", "").strip()[:120]
    text = ""

    url_input = request.form.get("url_input", "").strip()
    text_input = request.form.get("text_input", "").strip()
    uploaded_file = request.files.get("file_upload")

    try:
        if url_input:
            text = extract_text_from_url(url_input)
        elif uploaded_file and uploaded_file.filename:
            fname = uploaded_file.filename.lower()
            raw = uploaded_file.read()
            if fname.endswith(".pdf"):
                text = extract_text_from_pdf(raw)
            elif fname.endswith(".docx"):
                text = extract_text_from_docx(raw)
            elif fname.endswith(".txt"):
                text = raw.decode("utf-8", errors="replace")
            else:
                return jsonify({"error": "Поддерживаются только .txt, .pdf, .docx"}), 400
        elif text_input:
            text = text_input
        else:
            return jsonify({"error": "Нет текста для обработки"}), 400

        if len(text.strip()) < 50:
            return jsonify({"error": "Текст слишком короткий (минимум 50 символов)"}), 400

    except Exception as e:
        return jsonify({"error": str(e)}), 400

    job_id = str(uuid.uuid4())

    # Сохраняем загруженные образцы голоса
    sample_dir = os.path.join(VOICE_SAMPLES_DIR, job_id)
    host_sample_path: str | None = None
    guest_sample_path: str | None = None
    narrator_sample_path: str | None = None

    def _save_sample(field_name: str, role_name: str) -> str | None:
        f = request.files.get(field_name)
        if not f or not f.filename:
            return None
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_AUDIO_EXTS:
            return None
        os.makedirs(sample_dir, exist_ok=True)
        dest = os.path.join(sample_dir, f"{role_name}{ext}")
        f.save(dest)
        return dest

    host_sample_path = _save_sample("host_voice_sample", "host")
    guest_sample_path = _save_sample("guest_voice_sample", "guest")
    narrator_sample_path = _save_sample("narrator_voice_sample", "narrator")

    job_data: dict = {
        "status": "running",
        "step": "init",
        "percent": 0,
        "message": "Запуск генерации...",
        "text_preview": text[:200],
        "host_voice": host_voice,
        "guest_voice": guest_voice,
        "style": style,
        "podcast_mode": podcast_mode,
        "host_name": host_name,
        "guest_name": guest_name,
        "host_style": host_style,
        "guest_style": guest_style,
        "narrator_voice": narrator_voice,
        "narrator_name": narrator_name,
        "narrator_style": narrator_style,
        "host_description": host_description,
        "guest_description": guest_description,
        "host_sample": host_sample_path,
        "guest_sample": guest_sample_path,
        "narrator_sample": narrator_sample_path,
    }
    set_job(job_id, job_data)

    thread = threading.Thread(
        target=run_generation,
        args=(job_id, text, style, host_voice, guest_voice, duration, music_id,
              language, podcast_mode, host_name, guest_name),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status_stream(job_id: str):
    def generate_stream():
        while True:
            job = get_job(job_id)
            if not job:
                yield f"data: {json.dumps({'error': 'Job not found'})}\n\n"
                return

            payload = {
                "status": job.get("status"),
                "step": job.get("step"),
                "percent": job.get("percent", 0),
                "message": job.get("message", ""),
            }

            if job.get("status") == "done":
                payload["done"] = True
                payload["has_mp3"] = bool(job.get("mp3_path"))
                payload["has_mp4"] = bool(job.get("mp4_path"))
                payload["has_pptx"] = bool(job.get("pptx_path"))
                payload["music_tracks"] = job.get("music_tracks", [])
                payload["errors"] = job.get("errors", [])
                payload["tts_errors"] = job.get("tts_errors", [])
                payload["image_errors"] = job.get("image_errors", [])
                payload["speech_timeline"] = job.get("speech_timeline", [])
                payload["podcast_mode"] = job.get("podcast_mode", "video")
                payload["host_name"] = job.get("host_name", "Ведущий")
                payload["guest_name"] = job.get("guest_name", "Гость")
                yield f"data: {json.dumps(payload)}\n\n"
                return

            if job.get("status") == "error":
                payload["error_detail"] = job.get("message", "Неизвестная ошибка")
                yield f"data: {json.dumps(payload)}\n\n"
                return

            yield f"data: {json.dumps(payload)}\n\n"
            time.sleep(1.5)

    return Response(
        stream_with_context(generate_stream()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/download/<job_id>/<file_type>")
def download(job_id: str, file_type: str):
    job = get_job(job_id)
    if not job:
        return "Job not found", 404

    path_map = {
        "mp3": job.get("mp3_path"),
        "mp4": job.get("mp4_path"),
        "pptx": job.get("pptx_path"),
    }
    mime_map = {
        "mp3": "audio/mpeg",
        "mp4": "video/mp4",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
    name_map = {
        "mp3": "podcast.mp3",
        "mp4": "podcast.mp4",
        "pptx": "podcast.pptx",
    }

    path = path_map.get(file_type)
    if not path or not os.path.exists(path):
        return f"Файл {file_type} не готов или не существует", 404

    return send_file(
        path,
        mimetype=mime_map[file_type],
        as_attachment=True,
        download_name=name_map[file_type],
    )


@app.route("/rebuild_video/<job_id>", methods=["POST"])
def rebuild_video(job_id: str):
    """Пересобирает MP3 и MP4 с другой фоновой музыкой."""
    job = get_job(job_id)
    if not job or job.get("status") != "done":
        return jsonify({"error": "Job not ready"}), 400

    data = request.get_json(silent=True) or {}
    music_id = data.get("music_id", "calm")

    from generate_podcast import MUSIC_TRACKS, merge_audio_with_ducking, create_mp4

    output_dir = os.path.join(OUTPUTS_DIR, job_id)
    speech_dir = os.path.join(output_dir, "speech")
    mp3_path = os.path.join(output_dir, "podcast.mp3")
    mp4_path = os.path.join(output_dir, "podcast.mp4")

    music_track = next((t for t in MUSIC_TRACKS if t["id"] == music_id), MUSIC_TRACKS[0])
    speech_files = sorted(str(p) for p in Path(speech_dir).glob("line_*.mp3"))
    image_paths = job.get("image_paths", [])
    image_durations = job.get("image_durations", [])

    def rebuild():
        try:
            merge_audio_with_ducking(speech_files, music_track["url"], mp3_path)
            if image_paths and image_durations:
                create_mp4(image_paths, image_durations, mp3_path, mp4_path)
        except Exception as e:
            job["rebuild_error"] = str(e)

    t = threading.Thread(target=rebuild, daemon=True)
    t.start()
    t.join(timeout=150)

    if "rebuild_error" in job:
        return jsonify({"ok": False, "error": job["rebuild_error"]}), 500

    return jsonify({"ok": True})


@app.route("/voices")
def list_voices():
    from generate_podcast import ALL_VOICES
    return jsonify(ALL_VOICES)


@app.route("/languages")
def list_languages():
    from generate_podcast import LANGUAGES
    return jsonify(LANGUAGES)


VOICE_PREVIEW_TEXTS = {
    "alloy":   "Привет! Меня зовут Alloy. Я нейтральный и универсальный голос — подхожу для любой темы подкаста.",
    "ash":     "Привет! Я Ash. Мой голос мягкий и спокойный — идеален для вдумчивых разговоров и интервью.",
    "ballad":  "Привет! Я Ballad. Тёплый и певучий голос — создан для душевных историй и эмоциональных тем.",
    "coral":   "Привет! Я Coral. Дружелюбный женский голос — сделаю ваш подкаст живым и приятным для слуха.",
    "echo":    "Привет! Я Echo. Чёткий мужской голос — уверенный и выразительный, подходит для новостей и аналитики.",
    "fable":   "Привет! Я Fable. Выразительный театральный голос — оживлю любую историю с характером и эмоциями.",
    "nova":    "Привет! Я Nova. Живой женский голос — энергичный и современный, ваши слушатели меня полюбят!",
    "onyx":    "Привет! Я Onyx. Глубокий и басовитый голос — серьёзный, убедительный, запоминающийся.",
    "sage":    "Привет! Я Sage. Профессиональный и умеренный голос — подходит для деловых и образовательных подкастов.",
    "shimmer": "Привет! Я Shimmer. Тёплый и лёгкий голос — создам уютную атмосферу в вашем подкасте.",
}

VOICE_PREVIEWS_DIR = os.path.join(os.path.dirname(__file__), "voice_previews")
os.makedirs(VOICE_PREVIEWS_DIR, exist_ok=True)


@app.route("/voice_preview/<voice_id>")
def voice_preview(voice_id: str):
    from generate_podcast import VALID_VOICE_IDS, generate_tts

    if voice_id not in VALID_VOICE_IDS:
        return "Unknown voice", 404

    cache_path = os.path.join(VOICE_PREVIEWS_DIR, f"{voice_id}.mp3")

    if not os.path.exists(cache_path):
        text = VOICE_PREVIEW_TEXTS.get(voice_id, f"Привет! Я голос {voice_id}.")
        try:
            generate_tts(text, voice_id, cache_path, language="ru")
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return send_file(cache_path, mimetype="audio/mpeg")


@app.route("/github_push", methods=["POST"])
def github_push():
    import subprocess, tempfile, shutil
    data = request.get_json(force=True)
    token = (data.get("token") or "").strip()
    repo_url = (data.get("repo_url") or "").strip()
    commit_msg = (data.get("commit_msg") or "Update from PodCraft").strip()
    branch = (data.get("branch") or "main").strip()

    if not token:
        return jsonify({"ok": False, "error": "Токен не указан"}), 400
    if not repo_url:
        return jsonify({"ok": False, "error": "URL репозитория не указан"}), 400

    # Normalise URL: insert token for HTTPS auth
    if repo_url.startswith("https://github.com/"):
        path = repo_url[len("https://github.com/"):]
        auth_url = f"https://{token}@github.com/{path}"
    elif repo_url.startswith("git@github.com:"):
        return jsonify({"ok": False, "error": "Используйте HTTPS ссылку (https://github.com/...), а не SSH"}), 400
    else:
        auth_url = repo_url

    workspace_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    git_dir = os.path.join(workspace_root, ".git")

    env = {**os.environ, "GIT_AUTHOR_NAME": "PodCraft", "GIT_AUTHOR_EMAIL": "podcraft@replit.app",
           "GIT_COMMITTER_NAME": "PodCraft", "GIT_COMMITTER_EMAIL": "podcraft@replit.app"}

    def run(cmd, **kwargs):
        return subprocess.run(cmd, capture_output=True, text=True, env=env, **kwargs)

    try:
        # Init repo if needed
        if not os.path.exists(git_dir):
            r = run(["git", "init", "-b", branch], cwd=workspace_root)
            if r.returncode != 0:
                run(["git", "init"], cwd=workspace_root)
                run(["git", "checkout", "-b", branch], cwd=workspace_root)
        else:
            # Try to switch/create branch
            run(["git", "checkout", "-B", branch], cwd=workspace_root)

        # .gitignore — exclude big runtime dirs
        gi_path = os.path.join(workspace_root, ".gitignore")
        gi_entries = [
            "artifacts/podcraft/outputs/",
            "artifacts/podcraft/voice_samples/",
            ".pythonlibs/",
            "node_modules/",
            "__pycache__/",
            "*.pyc",
            ".env",
        ]
        existing = open(gi_path).read() if os.path.exists(gi_path) else ""
        with open(gi_path, "a") as f:
            for entry in gi_entries:
                if entry not in existing:
                    f.write(entry + "\n")

        run(["git", "add", "-A"], cwd=workspace_root)

        status = run(["git", "status", "--porcelain"], cwd=workspace_root)
        if not status.stdout.strip():
            return jsonify({"ok": True, "message": "Нет изменений для коммита — всё уже актуально на GitHub"})

        r = run(["git", "commit", "-m", commit_msg], cwd=workspace_root)
        if r.returncode != 0 and "nothing to commit" not in r.stdout:
            return jsonify({"ok": False, "error": f"Ошибка коммита: {r.stderr or r.stdout}"}), 500

        # Set remote
        run(["git", "remote", "remove", "origin"], cwd=workspace_root)
        run(["git", "remote", "add", "origin", auth_url], cwd=workspace_root)

        r = run(["git", "push", "-u", "origin", branch, "--force"], cwd=workspace_root)
        if r.returncode != 0:
            err = r.stderr or r.stdout
            if "Authentication failed" in err or "Invalid username" in err:
                return jsonify({"ok": False, "error": "Ошибка авторизации — проверьте токен"}), 401
            if "repository not found" in err.lower() or "does not exist" in err.lower():
                return jsonify({"ok": False, "error": "Репозиторий не найден — проверьте URL"}), 404
            return jsonify({"ok": False, "error": f"Ошибка пуша: {err[:400]}"}), 500

        return jsonify({"ok": True, "message": f"Успешно запушено в {repo_url} ({branch})"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
