import av
import json
import os
from typing import Any, Generator

class TimelapseVideo:
    """
    A unified wrapper for a 1:1 timelapse video with embedded JSON metadata.
    Provides sequence-like frame access and per-frame metadata extraction.
    """
    def __init__(self, path: str):
        """
        Initialize the TimelapseVideo by opening the video container
        and extracting metadata.
        
        Args:
            path (str): File path to the video (e.g., .mkv or .mp4).
        """
        if not os.path.exists(path):
            raise FileNotFoundError(f"Video file not found: {path}")
            
        self.path = path
        self._container = av.open(path)
        
        # Verify streams
        if not self._container.streams.video:
            raise ValueError(f"Video file {path} has no video stream.")
        
        self._video_stream = self._container.streams.video[0]
        self.length = self._video_stream.frames
        
        # If frame count is 0 (sometimes happens with certain containers/codecs), 
        # we might need to count manually, but for mkv/h264 it's usually reliable.
        if self.length == 0:
            # Fallback: decode once to count (slow but reliable)
            self.length = sum(1 for _ in self._container.decode(video=0))
            self._container.seek(0)
            
        self.metadata = self._extract_metadata()
        
        if len(self.metadata) > 0 and len(self.metadata) != self.length:
            print(f"Warning: Frame count ({self.length}) does not match metadata count ({len(self.metadata)})")
            self.length = min(self.length, len(self.metadata))

    def _extract_metadata(self) -> list[dict[str, Any]]:
        """
        Extract JSON metadata interleaved in the video's subtitle stream or global metadata.
        
        Returns:
            list[dict[str, Any]]: A list of metadata dictionaries, one per frame.
        """
        # 1. Try legacy video stream tag
        json_data = self._video_stream.metadata.get("JSON_METADATA")
        if json_data:
            try:
                return json.loads(json_data)
            except json.JSONDecodeError:
                pass

        # 2. Try interleaved subtitle stream
        metadata = []
        sub_stream = None
        for s in self._container.streams.subtitles:
            if s.metadata.get("TITLE") == "JSON_METADATA" or len(self._container.streams.subtitles) == 1:
                sub_stream = s
                break
        
        if sub_stream:
            # We must demux/decode to get the packets. 
            # Subtitle packets are usually very small, so this is fast.
            # We seek to the beginning first.
            self._container.seek(0)
            # Use demux to get packets specifically for the subtitle stream
            for packet in self._container.demux(sub_stream):
                if packet.pts is None:
                    continue
                try:
                    # Packet data for 'ass' is a bytes string
                    data = bytes(packet).decode("utf-8")
                    # Remove potential SRT formatting if any (though we wrote raw JSON)
                    if "-->" in data:
                        # Simple SRT parser fallback if needed, 
                        # but our compile.py writes raw JSON.
                        pass
                    
                    # Try to parse the whole packet as JSON
                    meta_item = json.loads(data)
                    metadata.append(meta_item)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
            
            # Reset container for subsequent video decoding
            self._container.seek(0)

        return metadata

    def get_frame(self, idx: int):
        """
        Retrieve a specific frame by its index.
        
        Args:
            idx (int): The 0-based frame index to retrieve.
            
        Returns:
            tuple: A tuple containing (rgb_ndarray, metadata_dict).
            
        Raises:
            IndexError: If the index is out of bounds.
            RuntimeError: If the frame could not be located.
        """
        if idx < 0 or idx >= self.length:
            raise IndexError(f"Frame index {idx} out of bounds (0 - {self.length-1})")
            
        # For random access, seek to the target index.
        # Video streams in MKV are usually indexed.
        # Seek to the target timestamp.
        time_base = self._video_stream.time_base
        # In a 1:1 timelapse, usually timestamp = idx / fps
        # Or better: use the average frame rate
        fps = self._video_stream.average_rate
        target_pts = int(idx * (time_base.denominator / (time_base.numerator * fps)))
        
        # Seek to the nearest keyframe at or before target_pts
        self._container.seek(target_pts, stream=self._video_stream)
        
        # Decode until we reach the exact frame index
        last_frame = None
        for frame in self._container.decode(video=0):
            # We calculate current index from PTS
            current_idx = int(round(frame.pts * time_base.numerator * fps / time_base.denominator))
            if current_idx == idx:
                last_frame = frame
                break
            elif current_idx > idx:
                # We missed it or it doesn't exist? (Shouldn't happen on 1:1)
                break
        
        if last_frame is None:
            raise RuntimeError(f"Could not find frame at index {idx}")
            
        frame_ndarray = last_frame.to_ndarray(format="rgb24")
        meta = self.metadata[idx] if idx < len(self.metadata) else {}
        return frame_ndarray, meta

    def __getitem__(self, key):
        if isinstance(key, slice):
            start, stop, step = key.indices(self.length)
            # This is slow for large slices, but works.
            return [self.get_frame(i) for i in range(start, stop, step)]
        elif isinstance(key, int):
            if key < 0:
                key += self.length
            return self.get_frame(key)
        else:
            raise TypeError(f"Invalid argument type: {type(key)}")

    def __len__(self):
        return self.length

    def __iter__(self) -> Generator[tuple[Any, dict[str, Any]], None, None]:
        """
        Iterate through all frames in the video sequentially.
        
        Yields:
            tuple: A tuple containing (rgb_ndarray, metadata_dict).
        """
        self._container.seek(0)
        idx = 0
        for frame in self._container.decode(video=0):
            if idx >= self.length:
                break
            meta = self.metadata[idx] if idx < len(self.metadata) else {}
            yield frame.to_ndarray(format="rgb24"), meta
            idx += 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        """Close the underlying video container."""
        if hasattr(self, '_container'):
            self._container.close()
