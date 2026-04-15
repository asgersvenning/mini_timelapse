import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from mini_timelapse.compile import LocalImageSource, compile_video
from mini_timelapse.reader import TimelapseVideo
from mini_timelapse.utils import parse_time

# Add project root and tests to sys.path
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

try:
    from tests.gen_test_images import generate_test_images
except ImportError:
    from gen_test_images import generate_test_images

NUM_FRAMES = 250


@pytest.fixture(scope="module")
def timelapse_video_data(tmp_path_factory):
    """
    Module-scoped fixture.
    Generates the 250-frame video exactly ONCE for this entire file
    to keep tests fast, while providing isolated environments.
    """
    # tmp_path_factory is required for module-scoped temporary directories
    tmp_dir = tmp_path_factory.mktemp("reader_test_env")
    src_dir = tmp_dir / "src"
    src_dir.mkdir()

    video_path = str(tmp_dir / "test.mkv")

    # 1. Setup: Generate images and compile
    expected_meta = generate_test_images(str(src_dir), num_images=NUM_FRAMES)

    src_spec = LocalImageSource.SourceSpec(src=str(src_dir))
    with LocalImageSource(spec=src_spec) as source:
        compile_video(source, video_path, fps=30, quality=23, preset="ultrafast")

    return video_path, expected_meta


def test_reader_iteration(timelapse_video_data):
    video_path, expected_meta = timelapse_video_data

    with TimelapseVideo(video_path) as tv:
        assert len(tv) == NUM_FRAMES

        count = 0
        for i, (frame, meta) in enumerate(tv):
            assert meta["index"] == i
            assert meta["filename"] == os.path.basename(expected_meta[i]["path"])
            count += 1

        assert count == NUM_FRAMES


def test_reader_indexing(timelapse_video_data):
    video_path, _ = timelapse_video_data

    with TimelapseVideo(video_path) as tv:
        # Positive
        _, m0 = tv[0]
        assert m0["index"] == 0

        _, m_last = tv[NUM_FRAMES - 1]
        assert m_last["index"] == NUM_FRAMES - 1

        # Negative
        _, mn1 = tv[-1]
        assert mn1["index"] == NUM_FRAMES - 1

        _, mn_len = tv[-NUM_FRAMES]
        assert mn_len["index"] == 0

        # Bounds checking
        with pytest.raises(IndexError):
            _ = tv[NUM_FRAMES]

        with pytest.raises(IndexError):
            _ = tv[-(NUM_FRAMES + 1)]


def test_reader_slicing(timelapse_video_data):
    video_path, _ = timelapse_video_data

    with TimelapseVideo(video_path) as tv:
        # Simple slice
        subset = tv[100:150]
        assert len(subset) == 50
        assert subset[0][1]["index"] == 100
        assert subset[-1][1]["index"] == 149

        # Step slice
        stepped = tv[0:250:50]
        assert len(stepped) == 5
        indices = [m["index"] for _, m in stepped]
        assert indices == [0, 50, 100, 150, 200]

        # Empty slice
        empty = tv[150:100]
        assert len(empty) == 0

        # Slice with negative indices
        neg_slice = tv[-10:]
        assert len(neg_slice) == 10
        assert neg_slice[0][1]["index"] == 240


def test_reader_get_frame_by_time(timelapse_video_data):
    video_path, _ = timelapse_video_data

    with TimelapseVideo(video_path) as tv:
        actual_time_str = tv.metadata[123]["time"]

        # Exact match
        frame, meta, diff = tv.get_frame_by_time(actual_time_str, max_diff=0)
        assert meta["index"] == 123
        assert diff == 0
        assert frame.shape == (240, 320, 3)

        # Closest match
        dt_base = parse_time(actual_time_str)
        dt_offset = dt_base + timedelta(seconds=5)
        frame, meta, diff = tv.get_frame_by_time(dt_offset)
        assert meta["index"] == 123
        assert diff == 5.0

        # Max diff exception
        with pytest.raises(ValueError, match="exceeding max_diff of 2.0s"):
            tv.get_frame_by_time(dt_offset, max_diff=2.0)

        # Unsorted fallback
        orig_meta = tv.metadata.copy()
        try:
            tv.metadata[10], tv.metadata[20] = tv.metadata[20], tv.metadata[10]
            frame, meta, diff = tv.get_frame_by_time(actual_time_str)
            assert meta["index"] == 123
            assert diff == 0
        finally:
            tv.metadata = orig_meta


def test_reader_private_search_methods(timelapse_video_data):
    video_path, _ = timelapse_video_data

    with TimelapseVideo(video_path) as tv:
        valid_indices = list(range(NUM_FRAMES))

        # Test all frames for robustness
        for ti in range(NUM_FRAMES):
            tt = parse_time(tv.metadata[ti]["time"])
            assert tv._binary_search(tt, valid_indices) == ti
            assert tv._linear_search(tt, valid_indices) == ti

            # Closest neighbor
            tt_plus = tt + timedelta(seconds=1)
            assert tv._binary_search(tt_plus, valid_indices) == ti
            assert tv._linear_search(tt_plus, valid_indices) == ti

        # Out of bounds (before)
        t_before = parse_time(tv.metadata[0]["time"]) - timedelta(days=1)
        assert tv._binary_search(t_before, valid_indices) == 0
        assert tv._linear_search(t_before, valid_indices) == 0

        # Out of bounds (after)
        t_after = parse_time(tv.metadata[NUM_FRAMES - 1]["time"]) + timedelta(days=1)
        assert tv._binary_search(t_after, valid_indices) == NUM_FRAMES - 1
        assert tv._linear_search(t_after, valid_indices) == NUM_FRAMES - 1

        # Unsorted fallback for binary search
        orig_meta = tv.metadata.copy()
        try:
            # Swap first and last entries to break monotonicity
            tv.metadata[0], tv.metadata[NUM_FRAMES - 1] = tv.metadata[NUM_FRAMES - 1], tv.metadata[0]
            target_dt = parse_time(tv.metadata[NUM_FRAMES // 2]["time"])

            assert tv._binary_search(target_dt, valid_indices) is None
            assert tv._linear_search(target_dt, valid_indices) == NUM_FRAMES // 2
        finally:
            tv.metadata = orig_meta
