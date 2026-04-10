import os
import shutil
import sys
import tempfile
from pathlib import Path

from mini_timelapse.compile import LocalImageSource, compile_video
from mini_timelapse.reader import TimelapseVideo

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

            # Step slice
            stepped = tv[0:250:50]
            assert len(stepped) == 5
            indices = [m["index"] for f, m in stepped]
            assert indices == [0, 50, 100, 150, 200]

            # Empty slice
            empty = tv[150:100]
            assert len(empty) == 0

            # Slice with negative indices
            neg_slice = tv[-10:]
            assert len(neg_slice) == 10
            assert neg_slice[0][1]["index"] == 240
            print("✓ Slicing passed")

            # --- find_frame_by_time Test ---
            print("Testing find_frame_by_time...")
            # The reader might normalize to YYYY:MM:DD HH:MM:SS or keep it
            # Let's check what the reader actually stored
            actual_time_str = tv.metadata[123]["time"]
            found_idx = tv.find_frame_by_time(actual_time_str)
            assert found_idx == 123
            print("✓ find_frame_by_time passed")

    finally:
        print(f"🧹 Cleaning up {tmp_dir}")
        shutil.rmtree(tmp_dir)

    print("\n✅ ALL READER TESTS PASSED!")


if __name__ == "__main__":
    test_reader_functionality()
