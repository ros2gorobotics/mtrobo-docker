"""Classroom database, part 4 (admin / teachers), and the hub's sign-in with teachers' own PINs.

    cd hub && python3 test_db.py
"""
import asyncio
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import db as dbm
from db import DB, cli
from hub import Hub
from wsproto import connect

fails: list[str] = []


def ok(cond, what: str) -> None:
    if not cond:
        fails.append(what)
        print("FAIL", what)


def raises(fn, what: str) -> None:
    try:
        fn()
    except ValueError:
        return
    fails.append(what)
    print("FAIL", what)


def test_db(tmp: Path) -> DB:
    d = DB(tmp / "mtrobo.db")
    ok(d.setting("code_secret") and len(d.setting("code_secret")) == 64, "no code secret made")
    secret = d.setting("code_secret")
    ok(DB(tmp / "mtrobo.db").setting("code_secret") == secret, "code secret changed on reopen")
    raises(lambda: d.add_user("teacher", "ก", "246813"), "user added before setup")
    d.setup("โรงเรียนทดสอบ", "ขอนแก่น", "public", True, "P")
    raises(lambda: d.setup("อีกโรงเรียน"), "second school accepted")
    raises(lambda: d.add_user("teacher", "ก", "1234"), "short PIN accepted")
    raises(lambda: d.add_user("teacher", "ก", "111111"), "repeated-digit PIN accepted")
    raises(lambda: d.add_user("teacher", "ก", "123456"), "sequential PIN accepted")
    raises(lambda: d.add_user("teacher", "ก", "24681a"), "non-digit PIN accepted")
    raises(lambda: d.add_user("boss", "ก", "246813"), "unknown role accepted")
    admin = d.add_user("school_admin", "ผอ.", "975310")
    t1 = d.add_user("teacher", "ครูสมศรี", "246813")
    raises(lambda: d.add_user("teacher", "ครูอื่น", "246813"), "duplicate PIN accepted")
    row = d.con.execute("SELECT pin_hash FROM users WHERE id = ?", (t1,)).fetchone()
    ok("246813" not in row["pin_hash"] and row["pin_hash"].startswith("scrypt$"), "PIN stored in clear")
    ok(d.user_by_pin("246813")["id"] == t1, "teacher PIN not found")
    ok(d.user_by_pin("975310")["id"] == admin, "admin PIN not found")
    ok(d.user_by_pin("000000") is None, "wrong PIN found a user")
    d.set_pin("ครูสมศรี", "802461")
    ok(d.user_by_pin("246813") is None and d.user_by_pin("802461")["id"] == t1, "set-pin did not change the PIN")
    d.set_active(t1[:8], False)
    ok(d.user_by_pin("802461") is None, "disabled teacher can sign in")
    raises(lambda: d.set_active(admin, False), "last admin disabled")
    d.set_active(t1, True)
    ok(d.user_by_pin("802461") is not None, "re-enabled teacher cannot sign in")
    d.add_robot(1, "แมวส้ม", "#f97316")
    raises(lambda: d.add_license("L2", 1, "2026-13-01", "2028-11-01"), "bad date accepted")
    d.add_license("L2", 1, "2026-11-01", "2028-11-01", 19900)
    d.add_training("ครูสมศรี", "MTRobo พื้นฐาน", "L2", "2026-11-20", True)
    ok(tuple(d.con.execute("SELECT trained, teach_alone FROM v_teachers_trained").fetchone()) == (1, 1), "v_teachers_trained wrong")
    try:
        d.add_robot(1, "ซ้ำ")
        fails.append("duplicate robot number accepted"); print("FAIL duplicate robot number accepted")
    except sqlite3.IntegrityError:
        pass
    h = d.code_hash("1-1007-01234-56-7")
    ok(h == d.code_hash("1100701234567") and len(h) == 64 and "1234567" not in h, "student ID hash wrong")
    # backups: one per day, newest 14 kept, a copy that opens
    for day in range(1, 18):
        d.backup(tmp / "bk", f"2026-11-{day:02d}")
    files = sorted((tmp / "bk").glob("mtrobo-*.db"))
    ok(len(files) == dbm.BACKUPS_KEPT and files[0].name == "mtrobo-2026-11-04.db", f"backup rotation wrong: {len(files)}")
    ok(sqlite3.connect(files[-1]).execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2, "backup copy is not the data")
    d.set_setting("last_backup", "2000-01-01")
    ok(d.backup_if_due(tmp / "bk") is not None and d.backup_if_due(tmp / "bk") is None, "backup_if_due not once a day")
    # part 1: roster import (header row with a names column that must be dropped, dashes in IDs, bad lines)
    raises(lambda: d.classroom("ป.5", "1", "L9", 2569, 2), "bad level accepted")
    cid = d.classroom("ป.5", "1", "L2", 2569, 2)
    ok(d.classroom("ป.5", "1", "l2", 2569, 2) == cid, "same classroom made twice")
    csv_text = "\ufeffเลขที่,รหัสนักเรียน,ชื่อ-สกุล,ชื่อเล่น\n3,1-1007-01234-56-7,สมชาย ใจดี,ภูมิ\n7,1209900456781,สมหญิง รักเรียน,ต้นข้าว\n" \
               "8,12AB,x,y\n9,1209900456781,ซ้ำ,ซ้ำ\n"
    raises(lambda: d.import_roster(cid, "ชื่อ,นามสกุล\nก,ข\n"), "CSV without an ID column accepted")
    r = d.import_roster(cid, csv_text.replace("เลขที่,รหัสนักเรียน", "เลขที่,รหัสนักเรียน"))
    ok(r["added"] == 2 and r["updated"] == 0 and [e["line"] for e in r["errors"]] == [4, 5], f"roster import wrong: {r}")
    dump = "\n".join(d.con.iterdump())
    ok("1100701234567" not in dump and "สมชาย" not in dump and "สมหญิง" not in dump, "raw student ID or real name stored")
    ok(d.find_student(cid, "1100701234567") == {"id": d.find_student(cid, "1-1007-01234-56-7")["id"], "no": 3, "nick": "ภูมิ", "last4": "4567"},
       "student lookup wrong")
    ok(d.find_student(cid, "1100701234568") is None, "unknown ID found")
    r2 = d.import_roster(cid, "1100701234567\t3\tภูมิใจ\n")
    ok(r2["added"] == 0 and r2["updated"] == 1 and d.find_student(cid, "1100701234567")["nick"] == "ภูมิใจ", f"re-import wrong: {r2}")
    ro = d.rosters()
    ok(len(ro) == 1 and ro[0]["count"] == 2 and ro[0]["label"] == "ป.5/1 · L2 · 2/2569", f"rosters wrong: {ro}")
    # command line
    p = str(tmp / "cli.db")
    ok(cli(["--db", p, "setup", "--school", "ร.ร.", "--admin", "ผอ.", "--pin", "135792"]) == 0, "cli setup failed")
    ok(cli(["--db", p, "add-user", "--name", "ครู ข", "--pin", "1111"]) == 1, "cli accepted a short PIN")
    ok(cli(["--db", p, "add-user", "--name", "ครู ข", "--pin", "864209"]) == 0, "cli add-user failed")
    ok(cli(["--db", p, "users"]) == 0, "cli users failed")
    return d


