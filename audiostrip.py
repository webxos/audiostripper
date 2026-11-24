#!/usr/bin/env python3
"""
audiostrip.py — Self-installing Audio Stripper with Rust backend + mastering mixer.

Run:
    python3 audiostrip.py

First run installs Rust (if needed), writes the Rust backend + dashboard,
compiles the binary, and serves the dashboard at http://127.0.0.1:8765.

Exports WAV (lossless, source bit depth / sample rate preserved) and
MP3 (max quality, VBR -q:a 0). The dashboard includes a lightweight
mastering mixer between the transfer (range + format) and export phases:

  * 10-band ISO graphic EQ (31 Hz … 16 kHz, ±12 dB, octave Q)
  * Master gain (-24 … +12 dB)
  * Peak normalize ("max volume": every export peaks at -0.1 dBFS)
  * EBU R128 loudness normalize (-14 LUFS)
  * Anti-clip limiter (-0.1 dB ceiling)
  * Live in-browser monitor (Web Audio) with a real-time level meter

The mixer curve is previewed live with Web Audio and rendered into the
exported file by the Rust backend (ffmpeg filter graph, two-pass when
normalizing). Requires: Python 3.9+, ffmpeg on PATH. No third-party
Python packages.
"""

import hashlib
import http.server
import json
import os
import platform
import re
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
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
FFMPEG_TIMEOUT_S = 600                        # covers measure + render passes
FFPROBE_TIMEOUT_S = 15
ALLOWED_HOSTS = {
    f"localhost:{PORT}",
    f"127.0.0.1:{PORT}",
    f"[::1]:{PORT}",           # IPv6 loopback (Host header as browsers send it)
}

# Request-handling bounds. ThreadingMixIn would spawn one thread per
# connection (unbounded); the server now runs a fixed worker pool and sheds
# excess connections, so a client flood cannot exhaust memory/threads.
MAX_WORKERS = 8                  # fixed request-handling thread pool
MAX_INFLIGHT_CONNECTIONS = 16    # hard cap on accepted-but-unfinished sockets

# Mixer field validation (must match the Rust backend and the UI)
EQ_BANDS        = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
EQ_GAIN_MIN     = -24.0
EQ_GAIN_MAX     = 24.0
MASTER_GAIN_MIN = -60.0
MASTER_GAIN_MAX = 60.0
ALLOWED_NORMALIZE = {"off", "peak", "loudness"}
ALLOWED_LIMITER = {"0", "1"}

# Populated at startup in main()
HAS_LIBMP3LAME = False

# ============================================================
# TEMPLATE VERSIONING
# ============================================================
TEMPLATE_VERSION = "7"
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
version = "0.2.0"
edition = "2021"

[[bin]]
name = "audiostrip-backend"
path = "src/main.rs"

[profile.release]
opt-level = 2
'''

MAIN_RS = r'''use std::env;
use std::process::{exit, Command};

// ---- Mixer constants (must match the Python server + dashboard) ----
const EQ_BANDS: [f64; 10] = [31.0, 62.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 16000.0];
const EQ_WIDTH_OCTAVES: f64 = 1.0;          // octave width <-> Q ~1.414, classic graphic-EQ response
const PEAK_CEILING_DB: f64 = -0.1;          // "max volume" target peak
const MAX_NORMALIZE_BOOST_DB: f64 = 36.0;   // guard: near-silence is not normalized
const LOUDNESS_I_DB: f64 = -14.0;           // EBU R128 streaming target
const LOUDNESS_TP_DB: f64 = -1.0;
const LOUDNESS_LRA: f64 = 11.0;
const ALIMITER: &str = "alimiter=level_in=1:level_out=1:limit=0.989:attack=5:release=50:level=disabled";

fn parse_f64(s: &str, label: &str) -> f64 {
    match s.trim().parse::<f64>() {
        Ok(v) if v.is_finite() => v,
        _ => {
            eprintln!("Invalid {}: expected a finite number, got {:?}", label, s);
            exit(2);
        }
    }
}

fn parse_non_negative(s: &str, label: &str) -> f64 {
    let v = parse_f64(s, label);
    if v < 0.0 {
        eprintln!("Invalid {}: must be >= 0, got {}", label, v);
        exit(2);
    }
    v
}

fn fmt_freq(f: f64) -> String {
    if (f - f.round()).abs() < 0.01 {
        format!("{}", f.round() as i64)
    } else {
        format!("{}", f)
    }
}

struct Band {
    freq: f64,
    gain: f64,
}

/// Parse an EQ spec like "31:-6.00,62:2.50". Frequencies must match the
/// known band set; gains are clamped-checked to +/-24 dB.
fn parse_eq(spec: &str) -> Vec<Band> {
    let mut out = Vec::new();
    let s = spec.trim();
    if s.is_empty() {
        return out;
    }
    for part in s.split(',') {
        let mut it = part.splitn(2, ':');
        let freq_s = match it.next() {
            Some(v) => v,
            None => {
                eprintln!("Malformed EQ part: {:?}", part);
                exit(2);
            }
        };
        let gain_s = match it.next() {
            Some(v) => v,
            None => {
                eprintln!("Malformed EQ part (missing gain): {:?}", part);
                exit(2);
            }
        };
        let freq = parse_non_negative(freq_s, "EQ frequency");
        let gain = parse_f64(gain_s, "EQ gain");
        if !EQ_BANDS.iter().any(|b| (*b - freq).abs() < 0.5) {
            eprintln!("Unknown EQ frequency: {}", freq);
            exit(2);
        }
        if gain < -24.0 || gain > 24.0 {
            eprintln!("EQ gain out of range [-24, 24]: {}", gain);
            exit(2);
        }
        out.push(Band { freq, gain });
    }
    out
}

/// Peaking filters for every band with a non-zero gain, low -> high.
fn eq_filters(bands: &[Band]) -> Vec<String> {
    bands
        .iter()
        .filter(|b| b.gain.abs() >= 0.05)
        .map(|b| {
            format!(
                "equalizer=f={}:t=o:w={}:g={:.2}",
                fmt_freq(b.freq),
                EQ_WIDTH_OCTAVES,
                b.gain
            )
        })
        .collect()
}

fn volume_filter(db: f64) -> String {
    format!("volume={:.2}dB:precision=double", db)
}

/// Extract a string value for a JSON key from loudnorm's stderr JSON print.
/// Escape-aware per RFC 8259 (\\, \", \uXXXX, …): a naive find-the-next-quote
/// scan would truncate on escaped quotes. Still zero-dependency on purpose —
/// the backend must build offline without a crates.io fetch.
fn json_field(text: &str, key: &str) -> Option<String> {
    let pat = format!("\"{}\"", key);
    let mut from = 0usize;
    while let Some(rel) = text[from..].find(&pat) {
        let idx = from + rel;
        if let Some(v) = json_string_after_colon(&text[idx + pat.len()..]) {
            return Some(v);
        }
        from = idx + pat.len();
    }
    None
}

