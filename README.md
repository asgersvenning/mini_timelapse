# mini-timelapse

[![Python version](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://github.com/asgersvenning/mini_timelapse/actions/workflows/ci.yml/badge.svg)](https://github.com/asgersvenning/mini_timelapse/actions)
[![codecov](https://codecov.io/github/asgersvenning/mini_timelapse/graph/badge.svg)](https://codecov.io/github/asgersvenning/mini_timelapse)
[![Pyinstrument Profile](https://img.shields.io/badge/Pyinstrument-Profile-3776AB?logo=python&logoColor=white)](https://asgersvenning.github.io/mini_timelapse/main/)

---

**mini-timelapse** is a Python toolkit for compiling timestamped, geolocated images into compact `.mkv` video containers, and extracting them back out with metadata intact.

It maps each source image to exactly one video frame (1:1), embeds the original EXIF timestamps and GPS coordinates as a strictly interleaved data stream, and provides a Python API for temporal and random-access frame retrieval.

When reconstructing the frames, the original EXIF data is attempted to be restored under the assumption that only the timestamp and GPS coordinates change between frames (other EXIF tags remain constant).

> [!TIP]
> To view the video with the embedded metadata, I recommend [MPC-HC](https://github.com/clsid2/mpc-hc). The timestamps are embedded as a subtitle track and can be toggled on and off with the `s` key.

## Table of Contents

- [Features](#features)
- [Data Recovery Philosophy](#data-recovery-philosophy)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Python API](#python-api)
- [CLI Reference](#cli-reference)
- [Architecture Details](#architecture-details)

## Features

- **Compile**: Converts a directory of JPEGs/PNGs into a single `.mkv` file with interleaved metadata.

- **Decompile**: Extracts `.mkv` frames back to JPEGs, restoring EXIF timestamps and GPS data.
- **Lossless Color**: Encodes using the `yuv444p` color space to preserve 100% of the original RGB chroma data.
- **Temporal Search**: Query frames by their actual real-world capture time.
- **Remote IO**: Supports direct compilation from and decompilation to SFTP/ERDA storage via [pyremotedata](https://github.com/asgersvenning/pyremotedata).
- **Repair**: Fixes damaged, truncated, or out-of-order timelapse videos by re-encoding them based on capture-time metadata.

## Data Recovery Philosophy

The recommended way to read or extract the compiled data is to use the `mini-timelapse` Python module or the `timelapse-decompile` CLI. However, to ensure your data is never locked behind a proprietary tool, the metadata is stored using standard Matroska structures. **You do not strictly need this Python package to recover your data.** Using standard `ffmpeg`, you can extract the raw data manually:

```bash
# 1. Extract the sovereign JSON metadata array
ffmpeg -dump_attachment:t:0 metadata.json -i timelapse.mkv -y

# 2. Extract the master EXIF binary template
ffmpeg -dump_attachment:t:1 master.exif -i timelapse.mkv -y

# 3. Extract the visual HUD to a standard SRT file
ffmpeg -i timelapse.mkv -map 0:s:0 -f srt extracted_subtitles.srt
```

## Installation

### Prerequisites

- **Python 3.12+**
- **FFmpeg**: Required for embedding and extracting Matroska attachments and metadata. (`sudo apt install ffmpeg` / `brew install ffmpeg` / `winget install ffmpeg`)
- **[uv](https://docs.astral.sh/uv/)**: Recommended for installation and dependency management.

### Option A: End-User (CLI Only)

If you just want to use the command-line tools without cluttering your system environments, use `uv tool`. This installs the CLI commands globally in an isolated environment.

```bash
# Install directly from GitHub
uv tool install git+https://github.com/asgersvenning/mini_timelapse.git

# You can now run the commands from anywhere:
timelapse-compile --help
```

### Option B: As a Python Dependency

If you are building a Python script and want to use the `mini_timelapse` module in your own project:

```bash
uv add git+https://github.com/asgersvenning/mini_timelapse.git
```

### Option C: Local Development (Contributors)

If you want to modify the source code, use `uv sync` to set up a robust, locked development environment.

```bash
git clone git@github.com:asgersvenning/mini_timelapse.git
cd mini_timelapse
uv sync --all-extras

# Run local CLI changes via the virtual environment
uv run timelapse-compile --help
```

## Quick Start

### 1. Compile images to video

Frames are *(attempted to be)* sorted chronologically and encoded 1:1 into the container.

```bash
timelapse-compile -i ./my_photos/ -o timelapse.mkv
```

*(Note: If developing locally, prefix these with `uv run`)*

### 2. Decompile video back to images

Each frame is extracted as a JPEG with its original EXIF tags restored.

```bash
timelapse-decompile -i timelapse.mkv -o ./extracted/
```

### 3. Remote Storage

If installed with remote capabilities, you can interface directly with SFTP sources:

```bash
# Compile from remote
timelapse-compile -i /remote/path/to/images/ -o timelapse.mkv --remote
```

> [!NOTE]
> Using `--remote` requires installing with the optional dependencies "remote", which can be done via:
> ```bash
> uv sync --extra remote
> ```

### 4. Repair Damaged Videos

If a video was muxed out of order, or some frames are corrupted/missing:

```bash
# Fix out-of-order frames
timelapse-repair -i damaged.mkv -o repaired.mkv

# Recover frames from a truncated file
timelapse-repair -i truncated.mkv -o recovered.mkv --skip-corrupted
```

## Python API

### Reading and Querying Data

```python
from mini_timelapse.reader import TimelapseVideo

with TimelapseVideo("timelapse.mkv") as video:
    print(f"Loaded {len(video)} frames")

    # Temporal Search: Get frame closest to a real-world datetime
    frame, meta, diff = video.get_frame_by_time("2024-06-15 12:00:00", max_diff=3600)
    print(f"Closest match is {diff:.1f}s away. Capture time: {meta.get('time')}")

    # Random access by index
    frame, meta = video[42]
    print(meta.get("lat"), meta.get("lon"))

    # Slicing
    first_ten = video[0:10]
```

### Compilation & Decompilation

```python
from mini_timelapse.compile import compile_video, LocalImageSource
from mini_timelapse.decompile import decompile_video

# Compilation
src_spec = LocalImageSource.SourceSpec(src="./my_photos/", recursive=True, n_max=100)
with LocalImageSource(spec=src_spec) as source:
    compile_video(source=source, output="timelapse.mkv", fps=30)

# Decompilation
decompile_video(video_path="timelapse.mkv", output_dir="./extracted/")
```

## CLI Reference

### `timelapse-compile`

| Flag              | Description                                     | Default    |
| :---------------- | :---------------------------------------------- | :--------- |
| `-i`, `--input`   | Path to image directory, file, or glob pattern  | *required* |
| `-o`, `--output`  | Output video path (`.mkv` strictly recommended) | derived    |
| `--fps`           | Playback framerate                              | `30`       |
| `-q`, `--quality` | H.264 CRF quality (0–51, lower is better)       | `23`       |
| `--preset`        | x264 speed preset                               | `medium`   |
| `--remote`        | Use [`pyremotedata`](https://github.com/asgersvenning/pyremotedata) backend for SFTP access | |

### `timelapse-decompile`

| Flag             | Description                           | Default    |
| :--------------- | :------------------------------------ | :--------- |
| `-i`, `--input`  | Path to compiled timelapse video      | *required* |
| `-o`, `--output` | Output directory for extracted images | derived    |
| `--prefix`       | Filename prefix                       | `frame`    |

### `timelapse-repair`

| Flag               | Description                                           | Default    |
| :----------------- | :---------------------------------------------------- | :--------- |
| `-i`, `--input`    | Path to damaged timelapse video                       | *required* |
| `-o`, `--output`   | Path for the repaired output video                    | derived    |
| `--skip-corrupted` | Skip frames that cannot be decoded                    |            |
| `--infer-metadata` | Deduce timestamps from video creation time if missing |            |

> [!TIP]
> Run any CLI command with `--help` for the full list of arguments.

## Architecture Details

1. **Format:** Uses the Matroska (`.mkv`) container paired with H.264 video.
2. **Color Space:** Enforces `yuv444p` and `bt709` tagging. Unlike standard video profiles (`yuv420p`), this prevents chroma subsampling, ensuring exact RGB data reconstruction.
3. **Data Interleaving:** Extracted EXIF data is serialized as JSON and strictly interleaved into a standard SubRip (`srt`) subtitle stream. This aligns metadata packets adjacent to their corresponding video packets, preventing decoder buffer overflows during random access.
4. **Time Alignment:** The MKV timeline is synthetic (Constant Frame Rate) to ensure broad media player compatibility. True temporal context is preserved in the interleaved JSON payload, allowing the reader to map variable real-world time gaps onto the sequential video track via binary search.

## License

This project is licensed under the **MIT License**.
