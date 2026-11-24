#!/usr/bin/env python3
"""Simulated MTRobo cat robot (2wd) for the classroom: no Gazebo, no robot needed.

It stands in for the ESP32 + lidar side of a real robot and publishes what they publish:
  /odom (nav_msgs/Odometry) + TF odom -> base_footprint   30 Hz
  /scan (sensor_msgs/LaserScan, frame laser_frame)        10 Hz   360 beams, raycast in a built-in room
  /imu/data (sensor_msgs/Imu)                              30 Hz
  /battery (sensor_msgs/BatteryState)                       1 Hz   drains slowly
  /robot_status (std_msgs/Float32MultiArray)                1 Hz   [0] = Wi-Fi RSSI, layout as firmware_oled
and listens to /cmd_vel (geometry_msgs/Twist). Like the firmware it stops when no cmd_vel arrives for 500 ms.

The laser frame comes from the real robot's URDF (robot_state_publisher): x 0.12 m, z 0.33 m, mounted upside down
(roll pi), so laser angle a points along -a in the robot frame.

Environment: SIM_POSE="x,y,yaw" start pose in the room (m, m, rad); odom starts at 0, 0, 0 there.
"""
import math
import os
import random
import time

import numpy as np
import rclpy
from geometry_msgs.msg import TransformStamped, Twist
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import BatteryState, Imu, LaserScan
from std_msgs.msg import Float32MultiArray
from tf2_ros import TransformBroadcaster

ROBOT_R = 0.17            # collision radius (m)
LASER_X = 0.12            # laser in front of the centre (URDF laser_frame_to_base_link)
MAX_V, MAX_W = 0.5, 2.0   # firmware limits
ACC_V, ACC_W = 0.8, 4.0   # m/s^2, rad/s^2
CMD_TIMEOUT = 0.5         # s, as CMD_TIMEOUT_MS in the firmware
BEAMS, RANGE_MIN, RANGE_MAX = 360, 0.12, 10.0


def box(x, y, w, h):
    return [((x, y), (x + w, y)), ((x + w, y), (x + w, y + h)), ((x + w, y + h), (x, y + h)), ((x, y + h), (x, y))]


def pillar(cx, cy, r, n=10):
    p = [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]
    return [(p[i], p[(i + 1) % n]) for i in range(n)]


def classroom():
    """an 8 x 6 m room: a part wall with a doorway, three tables, two pillars, a counter"""
    segs = box(0, 0, 8, 6)
    segs += [((5.0, 0.0), (5.0, 2.2)), ((5.0, 3.2), (5.0, 6.0))]   # part wall, 1 m doorway at y 2.2..3.2
    segs += box(2.0, 1.6, 0.8, 0.8) + box(2.0, 3.6, 0.8, 0.8) + box(6.4, 4.2, 0.8, 0.8)
    segs += pillar(3.6, 3.0, 0.15) + pillar(6.5, 1.4, 0.2)
    segs += box(6.2, 0.0, 1.8, 0.5)                                  # counter along the wall
    a = np.array([s[0] for s in segs], dtype=float)
    b = np.array([s[1] for s in segs], dtype=float)
    return a, b


