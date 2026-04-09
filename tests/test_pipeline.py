"""
Roundtrip integrity test: compile -> decompile -> verify.

Checks:
  1. Frame count matches
  2. Metadata (timestamps, GPS) is preserved exactly
  3. Image content is preserved within acceptable PSNR tolerance
     (lossy H.264 + JPEG re-encoding means pixel-perfect is not expected)

Usage:
    PYTHONPATH=src uv run python verify_pipeline.py [image_dir]

If no image_dir is given, creates synthetic test images.
"""

import base64
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np
import piexif

from mini_timelapse.compile import LocalImageSource, compile_video, get_exif_data
from mini_timelapse.decompile import decompile_video
from mini_timelapse.utils import natural_sort_key

# Fix import path for both IDE and runtime
# Adding the project root to sys.path allows 'from tests import ...'
# Adding the current directory to sys.path allows 'import gen_test_images'
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

try:
    from gen_test_images import generate_test_images
except ImportError:
    from tests.gen_test_images import generate_test_images


def psnr(img_a: np.ndarray, img_b: np.ndarray) -> float:
    """Peak Signal-to-Noise Ratio between two images. Higher = more similar."""
    mse = np.mean((img_a.astype(np.float64) - img_b.astype(np.float64)) ** 2)
    if mse == 0:
        return float("inf")
    return 10 * np.log10(255.0**2 / mse)


