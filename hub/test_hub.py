"""Classroom hub test without Docker or robots: two fake rosbridge servers, the hub, and WebSocket clients playing the
teacher page, two student groups and the teacher's demo tab, with the messages the mtrobo web sends.

    cd hub && python3 test_hub.py
"""
import asyncio
import json
import sys

from hub import Hub, Robot
from wsproto import Closed, connect, serve

fails: list[str] = []


def ok(cond, what: str) -> None:
    if not cond:
        fails.append(what)
        print("FAIL", what)


class FakeRosbridge:
    """records what reaches the robot; answers subscribe with one message and call_service with success"""

    def __init__(self) -> None:
        self.got: list[dict] = []
        self.port = 0

    async def conn(self, ws) -> None:
        while True:
            m = json.loads(await ws.recv())
            self.got.append(m)
            if m["op"] == "subscribe":
                msg = {"/battery": {"percentage": 0.8}, "/robot_status": {"data": [-55.0, 1, 2]}}.get(m["topic"], {"data": m["topic"]})
                await ws.send(json.dumps({"op": "publish", "topic": m["topic"], "msg": msg}))
            elif m["op"] == "call_service":
                await ws.send(json.dumps({"op": "service_response", "id": m.get("id"), "service": m["service"], "result": True, "values": {}}))

    async def start(self) -> None:
        s = await serve(self.conn, "127.0.0.1", 0)
        self.port = s.sockets[0].getsockname()[1]

    def pubs(self, topic: str) -> list[dict]:
        return [m for m in self.got if m["op"] == "publish" and m["topic"] == topic]

    def calls(self, suffix: str) -> list[dict]:
        return [m for m in self.got if m["op"] == "call_service" and m["service"].endswith(suffix)]


class Client:
    """a page's WebSocket: collects everything it receives"""

    def __init__(self, ws) -> None:
        self.ws, self.got = ws, []
        self.task = asyncio.create_task(self.read())

    async def read(self) -> None:
        try:
            while True:
                self.got.append(json.loads(await self.ws.recv()))
        except Closed:
            self.got.append({"op": "_closed"})

    async def send(self, m: dict) -> None:
        await self.ws.send(json.dumps(m))

    def last(self, op: str) -> dict | None:
        return next((m for m in reversed(self.got) if m.get("op") == op), None)

    def closed(self) -> bool:
        return any(m.get("op") == "_closed" for m in self.got)


async def settle(t: float = 0.15) -> None:
    await asyncio.sleep(t)


