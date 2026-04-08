import json
import logging
import re
from typing import Any, Union

logger = logging.getLogger(__name__)

# ASS extradata required for MKV subtitle streams.
_ASS_EXTRADATA = (
    b"[Script Info]\n"
    b"ScriptType: v4.00+\n"
    b"PlayResX: 640\n"
    b"PlayResY: 360\n"
    b"\n"
    b"[V4+ Styles]\n"
    b"Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
    b"Style: Default,Arial,18,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,1,2,10,10,10,1\n"
    b"\n"
    b"[Events]\n"
    b"Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
)

def get_mkv_subtitle_header() -> bytes:
    """Returns the required byte header for Matroska ASS subtitle streams."""
    return _ASS_EXTRADATA

def format_ass_time(seconds: float) -> str:
    """Formats seconds into H:MM:SS.CC for ASS subtitles."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"

def encode_metadata_payload(index: int, meta: dict, fps: float = 30.0) -> bytes:
    """
    Formats a JSON dictionary into a strictly compliant MKV ASS event block.
    """
    json_str = json.dumps(meta, separators=(',', ':'))
    
    start_time = index / fps
    end_time = (index + 1) / fps
    ts_start = format_ass_time(start_time)
    ts_end = format_ass_time(end_time)
    
    # Visible metadata prefix for HUD in players
    prefix = f"Timestamp: {meta.get('time', 'N/A')} | File: {meta.get('filename', 'N/A')} | "
    
    # Format: ReadOrder, Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
    # Use standard ASS markers found in our decoding logic
    payload = f"{index},0,{ts_start},{ts_end},Default,,0,0,0,,{prefix}###METADATA_START###{json_str}###METADATA_END###\r\n"
    return payload.encode("utf-8")

def decode_metadata_payload(data: Union[bytes, str]) -> list[dict]:
    """
    Decodes one or more JSON metadata payloads from a subtitle event.
    Extremely robust: handles raw bytes, ASS lines, and merged content via regex.
    """
    if isinstance(data, bytes):
        text = data.decode("utf-8", errors="ignore")
    else:
        text = data
    
    # Use regex to find all matches between the markers
    pattern = r"###METADATA_START###(.*?)###METADATA_END###"
    matches = re.findall(pattern, text)
    
    results = []
    for match in matches:
        try:
            results.append(json.loads(match.strip()))
        except json.JSONDecodeError:
            pass
            
    return results