"""MTRobo classroom hub (issue #1; contract jamorn1976/mtrobo docs/contracts/classroom.md v1.1).

One port (9000) on the teacher's PC:
  ws://<hub>/class                   the class channel: teacher page (/class) and student pages (/join, /?class=1)
  ws://<hub>/robot/<n>?token=<token> robot n's rosbridge, passed through and filtered by the group's role

The robots' own rosbridge ports are not reachable from the classroom Wi-Fi (docker-compose.classroom.yml); every
command goes through here, so the teacher's choices are enforced, not just hidden in the web UI. State lives in memory
only; groups are the names students type, nothing else (PDPA).

Config (environment):
  HUB_PIN          teacher PIN (digits)                        default 1234 (change it!)
  HUB_ROBOTS       robots, comma separated: url | name|url | name|#colour|url   default ws://mtrobo:9090
  CLASS_JOIN_URL   the link students open, e.g. http://192.168.1.105:3000/join (shown as a QR on the teacher page)
  HUB_MAX_V        class speed cap, m/s (forwarded cmd_vel)    default 0.25
  HUB_MAX_W        class turn cap, rad/s                       default 1.0
  HUB_PORT         default 9000
"""
import asyncio
import json
import logging
import os
import random
import re
import secrets
import time

from wsproto import WS, Closed, connect, serve

log = logging.getLogger("hub")

COLORS = ["#f59f00", "#1c7ed6", "#e03131", "#2f9e44", "#ae3ec9", "#f76707", "#0c8599", "#d6336c", "#5c940d", "#495057"]
COLOR_NAMES = ["เหลือง", "ฟ้า", "แดง", "เขียว", "ม่วง", "ส้ม", "เขียวน้ำทะเล", "ชมพู", "เขียวมะกอก", "เทา"]
ROLES = ("watch", "blocks", "goal", "drive")

# topics the web uses (mtrobo lib/settings.ts defaults)
CMD_VEL, GOAL, INITIALPOSE, SERVO = "/cmd_vel", "/goal_pose", "/initialpose", "/servo/cmd"
NAV_ACTIONS = ("/navigate_to_pose", "/navigate_through_poses", "/follow_waypoints")
PUBLISH = {
    "watch": set(), "demo": set(),
    "blocks": {CMD_VEL, GOAL, SERVO},
    "goal": {GOAL, INITIALPOSE},
    "drive": {CMD_VEL, GOAL, INITIALPOSE, SERVO},
}
CAN_CANCEL = {"blocks", "goal", "drive"}
# read-only services every role may call (topic list, map, parameter reads, a goal's result)
# rosapi is read-only except set_param / delete_param, which would let any group change any node's parameters
READ_SERVICES = re.compile(r"^/rosapi/(?!set_param$|delete_param$)|^/map_server/map$|/(get_parameters|list_parameters|describe_parameters|get_parameter_types)$|/_action/get_result$")
ZERO_UUID = [0] * 16
PIN_DELAY = 1.0      # s per wrong teacher PIN, for the whole hub: 4 digits take hours to guess, not milliseconds


def now() -> float:
    return time.monotonic()


