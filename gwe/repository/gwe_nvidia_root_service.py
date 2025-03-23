# gwe-nvidia-root-service
#
# Copyright (C) 2025 Roberto Leinardi <roberto@leinardi.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import sys
import logging
import threading
import time
import signal
import json
import socket
from typing import List, Dict, Optional, Tuple, Callable, Any
from ctypes import *

from injector import singleton, inject
import pynvml

from gwe.model.clocks import Clocks
from gwe.model.fan import Fan
from gwe.model.gpu_status import GpuStatus
from gwe.model.info import Info
from gwe.model.overclock import Overclock
from gwe.model.power import Power
from gwe.model.status import Status
from gwe.model.temp import Temp
from gwe.util.concurrency import synchronized_with_attr

_LOG = logging.getLogger(__name__)
nv_control_extension = False

# Socket configuration
SOCKET_PATH = "/run/gwe/gwe_nvidia_root_service.sock"


class NvidiaRepository:
    # TODO inject?
    @inject
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._gpu_count = 0
        self._gpu_setting_cache: List[Dict[str, str]] = []
        self._ctrl_display: Optional[str] = None

    def set_ctrl_display(self, ctrl_display: str) -> None:
        self._ctrl_display = ctrl_display

    @synchronized_with_attr("_lock")
    def has_nvml_shared_library(self) -> bool:
        try:
            pynvml.nvmlInit()
            pynvml.nvmlShutdown()
            return True
        except:
            _LOG.exception("Error while checking NVML Shared Library")
        return False

    @synchronized_with_attr("_lock")
    def has_min_driver_version(self) -> bool:
        try:
            pynvml.nvmlInit()
            driver = self._nvml_get_val(pynvml.nvmlSystemGetDriverVersion)
            pynvml.nvmlShutdown()
        except:
            _LOG.exception("Error while checking NVML Shared Library")
            return False
        vmajor = int(driver.split(".", 1)[0])
        return 'WAYLAND_DISPLAY' not in os.environ and vmajor >= 535 or vmajor >= 555

    def _nvml_get_val(self, func, *args):
        try:
            return func(*args)
        except Exception as e:
            _LOG.error(f"NVML error in {func.__name__}: {e}")
            return None

    def set_fan_speed(self, gpu_index: int, speed: int = 100, manual_control: bool = False) -> bool:
        # /**
        #  * Sets the speed of a specified fan.
        #  *
        #  * WARNING: This function changes the fan control policy to manual. It means that YOU have to monitor
        #  *          the temperature and adjust the fan speed accordingly.
        #  *          If you set the fan speed too low you can burn your GPU!
        #  *          Use nvmlDeviceSetDefaultFanSpeed_v2 to restore default control policy.
        #  *
        #  * For all cuda-capable discrete products with fans that are Maxwell or Newer.
        #  *
        #  * device                                The identifier of the target device
        #  * fan                                   The index of the fan, starting at zero
        #  * speed                                 The target speed of the fan [0-100] in % of max speed
        #  *
        #  * return
        #  *        NVML_SUCCESS                   if the fan speed has been set
        #  *        NVML_ERROR_UNINITIALIZED       if the library has not been successfully initialized
        #  *        NVML_ERROR_INVALID_ARGUMENT    if the device is not valid, or the speed is outside acceptable ranges,
        #  *                                              or if the fan index doesn't reference an actual fan.
        #  *        NVML_ERROR_NOT_SUPPORTED       if the device is older than Maxwell.
        #  *        NVML_ERROR_UNKNOWN             if there was an unexpected error.
        #  */

        pynvml.nvmlInit()
        handle = self._nvml_get_val(pynvml.nvmlDeviceGetHandleByIndex, gpu_index)
        fan_indexes = self._nvml_get_val(pynvml.nvmlDeviceGetNumFans, handle)
        if fan_indexes is not None and fan_indexes > 0:
            for fan_index in range(fan_indexes):
                try:
                    if manual_control:
                        ret = pynvml.nvmlDeviceSetFanSpeed_v2(handle, fan_index, speed)
                        _LOG.error(f"test set_fan_speed 3: {ret}")
                    else:
                        ret = pynvml.nvmlDeviceSetDefaultFanSpeed_v2(handle, fan_index)
                        _LOG.error(f"test set_fan_speed 4: {ret}")
                except pynvml.NVMLError as err:
                    _LOG.warning(f"Error setting speed for fan{fan_index} on gpu{gpu_index}: {err}")
                    return True
        pynvml.nvmlShutdown()


# secure_fan_service.py
import os
import socket
import struct
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO)
SOCKET_PATH = "/run/gpu-control/fan.sock"