/// Scan `:"…value…"` (whitespace tolerated) with full escape handling.
fn json_string_after_colon(text: &str) -> Option<String> {
    let mut chars = text.chars().skip_while(|c| c.is_whitespace());
    match chars.next()? {
        ':' => {}
        _ => return None,
    }
    let mut chars = chars.skip_while(|c| c.is_whitespace());
    match chars.next()? {
        '"' => {}
        _ => return None,
    }
    let mut out = String::new();
    while let Some(c) = chars.next() {
        match c {
            '"' => return Some(out),
            '\\' => match chars.next()? {
                '"' => out.push('"'),
                '\\' => out.push('\\'),
                '/' => out.push('/'),
                'n' => out.push('\n'),
                't' => out.push('\t'),
                'r' => out.push('\r'),
                'b' => out.push('\u{0008}'),
                'f' => out.push('\u{000C}'),
                'u' => {
                    let mut hex = String::new();
                    for _ in 0..4 {
                        hex.push(chars.next()?);
                    }
                    let cp = u32::from_str_radix(&hex, 16).ok()?;
                    out.push(char::from_u32(cp).unwrap_or('\u{FFFD}'));
                }
                _ => return None,
            },
            _ => out.push(c),
        }
    }
    None
}

struct LoudnessMeasure {
    input_i: String,
    input_lra: String,
    input_tp: String,
    input_thresh: String,
    target_offset: String,
}

/// Pass 1 for peak mode: decode the selection with the mixer chain and
/// report the highest sample peak in dBFS.
fn measure_peak(input: &str, start_s: &str, duration_s: &str, chain: &[String]) -> Option<f64> {
    let mut filters = chain.to_vec();
    filters.push("volumedetect".to_string());
    let out = Command::new("ffmpeg")
        .args(["-hide_banner", "-nostdin", "-threads", "1"])
        .args(["-ss", start_s, "-i", input, "-t", duration_s])
        .args(["-map", "0:a:0", "-vn"])
        .args(["-af", &filters.join(","), "-f", "null", "-"])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let stderr = String::from_utf8_lossy(&out.stderr);
    for line in stderr.lines() {
        // volumedetect prefixes every line with "[Parsed_volumedetect_N @ 0x…]",
        // so search for the field instead of matching a line prefix.
        if let Some(idx) = line.find("max_volume:") {
            let rest = line[idx + "max_volume:".len()..].trim();
            let tok = rest.trim_end_matches("dB").trim();
            if tok.contains("inf") {
                return None; // digital silence
            }
            return tok.parse::<f64>().ok();
        }
    }
    None
}

