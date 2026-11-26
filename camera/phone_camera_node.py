#!/usr/bin/env python3
"""phone_camera_node: a phone's IP-camera app -> /camera/image/compressed (sensor_msgs/CompressedImage, jpeg).

Work order jamorn1976/mtrobo docs/requests/2026-11-26-robot-phone-camera-workorder.md (contract docs/contracts/topics.md).

The phone serves JPEGs over HTTP (Android "IP Webcam": /shot.jpg one picture, /video MJPEG). The bytes are published
as they come: no decode, no re-encode, so the node costs almost no CPU.

Parameters
  url       ""                          http://<phone>:8080/shot.jpg (snapshot) or http://<phone>:8080/video (mjpeg)
  mode      snapshot                    snapshot = GET one picture each time, mjpeg = read the stream, drop to `rate`
  rate      5.0                         Hz published
  topic     /camera/image/compressed
  frame_id  camera_link
  timeout   1.0                         s per request / per frame

The phone may go away (app closed, screen locked, Wi-Fi): frames are skipped, one warning when it goes, one info
when it comes back, and the node keeps retrying. It never exits because of the phone. Empty url: it says so and ends.
QoS: RELIABLE, KEEP_LAST 1, VOLATILE (rosbridge subscribes with its defaults).
"""
import http.client
import re
import threading
import time
from urllib.parse import urlsplit

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CompressedImage

SOI, EOI = b"\xff\xd8", b"\xff\xd9"
MAX_FRAME = 4 * 1024 * 1024
CLEN = re.compile(rb"content-length:\s*(\d+)", re.I)


class PhoneCamera(Node):
    def __init__(self):
        super().__init__("phone_camera_node")
        self.url = str(self.declare_parameter("url", "").value).strip()
        self.mode = str(self.declare_parameter("mode", "snapshot").value).strip().lower()
        self.rate = max(0.2, float(self.declare_parameter("rate", 5.0).value))
        topic = str(self.declare_parameter("topic", "/camera/image/compressed").value)
        self.frame_id = str(self.declare_parameter("frame_id", "camera_link").value)
        self.timeout = max(0.1, float(self.declare_parameter("timeout", 1.0).value))
        qos = QoSProfile(reliability=ReliabilityPolicy.RELIABLE, history=HistoryPolicy.KEEP_LAST, depth=1,
                         durability=DurabilityPolicy.VOLATILE)
        self.pub = self.create_publisher(CompressedImage, topic, qos)
        self.down = None           # None = not known yet, True = phone unreachable, False = getting pictures
        self.frames = 0
        self.stop = threading.Event()
        if self.mode not in ("snapshot", "mjpeg"):
            self.get_logger().warn(f"mode '{self.mode}' unknown: using snapshot")
            self.mode = "snapshot"
        self.get_logger().info(f"phone camera {self.mode} {self.url} -> {topic} at {self.rate:g} Hz (frame {self.frame_id})")

    # ---------- state ----------
    def lost(self, why: str) -> None:
        if self.down is not True:
            self.get_logger().warn(f"phone camera unreachable ({why}): skipping frames, retrying")
        self.down = True

    def got(self, jpeg: bytes) -> None:
        if self.down is not False:
            self.get_logger().info(f"phone camera {'back' if self.down else 'connected'}: {len(jpeg) // 1024} KB per picture")
        self.down = False
        m = CompressedImage()
        m.header.stamp = self.get_clock().now().to_msg()      # the time the picture arrived
        m.header.frame_id = self.frame_id
        m.format = "jpeg"
        m.data = jpeg
        self.pub.publish(m)
        self.frames += 1

    # ---------- HTTP ----------
    def open(self) -> http.client.HTTPResponse:
        u = urlsplit(self.url)
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        c = cls(u.hostname, u.port, timeout=self.timeout)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        headers = {"User-Agent": "mtrobo-phone-camera", "Connection": "keep-alive" if self.mode == "snapshot" else "close"}
        if u.username:
            import base64
            headers["Authorization"] = "Basic " + base64.b64encode(f"{u.username}:{u.password or ''}".encode()).decode()
        c.request("GET", path, headers=headers)
        r = c.getresponse()
        if r.status != 200:
            r.read()
            c.close()
            raise OSError(f"HTTP {r.status}")
        r._conn = c  # keep the connection with the response  # noqa: SLF001
        return r

    def snapshot(self) -> None:
        period = 1.0 / self.rate
        nxt = time.monotonic()
        while not self.stop.is_set():
            try:
                r = self.open()
                data = r.read(MAX_FRAME + 1)
                r._conn.close()  # noqa: SLF001
                if len(data) > MAX_FRAME or not data.startswith(SOI):
                    raise ValueError("not a JPEG")
                self.got(data)
            except (OSError, ValueError, http.client.HTTPException) as e:
                self.lost(str(e) or type(e).__name__)
                self.stop.wait(1.0)            # phone gone: do not hammer it
                nxt = time.monotonic()
                continue
            nxt += period
            wait = nxt - time.monotonic()
            if wait < -period:                 # far behind (slow phone): restart the clock
                nxt, wait = time.monotonic(), 0
            self.stop.wait(max(0.0, wait))

    def mjpeg(self) -> None:
        period = 1.0 / self.rate
        while not self.stop.is_set():
            try:
                r = self.open()
                buf, due = b"", 0.0
                while not self.stop.is_set():
                    chunk = r.read1(65536) if hasattr(r, "read1") else r.read(65536)
                    if not chunk:
                        raise OSError("stream ended")
                    buf += chunk
                    while True:
                        s = buf.find(SOI)
                        if s < 0:
                            buf = buf[-1:]
                            break
                        m = CLEN.search(buf, max(0, s - 512), s)        # part header just before the JPEG
                        if m:
                            end = s + int(m.group(1))
                            if len(buf) < end:
                                break
                            frame = buf[s:end]
                            buf = buf[end:]
                        else:
                            e = buf.find(EOI, s + 2)
                            if e < 0:
                                if len(buf) - s > MAX_FRAME:
                                    buf = b""
                                break
                            frame = buf[s:e + 2]
                            buf = buf[e + 2:]
                        now = time.monotonic()
                        if now >= due:                                 # drop frames down to `rate` on average
                            due = due + period if now - due < period else now + period
                            self.got(frame)
                r._conn.close()  # noqa: SLF001
            except (OSError, ValueError, http.client.HTTPException) as e:
                self.lost(str(e) or type(e).__name__)
                self.stop.wait(1.0)

    def run(self) -> None:
        (self.mjpeg if self.mode == "mjpeg" else self.snapshot)()


def main():
    rclpy.init()
    n = PhoneCamera()
    if not n.url:
        n.get_logger().info("no phone camera url set (PHONE_CAMERA_URL): camera off")
        n.destroy_node()
        rclpy.shutdown()
        return
    t = threading.Thread(target=n.run, daemon=True)
    t.start()
    try:
        rclpy.spin(n)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        n.stop.set()


if __name__ == "__main__":
    main()
