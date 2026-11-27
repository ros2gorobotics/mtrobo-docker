#!/usr/bin/env python3
"""Send a saved map to the web: /mtrobo/map/read -> /mtrobo/map/result (op "read").

Work order 2026-11-27 D (jamorn1976/mtrobo docs/requests/2026-11-27-jimmy-bundle.md, contract draft D3).
The pair of /mtrobo/map/write in mtrobo_manager, kept outside the image so it needs no rebuild: it runs in its own
container on the mtrobo network with the maps folder mounted read-only. It only reads <maps_dir>/<name>.pgm/.yaml.

  request  /mtrobo/map/read    std_msgs/String {"name": "<map name without extension>"}
  reply    /mtrobo/map/result  {"op": "read", "ok": true,  "name", "pgm_b64", "yaml", "msg"}
                               {"op": "read", "ok": false, "name", "msg"}
"""
import base64
import json
import os
import re

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

NAME_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")   # same rule as /mtrobo/map/write
MAX_PGM = 8 * 1024 * 1024                          # bigger than any classroom map; keeps one reply sane over rosbridge


def image_name(yaml_text: str) -> str:
    """the `image:` file named in the map yaml (map_saver writes <name>.pgm), or "" """
    for line in yaml_text.splitlines():
        m = re.match(r"\s*image\s*:\s*['\"]?([^'\"#]+?)['\"]?\s*(#.*)?$", line)
        if m:
            return os.path.basename(m.group(1).strip())
    return ""


def read_map(maps_dir: str, name: str) -> dict:
    out = {"op": "read", "ok": False, "name": name}
    if not isinstance(name, str) or not NAME_OK.match(name):
        out["msg"] = "ชื่อแผนที่ไม่ถูกต้อง: ใช้ได้เฉพาะ A-Z a-z 0-9 _ - ไม่เกิน 64 ตัว"
        return out
    yaml_path = os.path.join(maps_dir, name + ".yaml")
    try:
        with open(yaml_path, encoding="utf-8", errors="replace") as f:
            yaml_text = f.read()
    except FileNotFoundError:
        out["msg"] = f"ไม่พบแผนที่ {name} ในโฟลเดอร์ maps"
        return out
    except OSError as e:
        out["msg"] = f"อ่าน {name}.yaml ไม่ได้: {e.strerror}"
        return out
    # <name>.pgm first; else the image the yaml names, only if it is a plain file in maps_dir
    pgm = name + ".pgm"
    if not os.path.isfile(os.path.join(maps_dir, pgm)):
        alt = image_name(yaml_text)
        if alt and alt.lower().endswith(".pgm") and os.path.isfile(os.path.join(maps_dir, alt)):
            pgm = alt
    pgm_path = os.path.join(maps_dir, pgm)
    try:
        size = os.path.getsize(pgm_path)
        if size > MAX_PGM:
            out["msg"] = f"ไฟล์ {pgm} ใหญ่เกินไป ({size // 1024} KB)"
            return out
        with open(pgm_path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        out["msg"] = f"พบ {name}.yaml แต่ไม่พบไฟล์ภาพ {pgm}"
        return out
    except OSError as e:
        out["msg"] = f"อ่าน {pgm} ไม่ได้: {e.strerror}"
        return out
    if data[:2] not in (b"P5", b"P2"):
        out["msg"] = f"{pgm} ไม่ใช่ไฟล์ PGM (P5/P2)"
        return out
    out.update(ok=True, pgm_b64=base64.b64encode(data).decode(), yaml=yaml_text, msg=f"อ่าน {pgm} แล้ว")
    return out


class MapReader(Node):
    def __init__(self):
        super().__init__("mtrobo_map_reader")
        self.maps_dir = os.path.abspath(os.path.expanduser(
            self.declare_parameter("maps_dir", os.environ.get("MAPS_DIR", "~/maps")).value))
        # same QoS as mtrobo_manager's /mtrobo/map/result publisher and the web's subscriber (reliable, depth 10)
        self.pub = self.create_publisher(String, "/mtrobo/map/result", 10)
        self.create_subscription(String, "/mtrobo/map/read", self.on_read, 10)
        self.get_logger().info(f"map reader: maps_dir = {self.maps_dir}")

    def on_read(self, m: String):
        try:
            req = json.loads(m.data)
            name = req.get("name") if isinstance(req, dict) else None
        except ValueError:
            name = None
        out = read_map(self.maps_dir, name) if name is not None else {
            "op": "read", "ok": False, "name": "", "msg": 'คำขอต้องเป็น JSON {"name": "<ชื่อแผนที่>"}'}
        self.get_logger().info(f"read {out.get('name')!r}: {'ok' if out['ok'] else out['msg']}")
        self.pub.publish(String(data=json.dumps(out, ensure_ascii=False)))


def main():
    rclpy.init()
    rclpy.spin(MapReader())


if __name__ == "__main__":
    main()
