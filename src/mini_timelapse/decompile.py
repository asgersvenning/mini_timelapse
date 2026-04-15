import argparse
import logging
import os
import queue
import tempfile
import threading

import piexif
from PIL import Image
from tqdm import tqdm

from mini_timelapse.reader import TimelapseVideo
from mini_timelapse.utils import normalize_cli_args, parse_time

try:
    from pyremotedata.implicit_mount import IOHandler

    REMOTE_AVAILABLE = True
except ImportError:
    REMOTE_AVAILABLE = False

logger = logging.getLogger("decompile_timelapse")


def _build_exif_bytes(meta: dict, master_exif: bytes = None) -> bytes:
    # ... (Keep this function exactly as it was, no changes needed) ...
    exif_dict = None

    if master_exif:
        try:
            exif_dict = piexif.load(master_exif)
            exif_dict.pop("thumbnail", None)
        except Exception as e:
            logger.warning(f"Failed to load master EXIF template: {e}. Falling back to blank EXIF.")

    if not exif_dict:
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "Interop": {}}

    if "time" in meta:
        dt_str = meta["time"]
        dt = parse_time(dt_str)
        exif_dt = dt.strftime("%Y:%m:%d %H:%M:%S").encode("utf-8")
        exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = exif_dt
        exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = exif_dt
        exif_dict["0th"][piexif.ImageIFD.DateTime] = exif_dt

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


def _image_writer_worker(write_queue: queue.Queue, pbar: tqdm, errors: list):
    """Background thread function that drains the queue and saves images."""
    while True:
        item = write_queue.get()
        if item is None:  # Sentinel value to kill the thread
            write_queue.task_done()
            break

        frame_array, meta, out_path, master_exif_bytes, quality = item

        try:
            # Robust Resume: Skip if fully written
            if os.path.exists(out_path):
                # Using a low log level inside tight loops keeps stdout clean
                pass
            else:
                img = Image.fromarray(frame_array)
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
        except Exception as e:
            errors.append(e)
        finally:
            # Safely update the progress bar from the background thread
            pbar.update(1)
            write_queue.task_done()


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

        # Concurrency Setup
        num_workers = min(8, (os.cpu_count() or 4) + 4)  # Optimize for I/O bounds
        write_queue = queue.Queue(maxsize=16)  # Cap memory usage
        errors = []
        workers = []

        with tqdm(total=n, desc="Decompiling", unit="frame") as pbar:
            # Spin up the background writers
            for _ in range(num_workers):
                t = threading.Thread(target=_image_writer_worker, args=(write_queue, pbar, errors), daemon=True)
                t.start()
                workers.append(t)

            # Producer loop (Main Thread)
            for i, (frame_array, meta) in enumerate(video):
                if errors:
                    logger.error("A background write failed. Aborting extraction.")
                    break

                if meta and "filename" in meta:
                    filename = meta["filename"]
                else:
                    filename = f"{prefix}_{str(i).zfill(pad)}.jpg"

                out_path = os.path.join(effective_output, filename)

                # This will block if the queue is full, naturally throttling the PyAV loop
                write_queue.put((frame_array, meta, out_path, master_exif_bytes, quality))

            # Send the kill signals to the workers
            for _ in range(num_workers):
                write_queue.put(None)

            # Block the main thread until all queue items are flushed to disk
            for t in workers:
                t.join()

        # Re-raise the first background error so the script crashes appropriately
        if errors:
            raise RuntimeError(f"Decompilation failed: {errors[0]}") from errors[0]

        if remote:
            logger.info(f"Uploading {n} images to remote: {output_dir}...")
            with IOHandler(user=sharelink_id, password=sharelink_id) as io:
                io.upload(effective_output, output_dir)

            temp_dir_obj.cleanup()
            logger.info(f"Done. Uploaded images to {output_dir}/")
        else:
            logger.info(f"Done. Extracted {n} images to {output_dir}/")


def cli():
    import sys

    parser = argparse.ArgumentParser(
        prog="decompile_timelapse",
        description="Extract frames from a compiled timelapse video back to individual images with EXIF metadata restored.",
    )
    parser.add_argument("-i", "--input", type=str, required=True, help="Path to the compiled timelapse video (e.g., timelapse.mkv).")
    parser.add_argument("-o", "--output", type=str, required=False, help="Directory to write extracted images to.")
    parser.add_argument("--prefix", type=str, default="frame", help="Filename prefix for extracted images. Default: 'frame'.")
    parser.add_argument("-q", "--quality", type=int, default=95, help="JPEG save quality (1-100). Default: 95.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    parser.add_argument("--remote", action="store_true", help="Upload extracted images to a remote SFTP destination via pyremotedata.")
    parser.add_argument(
        "--sharelink-id",
        type=int,
        required=False,
        help="Sharelink ID for ERDA. If provided, pyremotedata will attempt an anonymous "
        "login with the given sharelink id as both username and password.",
    )

    return parser.parse_args(normalize_cli_args(sys.argv[1:]))


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
