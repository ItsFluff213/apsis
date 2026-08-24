"""Thin wrapper around the kRPC connection with background reconnect.

Connecting never blocks the caller: a background thread keeps trying (and
keeps watching an established connection for drops) so the web server can
start and serve the dashboard immediately, showing a "waiting for KSP"
state until the game + kRPC server are actually up.
"""

import logging
import threading
import time

import krpc

logger = logging.getLogger("krpc_client")


class NotConnected(Exception):
    """kRPC isn't reachable yet -- KSP may not be running / server not started."""


class KRPCClient:
    def __init__(self, name="KSP Autopilot", address="127.0.0.1", rpc_port=50000, stream_port=50001,
                 retry_seconds=5, watchdog_seconds=3):
        self._name = name
        self._address = address
        self._rpc_port = rpc_port
        self._stream_port = stream_port
        self._retry_seconds = retry_seconds
        self._watchdog_seconds = watchdog_seconds
        self._conn = None
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def _try_connect_once(self):
        return krpc.connect(
            name=self._name,
            address=self._address,
            rpc_port=self._rpc_port,
            stream_port=self._stream_port,
        )

    def _watchdog_loop(self):
        while not self._stop.is_set():
            if self._conn is None:
                try:
                    conn = self._try_connect_once()
                    with self._lock:
                        self._conn = conn
                    logger.info("Connected to kRPC server (%s)", conn.krpc.get_status().version)
                except Exception:
                    self._stop.wait(self._retry_seconds)
                    continue
            else:
                try:
                    self._conn.krpc.get_status()
                except Exception:
                    logger.warning("Lost connection to kRPC, will reconnect...")
                    with self._lock:
                        self._conn = None
            self._stop.wait(self._watchdog_seconds)

    def connect_in_background(self):
        """Returns immediately; connection happens (and is maintained) on a
        daemon thread so the web server never blocks on KSP being up."""
        thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        thread.start()

    @property
    def is_connected(self):
        return self._conn is not None

    @property
    def conn(self):
        conn = self._conn
        if conn is None:
            raise NotConnected("kRPC server not reachable -- is KSP running with the kRPC server started?")
        return conn

    @property
    def space_center(self):
        return self.conn.space_center
