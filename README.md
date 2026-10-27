# MTRobo Docker (ROS 2 Jazzy)

คู่มือติดตั้งและใช้งานหุ่น MTRobo บน **Windows** ด้วย **Docker Desktop**

ภายใน container มีทุกอย่างที่ต้องใช้ ไม่ต้องติดตั้ง ROS เอง:

- **bringup**: micro-ROS agent, EKF, rf2o laser odometry, UDP lidar receiver
- **rosbridge**: ให้หน้าเว็บ MTRobo เชื่อมต่อ
- **mtrobo_manager**: เปิด/ปิด SLAM (สร้างแผนที่) และ Nav2 (นำทาง) ตามคำสั่งจากหน้าเว็บ

Image: `ghcr.io/ros2gorobotics/mtrobo-docker:jazzy`

---

## สิ่งที่ต้องมี

- Windows 10 (22H2) หรือ Windows 11
- CPU แบบ x86-64 (Intel/AMD) ใช้กับเครื่อง ARM ไม่ได้
- RAM 8 GB ขึ้นไป
- พื้นที่ว่างประมาณ 10 GB
- เครื่องคอมพิวเตอร์กับหุ่นต้องอยู่ Wi-Fi วงเดียวกัน

---

## ขั้นที่ 1: ติดตั้ง WSL2 (ทำครั้งเดียว)

1. กด **Start** แล้วพิมพ์ `powershell`
2. **คลิกขวา** ที่ Windows PowerShell → **Run as administrator** → กด **Yes**
3. รันคำสั่ง:
   ```powershell
   wsl --install
   ```
4. **รีสตาร์ทเครื่อง**

ถ้าเครื่องเคยติดตั้ง WSL ไว้แล้ว ให้รัน `wsl --update` แทน

---

## ขั้นที่ 2: ติดตั้ง Docker Desktop (ทำครั้งเดียว)

1. ดาวน์โหลดจาก https://www.docker.com/products/docker-desktop/ แล้วติดตั้ง ใช้ค่าเริ่มต้นได้เลย
2. เปิด Docker Desktop แล้วไปที่ **Settings (รูปเฟือง) → General** ติ๊ก:
   - ✅ **Use the WSL 2 based engine**
   - ✅ **Start Docker Desktop when you sign in to your computer**
3. กด **Apply & restart**
4. รอจนมุมล่างซ้ายขึ้น **Engine running** (สีเขียว)

---

## ขั้นที่ 3: ดาวน์โหลดไฟล์ MTRobo

