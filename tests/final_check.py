import av
import sys
import os
from mini_timelapse.metadata import decode_metadata_payload

def final_inspect(path):
    container = av.open(path)
    sub_stream = container.streams.subtitles[0]
    
    print(f"Inspecting {path}")
    
    for i, packet in enumerate(container.demux(sub_stream)):
        if packet.pts is None: continue
        
        data = bytes(packet)
        print(f"PTS: {packet.pts:4} | Size: {len(data):3} | Content: {data[:100]}")
        
        if i > 20: break
    container.close()

if __name__ == "__main__":
    if os.path.exists("tests/tmp/test.mkv"):
        final_inspect("tests/tmp/test.mkv")
