"""Process-wide state shared by the host and the resource API."""

from __future__ import annotations

import contextvars
import threading


class Resource:
    def __init__(self, name: str, directory: str):
        self.name = name
        self.directory = directory
        self.name_bytes = name.encode("utf-8")
        self.trusted = False
        self.alive = True
        self.namespace = None
        self.on_path = False


current: contextvars.ContextVar = contextvars.ContextVar("nodemp_resource", default=None)
host_io: contextvars.ContextVar = contextvars.ContextVar("nodemp_host_io", default=False)
slots_lock = threading.Lock()
resources: dict[str, Resource] = {}
resources_lock = threading.Lock()
