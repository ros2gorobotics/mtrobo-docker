# MTRobo Docker (ROS 2 Jazzy)

ชุดรันหุ่น MTRobo บน Windows ด้วย Docker Desktop ภายใน container มี:

- **bringup**: micro-ROS agent, EKF, rf2o laser odometry, UDP lidar receiver
- **rosbridge**: ให้หน้าเว็บ MTRobo เชื่อมต่อ
- **mtrobo_manager**: เปิด/ปิด SLAM และ Nav2 ตามคำสั่งจากหน้าเว็บ

Image: `ghcr.io/ros2gorobotics/mtrobo-docker:jazzy`

## สิ่งที่ต้องมี

- Windows 11 + WSL2 (Ubuntu 24.04) + Docker Desktop (เปิด WSL2 backend)
- CPU แบบ x86-64 (Intel/AMD)
- พื้นที่ว่างประมาณ 6 GB

## ติดตั้ง

```powershell
git clone https://github.com/ros2gorobotics/mtrobo-docker.git
cd mtrobo-docker
mkdir maps
docker compose pull
```

> ไม่มี git ก็ได้: ดาวน์โหลด ZIP จากปุ่ม **Code → Download ZIP** แล้วแตกไฟล์

เปิด firewall ให้หุ่นส่งข้อมูลเข้ามาได้ (PowerShell แบบ **Administrator**, ทำครั้งเดียว):

```powershell
New-NetFirewallRule -DisplayName "MTRobo micro-ROS" -Direction Inbound -Protocol UDP -LocalPort 8888 -Action Allow
New-NetFirewallRule -DisplayName "MTRobo UDP lidar" -Direction Inbound -Protocol UDP -LocalPort 9999 -Action Allow
New-NetFirewallRule -DisplayName "MTRobo rosbridge" -Direction Inbound -Protocol TCP -LocalPort 9090 -Action Allow
```

## ใช้งาน

```powershell
docker compose up -d          # เริ่ม (และเริ่มเองทุกครั้งที่เปิด Docker Desktop)
docker compose logs -f        # ดู log
docker compose down           # หยุด
```

- ตั้ง ESP32 (micro-ROS และ lidar) ให้ส่งไปที่ **IP ของเครื่อง Windows** (ดูด้วย `ipconfig`) port **8888** และ **9999**
- หน้าเว็บ MTRobo เชื่อมต่อที่ `ws://<IP เครื่อง Windows>:9090`
- แผนที่ที่บันทึกจากหน้าเว็บอยู่ในโฟลเดอร์ `maps`

ตั้งค่าหุ่น: คัดลอก `.env.example` เป็น `.env` แล้วแก้ค่า (ไม่มีไฟล์นี้ก็ใช้ค่าเริ่มต้น)

## เข้า shell / เปิด RViz

```powershell
docker exec -it mtrobo bash
```

```bash
ros2 topic list
ros2 topic hz /scan
rviz2 -d /opt/mtrobo/share/mtrobo_navigation/rviz/mtrobo_navigation.rviz
```

## อัปเดตเป็นเวอร์ชันล่าสุด

```powershell
docker compose pull
docker compose up -d
```

## แก้ปัญหา

| อาการ | ตรวจสอบ |
|---|---|
| หุ่นไม่เชื่อมต่อ micro-ROS | IP ใน ESP32 ตรงกับ `ipconfig`, firewall port 8888, อยู่วง Wi-Fi เดียวกัน |
| ไม่มี `/scan` | firewall port 9999, `docker compose logs` ต้องเห็น `Listening for Lidar UDP on port 9999` |
| หน้าเว็บต่อไม่ได้ | firewall port 9090, ใช้ IP เครื่อง Windows ไม่ใช่ `localhost` เมื่อเปิดจากเครื่องอื่น |
