import os
import shutil
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from mini_timelapse.compile import LocalImageSource, compile_video
from mini_timelapse.reader import TimelapseVideo
from mini_timelapse.repair import repair_video
from mini_timelapse.utils import parse_time

try:
    from tests.gen_test_images import generate_test_images
except ImportError:
    from gen_test_images import generate_test_images


def test_repair_sorting():
    """Verify that repair correctly re-orders frames based on time."""
    tmp_dir = tempfile.mkdtemp(prefix="repair_test_")
    try:
        src_dir = os.path.join(tmp_dir, "src")
        video_bad = os.path.join(tmp_dir, "bad.mkv")
        video_good = os.path.join(tmp_dir, "good.mkv")
        num_frames = 10
        generate_test_images(src_dir, num_images=num_frames)

        src_spec = LocalImageSource.SourceSpec(src=src_dir)
        with LocalImageSource(spec=src_spec) as source:
            source.files.reverse()
            compile_video(source, video_bad, fps=30)

        with TimelapseVideo(video_bad) as tv:
            times = [parse_time(m["time"]) for m in tv.metadata]
            assert times[0] > times[-1]

        repair_video(input_path=video_bad, output_path=video_good, fps=30)

        with TimelapseVideo(video_good) as tv:
            times = [parse_time(m["time"]) for m in tv.metadata]
            assert times[0] < times[-1]
            assert len(tv) == num_frames
    finally:
        shutil.rmtree(tmp_dir)


def test_repair_infer_metadata():
    """Verify metadata inference for videos without module metadata."""
    tmp_dir = tempfile.mkdtemp(prefix="repair_infer_")
    try:
        src_dir = os.path.join(tmp_dir, "src")
        video_no_meta = os.path.join(tmp_dir, "nometa.mkv")
        video_repaired = os.path.join(tmp_dir, "repaired.mkv")
        generate_test_images(src_dir, num_images=5)

        # Create a video WITHOUT metadata
        import subprocess

        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-nostdin",
                "-pattern_type",
                "glob",
                "-i",
                os.path.join(src_dir, "*.jpg"),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                video_no_meta,
            ],
            capture_output=True,
        )

        # Should fail without infer
        with patch("sys.exit", side_effect=SystemExit) as mock_exit:
            with pytest.raises(SystemExit):
                repair_video(video_no_meta, video_repaired, infer_metadata=False)
            mock_exit.assert_any_call(1)

        # Should succeed with infer
        repair_video(video_no_meta, video_repaired, infer_metadata=True)
        assert os.path.exists(video_repaired)
        with TimelapseVideo(video_repaired) as tv:
            assert len(tv) >= 5
            assert "time" in tv.metadata[0]
    finally:
        shutil.rmtree(tmp_dir)


def test_repair_normal():
    """Scenario 1: Verify normal video works without issues."""
    tmp_dir = tempfile.mkdtemp(prefix="repair_normal_")
    try:
        src_dir = os.path.join(tmp_dir, "src")
        video_in = os.path.join(tmp_dir, "in.mkv")
        video_out = os.path.join(tmp_dir, "out.mkv")
        generate_test_images(src_dir, num_images=5)

        src_spec = LocalImageSource.SourceSpec(src=src_dir)
        with LocalImageSource(src_spec) as source:
            compile_video(source, video_in, fps=30)

        repair_video(video_in, video_out, fps=30)
        assert os.path.exists(video_out)
        with TimelapseVideo(video_out) as tv:
            assert len(tv) == 5
    finally:
        shutil.rmtree(tmp_dir)


def test_repair_partial_metadata():
    """Scenario 2: Handle cases where some frames have no metadata."""
    tmp_dir = tempfile.mkdtemp(prefix="repair_partial_meta_")
    try:
        src_dir = os.path.join(tmp_dir, "src")
        video_in = os.path.join(tmp_dir, "in.mkv")
        video_out = os.path.join(tmp_dir, "out.mkv")
        num_frames = 5
        generate_test_images(src_dir, num_images=num_frames)

        src_spec = LocalImageSource.SourceSpec(src=src_dir)
        with LocalImageSource(src_spec) as source:
            compile_video(source, video_in, fps=30)

        # Mock TimelapseVideo to return partial metadata
        with patch("mini_timelapse.repair.TimelapseVideo") as mock_class:
            mock_video = MagicMock(spec=TimelapseVideo)
            mock_video.__enter__.return_value = mock_video
            mock_video.path = video_in
            mock_video.length = num_frames
            mock_video.__len__.return_value = num_frames
            mock_video.metadata_sources = {"attachment"}
            mock_video._fps = 30.0
            mock_video.width = 320
            mock_video.height = 240

            # Partial metadata: frame 3 has no 'time'
            meta = [{"index": i, "time": f"2023:01:01 12:00:{i:02d}"} for i in range(num_frames)]
            del meta[3]["time"]
            mock_video.metadata = meta
            mock_class.return_value = mock_video

            # Fail without skip_corrupted
            with patch("sys.exit", side_effect=SystemExit) as mock_exit:
                with pytest.raises(SystemExit):
                    repair_video(video_in, video_out)
                mock_exit.assert_any_call(1)

            # Success with skip_corrupted
            with patch("mini_timelapse.repair.compile_video") as mock_compile:
                repair_video(video_in, video_out, skip_corrupted=True)
                mock_compile.assert_called_once()
                source = mock_compile.call_args.kwargs["source"]
                assert len(source) == 4  # One frame skipped

    finally:
        shutil.rmtree(tmp_dir)


