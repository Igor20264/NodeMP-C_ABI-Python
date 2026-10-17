def on_join(player_id):
    name = node.get_player_name(player_id) or "?"
    node.log_info(f"hello, {name}")


def crunch(n):
    return n * (n + 1) // 2


def show(result, error):
    if error:
        node.log_error(error)
    else:
        node.log_info(f"sum = {result} on core {node.core_info()['index']}")


node.on("playerJoined", on_join)
node.job(crunch, 1000, done=show)


def unload():
    node.log_info(f"{node.resource_name} unloaded")
