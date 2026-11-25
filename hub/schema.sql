-- MTRobo classroom database (SQLite), v0 draft, 2026-11-25.
-- Lives on the teacher's PC, owned by the classroom hub. Contract: docs/contracts/database.md
-- Build order (owner 2026-11-25): 4 admin/teachers -> 1 students -> 3 usage -> 5 worksheets -> 2 reports.
--
-- Conventions
--   * ids are TEXT UUIDs made on the PC, so files from two PCs (or a cloud later) merge without clashes.
--   * times are TEXT, ISO 8601 UTC ('2026-11-25T07:40:00Z'); dates are 'YYYY-MM-DD'.
--   * usage rows are written by the hub only; teachers cannot type hours.
--   * no national ID, birth date or photo of a student anywhere.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL);
INSERT INTO schema_version VALUES (0, strftime('%Y-%m-%dT%H:%M:%SZ','now'));

-- Hub settings on this PC: 'code_secret' (hex, the HMAC key for student IDs), 'last_backup' (date).
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- ───────────── 4. Admin / teachers ─────────────
CREATE TABLE schools (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  province      TEXT,
  type          TEXT CHECK (type IN ('public','private','tutor','club')),
  is_connext_ed INTEGER NOT NULL DEFAULT 0,
  school_partner TEXT                          -- name of the CONNEXT ED School Partner, if any
);

CREATE TABLE licenses (
  id          TEXT PRIMARY KEY,
  school_id   TEXT NOT NULL REFERENCES schools(id),
  package     TEXT NOT NULL,                   -- 'L2' | 'L3' | 'L4' | ...
  robot_count INTEGER NOT NULL,
  price_thb   REAL,                            -- for cost per head
  starts_on   TEXT NOT NULL,
  ends_on     TEXT NOT NULL
);

CREATE TABLE robots (
  id        TEXT PRIMARY KEY,
  school_id TEXT NOT NULL REFERENCES schools(id),
  n         INTEGER NOT NULL,                  -- number used by the hub (/robot/<n>)
  serial    TEXT,
  name      TEXT,
  color     TEXT,
  firmware  TEXT,
  UNIQUE (school_id, n)
);

