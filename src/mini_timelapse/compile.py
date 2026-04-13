import argparse
import json
import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from fractions import Fraction

import av
import numpy as np
import PIL.Image
from tqdm import tqdm

from mini_timelapse.metadata import encode_metadata_payload, get_mkv_subtitle_header
from mini_timelapse.utils import BaseImageSource, TimelapseSpec, natural_sort_key, normalize_cli_args, parse_time

try:
    from pyremotedata.implicit_mount import IOHandler, RemotePathIterator
except ImportError:
    IOHandler = None
    RemotePathIterator = None

logger = logging.getLogger(__name__)

IMAGE_PATTERN = r"\.([pP][nN][gG]|[jJ][pP][eE]?[gG]|[tT][iI][fF][fF]?)$"


def extract_image_metadata(img: PIL.Image.Image) -> dict:
    """
    Unified robust function to extract and parse EXIF metadata from a PIL Image.
    Returns a dictionary with both raw string values and parsed objects.
    """
    meta = {}
    exif = img.getexif()
    if not exif:
        return meta

    # DateTimeOriginal (36867) usually lives in the Exif sub-IFD (0x8769)
    exif_ifd = exif.get_ifd(0x8769)
    dt_str = exif_ifd.get(36867) or exif.get(306)

    if dt_str:
        dt_str = str(dt_str).strip()
        meta["time"] = dt_str
        meta["dt"] = parse_time(dt_str)

    return meta


class LocalImageSource(BaseImageSource):
    """Provides images from a local directory or file list."""

    def __init__(self, spec: BaseImageSource.SourceSpec):
        super().__init__(spec)
        if os.path.isdir(self.src):
            if self.recursive:
                self.files = [
                    os.path.join(root, f)
                    for root, dirs, files in os.walk(self.src)
                    for f in files
                    if f.lower().endswith((".png", ".jpg", ".jpeg", ".tiff"))
                ]
            else:
                self.files = [
                    os.path.join(self.src, f) for f in os.listdir(self.src) if f.lower().endswith((".png", ".jpg", ".jpeg", ".tiff"))
                ]
            self.files.sort(key=natural_sort_key)
        else:
            self.files = [self.src]
        if self.n_max is not None:
            self.files = self.files[: min(len(self.files), self.n_max)]

    def get_timelapse_spec(self) -> TimelapseSpec:
        """Analyzes the first frame to determine video constraints and extract master EXIF."""
        img = PIL.Image.open(self.files[0])
        # Raw bytes of the EXIF block, or None if it doesn't exist
        raw_exif = img.info.get("exif")
        return TimelapseSpec(width=img.size[0], height=img.size[1], master_exif=raw_exif)

    def __iter__(self) -> Iterator[tuple[np.ndarray, dict]]:
        for f in self.files:
            img = PIL.Image.open(f)
            meta = {"filename": os.path.basename(f)}

            # Unified EXIF extraction
            exif_data = extract_image_metadata(img)
            if "time" in exif_data:
                meta["time"] = exif_data["time"]

            yield np.array(img), meta

    def __len__(self):
        return len(self.files)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class RemoteImageSource(BaseImageSource):
    """Provides images from a remote SFTP source via pyremotedata."""

    def __init__(self, spec: BaseImageSource.SourceSpec, sharelink_id: int | None = None, preext_pattern: str | None = None):
        super().__init__(spec)
        if IOHandler is None or RemotePathIterator is None:
            raise ImportError(
                "pyremotedata is not installed. Please install it to use remote sources (pip install mini-timelapse[remote])."
            )
        self.pattern = f"^.*{preext_pattern}.*{IMAGE_PATTERN}$" if preext_pattern is not None else IMAGE_PATTERN
        self.handler = IOHandler(user=sharelink_id, password=sharelink_id)
        self.files = None

    def get_timelapse_spec(self) -> TimelapseSpec:
        """Downloads and analyzes the first frame to determine video constraints and extract master EXIF."""
        if self.files is None:
            raise RuntimeError("__iter__ called before __enter__")
        local_file = self.handler.download(self.files[0])
        try:
            img = PIL.Image.open(local_file)
            raw_exif = img.info.get("exif")
            return TimelapseSpec(width=img.size[0], height=img.size[1], master_exif=raw_exif)
        finally:
            os.remove(local_file)

    def __iter__(self) -> Iterator[tuple[np.ndarray, dict]]:
        if RemotePathIterator is None:
            raise ImportError(
                "pyremotedata.implicit_mount.RemotePathIterator is not installed. "
                "Please install it to use remote sources (pip install mini-timelapse[remote])."
            )
        if self.files is None:
            raise RuntimeError("__iter__ called before __enter__")
        iterator = RemotePathIterator(io_handler=self.handler, clear_local=True)
        iterator.remote_paths = self.files
        for lf, rf in iterator:
            img = PIL.Image.open(lf)
            meta = {"filename": os.path.basename(lf), "source": "remote"}

            # Unified EXIF extraction
            exif_data = extract_image_metadata(img)
            if "time" in exif_data:
                meta["time"] = exif_data["time"]

            yield np.array(img), meta

    def __len__(self):
        if self.files is None:
            raise RuntimeError("__len__ called before __enter__")
        return len(self.files)

    def __enter__(self):
        self.handler.__enter__()
        self.handler.cd(self.src)
        self.files = self.handler.get_file_index(pattern=self.pattern, nmax=self.n_max)
        if not self.recursive:
            self.files = [f for f in self.files if f.count("/") < 1]
        if len(self.files) == 0:
            raise ValueError(f"No files found matching pattern '{self.pattern}' in directory '{self.src}'.")
        self.files.sort(key=natural_sort_key)
        return self

    def __exit__(self, *args):
        self.handler.__exit__()


