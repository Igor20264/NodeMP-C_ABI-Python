"""Load Python resources and keep untrusted ones inside their folder."""

from __future__ import annotations

import os
import shlex
import sys
import traceback

from nodemp.api import NodeError, bridge, drop_resource, node
from nodemp.state import Resource, current, host_io, resources, resources_lock

_BLOCKED_MODULES = {"subprocess", "multiprocessing", "ctypes", "_ctypes", "faulthandler"}
_DANGEROUS_EVENTS = {
    "subprocess.Popen",
    "os.system",
    "os.exec",
    "os.posix_spawn",
    "os.spawn",
    "os._exit",
    "os.putenv",
    "os.unsetenv",
    "ctypes.dlopen",
    "ctypes.dlsym",
    "sys.addaudithook",
}
_NATIVE_SUFFIXES = (".pyd", ".dll", ".so", ".dylib")
_PIP_NAMES = {"pip", "pip3"}
_PY_NAMES = {"py", "python", "python3", "pythonw"}
_hook = False
_real_system = None


_root_cache: dict[str, str | None] = {}
_prefix_norm: list[str] | None = None
_has_untrusted = False


def _norm_root(root) -> str | None:
    try:
        return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(root))))
    except (OSError, TypeError, ValueError):
        return None


def _cached_root(root) -> str | None:
    if not root:
        return None
    key = root if isinstance(root, str) else os.fspath(root)
    if key in _root_cache:
        return _root_cache[key]
    base = _norm_root(key)
    _root_cache[key] = base
    return base


def path_allowed(path, roots) -> bool:
    try:
        cheap = os.path.normcase(os.path.abspath(os.fspath(path)))
    except (OSError, TypeError, ValueError):
        return False
    bases = []
    for root in roots:
        base = _cached_root(root)
        if not base:
            continue
        bases.append(base)
        if cheap == base or cheap.startswith(base + os.sep):
            return True
    if not bases:
        return False
    try:
        candidate = os.path.normcase(os.path.realpath(cheap))
    except (OSError, TypeError, ValueError):
        return False
    if candidate == cheap:
        return False
    for base in bases:
        if candidate == base or candidate.startswith(base + os.sep):
            return True
    return False


def _site_roots() -> list[str]:
    found = []
    try:
        import site

        user = site.getusersitepackages()
        if isinstance(user, str):
            found.append(user)
        for value in site.getsitepackages():
            if value:
                found.append(value)
    except Exception:
        return found
    return found


def _note_trust() -> None:
    global _has_untrusted
    with resources_lock:
        _has_untrusted = any(res.alive and not res.trusted for res in resources.values())


def prefix_roots() -> list[str]:
    global _prefix_norm
    if _prefix_norm is not None:
        return _prefix_norm
    roots = []
    for value in (
        getattr(sys, "base_prefix", None),
        getattr(sys, "prefix", None),
        getattr(sys, "_stdlib_dir", None),
        *_site_roots(),
    ):
        if value and value not in roots:
            roots.append(value)
    _prefix_norm = roots
    return roots


def privileged() -> bool:
    if host_io.get():
        return True
    res = current.get()
    return bool(res and res.trusted and res.alive)


def native_allowed() -> bool:
    return privileged()


def _any_untrusted() -> bool:
    return _has_untrusted


def _audit(event, args):
    if event in _DANGEROUS_EVENTS and not privileged():
        raise PermissionError(f"{event} is not available to this python resource")
    if event == "import" and not privileged():
        module = args[0] if args else ""
        filename = args[1] if len(args) > 1 else None
        root = str(module or "").split(".", 1)[0]
        if root in _BLOCKED_MODULES:
            raise PermissionError(f"import of {root} is not available to this python resource")
        if filename and str(filename).lower().endswith(_NATIVE_SUFFIXES):
            if not path_allowed(filename, prefix_roots()):
                raise PermissionError(f"native import is outside the Python install: {filename}")
    if event == "open" and not privileged():
        path = args[0] if args else None
        if path is None or isinstance(path, int):
            return
        res = current.get()
        if res is not None and not res.trusted:
            roots = (res.directory, *prefix_roots())
        elif _has_untrusted:
            roots = prefix_roots()
        else:
            return
        if not path_allowed(path, roots):
            raise PermissionError(f"path is outside the resource: {path}")


