import os
import piexif
from PIL import Image
from datetime import datetime, timedelta

os.makedirs("tests/test_images", exist_ok=True)

dt_start = datetime(2023, 1, 1, 12, 0, 0)
num_images = 10

def _to_deg(value, loc):
    # This is a highly simplified GPS encoding just for test cases
    # We output purely as rationals (numerator, denominator)
    d = int(value)
    m = int((value - d) * 60)
    s = round((value - d - m/60) * 3600 * 100)
    return ((d, 1), (m, 1), (s, 100))

# Generate images with an increasing gap and moving GPS
for i in range(num_images):
    gap = timedelta(minutes=i*10) 
    dt_curr = dt_start + gap
    
    img = Image.new("RGB", (640, 480), color=(10 * i, 20 * i, 255 - 10 * i))
    
    # 55.6761 N, 12.5683 E (approx Copenhagen) moving slightly
    lat = 55.6761 + (i * 0.001)
    lon = 12.5683 + (i * 0.001)
    
    gps_ifd = {
        piexif.GPSIFD.GPSVersionID: (2, 2, 0, 0),
        piexif.GPSIFD.GPSLatitudeRef: 'N',
        piexif.GPSIFD.GPSLatitude: _to_deg(lat, ["S", "N"]),
        piexif.GPSIFD.GPSLongitudeRef: 'E',
        piexif.GPSIFD.GPSLongitude: _to_deg(lon, ["W", "E"]),
        piexif.GPSIFD.GPSDateStamp: dt_curr.strftime("%Y:%m:%d").encode(),
        piexif.GPSIFD.GPSTimeStamp: ((dt_curr.hour, 1), (dt_curr.minute, 1), (dt_curr.second, 1))
    }
    
    exif_dict = {
        "Exif": {piexif.ExifIFD.DateTimeOriginal: dt_curr.strftime("%Y:%m:%d %H:%M:%S").encode('utf-8')},
        "GPS": gps_ifd
    }
    exif_bytes = piexif.dump(exif_dict)
    
    path = f"tests/test_images/img_{i:03d}.jpg"
    img.save(path, "jpeg", exif=exif_bytes)
    print(f"Generated {path} with time: {dt_curr} at Lat: {lat}, Lon: {lon}")