async def main() -> None:
    R1, R2 = FakeRosbridge(), FakeRosbridge()
    await R1.start()
    await R2.start()
    hub = Hub("2468", [Robot(1, "หุ่น 1", "#f59f00", f"ws://127.0.0.1:{R1.port}"), Robot(2, "หุ่น 2", "#1c7ed6", f"ws://127.0.0.1:{R2.port}")],
              join="http://192.168.1.105:3000/join", max_v=0.25, max_w=1.0)
    server = await serve(hub.route, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    for r in hub.robots:
        asyncio.create_task(r.run(hub.changed))
    asyncio.create_task(hub.ticker())
    H = f"ws://127.0.0.1:{port}"
    page = lambda path: connect(H + path)   # noqa: E731

    # ---------- teacher ----------
    T = Client(await page("/class"))
    await T.send({"op": "open"})
    await settle()
    ok(T.last("error") and T.last("error")["reason"] == "pin", "teacher command without PIN not refused")
    await T.send({"op": "teacher", "pin": "0000"})
    await settle()
    ok(T.last("error")["reason"] == "pin", "wrong PIN accepted")
    await T.send({"op": "teacher", "pin": "2468"})
    await T.send({"op": "open"})
    await settle(0.3)
    st = T.last("class")["state"]
    ok(st and len(st["code"]) == 4 and st["join"] == "http://192.168.1.105:3000/join", f"class state {st}")
    code = st["code"]
    await settle(1.2)
    st = T.last("class")["state"]
    ok(st["robots"][0]["online"] and st["robots"][0]["battery"] == 80 and st["robots"][0]["rssi"] == -55, f"robot card {st['robots'][0]}")

    # ---------- groups ----------
    A, B = Client(await page("/class")), Client(await page("/class"))
    await A.send({"op": "join", "code": "0000", "group": "แมว"})
    await settle()
    ok(A.last("error")["reason"] == "code", "wrong code accepted")
    await A.send({"op": "join", "code": code, "group": "แมว"})
    await B.send({"op": "join", "code": code, "group": "แมว"})
    await settle()
    ok(B.last("error")["reason"] == "taken", "same group name twice")
    await B.send({"op": "join", "code": code, "group": "หมา"})
    await settle()
    ta, tb = A.last("waiting")["token"], B.last("waiting")["token"]
    ga, gb = [g["id"] for g in T.last("class")["state"]["groups"]]
    await T.send({"op": "assign", "group": ga, "robot": 1, "role": "drive"})
    await settle()
    ok(A.last("granted") and A.last("granted")["robot"]["n"] == 1 and A.last("granted")["role"] == "drive", "A not granted robot 1")

    # ---------- /robot/<n> filtering ----------
    PA = Client(await page(f"/robot/1?token={ta}"))
    await PA.send({"op": "subscribe", "id": "s1", "topic": "/scan", "type": "sensor_msgs/msg/LaserScan"})
    await PA.send({"op": "advertise", "topic": "/cmd_vel", "type": "geometry_msgs/msg/Twist"})
    await PA.send({"op": "publish", "topic": "/cmd_vel", "msg": {"linear": {"x": 1.0}, "angular": {"z": -3.0}}})
    await PA.send({"op": "publish", "topic": "/mtrobo/map/save", "msg": {"data": "{}"}})
    await PA.send({"op": "call_service", "id": "c1", "service": "/rosapi/topics", "type": "rosapi_msgs/srv/Topics", "args": {}})
    await PA.send({"op": "call_service", "id": "c2", "service": "/controller_server/set_parameters", "type": "x", "args": {}})
    await settle()
    ok(any(m.get("topic") == "/scan" for m in PA.got), "subscribe did not pass through")
    cv = R1.pubs("/cmd_vel")
    ok(cv and cv[-1]["msg"]["linear"]["x"] == 0.25 and cv[-1]["msg"]["angular"]["z"] == -1.0, f"cmd_vel not clamped: {cv[-1:]} ")
    ok(not R1.pubs("/mtrobo/map/save"), "student published /mtrobo/map/save")
    ok(any(m.get("op") == "service_response" and m.get("id") == "c1" and m.get("result") for m in PA.got), "read-only service refused")
    ok(not R1.calls("/set_parameters") and any(m.get("id") == "c2" and m.get("result") is False for m in PA.got), "set_parameters not refused")
    ok(A.last("denied") is not None, "group not told about a refusal")
    PB = Client(await page(f"/robot/1?token={tb}"))
    await settle()
    ok(any("denied" in str(m.get("msg", "")) for m in PB.got) and PB.closed(), "group B reached robot 1")

    # role watch: no driving
    await T.send({"op": "assign", "group": ga, "robot": 1, "role": "watch"})
    await settle()
    n = len(R1.pubs("/cmd_vel"))
    await PA.send({"op": "publish", "topic": "/cmd_vel", "msg": {"linear": {"x": 0.1}}})
    await settle()
    ok(len(R1.pubs("/cmd_vel")) == n, "watch role drove the robot")

    # stop all: zero + cancel on every robot, students refused with reason stopped
    await T.send({"op": "assign", "group": ga, "robot": 1, "role": "drive"})
    R1.got.clear(); R2.got.clear()
    await T.send({"op": "stop_all", "on": True})
    await settle()
    ok(A.last("stop_all") and A.last("stop_all")["on"], "group not told stop all")
    z1 = R1.pubs("/cmd_vel")
    ok(len(z1) >= 3 and all(m["msg"]["linear"]["x"] == 0.0 for m in z1), "stop all: robot 1 not zeroed")
    ok(R1.calls("/navigate_to_pose/_action/cancel_goal") and R2.calls("/navigate_to_pose/_action/cancel_goal"), "stop all: goals not cancelled")
    await PA.send({"op": "publish", "topic": "/goal_pose", "msg": {}})
    await settle()
    ok(not R1.pubs("/goal_pose") and A.last("denied")["reason"] == "stopped", "goal during stop all")
    await T.send({"op": "stop_all", "on": False})
    await settle()
    await PA.send({"op": "publish", "topic": "/goal_pose", "msg": {}})
    await settle()
    ok(R1.pubs("/goal_pose"), "goal refused after stop all released")

    # ---------- demo: teacher on robot 2, whole class ----------
    await T.send({"op": "demo_start", "by": "teacher", "robot": 2})
    await settle()
    D = T.last("class")["state"]["demo"]
    ok(D and D["robot"] == 2 and D.get("key"), f"demo state {D}")
    for c, nm in ((A, "A"), (B, "B")):
        ok(c.last("demo") and c.last("demo")["on"] and c.last("demo")["robot"]["n"] == 2, f"{nm}: no demo on")
    TT = Client(await page("/class"))
    await TT.send({"op": "resume", "token": D["key"]})
    await settle()
    ok(TT.last("granted") and TT.last("present")["on"], "teacher demo tab not granted")
    PK = Client(await page(f"/robot/2?token={D['key']}"))
    await PK.send({"op": "publish", "topic": "/mtrobo/route/go", "msg": {"data": "{}"}})
    await TT.send({"op": "demo_view", "token": D["key"], "seq": 5, "view": {"tab": "nav", "blocks": True}})
    VB = Client(await page(f"/robot/2?token={tb}"))
    await VB.send({"op": "subscribe", "id": "s", "topic": "/map", "type": "nav_msgs/msg/OccupancyGrid"})
    await VB.send({"op": "publish", "topic": "/cmd_vel", "msg": {"linear": {"x": 0.1}}})
    await settle()
    ok(R2.pubs("/mtrobo/route/go"), "teacher demo key has no teacher rights")
    ok(B.last("demo_view") and B.last("demo_view")["view"]["blocks"] is True, "view not fanned out")
    ok(any(m.get("topic") == "/map" for m in VB.got), "viewer cannot watch the demo robot")
    ok(not [m for m in R2.pubs("/cmd_vel") if m["msg"]["linear"]["x"]] and B.last("denied")["reason"] == "demo", f"viewer drove the demo robot: {R2.pubs('/cmd_vel')} {B.last('denied')}")
    # a viewer reconnecting lands in the demo with the latest view
    B2 = Client(await page("/class"))
    await B2.send({"op": "resume", "token": tb})
    await settle()
    ok(B2.last("demo") and B2.last("demo")["view"].get("tab") == "nav", "resume mid-demo")
    await T.send({"op": "demo_end"})
    await settle()
    ok(A.last("demo")["on"] is False and TT.last("expired"), "demo end not told")
    ok(PK.closed() and VB.closed(), "demo robot connections not closed after the demo")

    # ---------- a group presents to the chosen group only ----------
    await T.send({"op": "demo_start", "by": ga, "to": [gb]})
    await settle()
    ok(A.last("present")["on"] is True and B2.last("demo")["on"] is True, "group presenting")
    await A.send({"op": "demo_view", "token": ta, "seq": 1, "view": {"cam": {"tx": 1}}})
    await B2.send({"op": "demo_view", "token": tb, "seq": 2, "view": {"tab": "x"}})
    await settle()
    ok(B2.last("demo_view")["view"].get("cam") == {"tx": 1}, "presenter view not fanned out")
    ok(B2.last("denied")["what"] == "demo_view", "viewer could send a view")
    # the presenter taken off its robot ends the demo, and its robot connection closes
    await T.send({"op": "unassign", "group": ga})
    await settle()
    ok(T.last("class")["state"]["demo"] is None and B2.last("demo")["on"] is False, "unassign did not end the demo")
    ok(PA.closed(), "robot connection stayed open after unassign")
    ok(A.last("waiting") and A.last("revoked")["reason"] == "teacher", "A not sent back to wait")

    # ---------- close ----------
    await T.send({"op": "close"})
    await settle()
    ok(T.last("class")["state"] is None and A.last("expired") and B2.last("expired"), "close")

    server.close()
    print("FAILED" if fails else "OK hub", f"({len(fails)} failures)")
    sys.exit(1 if fails else 0)


asyncio.run(main())
