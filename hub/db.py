"""MTRobo classroom database on the teacher's PC (contract: mtrobo docs/contracts/database.md, part 4: admin / teachers).

SQLite, Python standard library only. The hub is the only writer. schema.sql is a copy of the contract's
docs/contracts/db/schema.sql: change both together.

Setup and accounts from the command line on the teacher's PC (the installer does this once):

    docker compose -f docker-compose.yml -f docker-compose.classroom.yml exec hub python db.py setup
    docker compose ... exec hub python db.py add-user --role teacher --name "ครูสมศรี"
    docker compose ... exec hub python db.py users
    docker compose ... exec hub python db.py --help

PINs are asked for without echo when --pin is not given (a PIN on the command line stays in the shell history).
"""
import argparse
import csv
import io
import datetime as dt
import getpass
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import sys
import uuid
from pathlib import Path

log = logging.getLogger("db")

HERE = Path(__file__).resolve().parent
ROLES = ("installer", "school_admin", "teacher")
SCHOOL_TYPES = ("public", "private", "tutor", "club")
BACKUPS_KEPT = 14
SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)   # ~50 ms per check: fine for a few teachers, slow for guessing


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id() -> str:
    return str(uuid.uuid4())


def hash_pin(pin: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pin.encode(), salt=salt, **SCRYPT)
    return f"scrypt${salt.hex()}${h.hex()}"


def pin_matches(pin: str, stored: str) -> bool:
    try:
        kind, salt, h = stored.split("$")
    except ValueError:
        return False
    if kind != "scrypt":
        return False
    got = hashlib.scrypt(pin.encode(), salt=bytes.fromhex(salt), **SCRYPT)
    return hmac.compare_digest(got.hex(), h)