def main() -> int:
    with tempfile.TemporaryDirectory() as t:
        d = test_db(Path(t))
        # hub sign-in on a free port
        import socket
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()

        async def run() -> None:
            hub = Hub("2468", [], sim=2, db=d)
            task = asyncio.create_task(hub.run(host="127.0.0.1", port=port))
            await asyncio.sleep(0.3)
            ws = await connect(f"ws://127.0.0.1:{port}/class")

            async def ask(pin: str) -> dict:
                await ws.send(json.dumps({"op": "teacher", "pin": pin}))
                return json.loads(await asyncio.wait_for(ws.recv(), 5))

            ok((await ask("2468"))["op"] == "error", "HUB_PIN still works once the database has users")
            ok((await ask("802461"))["op"] == "class", "teacher's own PIN refused")
            ok(list(hub.teacher_user.values()) == [d.user_by_pin("802461")["id"]], "hub did not record who signed in")
            # v1.4: a lesson with a roster: students join with their IDs
            cid = d.rosters()[0]["id"]
            await ws.send(json.dumps({"op": "rosters"}))
            r = json.loads(await asyncio.wait_for(ws.recv(), 5))
            ok(r["op"] == "rosters" and r["list"][0]["id"] == cid, "rosters op wrong")
            await ws.send(json.dumps({"op": "roster_import", "room": {"grade": "ป.5", "room": "1", "level": "L2", "year": 2569, "term": 2},
                                      "csv": "1103300987654,12,มายด์"}))
            r = json.loads(await asyncio.wait_for(ws.recv(), 5))
            ok(r["op"] == "roster_done" and r["added"] == 1 and r["id"] == cid, f"roster_import op wrong: {r}")
            await ws.recv()   # rosters
            await ws.send(json.dumps({"op": "open", "robots": 2, "roster": cid}))
            st = None
            for _ in range(5):
                r = json.loads(await asyncio.wait_for(ws.recv(), 5))
                if r["op"] == "class" and r["state"]:
                    st = r["state"]; break
            ok(st and st.get("roster", {}).get("id") == cid, "lesson has no roster")
            code = st["code"]
            s1 = await connect(f"ws://127.0.0.1:{port}/class")

            async def say(m: dict) -> dict:
                await s1.send(json.dumps(m))
                while True:
                    x = json.loads(await asyncio.wait_for(s1.recv(), 5))
                    if x["op"] not in ("style", "demo"):
                        return x

            x = await say({"op": "peek", "code": code})
            ok(x == {"op": "peek", "ok": True, "roster": True, "label": "ป.5/1 · L2 · 2/2569"}, f"peek wrong: {x}")
            x = await say({"op": "check_student", "code": code, "student": "1-1007-01234-56-7"})
            ok(x.get("ok") and x["nick"] == "ภูมิใจ" and x["last4"] == "4567", f"check_student wrong: {x}")
            x = await say({"op": "check_student", "code": code, "student": "9999999999999"})
            ok(x == {"op": "student", "ok": False, "reason": "student"}, f"unknown ID accepted: {x}")
            x = await say({"op": "join", "code": code, "group": "แมว"})
            ok(x == {"op": "error", "reason": "students"}, f"join without IDs accepted: {x}")
            x = await say({"op": "join", "code": code, "group": "แมว", "students": ["1100701234567", "0000000000000"]})
            ok(x == {"op": "error", "reason": "student", "bad": [1]}, f"bad ID in join: {x}")
            x = await say({"op": "join", "code": code, "group": "แมว", "students": ["1100701234567", "1209900456781"]})
            ok(x["op"] == "waiting", f"join with IDs refused: {x}")
            s2 = await connect(f"ws://127.0.0.1:{port}/class")
            await s2.send(json.dumps({"op": "join", "code": code, "group": "หมา", "students": ["1103300987654", "1209900456781"]}))
            x = json.loads(await asyncio.wait_for(s2.recv(), 5))
            ok(x == {"op": "error", "reason": "student_taken", "bad": [1]}, f"student in two groups: {x}")
            g = [v for v in hub.groups.values()][0].card()
            ok(g["students"] == [{"no": 3, "nick": "ภูมิใจ", "last4": "4567"}, {"no": 7, "nick": "ต้นข้าว", "last4": "6781"}],
               f"group card students wrong: {g}")
            ok("id" not in str(g["students"]), "student db ids leak to the teacher page")
            # part 3: usage written by the hub (assign = a robot run, stop, close ends everything)
            gid = [v for v in hub.groups.values()][0].id
            await ws.send(json.dumps({"op": "assign", "group": gid, "robot": 1, "role": "drive"}))
            await asyncio.sleep(0.2)
            await ws.send(json.dumps({"op": "stop", "robot": 1}))
            await ws.send(json.dumps({"op": "assign", "group": gid, "robot": 1, "role": "blocks"}))
            await asyncio.sleep(0.2)
            await ws.send(json.dumps({"op": "close"}))
            await asyncio.sleep(0.3)
            c = d.con
            sess = c.execute("SELECT * FROM class_sessions").fetchall()
            ok(len(sess) == 1 and sess[0]["classroom_id"] == cid and sess[0]["teacher_id"] == d.user_by_pin("802461")["id"]
               and sess[0]["ended_at"] >= sess[0]["started_at"], f"class session wrong: {[dict(x) for x in sess]}")
            att = c.execute("SELECT student_id, left_at FROM attendance").fetchall()
            ok(len(att) == 2 and all(x["left_at"] for x in att), f"attendance wrong: {[dict(x) for x in att]}")
            runs = c.execute("SELECT mode, role, teacher_stops, ended_at FROM robot_runs ORDER BY started_at").fetchall()
            ok([(r["mode"], r["role"], r["teacher_stops"]) for r in runs] == [("sim", "drive", 1), ("sim", "blocks", 0)]
               and all(r["ended_at"] for r in runs), f"robot runs wrong: {[dict(x) for x in runs]}")
            ok(c.execute("SELECT COUNT(*) FROM group_members").fetchone()[0] == 2, "group members not written")
            ev = [r[0] for r in c.execute("SELECT type FROM events ORDER BY ts")]
            ok({"join", "assign", "close"} <= set(ev), f"events wrong: {ev}")
            # 45 minutes long: counts as teaching hours and students reached for 2/2569
            c.execute("UPDATE class_sessions SET started_at = '2026-11-25T02:00:00Z', ended_at = '2026-11-25T02:45:00Z'")
            c.execute("UPDATE robot_runs SET started_at = '2026-11-25T02:05:00Z', ended_at = '2026-11-25T02:35:00Z'")
            u = d.usage_summary()
            ok(u == [{"year": 2569, "term": 2, "hours": 0.8, "hours_real": 0.0, "sessions": 1, "students_year": 2}], f"usage summary wrong: {u}")
            # a power cut: an open lesson is closed at its last minute on the next start
            sid2 = d.session_open(cid, None, "1111")
            d.run_start(sid2, None, "real", 1, "drive")
            ok(d.close_dangling() == 1 and c.execute("SELECT COUNT(*) FROM robot_runs WHERE ended_at IS NULL").fetchone()[0] == 0,
               "dangling lesson not closed")
            await s1.close(); await s2.close()
            await ws.close()
            task.cancel()
            # no users in the database: HUB_PIN works
            e = DB(Path(t) / "empty.db")
            hub2 = Hub("2468", [], sim=2, db=e)
            ok(await hub2.check_pin("2468") == (True, None), "HUB_PIN refused with an empty database")
            ok((await hub2.check_pin("0000"))[0] is False, "wrong PIN accepted with an empty database")

        asyncio.run(run())
    print("OK" if not fails else f"{len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