def test_repair_corrupted_frames():
    """Scenario 3 & 6: Handle truncated files or decode errors."""
    tmp_dir = tempfile.mkdtemp(prefix="repair_corrupt_frames_")
    try:
        src_dir = os.path.join(tmp_dir, "src")
        video_in = os.path.join(tmp_dir, "in.mkv")
        video_out = os.path.join(tmp_dir, "out.mkv")
        num_frames = 5
        generate_test_images(src_dir, num_images=num_frames)

        src_spec = LocalImageSource.SourceSpec(src=src_dir)
        with LocalImageSource(src_spec) as source:
            compile_video(source, video_in, fps=30)

        # Mock TimelapseVideo to throw on specific frame
        with patch("mini_timelapse.repair.TimelapseVideo") as mock_class:
            mock_video = MagicMock(spec=TimelapseVideo)
            mock_video.__enter__.return_value = mock_video
            mock_video.path = video_in
            mock_video.length = num_frames
            mock_video.__len__.return_value = num_frames
            mock_video.metadata = [{"index": i, "time": f"2023:01:01 12:00:{i:02d}"} for i in range(num_frames)]
            mock_video.metadata_sources = {"attachment", "subtitle"}

            # Frame 4 is "corrupted" (decode error)
            def get_frame_mock(idx):
                if idx == 4:
                    raise RuntimeError("Decode error")
                return (MagicMock(), mock_video.metadata[idx])

            mock_video.get_frame.side_effect = get_frame_mock
            mock_class.return_value = mock_video

            # Run repair with skip_corrupted=True
            with patch("mini_timelapse.repair.compile_video") as mock_compile:
                repair_video(video_in, video_out, skip_corrupted=True)
                mock_compile.assert_called_once()
                source = mock_compile.call_args.kwargs["source"]
                # Iterate the source to trigger the skips
                frames = list(source)
                assert len(frames) == 4

    finally:
        shutil.rmtree(tmp_dir)


def test_repair_real_truncation():
    """Scenario 7: Physically truncate an MKV and verify repair recovers intact frames."""
    tmp_dir = tempfile.mkdtemp(prefix="repair_real_trunc_")
    try:
        src_dir = os.path.join(tmp_dir, "src")
        video_full = os.path.join(tmp_dir, "full.mkv")
        video_repaired = os.path.join(tmp_dir, "repaired.mkv")
        num_frames = 1000  # Enough frames to ensure truncation hits the payload
        generate_test_images(src_dir, num_images=num_frames)

        # 1. Compile full video
        src_spec = LocalImageSource.SourceSpec(src=src_dir)
        with LocalImageSource(src_spec) as source:
            compile_video(source, video_full, fps=30)

        full_size = os.path.getsize(video_full)

        # 2. Truncate (keep only 60% of bytes)
        with open(video_full, "rb") as f_in:
            truncated_data = f_in.read(int(full_size * 0.5))
        with open(video_full, "wb") as f_out:
            f_out.write(truncated_data)

        # 3. Repair with skip_corrupted=True
        repair_video(video_full, video_repaired, skip_corrupted=True, fps=30)

        # 4. Verify repaired output
        assert os.path.exists(video_repaired)
        with TimelapseVideo(video_repaired) as tv:
            # Should have recovered some but not all frames
            # At 60% size, we should have lost some of the end
            assert 0 < len(tv) < num_frames
            assert "time" in tv.metadata[0]

    finally:
        shutil.rmtree(tmp_dir)


def test_repair_real_corruption():
    """Scenario 8: Physically corrupt video data and verify repair skips the damage."""
    tmp_dir = tempfile.mkdtemp(prefix="repair_real_corrupt_")
    try:
        src_dir = os.path.join(tmp_dir, "src")
        video_orig = os.path.join(tmp_dir, "orig.mkv")
        video_repaired = os.path.join(tmp_dir, "repaired.mkv")
        num_frames = 20
        generate_test_images(src_dir, num_images=num_frames)

        # 1. Compile original video
        src_spec = LocalImageSource.SourceSpec(src=src_dir)
        with LocalImageSource(src_spec) as source:
            compile_video(source, video_orig, fps=30)

        # 2. Corrupt a chunk in the middle (avoiding header)
        with open(video_orig, "r+b") as f:
            f.seek(int(os.path.getsize(video_orig) * 0.5))
            f.write(b"\xff" * 1024)  # Overwrite 1KB of data with garbage

        # 3. Repair with skip_corrupted=True
        repair_video(video_orig, video_repaired, skip_corrupted=True, fps=30)

        # 4. Verify repaired output
        assert os.path.exists(video_repaired)
        with TimelapseVideo(video_repaired) as tv:
            # Should have most frames, skipping only the corrupted segment
            assert 0 < len(tv) <= num_frames
            assert "time" in tv.metadata[0]

    finally:
        shutil.rmtree(tmp_dir)
