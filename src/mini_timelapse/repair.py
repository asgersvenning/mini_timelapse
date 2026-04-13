import argparse
import logging
import os
import sys
from datetime import datetime, timedelta

from mini_timelapse.compile import compile_video
from mini_timelapse.reader import TimelapseVideo, VideoImageSource
from mini_timelapse.utils import normalize_cli_args

logger = logging.getLogger("timelapse-repair")


def repair_video(
    input_path: str,
    output_path: str,
    fps: int = 30,
    quality: int = 23,
    preset: str = "medium",
    skip_corrupted: bool = False,
    infer_metadata: bool = False,
):
    """
    Repairs a timelapse video by re-encoding frames in the correct temporal order.
    """
    if not os.path.exists(input_path):
        logger.error(f"Input file not found: {input_path}")
        sys.exit(1)

    logger.info(f"Opening video for repair: {input_path}")
    with TimelapseVideo(input_path, fps=float(fps)) as video:
        num_frames = len(video)
        
        # --- Metadata Integrity Check ---
        valid_indices = [i for i, m in enumerate(video.metadata) if "time" in m]
        
        if not valid_indices:
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
                        base_dt = video._parse_time(start_time.replace("T", " ").split(".")[0])
                    except Exception:
                        base_dt = datetime.now()
                else:
                    base_dt = datetime.now()
                
                logger.info(f"Inferring start time: {base_dt.strftime('%Y-%m-%d %H:%M:%S')}")
                for i in range(num_frames):
                    dt = base_dt + timedelta(seconds=i / fps)
                    video.metadata[i]["time"] = dt.strftime("%Y:%m:%d %H:%M:%S")
                    video.metadata[i]["index"] = i
                valid_indices = list(range(num_frames))

        # --- Redundancy check ---
        if "attachment" not in video.metadata_sources and "subtitle" not in video.metadata_sources:
             logger.warning("Metadata recovery relied solely on raw demuxing; reliability may be limited.")
        elif len(video.metadata_sources) < 2:
             source = list(video.metadata_sources)[0]
             logger.warning(f"Only one metadata source found ({source}). Redundancy layer is missing.")

        # --- Sorting ---
        # Sort valid indices by time
        sorted_indices = sorted(valid_indices, key=lambda i: video._parse_time(video.metadata[i]["time"]))
        
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

        # --- Compilation ---
        source = VideoImageSource(video, indices=sorted_indices, skip_corrupted=skip_corrupted)
        
        logger.info(f"Starting repair compilation: {len(sorted_indices)} frames -> {output_path}")
        compile_video(
            source=source,
            output=output_path,
            fps=fps,
            quality=quality,
            preset=preset
        )
        logger.info("Repair complete!")


def cli():
    parser = argparse.ArgumentParser(
        prog="timelapse-repair",
        description="Repair out-of-order or damaged timelapse videos by re-encoding them based on metadata."
    )
    parser.add_argument("-i", "--input", required=True, help="Path to damaged timelapse video.")
    parser.add_argument("-o", "--output", help="Path for the repaired output video.")
    parser.add_argument("--fps", type=int, default=30, help="Playback framerate.")
    parser.add_argument("-q", "--quality", type=int, default=23, help="H.264 quality (CRF).")
    parser.add_argument("--preset", default="medium", help="x264 speed preset.")
    parser.add_argument("--skip-corrupted", action="store_true", help="Skip frames that cannot be decoded or have missing metadata.")
    parser.add_argument("--infer-metadata", action="store_true", help="Duce metadata from video creation time if missing.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    
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
        infer_metadata=args.infer_metadata
    )


if __name__ == "__main__":
    main()