/// Pass 1 for loudness mode: run loudnorm in analysis mode and pull the
/// measured values out of its JSON print.
fn measure_loudness(input: &str, start_s: &str, duration_s: &str, chain: &[String]) -> Option<LoudnessMeasure> {
    let mut filters = chain.to_vec();
    filters.push(format!(
        "loudnorm=I={}:TP={}:LRA={}:print_format=json",
        LOUDNESS_I_DB, LOUDNESS_TP_DB, LOUDNESS_LRA
    ));
    let out = Command::new("ffmpeg")
        .args(["-hide_banner", "-nostdin", "-threads", "1"])
        .args(["-ss", start_s, "-i", input, "-t", duration_s])
        .args(["-map", "0:a:0", "-vn"])
        .args(["-af", &filters.join(","), "-f", "null", "-"])
        .output()
        .ok()?;
    let stderr = String::from_utf8_lossy(&out.stderr);
    let m = LoudnessMeasure {
        input_i: json_field(&stderr, "input_i")?,
        input_lra: json_field(&stderr, "input_lra")?,
        input_tp: json_field(&stderr, "input_tp")?,
        input_thresh: json_field(&stderr, "input_thresh")?,
        target_offset: json_field(&stderr, "target_offset")?,
    };
    for v in [&m.input_i, &m.input_lra, &m.input_tp, &m.input_thresh, &m.target_offset] {
        if v.contains("inf") || v.parse::<f64>().ok().filter(|f| f.is_finite()).is_none() {
            return None;
        }
    }
    Some(m)
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 6 {
        eprintln!(
            "Usage: audiostrip-backend <input> <output> <mode> <start> <duration> [wav_codec] [eq] [gain_db] [normalize] [limiter] [sample_rate]"
        );
        eprintln!("  eq        \"31:-6.00,1000:2.50\" (band:gain dB, optional)");
        eprintln!("  gain_db   master gain in dB, e.g. -3.50 (optional)");
        eprintln!("  normalize off | peak | loudness (optional)");
        eprintln!("  limiter   0 | 1 (optional, only used when normalize=off)");
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
    let eq_spec = if args.len() >= 8 { args[7].as_str() } else { "" };
    let gain_db = if args.len() >= 9 { parse_f64(&args[8], "gain_db") } else { 0.0 };
    let normalize = if args.len() >= 10 { args[9].as_str() } else { "off" };
    let limiter = if args.len() >= 11 { args[10].as_str() } else { "0" };
    let sample_rate: u32 = if args.len() >= 12 {
        args[11].trim().parse::<u32>().unwrap_or(0)
    } else {
        0
    };

    if duration <= 0.0 {
        eprintln!("Duration must be > 0, got {}", duration);
        exit(2);
    }
    if gain_db < -60.0 || gain_db > 60.0 {
        eprintln!("Master gain out of range [-60, 60] dB: {}", gain_db);
        exit(2);
    }
    if !matches!(normalize, "off" | "peak" | "loudness") {
        eprintln!("Unknown normalize mode: {:?}", normalize);
        exit(2);
    }
    if limiter != "0" && limiter != "1" {
        eprintln!("Invalid limiter flag: {:?} (expected 0 or 1)", limiter);
        exit(2);
    }

    // Re-format so the values passed to ffmpeg are fixed, numeric strings.
    // This is defence in depth: even if the caller bypasses HTTP validation,
    // the arguments can never begin with '-' or contain option syntax.
    let start_s = format!("{:.3}", start);
    let duration_s = format!("{:.3}", duration);

    let bands = parse_eq(eq_spec);
    let active_eq = bands.iter().filter(|b| b.gain.abs() >= 0.05).count();
    eprintln!(
        "[mixer] EQ bands active: {} | master gain: {:.2} dB | normalize: {} | limiter: {}",
        active_eq, gain_db, normalize, if limiter == "1" { "on" } else { "off" }
    );

    // Shared mixer chain: EQ -> master gain (order matters).
    let mut filters = eq_filters(&bands);
    if gain_db.abs() >= 0.01 {
        filters.push(volume_filter(gain_db));
    }

    match normalize {
        "peak" => {
            match measure_peak(input, &start_s, &duration_s, &filters) {
                Some(max_db) => {
                    let extra = PEAK_CEILING_DB - max_db;
                    if extra > MAX_NORMALIZE_BOOST_DB {
                        eprintln!(
                            "[mixer] peak measure: {:.1} dBFS -> near-silence, normalization skipped",
                            max_db
                        );
                    } else {
                        eprintln!(
                            "[mixer] peak measure: {:.1} dBFS -> normalize gain {:+.2} dB (ceiling {} dB)",
                            max_db, extra, PEAK_CEILING_DB
                        );
                        filters.push(volume_filter(extra));
                    }
                }
                None => {
                    eprintln!("[mixer] peak measurement failed; exporting without normalization");
                }
            }
        }
        "loudness" => {
            match measure_loudness(input, &start_s, &duration_s, &filters) {
                Some(m) => {
                    eprintln!(
                        "[mixer] loudness measured: I={} LUFS, LRA={}, TP={} dB, thresh={} dB -> linear normalize to {} LUFS",
                        m.input_i, m.input_lra, m.input_tp, m.input_thresh, LOUDNESS_I_DB
                    );
                    filters.push(format!(
                        "loudnorm=I={}:TP={}:LRA={}:linear=true:measured_I={}:measured_LRA={}:measured_TP={}:measured_thresh={}:offset={}",
                        LOUDNESS_I_DB, LOUDNESS_TP_DB, LOUDNESS_LRA,
                        m.input_i, m.input_lra, m.input_tp, m.input_thresh, m.target_offset
                    ));
                }
                None => {
                    eprintln!("[mixer] loudness measurement failed; falling back to single-pass loudnorm");
                    filters.push(format!(
                        "loudnorm=I={}:TP={}:LRA={}",
                        LOUDNESS_I_DB, LOUDNESS_TP_DB, LOUDNESS_LRA
                    ));
                }
            }
        }
        _ => {
            if limiter == "1" {
                eprintln!("[mixer] anti-clip limiter armed ({} dB ceiling)", PEAK_CEILING_DB);
                filters.push(ALIMITER.to_string());
            }
        }
    }

    let mut cmd = Command::new("ffmpeg");
    cmd.args(["-hide_banner", "-nostdin", "-y", "-threads", "1"]);

    // Fast decode-seek for both mp3 and wav (both re-encode).
    cmd.args(["-ss", &start_s, "-i", input, "-t", &duration_s]);

    cmd.args(["-map", "0:a:0", "-vn"]);
    if !filters.is_empty() {
        cmd.args(["-af", &filters.join(",")]);
    }
    // loudnorm processes at 192 kHz internally and would otherwise hand that
    // rate to the muxer; pin the output back to the source sample rate.
    if normalize == "loudness" && mode == "wav" && sample_rate > 0 {
        cmd.args(["-ar", &sample_rate.to_string()]);
    }

    match mode.as_str() {
        "mp3" => {
            cmd.args([
                "-c:a", "libmp3lame", "-q:a", "0",
                "-map_metadata", "-1", "-map_chapters", "-1",
                output,
            ]);
        }
        "wav" => {
            cmd.args([
                "-c:a", wav_codec,
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
  .app { max-width: 760px; margin: 0 auto; }
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
  /* ---- Master & Mix ---- */
  .mono { font-family: ui-monospace, monospace; }
  .mixerRow { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin: 10px 0 6px; }
  .mixerLabel {
    font-size: 0.72rem; color: var(--muted); font-weight: 700;
    min-width: 78px; text-transform: uppercase; letter-spacing: 0.05em;
  }
  select.sel {
    padding: 7px 9px; border-radius: var(--radius);
    border: 1px solid var(--border); background: var(--panel-2);
    color: var(--text); font-size: 0.82rem; font-family: inherit; max-width: 100%;
  }
  .chk { display: flex; align-items: center; gap: 6px; font-size: 0.8rem; color: var(--muted); cursor: pointer; }
  .chk input { accent-color: var(--text); margin: 0; }
  input[type="range"] { accent-color: var(--text); cursor: pointer; }
  input[type="range"].wide { flex: 1 1 160px; min-width: 120px; }
  #gainVal { min-width: 64px; text-align: right; font-size: 0.82rem; color: var(--text); }
  .eqScroller { overflow-x: auto; margin: 4px 0 2px; padding-bottom: 4px; }
  .eqRack { display: flex; gap: 4px; width: max-content; min-width: 100%; }
  .eqBand { display: flex; flex-direction: column; align-items: center; gap: 3px; width: 48px; flex: 0 0 auto; }
  .eqVal { font-family: ui-monospace, monospace; font-size: 0.66rem; color: var(--muted); min-height: 1em; }
  .eqFreq { font-family: ui-monospace, monospace; font-size: 0.66rem; color: var(--dim); }
  .vslider { position: relative; width: 48px; height: 150px; }
  .vslider input[type="range"] {
    position: absolute; top: 50%; left: 50%;
    width: 150px; height: 48px; margin: 0;
    transform: translate(-50%, -50%) rotate(-90deg);
    background: transparent;
  }
  .meterWrap { position: relative; margin-top: 12px; }
  #meterCanvas {
    width: 100%; height: 34px; display: block;
    background: #000; border: 1px solid var(--border); border-radius: 4px;
  }
  #clipLed {
    position: absolute; top: 10px; right: 10px;
    font-size: 0.6rem; font-weight: 800; letter-spacing: 0.12em; color: #333;
  }
  #clipLed.on { color: #ff4444; }
  #chainSummary { font-size: 0.7rem; color: var(--dim); margin-top: 8px; line-height: 1.5; word-break: break-word; }
  @media (max-width: 640px) {
    body { padding: 14px; }
    .mixerLabel { min-width: 100%; }
    .vslider { width: 44px; }
    .eqBand { width: 44px; }
  }
</style>
</head>
<body>
<div class="app">
  <h1>Audio Stripper</h1>
  <div class="sub">Rust backend · FFmpeg processing · WAV / MP3 export · 10-band EQ + volume mixer</div>

  <div id="dropZone" role="button" tabindex="0" aria-label="Choose a video or audio file">
    <div class="icon">🎬</div>
    <div><strong>Click or drag a video/audio file</strong></div>
    <div class="hint">Max 500 MiB · up to 60 minutes · MP4, WebM, MOV, MKV, MP3, WAV, M4A — exports WAV or MP3 with your mixer chain baked in</div>
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
    </div>

    <div class="panel">
      <h2>Master &amp; Mix</h2>
      <div class="qualityNote">
        Shape the tone with the 10-band graphic EQ, set the level, and choose how the export is
        normalized. Press play on the preview above to hear the chain and watch the meter — the same
        curve is rendered into the exported file by the Rust backend (FFmpeg).
      </div>

      <div class="mixerRow">
        <span class="mixerLabel">Graphic EQ</span>
        <select id="presetSelect" class="sel" aria-label="EQ preset">
          <option value="flat">Flat</option>
          <option value="bass">Bass boost</option>
          <option value="vocal">Vocal clarity</option>
          <option value="treble">Treble boost</option>
          <option value="rock">Rock (V-shape)</option>
          <option value="podcast">Podcast voice</option>
          <option value="custom" hidden>Custom</option>
        </select>
        <span class="qualityNote" style="margin:0;">±12 dB per band · double-click a fader to zero it</span>
      </div>
      <div class="eqScroller"><div class="eqRack" id="eqRack"></div></div>

      <div class="mixerRow">
        <span class="mixerLabel">Master gain</span>
        <input id="gainSlider" class="wide" type="range" min="-24" max="12" step="0.5" value="0" aria-label="Master gain in dB">
        <span id="gainVal" class="mono">+0.0 dB</span>
      </div>

      <div class="mixerRow">
        <span class="mixerLabel">Normalize</span>
        <select id="normalizeSelect" class="sel" aria-label="Normalization mode">
          <option value="peak" selected>Peak — max volume (-0.1 dBFS)</option>
          <option value="loudness">Loudness — EBU R128 (-14 LUFS)</option>
          <option value="off">Off</option>
        </select>
        <label class="chk"><input type="checkbox" id="limiterCheck" checked> <span id="limiterNote">Anti-clip limiter (-0.1 dB)</span></label>
      </div>

      <div class="mixerRow">
        <span class="mixerLabel">Monitor</span>
        <label class="chk"><input type="checkbox" id="monitorCheck" checked> Hear the EQ + gain while previewing</label>
        <button id="resetMixer" type="button">Reset mixer</button>
      </div>

      <div class="meterWrap">
        <canvas id="meterCanvas"></canvas>
        <span id="clipLed">CLIP</span>
      </div>
      <div id="chainSummary" class="mono"></div>
    </div>

    <div class="panel">
      <h2>Export</h2>
      <div class="qualityNote">
        The preview approximates the curve in your browser; the export renders it exactly
        (peaking biquads, octave width 1.0) and normalization is measured on your real audio
        in a first analysis pass.
      </div>
      <button id="processBtn" class="primary" style="width:100%;">Render &amp; Export with Rust</button>
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

  // ---- Mixer constants (must match the Rust backend / server) ----
  var EQ_FREQS  = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000];
  var EQ_LABELS = ['31', '62', '125', '250', '500', '1k', '2k', '4k', '8k', '16k'];
  var PRESETS = {
    flat:    [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    bass:    [6, 5, 4, 2, 0, 0, 0, 0, 0, 0],
    vocal:   [-3, -2, 0, 1.5, 3, 4, 3.5, 2.5, 1, 0],
    treble:  [0, 0, 0, 0, 0, 1, 2, 4, 5.5, 6.5],
    rock:    [5, 4.5, 3, 1.5, 0, -0.5, 1, 2.5, 4.5, 5],
    podcast: [-6, -5, -3, -1, 1.5, 3, 3.5, 3, 1.5, 0]
  };
  // Web Audio peaking Q matching the export chain's ffmpeg equalizer
  // (width_type=o, width=1.0 octave). For octave width w the biquad Q is
  // sqrt(2^w) / (2^w − 1); at w = 1 that is exactly sqrt(2).
  var EQ_Q = Math.SQRT2; // ≈ 1.4142

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
    logPre: document.getElementById('logPre'),
    eqRack: document.getElementById('eqRack'),
    presetSelect: document.getElementById('presetSelect'),
    gainSlider: document.getElementById('gainSlider'),
    gainVal: document.getElementById('gainVal'),
    normalizeSelect: document.getElementById('normalizeSelect'),
    limiterCheck: document.getElementById('limiterCheck'),
    limiterNote: document.getElementById('limiterNote'),
    monitorCheck: document.getElementById('monitorCheck'),
    resetMixer: document.getElementById('resetMixer'),
    meterCanvas: document.getElementById('meterCanvas'),
    clipLed: document.getElementById('clipLed'),
    chainSummary: document.getElementById('chainSummary')
  };

  var currentFile = null;
  var videoDuration = 0;
  var busy = false;
  var videoURL = null;
  var outputURL = null;

  // ---- Mixer state ----
  var eqGains = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0];
  var gainDb = 0;

  // ---- Web Audio monitor state ----
  var audioCtx = null;
  var srcNode = null;
  var eqNodes = [];
  var masterNode = null;
  var analyser = null;
  var meterData = null;
  var meterRaf = 0;
  var meterPeak = 0;
  var meterHold = 0;
  var clipUntil = 0;

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
      chainSummary();
      setStatus('Ready — dial in the mixer, choose a mode, then Render & Export.', 'ok');
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
    el.processBtn.textContent = v ? 'Rendering…' : 'Render & Export with Rust';
    el.dropZone.classList.toggle('disabled', v);
  }

  // ================= Mixer =================
  function fmtDb(v) {
    if (Math.abs(v) < 0.025) v = 0;
    return (v > 0 ? '+' : '') + v.toFixed(1);
  }

  function setBand(i, v, fromUser) {
    if (!isFinite(v)) v = 0;
    if (Math.abs(v) < 0.025) v = 0;
    eqGains[i] = v;
    var band = el.eqRack.children[i];
    var slider = band.querySelector('input');
    if (parseFloat(slider.value) !== v) slider.value = v;
    band.querySelector('.eqVal').textContent = fmtDb(v);
    if (fromUser) el.presetSelect.value = 'custom';
    applyWebAudioEq();
    chainSummary();
  }

  function applyPreset(name) {
    var p = PRESETS[name];
    if (!p) return;
    for (var i = 0; i < EQ_FREQS.length; i++) setBand(i, p[i], false);
    el.presetSelect.value = name;
  }

  function buildEqRack() {
    EQ_FREQS.forEach(function (f, i) {
      var band = document.createElement('div');
      band.className = 'eqBand';
      var val = document.createElement('div');
      val.className = 'eqVal';
      val.textContent = '0.0';
      var wrap = document.createElement('div');
      wrap.className = 'vslider';
      var s = document.createElement('input');
      s.type = 'range';
      s.min = '-12';
      s.max = '12';
      s.step = '0.5';
      s.value = '0';
      s.setAttribute('aria-label', EQ_LABELS[i] + ' Hz band gain in dB');
      s.title = EQ_LABELS[i] + ' Hz — double-click to zero';
      s.addEventListener('input', function () { setBand(i, parseFloat(s.value), true); });
      s.addEventListener('dblclick', function () { setBand(i, 0, true); });
      wrap.appendChild(s);
      var freq = document.createElement('div');
      freq.className = 'eqFreq';
      freq.textContent = EQ_LABELS[i];
      band.appendChild(val);
      band.appendChild(wrap);
      band.appendChild(freq);
      el.eqRack.appendChild(band);
    });
  }

  function eqSpecString() {
    var parts = [];
    for (var i = 0; i < EQ_FREQS.length; i++) {
      parts.push(EQ_FREQS[i] + ':' + eqGains[i].toFixed(2));
    }
    return parts.join(',');
  }

  function refreshLimiterState() {
    var off = el.normalizeSelect.value === 'off';
    el.limiterCheck.disabled = !off;
    el.limiterNote.textContent = off
      ? 'Anti-clip limiter (-0.1 dB)'
      : 'Limiter idle — normalization prevents clipping';
  }

  function chainSummary() {
    var parts = [];
    var eqParts = [];
    for (var i = 0; i < EQ_FREQS.length; i++) {
      if (Math.abs(eqGains[i]) >= 0.05) eqParts.push(EQ_LABELS[i] + ':' + fmtDb(eqGains[i]));
    }
    parts.push(eqParts.length ? 'EQ ' + eqParts.join(' ') : 'EQ flat');
    parts.push('Gain ' + fmtDb(gainDb) + ' dB');
    var n = el.normalizeSelect.value;
    if (n === 'peak') parts.push('Peak normalize to -0.1 dBFS');
    else if (n === 'loudness') parts.push('Loudness normalize to -14 LUFS (EBU R128)');
    if (n === 'off' && el.limiterCheck.checked) parts.push('Limiter -0.1 dB');
    el.chainSummary.textContent = 'Export chain: ' + parts.join(' · ');
  }

  function resetMixer() {
    applyPreset('flat');
    gainDb = 0;
    el.gainSlider.value = '0';
    el.gainVal.textContent = '+0.0 dB';
    el.normalizeSelect.value = 'peak';
    el.limiterCheck.checked = true;
    refreshLimiterState();
    applyWebAudioEq();
    chainSummary();
  }

  // ============ Web Audio monitor ============
  function dbToLin(db) { return Math.pow(10, db / 20); }

  function ensureGraph() {
    if (audioCtx) return;
    var Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) {
      el.monitorCheck.checked = false;
      el.monitorCheck.disabled = true;
      return;
    }
    try {
      audioCtx = new Ctx();
      srcNode = audioCtx.createMediaElementSource(el.previewPlayer);
    } catch (e) {
      audioCtx = null;
      el.monitorCheck.checked = false;
      el.monitorCheck.disabled = true;
      return;
    }
    eqNodes = EQ_FREQS.map(function (f) {
      var b = audioCtx.createBiquadFilter();
      b.type = 'peaking';
      b.frequency.value = f;
      b.Q.value = EQ_Q;
      b.gain.value = 0;
      return b;
    });
    masterNode = audioCtx.createGain();
    masterNode.gain.value = dbToLin(gainDb);
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 2048;
    meterData = new Float32Array(analyser.fftSize);
    routeGraph();
    applyWebAudioEq();
  }

  function routeGraph() {
    if (!audioCtx) return;
    var monitorOn = el.monitorCheck.checked;
    var nodes = [srcNode].concat(eqNodes, [masterNode, analyser]);
    nodes.forEach(function (n) { try { n.disconnect(); } catch (_) {} });
    if (monitorOn) {
      srcNode.connect(eqNodes[0]);
      for (var i = 0; i < eqNodes.length - 1; i++) eqNodes[i].connect(eqNodes[i + 1]);
      eqNodes[eqNodes.length - 1].connect(masterNode);
      masterNode.connect(analyser);
    } else {
      srcNode.connect(analyser); // analyser is a pass-through: meter still works
    }
    analyser.connect(audioCtx.destination);
  }

  function applyWebAudioEq() {
    if (!audioCtx) return;
    for (var i = 0; i < eqNodes.length; i++) eqNodes[i].gain.value = eqGains[i];
    if (masterNode) masterNode.gain.value = dbToLin(gainDb);
  }

  // ============ Meter ============
  function startMeter() {
    if (!meterRaf) meterRaf = requestAnimationFrame(meterLoop);
  }
  function stopMeter() {
    if (meterRaf) { cancelAnimationFrame(meterRaf); meterRaf = 0; }
    meterPeak = 0; meterHold = 0;
    drawMeter();
  }
  function meterLoop() {
    drawMeter();
    meterRaf = requestAnimationFrame(meterLoop);
  }
  function drawMeter() {
    var c = el.meterCanvas;
    var dpr = window.devicePixelRatio || 1;
    var w = c.clientWidth, h = c.clientHeight;
    if (w === 0 || h === 0) return;
    if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
      c.width = Math.round(w * dpr);
      c.height = Math.round(h * dpr);
    }
    var ctx = c.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    var now = performance.now();
    if (analyser && audioCtx) {
      var p = 0;
      try {
        analyser.getFloatTimeDomainData(meterData);
        for (var i = 0; i < meterData.length; i++) {
          var a = Math.abs(meterData[i]);
          if (a > p) p = a;
        }
      } catch (_) { p = 0; }
      meterPeak = p > meterPeak ? p : meterPeak * 0.86;
      meterHold = p > meterHold ? p : meterHold * 0.995;
      if (p >= 0.999) clipUntil = now + 900;
    }
    var db = 20 * Math.log10(Math.max(meterPeak, 1e-6));
    var x = Math.min(1, Math.max(0, (db + 48) / 48));
    var barW = x * (w - 2), barH = h - 2, y0 = 1;

    if (barW > 0) {
      var seg = [
        [0.70, '#22c55e'], // > -14.4 dB
        [0.90, '#eab308'], // > -4.8 dB
        [1.01, '#ef4444']
      ];
      var xPrev = 0;
      for (var s = 0; s < seg.length; s++) {
        var xSeg = Math.min(barW, seg[s][0] * (w - 2));
        if (xSeg <= xPrev) continue;
        ctx.fillStyle = seg[s][1];
        ctx.fillRect(1 + xPrev, y0, xSeg - xPrev, barH);
        xPrev = xSeg;
        if (xPrev >= barW) break;
      }
    }
    var hDb = 20 * Math.log10(Math.max(meterHold, 1e-6));
    var hx = Math.min(1, Math.max(0, (hDb + 48) / 48)) * (w - 2);
    if (hx > 0) {
      ctx.fillStyle = '#ffffff';
      ctx.fillRect(1 + hx - 1, y0, 2, barH);
    }
    el.clipLed.classList.toggle('on', now < clipUntil);

    // dB scale ticks are drawn in the same frame as the bar: clearRect()
    // wipes the whole canvas every frame, so a scale drawn by a separate
    // one-off pass would vanish the moment playback starts.
    ctx.fillStyle = '#555';
    ctx.font = '9px ui-monospace, monospace';
    var marks = [-48, -36, -24, -12, -6, 0];
    for (var m = 0; m < marks.length; m++) {
      var mx = 1 + ((marks[m] + 48) / 48) * (w - 2);
      ctx.fillRect(mx, h - 5, 1, 3);
      ctx.fillText(String(marks[m]), Math.min(Math.max(mx - 8, 1), w - 22), h - 7);
    }
  }

  // ================= Process =================
  function process() {
    if (busy || !currentFile) return;
    setBusy(true);
    try { el.previewPlayer.pause(); } catch (_) {}
    resetResult();
    resetLog();
    setStatus('Rendering with Rust backend (EQ + gain + normalization)…', 'busy');

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
    form.append('eq', eqSpecString());
    form.append('gain', gainDb.toFixed(2));
    form.append('normalize', el.normalizeSelect.value);
    form.append('limiter', el.limiterCheck.checked ? '1' : '0');

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
        var name = base + '-mastered.' + res.ext;
        el.downloadBtn.href = outputURL;
        el.downloadBtn.download = name;
        el.downloadBtn.textContent = '⬇ Download ' + name;
        el.downloadBtn.hidden = false;
        el.audioPlayer.src = outputURL;
        el.audioPlayer.hidden = false;
        el.result.hidden = false;
        setStatus('Done — mixer chain rendered into the export.', 'ok');
        showLog('Rust backend returned ' + res.buf.byteLength + ' bytes (' + res.ext + ').');
        showLog('Export chain: ' + el.chainSummary.textContent.replace(/^Export chain: /, ''));
      })
      .catch(function (err) {
        setStatus('Error: ' + (err && err.message ? err.message : String(err)), 'error');
        showLog(err && err.stack ? err.stack : String(err));
      })
      .finally(function () { setBusy(false); });
  }

  // ================= Wiring =================
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

  // Mixer wiring
  buildEqRack();
  el.presetSelect.addEventListener('change', function () { applyPreset(el.presetSelect.value); });
  el.gainSlider.addEventListener('input', function () {
    gainDb = parseFloat(el.gainSlider.value);
    el.gainVal.textContent = fmtDb(gainDb) + ' dB';
    applyWebAudioEq();
    chainSummary();
  });
  el.normalizeSelect.addEventListener('change', function () {
    refreshLimiterState();
    chainSummary();
  });
  el.limiterCheck.addEventListener('change', chainSummary);
  el.monitorCheck.addEventListener('change', function () {
    if (el.monitorCheck.checked && !audioCtx) ensureGraph();
    routeGraph();
  });
  el.resetMixer.addEventListener('click', resetMixer);

  el.previewPlayer.addEventListener('play', function () {
    ensureGraph();
    if (audioCtx && audioCtx.state === 'suspended') { try { audioCtx.resume(); } catch (_) {} }
    startMeter();
  });
  el.previewPlayer.addEventListener('pause', stopMeter);
  el.previewPlayer.addEventListener('ended', stopMeter);

  window.addEventListener('resize', drawMeter);
  drawMeter();
  refreshLimiterState();
  chainSummary();

  window.addEventListener('pagehide', function () {
    revokeVideo();
    revokeOutput();
    if (audioCtx) { try { audioCtx.close(); } catch (_) {} audioCtx = null; }
  });
})();
</script>
</body>
</html>
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
# Manual RFC 7578/2046 boundary splitter. The email module is tuned for
# RFC 2822 mail and can re-encode, wrap, or otherwise mangle arbitrary
# binary HTTP payloads, so multipart/form-data is split here byte-exactly.