-- Local accounts on this PC. No passwords for students.
CREATE TABLE users (
  id         TEXT PRIMARY KEY,
  school_id  TEXT NOT NULL REFERENCES schools(id),
  role       TEXT NOT NULL CHECK (role IN ('installer','school_admin','teacher')),
  full_name  TEXT NOT NULL,
  email      TEXT,
  pin_hash   TEXT NOT NULL,                    -- scrypt/argon2 of the PIN, never the PIN
  active     INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE TABLE teacher_trainings (
  id              TEXT PRIMARY KEY,
  user_id         TEXT NOT NULL REFERENCES users(id),
  course          TEXT NOT NULL,
  level           TEXT,                        -- L2..L4
  passed_on       TEXT,
  can_teach_alone INTEGER NOT NULL DEFAULT 0
);

-- ───────────── 1. Students ─────────────
CREATE TABLE classrooms (
  id            TEXT PRIMARY KEY,
  school_id     TEXT NOT NULL REFERENCES schools(id),
  academic_year INTEGER NOT NULL,              -- Buddhist year, e.g. 2569
  term          INTEGER NOT NULL CHECK (term IN (1,2,3)),
  grade         TEXT NOT NULL,                 -- 'ป.4' .. 'ม.6'
  room          TEXT,                          -- '1', '2', ...
  level         TEXT NOT NULL,                 -- 'L2' | 'L3' | 'L4'
  teacher_id    TEXT REFERENCES users(id)
);

-- The student types their ID (up to 13 digits, owner 2026-11-25) at /join. 13 digits is also the length of
-- a Thai national ID, and some schools use it as the student ID, so the raw ID is never stored:
-- code_hash = HMAC-SHA256(per-PC secret, digits only); the hub hashes what the student types and compares.
-- The teacher sees code_last4, number and nickname. Roster is imported by the teacher (CSV).
CREATE TABLE students (
  id             TEXT PRIMARY KEY,
  school_id      TEXT NOT NULL REFERENCES schools(id),
  code_hash      TEXT NOT NULL,
  code_last4     TEXT NOT NULL CHECK (length(code_last4) <= 4),
  grade          TEXT,
  room           TEXT,
  number         INTEGER,                      -- เลขที่
  nickname       TEXT,                         -- optional
  consent        TEXT NOT NULL DEFAULT 'unknown' CHECK (consent IN ('unknown','given','refused')),
  archived_at    TEXT,
  UNIQUE (school_id, code_hash)
);

-- Real names only if the school wants them; one table, so it can be emptied at once.
CREATE TABLE student_names (
  student_id TEXT PRIMARY KEY REFERENCES students(id) ON DELETE CASCADE,
  full_name  TEXT NOT NULL
);

CREATE TABLE enrollments (
  student_id   TEXT NOT NULL REFERENCES students(id),
  classroom_id TEXT NOT NULL REFERENCES classrooms(id),
  PRIMARY KEY (student_id, classroom_id)
);

-- ───────────── 3. Usage: simulation / real robot ─────────────
CREATE TABLE lessons (
  id         TEXT PRIMARY KEY,
  level      TEXT NOT NULL,                    -- L1..L6
  seq        INTEGER,
  title      TEXT NOT NULL,
  minutes    INTEGER,
  objectives TEXT
);

CREATE TABLE class_sessions (
  id           TEXT PRIMARY KEY,
  classroom_id TEXT REFERENCES classrooms(id), -- NULL = a demo / open session not tied to a class
  teacher_id   TEXT REFERENCES users(id),
  lesson_id    TEXT REFERENCES lessons(id),
  class_code   TEXT,                           -- the 4–6 digit code of that lesson
  started_at   TEXT NOT NULL,                  -- hub 'open'
  ended_at     TEXT                            -- hub 'close' (or last activity if the PC was switched off)
);

CREATE TABLE attendance (
  session_id TEXT NOT NULL REFERENCES class_sessions(id),
  student_id TEXT NOT NULL REFERENCES students(id),
  joined_at  TEXT NOT NULL,
  left_at    TEXT,
  PRIMARY KEY (session_id, student_id)
);

CREATE TABLE groups (
  id         TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES class_sessions(id),
  name       TEXT NOT NULL
);

CREATE TABLE group_members (
  group_id   TEXT NOT NULL REFERENCES groups(id),
  student_id TEXT NOT NULL REFERENCES students(id),
  PRIMARY KEY (group_id, student_id)
);

-- One row per stretch of a group controlling a robot (real) or a simulated robot (sim).
CREATE TABLE robot_runs (
  id            TEXT PRIMARY KEY,
  session_id    TEXT NOT NULL REFERENCES class_sessions(id),
  group_id      TEXT REFERENCES groups(id),
  mode          TEXT NOT NULL CHECK (mode IN ('sim','real')),
  robot_id      TEXT REFERENCES robots(id),    -- NULL for sim
  role          TEXT,                          -- watch | blocks | goal | drive | teacher
  venue         TEXT,                          -- cafe | hotel | hospital | office | ...
  started_at    TEXT NOT NULL,
  ended_at      TEXT,
  distance_m    REAL,
  goals_sent    INTEGER DEFAULT 0,
  goals_reached INTEGER DEFAULT 0,
  teacher_stops INTEGER DEFAULT 0,
  errors        INTEGER DEFAULT 0
);

-- Raw append-only log; the tables above are what reports read.
CREATE TABLE events (
  id         TEXT PRIMARY KEY,
  session_id TEXT REFERENCES class_sessions(id),
  ts         TEXT NOT NULL,
  actor      TEXT,                             -- 'teacher' | group id | 'hub'
  type       TEXT NOT NULL,                    -- join, assign, stop_all, goal_reached, ...
  payload    TEXT                              -- JSON
);
CREATE INDEX events_session_ts ON events(session_id, ts);

-- ───────────── 5. Worksheets ─────────────
CREATE TABLE worksheets (
  id        TEXT PRIMARY KEY,
  lesson_id TEXT REFERENCES lessons(id),
  version   INTEGER NOT NULL DEFAULT 1,
  kind      TEXT NOT NULL CHECK (kind IN ('practice','pre_test','post_test','project','rubric')),
  pair_key  TEXT,                              -- a pre_test and its post_test share this
  title     TEXT NOT NULL,
  max_score REAL
);

CREATE TABLE worksheet_items (
  id           TEXT PRIMARY KEY,
  worksheet_id TEXT NOT NULL REFERENCES worksheets(id),
  seq          INTEGER NOT NULL,
  type         TEXT NOT NULL CHECK (type IN ('mcq','short','blocks_task','robot_mission')),
  content      TEXT NOT NULL,                  -- JSON: question, choices, mission spec
  answer_key   TEXT,                           -- JSON
  points       REAL NOT NULL DEFAULT 1,
  skill_tags   TEXT                            -- JSON array, e.g. ["analytical","digital_literacy"]
);

CREATE TABLE assignments (
  id           TEXT PRIMARY KEY,
  worksheet_id TEXT NOT NULL REFERENCES worksheets(id),
  classroom_id TEXT NOT NULL REFERENCES classrooms(id),
  session_id   TEXT REFERENCES class_sessions(id),
  open_at      TEXT,
  due_at       TEXT
);

CREATE TABLE submissions (
  id             TEXT PRIMARY KEY,
  assignment_id  TEXT NOT NULL REFERENCES assignments(id),
  student_id     TEXT REFERENCES students(id),
  group_id       TEXT REFERENCES groups(id),   -- group work: every member gets the score
  answers        TEXT,                         -- JSON
  blocks_program TEXT,                         -- JSON / XML of the block program
  robot_run_id   TEXT REFERENCES robot_runs(id),
  score          REAL,
  submitted_at   TEXT NOT NULL,
  CHECK (student_id IS NOT NULL OR group_id IS NOT NULL)
);

CREATE TABLE item_scores (
  submission_id TEXT NOT NULL REFERENCES submissions(id),
  item_id       TEXT NOT NULL REFERENCES worksheet_items(id),
  score         REAL NOT NULL,
  PRIMARY KEY (submission_id, item_id)
);

-- Steps inside a lesson (e.g. "drive to the table", "use the lidar to stop"), for progress and attempts.
CREATE TABLE lesson_steps (
  id        TEXT PRIMARY KEY,
  lesson_id TEXT NOT NULL REFERENCES lessons(id),
  seq       INTEGER NOT NULL,
  title     TEXT NOT NULL,
  skill     TEXT                               -- criterion code below, e.g. 'coding'
);

-- Every try at a step: written by the hub from the student's page (block run, mission run).
CREATE TABLE step_attempts (
  id            TEXT PRIMARY KEY,
  step_id       TEXT NOT NULL REFERENCES lesson_steps(id),
  session_id    TEXT NOT NULL REFERENCES class_sessions(id),
  student_id    TEXT REFERENCES students(id),
  group_id      TEXT REFERENCES groups(id),
  robot_run_id  TEXT REFERENCES robot_runs(id),
  started_at    TEXT NOT NULL,
  ended_at      TEXT,
  success       INTEGER NOT NULL DEFAULT 0,
  teacher_help  INTEGER NOT NULL DEFAULT 0,   -- teacher marks "ครูช่วย"
  CHECK (student_id IS NOT NULL OR group_id IS NOT NULL)
);

-- Rubric: criteria and weights are data (default: knowledge 20, practical 30, problem 25, creativity 15, team 10).
CREATE TABLE rubric_criteria (
  code        TEXT PRIMARY KEY,                -- knowledge | coding | robot_operation | problem_solving | creativity | teamwork
  name        TEXT NOT NULL,
  weight      REAL NOT NULL DEFAULT 0,         -- percent of the overall score
  level1 TEXT, level2 TEXT, level3 TEXT, level4 TEXT   -- what each level means, shown to the teacher
);

INSERT INTO rubric_criteria (code, name, weight, level1, level2, level3, level4) VALUES
 ('knowledge',       'ความรู้',          20, 'ยังตอบไม่ได้',   'ตอบได้บางส่วน', 'ตอบได้ถูกต้อง',  'อธิบายให้คนอื่นได้'),
 ('coding',          'Coding',           15, 'ยังทำไม่ได้',    'ทำตามตัวอย่าง', 'ทำได้เอง',       'ประยุกต์ได้'),
 ('robot_operation', 'ใช้งานหุ่นยนต์',     15, 'ใช้ไม่ได้',       'ใช้ได้บางส่วน', 'ใช้ได้ถูกต้อง',  'ปรับแต่งได้'),
 ('problem_solving', 'การแก้ปัญหา',      25, 'ต้องช่วยตลอด',   'ช่วยบางส่วน',   'แก้เองได้',      'วิเคราะห์และปรับปรุงได้'),
 ('creativity',      'ความคิดสร้างสรรค์', 15, 'ทำตามโจทย์',     'ปรับเล็กน้อย',   'มีแนวคิดใหม่',    'สร้างนวัตกรรม'),
 ('teamwork',        'การทำงานเป็นทีม',   10, 'ไม่ร่วมงาน',      'ร่วมบางครั้ง',    'ร่วมมือดี',       'ช่วยนำทีม');

-- One rating of one student on one criterion, by the teacher or by the student (self).
CREATE TABLE skill_ratings (
  id            TEXT PRIMARY KEY,
  student_id    TEXT NOT NULL REFERENCES students(id),
  session_id    TEXT REFERENCES class_sessions(id),
  lesson_id     TEXT REFERENCES lessons(id),
  submission_id TEXT REFERENCES submissions(id),
  criterion     TEXT NOT NULL REFERENCES rubric_criteria(code),
  level         INTEGER NOT NULL CHECK (level BETWEEN 1 AND 4),
  rater         TEXT NOT NULL CHECK (rater IN ('teacher','self','peer')),
  rated_by      TEXT REFERENCES users(id),     -- NULL when rater = self / peer
  rated_at      TEXT NOT NULL,
  note          TEXT
);

-- Portfolio: work, not children. Photos and videos show the robot / the work; files stay on this PC.
CREATE TABLE portfolio_items (
  id           TEXT PRIMARY KEY,
  student_id   TEXT REFERENCES students(id),
  group_id     TEXT REFERENCES groups(id),
  session_id   TEXT REFERENCES class_sessions(id),
  lesson_id    TEXT REFERENCES lessons(id),
  kind         TEXT NOT NULL CHECK (kind IN ('photo','video','code','project','reflection')),
  file_path    TEXT,                           -- under ./data/portfolio/, NULL for text
  text         TEXT,                           -- reflection ("สิ่งที่ได้เรียนรู้") or code
  created_at   TEXT NOT NULL,
  shareable    INTEGER NOT NULL DEFAULT 0,     -- teacher allows it in reports that leave the PC
  CHECK (student_id IS NOT NULL OR group_id IS NOT NULL)
);

CREATE TABLE badges (
  code  TEXT PRIMARY KEY,
  name  TEXT NOT NULL,
  rule  TEXT                                   -- e.g. 'lesson L2-04 passed', given by the hub or the teacher
);

CREATE TABLE student_badges (
  student_id TEXT NOT NULL REFERENCES students(id),
  badge_code TEXT NOT NULL REFERENCES badges(code),
  given_at   TEXT NOT NULL,
  PRIMARY KEY (student_id, badge_code)
);

-- ───────────── 2. Reports (CONNEXT ED framework) ─────────────
-- Indicators are data, not code: the real CONNEXT ED template is not known yet.
CREATE TABLE frameworks (
  id      TEXT PRIMARY KEY,
  code    TEXT NOT NULL,                       -- 'CONNEXT_ED'
  version TEXT NOT NULL
);

CREATE TABLE indicators (
  id           TEXT PRIMARY KEY,
  framework_id TEXT NOT NULL REFERENCES frameworks(id),
  code         TEXT NOT NULL,
  domain       TEXT NOT NULL CHECK (domain IN ('learner','teacher','school','reach')),
  name         TEXT NOT NULL,
  unit         TEXT,
  target       TEXT,
  source_view  TEXT                            -- which v_* view gives the number
);

CREATE TABLE skill_map (
  skill_tag      TEXT NOT NULL,
  indicator_id   TEXT NOT NULL REFERENCES indicators(id),
  PRIMARY KEY (skill_tag, indicator_id)
);

-- Frozen copy of a report once it is sent; later data does not change it.
CREATE TABLE report_snapshots (
  id                TEXT PRIMARY KEY,
  school_id         TEXT NOT NULL REFERENCES schools(id),
  academic_year     INTEGER NOT NULL,
  term              INTEGER,
  framework_id      TEXT REFERENCES frameworks(id),
  data              TEXT NOT NULL,             -- JSON of every indicator value
  generated_at      TEXT NOT NULL,
  approved_by       TEXT REFERENCES users(id)
);

-- Teaching session: has at least one robot run (sim or real) and lasts at least 20 minutes.
CREATE VIEW v_teaching_sessions AS
SELECT s.id, s.classroom_id, s.teacher_id, c.school_id, c.academic_year, c.term,
       (julianday(s.ended_at) - julianday(s.started_at)) * 24.0 AS hours,
       EXISTS (SELECT 1 FROM robot_runs r WHERE r.session_id = s.id AND r.mode = 'real') AS used_real,
       EXISTS (SELECT 1 FROM robot_runs r WHERE r.session_id = s.id AND r.mode = 'sim')  AS used_sim
FROM class_sessions s JOIN classrooms c ON c.id = s.classroom_id
WHERE s.ended_at IS NOT NULL
  AND (julianday(s.ended_at) - julianday(s.started_at)) * 1440 >= 20
  AND EXISTS (SELECT 1 FROM robot_runs r WHERE r.session_id = s.id);

CREATE VIEW v_teaching_hours_term AS
SELECT school_id, academic_year, term,
       ROUND(SUM(hours), 1)                                   AS hours_total,
       ROUND(SUM(CASE WHEN used_real THEN hours ELSE 0 END), 1) AS hours_with_real_robot,
       COUNT(*)                                               AS sessions
FROM v_teaching_sessions GROUP BY school_id, academic_year, term;

CREATE VIEW v_robot_minutes AS
SELECT c.school_id, c.academic_year, c.term, r.mode,
       ROUND(SUM((julianday(r.ended_at) - julianday(r.started_at)) * 1440), 0) AS minutes
FROM robot_runs r JOIN class_sessions s ON s.id = r.session_id JOIN classrooms c ON c.id = s.classroom_id
WHERE r.ended_at IS NOT NULL
GROUP BY c.school_id, c.academic_year, c.term, r.mode;

CREATE VIEW v_students_reached AS
SELECT c.school_id, c.academic_year,
       COUNT(DISTINCT a.student_id) AS students
FROM attendance a JOIN class_sessions s ON s.id = a.session_id JOIN classrooms c ON c.id = s.classroom_id
GROUP BY c.school_id, c.academic_year;

CREATE VIEW v_cost_per_head AS
SELECT l.school_id, sr.academic_year, l.price_thb, sr.students,
       ROUND(l.price_thb * 1.0 / NULLIF(sr.students, 0), 0) AS thb_per_student
FROM licenses l JOIN v_students_reached sr ON sr.school_id = l.school_id;

-- Percent score of each student on each test (group submissions credit every member).
CREATE VIEW v_test_scores AS
SELECT sub.id AS submission_id, COALESCE(sub.student_id, gm.student_id) AS student_id,
       w.kind, w.pair_key, a.classroom_id,
       100.0 * sub.score / NULLIF(w.max_score, 0) AS pct
FROM submissions sub
JOIN assignments a ON a.id = sub.assignment_id
JOIN worksheets w  ON w.id = a.worksheet_id
LEFT JOIN group_members gm ON gm.group_id = sub.group_id AND sub.student_id IS NULL
WHERE w.kind IN ('pre_test','post_test') AND sub.score IS NOT NULL;

CREATE VIEW v_prepost_gain AS
SELECT c.school_id, c.academic_year, c.term, pre.pair_key,
       COUNT(*)                         AS students_with_both,
       ROUND(AVG(pre.pct), 1)           AS pre_avg,
       ROUND(AVG(post.pct), 1)          AS post_avg,
       ROUND(AVG(post.pct - pre.pct), 1) AS gain_points
FROM v_test_scores pre
JOIN v_test_scores post ON post.student_id = pre.student_id AND post.pair_key = pre.pair_key
                       AND post.kind = 'post_test'
JOIN classrooms c ON c.id = pre.classroom_id
WHERE pre.kind = 'pre_test'
GROUP BY c.school_id, c.academic_year, c.term, pre.pair_key;

-- Per student, per step: tries, success, time, help. Group attempts count for every member.
CREATE VIEW v_step_progress AS
SELECT COALESCE(a.student_id, gm.student_id) AS student_id, st.lesson_id, a.step_id,
       COUNT(*)                                         AS attempts,
       MAX(a.success)                                   AS passed,
       MAX(CASE WHEN a.success AND NOT a.teacher_help THEN 1 ELSE 0 END) AS passed_alone,
       ROUND(SUM((julianday(a.ended_at) - julianday(a.started_at)) * 1440), 1) AS minutes
FROM step_attempts a
JOIN lesson_steps st ON st.id = a.step_id
LEFT JOIN group_members gm ON gm.group_id = a.group_id AND a.student_id IS NULL
GROUP BY 1, 2, 3;

-- Per student, per lesson: share of steps passed (the "65 %" on the student page).
CREATE VIEW v_lesson_progress AS
SELECT p.student_id, p.lesson_id,
       ROUND(100.0 * SUM(p.passed) / (SELECT COUNT(*) FROM lesson_steps s WHERE s.lesson_id = p.lesson_id), 0) AS pct,
       SUM(p.attempts) AS attempts
FROM v_step_progress p GROUP BY p.student_id, p.lesson_id;

-- Latest teacher rating per student and criterion, as 0–100, plus the weighted overall score.
CREATE VIEW v_skill_profile AS
SELECT r.student_id, r.criterion, c.weight, r.level, r.level * 25.0 AS score100   -- 4 = 100
FROM skill_ratings r JOIN rubric_criteria c ON c.code = r.criterion
WHERE r.rater = 'teacher'
  AND r.rated_at = (SELECT MAX(r2.rated_at) FROM skill_ratings r2
                    WHERE r2.student_id = r.student_id AND r2.criterion = r.criterion AND r2.rater = 'teacher');

CREATE VIEW v_overall_score AS
SELECT student_id,
       ROUND(SUM(score100 * weight) / NULLIF(SUM(weight), 0), 2) AS overall,
       COUNT(*) AS criteria_rated
FROM v_skill_profile GROUP BY student_id;

CREATE VIEW v_teachers_trained AS
SELECT u.school_id,
       COUNT(DISTINCT t.user_id)                                           AS trained,
       COUNT(DISTINCT CASE WHEN t.can_teach_alone THEN t.user_id END)      AS teach_alone
FROM teacher_trainings t JOIN users u ON u.id = t.user_id
WHERE t.passed_on IS NOT NULL
GROUP BY u.school_id;
