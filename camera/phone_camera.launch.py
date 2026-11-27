"""Phone camera on the robot: phone_camera_node + static TF base_link -> camera_link.

    ros2 launch /camera/phone_camera.launch.py phone_camera_url:=http://192.168.1.50:8080/shot.jpg

docker-compose.yml runs this in the `camera` service; every argument defaults to an environment variable from .env:
  phone_camera_url  PHONE_CAMERA_URL   ""          empty: the node waits for a url set from the web (camera window)
  phone_camera_mode PHONE_CAMERA_MODE  snapshot    snapshot (/shot.jpg) or mjpeg (/video)
  phone_camera_rate PHONE_CAMERA_RATE  5.0         Hz
  cam_x             CAM_X              0.10        m in front of base_link
  cam_z             CAM_Z              0.35        m above base_link (base_link is 0.075 m above the floor)
  cam_pitch         CAM_PITCH          10          degrees, positive = tilted down

The node and the TF always start. A url / mode set from the web is saved to maps/phone_camera.json and wins over
PHONE_CAMERA_URL at the next start.
"""
import math
import os

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

HERE = os.path.dirname(os.path.abspath(__file__))


def setup(context):
    get = lambda k: LaunchConfiguration(k).perform(context).strip()  # noqa: E731
    url = get("phone_camera_url")
    pitch = math.radians(float(get("cam_pitch") or 0))
    return [
        Node(package="tf2_ros", executable="static_transform_publisher", name="camera_link_tf", output="screen",
             arguments=["--x", get("cam_x"), "--y", "0", "--z", get("cam_z"), "--roll", "0", "--pitch", str(pitch), "--yaw", "0",
                        "--frame-id", "base_link", "--child-frame-id", "camera_link"]),
        Node(executable="python3", arguments=[os.path.join(HERE, "phone_camera_node.py")], name="phone_camera_node", output="screen",
             parameters=[{"url": url, "mode": get("phone_camera_mode") or "snapshot",
                          "rate": float(get("phone_camera_rate") or 5.0)}], respawn=True, respawn_delay=2.0),
    ]


def generate_launch_description():
    env = os.environ.get
    return LaunchDescription([
        DeclareLaunchArgument("phone_camera_url", default_value=env("PHONE_CAMERA_URL", "")),
        DeclareLaunchArgument("phone_camera_mode", default_value=env("PHONE_CAMERA_MODE", "snapshot")),
        DeclareLaunchArgument("phone_camera_rate", default_value=env("PHONE_CAMERA_RATE", "5.0")),
        DeclareLaunchArgument("cam_x", default_value=env("CAM_X", "0.10")),
        DeclareLaunchArgument("cam_z", default_value=env("CAM_Z", "0.35")),
        DeclareLaunchArgument("cam_pitch", default_value=env("CAM_PITCH", "10")),
        OpaqueFunction(function=setup),
    ])