# Content-Disposition parameters: name="quoted \"value\"" or name=token.
_CONTENT_DISP_PARAM_RE = re.compile(
    r'([A-Za-z0-9!*+._-]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s;]*))'
)


def _unescape_param(value: str) -> str:
    """Undo the backslash escaping used inside quoted-string parameters."""
    return re.sub(r"\\(.)", r"\1", value)


def _parse_content_disposition(value: str):
    """Return (name, filename) from a Content-Disposition header value."""
    name = None
    filename = None
    for match in _CONTENT_DISP_PARAM_RE.finditer(value):
        key = match.group(1).lower()
        if match.group(2) is not None:
            val = _unescape_param(match.group(2))
        else:
            val = match.group(3)
        if key == "name" and name is None:
            name = val
        elif key == "filename" and filename is None:
            filename = val
    return name, filename


def _multipart_boundary(content_type: str) -> bytes:
    """Extract the boundary parameter from a Content-Type header."""
    for param in content_type.split(";")[1:]:
        key, _, val = param.partition("=")
        if key.strip().lower() == "boundary":
            val = val.strip()
            if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                val = val[1:-1]
            if val:
                try:
                    return val.encode("ascii")
                except UnicodeEncodeError:
                    break
    raise ValueError("missing or invalid multipart boundary")


