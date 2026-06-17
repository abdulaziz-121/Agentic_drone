import os
from threading import Lock
from datetime import datetime

import numpy as np
import cv2
from dotenv import load_dotenv

load_dotenv()

_latest_frame = None
_lock = Lock()
_node = None

TOPIC = os.getenv("GZ_CAMERA_TOPIC", "/drone/camera/image")
PHOTO_DIR = os.path.join(os.path.dirname(__file__), "static", "photos")
os.makedirs(PHOTO_DIR, exist_ok=True)


def _callback(msg):
    global _latest_frame
    try:
        data = np.frombuffer(msg.data, dtype=np.uint8)
        # pixel_format_type 3 = RGB_INT8
        channels = 3 if msg.pixel_format_type in (3, 6) else 1
        frame = data.reshape((msg.height, msg.width, channels))
        if channels == 3:
            frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        with _lock:
            _latest_frame = frame.copy()
    except Exception as exc:
        print(f"[camera] frame error: {exc}")


def init():
    global _node
    for transport_mod, msgs_mod in [
        ("gz.transport13", "gz.msgs10.image_pb2"),
        ("gz.transport12", "gz.msgs9.image_pb2"),
        ("gz.transport11", "gz.msgs8.image_pb2"),
    ]:
        try:
            transport = __import__(transport_mod, fromlist=["Node"])
            msgs = __import__(msgs_mod, fromlist=["Image"])
            _node = transport.Node()
            _node.subscribe(msgs.Image, TOPIC, _callback)
            print(f"[camera] subscribed to {TOPIC} via {transport_mod}")
            return True
        except Exception:
            continue
    print("[camera] gz-transport unavailable — image capture disabled")
    return False


def capture(label="incident"):
    with _lock:
        frame = _latest_frame
    if frame is None:
        return None, "No frame received yet. Is Gazebo running with x500_gimbal?"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{label}_{timestamp}.jpg"
    cv2.imwrite(os.path.join(PHOTO_DIR, filename), frame)
    return filename, None


def latest_filename():
    try:
        files = sorted(f for f in os.listdir(PHOTO_DIR) if f.endswith(".jpg"))
        return files[-1] if files else None
    except OSError:
        return None