def parse_unknown_arguments(extra_args: list[str]) -> dict:
    results = {}
    for arg in extra_args:
        if "=" in arg:
            key, value = arg.split("=", 1)
            results[key.lstrip("-")] = value
    return results


def get_exif_data(path: str) -> dict:
    """Standalone wrapper that utilizes the unified metadata extractor."""
    img = PIL.Image.open(path)
    exif_data = extract_image_metadata(img)
    return {"dt": exif_data["dt"]} if "dt" in exif_data else {}


def compile_video(
    source: BaseImageSource,
    output: str | None = None,
    fps: int = 30,
    quality: int = 23,
    preset: str = "medium",
    dry_run: bool = False,
):
    """
    Compiles images into a video with mirrored metadata:
    1. Visible HUD via subtitle stream
    2. Sovereign 100% reliable backbone via Matroska attachments
    """
    if output is None:
        output = os.path.split(os.path.normpath(source.src))[-1] + ".mkv"

    # RESUME CHECK: If the final file exists, we're already done.
    if os.path.exists(output):
        logger.info(f"Output video '{output}' already exists. Skipping compilation.")
        return

    if dry_run:
        logger.info(f"Dry-run: would encode {len(source)} images to {output}")
        return

    # Track temporary files for robust cleanup
    meta_json_path = None
    master_exif_path = None
    final_part = output + ".part"

    temp_fd, temp_video_path = tempfile.mkstemp(suffix=".mkv")
    os.close(temp_fd)

    all_metadata = []

    container = av.open(
        temp_video_path,
        mode="w",
        options={
            "cluster_size_limit": "1048576",
            "cluster_time_limit": "1000",
        },
    )

    try:
        with source:
            spec = source.get_timelapse_spec()

            vstream = container.add_stream("libx264", rate=fps)
            vstream.width = spec.width
            vstream.height = spec.height
            vstream.pix_fmt = "yuv444p"

            time_base = Fraction(1, 1000)
            vstream.time_base = time_base
            vstream.codec_context.time_base = time_base

            vstream.color_primaries = 1
            vstream.color_trc = 1
            vstream.colorspace = 1
            vstream.options = {"crf": str(quality), "preset": preset, "tune": "zerolatency"}

            vstream.thread_count = 0
            vstream.thread_type = "SLICE"

            mstream = container.add_stream("ass")
            mstream.time_base = time_base
            mstream.codec_context.extradata = get_mkv_subtitle_header()

            for i, (rgb_array, meta) in enumerate(tqdm(source, desc="Compiling", unit="frame")):
                mpts = int(round(i * 1000 / fps))
                next_mpts = int(round((i + 1) * 1000 / fps))
                mdur = max(1, next_mpts - mpts)

                full_meta = meta.copy()
                full_meta["index"] = i
                all_metadata.append(full_meta)

                m_payload = encode_metadata_payload(i, full_meta, float(fps))
                m_packet = av.Packet(m_payload)
                m_packet.stream = mstream
                m_packet.pts = mpts
                m_packet.dts = mpts
                m_packet.duration = mdur
                m_packet.is_keyframe = True
                container.mux(m_packet)

                frame = av.VideoFrame.from_ndarray(rgb_array, format="rgb24")
                frame.pts = mpts
                for vpacket in vstream.encode(frame):
                    container.mux(vpacket)

            for vpacket in vstream.encode():
                container.mux(vpacket)

            container.close()

            # --- MKV Attachment Finalization ---
            meta_fd, meta_json_path = tempfile.mkstemp(suffix=".json")
            os.close(meta_fd)
            with open(meta_json_path, "w") as f:
                json.dump(all_metadata, f)

            if spec.master_exif:
                exif_fd, master_exif_path = tempfile.mkstemp(suffix=".exif")
                os.close(exif_fd)
                with open(master_exif_path, "wb") as f:
                    f.write(spec.master_exif)

            logger.info("Mirroring metadata to Matroska attachments and optimizing for streaming...")

            ffmpeg_cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                temp_video_path,
                # CRITICAL ADDITION: Explicitly map all streams from input 0.
                # Without this, FFmpeg might drop the subtitle stream if it gets
                # confused by the attachments. This forces it to keep the Video + ASS.
                "-map",
                "0",
                "-attach",
                meta_json_path,
                "-metadata:s:t:0",
                "mimetype=application/json",
                "-metadata:s:t:0",
                "filename=metadata.json",
            ]

            if master_exif_path:
                ffmpeg_cmd.extend(
                    [
                        "-attach",
                        master_exif_path,
                        "-metadata:s:t:1",
                        "mimetype=application/octet-stream",
                        "-metadata:s:t:1",
                        "filename=master.exif",
                    ]
                )

            # Calculate dynamic index space (min 2MB, scales up for millions of frames)
            index_kb = max(2048, int((len(source) / 1000) * 10))

            ffmpeg_cmd.extend(
                [
                    "-c",
                    "copy",
                    "-f",
                    "matroska",
                    "-reserve_index_space",
                    f"{index_kb}K",
                    final_part,
                ]
            )

            try:
                subprocess.run(ffmpeg_cmd, check=True)
            except Exception as e:
                logger.error(f"Post-process attachment failed: {e}. Falling back to unattached video.")
                shutil.copy2(temp_video_path, final_part)

            os.replace(final_part, output)
            logger.info(f"Successfully created: {output}")
    except Exception as e:
        logger.error(f"Compilation failed: {e}")
        raise
    finally:
        # Ironclad cleanup for any temporary files generated during the process
        for path in [temp_video_path, meta_json_path, master_exif_path, final_part]:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass


