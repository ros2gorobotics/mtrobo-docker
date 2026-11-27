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
when it comes back, and the node keeps retrying. It never exits because of the phone.
QoS: RELIABLE, KEEP_LAST 1, VOLATILE (rosbridge subscribes with its defaults).

Set from the web (work order 2026-11-27 E): the node always runs, an empty url just means no pictures. The web sets
url / mode / rate with the standard /phone_camera_node/set_parameters; the values are checked (Thai reason when
refused), the picture loop restarts with them at once, and url / mode / rate are saved to `settings` (JSON, default
/home/ros/maps/phone_camera.json) so they survive a restart. At start the saved file wins over the launch values
(PHONE_CAMERA_URL in .env); without the file .env works as before.
"""
import http.client
import json
import os
import re
import threading
import time
from urllib.parse import urlsplit

import rclpy
from rcl_interfaces.msg import SetParametersResult
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CompressedImage

SOI, EOI = b"\xff\xd8", b"\xff\xd9"
MAX_FRAME = 4 * 1024 * 1024
CLEN = re.compile(rb"content-length:\s*(\d+)", re.I)
MODES = ("snapshot", "mjpeg")


def check(name: str, value):
    """(clean value, None) or (None, Thai reason)"""
    if name == "url":
        v = str(value).strip()
        if not v:
            return "", None
        u = urlsplit(v)
        if u.scheme not in ("http", "https") or not u.hostname:
            return None, "URL ต้องขึ้นต้นด้วย http:// หรือ https:// และมีที่อยู่ เช่น http://192.168.1.50:8080/shot.jpg"
        try:
            u.port
        except ValueError:
            return None, "พอร์ตใน URL ไม่ถูกต้อง"
        return v, None
    if name == "mode":
        v = str(value).strip().lower()
        return (v, None) if v in MODES else (None, "mode ต้องเป็น snapshot หรือ mjpeg")
    if name == "rate":
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None, "rate ต้องเป็นตัวเลข"
        return (v, None) if 0.2 <= v <= 30 else (None, "rate ต้องอยู่ระหว่าง 0.2 ถึง 30 Hz")
    return value, None


class PhoneCamera(Node):
    def __init__(self):
        super().__init__("phone_camera_node")
        self.settings = str(self.declare_parameter(
            "settings", os.environ.get("PHONE_CAMERA_SETTINGS", "/home/ros/maps/phone_camera.json")).value)
        self.url = str(self.declare_parameter("url", "").value).strip()
        self.mode = str(self.declare_parameter("mode", "snapshot").value).strip().lower()
        self.rate = max(0.2, float(self.declare_parameter("rate", 5.0).value))
        saved = self.load()
        if saved:
            self.set_parameters([Parameter(k, value=v) for k, v in saved.items()])
            self.url, self.mode, self.rate = saved.get("url", self.url), saved.get("mode", self.mode), saved.get("rate", self.rate)
        topic = str(self.declare_parameter("topic", "/camera/image/compressed").value)
        self.frame_id = str(self.declare_parameter("frame_id", "camera_link").value)
        self.timeout = max(0.1, float(self.declare_parameter("timeout", 1.0).value))
        qos = QoSProfile(reliability=ReliabilityPolicy.RELIABLE, history=HistoryPolicy.KEEP_LAST, depth=1,
                         durability=DurabilityPolicy.VOLATILE)
        self.pub = self.create_publisher(CompressedImage, topic, qos)
        self.topic = topic
        self.down = None           # None = not known yet, True = phone unreachable, False = getting pictures
        self.frames = 0
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.apply_timer = None
        if self.mode not in MODES:
            self.get_logger().warn(f"mode '{self.mode}' unknown: using snapshot")
            self.mode = "snapshot"
        self.add_on_set_parameters_callback(self.on_set)
        self.start()

    # ---------- settings from the web ----------
    def load(self) -> dict:
        """saved url / mode / rate, checked; {} when there is no file or it is broken"""
        try:
            with open(self.settings, encoding="utf-8") as f:
                d = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            self.get_logger().warn(f"cannot read {self.settings} ({e}): using the launch values")
            return {}
        out = {}
        for k in ("url", "mode", "rate"):
            if isinstance(d, dict) and k in d:
                v, why = check(k, d[k])
                if why is None:
                    out[k] = v
        if out:
            self.get_logger().info(f"settings from {self.settings}: {out}")
        return out

    def save(self) -> None:
        tmp = self.settings + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"url": self.url, "mode": self.mode, "rate": self.rate}, f, ensure_ascii=False)
            os.replace(tmp, self.settings)
        except OSError as e:
            self.get_logger().warn(f"cannot save {self.settings} ({e}): the setting works until the next restart")

    def on_set(self, params) -> SetParametersResult:
        changed = {}
        for p in params:
            if p.name in ("topic", "frame_id", "timeout", "settings"):
                return SetParametersResult(successful=False, reason=f"{p.name} เปลี่ยนตอนทำงานไม่ได้: ตั้งใน launch")
            if p.name not in ("url", "mode", "rate"):
                continue
            v, why = check(p.name, p.value)
            if why:
                return SetParametersResult(successful=False, reason=why)
            changed[p.name] = v
        if changed:
            with self.lock:
                self.url, self.mode, self.rate = (changed.get("url", self.url), changed.get("mode", self.mode),
                                                  changed.get("rate", self.rate))
                # the web sends url and mode as separate parameters: restart once, a moment after the last one
                if self.apply_timer is not None:
                    self.apply_timer.cancel()
                self.apply_timer = threading.Timer(0.3, self.apply)
                self.apply_timer.daemon = True
                self.apply_timer.start()
        return SetParametersResult(successful=True)

    def apply(self) -> None:
        with self.lock:
            self.save()
            self.start()

    def start(self) -> None:
        """stop the picture loop that runs (if any) and start one with the current url / mode / rate"""
        self.stop.set()
        self.stop = threading.Event()
        self.down = None
        if not self.url:
            self.get_logger().info("no phone camera url: waiting (set it from the web camera window or PHONE_CAMERA_URL)")
            return
        self.get_logger().info(f"phone camera {self.mode} {self.url} -> {self.topic} at {self.rate:g} Hz (frame {self.frame_id})")
        url, mode, rate, stop = self.url, self.mode, self.rate, self.stop
        threading.Thread(target=self.run, args=(url, mode, rate, stop), daemon=True).start()

    # ---------- state ----------
    def lost(self, why: str, stop: threading.Event) -> None:
        if stop.is_set():
            return
        if self.down is not True:
            self.get_logger().warn(f"phone camera unreachable ({why}): skipping frames, retrying")
        self.down = True

    def got(self, jpeg: bytes, stop: threading.Event) -> None:
        if stop.is_set():          # an old loop finishing its last request after a new url was applied
            return
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
    def open(self, url: str, mode: str) -> http.client.HTTPResponse:
        u = urlsplit(url)
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        c = cls(u.hostname, u.port, timeout=self.timeout)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        headers = {"User-Agent": "mtrobo-phone-camera", "Connection": "keep-alive" if mode == "snapshot" else "close"}
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

    def snapshot(self, url: str, rate: float, stop: threading.Event) -> None:
        period = 1.0 / rate
        nxt = time.monotonic()
        while not stop.is_set():
            try:
                r = self.open(url, "snapshot")
                data = r.read(MAX_FRAME + 1)
                r._conn.close()  # noqa: SLF001
                if len(data) > MAX_FRAME or not data.startswith(SOI):
                    raise ValueError("not a JPEG")
                self.got(data, stop)
            except (OSError, ValueError, http.client.HTTPException) as e:
                self.lost(str(e) or type(e).__name__, stop)
                stop.wait(1.0)                 # phone gone: do not hammer it
                nxt = time.monotonic()
                continue
            nxt += period
            wait = nxt - time.monotonic()
            if wait < -period:                 # far behind (slow phone): restart the clock
                nxt, wait = time.monotonic(), 0
            stop.wait(max(0.0, wait))

    def mjpeg(self, url: str, rate: float, stop: threading.Event) -> None:
        period = 1.0 / rate
        while not stop.is_set():
            try:
                r = self.open(url, "mjpeg")
                buf, due = b"", 0.0
                while not stop.is_set():
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
                            self.got(frame, stop)
                r._conn.close()  # noqa: SLF001
            except (OSError, ValueError, http.client.HTTPException) as e:
                self.lost(str(e) or type(e).__name__, stop)
                stop.wait(1.0)

    def run(self, url: str, mode: str, rate: float, stop: threading.Event) -> None:
        (self.mjpeg if mode == "mjpeg" else self.snapshot)(url, rate, stop)


def main():
    rclpy.init()
    n = PhoneCamera()
    try:
        rclpy.spin(n)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        n.stop.set()


if __name__ == "__main__":
    main()
