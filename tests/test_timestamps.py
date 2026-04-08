import os
import shutil
import av
from fractions import Fraction
from mini_timelapse.compile import compile_video, LocalImageSource
try:
    from tests.gen_test_images import generate_test_images
except ImportError:
    from gen_test_images import generate_test_images

def test_subtitle_alignment():
    """
    Verifies that for every video packet in an MKV, there is a corresponding
    subtitle packet at the exact same PTS.
    """
    tmp_root = "tests/tmp_timestamp_test"
    if os.path.exists(tmp_root):
        shutil.rmtree(tmp_root)
    os.makedirs(tmp_root)
    
    src_dir = os.path.join(tmp_root, "src")
    generate_test_images(src_dir, num_images=50)
    
    video_path = os.path.join(tmp_root, "test.mkv")
    fps = 30
    
    with LocalImageSource(src_dir) as source:
        compile_video(source, video_path, fps=fps, quality=23, preset="ultrafast", dry_run=False)
    
    # Verify timestamps
    container = av.open(video_path)
    video_stream = container.streams.video[0]
    sub_stream = container.streams.subtitles[0]
    
    video_timestamps = []
    sub_timestamps = []
    
    print(f"Video stream time_base: {video_stream.time_base}")
    print(f"Sub   stream time_base: {sub_stream.time_base}")
    
    packet_count = 0
    for packet in container.demux(video_stream, sub_stream):
        if packet.pts is None:
            continue
            
        if packet_count < 10:
             print(f"DEBUG: Packet stream={packet.stream.type}, pts={packet.pts}, time={float(packet.pts * packet.stream.time_base):.4f}")
        packet_count += 1
            
        if packet.stream.type == 'video':
            # Convert to seconds for easy comparison
            ts = float(packet.pts * packet.stream.time_base)
            video_timestamps.append(ts)
        elif packet.stream.type == 'subtitle':
            ts = float(packet.pts * packet.stream.time_base)
            sub_timestamps.append(ts)
    
    container.close()
    
    # Sort both sequences to account for out-of-order demuxing 
    # (Matroska interleaving doesn't always guarantee demux order match for dual streams)
    video_timestamps.sort()
    sub_timestamps.sort()
    
    print(f"Video PTS head: {[f'{t:.4f}' for t in video_timestamps[:5]]}")
    print(f"Sub   PTS head: {[f'{t:.4f}' for t in sub_timestamps[:5]]}")
    
    assert len(video_timestamps) == 50, f"Expected 50 video frames, got {len(video_timestamps)}"
    assert len(sub_timestamps) == 50, f"Expected 50 subtitle packets, got {len(sub_timestamps)}"
    
    # Check 1:1 alignment with tolerance for float rounding
    max_drift = 0.001 # 1ms
    for i in range(50):
        v_ts = video_timestamps[i]
        s_ts = sub_timestamps[i]
        diff = abs(v_ts - s_ts)
        assert diff < max_drift, f"Frame {i} misaligned: video={v_ts:.4f}s, sub={s_ts:.4f}s (diff={diff:.4f}s)"

    print("✅ All timestamps perfectly aligned!")
    shutil.rmtree(tmp_root)

if __name__ == "__main__":
    import sys
    from pathlib import Path
    
    # Add project root to path
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
        
    # Add tests dir to path for gen_test_images
    tests_dir = project_root / "tests"
    if str(tests_dir) not in sys.path:
        sys.path.insert(0, str(tests_dir))
        
    test_subtitle_alignment()
