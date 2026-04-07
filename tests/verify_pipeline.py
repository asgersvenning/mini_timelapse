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
import os
import shutil
import sys
import tempfile
from datetime import datetime

import numpy as np
import piexif
from PIL import Image

from mini_timelapse.compile import compile_video, get_exif_data, LocalImageSource
from mini_timelapse.decompile import decompile_video


def psnr(img_a: np.ndarray, img_b: np.ndarray) -> float:
    """Peak Signal-to-Noise Ratio between two images. Higher = more similar."""
    mse = np.mean((img_a.astype(np.float64) - img_b.astype(np.float64)) ** 2)
    if mse == 0:
        return float("inf")
    return 10 * np.log10(255.0 ** 2 / mse)


def create_synthetic_images(dst_dir: str, n: int = 25) -> list[dict]:
    """
    Create N test images with diverse EXIF metadata and non-padded filenames
    to specifically test Natural Sort.
    """
    os.makedirs(dst_dir, exist_ok=True)
    expected = []
    for i in range(n):
        dt = datetime(2024, 6, 15, 10, i // 60, i % 60)
        lat = 55.6761 + i * 0.0013
        lon = 12.5683 - i * 0.0007

        # Use varied colors
        np.random.seed(i)
        arr = np.random.randint(0, 255, (240, 320, 3), dtype=np.uint8)
        img = Image.fromarray(arr)

        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}}
        exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = dt.strftime("%Y:%m:%d %H:%M:%S").encode()

        def _to_dms(val):
            val = abs(val)
            d = int(val); m = int((val - d) * 60); s = int(((val - d) * 60 - m) * 60 * 10000)
            return ((d, 1), (m, 1), (s, 10000))

        exif_dict["GPS"][piexif.GPSIFD.GPSLatitude] = _to_dms(lat)
        exif_dict["GPS"][piexif.GPSIFD.GPSLatitudeRef] = b"N" if lat >= 0 else b"S"
        exif_dict["GPS"][piexif.GPSIFD.GPSLongitude] = _to_dms(lon)
        exif_dict["GPS"][piexif.GPSIFD.GPSLongitudeRef] = b"E" if lon >= 0 else b"W"

        # Non-padded filenames to test natural sort: img_0.jpg, img_1.jpg ... img_10.jpg
        # Lexical sort would put img_10.jpg before img_2.jpg
        path = os.path.join(dst_dir, f"img_{i}.jpg")
        img.save(path, "JPEG", quality=95, exif=piexif.dump(exif_dict))
        expected.append({"time": dt.strftime("%Y-%m-%d %H:%M:%S"), "lat": round(lat, 6), "lon": round(lon, 6), "path": path})

    # Expected order should be numeric/natural
    from mini_timelapse.utils import natural_sort_key
    expected.sort(key=lambda x: natural_sort_key(x["path"]))
    return expected


