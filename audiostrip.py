#!/usr/bin/env python3
"""
audiostrip.py — Self-installing Audio Stripper with Rust backend.

Run:
    python3 audiostrip.py

First run installs Rust (if needed), writes the Rust backend + dashboard,
compiles the binary, and serves the dashboard at http://127.0.0.1:8765.

Exports WAV (lossless, source bit depth / sample rate preserved) and
MP3 (max quality, VBR -q:a 0). Requires: Python 3.9+, ffmpeg on PATH.
No third-party Python packages.
"""

import hashlib
import http.server
import json
import os
import platform
import signal
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import webbrowser
from email.parser import BytesParser
from email.policy import default as email_policy_default
from pathlib import Path

# ============================================================
# CONFIGURATION
# ============================================================
PROJECT_DIR  = Path(__file__).parent.resolve()
DASHBOARD_DIR = PROJECT_DIR / "dashboard"
RUST_DIR     = PROJECT_DIR / "rust-backend"
RUST_BINARY  = RUST_DIR / "target" / "release" / "audiostrip-backend"
PORT         = 8765

MAX_BYTES       = 500 * 1024 * 1024          # 500 MiB — must match UI
MAX_SECONDS     = 3600.0                      # 60 min — must match UI
ALLOWED_MODES   = {"mp3", "wav"}
MODE_TO_EXT     = {"mp3": "mp3", "wav": "wav"}
ALLOWED_FORMATS = {
    "mp4", "mov", "m4a", "3gp", "3g2", "mj2",   # mov,mp4,m4a,... family
    "matroska", "webm", "mp3", "wav", "aac", "flac", "ogg", "opus", "mp2",
}
TEMP_PREFIX     = "audiostrip_"
TEMP_MAX_AGE_S  = 24 * 3600                   # sweep stale temp files
MAX_CONCURRENT_REQUESTS = 2
ACQUIRE_TIMEOUT_S = 60.0                      # max wait for an ffmpeg slot
FFMPEG_TIMEOUT_S = 300
FFPROBE_TIMEOUT_S = 15
ALLOWED_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}"}

# Populated at startup in main()
HAS_LIBMP3LAME = False

# ============================================================
# TEMPLATE VERSIONING
# ============================================================
TEMPLATE_VERSION = "4"
TEMPLATE_VERSION_FILE = PROJECT_DIR / ".template_version"
MAIN_RS_HASH_FILE = RUST_DIR / ".main_rs.sha256"


def template_version_matches() -> bool:
    try:
        return TEMPLATE_VERSION_FILE.read_text(encoding="utf-8").strip() == TEMPLATE_VERSION
    except OSError:
        return False


def write_template_version() -> None:
    try:
        TEMPLATE_VERSION_FILE.write_text(TEMPLATE_VERSION, encoding="utf-8")
    except OSError as exc:
        print(f"[!] Could not write {TEMPLATE_VERSION_FILE}: {exc}")


def _write_if_needed(path: Path, content: str, label: str) -> bool:
    """Write `content` to `path` if missing or template version changed.

    Returns True if the file was (re)written.
    """
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            existing = None
        if existing == content:
            print(f"[=] {label} up to date")
            return False
        if existing is not None and template_version_matches():
            print(f"[!] {label} differs from template but version {TEMPLATE_VERSION} is current; leaving as-is")
            return False
        if existing is not None:
            print(f"[!] Overwriting modified {label} due to template version change")
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        print(f"[!] Failed to write {label} to {path}: {exc}")
        sys.exit(1)
    print(f"[+] Wrote {label}")
    return True


