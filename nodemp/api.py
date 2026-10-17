"""Python view of NodeApi.

Callbacks stay on the server worker. ``job`` moves a callable onto the host's
pinned core; ``submit_job`` uses the server pool, which is not pinned.
"""

from __future__ import annotations

import asyncio
import ctypes
import heapq
import inspect
import json
import math
import traceback
from contextlib import contextmanager

from nodemp._abi import (
    ABI_MAJOR,
    CATALOG,
    CONST,
    NodeApi,
    NodeBuiltinEventCallback,
    NodeBusAnswerCallback,
    NodeCanRelayCallback,
    NodeCancellableEventCallback,
    NodeClientEventCallback,
    NodeHttpCallback,
    NodeJobDoneFn,
    NodeJobWorkFn,
    NodeLogSink,
    NodeModuleChannelCallback,
    NodeResourceEventCallback,
    NodeTimerCallback,
    NodeVehicleControls,
    NodeVehicleEventCallback,
    NodeVehicleInfo,
    NodeVehicleTransform,
)
from nodemp.state import current, slots_lock

_next_slot = 0
_SLOTS: dict[int, "Slot"] = {}
_TIMERS: dict[int, int] = {}
_relay_warned = False
_host_loop = None
_pump_armed = False
_pump_hook = None
_tasks: set[asyncio.Task] = set()


class NodeError(RuntimeError):
    pass


class _Raised:
    pass


_RAISED = _Raised()


class Handle:
    """Registration returned by ``on``, ``after`` and ``every``."""

    __slots__ = ("kind", "slot_id", "timer_id")

    def __init__(self, kind: str, slot_id: int, timer_id: int | None = None):
        self.kind = kind
        self.slot_id = slot_id
        self.timer_id = timer_id


class Slot:
    __slots__ = ("id", "kind", "name", "fn", "fn_done", "resource", "keep", "args", "result", "error", "timer_id")

    def __init__(self, slot_id: int, kind: str, fn, name=None):
        self.id = slot_id
        self.kind = kind
        self.name = name
        self.fn = fn
        self.fn_done = None
        self.resource = current.get()
        self.keep = None
        self.args = ()
        self.result = None
        self.error = None
        self.timer_id = None


class Bridge:
    def __init__(self):
        self.raw = None
        self.size = 0
        self._bound: dict[str, object] = {}

    def bind(self, addr: int) -> None:
        self.raw = NodeApi.from_address(int(addr))
        self.size = int(self.raw.struct_size)
        self._bound = {}
        major = int(self.raw.abi_version) >> 16
        if major != ABI_MAJOR:
            raise NodeError(f"server ABI major is {major}, this host was generated for {ABI_MAJOR}")

    def fn(self, name: str):
        found = self._bound.get(name)
        if found is not None:
            return found
        if self.raw is None:
            raise NodeError("python host is not connected to a server")
        spec = CATALOG[name]
        if spec["offset"] + 8 > self.size:
            raise NodeError(f"{name} is not provided by this server")
        found = getattr(self.raw, name)
        self._bound[name] = found
        return found

    def call(self, name: str, *args):
        return self.fn(name)(*_prepare(name, args))

    def text(self, name: str, *args):
        fn = self.fn(name)
        args = _prepare(name, args)
        # A real buffer first. This server answers -1 to the NULL size probe,
        # so a name query never got past the probe and came back empty.
        buf = ctypes.create_string_buffer(512)
        written = fn(*args, buf, len(buf))
        if isinstance(written, int) and 0 <= written < len(buf):
            return buf.raw[:written].decode("utf-8", "replace")
        size = fn(*args, None, 0)
        if not isinstance(size, int) or size < 0:
            return None
        if size == 0:
            return ""
        buf = ctypes.create_string_buffer(size + 1)
        written = fn(*args, buf, size + 1)
        if not isinstance(written, int) or written < 0:
            return None
        return buf.raw[:written].decode("utf-8", "replace")


bridge = Bridge()


def _bytes(value) -> bytes | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")