def check_pin_rules(pin: str) -> str | None:
    """None if the PIN is acceptable, else why not (Thai, shown to the installer)"""
    if not pin.isdigit():
        return "PIN ต้องเป็นตัวเลขเท่านั้น"
    if len(pin) < 6:
        return "PIN ต้องยาวอย่างน้อย 6 หลัก"
    if len(set(pin)) == 1 or pin in "0123456789" or pin in "9876543210":
        return "PIN เดาง่ายเกินไป (เลขซ้ำหรือเรียงกัน)"
    return None


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys = ON")
        if not self.con.execute("SELECT 1 FROM sqlite_master WHERE name = 'schema_version'").fetchone():
            self.con.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
            log.info("database created: %s", self.path)
        if self.setting("code_secret") is None:
            self.set_setting("code_secret", secrets.token_hex(32))

    # ---------- settings ----------
    def setting(self, key: str) -> str | None:
        r = self.con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else None

    def set_setting(self, key: str, value: str) -> None:
        self.con.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                         (key, value))

    def code_hash(self, code: str) -> str:
        """student ID -> HMAC with this PC's secret (contract: the raw ID is never stored). Digits only."""
        digits = "".join(ch for ch in code if ch.isdigit())
        return hmac.new(bytes.fromhex(self.setting("code_secret")), digits.encode(), hashlib.sha256).hexdigest()

    # ---------- school ----------
    def school(self) -> sqlite3.Row | None:
        return self.con.execute("SELECT * FROM schools LIMIT 1").fetchone()

    def setup(self, name: str, province: str = "", type_: str = "public", connext_ed: bool = False,
              partner: str = "") -> str:
        if self.school():
            raise ValueError("ตั้งค่าโรงเรียนไว้แล้ว (เครื่องนี้มีได้ 1 โรงเรียน)")
        if type_ not in SCHOOL_TYPES:
            raise ValueError(f"type ต้องเป็นหนึ่งใน {', '.join(SCHOOL_TYPES)}")
        sid = new_id()
        self.con.execute("INSERT INTO schools (id, name, province, type, is_connext_ed, school_partner) VALUES (?,?,?,?,?,?)",
                         (sid, name, province or None, type_, int(connext_ed), partner or None))
        return sid

    def school_id(self) -> str:
        s = self.school()
        if not s:
            raise ValueError("ยังไม่ได้ตั้งค่าโรงเรียน: รัน python db.py setup ก่อน")
        return s["id"]

    # ---------- users ----------
    def add_user(self, role: str, full_name: str, pin: str, email: str = "") -> str:
        if role not in ROLES:
            raise ValueError(f"role ต้องเป็นหนึ่งใน {', '.join(ROLES)}")
        why = check_pin_rules(pin)
        if why:
            raise ValueError(why)
        if self.user_by_pin(pin):
            raise ValueError("PIN นี้มีคนใช้แล้ว ใช้ PIN อื่น")   # the PIN alone says who signs in
        uid = new_id()
        self.con.execute("INSERT INTO users (id, school_id, role, full_name, email, pin_hash, active, created_at) "
                         "VALUES (?,?,?,?,?,?,1,?)", (uid, self.school_id(), role, full_name, email or None, hash_pin(pin), utcnow()))
        return uid

    def users(self, active_only: bool = False) -> list[sqlite3.Row]:
        q = "SELECT id, role, full_name, email, active, created_at FROM users"
        return self.con.execute(q + (" WHERE active = 1" if active_only else "") + " ORDER BY created_at").fetchall()

    def has_users(self) -> bool:
        return self.con.execute("SELECT 1 FROM users WHERE active = 1 LIMIT 1").fetchone() is not None

    def user_by_pin(self, pin: str) -> sqlite3.Row | None:
        """the active user whose PIN this is, or None. Checks every active user (a few teachers per PC)."""
        for u in self.con.execute("SELECT * FROM users WHERE active = 1").fetchall():
            if pin_matches(pin, u["pin_hash"]):
                return u
        return None

    def find_user(self, key: str) -> sqlite3.Row:
        rows = self.con.execute("SELECT * FROM users WHERE id LIKE ? OR full_name = ?", (key + "%", key)).fetchall()
        if len(rows) != 1:
            raise ValueError("ไม่พบผู้ใช้" if not rows else "พบหลายคน: ใช้ id ให้ยาวขึ้น")
        return rows[0]

    def set_pin(self, key: str, pin: str) -> None:
        u = self.find_user(key)
        why = check_pin_rules(pin)
        if why:
            raise ValueError(why)
        other = self.user_by_pin(pin)
        if other and other["id"] != u["id"]:
            raise ValueError("PIN นี้มีคนใช้แล้ว ใช้ PIN อื่น")
        self.con.execute("UPDATE users SET pin_hash = ? WHERE id = ?", (hash_pin(pin), u["id"]))

    def set_active(self, key: str, active: bool) -> None:
        u = self.find_user(key)
        if not active and u["role"] in ("installer", "school_admin"):
            left = self.con.execute("SELECT COUNT(*) FROM users WHERE active = 1 AND role IN ('installer','school_admin') "
                                    "AND id != ?", (u["id"],)).fetchone()[0]
            if left == 0:
                raise ValueError("ต้องเหลือผู้ดูแล (school_admin หรือ installer) อย่างน้อย 1 คน")
        self.con.execute("UPDATE users SET active = ? WHERE id = ?", (int(active), u["id"]))

    # ---------- part 1: classrooms and students (roster) ----------
    def classroom(self, grade: str, room: str, level: str, year: int, term: int) -> str:
        """the classroom with these keys, made if new"""
        grade, room, level = grade.strip()[:12], room.strip()[:8], level.strip().upper()[:4]
        if not grade or not re.fullmatch(r"L[1-6]", level) or term not in (1, 2, 3) or not 2500 <= year <= 2700:
            raise ValueError("ข้อมูลห้องไม่ครบ: ชั้น, ระดับ L1–L6, ปีการศึกษา (พ.ศ.), ภาคเรียน 1–3")
        r = self.con.execute("SELECT id FROM classrooms WHERE school_id = ? AND academic_year = ? AND term = ? AND grade = ? "
                             "AND IFNULL(room,'') = ? AND level = ?", (self.school_id(), year, term, grade, room, level)).fetchone()
        if r:
            return r["id"]
        cid = new_id()
        self.con.execute("INSERT INTO classrooms (id, school_id, academic_year, term, grade, room, level) VALUES (?,?,?,?,?,?,?)",
                         (cid, self.school_id(), year, term, grade, room or None, level))
        return cid

    def import_roster(self, cid: str, text: str) -> dict:
        """CSV / tab text from the teacher: student ID (1–13 digits), number (เลขที่), nickname; other columns (names …)
        are dropped. A header row may name the columns (รหัส / เลขที่ / ชื่อเล่น), else they are in that order.
        Adds or updates students and enrols them in the classroom. The raw ID is only hashed, never stored."""
        c = self.con.execute("SELECT * FROM classrooms WHERE id = ?", (cid,)).fetchone()
        if not c:
            raise ValueError("ไม่พบห้องเรียน")
        lines = [ln for ln in text.replace("\ufeff", "").splitlines() if ln.strip()]
        if len(lines) > 400:
            raise ValueError("ไฟล์ยาวเกินไป (ไม่เกิน 400 คนต่อห้อง)")
        delim = "\t" if any("\t" in ln for ln in lines[:3]) else ","
        rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delim))
        col = {"code": 0, "no": 1, "nick": 2}
        start = 0
        if rows and rows[0] and not re.sub(r"\D", "", rows[0][0]):   # a header row: its first cell has no digits
            head = [h.strip().lower() for h in rows[0]]
            found = {}
            for i, h in enumerate(head):
                if "code" not in found and ("รหัส" in h or h in ("id", "code", "student id", "student_id", "student code")):
                    found["code"] = i
                elif "no" not in found and ("เลขที่" in h or h in ("no", "no.", "number")):
                    found["no"] = i
                elif "nick" not in found and ("ชื่อเล่น" in h or "nick" in h):
                    found["nick"] = i
            if "code" not in found:
                raise ValueError("ไม่พบคอลัมน์รหัสนักเรียนในหัวตาราง")
            col = {"code": found["code"], "no": found.get("no", -1), "nick": found.get("nick", -1)}
            start = 1
        added = updated = 0
        errors: list[dict] = []
        seen: set[str] = set()
        self.con.execute("BEGIN")
        try:
            for i, row in enumerate(rows[start:], start=start + 1):
                cell = lambda k: row[col[k]].strip() if 0 <= col[k] < len(row) else ""
                code = re.sub(r"[\s-]", "", cell("code"))
                if not re.fullmatch(r"\d{1,13}", code):
                    errors.append({"line": i, "reason": "รหัสต้องเป็นตัวเลข 1–13 หลัก"}); continue
                h = self.code_hash(code)
                if h in seen:
                    errors.append({"line": i, "reason": "รหัสซ้ำในไฟล์"}); continue
                seen.add(h)
                no = cell("no")
                no = int(no) if no.isdigit() and int(no) < 1000 else None
                nick = cell("nick")[:24] or None
                old = self.con.execute("SELECT id FROM students WHERE school_id = ? AND code_hash = ?", (c["school_id"], h)).fetchone()
                if old:
                    sid = old["id"]
                    self.con.execute("UPDATE students SET grade = ?, room = ?, number = COALESCE(?, number), "
                                     "nickname = COALESCE(?, nickname), archived_at = NULL WHERE id = ?",
                                     (c["grade"], c["room"], no, nick, sid))
                    updated += 1
                else:
                    sid = new_id()
                    self.con.execute("INSERT INTO students (id, school_id, code_hash, code_last4, grade, room, number, nickname) "
                                     "VALUES (?,?,?,?,?,?,?,?)", (sid, c["school_id"], h, code[-4:], c["grade"], c["room"], no, nick))
                    added += 1
                self.con.execute("INSERT OR IGNORE INTO enrollments (student_id, classroom_id) VALUES (?, ?)", (sid, cid))
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise
        return {"id": cid, "added": added, "updated": updated, "errors": errors[:50]}

    @staticmethod
    def roster_label(c) -> str:
        room = f"/{c['room']}" if c["room"] else ""
        return f"{c['grade']}{room} · {c['level']} · {c['term']}/{c['academic_year']}"

    def rosters(self) -> list[dict]:
        rows = self.con.execute("SELECT c.*, COUNT(e.student_id) AS n FROM classrooms c LEFT JOIN enrollments e ON e.classroom_id = c.id "
                                "GROUP BY c.id ORDER BY c.academic_year DESC, c.term DESC, c.grade, c.room").fetchall()
        return [{"id": c["id"], "label": self.roster_label(c), "grade": c["grade"], "room": c["room"] or "", "level": c["level"],
                 "year": c["academic_year"], "term": c["term"], "count": c["n"]} for c in rows]

    def roster_info(self, cid: str) -> dict | None:
        return next((r for r in self.rosters() if r["id"] == cid), None)

    def find_student(self, cid: str, code: str) -> dict | None:
        """the enrolled student with this ID in the classroom: {id, no, nick, last4}, or None"""
        code = re.sub(r"[\s-]", "", str(code))
        if not re.fullmatch(r"\d{1,13}", code):
            return None
        r = self.con.execute("SELECT s.id, s.number, s.nickname, s.code_last4 FROM students s JOIN enrollments e ON e.student_id = s.id "
                             "WHERE e.classroom_id = ? AND s.code_hash = ? AND s.archived_at IS NULL", (cid, self.code_hash(code))).fetchone()
        return {"id": r["id"], "no": r["number"], "nick": r["nickname"] or "", "last4": r["code_last4"]} if r else None

    # ---------- robots, licences, trainings ----------
    def add_robot(self, n: int, name: str = "", color: str = "", serial: str = "", firmware: str = "") -> str:
        rid = new_id()
        self.con.execute("INSERT INTO robots (id, school_id, n, serial, name, color, firmware) VALUES (?,?,?,?,?,?,?)",
                         (rid, self.school_id(), n, serial or None, name or None, color or None, firmware or None))
        return rid

    def robots(self) -> list[sqlite3.Row]:
        return self.con.execute("SELECT * FROM robots ORDER BY n").fetchall()

    def add_license(self, package: str, robot_count: int, starts_on: str, ends_on: str, price_thb: float | None = None) -> str:
        dt.date.fromisoformat(starts_on), dt.date.fromisoformat(ends_on)
        lid = new_id()
        self.con.execute("INSERT INTO licenses (id, school_id, package, robot_count, price_thb, starts_on, ends_on) "
                         "VALUES (?,?,?,?,?,?,?)", (lid, self.school_id(), package, robot_count, price_thb, starts_on, ends_on))
        return lid

    def licenses(self) -> list[sqlite3.Row]:
        return self.con.execute("SELECT * FROM licenses ORDER BY starts_on").fetchall()

    def add_training(self, key: str, course: str, level: str = "", passed_on: str = "", teach_alone: bool = False) -> str:
        u = self.find_user(key)
        if passed_on:
            dt.date.fromisoformat(passed_on)
        tid = new_id()
        self.con.execute("INSERT INTO teacher_trainings (id, user_id, course, level, passed_on, can_teach_alone) VALUES (?,?,?,?,?,?)",
                         (tid, u["id"], course, level or None, passed_on or None, int(teach_alone)))
        return tid

    # ---------- backup ----------
    def backup(self, folder: str | Path | None = None, today: str | None = None) -> Path:
        """a consistent copy (VACUUM INTO) as mtrobo-YYYY-MM-DD.db; keeps the newest BACKUPS_KEPT"""
        folder = Path(folder or Path(self.path).parent / "backup")
        folder.mkdir(parents=True, exist_ok=True)
        today = today or dt.date.today().isoformat()
        out = folder / f"mtrobo-{today}.db"
        if out.exists():
            out.unlink()
        self.con.execute("VACUUM INTO ?", (str(out),))
        self.set_setting("last_backup", today)
        for old in sorted(folder.glob("mtrobo-*.db"))[:-BACKUPS_KEPT]:
            old.unlink()
        return out

    def backup_if_due(self, folder: str | Path | None = None) -> Path | None:
        today = dt.date.today().isoformat()
        if self.setting("last_backup") == today or not self.school():
            return None
        return self.backup(folder, today)