# ============================================================
# RUST INSTALLATION
# ============================================================
def check_rust() -> bool:
    """Return True if `cargo` is runnable."""
    try:
        subprocess.run(["cargo", "--version"], capture_output=True, check=True, timeout=15)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Fall back to common install locations, and patch PATH in-process.
    candidates = [
        Path.home() / ".cargo" / "bin" / "cargo",
        Path("/usr/local/bin/cargo"),
        Path("/usr/bin/cargo"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            os.environ["PATH"] = str(candidate.parent) + os.pathsep + os.environ.get("PATH", "")
            try:
                subprocess.run([str(candidate), "--version"], capture_output=True, check=True, timeout=15)
                return True
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
                continue
    return False


def _rustup_target_triple():
    """Return the rustup dist target triple for this platform, or None."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux":
        if machine in ("x86_64", "amd64"):
            return "x86_64-unknown-linux-gnu"
        if machine in ("aarch64", "arm64"):
            return "aarch64-unknown-linux-gnu"
        if machine.startswith("arm"):
            return "armv7-unknown-linux-gnueabihf"
    elif system == "darwin":
        if machine in ("x86_64", "amd64"):
            return "x86_64-apple-darwin"
        if machine in ("aarch64", "arm64"):
            return "aarch64-apple-darwin"
    return None


def _install_rust_via_binary(triple: str) -> None:
    """Download rustup-init for `triple`, verify its SHA-256, and run it."""
    base = f"https://static.rust-lang.org/rustup/dist/{triple}"
    bin_url = f"{base}/rustup-init"
    sha_url = f"{base}/rustup-init.sha256"
    try:
        with urllib.request.urlopen(bin_url, timeout=60) as resp:
            data = resp.read(64 * 1024 * 1024)  # cap at 64 MiB
        with urllib.request.urlopen(sha_url, timeout=60) as resp:
            sha_text = resp.read(4096).decode("ascii", "replace").strip()
    except OSError as exc:
        print(f"[!] Failed to download rustup-init: {exc}")
        sys.exit(1)
    expected = sha_text.split()[0].lower() if sha_text else ""
    actual = hashlib.sha256(data).hexdigest()
    if not expected or actual != expected:
        print(f"[!] rustup-init checksum verification failed")
        print(f"    expected: {expected!r}")
        print(f"    actual:   {actual!r}")
        sys.exit(1)
    print(f"[+] rustup-init integrity check (same-origin) sha256={actual[:16]}…")
    with tempfile.TemporaryDirectory(prefix="rustup_") as td:
        bin_path = Path(td) / "rustup-init"
        try:
            bin_path.write_bytes(data)
            os.chmod(bin_path, 0o755)
            subprocess.run([str(bin_path), "-y"], check=True, timeout=600)
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"[!] rustup-init failed: {exc}")
            sys.exit(1)


def _install_rust_via_script() -> None:
    """Fallback: run the official shell installer (logs its SHA-256)."""
    try:
        with tempfile.TemporaryDirectory(prefix="rustup_") as td:
            script_path = Path(td) / "rustup-init.sh"
            try:
                with urllib.request.urlopen("https://sh.rustup.rs", timeout=60) as resp:
                    data = resp.read(4 * 1024 * 1024)  # cap at 4 MiB
            except OSError as exc:
                print(f"[!] Failed to download rustup installer: {exc}")
                sys.exit(1)
            print(f"[*] rustup-init.sh SHA-256: {hashlib.sha256(data).hexdigest()}")
            script_path.write_bytes(data)
            os.chmod(script_path, 0o755)
            try:
                subprocess.run(["sh", str(script_path), "-y"], check=True, timeout=600)
            except (OSError, subprocess.CalledProcessError) as exc:
                print(f"[!] rustup installer failed: {exc}")
                sys.exit(1)
    except OSError as exc:
        print(f"[!] Could not prepare temporary directory: {exc}")
        sys.exit(1)


def install_rust() -> None:
    """Install Rust via the official rustup (checksum-verified when possible)."""
    print("[*] Rust not found. Installing via rustup (this can take 2-5 minutes)...")
    triple = _rustup_target_triple()
    if triple is None:
        print("[!] Platform not recognised for checksum-verified rustup; using shell installer.")
        _install_rust_via_script()
    else:
        _install_rust_via_binary(triple)

    # Ensure cargo is on PATH for this process
    cargo_bin = Path.home() / ".cargo" / "bin"
    if cargo_bin.is_dir():
        os.environ["PATH"] = str(cargo_bin) + os.pathsep + os.environ.get("PATH", "")
    print("[+] Rust installed.")


# ============================================================
# RUST PROJECT
# ============================================================
CARGO_TOML = '''[package]
name = "audiostrip-backend"
version = "0.1.0"
edition = "2021"

[[bin]]
name = "audiostrip-backend"
path = "src/main.rs"

[profile.release]
opt-level = 2
'''

MAIN_RS = r'''use std::env;
use std::process::{exit, Command};

fn parse_non_negative(s: &str, label: &str) -> f64 {
    match s.parse::<f64>() {
        Ok(v) if v.is_finite() && v >= 0.0 => v,
        _ => {
            eprintln!(
                "Invalid {}: expected a non-negative finite number, got {:?}",
                label, s
            );
            exit(2);
        }
    }
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 6 {
        eprintln!("Usage: audiostrip-backend <input> <output> <mode> <start> <duration> [wav_codec]");
        exit(1);
    }

    let input = &args[1];
    let output = &args[2];
    let mode = &args[3];
    let start = parse_non_negative(&args[4], "start");
    let duration = parse_non_negative(&args[5], "duration");
    // wav_codec only applies to mode=wav; ignored otherwise. Safe default
    // (pcm_s24le) covers the case where the caller omitted it.
    let wav_codec = if args.len() >= 7 { args[6].as_str() } else { "pcm_s24le" };

    if duration <= 0.0 {
        eprintln!("Duration must be > 0, got {}", duration);
        exit(2);
    }

    // Re-format so the value passed to ffmpeg is a fixed, numeric string.
    // This is defence in depth: even if the caller bypasses HTTP validation,
    // the argument can never begin with '-' or contain option syntax.
    let start_s = format!("{:.3}", start);
    let duration_s = format!("{:.3}", duration);

    let mut cmd = Command::new("ffmpeg");
    cmd.args(["-hide_banner", "-nostdin", "-y", "-threads", "1"]);

    // Fast decode-seek for both mp3 and wav (both re-encode).
    cmd.args(["-ss", &start_s, "-i", input, "-t", &duration_s]);

    match mode.as_str() {
        "mp3" => {
            cmd.args([
                "-map", "0:a:0", "-vn",
                "-c:a", "libmp3lame", "-q:a", "0",
                "-map_metadata", "-1", "-map_chapters", "-1",
                output,
            ]);
        }
        "wav" => {
            cmd.args([
                "-map", "0:a:0", "-vn", "-c:a", wav_codec,
                "-map_metadata", "-1", "-map_chapters", "-1",
                output,
            ]);
        }
        _ => {
            eprintln!("Unknown mode: {:?}", mode);
            exit(2);
        }
    }

    match cmd.status() {
        Ok(s) if s.success() => {}
        Ok(s) => {
            eprintln!("FFmpeg exited with status {:?}", s);
            exit(1);
        }
        Err(e) => {
            eprintln!("Failed to execute ffmpeg: {}", e);
            exit(1);
        }
    }
}
'''


def create_rust_project() -> bool:
    """Write Cargo.toml and main.rs as needed. Returns True if main.rs was written."""
    try:
        (RUST_DIR / "src").mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"[!] Could not create {RUST_DIR / 'src'}: {exc}")
        sys.exit(1)
    _write_if_needed(RUST_DIR / "Cargo.toml", CARGO_TOML, "Cargo.toml")
    return _write_if_needed(RUST_DIR / "src" / "main.rs", MAIN_RS, "main.rs")


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _record_build_hash() -> None:
    try:
        MAIN_RS_HASH_FILE.write_text(
            _file_sha256(RUST_DIR / "src" / "main.rs"),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"[!] Warning: could not write build hash sidecar: {exc}")


def binary_is_stale() -> bool:
    if not RUST_BINARY.exists():
        return True
    main_rs = RUST_DIR / "src" / "main.rs"
    cargo_toml = RUST_DIR / "Cargo.toml"
    if not main_rs.exists() or not cargo_toml.exists():
        return True
    try:
        bin_mtime = RUST_BINARY.stat().st_mtime
        if (main_rs.stat().st_mtime > bin_mtime or
                cargo_toml.stat().st_mtime > bin_mtime):
            return True
    except OSError:
        return True
    # Content-hash check catches same-second rewrites and manual edits.
    current = _file_sha256(main_rs)
    try:
        recorded = MAIN_RS_HASH_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        recorded = ""
    if current != recorded:
        return True
    return False


def build_rust() -> None:
    """Compile the Rust binary in release mode."""
    print("[*] Building Rust backend...")
    try:
        result = subprocess.run(
            ["cargo", "build", "--release"],
            cwd=RUST_DIR,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except FileNotFoundError:
        print("[!] cargo not found — re-run this script to reinstall Rust.")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("[!] cargo build timed out after 10 minutes.")
        sys.exit(1)
    if result.returncode != 0:
        print("[!] Build failed:")
        print(result.stderr)
        sys.exit(1)
    if not RUST_BINARY.exists():
        print(f"[!] Build reported success but binary is missing at {RUST_BINARY}")
        sys.exit(1)
    _record_build_hash()
    print(f"[+] Built: {RUST_BINARY}")


# ============================================================
# DASHBOARD (HTML)
# ============================================================
HTML_TEMPLATE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Audio Stripper — Rust Backend</title>
<style>
  :root {
    --bg: #000; --panel: #0a0a0a; --panel-2: #161616;
    --border: #262626; --border-strong: #4a4a4a;
    --text: #fff; --muted: #9a9a9a; --dim: #666; --radius: 6px;
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg); color: var(--text); min-height: 100vh; line-height: 1.5;
    margin: 0; padding: 24px;
  }
  .app { max-width: 720px; margin: 0 auto; }
  h1 { font-size: 1.5rem; margin: 0 0 4px; }
  .sub { color: var(--muted); font-size: 0.85rem; margin-bottom: 24px; }
  #dropZone {
    border: 2px dashed var(--border-strong); border-radius: var(--radius);
    padding: 40px 20px; text-align: center; cursor: pointer;
    background: var(--panel); margin-bottom: 16px;
  }
  #dropZone:hover, #dropZone.drag { border-color: var(--text); }
  #dropZone.disabled { opacity: 0.4; pointer-events: none; }
  .icon { font-size: 2rem; margin-bottom: 8px; }
  .hint { color: var(--muted); font-size: 0.82rem; margin-top: 6px; }
  #status {
    padding: 12px 16px; border-radius: var(--radius); background: var(--panel);
    border-left: 3px solid var(--border-strong); font-size: 0.9rem; margin-bottom: 16px;
  }
  #status.busy { border-left-color: var(--dim); color: var(--muted); }
  #status.ok { border-left-color: var(--text); }
  #status.error { border-left: 6px solid #ff4444; background: #2a0a0a; color: #ff8888; }
  .panel {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 16px; margin-bottom: 16px;
  }
  .panel h2 { font-size: 0.78rem; margin: 0 0 12px; color: var(--muted); text-transform: uppercase; }
  .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 8px; }
  .row label { font-size: 0.82rem; color: var(--muted); min-width: 44px; font-weight: 600; }
  input[type="number"] {
    flex: 1; min-width: 100px; padding: 8px 10px; border-radius: var(--radius);
    border: 1px solid var(--border); background: var(--panel-2); color: var(--text);
    font-family: ui-monospace, monospace;
  }
  .modes { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 8px; margin-bottom: 12px; }
  .modes label {
    display: flex; align-items: center; gap: 8px; padding: 10px 12px;
    border-radius: var(--radius); border: 1px solid var(--border);
    background: var(--panel-2); cursor: pointer; font-size: 0.88rem;
  }
  .modes label:hover { border-color: var(--border-strong); }
  .modes input[type="radio"] { accent-color: var(--text); margin: 0; }
  .modes input[type="radio"]:checked + span { font-weight: 700; }
  .qualityNote { color: var(--muted); font-size: 0.78rem; line-height: 1.45; margin: 4px 0 12px; }
  .qualityNote code { color: var(--text); font-family: ui-monospace, monospace; font-size: 0.75rem; }
  button {
    font-family: inherit; font-size: 0.9rem; font-weight: 600;
    padding: 10px 18px; border-radius: var(--radius);
    border: 1px solid var(--border-strong); background: transparent; color: var(--text);
    cursor: pointer; transition: background 0.15s, border-color 0.15s;
  }
  button:hover:not(:disabled) { border-color: var(--text); }
  button.primary { background: var(--text); color: var(--bg); border-color: var(--text); }
  button.primary:hover:not(:disabled) { background: var(--muted); border-color: var(--muted); }
  button:disabled { opacity: 0.4; cursor: not-allowed; }
  #result { display: flex; flex-direction: column; gap: 12px; }
  #result[hidden] { display: none; }
  audio, video { width: 100%; border-radius: var(--radius); }
  a#downloadBtn {
    display: block; text-align: center; text-decoration: none;
    background: var(--text); color: var(--bg); padding: 12px 18px;
    border-radius: var(--radius); font-weight: 700;
  }
  a#downloadBtn:hover { background: var(--muted); }
  a#downloadBtn[hidden] { display: none; }
  details { background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); padding: 12px 14px; margin-top: 16px; font-size: 0.82rem; }
  details summary { cursor: pointer; color: var(--muted); font-weight: 700; user-select: none; text-transform: uppercase; letter-spacing: 0.06em; font-size: 0.75rem; }
  details pre {
    background: #000; border: 1px solid var(--border); border-radius: var(--radius);
    padding: 12px; max-height: 240px; overflow: auto; color: var(--muted);
    font-family: ui-monospace, monospace; font-size: 0.72rem; white-space: pre-wrap;
    margin: 12px 0 0; word-break: break-word;
  }
</style>
</head>
<body>
<div class="app">
  <h1>Audio Stripper</h1>
  <div class="sub">Rust backend · FFmpeg processing · WAV / MP3 export</div>

  <div id="dropZone" role="button" tabindex="0" aria-label="Choose a video or audio file">
    <div class="icon">🎬</div>
    <div><strong>Click or drag a video/audio file</strong></div>
    <div class="hint">Max 500 MiB · up to 60 minutes · MP4, WebM, MOV, MKV, MP3, WAV, M4A — exports WAV or MP3</div>
  </div>

  <div id="status" role="status" aria-live="polite">Waiting for file…</div>

  <div id="workspace" hidden>
    <div class="panel">
      <video id="previewPlayer" controls playsinline hidden></video>
    </div>

    <div class="panel">
      <h2>Range</h2>
      <div class="row">
        <label for="startInput">Start</label>
        <input id="startInput" type="number" step="0.1" min="0" value="0" inputmode="decimal">
        <button id="markStart" type="button">Mark current</button>
      </div>
      <div class="row">
        <label for="endInput">End</label>
        <input id="endInput" type="number" step="0.1" min="0" value="0" inputmode="decimal">
        <button id="markEnd" type="button">Mark current</button>
      </div>
      <div class="row"><button id="resetRange" type="button">Reset to full length</button></div>
    </div>

    <div class="panel">
      <h2>Output mode</h2>
      <div class="modes">
        <label><input type="radio" name="mode" value="wav" checked><span>WAV (lossless, full source quality)</span></label>
        <label><input type="radio" name="mode" value="mp3"><span>MP3 (max quality — VBR ~320 kbps)</span></label>
      </div>
      <div class="qualityNote">
        WAV preserves the source bit depth and sample rate. MP3 is capped at
        ~320 kbps by the format itself (VBR <code>-q:a 0</code>).
      </div>
      <button id="processBtn" class="primary" style="width:100%;">Process with Rust</button>
    </div>

    <div id="result" hidden>
      <audio id="audioPlayer" controls hidden></audio>
      <a id="downloadBtn" href="#" hidden>Download</a>
    </div>

    <details id="logBox" hidden>
      <summary>Backend log</summary>
      <pre id="logPre"></pre>
    </details>
  </div>
</div>

<script>
(function () {
  'use strict';
  var el = {
    dropZone: document.getElementById('dropZone'),
    status: document.getElementById('status'),
    workspace: document.getElementById('workspace'),
    startInput: document.getElementById('startInput'),
    endInput: document.getElementById('endInput'),
    markStart: document.getElementById('markStart'),
    markEnd: document.getElementById('markEnd'),
    resetRange: document.getElementById('resetRange'),
    processBtn: document.getElementById('processBtn'),
    result: document.getElementById('result'),
    audioPlayer: document.getElementById('audioPlayer'),
    previewPlayer: document.getElementById('previewPlayer'),
    downloadBtn: document.getElementById('downloadBtn'),
    logBox: document.getElementById('logBox'),
    logPre: document.getElementById('logPre')
  };

  var currentFile = null;
  var videoDuration = 0;
  var busy = false;
  var videoURL = null;
  var outputURL = null;

  function setStatus(t, kind) {
    el.status.textContent = t;
    el.status.className = kind ? kind : '';
  }
  function showLog(msg) {
    if (msg == null) return;
    el.logPre.textContent += String(msg) + '\n';
    el.logBox.hidden = false;
  }
  function resetLog() {
    el.logPre.textContent = '';
    el.logBox.hidden = true;
  }
  function revokeVideo()  { if (videoURL)  { URL.revokeObjectURL(videoURL);  videoURL  = null; } }
  function revokeOutput() { if (outputURL) { URL.revokeObjectURL(outputURL); outputURL = null; } }

  function resetResult() {
    revokeOutput();
    try { el.audioPlayer.pause(); } catch (_) {}
    el.audioPlayer.removeAttribute('src');
    el.audioPlayer.hidden = true;
    el.downloadBtn.hidden = true;
    el.downloadBtn.removeAttribute('href');
    el.result.hidden = true;
  }

  function resetFileState() {
    currentFile = null;
    videoDuration = 0;
    el.workspace.hidden = true;
    el.previewPlayer.onloadedmetadata = null;
    el.previewPlayer.onerror = null;
    try { el.previewPlayer.pause(); } catch (_) {}
    el.previewPlayer.removeAttribute('src');
    try { el.previewPlayer.load(); } catch (_) {}
    el.previewPlayer.hidden = true;
  }

  function handleFile(file) {
    if (!file || busy) return;
    var looksMedia = /^(video|audio)\//.test(file.type || '') ||
      /\.(mp4|m4v|mov|webm|mkv|avi|ogv|mp3|m4a|wav|ogg|aac|flac)$/i.test(file.name || '');
    if (!looksMedia) { alert('Please choose a video or audio file.'); return; }
    if (file.size > 500 * 1024 * 1024) {
      alert('File too large (max 500 MiB).');
      return;
    }

    revokeVideo();
    resetResult();
    resetLog();
    resetFileState();

    var token = (handleFile._token = (handleFile._token || 0) + 1);
    videoURL = URL.createObjectURL(file);

    el.previewPlayer.onloadedmetadata = function () {
      if (token !== handleFile._token) return;
      var dur = el.previewPlayer.duration;
      if (!isFinite(dur) || dur <= 0) {
        setStatus('Could not determine file duration.', 'error');
        resetFileState();
        return;
      }
      if (dur > 3600.5) {
        alert('File too long (max 60 minutes).');
        setStatus('File too long.', 'error');
        resetFileState();
        return;
      }
      currentFile = file;
      videoDuration = dur;
      el.startInput.value = '0';
      el.endInput.value = dur.toFixed(3);
      el.startInput.max = dur.toFixed(3);
      el.endInput.max = dur.toFixed(3);
      el.previewPlayer.hidden = false;
      el.workspace.hidden = false;
      setStatus('Ready — choose mode and press Process.', 'ok');
    };
    el.previewPlayer.onerror = function () {
      if (token !== handleFile._token) return;
      setStatus('Could not decode this file.', 'error');
      resetFileState();
    };

    el.previewPlayer.src = videoURL;
    el.previewPlayer.load();
  }

  function getMode() {
    var r = document.querySelector('input[name="mode"]:checked');
    return r ? r.value : 'wav';
  }

  function setBusy(v) {
    busy = v;
    el.processBtn.disabled = v;
    el.processBtn.textContent = v ? 'Processing…' : 'Process with Rust';
    el.dropZone.classList.toggle('disabled', v);
  }

  function process() {
    if (busy || !currentFile) return;
    setBusy(true);
    resetResult();
    resetLog();
    setStatus('Sending to Rust backend…', 'busy');

    var startRaw = parseFloat(el.startInput.value);
    var endRaw   = parseFloat(el.endInput.value);
    var start    = isFinite(startRaw) && startRaw >= 0 ? startRaw : 0;
    var end      = isFinite(endRaw)   && endRaw   > start ? endRaw : videoDuration;
    var duration = Math.max(0.1, end - start);
    var mode = getMode();

    var form = new FormData();
    form.append('file', currentFile);
    form.append('mode', mode);
    form.append('start', start.toFixed(3));
    form.append('duration', duration.toFixed(3));

    fetch('/process', { method: 'POST', body: form })
      .then(function (r) {
        if (!r.ok) return r.text().then(function (t) {
          throw new Error('HTTP ' + r.status + ': ' + (t || r.statusText));
        });
        var actualExt = r.headers.get('X-Output-Ext') || 'bin';
        var actualMime = r.headers.get('Content-Type') || 'application/octet-stream';
        return r.arrayBuffer().then(function (buf) {
          return { buf: buf, ext: actualExt, mime: actualMime };
        });
      })
      .then(function (res) {
        if (!res.buf || res.buf.byteLength === 0) throw new Error('Backend returned an empty file.');
        var blob = new Blob([res.buf], { type: res.mime });
        outputURL = URL.createObjectURL(blob);
        var base = (currentFile.name || 'output').replace(/\.[^.]+$/, '');
        var name = base + '-audio.' + res.ext;
        el.downloadBtn.href = outputURL;
        el.downloadBtn.download = name;
        el.downloadBtn.textContent = '⬇ Download ' + name;
        el.downloadBtn.hidden = false;
        el.audioPlayer.src = outputURL;
        el.audioPlayer.hidden = false;
        el.result.hidden = false;
        setStatus('Done!', 'ok');
        showLog('Rust backend returned ' + res.buf.byteLength + ' bytes (' + res.ext + ').');
      })
      .catch(function (err) {
        setStatus('Error: ' + (err && err.message ? err.message : String(err)), 'error');
        showLog(err && err.stack ? err.stack : String(err));
      })
      .finally(function () { setBusy(false); });
  }

  // File picker
  el.dropZone.addEventListener('click', function () {
    if (busy) return;
    var inp = document.createElement('input');
    inp.type = 'file';
    inp.accept = 'video/*,audio/*';
    inp.onchange = function () { if (inp.files && inp.files[0]) handleFile(inp.files[0]); };
    inp.click();
  });
  el.dropZone.addEventListener('keydown', function (e) {
    if ((e.key === 'Enter' || e.key === ' ') && !busy) {
      e.preventDefault();
      el.dropZone.click();
    }
  });
  ['dragenter', 'dragover'].forEach(function (evt) {
    el.dropZone.addEventListener(evt, function (e) {
      e.preventDefault(); e.stopPropagation();
      if (!busy) el.dropZone.classList.add('drag');
    });
  });
  ['dragleave', 'dragend', 'drop'].forEach(function (evt) {
    el.dropZone.addEventListener(evt, function (e) {
      e.preventDefault(); e.stopPropagation();
      el.dropZone.classList.remove('drag');
    });
  });
  el.dropZone.addEventListener('drop', function (e) {
    var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f && !busy) handleFile(f);
  });
  window.addEventListener('dragover', function (e) { e.preventDefault(); });
  window.addEventListener('drop',     function (e) { e.preventDefault(); });

  el.markStart.addEventListener('click', function () {
    if (!el.previewPlayer || busy) return;
    el.startInput.value = (el.previewPlayer.currentTime || 0).toFixed(3);
  });
  el.markEnd.addEventListener('click', function () {
    if (!el.previewPlayer || busy) return;
    el.endInput.value = (el.previewPlayer.currentTime || 0).toFixed(3);
  });
  el.resetRange.addEventListener('click', function () {
    if (!currentFile || busy) return;
    el.startInput.value = '0';
    el.endInput.value = videoDuration.toFixed(3);
  });
  el.processBtn.addEventListener('click', process);

  window.addEventListener('pagehide', function () {
    revokeVideo();
    revokeOutput();
  });
})();
</script>
</body>
</html>
'''


def create_dashboard() -> None:
    """Write the HTML dashboard as needed."""
    try:
        DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"[!] Could not create {DASHBOARD_DIR}: {exc}")
        sys.exit(1)
    _write_if_needed(DASHBOARD_DIR / "index.html", HTML_TEMPLATE, "dashboard/index.html")


# ============================================================
# MULTIPART PARSING (stdlib only — works on Python 3.9+)
# ============================================================
def parse_multipart(content_type: str, body: bytes) -> dict:
    """
    Parse a multipart/form-data body into a dict.
    Parts with a `filename` are returned as bytes; other parts as str.
    """
    full = (
        b"Content-Type: " + content_type.encode("utf-8") +
        b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    )
    msg = BytesParser(policy=email_policy_default).parsebytes(full)
    if not msg.is_multipart():
        raise ValueError("Body is not multipart/form-data")

    fields = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            raw = part.get_payload()
            payload = raw.encode("utf-8") if isinstance(raw, str) else b""
        if part.get_filename():
            fields[name] = payload
        else:
            try:
                fields[name] = payload.decode("utf-8")
            except UnicodeDecodeError:
                fields[name] = payload
    return fields


# ============================================================
# HELPERS
# ============================================================
def parse_non_negative(value, label: str) -> float:
    """Parse `value` as a non-negative finite float; raise ValueError otherwise."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} is not a number: {value!r}")
    if not (v == v) or v in (float("inf"), float("-inf")):
        raise ValueError(f"{label} is not finite: {value!r}")
    if v < 0:
        raise ValueError(f"{label} is negative: {v}")
    return v


def ffmpeg_has_encoder(name: str) -> bool:
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and name in (result.stdout or "")


def probe_media(path: Path):
    """Return (available: bool, info: dict | None).

    - available=False  → ffprobe missing/timeout: skip validation.
    - available=True, info=None → ffprobe ran but the file is unreadable.
    - available=True, info=dict → parsed stream/format information.
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=format_name",
             "-show_entries", "stream=codec_type,codec_name,bits_per_sample",
             "-of", "json",
             str(path)],
            capture_output=True, text=True, timeout=FFPROBE_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return (False, None)
    if result.returncode != 0:
        return (True, None)
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return (True, None)

    fmt = (data.get("format") or {}).get("format_name") or ""
    streams = data.get("streams") or []
    if not streams:
        return (True, None)

    has_video = any(s.get("codec_type") == "video" for s in streams)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    return (True, {
        "format_name": fmt.lower(),
        "has_video": has_video,
        "has_audio": has_audio,
        "audio_codec": audio_stream.get("codec_name") if audio_stream else None,
        "video_codec": video_stream.get("codec_name") if video_stream else None,
        "bits_per_sample": audio_stream.get("bits_per_sample") if audio_stream else None,
    })


def pick_wav_codec(audio_codec, bits_per_sample) -> str:
    """Return the ffmpeg PCM encoder that preserves the source's sample format.

    Priority order:
      1. If source is already PCM, keep its bit depth (lossless transcode).
      2. Otherwise use `bits_per_sample` from ffprobe as a hint.
      3. Fall back to 24-bit (safe — exceeds consumer-source dynamic range
         while preserving every bit of 16-bit content).
    """
    codec = (audio_codec or "").lower()
    try:
        bps = int(bits_per_sample or 0)
    except (TypeError, ValueError):
        bps = 0

    if codec in ("pcm_s16le", "pcm_s16be"): return "pcm_s16le"
    if codec in ("pcm_s24le", "pcm_s24be"): return "pcm_s24le"
    if codec in ("pcm_s32le", "pcm_s32be"): return "pcm_s32le"
    if codec in ("pcm_f32le", "pcm_f32be"): return "pcm_f32le"
    if codec in ("pcm_f64le", "pcm_f64be"): return "pcm_f64le"

    if bps >= 32: return "pcm_s32le"
    if bps >= 24: return "pcm_s24le"
    if bps >= 16: return "pcm_s16le"
    return "pcm_s24le"


def sweep_stale_temp_files() -> None:
    """Delete leftover audiostrip_* temp files older than TEMP_MAX_AGE_S."""
    temp_root = Path(tempfile.gettempdir())
    cutoff = time.time() - TEMP_MAX_AGE_S
    for path in temp_root.glob(TEMP_PREFIX + "*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def _mime_for(mode: str, ext: str) -> str:
    # `ext` retained for forward-compatibility; current modes are audio-only.
    if mode == "mp3":
        return "audio/mpeg"
    if mode == "wav":
        return "audio/wav"
    return "application/octet-stream"


# ============================================================
# HTTP SERVER
# ============================================================
class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

    def log_message(self, fmt, *args):
        # Access logs suppressed; error paths go via send_error override.
        pass

    def send_error(self, code, message=None, explain=None):
        cmd = getattr(self, "command", "-") or "-"
        path = getattr(self, "path", "-") or "-"
        sys.stderr.write(f"[HTTP {code}] {cmd} {path} — {message or ''}\n")
        try:
            super().send_error(code, message, explain)
        except Exception:
            pass

    def _check_host(self) -> bool:
        host = self.headers.get("Host", "")
        if host not in ALLOWED_HOSTS:
            self.send_error(400, "Bad or missing Host header")
            return False
        return True

    # ---- GET (static files) ----
    def do_GET(self):
        if self.path == "/healthz":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self._check_host():
            return
        try:
            super().do_GET()
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---- POST /process ----
    def do_POST(self):
        if not self._check_host():
            return
        if self.path != "/process":
            self.send_error(404, "Not found")
            return

        # Enforce declared body size before reading anything
        cl_header = self.headers.get("Content-Length")
        if cl_header is None:
            self.send_error(411, "Length Required")
            return
        try:
            content_length = int(cl_header)
        except ValueError:
            self.send_error(400, "Bad Content-Length")
            return
        if content_length <= 0:
            self.send_error(400, "Empty body")
            return
        if content_length > MAX_BYTES + 1024 * 1024:
            self.send_error(413, f"Body too large: {content_length} bytes")
            return

        # Content-Type must be multipart/form-data with a boundary
        ctype_header = self.headers.get("Content-Type", "")
        if not ctype_header.lower().startswith("multipart/form-data"):
            self.send_error(400, "Expected multipart/form-data")
            return
        if "boundary=" not in ctype_header.lower():
            self.send_error(400, "Missing multipart boundary")
            return

        # Read the body with an absolute cap
        remaining = content_length
        chunks = []
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                self.send_error(400, "Truncated request body")
                return
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)

        try:
            fields = parse_multipart(ctype_header, body)
        except Exception as exc:
            self.send_error(400, f"Malformed multipart body: {exc}")
            return

        # ---- Extract & validate fields ----
        file_data = fields.get("file", b"")
        if not isinstance(file_data, (bytes, bytearray)) or not file_data:
            self.send_error(400, "Missing or empty file field")
            return
        if len(file_data) > MAX_BYTES:
            self.send_error(413, f"File too large: {len(file_data)} bytes (max {MAX_BYTES})")
            return

        mode = fields.get("mode", "")
        if not isinstance(mode, str) or mode not in ALLOWED_MODES:
            self.send_error(400, f"Invalid mode: {mode!r}")
            return

        try:
            start = parse_non_negative(fields.get("start", "0"), "start")
            duration = parse_non_negative(fields.get("duration", "0"), "duration")
        except ValueError as exc:
            self.send_error(400, str(exc))
            return
        if duration <= 0 or duration > MAX_SECONDS:
            self.send_error(400, f"duration out of range (0, {MAX_SECONDS:.0f}]")
            return
        if start > MAX_SECONDS or start + duration > MAX_SECONDS:
            self.send_error(400, f"start + duration out of range [0, {MAX_SECONDS:.0f}]")
            return

        if mode == "mp3" and not HAS_LIBMP3LAME:
            self.send_error(503, "MP3 encoder (libmp3lame) is not available in this ffmpeg build")
            return

        # ---- Concurrency slot: acquired just before ffmpeg, released in finally ----
        sem = getattr(self.server, "_sem", None)
        acquired = False
        input_path = None
        output_path = None
        response_started = False
        try:
            # ---- Write input to a temp file ----
            with tempfile.NamedTemporaryFile(
                prefix=TEMP_PREFIX + "in_", suffix=".bin", delete=False,
            ) as fh:
                fh.write(file_data)
                input_path = Path(fh.name)

            # ---- Media validation ----
            available, info = probe_media(input_path)

            if available:
                if info is None:
                    self.send_error(400, "Unreadable media file")
                    return
                fmt = info["format_name"]
                if fmt:
                    names = set(fmt.split(","))
                    if not (names & ALLOWED_FORMATS):
                        self.send_error(400, f"Unsupported media format: {fmt!r}")
                        return
                if not info["has_audio"]:
                    self.send_error(400, "Input has no audio stream")
                    return

            # ---- WAV codec choice: preserve source sample format where possible ----
            if mode == "wav" and available and info:
                wav_codec = pick_wav_codec(info.get("audio_codec"), info.get("bits_per_sample"))
            else:
                # ffprobe unavailable, or mp3 mode (unused by Rust but passed
                # for a uniform arg list). pcm_s24le is the safe default.
                wav_codec = "pcm_s24le"

            actual_ext = MODE_TO_EXT[mode]
            output_path = input_path.with_suffix(".out." + actual_ext)

            # ---- Acquire a concurrency slot (do NOT block the accept loop) ----
            if sem is not None:
                if not sem.acquire(timeout=ACQUIRE_TIMEOUT_S):
                    self.send_error(503, "Server busy — please try again shortly")
                    return
                acquired = True

            # ---- Invoke Rust binary ----
            cmd = [
                str(RUST_BINARY), str(input_path), str(output_path),
                mode, f"{start:.3f}", f"{duration:.3f}", wav_codec,
            ]
            popen_kwargs = dict(
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if os.name == "posix":
                popen_kwargs["start_new_session"] = True

            try:
                proc = subprocess.Popen(cmd, **popen_kwargs)
            except FileNotFoundError:
                self.send_error(503, "Backend binary missing — rerun audiostrip.py")
                return

            try:
                stdout, stderr = proc.communicate(timeout=FFMPEG_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except OSError:
                        try: proc.kill()
                        except OSError: pass
                else:
                    try: proc.kill()
                    except OSError: pass
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
                self.send_error(504, "Processing timed out")
                return

            if proc.returncode != 0:
                last_err = (stderr or "").strip().splitlines()[-1:] or ["unknown error"]
                self.send_error(500, f"FFmpeg failed: {last_err[0]}")
                return

            if not output_path.exists():
                self.send_error(500, "Backend did not produce an output file")
                return
            out_data = output_path.read_bytes()
            if not out_data:
                self.send_error(500, "Backend produced an empty file")
                return

            mime = _mime_for(mode, actual_ext)
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(out_data)))
            self.send_header("X-Output-Ext", actual_ext)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            response_started = True
            try:
                self.wfile.write(out_data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        except Exception as exc:
            if response_started:
                sys.stderr.write(f"[error] exception after headers sent: {exc}\n")
                traceback.print_exc()
                self.close_connection = True
            else:
                sys.stderr.write(f"[error] internal: {exc}\n")
                traceback.print_exc()
                try:
                    self.send_error(500, "Internal server error")
                except Exception:
                    pass
        finally:
            if acquired and sem is not None:
                try:
                    sem.release()
                except ValueError:
                    sys.stderr.write("[error] semaphore over-release detected\n")
            for p in (input_path, output_path):
                if p is not None:
                    try:
                        if p.exists():
                            p.unlink()
                    except OSError:
                        pass


class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # BoundedSemaphore catches over-release programming errors.
        # Acquired inside do_POST, just before ffmpeg, so the accept loop
        # (and /healthz, static files) never block on a busy conversion.
        self._sem = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)


def start_server() -> None:
    if not RUST_BINARY.exists():
        print(f"[!] Backend binary missing at {RUST_BINARY}")
        print("    Re-run this script to rebuild.")
        sys.exit(1)

    sweep_stale_temp_files()

    with ThreadingTCPServer(("127.0.0.1", PORT), Handler) as httpd:
        url = f"http://127.0.0.1:{PORT}/"
        print(f"[+] Dashboard: {url}")
        print("[+] Press Ctrl-C to stop.")
        try:
            opened = webbrowser.open(url)
        except Exception:
            opened = False
        if not opened:
            print(f"[!] Could not open a browser automatically. Open {url} manually.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[+] Shutting down.")


# ============================================================
# MAIN
# ============================================================
def main() -> None:
    global HAS_LIBMP3LAME

    print("=" * 56)
    print(" Audio Stripper — Rust Backend Installer (WAV / MP3)")
    print("=" * 56)

    # 1. Ensure Rust
    if not check_rust():
        install_rust()
        if not check_rust():
            print("[!] Rust installation could not be verified.")
            print("    Install manually: https://rustup.rs/")
            sys.exit(1)
    else:
        print("[+] Rust already installed.")

    # 2. Ensure ffmpeg
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True, timeout=10)
        print("[+] ffmpeg detected.")
    except FileNotFoundError:
        print("[!] ffmpeg not found — the Rust backend requires it.")
        print("    Install: sudo apt install ffmpeg   (or brew install ffmpeg)")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("[!] ffmpeg did not respond to -version within 10s.")
        sys.exit(1)

    # 3. ffprobe (used for media validation + WAV codec selection; optional)
    try:
        subprocess.run(["ffprobe", "-version"], capture_output=True, check=True, timeout=10)
        print("[+] ffprobe detected.")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("[!] ffprobe not found — media validation will be skipped "
              "(WAV will default to 24-bit PCM; MP3 unaffected).")

    HAS_LIBMP3LAME = ffmpeg_has_encoder("libmp3lame")
    if HAS_LIBMP3LAME:
        print("[+] libmp3lame encoder available.")
    else:
        print("[!] libmp3lame encoder NOT available — MP3 mode will return 503.")

    # 4. Create project files
    create_rust_project()
    create_dashboard()
    write_template_version()

    # 5. Build (or reuse cached binary)
    if binary_is_stale():
        build_rust()
    else:
        print(f"[+] Reusing existing binary: {RUST_BINARY}")

    # 6. Serve
    start_server()


if __name__ == "__main__":
    main()
