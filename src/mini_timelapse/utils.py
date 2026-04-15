import logging
import os
import queue
import re
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import PIL

try:
    from pyremotedata.implicit_mount import IOHandler, RemotePathIterator
except ImportError:
    IOHandler = None
    RemotePathIterator = None

logger = logging.getLogger(__name__)

IMAGE_PATTERN = r"\.([pP][nN][gG]|[jJ][pP][eE]?[gG]|[tT][iI][fF][fF]?)$"
_DEFAULT_FORMATS = [
    "%Y:%m:%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%fZ",
]

# Thread-local storage container
_thread_local = threading.local()


def parse_time(time_str: str) -> datetime:
    # 1. Initialize the private list for this specific thread if it doesn't exist yet
    if not hasattr(_thread_local, "formats"):
        _thread_local.formats = list(_DEFAULT_FORMATS)

    formats = _thread_local.formats

    # 2. Parse and mutate the thread's private list (100% thread-safe, no locks)
    for i, fmt in enumerate(formats):
        try:
            dt = datetime.strptime(time_str, fmt)
            if i > 0:
                formats.insert(0, formats.pop(i))
            return dt
        except ValueError:
            continue

    raise ValueError(f"Time data '{time_str}' does not match any known format.")


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


def natural_sort_key(s: str):
    """
    Sort key for strings that separates into lexical (text) and numeric (int) parts.
    Example: "Dryas_1_101.JPG" -> ("Dryas_", 1, "_", 101)
    """
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", s)]


def normalize_cli_args(argv: list[str]) -> list[str]:
    """
    Normalizes long CLI flags by replacing underscores with dashes.
    This makes flags like --sharelink_id and --sharelink-id interchangeable.
    """
    normalized = []
    for arg in argv:
        if arg.startswith("--"):
            # Only split on the first '=' if it's a --key=value pair
            if "=" in arg:
                key, value = arg.split("=", 1)
                normalized.append(f"{key.replace('_', '-')}={value}")
            else:
                normalized.append(arg.replace("_", "-"))
        else:
            normalized.append(arg)
    return normalized


@dataclass
class TimelapseSpec:
    width: int
    height: int
    master_exif: bytes | None


class BaseImageSource(ABC):
    @dataclass
    class SourceSpec:
        src: str
        n_max: int | None = None
        recursive: bool = False

    def __init__(self, spec: SourceSpec):
        self.spec = spec

    @property
    def src(self):
        return self.spec.src

    @property
    def n_max(self):
        return self.spec.n_max

    @property
    def recursive(self):
        return self.spec.recursive

    @property
    @abstractmethod
    def elements(self):
        """Returns the collection of abstract elements (e.g., paths, URIs)."""
        raise NotImplementedError

    def __len__(self):
        return len(self.elements)

    def __enter__(self):
        raise NotImplementedError

    def __exit__(self, *args):
        pass

    @abstractmethod
    def _get_image_and_metadata(self, element) -> tuple[np.ndarray, dict]:
        """Parses a specific element into a frame array and metadata dictionary."""
        raise NotImplementedError

    def _generate_elements(self):
        """Default behavior: yields items from the elements property."""
        yield from self.elements

    def __iter__(self) -> Iterator[tuple[np.ndarray, dict]]:
        """Handles background threading, queueing, error-skipping, and backpressure."""
        prefetch_queue = queue.Queue(maxsize=64)
        sentinel = object()
        error_container = []

        def producer():
            try:
                # Iterate over whatever the subclass defines as elements
                for element in self._generate_elements():
                    try:
                        # Extract data. If a specific frame is corrupt, catch it here
                        # so we don't kill the entire thread and ruin the timelapse.
                        frame_data = self._get_image_and_metadata(element)
                        prefetch_queue.put(frame_data)
                    except Exception as e:
                        logger.warning(f"Failed to process element {element}: {e}")
                        continue
            except Exception as e:
                # This catches fatal errors with the iterator itself (e.g., network loss)
                error_container.append(e)
            finally:
                prefetch_queue.put(sentinel)

        worker = threading.Thread(target=producer, daemon=True)
        worker.start()

        while True:
            item = prefetch_queue.get()
            if item is sentinel:
                break
            yield item

        if error_container:
            raise error_container[0]

    @abstractmethod
    def get_timelapse_spec(self):
        raise NotImplementedError


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

    @property
    def elements(self):
        return self.files

    def get_timelapse_spec(self) -> TimelapseSpec:
        """Analyzes the first frame to determine video constraints and extract master EXIF."""
        img = PIL.Image.open(self.files[0])
        raw_exif = img.info.get("exif")
        return TimelapseSpec(width=img.size[0], height=img.size[1], master_exif=raw_exif)

    def _get_image_and_metadata(self, path: str):
        img = PIL.Image.open(path)
        meta = {"filename": os.path.basename(path)}

        exif_data = extract_image_metadata(img)
        if "time" in exif_data:
            meta["time"] = exif_data["time"]

        return np.array(img), meta

    def __enter__(self):
        return self


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

    @property
    def elements(self):
        if self.files is None:
            raise RuntimeError("Cannot access elements before __enter__ is called.")
        return self.files

    def get_timelapse_spec(self) -> TimelapseSpec:
        """Downloads and analyzes the first frame to determine video constraints and extract master EXIF."""
        if self.files is None:
            raise RuntimeError("get_timelapse_spec called before __enter__")
        local_file = self.handler.download(self.files[0])
        try:
            img = PIL.Image.open(local_file)
            raw_exif = img.info.get("exif")
            return TimelapseSpec(width=img.size[0], height=img.size[1], master_exif=raw_exif)
        finally:
            os.remove(local_file)

    def _generate_elements(self):
        """Overrides default generation to utilize the remote iterator's side-effects."""
        iterator = RemotePathIterator(io_handler=self.handler, clear_local=True)
        iterator.remote_paths = self.elements

        # We only yield the local path. The base class's producer thread will
        # catch it and pass it to _get_image_and_metadata for parsing.
        for lf, rf in iterator:
            yield lf

    def _get_image_and_metadata(self, path: str):
        img = PIL.Image.open(path)
        meta = {"filename": os.path.basename(path), "source": "remote"}

        exif_data = extract_image_metadata(img)
        if "time" in exif_data:
            meta["time"] = exif_data["time"]

        return np.array(img), meta

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
