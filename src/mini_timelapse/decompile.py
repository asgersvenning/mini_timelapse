import argparse
import logging
import os
import sys
from datetime import datetime

import piexif
from PIL import Image

from mini_timelapse.reader import TimelapseVideo

try:
    from pyremotedata.implicit_mount import IOHandler
    REMOTE_AVAILABLE = True
except ImportError:
    REMOTE_AVAILABLE = False

logger = logging.getLogger("decompile_timelapse")


def _build_exif_bytes(meta: dict) -> bytes:
    """Reconstruct EXIF data from the stored metadata dict."""
    exif_dict = {"0th": {}, "Exif": {}, "GPS": {}}

    # DateTimeOriginal
    if "time" in meta:
        dt_str = meta["time"]
        # Try both common formats
        dt = None
        for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(dt_str, fmt)
                break
            except ValueError:
                continue
        if dt:
            exif_dt = dt.strftime("%Y:%m:%d %H:%M:%S")
            exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = exif_dt.encode()

    # GPS coordinates
    if "lat" in meta and "lon" in meta:
        lat = meta["lat"]
        lon = meta["lon"]

        def _to_dms_rational(value):
            """Convert a decimal degree to (degrees, minutes, seconds) as rationals."""
            value = abs(value)
            d = int(value)
            m = int((value - d) * 60)
            s = int(((value - d) * 60 - m) * 60 * 10000)
            return ((d, 1), (m, 1), (s, 10000))

        exif_dict["GPS"][piexif.GPSIFD.GPSLatitude] = _to_dms_rational(lat)
        exif_dict["GPS"][piexif.GPSIFD.GPSLatitudeRef] = b"N" if lat >= 0 else b"S"
        exif_dict["GPS"][piexif.GPSIFD.GPSLongitude] = _to_dms_rational(lon)
        exif_dict["GPS"][piexif.GPSIFD.GPSLongitudeRef] = b"E" if lon >= 0 else b"W"

    return piexif.dump(exif_dict)


def decompile_video(
    video_path: str,
    output_dir: str,
    prefix: str = "frame",
    quality: int = 95,
    remote: bool = False,
):
    """
    Extract all frames from a timelapse video back to individual JPEG images
    with their original EXIF metadata (timestamps, GPS) restored.

    Args:
        video_path: Path to the compiled .mkv timelapse video.
        output_dir: Directory or remote path to write the extracted images to.
        prefix: Filename prefix for extracted images (e.g. "frame" -> frame_0000.jpg).
        quality: JPEG save quality (1-100). Default: 95.
        remote: If True, upload the final directory to a remote SFTP source.
    """
    import tempfile
    import shutil

    if remote and not REMOTE_AVAILABLE:
        logger.error("pyremotedata is not installed. Remote output is unavailable. (pip install mini-timelapse[remote])")
        sys.exit(1)

    effective_output = output_dir
    temp_dir_obj = None

    if remote:
        temp_dir_obj = tempfile.TemporaryDirectory(prefix="timelapse_decompile_")
        effective_output = temp_dir_obj.name
        logger.info(f"Using temporary local buffer: {effective_output}")
    else:
        os.makedirs(output_dir, exist_ok=True)

    with TimelapseVideo(video_path) as video:
        n = len(video)
        pad = max(6, len(str(n - 1)))
        logger.info(f"Decompiling {n} frames from {video_path} -> {output_dir}/")

        from tqdm import tqdm
        for i, (frame_array, meta) in tqdm(enumerate(video), total=n, desc="Decompiling", unit="frame"):
            if meta and "filename" in meta:
                filename = meta["filename"]
            else:
                filename = f"{prefix}_{str(i).zfill(pad)}.jpg"
            out_path = os.path.join(effective_output, filename)

            img = Image.fromarray(frame_array)

            # Build and insert EXIF if we have metadata
            if meta:
                exif_bytes = _build_exif_bytes(meta)
                img.save(out_path, "JPEG", quality=quality, exif=exif_bytes)
            else:
                img.save(out_path, "JPEG", quality=quality)

        if remote:
            logger.info(f"Uploading {n} images to remote: {output_dir}...")
            with IOHandler() as io:
                # Use upload (mirror -R) for efficiency
                io.upload(effective_output, output_dir)
            
            temp_dir_obj.cleanup()
            logger.info(f"Done. Successfully uploaded images to {output_dir}/")
        else:
            logger.info(f"Done. Extracted {n} images to {output_dir}/")


def cli():
    parser = argparse.ArgumentParser(
        prog="decompile_timelapse",
        description="Extract frames from a compiled timelapse video back to individual images with EXIF metadata restored.",
    )
    parser.add_argument(
        "-i", "--input", type=str, required=True,
        help="Path to the compiled timelapse video (e.g., timelapse.mkv).",
    )
    parser.add_argument(
        "-o", "--output", type=str, required=True,
        help="Directory to write extracted images to.",
    )
    parser.add_argument(
        "--prefix", type=str, default="frame",
        help="Filename prefix for extracted images. Default: 'frame'.",
    )
    parser.add_argument(
        "-q", "--quality", type=int, default=95,
        help="JPEG save quality (1-100). Default: 95.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug logging.",
    )
    parser.add_argument(
        "--remote", action="store_true",
        help="Upload extracted images to a remote SFTP destination via pyremotedata.",
    )
    return parser.parse_args()


def main():
    args = cli()
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s: %(message)s")

    decompile_video(
        video_path=args.input,
        output_dir=args.output,
        prefix=args.prefix,
        quality=args.quality,
        remote=args.remote,
    )


if __name__ == "__main__":
    main()
