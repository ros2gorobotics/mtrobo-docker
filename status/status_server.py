#!/usr/bin/env python3
"""Robot machine status for the mtrobo web: GET http://<pc>:9092/status (read-only JSON).

Work order 2026-11-27 B (jamorn1976/mtrobo docs/requests/2026-11-27-robot-status-endpoint.md, contract draft).
Runs inside the `mtrobo` container beside the bringup launch, as its own process, so it still answers when rosbridge
is down. It only looks: a TCP connect to rosbridge, the container's process list, and ROS topics / graph. No Docker
socket, no commands, no writes.

checks, in the order to fix them (an earlier failure usually explains a later one):
  rosbridge  TCP localhost:9090 accepts
  agent      a micro_ros_agent process is running
  esp32      /robot_status within the last 5 s        (detail: age of the data)
  lidar      /scan within the last 3 s                 (detail: Hz)
  manager    node mtrobo_manager is in the ROS graph
"""
import json
import os
import socket
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import LaserScan
from std_msgs.msg import Float32MultiArray

PORT = int(os.environ.get("STATUS_PORT", "9092"))
ROSBRIDGE_PORT = int(os.environ.get("ROSBRIDGE_PORT", "9090"))
ESP32_MAX_AGE, LIDAR_MAX_AGE = 5.0, 3.0
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ.get("STATUS_TZ", "Asia/Bangkok"))
except Exception:  # noqa: BLE001 - no tz database: UTC
    TZ = None


def ago(sec: float) -> str:
    if sec < 90:
        return f"{sec:.0f} วินาที"
    if sec < 5400:
        return f"{sec / 60:.0f} นาที"
    return f"{sec / 3600:.1f} ชั่วโมง"


class Watch(Node):
    """remembers when /robot_status and /scan last arrived"""

    def __init__(self):
        super().__init__("mtrobo_status")
        self.t0 = time.monotonic()
        self.status_t = None
        self.scans = deque(maxlen=60)
        self.lock = threading.Lock()
        # the ESP32 and the lidar publish best effort: subscribe best effort (matches reliable publishers too)
        self.create_subscription(Float32MultiArray, "/robot_status", self.on_status, qos_profile_sensor_data)
        self.create_subscription(LaserScan, "/scan", self.on_scan, qos_profile_sensor_data)

    def on_status(self, _m):
        with self.lock:
            self.status_t = time.monotonic()

    def on_scan(self, _m):
        with self.lock:
            self.scans.append(time.monotonic())

    def snapshot(self):
        with self.lock:
            return self.status_t, list(self.scans)

    def nodes(self) -> set:
        try:
            return {n for n, _ns in self.get_node_names_and_namespaces()}
        except Exception:  # noqa: BLE001 - graph not ready
            return set()


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def process_running(word: str) -> bool:
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                if word.encode() in f.read():
                    return True
        except OSError:
            continue
    return False


def checks(w: Watch) -> list:
    now = time.monotonic()
    out = []
    rb = port_open(ROSBRIDGE_PORT)
    out.append({"id": "rosbridge", "name": f"rosbridge ({ROSBRIDGE_PORT})", "ok": rb,
                **({} if rb else {"detail": f"ยังไม่เปิดพอร์ต {ROSBRIDGE_PORT}"})})
    ag = process_running("micro_ros_agent")
    out.append({"id": "agent", "name": "micro-ROS agent (8888/udp)", "ok": ag,
                **({} if ag else {"detail": "โปรแกรม micro_ros_agent ไม่ทำงาน"})})
    st, scans = w.snapshot()
    if st is None:
        e = {"ok": False, "detail": f"ยังไม่มีข้อมูลจากหุ่น (รอมา {ago(now - w.t0)}): ดูจอ OLED หรือกดรีเซ็ต ESP32"}
    elif now - st > ESP32_MAX_AGE:
        e = {"ok": False, "detail": f"ไม่มีข้อมูล {ago(now - st)}: ดูจอ OLED หรือกดรีเซ็ต ESP32"}
    else:
        e = {"ok": True, "detail": f"ล่าสุด {now - st:.1f} วินาทีที่แล้ว"}
    out.append({"id": "esp32", "name": "ESP32 (/robot_status)", **e})
    recent = [t for t in scans if now - t <= LIDAR_MAX_AGE]
    if recent and len(recent) >= 2:
        hz = (len(recent) - 1) / max(1e-3, recent[-1] - recent[0])
        out.append({"id": "lidar", "name": "lidar (/scan)", "ok": True, "detail": f"{hz:.1f} Hz"})
    elif scans:
        out.append({"id": "lidar", "name": "lidar (/scan)", "ok": False, "detail": f"ไม่มีข้อมูล {ago(now - scans[-1])}"})
    else:
        out.append({"id": "lidar", "name": "lidar (/scan)", "ok": False, "detail": "ยังไม่มีข้อมูล lidar"})
    mg = "mtrobo_manager" in w.nodes()
    out.append({"id": "manager", "name": "mtrobo_manager", "ok": mg,
                **({} if mg else {"detail": "ไม่พบ node mtrobo_manager"})})
    return out


def make_handler(w: Watch):
    class H(BaseHTTPRequestHandler):
        server_version = "mtrobo-status"

        def log_message(self, *a):
            pass

        def cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Cache-Control", "no-store")

        def do_OPTIONS(self):
            self.send_response(204)
            self.cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if self.path.split("?")[0].rstrip("/") != "/status":
                body = b'{"error":"not found"}'
                self.send_response(404)
            else:
                c = checks(w)
                t = datetime.now(TZ).isoformat(timespec="seconds")
                body = json.dumps({"ok": all(x["ok"] for x in c), "time": t, "checks": c}, ensure_ascii=False).encode()
                self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.cors()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_HEAD(self):
            self.send_response(405)
            self.end_headers()

    return H


def main():
    rclpy.init()
    w = Watch()
    threading.Thread(target=rclpy.spin, args=(w,), daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), make_handler(w))
    w.get_logger().info(f"status endpoint on http://0.0.0.0:{PORT}/status")
    srv.serve_forever()


if __name__ == "__main__":
    main()