def _prepare(name: str, args: tuple) -> tuple:
    kinds = CATALOG[name]["kinds"]
    if "cstr" not in kinds:
        return args
    cooked = []
    for index, value in enumerate(args):
        if index < len(kinds) and kinds[index] == "cstr":
            cooked.append(_bytes(value))
        else:
            cooked.append(value)
    return tuple(cooked)


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _log_exc() -> None:
    try:
        bridge.call("log_error", traceback.format_exc())
    except Exception:
        return


def _alloc(kind: str, fn, name=None) -> Slot:
    global _next_slot
    with slots_lock:
        _next_slot += 1
        slot = Slot(_next_slot, kind, fn, name)
        _SLOTS[slot.id] = slot
        return slot


def _get(user) -> Slot | None:
    if not user:
        return None
    with slots_lock:
        return _SLOTS.get(int(user))


def _drop(slot_id: int) -> None:
    with slots_lock:
        _SLOTS.pop(slot_id, None)


def drop_resource(res) -> None:
    with slots_lock:
        dead = [sid for sid, slot in _SLOTS.items() if slot.resource is res]
        for sid in dead:
            _SLOTS.pop(sid, None)
        stale = [tid for tid, sid in _TIMERS.items() if sid not in _SLOTS]
        for tid in stale:
            _TIMERS.pop(tid, None)
    pending = [task for task in list(_tasks) if getattr(task, "_nodemp_resource", None) is res]
    for task in pending:
        task.cancel()
    if pending:
        _wake()


def _host_loop_get() -> asyncio.AbstractEventLoop:
    global _host_loop
    if _host_loop is None or _host_loop.is_closed():
        _host_loop = asyncio.new_event_loop()
    return _host_loop


def _next_delay_ms(loop: asyncio.AbstractEventLoop) -> int | None:
    if loop._ready:
        return 0
    scheduled = loop._scheduled
    while scheduled and scheduled[0].cancelled():
        heapq.heappop(scheduled)
    if not scheduled:
        return None
    delay = scheduled[0].when() - loop.time()
    if delay <= 0:
        return 0
    return max(1, math.ceil(delay * 1000))


def _arm_pump(ms: int) -> None:
    global _pump_armed
    if _pump_armed:
        return
    _pump_armed = True
    if _pump_hook is not None:
        _pump_hook(int(ms))
        return
    token = current.set(None)
    try:
        slot = _alloc("timeout", _run_pump)
    finally:
        current.reset(token)
    timer_id = bridge.call("set_timeout", int(ms), _on_timer, ctypes.c_void_p(slot.id))
    if not isinstance(timer_id, int) or timer_id < 0:
        _pump_armed = False
        _drop(slot.id)
        return
    slot.timer_id = int(timer_id)
    with slots_lock:
        _TIMERS[slot.timer_id] = slot.id


def _wake() -> None:
    loop = _host_loop
    if loop is None or loop.is_running() or _pump_hook is not None:
        _arm_pump(0)
        return
    _run_pump()


def _run_pump(_timer_id: int = 0) -> None:
    global _pump_armed
    _pump_armed = False
    loop = _host_loop_get()
    if loop.is_running():
        _arm_pump(0)
        return
    loop.call_soon(loop.stop)
    loop.run_forever()
    delay = _next_delay_ms(loop)
    if delay is not None:
        _arm_pump(delay)


async def _guard(coro):
    try:
        return await coro
    except asyncio.CancelledError:
        raise
    except Exception:
        _log_exc()
        return None


def spawn_coro(coro):
    if not inspect.iscoroutine(coro):
        raise TypeError("spawn expects a coroutine")
    loop = _host_loop_get()
    task = loop.create_task(_guard(coro))
    task._nodemp_resource = current.get()
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    _wake()
    return task


def _finish_sync(coro):
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    coro.close()
    raise RuntimeError("a request handler has to answer before the first await")


def _invoke(slot: Slot, args, log: bool = True, sync: bool = False):
    res = slot.resource
    if res is not None and not res.alive:
        return None
    token = current.set(res) if res is not None else None
    try:
        result = slot.fn(*args)
        if inspect.iscoroutine(result):
            if sync:
                return _finish_sync(result)
            spawn_coro(result)
            return None
        return result
    except Exception:
        if log:
            _log_exc()
        return _RAISED
    finally:
        if token is not None:
            current.reset(token)