def _iter_multipart_parts(body: bytes, boundary: bytes):
    """Yield each part as raw (headers + content) bytes.

    Delimiters must sit at the start of a line, and the CRLF before a
    delimiter belongs to the framing, not to the part content (RFC 2046).
    """
    sep = b"--" + boundary
    n = len(body)

    # First delimiter line (tolerate a preamble before it).
    pos = -1
    search = 0
    while True:
        k = body.find(sep, search)
        if k == -1:
            break
        if k == 0 or body[k - 1] == 0x0A:
            pos = k
            break
        search = k + 1
    if pos == -1:
        raise ValueError("boundary not found in body")
    if body.find(sep + b"--") == -1:
        raise ValueError("multipart body missing closing boundary")
    pos += len(sep)

    while pos < n:
        if body.startswith(b"--", pos):
            return  # closing boundary reached; ignore any epilogue
        line_end = body.find(b"\n", pos)
        if line_end == -1:
            return  # trailing garbage after the last boundary
        pos = line_end + 1

        # Next delimiter line. Requiring a line-start match means a byte
        # sequence equal to the delimiter inside content, mid-line, is
        # never mistaken for a boundary.
        nxt = -1
        search = pos
        while True:
            k = body.find(sep, search)
            if k == -1:
                break
            if k == 0 or body[k - 1] == 0x0A:
                nxt = k
                break
            search = k + 1
        if nxt == -1:
            raise ValueError("unterminated multipart part")

        part = body[pos:nxt]
        # Strip the framing CRLF (or bare LF from sloppy encoders) that
        # precedes the delimiter — it is not part of the content.
        if part.endswith(b"\r\n"):
            part = part[:-2]
        elif part.endswith(b"\n"):
            part = part[:-1]
        yield part
        pos = nxt + len(sep)


