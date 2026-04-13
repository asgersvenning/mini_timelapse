import re
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

import numpy as np

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

    def __enter__(self):
        raise NotImplementedError

    def __exit__(self, *args):
        pass

    @abstractmethod
    def _get_image_and_metadata(self, path: str):
        raise NotImplementedError

    @abstractmethod
    def __iter__(self) -> Iterator[tuple[np.ndarray, dict]]:
        raise NotImplementedError

    @abstractmethod
    def get_timelapse_spec(self) -> TimelapseSpec:
        raise NotImplementedError
