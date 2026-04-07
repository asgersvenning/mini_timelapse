<div align="center">
  <h1>📸 mini-timelapse</h1>
  <p><em>A lightweight Python toolkit for compiling timestamped, geolocated images into compact timelapse videos — and extracting them back out with metadata intact.</em></p>
  
  [![Python version](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
  [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
  [![Tests](https://github.com/asgersvenning/mini-timelapse/actions/workflows/test.yml/badge.svg)](https://github.com/asgersvenning/mini-timelapse/actions)
</div>

---

**mini-timelapse** encodes each source image as exactly one video frame (1:1 mapping), embeds the original EXIF timestamps and GPS coordinates inside the `.mkv` container, and provides a Pythonic API for random-access frame retrieval with associated metadata.

## ✨ Features

- 🎞️ **Compile**: Directory of JPEGs → single `.mkv` with interleaved metadata.
- 🖼️ **Decompile**: `.mkv` → directory of JPEGs with EXIF timestamps and GPS restored.
- 📡 **Remote Support**: Use [pyremotedata](https://github.com/asgersvenning/pyremotedata) to compile directly from or decompile to SFTP/ERDA storage.
- 🔢 **Natural Sort**: Handles non-padded filenames (`img_1.jpg`, `img_2.jpg`, `img_10.jpg`) in correct numeric order.
- 🐍 **Python API**: Index, slice, and iterate frames seamlessly with `TimelapseVideo`.
- 📦 **Portable**: Pure Python — no system FFmpeg installation required (powered by [PyAV](https://pyav.org/)).
- 🚀 **Efficient**: Threaded image pre-loading, single-pass interleaved metadata.

## 🛠️ Installation

Requires **Python ≥ 3.12**. Install rapidly with [uv](https://docs.astral.sh/uv/):

```bash
uv pip install -e .
```

Or with traditional pip:

```bash
pip install -e .
```

## 🚀 Quick Start

### 1️⃣ Compile Images to Video

```bash
timelapse-compile -i ./my_photos/ -o timelapse.mkv
```

> **Note**: Default behavior includes images ending in `.jpg`/`.png`. Frames are sorted chronologically and encoded 1:1 into the video.

### 2️⃣ Decompile Video Back to Images

```bash
timelapse-decompile -i timelapse.mkv -o ./extracted/
```

> Each frame is saved as a precise JPEG with its original EXIF timestamps and GPS coordinates restored.

### 3️⃣ Remote Support (Optional)

If you have [pyremotedata](https://github.com/asgersvenning/pyremotedata) installed, you can seamlessly work with SFTP sources (like ERDA):

```bash
# Set your credentials
export PYREMOTEDATA_REMOTE_USERNAME="myuser"
export PYREMOTEDATA_REMOTE_HOSTNAME="io.erda.dk"

# Compile directly from a remote folder
timelapse-compile -i /remote/path/to/images/ -o timelapse.mkv --remote

# Decompile directly to a remote folder
timelapse-decompile -i timelapse.mkv -o /remote/output/folder/ --remote
```

This uses `RemotePathIterator` to stream images in the background, smoothly overlapping network downloads with video encoding.

## 💻 Python API

The `mini-timelapse` toolkit provides a robust Python API for programmatic workflows.

### 🎥 Working with a Timelapse

```python
from mini_timelapse.reader import TimelapseVideo

with TimelapseVideo("timelapse.mkv") as video:
    print(f"Loaded {len(video)} frames")

    # Random access
    frame, meta = video[42]
    print(meta.get("time"))          # "2024-06-15 10:42:00"
    print(meta.get("lat"), meta.get("lon"))  # 55.6761, 12.5683
    print(frame.shape)               # (height, width, 3) — RGB numpy array

    # Sequential iteration (Fastest)
    for frame, meta in video:
        process(frame, meta)

    # Seamless Slicing
    first_ten = video[0:10]
```

### ⚙️ Compilation API

```python
from mini_timelapse.compile import compile_video, LocalImageSource

with LocalImageSource("./my_photos/") as source:
    compile_video(
        source=source,
        output="timelapse.mkv",
        fps=30,
        quality=23,
        preset="medium",
        dry_run=False,
    )
```

### 🔄 Decompilation API

```python
from mini_timelapse.decompile import decompile_video

decompile_video(
    video_path="timelapse.mkv",
    output_dir="./extracted/",
    prefix="frame",
    quality=95,
)
```

## 📖 CLI Reference

### `timelapse-compile`

| Flag | Description | Default |
|---|---|---|
| `-i`, `--input` | Path to image directory, file, or glob pattern | *required* |
| `-o`, `--output` | Output video path (`.mkv` recommended) | *required* |
| `--fps` | Playback framerate | `30` |
| `-q`, `--quality` | H.264 CRF quality (0–51, lower = better) | `23` |
| `--preset` | x264 speed preset (`ultrafast` … `veryslow`) | `medium` |
| `-d`, `--dry-run` | Log what would be done without encoding | |
| `-v`, `--verbose` | Enable debug logging | |
| `--remote` | Use pyremotedata backend for SFTP access | |

### `timelapse-decompile`

| Flag | Description | Default |
|---|---|---|
| `-i`, `--input` | Path to compiled timelapse video | *required* |
| `-o`, `--output` | Output directory for extracted images | *required* |
| `--prefix` | Filename prefix (e.g. `frame` → `frame_000000.jpg`) | `frame` |
| `-q`, `--quality` | JPEG save quality (1–100) | `95` |
| `-v`, `--verbose` | Enable debug logging | |
| `--remote` | Upload extracted images to SFTP destination | |

## 🧠 Architecture Details

1. **Compile** reads each source image, extracts EXIF `DateTimeOriginal` and GPS coordinates, and encodes into an H.264 video stream inside a Matroska (`.mkv`) container. Files are ordered using a **Natural Sort** (lexical + numeric). Metadata is interleaved as a hidden ASS subtitle stream (JSON payload), allowing for single-pass encoding without a full-dataset pre-scan.
2. **Decompile** decodes each video frame back to a JPEG and re-embeds the interleaved metadata as proper EXIF tags using [piexif](https://github.com/hMatoba/Piexif).
3. **TimelapseVideo** opens the container with [PyAV](https://pyav.org/), parses the interleaved subtitle stream at init, and supports both sequential iteration (demux in order) and random access (seek to keyframe + decode forward).

> ⚠️ **Note**: The compile→decompile roundtrip is lossy because of H.264 and JPEG compression. Typical PSNR on natural images is 30–40 dB, which is visually indistinguishable.

## ⚡ Performance Matrix

On 6080×3420 images, compilation runs at ~7–8 frames/s. The bottleneck is JPEG decoding and RGB→YUV colorspace conversion of 20 MP images — H.264 encoding is only ~17% of the total time. Use `--preset fast` or `--preset ultrafast` to trade some quality for speed on the encoding side.

## 📄 License

This project is licensed under the **MIT License**.
