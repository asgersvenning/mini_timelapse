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
    Uses a Dual-Layer payload (Visible HUD + Hidden Base64).
    """
    json_str = json.dumps(meta, separators=(",", ":"))
    json_b64 = base64.b64encode(json_str.encode("utf-8")).decode("ascii")

    # 1. VISIBLE HUD LAYER
    hud_lines = [f"Time: {meta.get('time', 'N/A')}", f"File: {meta.get('filename', 'N/A')}"]
    if "lat" in meta and "lon" in meta:
        hud_lines.append(f"GPS: {meta['lat']}, {meta['lon']}")

    # Anchor the HUD to the Top-Left corner using ASS override tags {\an7}
    # This keeps the text out of the center of your scientific imagery
    visible_hud = r"{\an7}" + r"\N".join(hud_lines)

    # 2. INVISIBLE MACHINE LAYER
    invisible_payload = f"{{_meta:{json_b64}}}"

    text_payload = f"{visible_hud}{invisible_payload}"

    # MKV AV_CODEC_ID_ASS Format:
    # ReadOrder, Layer, Style, Name, MarginL, MarginR, MarginV, Effect, Text
    # We use your frame 'index' as the ReadOrder to guarantee sequential rendering
    payload = f"{index},0,Default,,0,0,0,,{text_payload}"

    return payload.encode("utf-8")


def decode_metadata_payload(data: bytes | str) -> list[dict]:
    """
    Decodes one or more JSON metadata payloads from a subtitle event.
    Backwards compatible with the legacy ###METADATA_START### markers.
    """
    if isinstance(data, bytes):
        text = data.decode("utf-8", errors="ignore")
    else:
        text = data

    results = []

    # 1. Look for the new ASS-hidden format first: {_meta:BASE64}
    hidden_matches = re.findall(r"\{_meta:(.*?)\}", text)

    # 2. Look for the legacy format: ###METADATA_START###BASE64###METADATA_END###
    legacy_matches = re.findall(r"###METADATA_START###(.*?)###METADATA_END###", text)

    for match in hidden_matches + legacy_matches:
        match = match.strip()
        try:
            # Decode Base64 (Both new and legacy formats use this)
            decoded = base64.b64decode(match).decode("utf-8")
            results.append(json.loads(decoded))
        except (ValueError, binascii.Error, json.JSONDecodeError):
            # Fallback in case a legacy marker contained raw unencoded JSON
            try:
                results.append(json.loads(match))
            except json.JSONDecodeError:
                logger.debug("Failed to decode a matched metadata block.")

    return results
