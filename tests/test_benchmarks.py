import os
import random
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

from mini_timelapse.compile import LocalImageSource, compile_video
from mini_timelapse.decompile import decompile_video
from mini_timelapse.reader import TimelapseVideo

# Ensure tests package is findable
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

try:
    from gen_test_images import generate_test_images
except ImportError:
    from tests.gen_test_images import generate_test_images

@pytest.fixture(scope="module")
def benchmark_data():
    """Generate a set of images for benchmarking."""
    tmp_dir = tempfile.mkdtemp()
    img_dir = os.path.join(tmp_dir, "images")
    video_path = os.path.join(tmp_dir, "test.mkv")
    
    # Generate 100 images for a meaningful benchmark
    generate_test_images(img_dir, num_images=100, size=(1280, 720))
    
    # Pre-compile the video for reader benchmarks
    with LocalImageSource(img_dir) as source:
        compile_video(source, video_path, fps=30, quality=23)
        
    yield {
        "img_dir": img_dir,
        "video_path": video_path,
        "tmp_dir": tmp_dir
    }
    
    shutil.rmtree(tmp_dir)

def test_benchmark_compilation(benchmark, benchmark_data):
    """Benchmark video compilation speed."""
    output_path = os.path.join(benchmark_data["tmp_dir"], "bench_compile.mkv")
    
    def run_compilation():
        if os.path.exists(output_path):
            os.remove(output_path)
        with LocalImageSource(benchmark_data["img_dir"]) as source:
            compile_video(source, output_path, fps=30, quality=23)
            
    benchmark.pedantic(run_compilation, rounds=5, iterations=1)

def test_benchmark_decompilation(benchmark, benchmark_data):
    """Benchmark video decompilation speed."""
    out_dir = os.path.join(benchmark_data["tmp_dir"], "bench_decompile")
    
    def run_decompilation():
        if os.path.exists(out_dir):
            shutil.rmtree(out_dir)
        os.makedirs(out_dir)
        decompile_video(benchmark_data["video_path"], out_dir)
        
    benchmark.pedantic(run_decompilation, rounds=5, iterations=1)

def test_benchmark_iteration(benchmark, benchmark_data):
    """Benchmark Python API iteration speed."""
    def run_iteration():
        with TimelapseVideo(benchmark_data["video_path"]) as video:
            for _ in video:
                pass
                
    benchmark(run_iteration)

def test_benchmark_random_access(benchmark, benchmark_data):
    """Benchmark truly random access speed with detailed quantile reporting."""
    with TimelapseVideo(benchmark_data["video_path"]) as video:
        n = len(video)
        
        def run_random_access():
            idx = random.randint(0, n - 1)
            _ = video[idx]
            
        benchmark(run_random_access)

        # Manual sampling for specific quantiles (1%, 5%, 25%, 50%, 75%, 95%, 99%)
        # This provides the detailed distribution analysis requested.
        samples = []
        for _ in range(200):  # 200 samples for better quantile accuracy
            idx = random.randint(0, n - 1)
            import time
            start = time.perf_counter()
            _ = video[idx]
            samples.append(time.perf_counter() - start)
        
        data_ms = np.array(samples) * 1000
        quantiles = [1, 5, 25, 50, 75, 95, 99]
        values = np.percentile(data_ms, quantiles)
        
        print("\n" + "="*40)
        print("RANDOM ACCESS QUANTILES (ms)")
        print("-" * 40)
        for q, v in zip(quantiles, values):
            print(f"{q:>3}% quantile: {v:>8.4f} ms")
        print("="*40)