class SecureFanServer:
    def __init__(self):
        self.socket_path = SOCKET_PATH
        self._cleanup_socket()

    def _cleanup_socket(self):
        if Path(self.socket_path).exists():
            os.unlink(self.socket_path)

    def _authenticate_client(self, conn):
        try:
            creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
            pid, uid, gid = struct.unpack('3i', creds)
            # TODO check for pid of gwe(however you can do that?)
            return True
        except Exception as e:
            logging.error(f"Authentication failed: {str(e)}")
            return False

    def _handle_request(self, conn):
        if not self._authenticate_client(conn):
            return

        try:
            data = conn.recv(1024)
            # Add NVML fan control logic here
            conn.sendall(b"ACK: Fan speed updated")
        except Exception as e:
            logging.error(f"Handler error: {str(e)}")

    def run(self):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.socket_path)
        os.chmod(self.socket_path, 0o600)  # Restrict socket permissions
        server.listen(1)

        logging.info(f"Secure fan service started on {self.socket_path}")
        while True:
            conn, _ = server.accept()
            self._handle_request(conn)
            conn.close()


def check_root_privileges():
    """
    Check if the current process is running with root privileges.

    Raises:
        PermissionError: If the process is not running as root.
    """
    if os.geteuid() != 0:
        raise PermissionError("This service must be run as root")


# Flag to control the service loop
running = True


def signal_handler(sig, frame):
    """Handle termination signals to gracefully shut down the service"""
    global running
    print("Received termination signal. Shutting down...")
    running = False


def setup_logging():
    """Configure logging for the service"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout)
        ]
    )


def handle_client_request(repo, data):
    """Process client requests and execute the appropriate functions"""
    try:
        command = data.get('command')
        params = data.get('params', {})

        if command == 'set_fan_speed':
            gpu_index = params.get('gpu_index', 0)
            speed = params.get('speed', 100)
            manual_control = params.get('manual_control', True)

            result = repo.set_fan_speed(gpu_index, speed, manual_control)
            return {'success': result}
        elif command == 'has_nvml_shared_library':
            result = repo.has_nvml_shared_library()
            return {'success': True, 'result': result}
        elif command == 'has_min_driver_version':
            result = repo.has_min_driver_version()
            return {'success': True, 'result': result}
        else:
            return {'success': False, 'error': f'Unknown command: {command}'}
    except Exception as e:
        _LOG.exception(f"Error handling client request: {e}")
        return {'success': False, 'error': str(e)}


def setup_socket_server():
    """Set up the Unix domain socket server for IPC"""
    # Remove the socket file if it already exists
    try:
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
    except OSError as e:
        _LOG.error(f"Error removing existing socket file: {e}")
        sys.exit(1)

    # Create the socket server
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)

    # Set permissions to allow non-root processes to connect
    os.chmod(SOCKET_PATH, 0o666)

    server.listen(5)
    server.settimeout(1.0)  # 1 second timeout to allow checking the running flag

    return server


def main():
    try:
        # check_root_privileges()
        setup_logging()

        # Register signal handlers for graceful termination
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        print("NVIDIA root service started. Running until terminated...")
        _LOG.info("NVIDIA root service started")

        # Initialize repository
        repo = NvidiaRepository()

        # Set up socket server
        server = setup_socket_server()
        _LOG.info(f"Socket server listening on {SOCKET_PATH}")
        buffer_size = 4096
        # Main service loop
        while running:
            try:
                # Accept connections with timeout to allow checking the running flag
                client, _ = server.accept()
                client.settimeout(5.0)  # 5 second timeout for client operations

                try:
                    # Receive data from client
                    data = b''
                    while True:
                        chunk = client.recv(buffer_size)
                        if not chunk:
                            break
                        data += chunk

                    if data:
                        # Parse the request
                        request = json.loads(data.decode('utf-8'))
                        _LOG.info(f"Received request: {request}")

                        # Process the request
                        response = handle_client_request(repo, request)

                        # Send the response
                        client.sendall(json.dumps(response).encode('utf-8'))
                finally:
                    client.close()
            except socket.timeout:
                # This is expected due to the timeout we set
                pass
            except Exception as e:
                if running:  # Only log if we're still supposed to be running
                    _LOG.exception(f"Error in socket server: {e}")

        # Clean up
        server.close()
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)

        _LOG.info("NVIDIA root service shutting down")
        print("NVIDIA root service shut down")

    except PermissionError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        _LOG.exception("Unexpected error in NVIDIA root service")
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)

# test with sudo python3 -m gwe.repository.gwe_nvidia_root_service
if __name__ == '__main__':
    main()
