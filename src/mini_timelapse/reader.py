import json
import logging
import os
import subprocess
import tempfile
from collections.abc import Iterator
from datetime import datetime

import av
import numpy as np

from mini_timelapse.metadata import decode_metadata_payload
from mini_timelapse.utils import BaseImageSource, TimelapseSpec

logger = logging.getLogger(__name__)


class TimelapseVideo:
    """
    A high-level interface for reading and decompiling timelapse videos
    with frame-accurate metadata recovery.
    """

    def __init__(self, path: str, fps: float = 30.0):
        self.path = path
        self._fps = fps if fps is not None else 30.0
        self._container = av.open(path)
        self._video_stream = next((s for s in self._container.streams if s.type == "video"), None)
        if not self._video_stream:
            raise ValueError(f"No video stream found in {path}")

        self.width = self._video_stream.width
        self.height = self._video_stream.height
        self.length = self._video_stream.frames
        if self.length == 0:
            # Fallback for some containers: use duration from stream or container
            if self._video_stream.duration is not None:
                self.length = int(round(float(self._video_stream.duration * self._video_stream.time_base) * self._fps))
            elif self._container.duration is not None:
                # container.duration is in av.time_base (microseconds)
                self.length = int(round(float(self._container.duration / av.time_base) * self._fps))
            else:
                self.length = 0

        # Extraction stats
        self.metadata_sources = set()

        # Extract metadata
        self.metadata = self._extract_metadata()

    def __len__(self):
        return self.length

    def _extract_metadata(self) -> list[dict]:
        """
        Extracts metadata using the most reliable source available:
        1. Sovereign Backbone: Matroska JSON attachment (added via post-process)
        2. Visible Stream: FFmpeg SRT parsing (fallback)
        3. Raw Decoder: PyAV demuxing (safety fallback)
        """
        metadata_dict = {}
        unique_indices = set()

        # 1. SOVEREIGN PATH: Matroska Attachment (Atomic & 100% Reliable)
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_json = os.path.join(tmp_dir, "extracted_meta.json")
                cmd = [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-dump_attachment:t:0",
                    tmp_json,
                    "-i",
                    self.path,
                    "-f",
                    "null",
                    "-",
                ]
                # Run with timeout to prevent hangs
                subprocess.run(cmd, timeout=10, check=True, capture_output=True)

                if os.path.exists(tmp_json):
                    with open(tmp_json) as f:
                        attachment_data = json.load(f)

                    if isinstance(attachment_data, list):
                        logger.info(f"Loaded {len(attachment_data)} metadata entries from Matroska attachment.")
                        self.metadata_sources.add("attachment")
                        for item in attachment_data:
                            if "index" in item:
                                metadata_dict[int(item["index"])] = item
                                unique_indices.add(int(item["index"]))
        except Exception as e:
            logger.debug(f"Attachment extraction skipped or failed: {e}")

        # 2. LEGACY PATH: FFmpeg CLI SRT Parsing
        if len(unique_indices) < self.length:
            sub_stream = next((s for s in self._container.streams if s.type == "subtitle"), None)
            if sub_stream:
                try:
                    cmd = [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-i",
                        self.path,
                        "-map",
                        f"0:{sub_stream.index}",
                        "-f",
                        "srt",
                        "-",
                    ]
                    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    stdout, _ = process.communicate()

                    if process.returncode == 0:
                        srt_text = stdout.decode("utf-8", errors="ignore")
                        meta_items = decode_metadata_payload(srt_text)
                        if meta_items:
                            self.metadata_sources.add("subtitle")
                        for item in meta_items:
                            if "index" in item:
                                idx = int(item["index"])
                                if idx not in metadata_dict:
                                    metadata_dict[idx] = item
                                    unique_indices.add(idx)
                except Exception as e:
                    logger.debug(f"FFmpeg CLI extraction fallback failed: {e}")

        # 3. SAFETY FALLBACK: PyAV Demux
        if len(unique_indices) < self.length:
            sub_stream = next((s for s in self._container.streams if s.type == "subtitle"), None)
            if sub_stream:
                self._container.seek(0)
                for packet in self._container.demux(sub_stream):
                    self.metadata_sources.add("demux")
                    try:
                        for subtitle in packet.decode():
                            for rect in getattr(subtitle, "rects", []):
                                text_data = getattr(rect, "ass", "") or getattr(rect, "text", "")
                                if text_data:
                                    for item in decode_metadata_payload(text_data):
                                        idx = int(item.get("index", -1))
                                        if idx != -1 and idx not in metadata_dict:
                                            metadata_dict[idx] = item
                                            unique_indices.add(idx)
                    except Exception:
                        pass
                    for item in decode_metadata_payload(bytes(packet)):
                        idx = int(item.get("index", -1))
                        if idx != -1 and idx not in metadata_dict:
                            metadata_dict[idx] = item
                            unique_indices.add(idx)

        # Map to dense list
        metadata = [{} for _ in range(self.length)]
        for idx, item in metadata_dict.items():
            if 0 <= idx < self.length:
                metadata[idx] = item

        logger.info(f"Metadata Integrity: {len(unique_indices)}/{self.length} frames synchronized.")
        return metadata

    @property
    def master_exif(self) -> bytes | None:
        """Attempts to extract the original raw EXIF binary attachment."""
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_exif = os.path.join(tmp_dir, "master.exif")
                cmd = [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-dump_attachment:t",
                    tmp_exif,  # Extracts all attachments, we just read the EXIF one
                    "-i",
                    self.path,
                    "-f",
                    "null",
                    "-",
                ]
                subprocess.run(cmd, timeout=10, capture_output=True)

                if os.path.exists(tmp_exif):
                    with open(tmp_exif, "rb") as f:
                        return f.read()
        except Exception as e:
            logger.debug(f"Could not extract master EXIF attachment: {e}")
        return None

    def get_frame(self, index: int) -> tuple[np.ndarray, dict]:
        """Returns the RGB frame and metadata for a given index."""
        if index < 0:
            index = self.length + index

        if index < 0 or index >= self.length:
            raise IndexError(f"Frame index {index} out of bounds (0-{self.length - 1})")

        pts = int(round(index * 1000 / self._fps))
        self._container.seek(pts, stream=self._video_stream)

        for frame in self._container.decode(self._video_stream):
            # Check if this frame is actually the one we wanted or later
            # (seek might land earlier)
            frame_idx = int(round(frame.pts * frame.time_base * self._fps))
            if frame_idx >= index:
                return frame.to_ndarray(format="rgb24"), self.metadata[index]

        raise RuntimeError(f"Could not seek to frame {index}")

    def __getitem__(self, key: int | slice):
        """Supports indexing and slicing of video frames."""
        if isinstance(key, int):
            return self.get_frame(key)
        elif isinstance(key, slice):
            indices = range(*key.indices(self.length))
            return [self.get_frame(i) for i in indices]
        else:
            raise TypeError(f"Invalid argument type: {type(key)}")

    def __iter__(self):
        """Iterates through all frames in the video."""
        self._container.seek(0)
        idx = 0
        for frame in self._container.decode(self._video_stream):
            if idx >= self.length:
                break
            yield frame.to_ndarray(format="rgb24"), self.metadata[idx]
            idx += 1

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self._container.close()

    def _parse_time(self, time_str: str) -> datetime:
        """Parses common EXIF and ISO time formats."""
        try:
            return datetime.strptime(time_str, "%Y:%m:%d %H:%M:%S")
        except ValueError:
            return datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")

    def _binary_search(self, target_dt: datetime, valid_indices: list[int]) -> int | None:
        """
        Performs binary search on valid indices.
        Returns the closest matching index into self.metadata,
        or None if a monotonicity violation is detected.
        """
        low = 0
        high = len(valid_indices) - 1

        def get_dt(idx_into_valid):
            return self._parse_time(self.metadata[valid_indices[idx_into_valid]]["time"])

        while low <= high:
            mid = (low + high) // 2
            try:
                dt_low = get_dt(low)
                dt_mid = get_dt(mid)
                dt_high = get_dt(high)

                if not (dt_low <= dt_mid <= dt_high):
                    return None  # Monotonicity violation
            except Exception as e:
                logger.warning(f"Error during binary search metadata access: {e}")
                return None

            if dt_mid < target_dt:
                low = mid + 1
            elif dt_mid > target_dt:
                high = mid - 1
            else:
                return valid_indices[mid]

        # Binary search finished without exact match; check neighbors
        candidates = []
        if 0 <= low < len(valid_indices):
            candidates.append(valid_indices[low])
        if 0 <= high < len(valid_indices):
            candidates.append(valid_indices[high])

        if not candidates:
            return None

        return min(
            candidates,
            key=lambda i: abs((self._parse_time(self.metadata[i]["time"]) - target_dt).total_seconds()),
        )

    def _linear_search(self, target_dt: datetime, valid_indices: list[int]) -> int:
        """Finds the closest matching index via linear scan."""
        return min(
            valid_indices,
            key=lambda i: abs((self._parse_time(self.metadata[i]["time"]) - target_dt).total_seconds()),
        )

    def get_frame_by_time(
        self, target_time: str | datetime, max_diff: float | None = None
    ) -> tuple[np.ndarray, dict, float]:
        """
        Finds the frame closest to the target real-world datetime.
        Uses binary search for O(log N) lookups on sorted data,
        with automatic fallback to linear search if non-monotonicity is detected.

        Args:
            target_time: The datetime to search for (string or datetime object).
            max_diff: Maximum allowed difference in seconds. Raises ValueError if exceeded.

        Returns:
            A tuple of (frame, metadata, time_diff_seconds).
        """
        if isinstance(target_time, str):
            target_dt = self._parse_time(target_time)
        else:
            target_dt = target_time

        # Filter indices with a valid 'time' field
        valid_indices = [i for i, meta in enumerate(self.metadata) if "time" in meta]
        if not valid_indices:
            raise ValueError("No frames with time metadata found in video.")

        best_idx = self._binary_search(target_dt, valid_indices)

        if best_idx is None:
            logger.warning("Non-monotonic timestamps detected; falling back to linear search.")
            best_idx = self._linear_search(target_dt, valid_indices)

        match_dt = self._parse_time(self.metadata[best_idx]["time"])
        time_diff = abs((match_dt - target_dt).total_seconds())

        if max_diff is not None and time_diff > max_diff:
            raise ValueError(f"Closest frame found is {time_diff:.2f}s away, exceeding max_diff of {max_diff}s.")

        frame, meta = self.get_frame(best_idx)
        return frame, meta, time_diff


class VideoImageSource(BaseImageSource):
    """
    Provides images from an existing TimelapseVideo.
    Useful for repair, re-encoding, or filtering.
    """

    def __init__(
        self,
        video: TimelapseVideo,
        indices: list[int] = None,
        skip_corrupted: bool = False,
    ):
        # We use the video path as the source string for SourceSpec
        spec = BaseImageSource.SourceSpec(src=video.path)
        super().__init__(spec)
        self.video = video
        self.indices = indices if indices is not None else list(range(len(video)))
        self.skip_corrupted = skip_corrupted

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get_timelapse_spec(self) -> TimelapseSpec:
        return TimelapseSpec(
            width=self.video.width,
            height=self.video.height,
            master_exif=self.video.master_exif,
        )

    def __iter__(self) -> Iterator[tuple[np.ndarray, dict]]:
        for idx in self.indices:
            try:
                yield self.video.get_frame(idx)
            except Exception as e:
                if self.skip_corrupted:
                    logger.warning(f"Skipping corrupted/missing frame {idx}: {e}")
                    continue
                else:
                    logger.error(f"Failed to access frame {idx}: {e}")
                    raise

    def __len__(self):
        return len(self.indices)
