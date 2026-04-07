import argparse
import logging
import os
import re
import sys
from datetime import datetime
from glob import glob

from PIL import Image
import json
import numpy as np
from fractions import Fraction
from mini_timelapse.utils import natural_sort_key

try:
    from pyremotedata.implicit_mount import IOHandler, RemotePathIterator
    REMOTE_AVAILABLE = True
except ImportError:
    REMOTE_AVAILABLE = False

def parse_unknown_arguments(extra: list[str]) -> dict[str, any]:
    """
    Parse a list of remaining command-line arguments into a dictionary.

    Args:
        extra (list[str]): List of unparsed arguments from argparse.

    Returns:
        dict[str, any]: Parsed arguments with values correctly typed.
    """
    unknown_args = {}
    i = 0
    while i < len(extra):
        arg = extra[i]
        if arg.startswith("--"):
            key = arg.removeprefix("--")
            value = extra[i+1]
            i += 1
        elif arg.startswith("-"):
            key = arg.removeprefix("-")
            value = extra[i+1]
            i += 1
        elif "=" in arg:
            key, value = arg.split("=")
        else:
            position = sum([len(arg) for arg in extra[:i]]) + i
            raise ValueError(f"Unable to parse extra misspecified or unnamed argument: `{arg}` at position {position}:{position + len(arg)}.")
        if value.isdigit():
            value = int(value)
        elif value.replace('.', '', 1).isdigit() and value.count('.') < 2:
            value = float(value)
        unknown_args[key] = value
        i += 1
    return unknown_args

logger = logging.getLogger("compile_timelapse")

IMAGE_PATTERN = re.compile(r'\.(jpe?g|png)$', re.IGNORECASE)

def is_image(file: str) -> bool:
    return bool(re.search(IMAGE_PATTERN, file))

def _get_if_exist(data, key):
    if key in data:
        return data[key]
    return None

def get_exif_data(img_path: str) -> dict:
    """
    Extract EXIF metadata (datetime and GPS coordinates) from an image.

    Args:
        img_path (str): File path to the image.

    Returns:
        dict: A dictionary containing 'dt' (datetime object), 'lat' (latitude), 
              and 'lon' (longitude) if available.
    """
    res = {}
    try:
        from PIL import ExifTags
        with Image.open(img_path) as img:
            exif = img.getexif()
            if not exif:
                return res
            
            # DateTime
            exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
            dt_str = exif_ifd.get(ExifTags.Base.DateTimeOriginal)
            if dt_str:
                 res["dt"] = datetime.strptime(dt_str.strip(), "%Y:%m:%d %H:%M:%S")

            # GPS
            gps_ifd = exif.get_ifd(ExifTags.IFD.GPSInfo)
            if gps_ifd:
                def _to_deg(val):
                    def get_val(v):
                        if hasattr(v, 'numerator') and hasattr(v, 'denominator'):
                            return v.numerator / v.denominator if v.denominator != 0 else 0
                        return v[0] / v[1] if v[1] != 0 else 0
                    
                    d = get_val(val[0])
                    m = get_val(val[1])
                    s = get_val(val[2])
                    return d + (m / 60.0) + (s / 3600.0)
                
                lat_tuple = _get_if_exist(gps_ifd, 2)
                lat_ref = _get_if_exist(gps_ifd, 1)
                lon_tuple = _get_if_exist(gps_ifd, 4)
                lon_ref = _get_if_exist(gps_ifd, 3)

                if lat_tuple and lat_ref and lon_tuple and lon_ref:
                    lat = _to_deg(lat_tuple)
                    if lat_ref != 'N': lat = -lat
                    
                    lon = _to_deg(lon_tuple)
                    if lon_ref != 'E': lon = -lon
                    
                    res["lat"] = round(lat, 6)
                    res["lon"] = round(lon, 6)
                    
    except Exception as e:
        logger.debug(f"Failed to extract EXIF from {img_path}: {e}")
    return res


