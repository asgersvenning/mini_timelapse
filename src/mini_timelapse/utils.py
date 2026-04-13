import re
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np


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
    def __iter__(self) -> Iterator[tuple[np.ndarray, dict]]:
        raise NotImplementedError

    @abstractmethod
    def get_timelapse_spec(self) -> TimelapseSpec:
        raise NotImplementedError
