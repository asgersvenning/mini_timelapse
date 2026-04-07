from mini_timelapse.reader import TimelapseVideo

def test():
    print("Testing TimelapseVideo loader...")
    # This expects tests/test.mkv to have been created by compile_timelapse.py
    with TimelapseVideo("tests/test.mkv") as tv:
        print(f"Loaded video with {len(tv)} frames.")
        
        # Test 1: Slicing
        subset = tv[2:5]
        assert len(subset) == 3
        print(f"Slicing [2:5] yielded 3 frames. Metadata for first slice frame: {subset[0][1]}")
        
        # Test 2: Indexing
        last_frame, last_meta = tv[-1]
        print(f"Last frame metadata via [-1]: {last_meta}")
        
        # Test 3: Iteration
        i = 0
        for frame, meta in tv:
            i += 1
            if i == 5:
                # Just sampling the 5th frame
                print(f"Frame {i} metadata: {meta}")
                break
                
    print("All tests passed!")

if __name__ == "__main__":
    test()