class ImageSource:
    """Base class for image sources (local or remote)."""
    def __init__(self, src: str):
        self.src = src
        self.paths = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def __len__(self):
        return len(self.paths)

    def get_first_dims(self) -> tuple[int, int]:
        """Get width/height of the first image in the sequence."""
        raise NotImplementedError

    def __iter__(self):
        """Yield (rgb_ndarray, metadata_dict) for each image."""
        raise NotImplementedError

class LocalImageSource(ImageSource):
    """Image source handling local filesystem paths, directories, and globs."""
    def __init__(self, src: str):
        super().__init__(src)
        if os.path.exists(src):
            if os.path.isfile(src):
                self.paths = [src]
            else:
                self.paths = glob(os.path.join(src, "*"))
        else:
            self.paths = glob(src)
        
        self.paths = [p for p in self.paths if is_image(p)]
        self.paths.sort(key=natural_sort_key)
        
        if not self.paths:
            logger.error(f"No images found in: {src}")
            sys.exit(1)

    def get_first_dims(self) -> tuple[int, int]:
        with Image.open(self.paths[0]) as img:
            return img.size

    def __iter__(self):
        from concurrent.futures import ThreadPoolExecutor
        prefetch = min(8, len(self.paths))
        with ThreadPoolExecutor(max_workers=prefetch) as pool:
            futures = {}
            for j in range(prefetch):
                futures[j] = pool.submit(self._load, self.paths[j])
            
            next_submit = prefetch
            for i in range(len(self.paths)):
                yield futures[i].result()
                del futures[i]
                if next_submit < len(self.paths):
                    futures[next_submit] = pool.submit(self._load, self.paths[next_submit])
                    next_submit += 1

    def _load(self, path: str) -> tuple[np.ndarray, dict]:
        meta = get_exif_data(path)
        meta_dict = {"time": meta["dt"].strftime("%Y-%m-%d %H:%M:%S") if "dt" in meta else "unknown"}
        if "lat" in meta: meta_dict["lat"] = meta["lat"]
        if "lon" in meta: meta_dict["lon"] = meta["lon"]
        with Image.open(path) as img:
            return np.asarray(img.convert("RGB")), meta_dict

class RemoteImageSource(ImageSource):
    """Image source handling remote SFTP paths via pyremotedata."""
    def __init__(self, src: str):
        super().__init__(src)
        if not REMOTE_AVAILABLE:
            logger.error("pyremotedata not installed.")
            sys.exit(1)
        self.io = IOHandler()

    def __enter__(self):
        self.io.mount()
        self.io.cd(self.src)
        self.paths = [f for f in self.io.get_file_index() if is_image(f)]
        self.paths.sort(key=natural_sort_key)
        if not self.paths:
            logger.error(f"No images found in remote: {self.src}")
            sys.exit(1)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.io.unmount()

    def get_first_dims(self) -> tuple[int, int]:
        local_tmp = self.io.download(self.paths[0])
        with Image.open(local_tmp) as img:
            dims = img.size
        os.remove(local_tmp)
        return dims

    def __iter__(self):
        # uses the source's already-mounted IOHandler
        iterator = RemotePathIterator(self.io, clear_local=True)
        for local_path, _ in iterator:
            meta = get_exif_data(local_path)
            meta_dict = {"time": meta["dt"].strftime("%Y-%m-%d %H:%M:%S") if "dt" in meta else "unknown"}
            if "lat" in meta: meta_dict["lat"] = meta["lat"]
            if "lon" in meta: meta_dict["lon"] = meta["lon"]
            with Image.open(local_path) as img:
                yield np.asarray(img.convert("RGB")), meta_dict