def parse_multipart(content_type: str, body: bytes) -> dict:
    """Parse a multipart/form-data body into a dict.

    Parts with a `filename` are returned as raw bytes (binary-safe);
    other parts as UTF-8 str, falling back to raw bytes when not decodable.
    """
    boundary = _multipart_boundary(content_type)
    fields = {}
    for part in _iter_multipart_parts(body, boundary):
        blank = re.search(rb"\r\n\r\n|\n\n", part)
        if blank:
            header_blob = part[:blank.start()]
            content = part[blank.end():]
        else:
            header_blob = part
            content = b""

        name = None
        filename = None
        for line in header_blob.splitlines():
            if line.lower().startswith(b"content-disposition:"):
                n, f = _parse_content_disposition(line.decode("iso-8859-1"))
                if n:
                    name, filename = n, f
                    break
        if not name:
            continue
        if filename is not None:
            fields[name] = content
        else:
            try:
                fields[name] = content.decode("utf-8")
            except UnicodeDecodeError:
                fields[name] = content
    return fields


# ============================================================
# HELPERS
# ============================================================
def parse_bounded(value, label: str, lo: float, hi: float) -> float:
    """Parse `value` as a finite float within [lo, hi]; raise ValueError otherwise."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} is not a number: {value!r}")
    if v != v or v in (float("inf"), float("-inf")):
        raise ValueError(f"{label} is not finite: {value!r}")
    if v < lo or v > hi:
        raise ValueError(f"{label} out of range [{lo}, {hi}]: {v}")
    return v


def parse_non_negative(value, label: str) -> float:
    """Parse `value` as a non-negative finite float; raise ValueError otherwise."""
    return parse_bounded(value, label, 0.0, float("inf"))


def parse_eq(value) -> str:
    """Validate an EQ spec 'freq:gain,...' against the known bands.

    Returns the canonical form (bands ordered low -> high, gains at 2 dp).
    """
    s = (value or "").strip()
    if not s:
        return ""
    by_freq = {}
    for part in s.split(","):
        freq_s, sep, gain_s = part.partition(":")
        if not sep:
            raise ValueError(f"Malformed EQ part (expected freq:gain): {part!r}")
        freq = parse_bounded(freq_s, "EQ frequency", 0.0, 100000.0)
        match = next((b for b in EQ_BANDS if abs(b - freq) < 0.5), None)
        if match is None:
            raise ValueError(f"Unknown EQ frequency: {freq:g} (allowed: {', '.join(map(str, EQ_BANDS))})")
        gain = parse_bounded(gain_s, "EQ gain", EQ_GAIN_MIN, EQ_GAIN_MAX)
        by_freq[match] = gain
    return ",".join(f"{f}:{by_freq[f]:.2f}" for f in EQ_BANDS if f in by_freq)


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
             "-show_entries", "stream=codec_type,codec_name,bits_per_sample,sample_rate",
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
        "sample_rate": audio_stream.get("sample_rate") if audio_stream else None,
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
# BACKEND PROCESS REGISTRY (prompt shutdown / disconnect kills)
# ============================================================
_ACTIVE_BACKEND_PROCS = set()
_ACTIVE_BACKEND_PROCS_LOCK = threading.Lock()


def _kill_backend(proc) -> None:
    """Terminate a backend process tree (posix: the whole process group)."""
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass
    else:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _kill_all_backends() -> None:
    with _ACTIVE_BACKEND_PROCS_LOCK:
        procs = list(_ACTIVE_BACKEND_PROCS)
    for proc in procs:
        _kill_backend(proc)


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

    def _client_gone(self) -> bool:
        """True when the client has closed or reset the connection.

        Non-blocking 1-byte peek: EOF → closed; EWOULDBLOCK → the client is
        still waiting for the response (alive); reset/bad socket → gone.
        """
        sock = getattr(self, "connection", None)
        if sock is None:
            return True
        try:
            sock.setblocking(False)
        except OSError:
            return True
        try:
            return sock.recv(1, socket.MSG_PEEK) == b""
        except BlockingIOError:
            return False
        except OSError:
            return True
        finally:
            try:
                sock.setblocking(True)
            except OSError:
                pass

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

        # ---- Mixer fields ----
        try:
            eq_spec = parse_eq(fields.get("eq", ""))
        except ValueError as exc:
            self.send_error(400, f"Invalid EQ: {exc}")
            return
        try:
            master_gain = parse_bounded(fields.get("gain", "0"), "gain", MASTER_GAIN_MIN, MASTER_GAIN_MAX)
        except ValueError as exc:
            self.send_error(400, str(exc))
            return
        normalize = fields.get("normalize", "off")
        if not isinstance(normalize, str) or normalize not in ALLOWED_NORMALIZE:
            self.send_error(400, f"Invalid normalize: {normalize!r}")
            return
        limiter = fields.get("limiter", "0")
        if not isinstance(limiter, str) or limiter not in ALLOWED_LIMITER:
            self.send_error(400, f"Invalid limiter flag: {limiter!r}")
            return
        if normalize != "off":
            # The limiter is meaningless when a normalizer runs after it.
            limiter = "0"

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

            try:
                sample_rate = int((info or {}).get("sample_rate") or 0)
            except (TypeError, ValueError):
                sample_rate = 0
            if not (0 < sample_rate < 4_000_000):
                sample_rate = 0

            actual_ext = MODE_TO_EXT[mode]
            output_path = input_path.with_suffix(".out." + actual_ext)

            # ---- Acquire a concurrency slot (do NOT block the accept loop) ----
            # Polled in slices so a client that hangs up while queued frees
            # its slot right away instead of holding it for ACQUIRE_TIMEOUT_S.
            if sem is not None:
                waited = 0.0
                while True:
                    if sem.acquire(timeout=0.5):
                        acquired = True
                        break
                    waited += 0.5
                    if self._client_gone():
                        sys.stderr.write("[process] client left while queued for a slot\n")
                        return
                    if waited >= ACQUIRE_TIMEOUT_S:
                        self.send_error(503, "Server busy — please try again shortly")
                        return

            # ---- Invoke Rust binary ----
            cmd = [
                str(RUST_BINARY), str(input_path), str(output_path),
                mode, f"{start:.3f}", f"{duration:.3f}", wav_codec,
                eq_spec, f"{master_gain:.2f}", normalize, limiter,
                str(sample_rate),
            ]
            popen_kwargs = dict(
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            if os.name == "posix":
                popen_kwargs["start_new_session"] = True

            try:
                proc = subprocess.Popen(cmd, **popen_kwargs)
            except FileNotFoundError:
                self.send_error(503, "Backend binary missing — rerun audiostrip.py")
                return
            with _ACTIVE_BACKEND_PROCS_LOCK:
                _ACTIVE_BACKEND_PROCS.add(proc)

            # Drain the backend's stderr in a helper thread so a chatty
            # ffmpeg can never fill its pipe and stall, while this thread
            # polls process state and the client connection. A client that
            # disconnects mid-processing now frees this thread (and kills
            # ffmpeg) instead of blocking here for up to FFMPEG_TIMEOUT_S.
            stderr_chunks = []

            def _drain_stderr(pipe, sink):
                try:
                    while True:
                        chunk = pipe.read(65536)
                        if not chunk:
                            break
                        sink.append(chunk)
                except (OSError, ValueError):
                    pass

            drainer = threading.Thread(
                target=_drain_stderr, args=(proc.stderr, stderr_chunks),
                name="audiostrip-stderr", daemon=True,
            )
            drainer.start()

            deadline = time.monotonic() + FFMPEG_TIMEOUT_S
            killed_for = None
            while proc.poll() is None:
                if time.monotonic() > deadline:
                    killed_for = "timeout"
                    break
                if self._client_gone():
                    killed_for = "client disconnect"
                    break
                time.sleep(0.25)

            with _ACTIVE_BACKEND_PROCS_LOCK:
                _ACTIVE_BACKEND_PROCS.discard(proc)

            if killed_for is not None:
                _kill_backend(proc)
                drainer.join(timeout=5)
                sys.stderr.write(f"[process] backend stopped: {killed_for}\n")
                if killed_for == "timeout":
                    self.send_error(504, "Processing timed out")
                return  # on disconnect the client is gone; nothing to answer

            drainer.join(timeout=5)
            stderr_text = b"".join(stderr_chunks).decode("utf-8", "replace")

            if proc.returncode != 0:
                last_err = stderr_text.strip().splitlines()[-1:] or ["unknown error"]
                self.send_error(500, f"FFmpeg failed: {last_err[0]}")
                return

            # Surface the backend's mixer diagnostics in the server log.
            for line in stderr_text.splitlines():
                if line.startswith("[mixer]"):
                    sys.stderr.write(f"[backend] {line}\n")

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


class PooledTCPServer(socketserver.TCPServer):
    """TCP server with a fixed worker pool and a hard in-flight cap.

    ThreadingMixIn would spawn one thread per connection, so a connection
    flood could exhaust memory/threads before the ffmpeg semaphore ever
    applied. Here the accept loop hands sockets to a bounded
    ThreadPoolExecutor and sheds anything beyond MAX_INFLIGHT_CONNECTIONS.
    """
    allow_reuse_address = True
    request_queue_size = 8

    def __init__(self, *args, **kwargs):
        self._inflight = 0
        self._inflight_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(
            max_workers=MAX_WORKERS, thread_name_prefix="audiostrip")
        super().__init__(*args, **kwargs)
        # BoundedSemaphore catches over-release programming errors.
        # Acquired inside do_POST, just before the backend runs, so the
        # accept loop (and /healthz, static files) never block on a busy
        # conversion.
        self._sem = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)

    def process_request(self, request, client_address):
        with self._inflight_lock:
            if self._inflight >= MAX_INFLIGHT_CONNECTIONS:
                # Overloaded: shed the connection instead of queueing it.
                try:
                    request.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    request.close()
                except OSError:
                    pass
                return
            self._inflight += 1
        try:
            self._pool.submit(self._process_request_thread, request, client_address)
        except RuntimeError:
            # Pool already shut down (server closing).
            self._release_inflight()
            self.shutdown_request(request)

    def _process_request_thread(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except (ConnectionError, socket.timeout):
            # Routine client-side drops: no traceback noise.
            pass
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)
            self._release_inflight()

    def _release_inflight(self):
        with self._inflight_lock:
            self._inflight -= 1

    def server_close(self):
        super().server_close()
        self._pool.shutdown(wait=False)


class PooledTCPServerV6(PooledTCPServer):
    """Dual-stack variant: binds '::' and serves both ::1 and 127.0.0.1."""

    address_family = socket.AF_INET6

    def server_bind(self):
        # Must be set before bind() to take effect: keep IPv4-mapped
        # addresses reachable so one socket serves both loopbacks.
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        super().server_bind()


def start_server() -> None:
    if not RUST_BINARY.exists():
        print(f"[!] Backend binary missing at {RUST_BINARY}")
        print("    Re-run this script to rebuild.")
        sys.exit(1)

    sweep_stale_temp_files()

    # Prefer dual-stack IPv6 (one socket serves ::1 and 127.0.0.1); fall
    # back to plain IPv4 loopback when IPv6 is unavailable.
    try:
        httpd = PooledTCPServerV6(("::", PORT), Handler)
        print("[+] Listening on :: (dual-stack: 127.0.0.1 and ::1).")
    except OSError:
        httpd = PooledTCPServer(("127.0.0.1", PORT), Handler)
        print("[+] Listening on 127.0.0.1 (IPv6 loopback unavailable).")

    url = f"http://127.0.0.1:{PORT}/"
    print(f"[+] Dashboard: {url}")
    print("[+] Press Ctrl-C to stop.")
    try:
        opened = webbrowser.open(url)
    except Exception:
        opened = False
    if not opened:
        print(f"[!] Could not open a browser automatically. Open {url} manually.")
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[+] Shutting down.")
    finally:
        # Kill any in-flight conversions so pooled threads finish promptly.
        _kill_all_backends()
        httpd.server_close()


# ============================================================
# MAIN
# ============================================================
def main() -> None:
    global HAS_LIBMP3LAME

    print("=" * 56)
    print(" Audio Stripper — Rust Backend + Mixer (WAV / MP3)")
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
