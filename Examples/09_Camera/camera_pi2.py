import io
import time
from picamera2 import Picamera2, Preview
from base_camera import BaseCamera
import cv2
import libcamera


class Camera(BaseCamera):
    @staticmethod
    def frames():
        hflip = 0 
        vflip = 0 
        with Picamera2() as camera:
            camera.start()
            # # let camera warm up
            time.sleep(2) 


            stream = io.BytesIO()
            try:
                while True:
                    camera.capture_file(stream, format='jpeg')
                    stream.seek(0)
                    yield stream.read()

                    # reset stream for next frame
                    stream.seek(0)
                    stream.truncate()
            finally:
                camera.stop()
