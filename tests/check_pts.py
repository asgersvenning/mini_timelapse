import av
import sys
import os

def check_raw_pts(path):
    container = av.open(path)
    sub_stream = container.streams.subtitles[0]
    
    found_indices = {}
    pts_list = []
    
    print(f"Checking PTS order in {path}")
    for packet in container.demux(sub_stream):
        if packet.pts is None: continue
        pts_list.append(packet.pts)
        
        from mini_timelapse.metadata import decode_metadata_payload
        items = decode_metadata_payload(bytes(packet))
        for item in items:
            idx = item.get("index")
            if idx is not None:
                if idx in found_indices:
                    found_indices[idx].append(packet.pts)
                else:
                    found_indices[idx] = [packet.pts]
    
    print(f"Total packets: {len(pts_list)}")
    print(f"Unique Indices found: {len(found_indices)}")
    
    duplicates = {idx: pts for idx, pts in found_indices.items() if len(pts) > 1}
    if duplicates:
        print(f"DUPLICATES: {duplicates}")
        
    # Check for gaps
    for i in range(100):
        if i not in found_indices:
            print(f"MISSING: Index {i}")
            
    container.close()

if __name__ == "__main__":
    if os.path.exists("tests/tmp/test.mkv"):
        check_raw_pts("tests/tmp/test.mkv")
