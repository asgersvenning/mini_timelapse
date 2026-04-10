import argparse
import logging
import os
import tempfile
from datetime import datetime

import piexif
from PIL import Image
from tqdm import tqdm

from mini_timelapse.reader import TimelapseVideo

try:
    from pyremotedata.implicit_mount import IOHandler

    REMOTE_AVAILABLE = True
except ImportError:
    REMOTE_AVAILABLE = False

logger = logging.getLogger("decompile_timelapse")


def _build_exif_bytes(meta: dict, master_exif: bytes = None) -> bytes:
    """Reconstruct EXIF data, preserving the original camera template if available."""
    exif_dict = None

    # Load the master EXIF template if we have it
    if master_exif:
        try:
            exif_dict = piexif.load(master_exif)
            # Remove thumbnail to avoid saving Frame 0's thumbnail on every single image
            exif_dict.pop("thumbnail", None)
        except Exception as e:
            logger.warning(f"Failed to load master EXIF template: {e}. Falling back to blank EXIF.")

    # Fallback to a blank slate if no master EXIF exists or if parsing failed
    if not exif_dict:
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "Interop": {}}

    # Patch DateTimeOriginal
    if "time" in meta:
        dt_str = meta["time"]
        dt = None
        for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(dt_str, fmt)
                break
            except ValueError:
                continue
        if dt:
            exif_dt = dt.strftime("%Y:%m:%d %H:%M:%S").encode("utf-8")
            # Update all standard EXIF time fields to ensure consistency
            exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = exif_dt
            exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = exif_dt
            exif_dict["0th"][piexif.ImageIFD.DateTime] = exif_dt

    # Patch GPS coordinates (Using your existing Rational conversion logic)
    if "lat" in meta and "lon" in meta:
        lat = meta["lat"]
        lon = meta["lon"]

        def _to_dms_rational(value):
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
    output_dir: str | None = None,
    prefix: str = "frame",
    quality: int = 95,
    remote: bool = False,
    sharelink_id: int | None = None,
):
    if remote and not REMOTE_AVAILABLE:
        logger.error("pyremotedata is not installed. Remote output is unavailable.")
        raise ImportError("pyremotedata is not installed. Remote output is unavailable.")

    if output_dir is None:
        output_dir = os.path.splitext(video_path)[0]

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

        master_exif_bytes = getattr(video, "master_exif", None)
        if master_exif_bytes:
            logger.info("Found master EXIF template. Decompiled images will retain original camera metadata.")
        else:
            logger.info("No master EXIF template found. Falling back to basic timestamp reconstruction.")

        logger.info(f"Decompiling {n} frames from {video_path} -> {output_dir}/")

        for i, (frame_array, meta) in tqdm(enumerate(video), total=n, desc="Decompiling", unit="frame"):
            if meta and "filename" in meta:
                filename = meta["filename"]
            else:
                filename = f"{prefix}_{str(i).zfill(pad)}.jpg"

            out_path = os.path.join(effective_output, filename)

            # Robust Resume: Skip if fully written
            if os.path.exists(out_path):
                logger.debug(f"File {out_path} already exists. Skipping.")
                continue

            img = Image.fromarray(frame_array)

            # ATOMIC WRITE: Save to .tmp, then rename.
            tmp_path = out_path + ".tmp"
            try:
                if meta:
                    exif_bytes = _build_exif_bytes(meta, master_exif=master_exif_bytes)
                    img.save(tmp_path, "JPEG", quality=quality, exif=exif_bytes)
                else:
                    img.save(tmp_path, "JPEG", quality=quality)

                os.replace(tmp_path, out_path)
            except Exception:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise

        if remote:
            logger.info(f"Uploading {n} images to remote: {output_dir}...")
            with IOHandler(user=sharelink_id, password=sharelink_id) as io:
                io.upload(effective_output, output_dir)

            temp_dir_obj.cleanup()
            logger.info(f"Done. Uploaded images to {output_dir}/")
        else:
            logger.info(f"Done. Extracted {n} images to {output_dir}/")


def cli():
    parser = argparse.ArgumentParser(
        prog="decompile_timelapse",
        description="Extract frames from a compiled timelapse video back to individual images with EXIF metadata restored.",
    )
    parser.add_argument(
        "-i",
        "--input",
        type=str,
        required=True,
        help="Path to the compiled timelapse video (e.g., timelapse.mkv).",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        required=False,
        help="Directory to write extracted images to.",
    )
    parser.add_argument(
        "--prefix",
        type=str,
        default="frame",
        help="Filename prefix for extracted images. Default: 'frame'.",
    )
    parser.add_argument(
        "-q",
        "--quality",
        type=int,
        default=95,
        help="JPEG save quality (1-100). Default: 95.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="Upload extracted images to a remote SFTP destination via pyremotedata.",
    )
    parser.add_argument(
        "--sharelink_id",
        type=int,
        required=False,
        help="Sharelink ID for ERDA. "
        "If provided, pyremotedata will attempt an anonymous login "
        "with the given sharelink id as both username and password.",
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
        sharelink_id=args.sharelink_id,
    )


if __name__ == "__main__":
    main()