def compile_video(
    source: ImageSource,
    output: str,
    fps: int,
    quality: int,
    preset: str,
    dry_run: bool
):
    """
    Compile a sequence of images into a single video file.
    
    Metadata extracted from images is interleaved as an ASS subtitle stream 
    containing JSON-formatted metadata for each frame.

    Args:
        source (ImageSource): An initialized image source yielding frames and metadata.
        output (str): Path to the output video file.
        fps (int): Frames per second for the output video.
        quality (int): Video encoding quality (CRF).
        preset (str): x264 encoding preset (e.g., 'fast', 'slow').
        dry_run (bool): If True, log actions without writing the video.
    """
    import av
    from tqdm import tqdm

    if dry_run:
        logger.info(f"Dry-run: would encode {len(source)} images to {output}")
        return

    # Initialize PyAV container
    container = av.open(output, mode="w")
    
    # Setup video stream
    width, height = source.get_first_dims()
    vstream = container.add_stream("libx264", rate=fps)
    vstream.width = width
    vstream.height = height
    vstream.pix_fmt = "yuv420p"
    vstream.options = {"crf": str(quality), "preset": preset}
    vstream.thread_count = 0 
    vstream.thread_type = "FRAME"

    # Setup metadata stream (Subtitle track for interleaved metadata)
    mstream = container.add_stream("ass")
    mstream.metadata["TITLE"] = "JSON_METADATA"
    mstream.time_base = Fraction(1, fps)

    logger.info(f"Encoding {len(source)} frames to {output}...")

    metadata_to_mux = []

    try:
        for i, (rgb_array, meta) in enumerate(tqdm(source, desc="Compiling", unit="frame")):
            metadata_to_mux.append((i, meta))
            
            # Encode Video Frame
            frame = av.VideoFrame.from_ndarray(rgb_array, format="rgb24")
            frame.pts = i
            for packet in vstream.encode(frame):
                container.mux(packet)

        # Flush video encoder
        for packet in vstream.encode():
            container.mux(packet)

        # Mux all collected metadata (at the end for reliability)
        for i, meta in metadata_to_mux:
            packet = av.Packet(json.dumps(meta).encode("utf-8"))
            packet.stream = mstream
            packet.pts = i
            packet.dts = i
            packet.duration = 1
            container.mux(packet)
            
        container.close()
        logger.info(f"Successfully created: {output}")
        
    except Exception as e:
        logger.error(f"Compilation failed: {e}")
        try:
            container.close()
        except:
            pass
        raise


def cli():
    parser = argparse.ArgumentParser(
        prog="compile_timelapse", 
        description="Compile a timelapse video (1:1 frames) with embedded EXIF metadata."
    )
    parser.add_argument(
        "-i", "--input", type=str, required=True,
        help="Path to a directory with images, an image file or a glob to image files."
    )
    parser.add_argument(
        "-o", "--output", type=str, required=True,
        help="Path to the output video file (e.g., output.mkv). MKV is heavily recommended for subtitle streams."
    )
    parser.add_argument(
        "--fps", type=int, default=30,
        help="Playback speed (frames per second). Higher = faster scrubbing. Default: 30."
    )
    parser.add_argument(
        "-q", "--quality", type=int, default=23,
        help="Video encoding quality (CRF). Lower is better. Range 0-51. Default: 23."
    )
    parser.add_argument(
        "--preset", type=str, default="medium",
        help="x264 encoding preset. Faster presets trade quality for speed. "
             "Options: ultrafast, superfast, veryfast, faster, fast, medium, slow, slower, veryslow. Default: medium."
    )
    parser.add_argument(
        "-d", "--dry-run", action="store_true",
        help="Log actions and parameters without encoding."
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug logging."
    )
    parser.add_argument(
        "--remote", action="store_true",
        help="Use pyremotedata backend for remote SFTP access."
    )
    
    args, extra = parser.parse_known_args()
    try:
        extra_args = parse_unknown_arguments(extra)
    except ValueError as e:
        raise ValueError(
                f"Error parsing extra arguments: `{' '.join(extra)}`. {e}\n\n"
                f"{parser.format_help()}"
            )
    return {**vars(args), **extra_args}

def main():
    args = cli()
    log_level = logging.DEBUG if args["verbose"] else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s: %(message)s")
    
    src = args.pop("input")
    remote = args.pop("remote", False)
    
    SourceCls = RemoteImageSource if remote else LocalImageSource
    
    with SourceCls(src) as source:
        compile_video(
            source=source,
            output=args.pop("output"),
            fps=args.pop("fps"),
            quality=args.pop("quality"),
            preset=args.pop("preset"),
            dry_run=args.pop("dry_run")
        )

if __name__ == "__main__":
    main()
