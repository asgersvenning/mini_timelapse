import colorsys
import os
import random
from datetime import datetime, timedelta

import numpy as np
import piexif
from PIL import Image

from mini_timelapse.utils import natural_sort_key


def _to_deg(value, loc):
    d = int(value)
    m = int((value - d) * 60)
    s = round((value - d - m / 60) * 3600 * 100)
    return ((d, 1), (m, 1), (s, 100))


def generate_test_images(dst_dir: str, num_images: int = 10, size: tuple[int, int] = (320, 240)) -> list[dict]:
    os.makedirs(dst_dir, exist_ok=True)
    dt_curr = datetime(2023, 1, 1, 12, 0, 0)

    # Set seed for reproducibility
    SEED = 42
    random.seed(SEED)
    np.random.seed(SEED)

    expected = []
    lat, lon = 55.6761, 12.5683
    v_lat, v_lon = 0.0, 0.0
    momentum = 0.9
    step_scale = 0.001

    # Generate images with an irregular gap and moving GPS
    for i in range(num_images):
        dt_curr += timedelta(minutes=10 if random.random() < 0.5 else (random.random() * 100 + 10))

        # Random walk with momentum
        v_lat = momentum * v_lat + (1 - momentum) * random.uniform(-1, 1) * step_scale
        v_lon = momentum * v_lon + (1 - momentum) * random.uniform(-1, 1) * step_scale
        lat += v_lat
        lon += v_lon

        # Smooth color cycle in HSV space
        hue = i / num_images
        r, g, b = [int(x * 255) for x in colorsys.hsv_to_rgb(hue, 0.7, 0.8)]
        img = Image.new("RGB", size, color=(r, g, b))

        # add random noise to the image
        noise_weight = 0.05
        noise = np.random.randint(0, 255, (size[1], size[0], 3), dtype=np.uint8)
        img = Image.fromarray(
            (np.array(img).astype(np.float32) * (1 - noise_weight) + noise.astype(np.float32) * noise_weight).clip(0, 255).astype(np.uint8)
        )

        gps_ifd = {
            piexif.GPSIFD.GPSVersionID: (2, 2, 0, 0),
            piexif.GPSIFD.GPSLatitudeRef: "N",
            piexif.GPSIFD.GPSLatitude: _to_deg(lat, ["S", "N"]),
            piexif.GPSIFD.GPSLongitudeRef: "E",
            piexif.GPSIFD.GPSLongitude: _to_deg(lon, ["W", "E"]),
            piexif.GPSIFD.GPSDateStamp: dt_curr.strftime("%Y:%m:%d").encode(),
            piexif.GPSIFD.GPSTimeStamp: (
                (dt_curr.hour, 1),
                (dt_curr.minute, 1),
                (dt_curr.second, 1),
            ),
        }

        exif_dict = {
            "Exif": {piexif.ExifIFD.DateTimeOriginal: dt_curr.strftime("%Y:%m:%d %H:%M:%S").encode("utf-8")},
            "GPS": gps_ifd,
        }
        exif_bytes = piexif.dump(exif_dict)

        path = os.path.join(dst_dir, f"img_{i}.jpg")
        img.save(path, "jpeg", quality=95, exif=exif_bytes)

        expected.append(
            {
                "time": dt_curr.strftime("%Y-%m-%d %H:%M:%S"),
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "path": path,
            }
        )

    expected.sort(key=lambda x: natural_sort_key(x["path"]))
    return expected


if __name__ == "__main__":
    test_image_dir = os.path.join(os.path.dirname(__file__), "test_images")
    generate_test_images(test_image_dir, 10)