def _write_reason(reason, cap: int, text: str) -> None:
    if not reason or cap <= 1:
        return
    raw = text.encode("utf-8")[: cap - 1]
    buf = ctypes.cast(reason, ctypes.POINTER(ctypes.c_char))
    for index, byte in enumerate(raw):
        buf[index] = bytes((byte,))
    buf[len(raw)] = b"\0"


def _make_plain(name: str):
    def method(self, *args, _name=name):
        return bridge.call(_name, *args)

    method.__name__ = name
    method.__doc__ = f"NodeApi.{name}"
    return method


def _make_text(name: str):
    def method(self, *args, _name=name):
        return bridge.text(_name, *args)

    method.__name__ = name
    method.__doc__ = f"NodeApi.{name}. Returns the text, or None when the server returns -1."
    return method


@NodeClientEventCallback
def _on_client(player_id, data, user):
    slot = _get(user)
    if slot is not None:
        _invoke(slot, (int(player_id), _as_text(data)))


@NodeBuiltinEventCallback
def _on_builtin(player_id, user):
    slot = _get(user)
    if slot is not None:
        _invoke(slot, (int(player_id),))


@NodeVehicleEventCallback
def _on_vehicle(player_id, global_id, data, user):
    slot = _get(user)
    if slot is not None:
        _invoke(slot, (int(player_id), int(global_id), _as_text(data)))


@NodeCancellableEventCallback
def _on_request(player_id, global_id, data, reason, reason_cap, user):
    slot = _get(user)
    if slot is None:
        return 0
    result = _invoke(slot, (int(player_id), int(global_id), _as_text(data)), sync=True)
    if result is _RAISED or result is False or isinstance(result, str):
        if isinstance(result, str):
            _write_reason(reason, int(reason_cap), result)
        return 1
    return 0


@NodeTimerCallback
def _on_timer(timer_id, user):
    slot = _get(user)
    if slot is None:
        return
    _invoke(slot, (int(timer_id),))
    if slot.kind == "timeout":
        _drop(slot.id)
        with slots_lock:
            _TIMERS.pop(int(timer_id), None)


@NodeHttpCallback
def _on_http(status, body, body_len, headers, user):
    slot = _get(user)
    if slot is None:
        return
    blob = b"" if not body or int(body_len) <= 0 else ctypes.string_at(body, int(body_len))
    _invoke(slot, (int(status), blob, _as_text(headers)))
    _drop(slot.id)


@NodeResourceEventCallback
def _on_resource(source, data, user):
    slot = _get(user)
    if slot is not None:
        _invoke(slot, (_as_text(source), _as_text(data)))


@NodeBusAnswerCallback
def _on_answer(source, value, user):
    slot = _get(user)
    if slot is not None:
        _invoke(slot, (_as_text(source), _as_text(value)))


@NodeModuleChannelCallback
def _on_channel(player_id, channel, data, length, user):
    slot = _get(user)
    if slot is None:
        return
    blob = b"" if not data or int(length) <= 0 else ctypes.string_at(data, int(length))
    _invoke(slot, (int(player_id), int(channel), blob))


@NodeLogSink
def _on_sink(tag, level, msg, user):
    slot = _get(user)
    if slot is None:
        return 0
    result = _invoke(slot, (_as_text(tag), int(level), _as_text(msg)), log=False)
    return 1 if result is True else 0


@NodeCanRelayCallback
def _on_relay(from_id, to_id, category, subtype, global_id, user):
    slot = _get(user)
    if slot is None:
        return 0
    result = _invoke(
        slot,
        (int(from_id), int(to_id), int(category), int(subtype), int(global_id)),
        log=False,
    )
    return 1 if result is True else 0


@NodeJobWorkFn
def _pool_work(user):
    slot = _get(user)
    if slot is None:
        return None
    res = slot.resource
    if res is not None and not res.alive:
        slot.result = None
        slot.error = None
        return None
    token = current.set(res) if res is not None else None
    try:
        slot.result = slot.fn(*slot.args)
        slot.error = None
    except Exception:
        slot.result = None
        slot.error = traceback.format_exc()
    finally:
        if token is not None:
            current.reset(token)
    return None


