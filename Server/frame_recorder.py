#!/usr/bin/env/python
# File name   : frame_recorder.py
# Website     : www.Adeept.com
# Author      : Adeept
# Date        : 2026/04/21
#
# Background thread that keeps the last N JPEG frames from the shared
# Picamera2/OpenCV Camera (see camera_opencv.py) so the MCP server can
# return a filmstrip without forcing the caller to poll frame-by-frame.

import threading
import time
from collections import deque

import cv2
import numpy as np


class FrameRecorder(threading.Thread):
    def __init__(self, camera, interval_s=0.5, max_frames=5):
        super().__init__(daemon=True)
        self._camera = camera
        self._interval_s = interval_s
        self._frames = deque(maxlen=max_frames)
        self._lock = threading.Lock()
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        while self._running:
            try:
                frame = self._camera.get_frame()
                if frame:
                    with self._lock:
                        self._frames.append(frame)
            except Exception as e:
                print(f'FrameRecorder: {e}')
            time.sleep(self._interval_s)

    def latest(self):
        with self._lock:
            return self._frames[-1] if self._frames else None

    def snapshot(self):
        with self._lock:
            return list(self._frames)

    def filmstrip_bytes(self):
        # Oldest on the left, newest on the right. Re-encodes as one JPEG.
        frames = self.snapshot()
        if not frames:
            return None
        imgs = []
        for jpg in frames:
            arr = np.frombuffer(jpg, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is not None:
                imgs.append(img)
        if not imgs:
            return None
        min_h = min(img.shape[0] for img in imgs)
        normalised = [
            img if img.shape[0] == min_h
            else cv2.resize(img, (int(img.shape[1] * min_h / img.shape[0]), min_h))
            for img in imgs
        ]
        strip = cv2.hconcat(normalised)
        ok, encoded = cv2.imencode('.jpg', strip)
        if not ok:
            return None
        return encoded.tobytes()
