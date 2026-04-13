import os
import shutil
import tempfile
from unittest.mock import patch

import av

from mini_timelapse.compile import LocalImageSource, compile_video

try:
    from tests.gen_test_images import generate_test_images
except ImportError:
    from gen_test_images import generate_test_images


def test_compile_basic():
    """Verifies that compile_video produces a valid MKV from a set of images."""
    tmp_dir = tempfile.mkdtemp()
    try:
        src_dir = os.path.join(tmp_dir, "src")
        output_mkv = os.path.join(tmp_dir, "test.mkv")
        num_frames = 5
        generate_test_images(src_dir, num_images=num_frames)

        spec = LocalImageSource.SourceSpec(src=src_dir)
        with LocalImageSource(spec) as source:
            compile_video(source, output_mkv, fps=30)

        assert os.path.exists(output_mkv)

        # Verify attachment/subtitles with ffprobe or subprocess
        import subprocess

        probe_cmd = ["ffprobe", "-hide_banner", "-loglevel", "error", "-show_streams", "-show_format", output_mkv]
        probe_res = subprocess.run(probe_cmd, capture_output=True, text=True)
        assert "application/json" in probe_res.stdout or "metadata.json" in probe_res.stdout
        assert "subtitle" in probe_res.stdout

    finally:
        shutil.rmtree(tmp_dir)


def test_compile_skips_on_error():
    """Verifies that if _get_image_and_metadata fails, the frame is skipped."""
    tmp_dir = tempfile.mkdtemp()
    try:
        src_dir = os.path.join(tmp_dir, "src")
        output_mkv = os.path.join(tmp_dir, "skipped.mkv")
        num_frames = 5
        generate_test_images(src_dir, num_images=num_frames)

        spec = LocalImageSource.SourceSpec(src=src_dir)

        # Patch the logger and the method
        with patch("mini_timelapse.compile.logger") as mock_logger:
            with LocalImageSource(spec) as source:
                original_get = source._get_image_and_metadata

                # Get the first few files to see what they look like
                image_files = source.files
                target_file = image_files[2]  # 3rd file

                def mock_get(path):
                    if path == target_file:
                        raise RuntimeError("Simulated image corruption")
                    return original_get(path)

                with patch.object(LocalImageSource, "_get_image_and_metadata", side_effect=mock_get):
                    compile_video(source, output_mkv, fps=30)

                    # Verify warning was called on the logger
                    assert mock_logger.warning.called
                    assert any("Simulated image corruption" in str(arg) for call in mock_logger.warning.call_args_list for arg in call.args)

        assert os.path.exists(output_mkv)

        # Verify video has N-1 frames
        with av.open(output_mkv) as container:
            v_stream = next(s for s in container.streams if s.type == "video")
            count = 0
            for _ in container.decode(v_stream):
                count += 1
            assert count == num_frames - 1

    finally:
        shutil.rmtree(tmp_dir)