def cli():
    import sys

    parser = argparse.ArgumentParser(prog="compile_timelapse")
    parser.add_argument("-i", "--input", required=True)
    parser.add_argument("-o", "--output")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("-q", "--quality", type=int, default=23)
    parser.add_argument("--preset", default="medium")
    parser.add_argument("-r", "--recursive", action="store_true")
    parser.add_argument("-n", "--n-max", type=int, default=None)
    parser.add_argument("-d", "--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--remote", action="store_true")
    parser.add_argument("--sharelink-id", type=int, default=None)
    parser.add_argument("--preext-pattern", type=str, default=None)
    args, extra = parser.parse_known_args(normalize_cli_args(sys.argv[1:]))
    return {**vars(args), **parse_unknown_arguments(extra)}


def main():
    args = cli()
    log_level = logging.DEBUG if args.get("verbose") else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s: %(message)s")
    input, output = args.pop("input"), args.pop("output")
    src_spec = BaseImageSource.SourceSpec(
        src=input,
        n_max=args.pop("n_max", None),
        recursive=args.pop("recursive", False),
    )
    remote = args.pop("remote", False)
    if not remote:
        sharelink_id = args.pop("sharelink_id", None)
        preext_pattern = args.pop("preext_pattern", None)
        if sharelink_id is not None:
            logger.warning(f"{sharelink_id=} is ignored for local sources")
        if preext_pattern is not None:
            logger.warning(f"{preext_pattern=} is ignored for local sources")
        src = LocalImageSource(src_spec)
    else:
        sharelink_id = args.pop("sharelink_id", None)
        preext_pattern = args.pop("preext_pattern", None)
        src = RemoteImageSource(src_spec, sharelink_id=sharelink_id, preext_pattern=preext_pattern)
    compile_video(
        source=src,
        output=output,
        fps=args.pop("fps"),
        quality=args.pop("quality"),
        preset=args.pop("preset"),
        dry_run=args.pop("dry_run"),
    )


if __name__ == "__main__":
    main()