@NodeJobDoneFn
def _pool_done(result, user):
    del result
    slot = _get(user)
    if slot is None:
        return
    if slot.fn_done is not None and (slot.resource is None or slot.resource.alive):
        token = current.set(slot.resource) if slot.resource is not None else None
        try:
            slot.fn_done(slot.result, slot.error)
        except Exception:
            _log_exc()
        finally:
            if token is not None:
                current.reset(token)
    elif slot.error:
        try:
            bridge.call("log_error", slot.error)
        except Exception:
            pass
    _drop(slot.id)


REQUESTS = {
    "playerConnectRequest",
    "vehicleSpawnRequest",
    "vehicleEnterRequest",
    "vehicleExitRequest",
    "vehicleCouplerRequest",
    "vehicleEditRequest",
    "vehiclePaintRequest",
    "vehicleTriggerRequest",
    "vehicleNodeGrabRequest",
    "queueJoinRequest",
    "queueAdmitRequest",
}

VEHICLE = {
    "vehicleEdited",
    "vehicleReset",
    "vehiclePainted",
    "playerSeatChanged",
    "vehicleCouplerChanged",
    "vehicleControllerChanged",
    "playerVerifyReported",
    "playerLagging",
    "playerRecovered",
    "vehicleDeleted",
    "playerLeft",
    "resourceUnload",
    "queueAdmitted",
    "queueLeft",
    "queueNoShow",
    "playerJoinSlow",
    "serverShutdownScheduled",
    "playerPolicyChanged",
    "playerFrozenChanged",
}

BUILTIN = {
    "playerAuthenticated",
    "playerJoined",
    "playerLeft",
    "serverTick",
    "serverShutdown",
    "vehicleSpawned",
    "vehicleDeleted",
    "vehicleDamageChanged",
    "vehicleTagsChanged",
    "vehicleLockChanged",
    "vehiclePositionChanged",
    "vehicleInputsChanged",
    "vehicleElectricsChanged",
    "vehiclePowertrainChanged",
    "vehicleEngineChanged",
    "vehicleNodesChanged",
    "playerInputsChanged",
    "playerHeadPoseChanged",
    "vehicleTeleported",
    "vehicleBreakGroupsChanged",
    "playerCameraChanged",
}


