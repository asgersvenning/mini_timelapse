import os
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest
from pyinstrument import Profiler

from mini_timelapse.compile import LocalImageSource, compile_video
from mini_timelapse.decompile import decompile_video
from mini_timelapse.reader import TimelapseVideo
from mini_timelapse.repair import repair_video

# Ensure tests package is findable
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

try:
    from gen_test_images import generate_test_images
except ImportError:
    from tests.gen_test_images import generate_test_images


@pytest.fixture(scope="module")
def benchmark_data(tmp_path_factory):
    """Generate a set of images for benchmarking using Pytest's native tmp_path."""
    tmp_dir = tmp_path_factory.mktemp("bench_data")
    img_dir = tmp_dir / "images"
    img_dir.mkdir()

    video_path = str(tmp_dir / "test.mkv")
    shuffled_video_path = str(tmp_dir / "shuffled.mkv")

    generate_test_images(str(img_dir), num_images=100, size=(240, 360))
    src_spec = LocalImageSource.SourceSpec(src=str(img_dir))

    with LocalImageSource(spec=src_spec) as source:
        compile_video(source, video_path, fps=30, quality=23)

    with LocalImageSource(spec=src_spec) as source:
        random.shuffle(source.files)
        compile_video(source, shuffled_video_path, fps=30, quality=23)

    return {"img_dir": str(img_dir), "video_path": video_path, "shuffled_video_path": shuffled_video_path, "tmp_dir": str(tmp_dir)}


# =====================================================================
# CORE WORKLOADS (Pure Python, no Pytest or Profiler logic here)
# =====================================================================
def task_compile(data):
    out = os.path.join(data["tmp_dir"], "bench_compile.mkv")
    if os.path.exists(out):
        os.remove(out)
    with LocalImageSource(LocalImageSource.SourceSpec(src=data["img_dir"])) as source:
        compile_video(source, out, fps=30, quality=23)


def task_decompile(data):
    out = os.path.join(data["tmp_dir"], "bench_decompile")
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)
    decompile_video(data["video_path"], out)


def task_iteration(data):
    with TimelapseVideo(data["video_path"]) as video:
        for _ in video:
            pass


def task_repair(data):
    out = os.path.join(data["tmp_dir"], "bench_repair.mkv")
    if os.path.exists(out):
        os.remove(out)
    repair_video(data["shuffled_video_path"], out, fps=30)


def task_random_access(data):
    with TimelapseVideo(data["video_path"]) as video:
        # Fetch 50 random frames
        n = len(video)
        for _ in range(50):
            _ = video[random.randint(0, n - 1)]


# =====================================================================
# PYTEST BENCHMARK WRAPPERS
# =====================================================================
def test_benchmark_compilation(benchmark, benchmark_data):
    benchmark.pedantic(task_compile, args=(benchmark_data,), rounds=5, iterations=1)


def test_benchmark_decompilation(benchmark, benchmark_data):
    benchmark.pedantic(task_decompile, args=(benchmark_data,), rounds=5, iterations=1)


def test_benchmark_repair(benchmark, benchmark_data):
    benchmark.pedantic(task_repair, args=(benchmark_data,), rounds=5, iterations=1)


def test_benchmark_iteration(benchmark, benchmark_data):
    benchmark(task_iteration, benchmark_data)


def test_benchmark_random_access(benchmark, benchmark_data):
    benchmark(task_random_access, benchmark_data)


# =====================================================================
# PROFILING EXECUTION BLOCK
# =====================================================================
if __name__ == "__main__":
    print("Setting up profiling data...")
    tmp_dir = tempfile.mkdtemp()
    img_dir = os.path.join(tmp_dir, "images")
    video_path = os.path.join(tmp_dir, "test.mkv")
    shuffled_video_path = os.path.join(tmp_dir, "shuffled.mkv")

    generate_test_images(img_dir, num_images=1000, size=(240, 360))
    src_spec = LocalImageSource.SourceSpec(src=img_dir)

    with LocalImageSource(spec=src_spec) as source:
        compile_video(source, video_path, fps=30, quality=23)
    with LocalImageSource(spec=src_spec) as source:
        random.shuffle(source.files)
        compile_video(source, shuffled_video_path, fps=30, quality=23)

    mock_data = {"img_dir": img_dir, "video_path": video_path, "shuffled_video_path": shuffled_video_path, "tmp_dir": tmp_dir}

    tasks = {
        "compilation": task_compile,
        "decompilation": task_decompile,
        "iteration": task_iteration,
        "random_access": task_random_access,
        "repair": task_repair,
    }

    os.makedirs("public/profiles", exist_ok=True)

    for name, func in tasks.items():
        print(f"Profiling {name}...")
        profiler = Profiler()
        profiler.start()

        # Execute workload in a time-based loop to ensure statistical accuracy
        # Heavy tasks (compilation) will run 1-2 times, fast tasks (iteration) will run hundreds of times.
        end_time = time.time() + 1.5
        while time.time() < end_time:
            func(mock_data)

        profiler.stop()

        html_path = f"public/profiles/benchmark_{name}.html"
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(profiler.output_html())
        print(f"Saved: {html_path}")

    print("All profiling complete!")
    shutil.rmtree(tmp_dir)