class Robot:
    """one robot: its rosbridge address, the card the teacher sees, and the hub's own link for status and stopping"""

    def __init__(self, n: int, name: str, color: str, url: str):
        self.n, self.name, self.color, self.url = n, name, color, url
        self.battery: float | None = None
        self.rssi: float | None = None
        self.status_t = 0.0          # last /robot_status
        self.seen_t = 0.0            # last message of any kind
        self.navigating = False
        self.stopped = False
        self.group: str | None = None
        self.link: WS | None = None
        self.advertised = False

    def info(self) -> dict:
        return {"n": self.n, "name": self.name, "color": self.color}

    def card(self) -> dict:
        t = now()
        return {**self.info(), "online": self.link is not None and t - self.seen_t < 5,
                "battery": self.battery, "rssi": self.rssi,
                "agent": (t - self.status_t < 3) if self.status_t else None,
                "navigating": self.navigating, "group": self.group, "stopped": self.stopped}

    async def run(self, changed) -> None:
        """keep the hub's own link to this robot's rosbridge: battery, Wi-Fi, Nav2 goal status"""
        while True:
            try:
                ws = await connect(self.url)
                self.link, self.advertised = ws, False
                subs = [("/battery", "sensor_msgs/msg/BatteryState", 2000), ("/robot_status", "std_msgs/msg/Float32MultiArray", 1000)]
                subs += [(f"{a}/_action/status", "action_msgs/msg/GoalStatusArray", 500) for a in NAV_ACTIONS[:1]]
                for topic, typ, thr in subs:
                    await ws.send(json.dumps({"op": "subscribe", "id": f"hub:{topic}", "topic": topic, "type": typ, "throttle_rate": thr, "queue_length": 1}))
                log.info("robot %d: linked to %s", self.n, self.url)
                changed()
                while True:
                    m = json.loads(await ws.recv())
                    self.seen_t = now()
                    if m.get("op") != "publish":
                        continue
                    topic, msg = m.get("topic"), m.get("msg") or {}
                    if topic == "/battery":
                        p = msg.get("percentage")
                        if isinstance(p, (int, float)) and p == p:
                            self.battery = round(p * 100 if p <= 1.0 else p)
                    elif topic == "/robot_status":
                        d = msg.get("data") or []
                        self.status_t = now()
                        if d and isinstance(d[0], (int, float)):
                            self.rssi = round(d[0])
                    elif topic.endswith("/_action/status"):
                        self.navigating = any(s.get("status") in (1, 2) for s in msg.get("status_list") or [])
                    changed()
            except (Closed, OSError, asyncio.TimeoutError, ValueError) as e:
                if self.link:
                    log.warning("robot %d: link lost (%s)", self.n, e)
                self.link = None
                changed()
                await asyncio.sleep(2)

    async def stop(self) -> None:
        """zero velocity (3 times, as the web does on release) and cancel every Nav2 goal"""
        ws = self.link
        if not ws:
            return
        try:
            if not self.advertised:
                await ws.send(json.dumps({"op": "advertise", "topic": CMD_VEL, "type": "geometry_msgs/msg/Twist", "queue_size": 1}))
                self.advertised = True
            zero = {"linear": {"x": 0.0, "y": 0.0, "z": 0.0}, "angular": {"x": 0.0, "y": 0.0, "z": 0.0}}
            for _ in range(3):
                await ws.send(json.dumps({"op": "publish", "topic": CMD_VEL, "msg": zero}))
            for a in NAV_ACTIONS:
                await ws.send(json.dumps({"op": "call_service", "id": f"hub:cancel:{a}", "service": f"{a}/_action/cancel_goal",
                                          "type": "action_msgs/srv/CancelGoal",
                                          "args": {"goal_info": {"goal_id": {"uuid": ZERO_UUID}, "stamp": {"sec": 0, "nanosec": 0}}}}))
        except Closed:
            pass


class Group:
    def __init__(self, gid: str, name: str, token: str):
        self.id, self.name, self.token = gid, name, token
        self.robot: int | None = None
        self.role = "watch"
        self.conn: WS | None = None
        self.seen = now()

    def card(self) -> dict:
        return {"id": self.id, "name": self.name, "robot": self.robot, "role": self.role, "online": now() - self.seen < 7, "venue": ""}


class Proxy:
    """one browser connection to /robot/<n>, with its own upstream link to that robot's rosbridge"""

    def __init__(self, ws: WS, robot: Robot, token: str):
        self.ws, self.robot, self.token = ws, robot, token
        self.up: WS | None = None