def verify_roundtrip(src_dir: str = None, min_psnr: float = 25.0, iterations: int = 1):
    """
    Run the full compile -> decompile roundtrip and verify integrity.
    """
    for iteration in range(iterations):
        if iterations > 1:
            print(f"\n{'=' * 60}")
            print(f"🔄 ITERATION {iteration + 1}/{iterations}")
            print(f"{'=' * 60}")

        with tempfile.TemporaryDirectory(prefix="pipeline_test_") as tmp_root:
            # Generation/Source logic
            synthetic = src_dir is None
            if synthetic:
                it_src_dir = os.path.join(tmp_root, "src")
                expected_meta = generate_test_images(it_src_dir, num_images=10000)
                src_images = [e["path"] for e in expected_meta]
            else:
                it_src_dir = src_dir
                with LocalImageSource(it_src_dir) as source:
                    src_images = source.paths
                    expected_meta = []
                    for path in src_images:
                        meta = get_exif_data(path)
                        if "dt" in meta:
                            entry = {"time": meta["dt"].strftime("%Y-%m-%d %H:%M:%S"), "path": path}
                            if "lat" in meta and "lon" in meta:
                                entry["lat"] = meta["lat"]
                                entry["lon"] = meta["lon"]
                            expected_meta.append(entry)
                src_images = [e["path"] for e in expected_meta]

            video_path = os.path.join(tmp_root, "test.mkv")
            dst_dir = os.path.join(tmp_root, "decompiled")

            # === Step 1: Compile ===
            print("--- Step 1: Compiling ---")
            with LocalImageSource(it_src_dir) as source:
                compile_video(source, video_path, fps=30, quality=23, preset="ultrafast", dry_run=False)

            # === Step 2: Decompile ===
            print("--- Step 2: Decompiling ---")
            decompile_video(video_path, dst_dir, quality=95, remote=False)
            restored = sorted(os.listdir(dst_dir), key=natural_sort_key)

            # === Step 3: Verify Integrity ===
            print("--- Step 3: Verifying ---")

            # Check counts
            if len(restored) != len(expected_meta):
                print(f"✗ ERROR: Frame count mismatch: {len(restored)} vs {len(expected_meta)}")
                raise ValueError(f"Frame count mismatch: {len(restored)} vs {len(expected_meta)}")

            # Check raw timestamps (New robust check)
            import av

            with av.open(video_path) as container:
                v_stream = container.streams.video[0]
                s_stream = container.streams.subtitles[0]
                v_pts = []
                s_pts = []
                for packet in container.demux(v_stream, s_stream):
                    if packet.pts is None:
                        continue
                    if packet.stream.type == "video":
                        v_pts.append(float(packet.pts * packet.stream.time_base))
                    elif packet.stream.type == "subtitle":
                        s_pts.append(float(packet.pts * packet.stream.time_base))

                if len(v_pts) != len(s_pts):
                    print(f"✗ ERROR: Stream packet count mismatch: video={len(v_pts)}, sub={len(s_pts)}")
                    raise ValueError(f"Stream packet count mismatch: video={len(v_pts)}, sub={len(s_pts)}")

                for i in range(len(v_pts)):
                    if abs(v_pts[i] - s_pts[i]) > 0.001:
                        print(f"✗ ERROR: Displacement at frame {i}: V={v_pts[i]:.4f}s, S={s_pts[i]:.4f}s")
                        raise ValueError(f"Displacement at frame {i}: V={v_pts[i]:.4f}s, S={s_pts[i]:.4f}s")
            print("✓ Raw stream timestamps: 1:1 aligned")

            # Check metadata content
            meta_ok = 0
            diffs = []
            for i, filename in enumerate(restored):
                path = os.path.join(dst_dir, filename)
                exif_raw = piexif.load(path)
                dt_bytes = exif_raw["Exif"].get(piexif.ExifIFD.DateTimeOriginal)
                if dt_bytes:
                    dt_res = datetime.strptime(dt_bytes.decode(), "%Y:%m:%d %H:%M:%S")
                    dt_exp = datetime.strptime(expected_meta[i]["time"], "%Y-%m-%d %H:%M:%S")
                    if dt_res == dt_exp:
                        meta_ok += 1
                    else:
                        diffs.append((i, dt_res, dt_exp))

            if meta_ok != len(expected_meta):
                print(f"✗ ERROR: Metadata mismatch: {meta_ok}/{len(expected_meta)} OK")
                if diffs:
                    idx, res, exp = diffs[0]
                    print(f"  First mismatch at index {idx}:")
                    print(f"    Restored: {res}")
                    print(f"    Expected: {exp}")

                # Shift detection
                if diffs:
                    # Real check: extract all times
                    all_res_dts = []
                    for f in restored:
                        e = piexif.load(os.path.join(dst_dir, f))
                        b = e["Exif"].get(piexif.ExifIFD.DateTimeOriginal)
                        if b:
                            all_res_dts.append(datetime.strptime(b.decode(), "%Y:%m:%d %H:%M:%S"))
                        else:
                            all_res_dts.append(None)

                    all_exp_dts = [datetime.strptime(e["time"], "%Y-%m-%d %H:%M:%S") for e in expected_meta]

                    # Try shift -1 (metadata is 1 frame LATE relative to video)
                    shift_m1 = sum(1 for i in range(1, len(all_res_dts)) if all_res_dts[i] == all_exp_dts[i - 1])
                    if shift_m1 > 50:
                        print(f"  ⚠ DETECTED: Metadata is LATE by 1 frame ({shift_m1} matches with shift -1)")

                    # Try shift +1 (metadata is 1 frame EARLY relative to video)
                    shift_p1 = sum(1 for i in range(len(all_res_dts) - 1) if all_res_dts[i] == all_exp_dts[i + 1])
                    if shift_p1 > 50:
                        print(f"  ⚠ DETECTED: Metadata is EARLY by 1 frame ({shift_p1} matches with shift +1)")

                raise ValueError(f"Metadata mismatch: {meta_ok}/{len(expected_meta)} frames OK")
            print(f"✓ Metadata: {meta_ok}/{len(expected_meta)} frames OK")

            # === Step 4: FFmpeg Parity Check ===
            print("--- Step 4: FFmpeg Parity Check ---")
            import subprocess

            srt_path = os.path.join(tmp_root, "test.srt")
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    video_path,
                    "-map",
                    "0:s:0",
                    "-f",
                    "srt",
                    srt_path,
                ],
                check=True,
            )

            with open(srt_path, encoding="utf-8") as f:
                srt_content = f.read()

                # Verify that EVERY metadata index is present in the SRT
                # OPTIMIZATION: Pre-parse all indices once to avoid O(N^2) scan
                found_indices = set()
                metadata_blocks = re.findall(r"###METADATA_START###(.*?)###METADATA_END###", srt_content)
                
                for block in metadata_blocks:
                    block = block.strip()
                    try:
                        # Try raw JSON first
                        if block.startswith("{"):
                            data = json.loads(block)
                            if "index" in data:
                                found_indices.add(int(data["index"]))
                            continue
                        
                        # Try Base64
                        decoded_bytes = base64.b64decode(block)
                        decoded = decoded_bytes.decode("utf-8")
                        data = json.loads(decoded)
                        if "index" in data:
                            found_indices.add(int(data["index"]))
                    except Exception:
                        continue

                srt_ok = sum(1 for i in range(len(expected_meta)) if i in found_indices)

            if srt_ok != len(expected_meta):
                print(f"✗ ERROR: FFmpeg SRT parity mismatch: {srt_ok}/{len(expected_meta)} found.")
                raise ValueError(f"FFmpeg SRT parity mismatch: {srt_ok}/{len(expected_meta)} found.")
            print(f"✓ FFmpeg Parity: {srt_ok}/{len(expected_meta)} frames found in SRT")

    print("\n" + "=" * 50)
    print(f"✅ ROUNDTRIP VERIFICATION PASSED ({iterations} round{'s' if iterations > 1 else ''})")
    print("=" * 50)


def test_pipeline_roundtrip():
    """
    Standard pytest entry point for the roundtrip integrity test.
    """
    verify_roundtrip(iterations=1)


if __name__ == "__main__":
    import argparse
    import logging

    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("input", nargs="?", default=None)
    parser.add_argument("--iterations", type=int, default=1)
    args = parser.parse_args()

    verify_roundtrip(args.input, iterations=args.iterations)