def _exe_name(token: str) -> str:
    name = os.path.basename(token).lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def pip_argv(command) -> list[str] | None:
    """Return pip arguments when ``command`` is one pip invocation and nothing else."""
    if not isinstance(command, str):
        return None
    if any(char in command for char in "&|;<>\n\r"):
        return None
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not parts:
        return None
    program = _exe_name(parts[0])
    if program in _PIP_NAMES:
        return parts[1:]
    if program not in _PY_NAMES and not program.startswith("python3."):
        return None
    index = 1
    if index < len(parts) and parts[index].startswith("-") and parts[index] != "-m":
        if not parts[index].startswith("-3"):
            return None
        index += 1
    if index + 1 >= len(parts) or parts[index] != "-m" or _exe_name(parts[index + 1]) not in _PIP_NAMES:
        return None
    return parts[index + 2 :]


def _system_status(code: int) -> int:
    if os.name == "nt" or code < 0:
        return code
    return code << 8


def _note(text: str) -> None:
    if bridge.raw is None:
        return
    try:
        bridge.call("log_info", text)
    except Exception:
        return


def _import_pip():
    import pip  # noqa: F401

    return pip


def _ensure_pip() -> None:
    try:
        _import_pip()
        return
    except ModuleNotFoundError:
        pass
    import site

    site.main()
    try:
        _import_pip()
        return
    except ModuleNotFoundError:
        pass
    import ensurepip

    ensurepip.bootstrap(upgrade=True)
    _import_pip()


def _refresh_imports() -> None:
    import importlib
    import site

    importlib.invalidate_caches()
    try:
        site.main()
    except Exception:
        return


def run_embedded_pip(args: list[str]) -> int:
    """Run pip inside this interpreter and return an ``os.system`` status."""
    import runpy

    _note("python: pip " + " ".join(args))
    try:
        _ensure_pip()
    except Exception:
        traceback.print_exc()
        return _system_status(1)
    saved = sys.argv[:]
    code = 1
    try:
        sys.argv = ["pip", *args]
        try:
            runpy.run_module("pip", run_name="__main__")
            code = 0
        except SystemExit as exc:
            status = exc.code
            if status is None or status == 0:
                code = 0
            elif isinstance(status, int):
                code = status
            else:
                print(status, file=sys.stderr)
                code = 1
        except Exception:
            traceback.print_exc()
            code = 1
    finally:
        sys.argv = saved
    if code == 0:
        _refresh_imports()
    return _system_status(code)


def _system(command):
    args = pip_argv(command)
    if args is not None:
        if not privileged():
            raise PermissionError("pip is available to trusted python resources")
        return run_embedded_pip(args)
    return _real_system(command)


def install_system_hook() -> None:
    global _real_system
    if _real_system is None:
        _real_system = os.system
    os.system = _system


def bind(addr: int) -> None:
    global _hook
    if sys.version_info < (3, 14):
        raise NodeError(f"pyhost requires Python 3.14 or newer, this interpreter is {sys.version}")
    bridge.bind(int(addr))
    if not _hook:
        sys.addaudithook(_audit)
        install_system_hook()
        _hook = True


def _log_exc() -> None:
    try:
        bridge.call("log_error", traceback.format_exc())
    except Exception:
        return


def load_resource(name: str, directory: str, entry: str) -> int:
    if not entry:
        bridge.call("log_error", f"python: {name} has no server.main")
        return -1
    path = entry if os.path.isabs(entry) else os.path.join(directory, entry)
    res = Resource(name, directory)
    with resources_lock:
        resources[name] = res
    token = current.set(res)
    try:
        try:
            res.trusted = bool(node.is_resource_trusted(name))
        except NodeError:
            res.trusted = False
        _note_trust()
        io = host_io.set(True)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                source = handle.read()
        finally:
            host_io.reset(io)
        code = compile(source, path, "exec")
        namespace = {
            "__name__": f"nodemp_resource_{name}",
            "__file__": path,
            "__package__": None,
            "node": node,
        }
        if directory not in sys.path:
            sys.path.insert(0, directory)
            res.on_path = True
        res.namespace = namespace
        exec(code, namespace, namespace)
        return 0
    except Exception:
        res.alive = False
        _log_exc()
        bridge.call("log_error", f"python: {name} failed to load")
        return -1
    finally:
        _note_trust()
        current.reset(token)


def unload_resource(name: str) -> None:
    with resources_lock:
        res = resources.get(name)
    if res is None or not res.alive:
        return
    token = current.set(res)
    try:
        fn = None if res.namespace is None else res.namespace.get("unload")
        if callable(fn):
            try:
                fn()
            except Exception:
                _log_exc()
    finally:
        res.alive = False
        if res.on_path:
            try:
                sys.path.remove(res.directory)
            except ValueError:
                pass
            res.on_path = False
        drop_resource(res)
        with resources_lock:
            if resources.get(name) is res:
                resources.pop(name, None)
        _note_trust()
        current.reset(token)