class Hub:
    def __init__(self, pin: str, robots: list[Robot], join: str = "", max_v: float = 0.25, max_w: float = 1.0):
        self.pin, self.robots, self.join, self.max_v, self.max_w = pin, robots, join, max_v, max_w
        self.code: str | None = None
        self.stop_all = False
        self.groups: dict[str, Group] = {}
        self.teachers: set[WS] = set()
        self.next = 1
        self.demo: dict | None = None
        self.dview: dict = {}
        self.dseq = 0
        self.teach_conn: WS | None = None
        self.proxies: set[Proxy] = set()
        self._push_due = False
        self._last_state = ""
        self._pin_lock = asyncio.Lock()

    # ---------- helpers ----------
    def robot(self, n) -> Robot | None:
        return next((r for r in self.robots if r.n == n), None)

    def by_token(self, token: str) -> Group | None:
        return next((g for g in self.groups.values() if g.token == token), None)

    def state(self) -> dict | None:
        if self.code is None:
            return None
        s = {"code": self.code, "stopAll": self.stop_all, "robots": [r.card() for r in self.robots],
             "groups": [g.card() for g in self.groups.values()], "demo": self.demo}
        if self.join:
            s["join"] = self.join
        return s

    def changed(self) -> None:
        self._push_due = True

    async def push(self, force: bool = False) -> None:
        """the class state to every teacher page (on change, and once a second for battery / online)"""
        s = json.dumps({"op": "class", "state": self.state()}, ensure_ascii=False)
        if not force and s == self._last_state:
            return
        self._last_state = s
        for t in list(self.teachers):
            try:
                await t.send(s)
            except Closed:
                self.teachers.discard(t)

    async def tell(self, g: Group | None, msg: dict) -> None:
        if g and g.conn and not g.conn.closed:
            try:
                await g.conn.send(json.dumps(msg, ensure_ascii=False))
            except Closed:
                pass

    async def reply(self, ws: WS, msg: dict) -> None:
        try:
            await ws.send(json.dumps(msg, ensure_ascii=False))
        except Closed:
            pass

    def status(self, g: Group) -> dict:
        r = self.robot(g.robot)
        if r:
            return {"op": "granted", "group": g.name, "token": g.token, "robot": r.info(), "role": g.role, "venue": ""}
        return {"op": "waiting", "group": g.name, "token": g.token}

    async def free(self, g: Group) -> None:
        """take the group off its robot: the robot stops and its goal is cancelled"""
        r = self.robot(g.robot)
        if r:
            r.group = None
            asyncio.create_task(r.stop())
        g.robot = None

    # ---------- demo (work order 0006) ----------
    def viewers(self) -> list[Group]:
        D = self.demo
        if not D:
            return []
        return [g for g in self.groups.values() if g.id != D["by"] and (D["to"] is None or g.id in D["to"])]

    def demo_on(self) -> dict:
        D = self.demo
        r = self.robot(D["robot"])
        return {"op": "demo", "on": True, "by": D["name"], "robot": r.info() if r else None, "venue": "", "view": dict(self.dview)}

    async def end_demo(self) -> None:
        D = self.demo
        if not D:
            return
        for g in self.viewers():
            await self.tell(g, {"op": "demo", "on": False})
        if D["by"] != "teacher":
            await self.tell(self.groups.get(D["by"]), {"op": "present", "on": False})
        if self.teach_conn:
            await self.reply(self.teach_conn, {"op": "revoked", "reason": "demo_end"})
            await self.reply(self.teach_conn, {"op": "expired"})
        self.demo, self.dview, self.teach_conn = None, {}, None
        await self.prune()

    # ---------- rights on /robot/<n> ----------
    def rights(self, token: str, n: int) -> str | None:
        """what this token may do on robot n now: a role, 'demo' (watch the demo robot), 'teacher', or None"""
        D = self.demo
        if D and D.get("key") and token == D["key"]:
            return "teacher" if D["robot"] == n else None
        g = self.by_token(token)
        if not g:
            return None
        if D and g in self.viewers():
            if D["robot"] == n:
                return "demo"
            return "watch" if g.robot == n else None
        return g.role if g.robot == n else None

    async def prune(self) -> None:
        """close robot connections whose token lost that robot"""
        for p in list(self.proxies):
            if self.rights(p.token, p.robot.n) is None:
                await p.ws.close(4003)
                if p.up:
                    await p.up.close()

    # ---------- class channel ----------
    async def class_conn(self, ws: WS) -> None:
        try:
            while True:
                try:
                    m = json.loads(await ws.recv())
                except ValueError:
                    continue
                if isinstance(m, dict):
                    await self.handle(ws, m)
                    await self.push()      # teacher pages see joins, assignments … at once (unchanged state is not resent)
        finally:
            self.teachers.discard(ws)

    async def handle(self, ws: WS, m: dict) -> None:
        op = m.get("op")
        if op == "join":
            if self.code is None or str(m.get("code", "")).strip() != self.code:
                return await self.reply(ws, {"op": "error", "reason": "code"})
            name = str(m.get("group", "")).strip()[:24]
            if not name:
                return await self.reply(ws, {"op": "error", "reason": "name"})
            if any(g.name == name and now() - g.seen < 7 for g in self.groups.values()):
                return await self.reply(ws, {"op": "error", "reason": "taken"})
            gid = f"g{self.next}"
            self.next += 1
            g = Group(gid, name, f"{gid}.{secrets.token_urlsafe(12)}")
            g.conn = ws
            self.groups[gid] = g
            await self.reply(ws, {"op": "waiting", "group": name, "token": g.token})
            if self.demo and self.demo["to"] is None:
                await self.reply(ws, self.demo_on())
            log.info("group %s joined", name)
            self.changed()
            return
        if op in ("resume", "ping"):
            token = str(m.get("token", ""))
            D = self.demo
            if D and D.get("key") and token == D["key"]:
                self.teach_conn = ws
                if op == "ping":
                    return await self.reply(ws, {"op": "pong"})
                r = self.robot(D["robot"])
                if r:
                    await self.reply(ws, {"op": "granted", "group": "ครู", "token": token, "robot": r.info(), "role": "drive", "venue": ""})
                return await self.reply(ws, {"op": "present", "on": True})
            g = self.by_token(token)
            if not g:
                return await self.reply(ws, {"op": "expired"})
            g.conn, g.seen = ws, now()
            if op == "ping":
                return await self.reply(ws, {"op": "pong"})
            await self.reply(ws, self.status(g))
            if self.stop_all:
                await self.reply(ws, {"op": "stop_all", "on": True})
            if D and D["by"] == g.id:
                await self.reply(ws, {"op": "present", "on": True})
            elif D and g in self.viewers():
                await self.reply(ws, self.demo_on())
            else:
                await self.reply(ws, {"op": "demo", "on": False})
            self.changed()
            return
        if op == "leave":
            g = self.by_token(str(m.get("token", "")))
            if g:
                if self.demo and self.demo["by"] == g.id:
                    await self.end_demo()
                await self.free(g)
                del self.groups[g.id]
                await self.prune()
                self.changed()
            return
        if op == "demo_view":
            D = self.demo
            if not D:
                return
            token = str(m.get("token", ""))
            ok = token == D.get("key") if D["by"] == "teacher" else getattr(self.by_token(token), "id", None) == D["by"]
            if not ok:
                return await self.reply(ws, {"op": "denied", "what": "demo_view", "reason": "role"})
            seq = m.get("seq")
            if not isinstance(seq, (int, float)) or seq <= self.dseq:
                return
            self.dseq = seq
            view = m.get("view") if isinstance(m.get("view"), dict) else {}
            self.dview.update(view)
            out = {"op": "demo_view", "seq": seq, "view": view}
            for g in self.viewers():
                await self.tell(g, out)
            return
        if op == "teacher":
            if not secrets.compare_digest(str(m.get("pin", "")).encode(), self.pin.encode()):
                log.warning("wrong teacher PIN from %s", ws.peer)
                async with self._pin_lock:          # wrong PINs queue up hub-wide; the right PIN never waits
                    await asyncio.sleep(PIN_DELAY)
                return await self.reply(ws, {"op": "error", "reason": "pin"})
            self.teachers.add(ws)
            await self.reply(ws, {"op": "class", "state": self.state()})
            return
        # ---------- teacher commands ----------
        if ws not in self.teachers:
            return await self.reply(ws, {"op": "error", "reason": "pin"})
        await self.teacher(m)
        self.changed()

    async def teacher(self, m: dict) -> None:
        op = m.get("op")
        if op == "open":
            await self.close_class()
            self.code = f"{random.randint(1000, 9999)}"
            log.info("lesson open, code %s", self.code)
            return
        if op == "close":
            await self.close_class()
            return
        if self.code is None:
            return
        g = self.groups.get(str(m.get("group", "")))
        if op == "assign":
            r = self.robot(m.get("robot"))
            role = m.get("role") if m.get("role") in ROLES else "watch"
            if not g or not r:
                return
            old = self.groups.get(r.group) if r.group and r.group != g.id else None
            if old:
                if self.demo and self.demo["by"] == old.id:
                    await self.end_demo()
                await self.free(old)
                await self.tell(old, {"op": "revoked", "reason": "teacher"})
                await self.tell(old, self.status(old))
            if g.robot is not None and g.robot != r.n:
                await self.free(g)
            g.robot, g.role, r.group = r.n, role, g.id
            await self.tell(g, self.status(g))
            await self.prune()
        elif op == "unassign":
            if not g or g.robot is None:
                return
            if self.demo and self.demo["by"] == g.id:
                await self.end_demo()
            await self.free(g)
            await self.tell(g, {"op": "revoked", "reason": "teacher"})
            await self.tell(g, self.status(g))
            await self.prune()
        elif op == "remove":
            if not g:
                return
            if self.demo and self.demo["by"] == g.id:
                await self.end_demo()
            if self.demo and self.demo["to"]:
                self.demo["to"] = [x for x in self.demo["to"] if x != g.id]
            await self.free(g)
            await self.tell(g, {"op": "revoked", "reason": "removed"})
            await self.tell(g, {"op": "expired"})
            del self.groups[g.id]
            await self.prune()
        elif op == "stop_all":
            self.stop_all = bool(m.get("on"))
            for r in self.robots:
                r.stopped = self.stop_all
            if self.stop_all:
                await asyncio.gather(*(r.stop() for r in self.robots))
            for x in self.groups.values():
                await self.tell(x, {"op": "stop_all", "on": self.stop_all})
            log.info("stop all %s", "ON" if self.stop_all else "off")
        elif op == "stop":
            r = self.robot(m.get("robot"))
            if r:
                await r.stop()
                await self.tell(self.groups.get(r.group or ""), {"op": "halt"})
        elif op == "demo_start":
            by = str(m.get("by", ""))
            to = [x for x in (m.get("to") or []) if x in self.groups] or None
            if by == "teacher":
                r = self.robot(m.get("robot")) or (self.robots[0] if self.robots else None)
                robot, name = (r.n if r else None), "ครู"
            else:
                p = self.groups.get(by)
                if not p or p.robot is None:
                    return
                robot, name = p.robot, p.name
            if self.demo:
                await self.end_demo()
            self.dview, self.dseq = {}, 0
            self.demo = {"by": by, "name": name, "robot": robot, "to": to}
            if by == "teacher":
                self.demo["key"] = f"teach.{secrets.token_urlsafe(12)}"
            else:
                await self.tell(self.groups[by], {"op": "present", "on": True})
            on = self.demo_on()
            for x in self.viewers():
                await self.tell(x, on)
            await self.prune()
            log.info("demo by %s on robot %s to %s", name, robot, "all" if to is None else len(to))
        elif op == "demo_end":
            await self.end_demo()

    async def close_class(self) -> None:
        await self.end_demo()
        for g in list(self.groups.values()):
            await self.free(g)
            await self.tell(g, {"op": "revoked", "reason": "closed"})
            await self.tell(g, {"op": "expired"})
        self.groups.clear()
        self.code, self.stop_all = None, False
        for r in self.robots:
            r.stopped = False
        await self.prune()

    # ---------- /robot/<n> ----------
    async def robot_conn(self, ws: WS, n: int) -> None:
        token = ws.query.get("token", "")
        r = self.robot(n)
        if not r or self.rights(token, n) is None:
            await ws.send(json.dumps({"op": "status", "level": "error", "msg": "denied: not your robot"}))
            return
        p = Proxy(ws, r, token)
        try:
            p.up = await connect(r.url)
        except (Closed, OSError, asyncio.TimeoutError):
            await ws.send(json.dumps({"op": "status", "level": "error", "msg": f"robot {n} rosbridge unreachable"}))
            return
        self.proxies.add(p)

        async def down() -> None:      # robot -> browser, as is
            while True:
                await ws.send(await p.up.recv())

        async def upward() -> None:     # browser -> robot, filtered
            while True:
                raw = await ws.recv()
                out = await self.filter(p, raw)
                if out is not None:
                    await p.up.send(out)

        tasks = [asyncio.create_task(down()), asyncio.create_task(upward())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            self.proxies.discard(p)
            await p.up.close()

    async def filter(self, p: Proxy, raw: str) -> str | None:
        """the message to pass to rosbridge, or None (refused, and the browser told why)"""
        try:
            m = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(m, dict):
            return None
        rights = self.rights(p.token, p.robot.n)
        if rights is None:
            await p.ws.close(4003)
            return None
        op = m.get("op")
        if rights == "teacher":
            return raw
        if op in ("subscribe", "unsubscribe", "advertise", "unadvertise", "set_level"):
            return raw
        reason = "demo" if rights == "demo" else "stopped" if self.stop_all else "role"
        if op == "publish":
            topic = m.get("topic")
            if reason == "role" and topic in PUBLISH.get(rights, set()):
                if topic == CMD_VEL:
                    self.clamp(m.get("msg") or {})
                    return json.dumps(m)
                return raw
            return await self.deny(p, m, f"publish {topic}", reason)
        if op == "call_service":
            svc = str(m.get("service", ""))
            if READ_SERVICES.search(svc):
                return raw
            if reason == "role" and rights in CAN_CANCEL and svc.endswith("/_action/cancel_goal"):
                return raw
            # a cancel during stop all / a demo is harmless: the hub already stopped the robot
            return await self.deny(p, m, f"call {svc}", reason)
        return await self.deny(p, m, str(op), reason)

    def clamp(self, msg: dict) -> None:
        t = msg.get("twist", msg)     # Twist or TwistStamped
        lin, ang = t.get("linear") or {}, t.get("angular") or {}
        for k in ("x", "y"):
            if isinstance(lin.get(k), (int, float)):
                lin[k] = max(-self.max_v, min(self.max_v, lin[k]))
        if isinstance(ang.get("z"), (int, float)):
            ang["z"] = max(-self.max_w, min(self.max_w, ang["z"]))

    async def deny(self, p: Proxy, m: dict, what: str, reason: str) -> None:
        try:
            await p.ws.send(json.dumps({"op": "status", "level": "error", "id": m.get("id"), "msg": f"denied: {reason}"}))
            if m.get("op") == "call_service":
                await p.ws.send(json.dumps({"op": "service_response", "id": m.get("id"), "service": m.get("service"), "result": False, "values": f"denied: {reason}"}))
        except Closed:
            pass
        await self.tell(self.by_token(p.token), {"op": "denied", "what": what, "reason": reason})
        return None

    # ---------- server ----------
    async def route(self, ws: WS) -> None:
        if ws.path.rstrip("/") == "/class":
            return await self.class_conn(ws)
        mm = re.fullmatch(r"/robot/(\d+)/?", ws.path)
        if mm:
            return await self.robot_conn(ws, int(mm.group(1)))
        await ws.close(4004)

    async def ticker(self) -> None:
        while True:
            await asyncio.sleep(1)
            await self.push()

    async def run(self, host: str = "0.0.0.0", port: int = 9000) -> None:
        server = await serve(self.route, host, port)
        for r in self.robots:
            asyncio.create_task(r.run(self.changed))
        asyncio.create_task(self.ticker())
        log.info("classroom hub on ws://%s:%d (%d robots)", host, port, len(self.robots))
        async with server:
            await server.serve_forever()


def robots_from_env(spec: str) -> list[Robot]:
    out = []
    for i, item in enumerate(x.strip() for x in spec.split(",") if x.strip()):
        parts = [x.strip() for x in item.split("|")]
        url = parts[-1]
        name = parts[0] if len(parts) >= 2 else f"หุ่น {i + 1} {COLOR_NAMES[i % 10]}"
        color = parts[1] if len(parts) == 3 else COLORS[i % 10]
        out.append(Robot(i + 1, name, color, url))
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    pin = os.environ.get("HUB_PIN", "1234")
    if pin == "1234":
        log.warning("HUB_PIN not set: the teacher PIN is 1234")
    elif len(pin) < 6:
        log.warning("HUB_PIN is shorter than 6 digits: easier to guess")
    hub = Hub(pin, robots_from_env(os.environ.get("HUB_ROBOTS", "ws://mtrobo:9090")), os.environ.get("CLASS_JOIN_URL", ""),
              float(os.environ.get("HUB_MAX_V", "0.25")), float(os.environ.get("HUB_MAX_W", "1.0")))
    asyncio.run(hub.run(port=int(os.environ.get("HUB_PORT", "9000"))))


if __name__ == "__main__":
    main()
