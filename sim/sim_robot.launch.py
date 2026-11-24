"""Simulated MTRobo cat robot: the real robot's URDF + sim_robot.py (instead of ESP32, lidar and EKF) + rosbridge +
mtrobo_manager (so the web can start SLAM / Nav2 / save maps exactly as with a real robot).

    ros2 launch /sim/sim_robot.launch.py        (docker-compose.sim.yml does this, one container per robot)
"""
import os

from launch import LaunchDescription
from launch.actions import ExecuteProcess, IncludeLaunchDescription
from launch.launch_description_sources import AnyLaunchDescriptionSource, PythonLaunchDescriptionSource
from launch.substitutions import PathJoinSubstitution
from launch_ros.substitutions import FindPackageShare

HERE = os.path.dirname(os.path.abspath(__file__))


def generate_launch_description():
    share = lambda pkg, *p: PathJoinSubstitution([FindPackageShare(pkg), *p])  # noqa: E731
    return LaunchDescription([
        # the real robot's URDF (MTROBO_BASE=2wd): base_footprint -> base_link -> laser_frame, wheels, imu_link
        IncludeLaunchDescription(PythonLaunchDescriptionSource(share('mtrobo_description', 'launch', 'description.launch.py'))),
        ExecuteProcess(cmd=['python3', os.path.join(HERE, 'sim_robot.py')], output='screen'),
        IncludeLaunchDescription(AnyLaunchDescriptionSource(share('rosbridge_server', 'launch', 'rosbridge_websocket_launch.xml')),
                                 launch_arguments={'port': '9090'}.items()),
        ExecuteProcess(cmd=['python3', share('mtrobo_bringup', 'scripts', 'mtrobo_manager.py'),
                            '--ros-args', '-p', 'maps_dir:=' + os.path.expanduser('~/maps')], output='screen'),
    ])