class SimRobot(Node):
    def __init__(self):
        super().__init__('sim_robot')
        pose = [float(v) for v in os.environ.get('SIM_POSE', '1.0,1.0,0.0').split(',')]
        self.x, self.y, self.th = pose[0], pose[1], pose[2]
        self.x0, self.y0, self.th0 = self.x, self.y, self.th   # odom starts at 0, 0, 0 here, like a real robot
        self.v = self.w = 0.0
        self.cmd_v = self.cmd_w = 0.0
        self.cmd_t = 0.0
        self.a, self.b = classroom()
        self.batt = 0.95
        self.t0 = time.monotonic()
        self.last = time.monotonic()
        self.bumped = False

        self.create_subscription(Twist, '/cmd_vel', self.on_cmd, 10)
        self.odom_pub = self.create_publisher(Odometry, '/odom', 10)
        self.scan_pub = self.create_publisher(LaserScan, '/scan', qos_profile_sensor_data)
        self.imu_pub = self.create_publisher(Imu, '/imu/data', qos_profile_sensor_data)
        self.batt_pub = self.create_publisher(BatteryState, '/battery', qos_profile_sensor_data)
        self.stat_pub = self.create_publisher(Float32MultiArray, '/robot_status', qos_profile_sensor_data)
        self.tf = TransformBroadcaster(self)
        self.create_timer(1 / 30, self.step)
        self.create_timer(0.1, self.scan)
        self.create_timer(1.0, self.slow)
        self.get_logger().info(f'sim robot at x={self.x:.2f} y={self.y:.2f} yaw={self.th:.2f}')

    def on_cmd(self, m: Twist):
        self.cmd_v = max(-MAX_V, min(MAX_V, m.linear.x))
        self.cmd_w = max(-MAX_W, min(MAX_W, m.angular.z))
        self.cmd_t = time.monotonic()

    def clearance(self, x, y) -> float:
        """distance from (x, y) to the nearest wall"""
        p = np.array([x, y])
        d = self.b - self.a
        t = np.clip(np.einsum('ij,ij->i', p - self.a, d) / np.maximum(np.einsum('ij,ij->i', d, d), 1e-12), 0, 1)
        q = self.a + d * t[:, None]
        return float(np.min(np.hypot(*(q - p).T)))

    def step(self):
        now = time.monotonic()
        dt, self.last = min(now - self.last, 0.1), now
        if now - self.cmd_t > CMD_TIMEOUT:
            self.cmd_v = self.cmd_w = 0.0
        self.v += max(-ACC_V * dt, min(ACC_V * dt, self.cmd_v - self.v))
        self.w += max(-ACC_W * dt, min(ACC_W * dt, self.cmd_w - self.w))
        th = self.th + self.w * dt
        nx, ny = self.x + self.v * math.cos(th) * dt, self.y + self.v * math.sin(th) * dt
        if self.clearance(nx, ny) < ROBOT_R and self.clearance(nx, ny) < self.clearance(self.x, self.y):
            if not self.bumped:
                self.get_logger().warn('bumped into a wall: stopped')
            self.bumped, self.v = True, 0.0          # blocked: turn on the spot only, like a bumper stop
        else:
            self.bumped = False
            self.x, self.y = nx, ny
        self.th = math.atan2(math.sin(th), math.cos(th))
        self.publish_odom()

    def publish_odom(self):
        st = self.get_clock().now().to_msg()
        c, s = math.cos(self.th0), math.sin(self.th0)
        dx, dy = self.x - self.x0, self.y - self.y0
        ox, oy, oth = c * dx + s * dy, -s * dx + c * dy, self.th - self.th0
        qz, qw = math.sin(oth / 2), math.cos(oth / 2)
        t = TransformStamped()
        t.header.stamp, t.header.frame_id, t.child_frame_id = st, 'odom', 'base_footprint'
        t.transform.translation.x, t.transform.translation.y = ox, oy
        t.transform.rotation.z, t.transform.rotation.w = qz, qw
        self.tf.sendTransform(t)
        o = Odometry()
        o.header.stamp, o.header.frame_id, o.child_frame_id = st, 'odom', 'base_footprint'
        o.pose.pose.position.x, o.pose.pose.position.y = ox, oy
        o.pose.pose.orientation.z, o.pose.pose.orientation.w = qz, qw
        o.twist.twist.linear.x, o.twist.twist.angular.z = self.v, self.w
        o.pose.covariance[0] = o.pose.covariance[7] = 0.001
        o.pose.covariance[35] = 0.001
        self.odom_pub.publish(o)
        i = Imu()
        i.header.stamp, i.header.frame_id = st, 'imu_link'
        i.orientation.z, i.orientation.w = qz, qw
        i.angular_velocity.z = self.w
        self.imu_pub.publish(i)

    def scan(self):
        ox, oy = self.x + LASER_X * math.cos(self.th), self.y + LASER_X * math.sin(self.th)
        inc = 2 * math.pi / BEAMS
        la = -math.pi + inc * np.arange(BEAMS)              # laser angles
        wa = self.th - la                                    # upside-down laser: world angle = yaw - a
        dx, dy = np.cos(wa)[:, None], np.sin(wa)[:, None]
        ex, ey = (self.b - self.a)[:, 0][None, :], (self.b - self.a)[:, 1][None, :]
        fx, fy = (self.a[:, 0] - ox)[None, :], (self.a[:, 1] - oy)[None, :]
        den = dx * ey - dy * ex
        with np.errstate(divide='ignore', invalid='ignore'):
            t = (fx * ey - fy * ex) / den                    # along the beam
            u = (fx * dy - fy * dx) / den                    # along the segment
        t = np.where((np.abs(den) > 1e-12) & (t > 0) & (u >= 0) & (u <= 1), t, np.inf)
        r = t.min(axis=1)
        r = r + np.random.normal(0, 0.008, BEAMS)
        r = np.where((r < RANGE_MIN) | (r > RANGE_MAX), np.inf, r)
        m = LaserScan()
        m.header.stamp, m.header.frame_id = self.get_clock().now().to_msg(), 'laser_frame'
        m.angle_min, m.angle_max, m.angle_increment = -math.pi, -math.pi + inc * (BEAMS - 1), inc
        m.scan_time, m.time_increment = 0.1, 0.1 / BEAMS
        m.range_min, m.range_max = RANGE_MIN, RANGE_MAX
        m.ranges = [float(v) for v in r]
        self.scan_pub.publish(m)

    def slow(self):
        moving = abs(self.v) > 0.01 or abs(self.w) > 0.01
        self.batt = max(0.05, self.batt - (0.0004 if moving else 0.0001))
        b = BatteryState()
        b.header.stamp = self.get_clock().now().to_msg()
        b.voltage = 6.6 + 1.8 * self.batt
        b.percentage = self.batt
        b.present = True
        self.batt_pub.publish(b)
        # firmware_oled layout: rssi, heap, min heap, uptime, control Hz, lidar Hz, sessions, ping fails, reset reason
        s = Float32MultiArray()
        s.data = [float(-50 + random.randint(-4, 4)), 180.0, 150.0, float(int(time.monotonic() - self.t0)),
                  50.0, 10.0, 1.0, 0.0, 1.0]
        self.stat_pub.publish(s)


def main():
    rclpy.init()
    n = SimRobot()
    try:
        rclpy.spin(n)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
