import av
import os
import json
import logging
import argparse
import heapq
import itertools
import subprocess
import tempfile
import shutil
from tqdm import tqdm
from datetime import datetime
from typing import Iterator, Union, Optional
from fractions import Fraction
import numpy as np
import PIL.Image
import io
from mini_timelapse.utils import natural_sort_key
from mini_timelapse.metadata import get_mkv_subtitle_header, encode_metadata_payload

try:
    from pyremotedata.implicit_mount import IOHandler, RemotePathIterator
except ImportError:
    IOHandler = None
    RemotePathIterator = None

logger = logging.getLogger(__name__)

class LocalImageSource:
    """Provides images from a local directory or file list."""
    def __init__(self, path: str):
        self.path = path
        if os.path.isdir(path):
            self.files = [os.path.join(path, f) for f in os.listdir(path) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.tiff'))]
            self.files.sort(key=natural_sort_key)
        else:
            self.files = [path]
    
    def __iter__(self) -> Iterator[tuple[np.ndarray, dict]]:
        for f in self.files:
            img = PIL.Image.open(f)
            meta = {"filename": os.path.basename(f)}
            exif = img.getexif()
            if exif:
                # DateTimeOriginal (36867) usually lives in the Exif sub-IFD (0x8769)
                exif_ifd = exif.get_ifd(0x8769)
                dt = exif_ifd.get(36867) or exif.get(306)
                if dt:
                    meta["time"] = str(dt)
            yield np.array(img), meta

    def get_first_dims(self):
        img = PIL.Image.open(self.files[0])
        return img.size

    def __len__(self):
        return len(self.files)
    
    def __enter__(self): return self
    def __exit__(self, *args): pass

class RemoteImageSource:
    """Provides images from a remote SFTP source via pyremotedata."""
    def __init__(self, url: str):
        if IOHandler is None:
            raise ImportError("pyremotedata is not installed. Please install it to use remote sources (pip install mini-timelapse[remote]).")
        self.url = url
        self.handler = IOHandler()
        self.files = None

    def __iter__(self) -> Iterator[tuple[np.ndarray, dict]]:
        iterator = RemotePathIterator(io_handler=self.handler, clear_local=True)
        # Note: iterator.remote_paths is already populated after IOHandler.cd + iterator init
        iterator.remote_paths.sort(key=natural_sort_key)
        for lf, rf in iterator:
            img = PIL.Image.open(lf)
            meta = {"filename": os.path.basename(lf), "source": "remote"}
            exif = img.getexif()
            if exif:
                exif_ifd = exif.get_ifd(0x8769)
                dt = exif_ifd.get(36867) or exif.get(306)
                if dt:
                    meta["time"] = str(dt)
            yield np.array(img), meta

    def get_first_dims(self):
        files = self.handler.get_file_index(nmax=1, pattern=r"\.([pP][nN][gG]|[jJ][pP][eE]?[gG])$")
        local_file = self.handler.download(files[0])
        try:
            img = PIL.Image.open(local_file)
            return img.size
        finally:
            os.remove(local_file)

    def __len__(self):
        if self.files is None:
            raise RuntimeError("__len__ called before __enter__")
        return len(self.files)

    def __enter__(self):
        self.handler.__enter__()
        self.handler.cd(self.url)
        self.files = self.handler.get_file_index(pattern=r"\.([pP][nN][gG]|[jJ][pP][eE]?[gG]|[tT][iI][fF][fF]?)$")
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
    img = PIL.Image.open(path)
    exif = img.getexif()
    res = {}
    if exif:
        exif_ifd = exif.get_ifd(0x8769)
        dt_str = exif_ifd.get(36867) or exif.get(306)
        if dt_str:
            for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    res["dt"] = datetime.strptime(dt_str, fmt)
                    break
                except ValueError:
                    continue
    return res

def compile_video(
    source: Union[LocalImageSource, RemoteImageSource],
    output: str,
    fps: int = 30,
    quality: int = 23,
    preset: str = "medium",
    dry_run: bool = False
):
    """
    Compiles images into a video with mirrored metadata:
    1. Visible HUD via subtitle stream
    2. Sovereign 100% reliable backbone via Matroska JSON attachment
    """
    if dry_run:
        logger.info(f"Dry-run: would encode {len(source)} images to {output}")
        return

    # Use a temporary file for the initial mux to allow post-process attachment
    temp_fd, temp_video_path = tempfile.mkstemp(suffix=".mkv")
    os.close(temp_fd)

    all_metadata = []
    
    container = av.open(
        temp_video_path, 
        mode="w", 
        options={
            "cluster_size_limit": "2048",
            "cluster_time_limit": "33"
        }
    )
    
    with source:
        width, height = source.get_first_dims()
        vstream = container.add_stream("libx264", rate=fps)
        vstream.width = width
        vstream.height = height
        vstream.pix_fmt = "yuv444p"
        
        time_base = Fraction(1, 1000)
        vstream.time_base = time_base
        vstream.codec_context.time_base = time_base
        
        vstream.color_primaries = 1
        vstream.color_trc = 1
        vstream.colorspace = 1
        vstream.options = {"crf": str(quality), "preset": preset, "tune": "zerolatency"}
        
        # FIX 1: Use SLICE threading to strictly enforce zero-latency 1-in-1-out encoding
        vstream.thread_count = 0 
        vstream.thread_type = "SLICE"

        mstream = container.add_stream("ass")
        mstream.time_base = time_base
        mstream.codec_context.extradata = get_mkv_subtitle_header()

        try:
            for i, (rgb_array, meta) in enumerate(tqdm(source, desc="Compiling", unit="frame")):
                mpts = int(round(i * 1000 / fps))
                next_mpts = int(round((i + 1) * 1000 / fps))
                mdur = max(1, next_mpts - mpts)
                
                # Metadata Record
                full_meta = meta.copy()
                full_meta["index"] = i
                all_metadata.append(full_meta)
                
                # 1. Write Subtitle Packet directly to the container
                m_payload = encode_metadata_payload(i, full_meta, float(fps))
                m_packet = av.Packet(m_payload)
                m_packet.stream = mstream
                m_packet.pts = mpts
                m_packet.dts = mpts
                m_packet.duration = max(1, mdur - 2)
                m_packet.is_keyframe = True 
                container.mux(m_packet)
                
                # 2. Encode and Write Video Frame directly to the container
                frame = av.VideoFrame.from_ndarray(rgb_array, format="rgb24")
                frame.pts = mpts
                for vpacket in vstream.encode(frame):
                    container.mux(vpacket)

            # Flush remaining packets from the encoder
            for vpacket in vstream.encode():
                container.mux(vpacket)
            
            container.close()

            # Step 4: Post-Process Attachment (Final Boss Fix)
            meta_fd, meta_json_path = tempfile.mkstemp(suffix=".json")
            os.close(meta_fd)
            with open(meta_json_path, "w") as f:
                json.dump(all_metadata, f)

            logger.info("Mirroring metadata to Matroska attachment...")
            try:
                subprocess.run([
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", temp_video_path,
                    "-attach", meta_json_path,
                    "-metadata:s:t:0", "mimetype=application/json",
                    "-metadata:s:t:0", "filename=metadata.json",
                    "-c", "copy", output
                ], check=True)
                logger.info(f"Successfully created: {output}")
            except Exception as e:
                logger.error(f"Post-process attachment failed: {e}. Falling back to unattached video.")
                shutil.copy2(temp_video_path, output)
            finally:
                if os.path.exists(temp_video_path): os.remove(temp_video_path)
                if os.path.exists(meta_json_path): os.remove(meta_json_path)
                
        except Exception as e:
            logger.error(f"Compilation failed: {e}")
            if os.path.exists(temp_video_path): os.remove(temp_video_path)
            raise

def cli():
    parser = argparse.ArgumentParser(prog="compile_timelapse")
    parser.add_argument("-i", "--input", required=True)
    parser.add_argument("-o", "--output")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("-q", "--quality", type=int, default=23)
    parser.add_argument("--preset", default="medium")
    parser.add_argument("-d", "--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--remote", action="store_true")
    args, extra = parser.parse_known_args()
    return {**vars(args), **parse_unknown_arguments(extra)}

def main():
    args = cli()
    log_level = logging.DEBUG if args.get("verbose") else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s: %(message)s")
    src, output = args.pop("input"), args.pop("output")
    if output is None:
        output = os.path.split(os.path.normpath(src))[-1] + ".mkv"
    remote = args.pop("remote", False)
    SourceCls = RemoteImageSource if remote else LocalImageSource
    compile_video(
        source=SourceCls(src), 
        output=output, 
        fps=args.pop("fps"), 
        quality=args.pop("quality"), 
        preset=args.pop("preset"), 
        dry_run=args.pop("dry_run")
    )

if __name__ == "__main__":
    main()