class Node:
    """The ``node`` object injected into a Python resource."""

    @property
    def resource_name(self) -> str | None:
        res = current.get()
        return None if res is None else res.name

    @property
    def resource_dir(self) -> str | None:
        res = current.get()
        return None if res is None else res.directory

    @property
    def trusted(self) -> bool:
        res = current.get()
        return bool(res and res.trusted and res.alive)

    @property
    def mojo(self):
        import nodemp.mojo as mojo

        return mojo

    @contextmanager
    def _owned(self):
        res = current.get()
        pushed = False
        if res is not None and "set_resource_owner" in CATALOG:
            bridge.call("set_resource_owner", res.name_bytes)
            pushed = True
        try:
            yield
        finally:
            if pushed:
                bridge.call("set_resource_owner", None)

    def _bind_event(self, kind: str, name: str, fn, callback) -> Handle:
        slot = _alloc(kind, fn, name)
        slot.keep = _bytes(name)
        with self._owned():
            bridge.call(f"register_{kind}_event", slot.keep, callback, ctypes.c_void_p(slot.id))
        return Handle(kind, slot.id)

    def on_client(self, name: str, fn) -> Handle:
        return self._bind_event("client", name, fn, _on_client)

    def on_builtin(self, name: str, fn) -> Handle:
        return self._bind_event("builtin", name, fn, _on_builtin)

    def on_vehicle(self, name: str, fn) -> Handle:
        return self._bind_event("vehicle", name, fn, _on_vehicle)

    def on_request(self, name: str, fn) -> Handle:
        """Deny with ``False`` or a reason string. ``None`` or ``True`` allows.

        A handler that raises is a deny, and the traceback is logged.
        ``async def`` may be used only when it answers before the first ``await``.
        """
        return self._bind_event("cancellable", name, fn, _on_request)

    def on_resource(self, name: str, fn) -> Handle:
        return self._bind_event("resource", name, fn, _on_resource)

    def on(self, name: str, fn) -> Handle:
        """Subscribe by the server's event name.

        Request names become verdict handlers. Notification names that carry a
        payload become vehicle callbacks. One-argument notifications become
        builtins. Any other name is a client wire event.
        """
        if name in REQUESTS:
            return self.on_request(name, fn)
        if name in VEHICLE:
            return self.on_vehicle(name, fn)
        if name in BUILTIN:
            return self.on_builtin(name, fn)
        return self.on_client(name, fn)

    def on_channel(self, channel: int, fn) -> Handle:
        slot = _alloc("channel", fn, int(channel))
        with self._owned():
            bridge.call("register_module_channel", int(channel), _on_channel, ctypes.c_void_p(slot.id))
        return Handle("channel", slot.id)

    def off(self, handle: Handle) -> None:
        if handle.kind == "timer":
            self.cancel(handle)
            return
        with slots_lock:
            slot = _SLOTS.get(handle.slot_id)
        if slot is None:
            return
        user = ctypes.c_void_p(slot.id)
        if slot.kind == "channel":
            bridge.call("unregister_module_channel", int(slot.name), _on_channel, user)
        elif slot.kind == "relay":
            bridge.call("unregister_relay_filter", _on_relay, user)
        elif slot.kind == "sink":
            bridge.call("set_log_sink", None, None)
        else:
            callback = {
                "client": _on_client,
                "builtin": _on_builtin,
                "vehicle": _on_vehicle,
                "cancellable": _on_request,
                "resource": _on_resource,
            }[slot.kind]
            bridge.call(f"unregister_{slot.kind}_event", slot.keep, callback, user)
        _drop(slot.id)

    def set_timeout(self, ms: int, fn) -> Handle:
        slot = _alloc("timeout", fn)
        with self._owned():
            timer_id = bridge.call("set_timeout", int(ms), _on_timer, ctypes.c_void_p(slot.id))
        if timer_id < 0:
            _drop(slot.id)
            raise NodeError("set_timeout failed")
        slot.timer_id = int(timer_id)
        with slots_lock:
            _TIMERS[slot.timer_id] = slot.id
        return Handle("timer", slot.id, slot.timer_id)

    def set_interval(self, ms: int, fn) -> Handle:
        slot = _alloc("interval", fn)
        with self._owned():
            timer_id = bridge.call("set_interval", int(ms), _on_timer, ctypes.c_void_p(slot.id))
        if timer_id < 0:
            _drop(slot.id)
            raise NodeError("set_interval failed")
        slot.timer_id = int(timer_id)
        with slots_lock:
            _TIMERS[slot.timer_id] = slot.id
        return Handle("timer", slot.id, slot.timer_id)

    def after(self, ms: int, fn) -> Handle:
        return self.set_timeout(ms, fn)

    def every(self, ms: int, fn) -> Handle:
        return self.set_interval(ms, fn)

    def cancel(self, handle) -> None:
        timer_id = handle.timer_id if isinstance(handle, Handle) else int(handle)
        bridge.call("clear_timer", int(timer_id))
        with slots_lock:
            slot_id = _TIMERS.pop(int(timer_id), None)
            if slot_id is not None:
                _SLOTS.pop(slot_id, None)

    def set_log_sink(self, fn) -> Handle | None:
        if fn is None:
            bridge.call("set_log_sink", None, None)
            return None
        slot = _alloc("sink", fn)
        bridge.call("set_log_sink", _on_sink, ctypes.c_void_p(slot.id))
        return Handle("sink", slot.id)

    def relay_filter(self, fn) -> Handle:
        global _relay_warned
        if not _relay_warned:
            bridge.call("log_warn", "python relay filter runs on network threads; keep it to pure data")
            _relay_warned = True
        slot = _alloc("relay", fn)
        with self._owned():
            bridge.call("register_relay_filter", _on_relay, ctypes.c_void_p(slot.id))
        return Handle("relay", slot.id)

    def http_request(self, method: str, url: str, callback, headers=None, body=None) -> Handle:
        slot = _alloc("http", callback)
        method_b = _bytes(method)
        url_b = _bytes(url)
        headers_b = _bytes(json.dumps(headers)) if headers is not None else None
        body_b = None if body is None else (_bytes(body) if isinstance(body, str) else bytes(body))
        pointer = None
        if body_b is not None:
            pointer = ctypes.create_string_buffer(body_b)
        slot.keep = (method_b, url_b, headers_b, body_b, pointer)
        with self._owned():
            rc = bridge.call(
                "http_request",
                method_b,
                url_b,
                headers_b,
                pointer,
                0 if body_b is None else len(body_b),
                _on_http,
                ctypes.c_void_p(slot.id),
            )
        if rc != 0:
            _drop(slot.id)
            raise NodeError("http_request failed")
        return Handle("http", slot.id)

    def ask(self, name: str, data="", callback=None):
        """Call bus subscribers on the worker and return ``[(source, json), ...]``."""
        found = []

        def collect(source, value, _callback=callback, _found=found):
            _found.append((source, value))
            if _callback is not None:
                _callback(source, value)

        slot = _alloc("ask", collect, name)
        slot.keep = (_bytes(name), _bytes(data if data is not None else ""))
        try:
            count = bridge.call(
                "call_resource_event",
                slot.keep[0],
                slot.keep[1],
                _on_answer,
                ctypes.c_void_p(slot.id),
            )
        finally:
            _drop(slot.id)
        if count < 0:
            raise NodeError("call_resource_event must run on the server worker")
        return found

    def submit_job(self, fn, *args, done=None):
        """Run ``fn(*args)`` on the server's background pool. That pool is not pinned."""
        slot = _alloc("pool", fn)
        slot.args = args
        slot.fn_done = done
        with self._owned():
            rc = bridge.call("submit_job", _pool_work, _pool_done, ctypes.c_void_p(slot.id))
        if rc != 0:
            _drop(slot.id)
            raise NodeError("submit_job failed")
        return Handle("pool", slot.id)

    def spawn(self, coro):
        """Run a coroutine on the worker. The worker stays free while it waits."""
        return spawn_coro(coro)

    def job(self, fn, *args, done=None):
        """Run ``fn(*args)`` on the pinned core. ``done(result, error)`` runs on the worker.

        Inside ``async def``, ``await node.job(...)`` returns the value. A failure raises
        ``NodeError``. ``error`` for ``done`` is a traceback string, or None. Do not wait
        inside ``fn`` for ``done``: ``done`` is delivered on the worker after ``fn`` returns.
        """
        if not callable(fn):
            raise TypeError("job function must be callable")
        if done is not None and not callable(done):
            raise TypeError("done must be callable")
        res = current.get()
        fut = None
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and asyncio.current_task(running) is not None:
            fut = running.create_future()

            def _retrieve(done_fut, _fut=fut):
                del _fut
                if not done_fut.cancelled():
                    done_fut.exception()

            fut.add_done_callback(_retrieve)

        def run(_fn=fn, _args=args, _res=res):
            token = current.set(_res) if _res is not None else None
            try:
                if _res is not None and not _res.alive:
                    return None
                return _fn(*_args)
            finally:
                if token is not None:
                    current.reset(token)

        def finish(result, error, _done=done, _res=res, _fut=fut):
            if _fut is not None and not _fut.done():
                if error:
                    _fut.set_exception(NodeError(error))
                else:
                    _fut.set_result(result)
                _wake()
            if _done is None:
                if error and _fut is None:
                    try:
                        bridge.call("log_error", error)
                    except Exception:
                        pass
                return
            if _res is not None and not _res.alive:
                return
            token = current.set(_res) if _res is not None else None
            try:
                _done(result, error)
            finally:
                if token is not None:
                    current.reset(token)

        import _nodemp

        _nodemp.core_submit(run, finish)
        return fut

    def core_info(self) -> dict:
        import _nodemp

        return _nodemp.core_info()

    def on_core(self) -> bool:
        import _nodemp

        return bool(_nodemp.on_core())

    def reserve_core(self) -> None:
        import _nodemp

        _nodemp.reserve()

    def json(self, method: str, *args):
        text = getattr(self, method)(*args)
        if text is None:
            return None
        return json.loads(text)

    def world(self):
        return self.json("get_world_snapshot_json")

    def metrics(self):
        return self.json("get_metrics_json")

    def _id_list(self, name: str, prefix, cap: int = 64) -> list[int]:
        while True:
            arr = (ctypes.c_int * cap)()
            count = bridge.call(name, *prefix, arr, cap)
            if not isinstance(count, int) or count < 0:
                return []
            if count < cap or cap >= 100000:
                return [int(arr[i]) for i in range(count)]
            cap *= 2

    def get_players(self) -> list[int]:
        count = self.get_player_count()
        return self._id_list("get_players", (), max(int(count), 1))

    def get_vehicles(self) -> list[int]:
        count = self.get_vehicle_count()
        return self._id_list("get_vehicles", (), max(int(count), 1))

    def get_player_vehicles(self, player_id: int) -> list[int]:
        count = self.get_vehicle_count()
        return self._id_list("get_player_vehicles", (int(player_id),), max(int(count), 1))

    def get_player_vehicle(self, player_id: int):
        role = ctypes.c_int()
        gid = bridge.call("get_player_vehicle", int(player_id), ctypes.byref(role))
        return int(gid), int(role.value)

    def get_vehicle_occupants(self, global_id: int) -> list[dict]:
        cap = 8
        while True:
            ids = (ctypes.c_int * cap)()
            roles = (ctypes.c_int * cap)()
            count = bridge.call("get_vehicle_occupants", int(global_id), ids, roles, cap)
            if not isinstance(count, int) or count < 0:
                return []
            if count < cap or cap >= 256:
                return [{"player_id": int(ids[i]), "role": int(roles[i])} for i in range(count)]
            cap *= 2

    def get_vehicles_by_tag(self, key: str, value=None) -> list[int]:
        return self._id_list("get_vehicles_by_tag", (_bytes(key), None if value is None else _bytes(value)))

    def get_vehicles_by_tags(self, pairs) -> list[int]:
        if not isinstance(pairs, str):
            pairs = json.dumps(pairs)
        return self._id_list("get_vehicles_by_tags", (_bytes(pairs),))

    def get_vehicle_info(self, global_id: int):
        info = NodeVehicleInfo()
        if bridge.call("get_vehicle_info", int(global_id), ctypes.byref(info)) != 0:
            return None
        return {
            "global_id": int(info.global_id),
            "spawner_id": int(info.spawner_id),
            "driver_id": int(info.driver_id),
            "authority_id": int(info.authority_id),
            "is_unicycle": int(info.is_unicycle),
            "spawner_online": int(info.spawner_online),
            "idle_seconds": float(info.idle_seconds),
            "owner_name": bytes(info.owner_name).split(b"\0", 1)[0].decode("utf-8", "replace"),
        }

    def get_vehicle_transform(self, global_id: int):
        out = NodeVehicleTransform()
        if bridge.call("get_vehicle_transform", int(global_id), ctypes.byref(out)) != 0:
            return None
        return {
            "pos": (out.pos[0], out.pos[1], out.pos[2]),
            "rot": (out.rot[0], out.rot[1], out.rot[2], out.rot[3]),
            "vel": (out.vel[0], out.vel[1], out.vel[2]),
            "ang_vel": (out.ang_vel[0], out.ang_vel[1], out.ang_vel[2]),
            "tim_ms": int(out.tim_ms),
            "ping_ms": int(out.ping_ms),
            "seq": int(out.seq),
            "is_transitioning": int(out.is_transitioning),
            "paused": int(out.paused),
        }

    def get_vehicle_controls(self, global_id: int):
        out = NodeVehicleControls()
        if bridge.call("get_vehicle_controls", int(global_id), ctypes.byref(out)) != 0:
            return None
        return {
            "steering": float(out.steering),
            "throttle": float(out.throttle),
            "brake": float(out.brake),
            "clutch": float(out.clutch),
            "parking_brake": float(out.parking_brake),
            "gear": bytes(out.gear).split(b"\0", 1)[0].decode("utf-8", "replace"),
        }

    def get_player_position(self, player_id: int):
        pos = (ctypes.c_float * 3)()
        if bridge.call("get_player_position", int(player_id), pos) != 0:
            return None
        return (float(pos[0]), float(pos[1]), float(pos[2]))

    def get_vehicle_config_hash(self, global_id: int):
        out = ctypes.c_uint32()
        if bridge.call("get_vehicle_config_hash", int(global_id), ctypes.byref(out)) != 0:
            return None
        return int(out.value)

    def get_vehicle_damage_blob(self, global_id: int):
        fn = bridge.fn("get_vehicle_damage_blob")
        size = fn(int(global_id), None, 0, None)
        if not isinstance(size, int) or size < 0:
            return None
        if size == 0:
            return b"", 0
        buf = (ctypes.c_uint8 * size)()
        counter = ctypes.c_uint32()
        written = fn(int(global_id), buf, size, ctypes.byref(counter))
        if not isinstance(written, int) or written < 0:
            return None
        return bytes(buf[:written]), int(counter.value)

    def get_vehicle_lock(self, global_id: int, limit: int = 128):
        whitelist = (ctypes.c_int * limit)()
        count = ctypes.c_int()
        mode = bridge.call("get_vehicle_lock", int(global_id), whitelist, int(limit), ctypes.byref(count))
        if mode < 0:
            return None
        return {"mode": int(mode), "whitelist": [int(whitelist[i]) for i in range(int(count.value))]}

    def set_vehicle_lock(self, global_id: int, mode: int, whitelist=()) -> int:
        if not whitelist:
            return int(bridge.call("set_vehicle_lock", int(global_id), int(mode), None, 0))
        arr = (ctypes.c_int * len(whitelist))(*[int(item) for item in whitelist])
        return int(bridge.call("set_vehicle_lock", int(global_id), int(mode), arr, len(whitelist)))

    def sha256_hex(self, data) -> str:
        raw = b"" if data is None else (_bytes(data) if isinstance(data, str) else bytes(data))
        buf = ctypes.create_string_buffer(raw)
        out = ctypes.create_string_buffer(65)
        bridge.call("sha256_hex", buf, len(raw), out)
        return out.value.decode("ascii")

    def hmac_sha256_hex(self, key, data) -> str:
        key_b = b"" if key is None else (_bytes(key) if isinstance(key, str) else bytes(key))
        data_b = b"" if data is None else (_bytes(data) if isinstance(data, str) else bytes(data))
        key_buf = ctypes.create_string_buffer(key_b)
        data_buf = ctypes.create_string_buffer(data_b)
        out = ctypes.create_string_buffer(65)
        bridge.call("hmac_sha256_hex", key_buf, len(key_b), data_buf, len(data_b), out)
        return out.value.decode("ascii")

    def random_bytes(self, n: int) -> bytes:
        n = int(n)
        if n < 1 or n > 65536:
            raise ValueError("n must be 1..65536")
        buf = ctypes.create_string_buffer(n)
        if bridge.call("random_bytes", buf, n) != 0:
            raise NodeError("random_bytes failed")
        return buf.raw

    def emit_client_bytes(self, player_id: int, event: str, data) -> int:
        raw = b"" if data is None else bytes(data)
        buf = ctypes.create_string_buffer(raw)
        return int(bridge.call("emit_client_bytes", int(player_id), _bytes(event), buf, len(raw)))

    def emit_all_bytes(self, event: str, data) -> None:
        raw = b"" if data is None else bytes(data)
        buf = ctypes.create_string_buffer(raw)
        bridge.call("emit_all_bytes", _bytes(event), buf, len(raw))

    def send_module(self, player_id: int, channel: int, data) -> int:
        raw = b"" if data is None else bytes(data)
        buf = ctypes.create_string_buffer(raw)
        return int(bridge.call("send_module", int(player_id), int(channel), buf, len(raw)))


for _name, _spec in CATALOG.items():
    if hasattr(Node, _name):
        continue
    if _spec["auto"] == "plain":
        setattr(Node, _name, _make_plain(_name))
    elif _spec["auto"] == "text":
        setattr(Node, _name, _make_text(_name))

Node.log = Node.log_info
Node.cancel_timer = Node.cancel

for _key, _value in CONST.items():
    setattr(Node, _key, _value)

node = Node()
