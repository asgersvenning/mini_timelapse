# mini-timelapse

[![Python version](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://github.com/asgersvenning/mini_timelapse/actions/workflows/test.yml/badge.svg)](https://github.com/asgersvenning/mini_timelapse/actions)

---

**mini-timelapse** is a Python toolkit for compiling timestamped, geolocated images into compact `.mkv` video containers, and extracting them back out with metadata intact. 

It maps each source image to exactly one video frame (1:1), embeds the original EXIF timestamps and GPS coordinates as a strictly interleaved data stream, and provides a Python API for temporal and random-access frame retrieval.

## Features

* **Compile**: Converts a directory of JPEGs/PNGs into a single `.mkv` file with interleaved metadata.
* **Decompile**: Extracts `.mkv` frames back to JPEGs, restoring EXIF timestamps and GPS data.
* **Lossless Color**: Encodes using the `yuv444p` color space to preserve 100% of the original RGB chroma data.
* **Temporal Search**: Query frames by their actual real-world capture time.
* **Remote IO**: Supports direct compilation from and decompilation to SFTP/ERDA storage via [pyremotedata](https://github.com/asgersvenning/pyremotedata).
* **Standalone**: Built on [PyAV](https://pyav.org/) — no system FFmpeg installation required.

## Installation

Requires **Python ≥ 3.12**.

Using [uv](https://docs.astral.sh/uv/) (recommended):
```bash
uv pip install -e .
```

Using standard pip:
```bash
pip install -e .
```

## Quick Start

### 1. Compile images to video
```bash
timelapse-compile -i ./my_photos/ -o timelapse.mkv
```
Frames are sorted chronologically (using natural sort for filenames) and encoded 1:1 into the container. 

### 2. Decompile video back to images
```bash
timelapse-decompile -i timelapse.mkv -o ./extracted/
```
Each frame is extracted as a JPEG with its original EXIF tags restored.

### 3. Remote Storage
If `pyremotedata` is installed, you can interface directly with SFTP sources:
```bash
export PYREMOTEDATA_REMOTE_USERNAME="myuser"
export PYREMOTEDATA_REMOTE_HOSTNAME="io.erda.dk"

# Compile from remote
timelapse-compile -i /remote/path/to/images/ -o timelapse.mkv --remote

# Decompile to remote
timelapse-decompile -i timelapse.mkv -o /remote/output/folder/ --remote
```

## Python API

### Reading and Querying Data
```python
from mini_timelapse.reader import TimelapseVideo

with TimelapseVideo("timelapse.mkv") as video:
    print(f"Loaded {len(video)} frames")

    # Temporal Search (New): Get frame closest to a real-world datetime
    frame, meta = video.get_frame_by_time("2024-06-15 12:00:00")
    print(f"Actual capture time: {meta.get('time')}")

    # Random access by index
    frame, meta = video[42]
    print(meta.get("lat"), meta.get("lon"))  # 55.6761, 12.5683
    print(frame.shape)                       # (height, width, 3)

    # Sequential iteration
    for frame, meta in video:
        process(frame, meta)

    # Slicing
    first_ten = video[0:10]
```

### Compilation
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

### Decompilation
```python
from mini_timelapse.decompile import decompile_video

decompile_video(
    video_path="timelapse.mkv",
    output_dir="./extracted/",
    prefix="frame",
    quality=95,
)
```

## CLI Reference

### `timelapse-compile`

| Flag | Description | Default |
|---|---|---|
| `-i`, `--input` | Path to image directory, file, or glob pattern | *required* |
| `-o`, `--output` | Output video path (`.mkv` strictly recommended) | *required* |
| `--fps` | Playback framerate | `30` |
| `-q`, `--quality` | H.264 CRF quality (0–51, lower is better) | `23` |
| `--preset` | x264 speed preset (`ultrafast` … `veryslow`) | `medium` |
| `-d`, `--dry-run` | Log actions without encoding | |
| `-v`, `--verbose` | Enable debug logging | |
| `--remote` | Use pyremotedata backend for SFTP access | |

### `timelapse-decompile`

| Flag | Description | Default |
|---|---|---|
| `-i`, `--input` | Path to compiled timelapse video | *required* |
| `-o`, `--output` | Output directory for extracted images | *required* |
| `--prefix` | Filename prefix (e.g., `frame` → `frame_000000.jpg`) | `frame` |
| `-q`, `--quality` | JPEG save quality (1–100) | `95` |
| `-v`, `--verbose` | Enable debug logging | |
| `--remote` | Upload extracted images to SFTP destination | |

## Architecture Details

1. **Format:** Uses the Matroska (`.mkv`) container paired with H.264 video.
2. **Color Space:** Enforces `yuv444p` and `bt709` tagging. Unlike standard video profiles (`yuv420p`), this prevents chroma subsampling, ensuring exact RGB data reconstruction.
3. **Data Interleaving:** Extracted EXIF data is serialized as JSON and strictly interleaved into a standard SubRip (`srt`) subtitle stream. This aligns metadata packets immediately adjacent to their corresponding video packets, preventing decoder buffer overflows during random access.
4. **Time Alignment:** The MKV timeline is synthetic (Constant Frame Rate) to ensure broad media player compatibility. True temporal context is preserved in the interleaved JSON payload, allowing the reader to map variable real-world time gaps onto the sequential video track via binary search.

## License

This project is licensed under the **MIT License**.