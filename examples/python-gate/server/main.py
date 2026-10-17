# Запрос обязан ответить до выхода из функции.
# None разрешает, строка запрещает и уходит игроку как причина.
# HTTP живёт в своём потоке: uvicorn.run на worker остановил бы весь сервер.

import threading

BLOCKED = {"Player9"}
CAR_LIMIT = 2
HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8090

http_server = None
http_thread = None


def on_connect(player_id, global_id, name):
    del player_id, global_id
    if name in BLOCKED:
        return "You are not allowed on this server"
    node.log_info(f"python-gate: {name} passed the connect gate")
    return None


def on_spawn(player_id, global_id, config):
    del global_id, config
    owned = node.get_player_vehicles(player_id)
    if len(owned) >= CAR_LIMIT:
        return f"Vehicle limit reached ({CAR_LIMIT} per player)"
    name = node.get_player_name(player_id) or "?"
    node.log_info(f"python-gate: {name} spawns a vehicle ({len(owned) + 1}/{CAR_LIMIT})")
    return None


def greet(player_id):
    name = node.get_player_name(player_id) or "?"
    node.log_info(f"python-gate: {name} joined")
    node.emit_client(player_id, "greeting", f"Welcome, {name}")


def on_leave(player_id):
    name = node.get_player_name(player_id) or "?"
    node.log_info(f"python-gate: {name} left")


def on_minute(timer_id):
    del timer_id
    node.log_info(f"python-gate: {node.get_player_count()} players online")


def start_http():
    global http_server, http_thread
    try:
        import uvicorn
        from fastapi import FastAPI
    except ModuleNotFoundError:
        node.log_error("python-gate: fastapi or uvicorn is not installed for this Python")
        return

    app = FastAPI(title="python-gate")

    @app.get("/")
    def root():
        return {"resource": "python-gate", "players": node.get_player_count()}

    @app.get("/players")
    def players():
        found = []
        for player_id in node.get_players():
            found.append({"id": int(player_id), "name": node.get_player_name(player_id) or "?"})
        return found

    config = uvicorn.Config(app, host=HTTP_HOST, port=HTTP_PORT, log_level="warning", access_log=False)
    http_server = uvicorn.Server(config)
    http_thread = threading.Thread(target=http_server.run, name="python-gate-http", daemon=True)
    http_thread.start()
    node.log_info(f"python-gate: http://{HTTP_HOST}:{HTTP_PORT}")


node.on("playerConnectRequest", on_connect)
node.on("vehicleSpawnRequest", on_spawn)
node.on("playerJoined", greet)
# playerLeft есть и как уведомление с одним id, и как событие с тремя полями.
# node.on выбирает второй вариант, поэтому здесь явный канал.
node.on_builtin("playerLeft", on_leave)
clock = node.every(60000, on_minute)

for player_id in node.get_players():
    greet(player_id)

start_http()
node.log_info("python-gate loaded")


def unload():
    node.cancel(clock)
    if http_server is not None:
        http_server.should_exit = True
    if http_thread is not None:
        http_thread.join(timeout=3)
    node.log_info("python-gate unloaded")
