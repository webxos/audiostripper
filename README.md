```
▄▖▖▖▄ ▄▖▄▖▄▖▄▖▄▖▄▖▄▖▄▖▄▖▄▖
▌▌▌▌▌▌▐ ▌▌▚ ▐ ▙▘▐ ▙▌▙▌▙▖▙▘
▛▌▙▌▙▘▟▖▙▌▄▌▐ ▌▌▟▖▌ ▌ ▙▖▌▌
```
# Audio Stripper

**Self-installing audio extractor with a Rust backend**

Extract audio from video or audio files and export it as high-quality **WAV** (lossless) or **MP3** (maximum quality VBR).  
One Python script handles everything: installs Rust if needed, builds a tiny Rust binary that drives FFmpeg, writes a clean local web dashboard, and serves it at `http://127.0.0.1:8765`.

No third-party Python packages. No global installation required.

---

## Features

- **WAV export** — Lossless. Preserves the source bit depth and sample rate when possible (16 / 24 / 32-bit PCM).
- **MP3 export** — Maximum quality VBR (`-q:a 0`, typically ~320 kbps).
- **Range selection** — Set start and end times with the video/audio preview player or type values manually.
- **Drag-and-drop or click** to choose a file.
- **Self-contained** — The script writes its own Rust backend + HTML dashboard on first run (and when the template version changes).
- **Concurrency limited** — At most 2 concurrent processing jobs to keep the machine responsive.
- **Safe defaults** — Host header checks, size limits, duration limits, and proper cleanup of temporary files.
- **Zero external Python dependencies** — Uses only the standard library.

---

## Requirements

| Component       | Notes                                      |
|-----------------|--------------------------------------------|
| **Python**      | 3.9 or newer                               |
| **FFmpeg**      | Must be on `PATH` (includes `ffprobe`)     |
| **Rust / Cargo**| Auto-installed on first run if missing     |
| **Browser**     | Any modern browser (Chrome, Firefox, Safari, Edge) |

### Installing FFmpeg

```bash
# Debian / Ubuntu
sudo apt install ffmpeg

# Fedora
sudo dnf install ffmpeg

# macOS (Homebrew)
brew install ffmpeg

# Windows
# Download from https://ffmpeg.org/download.html
# or use winget / chocolatey and ensure ffmpeg.exe is on PATH
```

The script will detect whether the `libmp3lame` encoder is available.  
If it is missing, MP3 mode returns HTTP 503 and only WAV remains usable.

---

## Quick Start

1. Place `audiostrip.py` in any directory.
2. Run it:

```bash
cd /the folder you install the file in/
python3 audiostrip.py
```

3. On first run the script will:
   - Check for / install Rust via the official rustup installer
   - Write `rust-backend/` (Cargo.toml + main.rs)
   - Write `dashboard/index.html`
   - Compile the release binary
   - Start a local HTTP server on port **8765**
   - Attempt to open your browser automatically

4. Use the dashboard at:

```
http://127.0.0.1:8765/
```

Press `Ctrl-C` in the terminal to stop the server.

---

## Usage

### 1. Load a file
- Click the drop zone **or** drag a video/audio file onto it.
- Supported containers / formats include:  
  `mp4`, `mov`, `m4a`, `webm`, `mkv`, `mp3`, `wav`, `aac`, `flac`, `ogg`, `opus`, and others that FFmpeg can read and that contain an audio stream.

### 2. Set the range
- Use the preview player to scrub.
- Click **Mark current** next to Start or End to capture the current playback position.
- Or type precise values (seconds, with decimals).
- **Reset to full length** restores the entire duration.

### 3. Choose output mode
- **WAV** (default) — Lossless, preserves source quality when possible.
- **MP3** — Highest-quality VBR (`libmp3lame -q:a 0`).

### 4. Process
- Click **Process with Rust**.
- The file is sent to the local server, validated, and handed to the Rust binary, which invokes FFmpeg with carefully constructed arguments.
- When finished you get an in-browser audio player and a download link.

### Limits (hard-coded, must match the UI)