**แบบที่ 1: ใช้ Git** (ถ้ายังไม่มี ติดตั้งได้จาก https://git-scm.com/download/win)

เปิด PowerShell ปกติ (ไม่ต้องใช้ admin):
```powershell
cd D:\
git clone https://github.com/ros2gorobotics/mtrobo-docker.git
cd mtrobo-docker
mkdir maps
```

**แบบที่ 2: ไม่ใช้ Git**

1. เข้า https://github.com/ros2gorobotics/mtrobo-docker
2. กด **Code → Download ZIP**
3. แตกไฟล์ไว้ที่ `D:\mtrobo-docker`
4. สร้างโฟลเดอร์ชื่อ `maps` ข้างใน
5. เปิด PowerShell แล้วรัน `cd D:\mtrobo-docker`

> จะเก็บไว้ที่ไดรฟ์หรือโฟลเดอร์ไหนก็ได้ ตัวอย่างนี้ใช้ `D:\`

---

## ขั้นที่ 4: เปิด Firewall (ทำครั้งเดียว)

> ⚠️ **ต้องใช้ PowerShell แบบ Administrator** ไม่อย่างนั้นจะขึ้น `Access is denied`
>
> Start → พิมพ์ `powershell` → **คลิกขวา → Run as administrator** (แถบชื่อหน้าต่างต้องมีคำว่า *Administrator*)

```powershell
New-NetFirewallRule -DisplayName "MTRobo micro-ROS" -Direction Inbound -Protocol UDP -LocalPort 8888 -Action Allow
New-NetFirewallRule -DisplayName "MTRobo UDP lidar" -Direction Inbound -Protocol UDP -LocalPort 9999 -Action Allow
New-NetFirewallRule -DisplayName "MTRobo rosbridge" -Direction Inbound -Protocol TCP -LocalPort 9090 -Action Allow
New-NetFirewallRule -DisplayName "MTRobo web UI" -Direction Inbound -Protocol TCP -LocalPort 3000 -Action Allow
```

ตรวจสอบ ต้องเห็นกฎ MTRobo 4 รายการ และช่อง Enabled เป็น `True`:
```powershell
Get-NetFirewallRule -DisplayName "MTRobo*" | Format-Table DisplayName, Enabled, Profile
```

ถ้า Wi-Fi ถูกตั้งเป็น **Public network** ให้เปลี่ยนเป็น **Private**:
Settings → Network & internet → Wi-Fi → คลิกชื่อ Wi-Fi → **Network profile type → Private**

| Port | ใช้ทำอะไร |
|---|---|
| UDP 8888 | micro-ROS (หุ่น ↔ คอมพิวเตอร์) |
| UDP 9999 | ข้อมูล lidar จากหุ่น |
| TCP 9090 | rosbridge สำหรับหน้าเว็บ MTRobo |
| TCP 3000 | หน้าเว็บ MTRobo (เปิดจากมือถือ) |

---

## ขั้นที่ 5: ตั้ง IP ให้หุ่น (ESP32)

1. ดู IP ของเครื่อง Windows:
   ```powershell
   ipconfig
   ```
   ดูบรรทัด **IPv4 Address** ของ Wi-Fi เช่น `192.168.1.50`
2. ใน firmware ของ ESP32 ตั้ง `AGENT_IP` เป็น IP นี้ แล้ว upload ใหม่ หุ่นจะส่งทั้ง micro-ROS (port 8888) และ lidar (port 9999) ไปที่ IP นี้

> 💡 ควรตั้ง IP ของเครื่อง Windows ให้คงที่ (DHCP reservation ที่ router) ไม่อย่างนั้นถ้า IP เปลี่ยน ต้องแก้ firmware ใหม่

---

## ขั้นที่ 6: ตั้งค่าหุ่น (ไม่บังคับ)

คัดลอก `.env.example` เป็น `.env` แล้วแก้ค่าตามหุ่น ถ้าไม่มีไฟล์นี้จะใช้ค่าเริ่มต้น:

```
MTROBO_BASE=2wd
MTROBO_LASER_SENSOR=udp_lidar
ROS_DOMAIN_ID=0
```

---

## ขั้นที่ 7: เริ่มใช้งาน

ใน PowerShell ที่โฟลเดอร์ `mtrobo-docker`:
```powershell
docker compose up -d
```
ครั้งแรกจะดาวน์โหลด image ประมาณ 1–2 GB และ build หน้าเว็บจาก GitHub (ประมาณ 3–5 นาที ทำครั้งเดียว) เมื่อเสร็จจะขึ้น `Container mtrobo Started` และ `Container mtrobo_web Started`

เปิดหุ่น (หรือกดรีเซ็ต ESP32) แล้วดู log:
```powershell
docker compose logs -f
```
กด `Ctrl+C` เพื่อออกจาก log ได้ container ยังทำงานต่อ

**สิ่งที่ต้องเห็นใน log:**

| ข้อความ | ความหมาย |
|---|---|
| `running... \| port: 8888` | micro-ROS agent พร้อม |
| `Listening for Lidar UDP on port 9999...` | ตัวรับ lidar พร้อม |
| `session established \| address: 192.168.x.x` | ✅ หุ่นเชื่อมต่อแล้ว |
| `Packet format: ...` | ✅ ได้รับข้อมูล lidar แล้ว |

---

## ขั้นที่ 8: เชื่อมหน้าเว็บ MTRobo

หน้าเว็บรันอยู่ใน container `mtrobo_web` เปิดพร้อมกับ `docker compose up -d`

เปิดหน้าเว็บที่:

- `http://localhost:3000` บนเครื่อง Windows เครื่องเดียวกัน
- `http://<IP เครื่อง Windows>:3000` จากมือถือหรือเครื่องอื่นที่ต่อ Wi-Fi วงเดียวกัน (ห้ามใช้ `localhost` บนมือถือ) มือถือจะเข้าหน้า `/go` สำหรับเลือกจุดหมาย

หน้าเว็บต่อ rosbridge ที่ `ws://<IP เดียวกัน>:9090` ให้เอง จากนั้นสั่งสร้างแผนที่และนำทางจากหน้าเว็บได้เลย แผนที่ที่บันทึกจะอยู่ในโฟลเดอร์ **`maps`**

---

## ตรวจสอบข้อมูล / เปิด RViz

เข้าไปใน container:
```powershell
docker exec -it mtrobo bash
```

```bash
ros2 topic list            # ดู topic ทั้งหมด
ros2 topic hz /scan        # lidar ควรได้ประมาณ 10 Hz
ros2 topic hz /odom        # odometry
rviz2 -d /opt/mtrobo/share/mtrobo_navigation/rviz/mtrobo_navigation.rviz
exit                       # ออกจาก container
```

---

## คำสั่งที่ใช้ประจำ

| ต้องการ | คำสั่ง |
|---|---|
| เริ่ม | `docker compose up -d` |
| หยุด | `docker compose down` |
| ดู log | `docker compose logs -f` |
| ดู log เฉพาะ lidar | `docker compose logs --tail 200 \| Select-String "udp_lidar"` |
| เข้า shell | `docker exec -it mtrobo bash` |
| **อัปเดตเป็นเวอร์ชันล่าสุด** | `git pull` แล้วรัน `docker compose pull --ignore-buildable`, `docker compose build --no-cache web` ตามด้วย `docker compose up -d --force-recreate` |

หลังติดตั้งครั้งแรกเสร็จ MTRobo จะเริ่มทำงานเองทุกครั้งที่เปิดเครื่อง หากไม่ต้องการให้เริ่มเอง ให้สั่ง `docker compose down`

---

## แก้ปัญหา

| อาการ / ข้อความ | สาเหตุ | วิธีแก้ |
|---|---|---|
| `New-NetFirewallRule : Access is denied` | PowerShell ไม่ได้เปิดแบบ admin | เปิด PowerShell ด้วย **Run as administrator** แล้วรันใหม่ |
| `Bind for :::8888 failed: port is already allocated` | มี container อื่นใช้ port 8888 อยู่ เช่น `ros2_jazzy_rviz` ของคอร์ส ROS2 | ดูว่าตัวไหนใช้ด้วย `docker ps --format "table {{.Names}}\t{{.Ports}}"` แล้วหยุดด้วย `docker stop <ชื่อ>` และ `docker update --restart=no <ชื่อ>` จากนั้นรัน `docker compose up -d` ใหม่ |
| ไม่มี `session established` (หุ่นไม่เชื่อมต่อ) | IP หรือ firewall ไม่ถูกต้อง | เช็กว่า `AGENT_IP` ใน ESP32 ตรงกับ `ipconfig`, เปิด firewall port 8888 แล้ว, อยู่ Wi-Fi วงเดียวกัน แล้วกดรีเซ็ต ESP32 |
| หุ่นเชื่อมต่อได้ แต่ `ros2 topic echo /scan` ไม่มีข้อมูล | ยังไม่ได้เปิด firewall port **9999** | เปิด firewall UDP 9999 (ขั้นที่ 4) แล้วรีเซ็ต ESP32 |
| log ขึ้น `No LiDAR data from ESP32` | ข้อมูล lidar มาไม่ถึง | firewall 9999, หุ่นเปิดอยู่ไหม, แบตเตอรี่, Wi-Fi |
| log ขึ้น `Ignored UDP packet of ... bytes` | firmware lidar เป็นคนละเวอร์ชันกับตัวรับ | อัปเดต firmware ของ ESP32 |
| หน้าเว็บเชื่อมต่อไม่ได้ | firewall หรือ IP ไม่ถูกต้อง | เปิด firewall TCP 9090 ถ้าเปิดหน้าเว็บจากเครื่องอื่นให้ใช้ IP ของเครื่อง Windows ไม่ใช่ `localhost` |
| มือถือเปิด `:3000` ไม่ได้ | container หน้าเว็บไม่ทำงาน หรือ firewall | `docker compose ps` ต้องเห็น `mtrobo_web` เป็น `Up` และเปิด firewall TCP 3000 ถ้ารัน `npm run dev` อยู่ให้ปิดก่อน เพราะชนพอร์ต 3000 |
| `docker compose up` ขึ้น error ตอน build `web` | เครื่องต่อ GitHub ไม่ได้ | ตรวจอินเทอร์เน็ต แล้วรัน `docker compose up -d` ใหม่ |
| `docker: command not found` หรือ `cannot connect to the Docker daemon` | Docker Desktop ยังไม่เปิด | เปิด Docker Desktop แล้วรอจนขึ้น **Engine running** |
| RViz ไม่เปิดหน้าต่าง | WSLg ยังไม่พร้อม | รัน `wsl --update` ใน PowerShell แบบ admin แล้วรีสตาร์ทเครื่อง |

ถ้ายังแก้ไม่ได้ ให้แนบผลของ `docker compose logs --tail 200` มาเมื่อขอความช่วยเหลือ
