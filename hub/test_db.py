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