| Limit              | Value          |
|--------------------|----------------|
| Maximum file size  | 500 MiB        |
| Maximum duration   | 60 minutes     |
| Concurrent jobs    | 2              |
| FFmpeg timeout     | 5 minutes      |

---

## How It Works

```
┌─────────────────┐
│  audiostrip.py  │  ← single entry point
└────────┬────────┘
         │
         ├─ 1. Ensure Rust is installed (rustup if needed)
         ├─ 2. Write / update rust-backend/ + dashboard/
         ├─ 3. cargo build --release  (if binary is stale)
         └─ 4. Start ThreadingHTTPServer on 127.0.0.1:8765
                    │
                    ▼
         ┌──────────────────────┐
         │  Dashboard (HTML/JS) │
         └──────────┬───────────┘
                    │  POST /process  (multipart)
                    ▼
         ┌──────────────────────┐
         │  Python Handler      │  ← validates, probes media,
         │  (stdlib only)       │    chooses WAV codec, acquires
         └──────────┬───────────┘    concurrency slot
                    │
                    ▼
         ┌──────────────────────┐
         │  audiostrip-backend  │  ← tiny Rust binary
         │  (calls ffmpeg)      │
         └──────────┬───────────┘
                    │
                    ▼
               FFmpeg  →  WAV or MP3
```

### WAV bit-depth selection
When exporting WAV the Python side runs `ffprobe` and picks the most appropriate PCM encoder:

| Source                | Encoder used   |
|-----------------------|----------------|
| Already PCM 16-bit    | `pcm_s16le`    |
| Already PCM 24-bit    | `pcm_s24le`    |
| Already PCM 32-bit    | `pcm_s32le`    |
| Float 32 / 64         | matching float |
| Unknown / compressed  | `pcm_s24le` (safe default) |

This keeps the export truly lossless whenever the source information is available.

### Security & robustness notes
- Only listens on `127.0.0.1` (localhost).
- Host header must match `localhost:8765` or `127.0.0.1:8765`.
- Body size and duration are enforced before any heavy work.
- Temporary files are cleaned up after every request (and a background sweep removes stale ones older than 24 h).
- The Rust binary re-validates numeric arguments so they can never be interpreted as FFmpeg options.

---

## Project Layout (created on first run)

```
.
├── audiostrip.py              ← the only file you need to keep
├── .template_version          ← tracks embedded template version
├── dashboard/
│   └── index.html             ← the web UI
└── rust-backend/
    ├── Cargo.toml
    ├── .main_rs.sha256        ← content hash for rebuild detection
    └── src/
        └── main.rs
    └── target/release/
        └── audiostrip-backend ← compiled binary
```

You can delete `dashboard/` and `rust-backend/` at any time; the next run of `audiostrip.py` will recreate them.

---

## Command-line / Advanced

The script has no CLI flags. Everything is driven through the web UI.

If you need to re-build the Rust backend after manually editing `main.rs`:

```bash
# just re-run the script — it detects a changed source hash
python3 audiostrip.py
```

Or build manually:

```bash
cd rust-backend
cargo build --release
```

---

## Troubleshooting

| Symptom                              | Likely cause / fix                                      |
|--------------------------------------|---------------------------------------------------------|
| `ffmpeg not found`                   | Install FFmpeg and ensure it is on `PATH`               |
| MP3 mode returns 503                 | Your FFmpeg was built without `libmp3lame`              |
| “Unreadable media file”              | File is corrupt or uses an exotic codec                 |
| “Unsupported media format”           | Container not in the allow-list                         |
| Build fails                          | Network issues during first Rust install, or missing system build tools (`build-essential` on Debian) |
| Browser does not open automatically  | Manually visit `http://127.0.0.1:8765/`                 |
| Port already in use                  | Another process is using 8765; stop it or change `PORT` in the script |

---

## License
Designed MIT and open sourced by github.com/webxos (webXOS 2026)
This project is provided as-is for personal and educational use.  
FFmpeg and Rust/Cargo are subject to their own licenses.