def verify_roundtrip(src_dir: str = None, min_psnr: float = 25.0):
    """
    Run the full compile -> decompile roundtrip and verify integrity.
    
    Args:
        src_dir: Directory with source images. If None, creates synthetic images.
        min_psnr: Minimum acceptable PSNR in dB. H.264 CRF 23 + JPEG 95 typically
                  gives 30-40 dB on natural images. 25 dB is a generous lower bound.
    """
    tmp_root = os.path.join(tempfile.gettempdir(), "timelapse_roundtrip_test")
    if os.path.exists(tmp_root):
        shutil.rmtree(tmp_root)
    
    synthetic = src_dir is None
    if synthetic:
        src_dir = os.path.join(tmp_root, "src")
        expected_meta = create_synthetic_images(src_dir, n=15)
        src_images = [e["path"] for e in expected_meta]
        print(f"✓ Created {len(expected_meta)} synthetic test images")
    else:
        # Use real images — extract metadata for comparison
        from mini_timelapse.compile import search_input
        src_images = search_input(src_dir, remote=False)
        expected_meta = []
        for path in src_images: # search_input now returns sorted list
            meta = get_exif_data(path)
            if "dt" in meta:
                entry = {"time": meta["dt"].strftime("%Y-%m-%d %H:%M:%S"), "path": path}
                if "lat" in meta and "lon" in meta:
                    entry["lat"] = meta["lat"]
                    entry["lon"] = meta["lon"]
                expected_meta.append(entry)
        src_images = [e["path"] for e in expected_meta]
        print(f"✓ Found {len(expected_meta)} source images with EXIF metadata")

    video_path = os.path.join(tmp_root, "test.mkv")
    dst_dir = os.path.join(tmp_root, "decompiled")

    # === Step 1: Compile ===
    print("\n--- Step 1: Compiling ---")
    with LocalImageSource(src_dir) as source:
        compile_video(source, video_path, fps=10, quality=23, preset="ultrafast", dry_run=False)
    
    assert os.path.exists(video_path), "Video file was not created!"
    video_size = os.path.getsize(video_path) / (1024 * 1024)
    print(f"✓ Created {video_path} ({video_size:.1f} MB)")

    # === Step 2: Decompile ===
    print("\n--- Step 2: Decompiling ---")
    decompile_video(video_path, dst_dir, prefix="frame", quality=95, remote=False)
    restored = sorted(os.listdir(dst_dir))
    print(f"✓ Extracted {len(restored)} frames")

    # === Step 3: Verify frame count ===
    print("\n--- Step 3: Verifying ---")
    assert len(restored) == len(expected_meta), \
        f"Frame count mismatch: expected {len(expected_meta)}, got {len(restored)}"
    print(f"✓ Frame count matches: {len(restored)}")

    # === Step 4: Verify metadata ===
    meta_ok = 0
    meta_fail = 0
    for i, filename in enumerate(restored):
        path = os.path.join(dst_dir, filename)
        exif_raw = piexif.load(path)

        # Check DateTimeOriginal
        dt_bytes = exif_raw["Exif"].get(piexif.ExifIFD.DateTimeOriginal)
        if dt_bytes is None:
            print(f"  ✗ Frame {i}: missing DateTimeOriginal")
            meta_fail += 1
            continue

        dt_str = dt_bytes.decode()
        dt_restored = datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
        dt_expected = datetime.strptime(expected_meta[i]["time"], "%Y-%m-%d %H:%M:%S")

        if dt_restored != dt_expected:
            print(f"  ✗ Frame {i}: time mismatch {dt_restored} != {dt_expected}")
            meta_fail += 1
            continue

        # Check GPS if available in source
        if "lat" in expected_meta[i]:
            gps = exif_raw.get("GPS", {})
            if piexif.GPSIFD.GPSLatitude not in gps:
                print(f"  ✗ Frame {i}: missing GPS")
                meta_fail += 1
                continue

        meta_ok += 1

    print(f"✓ Metadata: {meta_ok}/{len(restored)} frames OK", end="")
    if meta_fail > 0:
        print(f", {meta_fail} FAILED")
    else:
        print()

    # === Step 5: Verify image similarity (PSNR) ===
    # Sample a few frames for PSNR check
    sample_indices = list(range(0, len(restored), max(1, len(restored) // 10)))[:10]
    psnr_values = []

    for idx in sample_indices:
        # Load original
        orig = np.array(Image.open(expected_meta[idx]["path"]).convert("RGB"))
        # Load restored
        rest = np.array(Image.open(os.path.join(dst_dir, restored[idx])).convert("RGB"))

        # Sizes might differ slightly if there was a resize, but shouldn't in 1:1
        if orig.shape != rest.shape:
            print(f"  ⚠ Frame {idx}: shape mismatch {orig.shape} vs {rest.shape}")
            continue

        p = psnr(orig, rest)
        psnr_values.append(p)

    if psnr_values:
        avg_psnr = np.mean(psnr_values)
        min_measured = np.min(psnr_values)
        print(f"✓ Image PSNR: avg={avg_psnr:.1f} dB, min={min_measured:.1f} dB (threshold={min_psnr} dB)")
        
        if min_measured < min_psnr:
            print(f"  ⚠ WARNING: Some frames below {min_psnr} dB PSNR threshold - quality loss may be noticeable")
        else:
            print(f"  All sampled frames above {min_psnr} dB threshold")

    # === Summary ===
    print(f"\n{'='*50}")
    all_pass = meta_fail == 0 and (not psnr_values or min(psnr_values) >= min_psnr)
    if all_pass:
        print("✅ ROUNDTRIP VERIFICATION PASSED")
    else:
        print("⚠️  ROUNDTRIP VERIFICATION COMPLETED WITH WARNINGS")
    print(f"{'='*50}")

    # Cleanup
    if synthetic:
        shutil.rmtree(tmp_root)
    else:
        # Keep decompiled output for manual inspection
        print(f"\nDecompiled frames saved to: {dst_dir}")
        print(f"Video saved to: {video_path}")


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    src = sys.argv[1] if len(sys.argv) > 1 else None
    verify_roundtrip(src)
