import os
import shutil
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

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


def test_reader_functionality():
    print("🚀 Starting TimelapseVideo reader tests...")

    tmp_dir = tempfile.mkdtemp(prefix="reader_test_")
    try:
        src_dir = os.path.join(tmp_dir, "src")
        src_spec = LocalImageSource.SourceSpec(src=src_dir)
        video_path = os.path.join(tmp_dir, "test.mkv")
        num_frames = 250

        # 1. Setup: Generate images and compile
        print(f"--- Step 1: Generating {num_frames} test images ---")
        expected_meta = generate_test_images(src_dir, num_images=num_frames)

        print("--- Step 2: Compiling video ---")
        with LocalImageSource(spec=src_spec) as source:
            compile_video(source, video_path, fps=30, quality=23, preset="ultrafast")

        # 2. Open Video
        print("--- Step 3: Loading video with TimelapseVideo ---")
        with TimelapseVideo(video_path) as tv:
            # Basic checks
            print(f"Length: {len(tv)}")
            assert len(tv) == num_frames

            # --- Iteration Test ---
            print("Testing iteration...")
            count = 0
            for i, (frame, meta) in enumerate(tv):
                assert meta["index"] == i
                assert meta["filename"] == os.path.basename(expected_meta[i]["path"])
                count += 1
            assert count == num_frames
            print("✓ Iteration passed")

            # --- Indexing Test ---
            print("Testing indexing...")
            # Positive
            f0, m0 = tv[0]
            assert m0["index"] == 0
            f_last, m_last = tv[num_frames - 1]
            assert m_last["index"] == num_frames - 1

            # Negative
            fn1, mn1 = tv[-1]
            assert mn1["index"] == num_frames - 1
            fn_len, mn_len = tv[-num_frames]
            assert mn_len["index"] == 0

            # Bounds
            try:
                tv[num_frames]
                raise AssertionError("Should have raised IndexError for positive out of bounds")
            except IndexError:
                pass

            try:
                tv[-(num_frames + 1)]
                raise AssertionError("Should have raised IndexError for negative out of bounds")
            except IndexError:
                pass
            print("✓ Indexing passed")

            # --- Slicing Test ---
            print("Testing slicing...")
            # Simple slice
            subset = tv[100:150]
            assert len(subset) == 50
            assert subset[0][1]["index"] == 100
            assert subset[-1][1]["index"] == 149
            print("✓ Slicing passed (simple)")

            # Step slice
            stepped = tv[0:250:50]
            assert len(stepped) == 5
            indices = [m["index"] for f, m in stepped]
            assert indices == [0, 50, 100, 150, 200]
            print("✓ Slicing passed (step)")

            # Empty slice
            empty = tv[150:100]
            assert len(empty) == 0
            print("✓ Slicing passed (empty)")

            # Slice with negative indices
            neg_slice = tv[-10:]
            assert len(neg_slice) == 10
            assert neg_slice[0][1]["index"] == 240
            print("✓ Slicing passed")

            # --- get_frame_by_time Test ---
            print("Testing get_frame_by_time...")
            actual_time_str = tv.metadata[123]["time"]
            frame, meta, diff = tv.get_frame_by_time(actual_time_str, max_diff=0)
            assert meta["index"] == 123
            assert diff == 0
            assert frame.shape == (240, 320, 3)
            print("✓ get_frame_by_time passed (exact match)")

            # Test closest match
            dt_base = parse_time(actual_time_str)
            dt_offset = dt_base + timedelta(seconds=5)
            frame, meta, diff = tv.get_frame_by_time(dt_offset)
            # Since generate_test_images uses ~10min gaps, 123 should still be closest
            assert meta["index"] == 123
            assert diff == 5.0
            print("✓ get_frame_by_time passed (closest match)")

            # Test max_diff
            try:
                tv.get_frame_by_time(dt_offset, max_diff=2.0)
                raise AssertionError("Should have raised ValueError for max_diff")
            except ValueError as e:
                assert "exceeding max_diff of 2.0s" in str(e)

            # Test unsorted fallback (requires mocking metadata)
            orig_meta = tv.metadata.copy()
            try:
                # Swap two entries to break monotonicity
                tv.metadata[10], tv.metadata[20] = tv.metadata[20], tv.metadata[10]
                # Searching should trigger a warning but still work (linear fallback)
                frame, meta, diff = tv.get_frame_by_time(actual_time_str)
                assert meta["index"] == 123
                assert diff == 0
            finally:
                tv.metadata = orig_meta
            print("✓ get_frame_by_time passed")

            # --- Private Search Methods Test ---
            print("Testing private search methods (_binary_search, _linear_search)...")
            valid_indices = list(range(num_frames))

            def test_search(ti):
                # Exact match
                tt = parse_time(tv.metadata[ti]["time"])
                assert tv._binary_search(tt, valid_indices) == ti
                assert tv._linear_search(tt, valid_indices) == ti

                # Closest neighbor
                tt_plus = tt + timedelta(seconds=1)
                assert tv._binary_search(tt_plus, valid_indices) == ti
                assert tv._linear_search(tt_plus, valid_indices) == ti

            # Test all frames to ensures robustness
            for ti in range(num_frames):
                test_search(ti)
            print("✓ Private search methods passed (all frames)")

            # Out of bounds (before)
            t_before = parse_time(tv.metadata[0]["time"]) - timedelta(days=1)
            assert tv._binary_search(t_before, valid_indices) == 0
            assert tv._linear_search(t_before, valid_indices) == 0

            # Out of bounds (after)
            t_after = parse_time(tv.metadata[num_frames - 1]["time"]) + timedelta(days=1)
            assert tv._binary_search(t_after, valid_indices) == num_frames - 1
            assert tv._linear_search(t_after, valid_indices) == num_frames - 1
            print("✓ Private search methods passed (out of bounds)")

            # Unsorted fallback for binary search
            orig_meta = tv.metadata.copy()
            try:
                # Swap first and last entries to break monotonicity
                tv.metadata[0], tv.metadata[num_frames - 1] = tv.metadata[num_frames - 1], tv.metadata[0]
                # binary search should return None, linear should still work
                assert tv._binary_search(parse_time(tv.metadata[num_frames // 2]["time"]), valid_indices) is None
                assert tv._linear_search(parse_time(tv.metadata[num_frames // 2]["time"]), valid_indices) == num_frames // 2
                print("✓ Private search fallback passed")
            finally:
                tv.metadata = orig_meta
            print("✓ Private search methods passed")

    finally:
        print(f"🧹 Cleaning up {tmp_dir}")
        shutil.rmtree(tmp_dir)

    print("\n✅ ALL READER TESTS PASSED!")


if __name__ == "__main__":
    test_reader_functionality()
