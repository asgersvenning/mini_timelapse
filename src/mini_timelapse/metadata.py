import base64
import binascii
import json
import logging
import re

logger = logging.getLogger(__name__)

# ASS extradata required for MKV subtitle streams.
_ASS_EXTRADATA = (
    b"[Script Info]\n"
    b"ScriptType: v4.00+\n"
    b"PlayResX: 640\n"
    b"PlayResY: 360\n"
    b"\n"
    b"[V4+ Styles]\n"
    b"Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"  # noqa: E501
    b"Style: Default,Arial,18,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,1,2,10,10,10,0\n"
    b"\n"
    b"[Events]\n"
    b"Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
)


def get_mkv_subtitle_header() -> bytes:
    """Returns the required byte header for Matroska ASS subtitle streams."""
    return _ASS_EXTRADATA


def format_ass_time(seconds: float) -> str:
    """Formats seconds into H:MM:SS.CC for ASS subtitles."""
    # Convert to centiseconds and round to avoid float precision issues during string formatting
    cs = int(round(seconds * 100))
    s = (cs // 100) % 60
    m = (cs // 6000) % 60
    h = cs // 360000
    return f"{h}:{m:02d}:{s:02d}.{cs % 100:02d}"


def encode_metadata_payload(index: int, meta: dict, fps: float = 30.0) -> bytes:
    """
    Formats a JSON dictionary into a strictly compliant MKV ASS event block.
    """
    json_str = json.dumps(meta, separators=(",", ":"))

    start_time = index / fps
    end_time = (index + 1) / fps
    ts_start = format_ass_time(start_time)
    ts_end = format_ass_time(end_time)

    # Visible metadata prefix for HUD in players
    prefix = f"Timestamp: {meta.get('time', 'N/A')} | File: {meta.get('filename', 'N/A')} | "

    # Base64 encode the JSON to prevent ASS formatting tag interpretation (e.g., braces)
    json_b64 = base64.b64encode(json_str.encode("utf-8")).decode("ascii")

    # Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text (10 fields)
    payload = f"0,{ts_start},{ts_end},Default,,0,0,0,,{prefix}###METADATA_START###{json_b64}###METADATA_END###"
    return payload.encode("utf-8")


def decode_metadata_payload(data: bytes | str) -> list[dict]:
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
        match = match.strip()
        try:
            # Try Base64 first (new format)
            try:
                decoded = base64.b64decode(match).decode("utf-8")
                results.append(json.loads(decoded))
                continue
            except (ValueError, binascii.Error) if "binascii" in globals() else Exception:
                pass

            # Fallback to raw JSON (old format)
            results.append(json.loads(match))
        except json.JSONDecodeError:
            pass

    return results
