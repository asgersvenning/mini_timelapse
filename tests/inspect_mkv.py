import av
import sys
from mini_timelapse.metadata import decode_metadata_payload

def inspect_mkv(path):
    container = av.open(path)
    sub_stream = container.streams.subtitles[0]
    
    print(f"Inspecting {path}")
    print(f"Timebase: {sub_stream.time_base}")
    
    count = 0
    for packet in container.demux(sub_stream):
        if packet.pts is None: continue
        
        raw_bytes = bytes(packet)
        print(f"PTS: {packet.pts} | Raw bytes: {raw_bytes[:100]}")
        items = decode_metadata_payload(raw_bytes)
        
        print(f"PTS: {packet.pts} | Items: {len(items)}")
        for item in items:
            print(f"  Index: {item.get('index')} | Time: {item.get('time')}")
        
        count += 1
        if count > 10: break
    container.close()

if __name__ == "__main__":
    inspect_mkv("tests/tmp/test.mkv")
