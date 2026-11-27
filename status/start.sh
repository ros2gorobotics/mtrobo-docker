#!/bin/bash
# container mtrobo: status endpoint (port 9092, restarted if it stops) + the normal bringup (the image's default command)
( while true; do python3 /opt/mtrobo-status/status_server.py; sleep 2; done ) &
exec ros2 launch mtrobo_bringup bringup.launch.py "$@"
