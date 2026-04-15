import argparse
import logging
import os
import sys
from datetime import datetime, timedelta

from mini_timelapse.compile import compile_video
from mini_timelapse.reader import TimelapseVideo, VideoImageSource
from mini_timelapse.utils import normalize_cli_args, parse_time

logger = logging.getLogger("timelapse-repair")


def repair_video(
    input_path: str,
    output_path: str | None = None,
    fps: float | None = None,
    quality: int = 23,
    preset: str = "medium",
    skip_corrupted: bool = False,
    infer_metadata: bool = False,
    force: bool = True,
) -> None:
    """
    Repair a damaged timelapse video by re-encoding frames in correct temporal order.

    Args:
        input_path: Path to the damaged .mkv or video file.
        output_path: Path for the repaired output video.
        fps: Optional framerate override.
        skip_corrupted: If True, skip frames that fail to decode or have missing metadata.
        infer_metadata: If True, infer timestamps for videos lacking module metadata.
        force: If True, skip interactive confirmation.
    """
    if not os.path.exists(input_path):
        logger.error(f"Input file not found: {input_path}")
        sys.exit(1)

    metadata_sources = set()

    logger.info(f"Opening video for repair: {input_path}")
    with TimelapseVideo(input_path, fps=fps, lazy=True, container_kwargs={"metadata_errors": "ignore"}) as video:
        num_frames = len(video)
        logger.debug(f"Video has {num_frames} frames")

        # --- Metadata Integrity Check ---
        print(video._extract_sovereign_metadata())
        attachment_indices = [i for i, m in video._extract_sovereign_metadata().items() if "time" in m]
        if attachment_indices:
            logger.debug("Validated metadata from attachment")
            metadata_sources.add("attachment")
        else:
            logger.debug("Could not validate metadata from attachment")

        # Check if we can extract metadata from subtitles
        subtitle_indices = []
        logger.debug("Attempting to load metadata from subtitles")
        for i in range(num_frames):
            if i % 1000 == 0:
                logger.debug(f"Processing frame {i}/{num_frames}")
            submeta = video._get_metadata(i, force_subtitle=True)
            if submeta and "time" in submeta:
                subtitle_indices.append(i)

        logger.debug(f"Found {len(subtitle_indices)} frames with metadata from subtitles")

        if subtitle_indices:
            logger.debug("Validated metadata from subtitles")
            metadata_sources.add("subtitle")
        else:
            logger.debug("Could not validate metadata from subtitles")

        valid_indices = list(set(attachment_indices) | set(subtitle_indices))

        if not metadata_sources:
            if not infer_metadata:
                logger.error("No timelapse metadata found in video!")
                logger.info("If this is a standard video, try using '--infer-metadata' to deduce timestamps.")
                sys.exit(1)
            else:
                logger.warning("No metadata found. Attempting to infer timestamps from video creation time...")
                # Inference logic: use container metadata or current time as fallback
                start_time = video._container.metadata.get("creation_time")
                if start_time:
                    try:
                        # ISO format usually: 2024-06-15T12:00:00.000000Z
                        base_dt = parse_time(start_time.replace("T", " ").split(".")[0])
                    except Exception:
                        base_dt = datetime.now()
                else:
                    base_dt = datetime.now()

                logger.info(f"Inferring start time: {base_dt.strftime('%Y-%m-%d %H:%M:%S')}")
                for i in range(num_frames):
                    dt = base_dt + timedelta(seconds=i / (fps or 30))
                    if i not in video.metadata:
                        video.metadata[i] = {}
                    video.metadata[i]["time"] = dt.strftime("%Y:%m:%d %H:%M:%S")
                    video.metadata[i]["index"] = i
                valid_indices = list(range(num_frames))

        # --- Redundancy check ---
        if "attachment" not in metadata_sources and "subtitle" not in metadata_sources:
            logger.warning("Metadata recovery relied solely on raw demuxing; reliability may be limited.")
        elif len(metadata_sources) < 2:
            source = list(metadata_sources)[0]
            logger.warning(f"Only one metadata source found ({source}). Redundancy layer is missing.")
        else:
            logger.debug(f"Metadata sources: {metadata_sources}")

        # --- Sorting ---
        # Sort valid indices by time
        attachment_metadata = video._extract_sovereign_metadata()
        valid_indices_with_time = [
            (i, tstr)
            for i in valid_indices
            if (tstr := video._get_metadata(i).get("time", attachment_metadata.get(i, {}).get("time", None)))
        ]
        sorted_indices = [i for i, tstr in sorted(valid_indices_with_time, key=lambda x: parse_time(x[1]))]

        if sorted_indices != list(range(num_frames)):
            logger.warning("Frames are out of order. Repair tool will re-order them.")

        if len(sorted_indices) < num_frames:
            msg = f"Video has {num_frames} frames, but only {len(sorted_indices)} have valid metadata."
            if not skip_corrupted:
                logger.error(msg)
                logger.info("Use '--skip-corrupted' to proceed with available frames.")
                sys.exit(1)
            else:
                logger.warning(f"{msg} Proceeding with available frames as requested.")

        # Interactive confirmation if not forced
        if not force and sys.stdin.isatty():
            try:
                response = input("\nProceed with repair? [Y/n]: ").strip().lower()
                if response and response != "y":
                    logger.info("Repair cancelled by user.")
                    return
            except EOFError:
                logger.info("Repair cancelled (EOF).")
                return

        # --- Compilation ---
        source = VideoImageSource(video, indices=sorted_indices, skip_corrupted=skip_corrupted)

        if output_path is None:
            name, ext = os.path.splitext(input_path)
            output_path = f"{name}_repaired{ext}"
        logger.info(f"Starting repair compilation: {len(source)} frames -> {output_path}")

        compile_video(source=source, output=output_path, fps=fps or 30, quality=quality, preset=preset)
        logger.info("Repair complete!")


def cli():
    parser = argparse.ArgumentParser(
        prog="timelapse-repair", description="Repair out-of-order or damaged timelapse videos by re-encoding them based on metadata."
    )
    parser.add_argument("-i", "--input", required=True, help="Path to damaged timelapse video.")
    parser.add_argument("-o", "--output", help="Path for the repaired output video. Defaults to input path with '_repaired' appended.")
    parser.add_argument("--fps", type=int, default=30, help="Playback framerate.")
    parser.add_argument("-q", "--quality", type=int, default=23, help="H.264 quality (CRF).")
    parser.add_argument("--preset", default="medium", help="x264 speed preset.")
    parser.add_argument("--skip-corrupted", action="store_true", help="Skip frames that fail to decode or have no metadata")
    parser.add_argument("--infer-metadata", action="store_true", help="Infer timestamps for standard videos")
    parser.add_argument("-f", "--force", action="store_true", help="Skip interactive confirmation")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    args = parser.parse_args(normalize_cli_args(sys.argv[1:]))
    return args


def main():
    args = cli()
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s: %(message)s")

    repair_video(
        input_path=args.input,
        output_path=args.output,
        fps=args.fps,
        quality=args.quality,
        preset=args.preset,
        skip_corrupted=args.skip_corrupted,
        infer_metadata=args.infer_metadata,
        force=args.force,
    )


if __name__ == "__main__":
    main()
