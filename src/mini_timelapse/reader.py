import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime

import av
import numpy as np

from mini_timelapse.metadata import decode_metadata_payload
from mini_timelapse.utils import BaseImageSource, TimelapseSpec, parse_time

logger = logging.getLogger(__name__)


class TimelapseVideo:
    """
    A high-level interface for reading and decompiling timelapse videos
    with frame-accurate metadata recovery via Just-In-Time (JIT) extraction.
    """

    def __init__(self, path: str, fps: float = 30.0, lazy: bool = False, container_kwargs: dict | None = None):
        self.path = path
        self._fps = fps if fps is not None else 30.0
        self._container = av.open(path, **(container_kwargs or {}))
        self._video_stream = next((s for s in self._container.streams if s.type == "video"), None)
        if not self._video_stream:
            raise ValueError(f"No video stream found in {path}")

        self.width = self._video_stream.width
        self.height = self._video_stream.height
        self.length = self._video_stream.frames

        if self.length == 0:
            if self._video_stream.duration is not None:
                self.length = int(round(float(self._video_stream.duration * self._video_stream.time_base) * self._fps))
            elif self._container.duration is not None:
                self.length = int(round(float(self._container.duration / av.time_base) * self._fps))
            else:
                self.length = 0

        if lazy:
            self.metadata = {}
        else:
            self.metadata = self._extract_sovereign_metadata()

    def __len__(self):
        return self.length

    def _extract_sovereign_metadata(self):
        """Attempts to load the Matroska JSON attachment instantly."""
        metadata = dict()
        try:
            # 1. Probe for attachments to validate existence and avoid opaque blocking
            probe_cmd = ["ffprobe", "-hide_banner", "-loglevel", "quiet", "-print_format", "json", "-show_streams", self.path]

            logger.debug(f"Probing {self.path} for attachments...")
            probe_result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
            probe_data = json.loads(probe_result.stdout)

            attachments = [stream for stream in probe_data.get("streams", []) if stream.get("codec_type") == "attachment"]

            if not attachments:
                logger.debug(f"No attachments found in {self.path}. Skipping extraction.")
                return metadata

            # Log useful information about the attachment we are targeting
            target_attachment = attachments[0]
            tags = target_attachment.get("tags", {})
            attached_filename = tags.get("filename", "unknown_filename")
            mimetype = tags.get("mimetype", "unknown_mimetype")

            logger.debug(f"Found attachment '{attached_filename}' ({mimetype}). Proceeding with optimized extraction...")

            # 2. Optimized extraction (skipping video/audio/sub decoding)
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_json = os.path.join(tmp_dir, "extracted_meta.json")
                cmd = [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-y",
                    "-dump_attachment:t:0",
                    tmp_json,
                    "-i",
                    self.path,
                    "-map",
                    "0:t:0",  # Give the null output exactly one stream (the attachment)
                    "-c",
                    "copy",  # Tell ffmpeg not to decode anything, just copy
                    "-f",
                    "null",
                    "-",
                ]

                subprocess.run(cmd, timeout=None, check=True, capture_output=True)

                if os.path.exists(tmp_json):
                    with open(tmp_json) as f:
                        attachment_data = json.load(f)

                    if isinstance(attachment_data, list):
                        logger.info(f"Loaded {len(attachment_data)} metadata entries from Matroska attachment.")
                        for item in attachment_data:
                            if "index" in item and 0 <= int(item["index"]) < self.length:
                                metadata[int(item["index"])] = item

        except Exception as e:
            logger.debug(f"Attachment extraction missing or failed, defaulting to JIT subtitle reading: {e}")

        return metadata

    def _get_metadata(self, index: int, force_subtitle: bool = False) -> dict:
        """
        JIT (Just-In-Time) Lazy Loader for metadata.
        If the attachment failed, it seeks the subtitle stream exactly to the target frame.
        """
        if not force_subtitle and index in self.metadata:
            return self.metadata[index]

        sub_stream = next((s for s in self._container.streams if s.type == "subtitle"), None)
        if not sub_stream:
            return {}

        pts = int(round(index * 1000 / self._fps))
        try:
            self._container.seek(pts, stream=sub_stream)
        except Exception as e:
            logger.debug(f"Failed to seek subtitle stream for metadata: {e}")
            return {}

        for packet in self._container.demux(sub_stream):
            if packet.pts is None:
                continue

            packet_idx = int(round(packet.pts * packet.time_base * self._fps))
            if packet_idx == index:
                text_data = bytes(packet).decode("utf-8", errors="ignore")
                items = decode_metadata_payload(text_data)

                if items:
                    self.metadata[index] = items[0]
                    return items[0]
            elif packet_idx > index:
                break

        if not force_subtitle:
            self.metadata[index] = {}
        return {}

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
                    "-nostdin",
                    "-y",
                    "-dump_attachment:t",
                    tmp_exif,
                    "-i",
                    self.path,
                    "-f",
                    "null",
                    "-",
                ]
                subprocess.run(cmd, timeout=None, capture_output=True)

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

        # 1. Fetch metadata lazily
        meta = self._get_metadata(index)

        # 2. Fetch video frame
        pts = int(round(index * 1000 / self._fps))
        self._container.seek(pts, stream=self._video_stream)

        for frame in self._container.decode(self._video_stream):
            frame_idx = int(round(frame.pts * frame.time_base * self._fps))
            if frame_idx == index:
                return frame.to_ndarray(format="rgb24"), meta
            elif frame_idx > index:
                logger.warning(f"Missed exact frame {index}, returning frame {frame_idx}")
                # Lazy-load the missed frame's metadata to prevent silent misalignment
                missed_meta = self._get_metadata(frame_idx)
                return frame.to_ndarray(format="rgb24"), missed_meta

        raise RuntimeError(f"Could not seek to frame {index}")

    def __getitem__(self, key: int | slice):
        if isinstance(key, int):
            return self.get_frame(key)
        elif isinstance(key, slice):
            indices = range(*key.indices(self.length))
            return [self.get_frame(i) for i in indices]
        else:
            raise TypeError(f"Invalid argument type: {type(key)}")

    def __iter__(self):
        self._container.seek(0)
        idx = 0
        for frame in self._container.decode(self._video_stream):
            if idx >= self.length:
                break
            yield frame.to_ndarray(format="rgb24"), self._get_metadata(idx)
            idx += 1

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self._container.close()

    def __del__(self):
        if hasattr(self, "_container") and self._container is not None:
            self.close()

    def _binary_search(self, target_dt: datetime, valid_indices: list[int]) -> int | None:
        low = 0
        high = len(valid_indices) - 1

        def get_dt(idx_into_valid):
            meta = self._get_metadata(valid_indices[idx_into_valid])
            if "time" not in meta:
                raise ValueError(f"No time data at frame {valid_indices[idx_into_valid]}")
            return parse_time(meta["time"])

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

        candidates = []
        if 0 <= low < len(valid_indices):
            candidates.append(valid_indices[low])
        if 0 <= high < len(valid_indices):
            candidates.append(valid_indices[high])

        if not candidates:
            return None

        # Return candidate closest to target time
        return min(
            candidates,
            key=lambda i: abs((parse_time(self._get_metadata(i)["time"]) - target_dt).total_seconds()),
        )

    def _linear_search(self, target_dt: datetime, valid_indices: list[int]) -> int:
        # Fallback linearly checks all given indices.
        def diff(i):
            meta = self._get_metadata(i)
            if "time" not in meta:
                return float("inf")
            return abs((parse_time(meta["time"]) - target_dt).total_seconds())

        return min(valid_indices, key=diff)

    def get_frame_by_time(self, target_time: str | datetime, max_diff: float | None = None) -> tuple[np.ndarray, dict, float]:
        if isinstance(target_time, str):
            target_dt = parse_time(target_time)
        else:
            target_dt = target_time

        # Assume all frame indices are valid candidates for search
        valid_indices = list(range(self.length))

        best_idx = self._binary_search(target_dt, valid_indices)

        if best_idx is None:
            logger.warning("Non-monotonic timestamps detected; falling back to linear search.")
            best_idx = self._linear_search(target_dt, valid_indices)

        match_meta = self._get_metadata(best_idx)
        if "time" not in match_meta:
            raise ValueError("Closest frame found lacks time metadata.")

        match_dt = parse_time(match_meta["time"])
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
        spec = BaseImageSource.SourceSpec(src=video.path)
        super().__init__(spec)
        self.video = video
        self.indices = indices if indices is not None else list(range(len(video)))
        self.skip_corrupted = skip_corrupted

    @property
    def elements(self):
        return self.indices

    def get_timelapse_spec(self) -> TimelapseSpec:
        return TimelapseSpec(
            width=self.video.width,
            height=self.video.height,
            master_exif=self.video.master_exif,
        )

    def _get_image_and_metadata(self, idx: int):
        # If skip_corrupted is False, we let exceptions bubble up naturally.
        # Note: Depending on your exact implementation of BaseImageSource, you may
        # want to ensure the base class doesn't suppress this exception if skip_corrupted is False.
        try:
            return self.video.get_frame(idx)
        except Exception as e:
            if not self.skip_corrupted:
                logger.error(f"Failed to access frame {idx}: {e}")
                raise
            # If skipping, raising a generic exception signals the base class to skip
            raise RuntimeError(f"Corrupted frame {idx}") from e

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass
