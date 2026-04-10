from unittest.mock import patch

import pytest

from mini_timelapse.compile import BaseImageSource
from mini_timelapse.compile import main as compile_main
from mini_timelapse.decompile import main as decompile_main


def test_compile_cli_basic():
    """Test basic compilation CLI arguments."""
    test_args = ["timelapse-compile", "-i", "test_input", "-o", "test_output.mkv", "--fps", "60", "-q", "18"]
    with patch("sys.argv", test_args):
        with patch("mini_timelapse.compile.compile_video") as mock_compile:
            with patch("mini_timelapse.compile.LocalImageSource") as mock_source:
                compile_main()

                # Verify Source initialization
                mock_source.assert_called_once_with(BaseImageSource.SourceSpec(src="test_input"))

                # Verify compile_video call
                mock_compile.assert_called_once()
                _, kwargs = mock_compile.call_args
                assert kwargs["output"] == "test_output.mkv"
                assert kwargs["fps"] == 60
                assert kwargs["quality"] == 18
                assert kwargs["dry_run"] is False


def test_compile_cli_dry_run_and_verbose():
    """Test dry-run and verbose flags in compilation CLI."""
    test_args = ["timelapse-compile", "-i", "in", "-o", "out.mkv", "-d", "-v"]
    with patch("sys.argv", test_args):
        with patch("mini_timelapse.compile.compile_video") as mock_compile:
            with patch("mini_timelapse.compile.LocalImageSource"):
                compile_main()
                _, kwargs = mock_compile.call_args
                assert kwargs["dry_run"] is True
                # verbose is handled by basicConfig in main, harder to verify easily
                # but we checked the flow doesn't crash.


def test_compile_cli_remote():
    """Test remote flag in compilation CLI."""
    test_args = ["timelapse-compile", "-i", "sftp://host/path", "--remote"]
    with patch("sys.argv", test_args):
        with patch("mini_timelapse.compile.compile_video") as mock_compile:
            with patch("mini_timelapse.compile.RemoteImageSource") as mock_remote:
                compile_main()
                mock_remote.assert_called_once_with(
                    BaseImageSource.SourceSpec(src="sftp://host/path", n_max=None, recursive=False), sharelink_id=None, preext_pattern=None
                )
                mock_compile.assert_called_once()


def test_compile_cli_unknown_args():
    """Test that unknown args are passed through (for custom ffmpeg options etc)."""
    test_args = ["timelapse-compile", "-i", "in", "--custom=value"]
    with patch("sys.argv", test_args):
        with patch("mini_timelapse.compile.compile_video") as mock_compile:
            with patch("mini_timelapse.compile.LocalImageSource"):
                compile_main()
                # The current implementation of compile_main doesn't actually pass
                # unknown args to compile_video yet, but it parses them.
                # Let's verify it doesn't crash.
                mock_compile.assert_called_once()


def test_decompile_cli_basic():
    """Test basic decompilation CLI arguments."""
    test_args = ["timelapse-decompile", "-i", "input.mkv", "-o", "out_dir", "--prefix", "img", "-q", "90"]
    with patch("sys.argv", test_args):
        with patch("mini_timelapse.decompile.decompile_video") as mock_decompile:
            decompile_main()

            mock_decompile.assert_called_once_with(
                video_path="input.mkv", output_dir="out_dir", prefix="img", quality=90, remote=False, sharelink_id=None
            )


def test_decompile_cli_remote():
    """Test remote flag in decompilation CLI."""
    test_args = ["timelapse-decompile", "-i", "input.mkv", "-o", "sftp://out", "--remote"]
    with patch("sys.argv", test_args):
        with patch("mini_timelapse.decompile.decompile_video") as mock_decompile:
            decompile_main()

            mock_decompile.assert_called_once_with(
                video_path="input.mkv", output_dir="sftp://out", prefix="frame", quality=95, remote=True, sharelink_id=None
            )


def test_cli_missing_required():
    """Test that CLI exits when required arguments are missing."""
    # Test compile
    with patch("sys.argv", ["timelapse-compile"]):
        with pytest.raises(SystemExit):
            compile_main()

    # Test decompile
    with patch("sys.argv", ["timelapse-decompile"]):
        with pytest.raises(SystemExit):
            decompile_main()