# ---------- command line (installer / school admin on the teacher's PC) ----------
def ask_pin(given: str | None) -> str:
    if given:
        return given
    a = getpass.getpass("PIN (ตัวเลข 6 หลักขึ้นไป): ")
    b = getpass.getpass("PIN อีกครั้ง: ")
    if a != b:
        raise ValueError("PIN สองครั้งไม่ตรงกัน")
    return a


def cli(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="db.py", description="ฐานข้อมูลห้องเรียน MTRobo บนเครื่องครู")
    p.add_argument("--db", default=os.environ.get("HUB_DB", "/data/mtrobo.db"), help="ไฟล์ฐานข้อมูล (HUB_DB)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="ตั้งค่าโรงเรียนและผู้ดูแลคนแรก (ครั้งเดียว)")
    s.add_argument("--school", required=True); s.add_argument("--province", default="")
    s.add_argument("--type", default="public", choices=SCHOOL_TYPES)
    s.add_argument("--connext-ed", action="store_true"); s.add_argument("--partner", default="")
    s.add_argument("--admin", required=True, help="ชื่อผู้ดูแลของโรงเรียน (school_admin)")
    s.add_argument("--pin")
    s = sub.add_parser("add-user", help="เพิ่มครูหรือผู้ดูแล")
    s.add_argument("--role", default="teacher", choices=ROLES); s.add_argument("--name", required=True)
    s.add_argument("--email", default=""); s.add_argument("--pin")
    sub.add_parser("users", help="รายชื่อผู้ใช้")
    s = sub.add_parser("set-pin", help="เปลี่ยน PIN"); s.add_argument("user", help="id (ต้นๆ ก็ได้) หรือชื่อ"); s.add_argument("--pin")
    s = sub.add_parser("disable", help="ปิดผู้ใช้"); s.add_argument("user")
    s = sub.add_parser("enable", help="เปิดผู้ใช้อีกครั้ง"); s.add_argument("user")
    s = sub.add_parser("add-robot", help="เพิ่มหุ่น"); s.add_argument("--n", type=int, required=True)
    s.add_argument("--name", default=""); s.add_argument("--color", default=""); s.add_argument("--serial", default="")
    s = sub.add_parser("add-license", help="เพิ่ม license"); s.add_argument("--package", required=True)
    s.add_argument("--robots", type=int, required=True); s.add_argument("--from", dest="starts", required=True)
    s.add_argument("--to", dest="ends", required=True); s.add_argument("--price", type=float)
    s = sub.add_parser("add-training", help="บันทึกการอบรมครู"); s.add_argument("user"); s.add_argument("--course", required=True)
    s.add_argument("--level", default=""); s.add_argument("--passed", default="", help="YYYY-MM-DD")
    s.add_argument("--teach-alone", action="store_true")
    s = sub.add_parser("info", help="สรุปโรงเรียน หุ่น license")
    s = sub.add_parser("backup", help="สำรองข้อมูลตอนนี้"); s.add_argument("--to", default=None)
    a = p.parse_args(argv)
    db = DB(a.db)
    try:
        if a.cmd == "setup":
            db.setup(a.school, a.province, a.type, a.connext_ed, a.partner)
            db.add_user("school_admin", a.admin, ask_pin(a.pin))
            print(f"ตั้งค่าแล้ว: {a.school} ผู้ดูแล {a.admin}")
        elif a.cmd == "add-user":
            uid = db.add_user(a.role, a.name, ask_pin(a.pin), a.email)
            print(f"เพิ่มแล้ว: {a.name} ({a.role}) id {uid[:8]}")
        elif a.cmd == "users":
            for u in db.users():
                print(f"{u['id'][:8]}  {u['role']:<12} {'ใช้งาน' if u['active'] else 'ปิด':<6} {u['full_name']}")
        elif a.cmd == "set-pin":
            db.set_pin(a.user, ask_pin(a.pin)); print("เปลี่ยน PIN แล้ว")
        elif a.cmd in ("disable", "enable"):
            db.set_active(a.user, a.cmd == "enable"); print("เปลี่ยนแล้ว")
        elif a.cmd == "add-robot":
            db.add_robot(a.n, a.name, a.color, a.serial); print(f"เพิ่มหุ่น {a.n} แล้ว")
        elif a.cmd == "add-license":
            db.add_license(a.package, a.robots, a.starts, a.ends, a.price); print("เพิ่ม license แล้ว")
        elif a.cmd == "add-training":
            db.add_training(a.user, a.course, a.level, a.passed, a.teach_alone); print("บันทึกแล้ว")
        elif a.cmd == "info":
            s = db.school()
            print(f"โรงเรียน: {s['name'] if s else '(ยังไม่ตั้งค่า)'}")
            for r in db.robots():
                print(f"หุ่น {r['n']}: {r['name'] or ''} {r['color'] or ''}")
            for l in db.licenses():
                print(f"license {l['package']} หุ่น {l['robot_count']} ตัว {l['starts_on']} ถึง {l['ends_on']}")
            print(f"สำรองล่าสุด: {db.setting('last_backup') or '-'}")
        elif a.cmd == "backup":
            print(f"สำรองแล้ว: {db.backup(a.to)}")
    except (ValueError, sqlite3.IntegrityError) as e:
        print(f"ไม่สำเร็จ: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(cli())
