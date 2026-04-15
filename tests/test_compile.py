import os
import shutil
import tempfile
from unittest.mock import patch

from mini_timelapse.compile import LocalImageSource, compile_video
from mini_timelapse.reader import TimelapseVideo

try:
    from tests.gen_test_images import generate_test_images
except ImportError:
    from gen_test_images import generate_test_images


def test_compile_basic(tmp_path):
    """Verifies that compile_video produces a valid MKV from a set of images."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    output_mkv = str(tmp_path / "test.mkv")
    num_frames = 5
    generate_test_images(str(src_dir), num_images=num_frames)

    spec = LocalImageSource.SourceSpec(src=str(src_dir))
    with LocalImageSource(spec) as source:
        compile_video(source, output_mkv, fps=30)

    assert os.path.exists(output_mkv)

    # Verify attachment/subtitles with ffprobe or subprocess
    import subprocess

    probe_cmd = ["ffprobe", "-hide_banner", "-loglevel", "error", "-show_streams", "-show_format", output_mkv]
    probe_res = subprocess.run(probe_cmd, capture_output=True, text=True)
    assert "application/json" in probe_res.stdout or "metadata.json" in probe_res.stdout
    assert "subtitle" in probe_res.stdout


# 1. Inject Pytest's built-in caplog fixture
def test_compile_skips_on_error(tmp_path, caplog):
    """Verifies that if _get_image_and_metadata fails, the frame is skipped."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    output_mkv = str(tmp_path / "skipped.mkv")
    num_frames = 5

    generate_test_images(str(src_dir), num_images=num_frames)

    spec = LocalImageSource.SourceSpec(src=str(src_dir))

    with LocalImageSource(spec) as source:
        original_get = LocalImageSource._get_image_and_metadata
        target_file = source.files[2]

        def mock_get(self, path, *args, **kwargs):
            if path == target_file:
                raise RuntimeError("Simulated image corruption")
            return original_get(self, path, *args, **kwargs)

        with patch.object(LocalImageSource, "_get_image_and_metadata", autospec=True, side_effect=mock_get):
            compile_video(source, output_mkv, fps=30)

    # 2. Check the globally captured logs instead of a mock
    assert any("Simulated image corruption" in record.message for record in caplog.records)
    assert any(record.levelname == "WARNING" for record in caplog.records)

    assert os.path.exists(output_mkv)

    with TimelapseVideo(output_mkv) as tv:
        assert len(tv) == num_frames - 1
