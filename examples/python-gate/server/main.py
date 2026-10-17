# Запрос обязан ответить до выхода из функции.
# None разрешает, строка запрещает и уходит игроку как причина.

BLOCKED = {"Player9"}
CAR_LIMIT = 2

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


node.on("playerConnectRequest", on_connect)
node.on("vehicleSpawnRequest", on_spawn)
node.on("playerJoined", greet)
# playerLeft есть и как уведомление с одним id, и как событие с тремя полями.
# node.on выбирает второй вариант, поэтому здесь явный канал.
node.on_builtin("playerLeft", on_leave)
clock = node.every(60000, on_minute)

for player_id in node.get_players():
    greet(player_id)

node.log_info("python-gate loaded")
print("\nBeamMP Сосут хуй!")

def onload():
    print("\ntest")

def unload():
    node.cancel(clock)
    node.log_info("python-gate unloaded")
