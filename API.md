# Функции `node`

Источник имён — `sdk/node.h`, ABI 2.8. Сервер NodeMP 1.12.0 говорит на ABI 2.7.
Предупреждение `NEWER SDK (2.8 vs 2.7)` в логе значит именно это: хост новее сервера.
Функция из раздела «нет на сервере 1.12.0» при вызове даёт ошибку, сервер от этого не падает.

Живой список в скрипте ресурса:

```python
names = sorted(name for name in dir(node) if not name.startswith('_'))
node.log_info(' '.join(names))
```

`dir(node)` показывает имена, которые объект реально имеет. Описание аргументов каждой функции NodeApi — комментарий над ней в `sdk/node.h`.

## Методы хоста

| Имя | Аргументы | Возврат | Смысл |
|---|---|---|---|
| `on` | name, fn | Handle | Событие по имени. Запрос, vehicle, builtin или клиентское. |
| `on_client` | name, fn | Handle | Клиентское событие. Аргументы: player_id, data. |
| `on_builtin` | name, fn | Handle | Уведомление с одним id. |
| `on_vehicle` | name, fn | Handle | Событие с player_id, global_id, data. |
| `on_request` | name, fn | Handle | Вердикт. False или строка запрещают. |
| `on_resource` | name, fn | Handle | Событие шины ресурсов. |
| `on_channel` | channel, fn | Handle | Канал модуля. |
| `off` | handle | None | Снять подписку. Для таймера отменяет его. |
| `set_timeout` | ms, fn | Handle | Один вызов. В функцию приходит timer_id. |
| `set_interval` | ms, fn | Handle | Повтор. В функцию приходит timer_id. |
| `after` | ms, fn | Handle | То же, что set_timeout. |
| `every` | ms, fn | Handle | То же, что set_interval. |
| `cancel` | handle | None | Остановить таймер. |
| `job` | fn, *args, done=None | None | fn на закреплённом ядре. done(result, error) на worker. |
| `submit_job` | fn, *args, done=None | Handle | Пул сервера, без привязки к ядру. |
| `core_info` | — | dict | index, processors, reserved, pinned, lane_id. |
| `on_core` | — | bool | Сейчас код на lane. |
| `reserve_core` | — | None | Снова снять ядро lane с остальных потоков. |
| `json` | method, *args | object | None | Вызвать текстовый геттер и разобрать JSON. |
| `world` | — | object | None | Снимок мира. |
| `metrics` | — | object | None | Метрики сервера. |
| `ask` | name, data='', callback=None | list | Спросить шину и дождаться ответов. Только с worker. |
| `http_request` | method, url, callback, headers=None, body=None | Handle | Ответ приходит на worker. |
| `set_log_sink` | fn | None | Handle | None | Сток лога. Вызывается с потока, который пишет строку. |
| `relay_filter` | fn | Handle | Фильтр ретрансляции. Только быстрые данные, без изменения состояния. |
| `get_players` | — | list[int] | Id игроков. |
| `get_vehicles` | — | list[int] | Id машин. |
| `get_player_vehicles` | player_id | list[int] | Машины игрока. |
| `get_player_vehicle` | player_id | (gid, role) | Текущая машина. gid -1, если пешком. |
| `get_vehicles_by_tag` | key, value=None | list[int] | Машины с тегом. |
| `get_vehicles_by_tags` | pairs | list[int] | Машины со всеми парами тегов. |
| `get_vehicle_info` | global_id | dict | None | Сводка по машине. |
| `get_vehicle_transform` | global_id | dict | None | Положение и поворот. |
| `get_vehicle_controls` | global_id | dict | None | Органы управления. |
| `get_player_position` | player_id | tuple | None | (x, y, z). |
| `get_vehicle_config_hash` | global_id | int | None | Хеш конфига. |
| `get_vehicle_damage_blob` | global_id | tuple | None | (bytes, counter). |
| `get_vehicle_lock` | global_id, limit=128 | dict | None | Режим замка и белый список. |
| `set_vehicle_lock` | global_id, mode, whitelist=() | int | Поставить замок. |
| `get_vehicle_occupants` | global_id | list[dict] | Кто сидит. |
| `sha256_hex` | data | str | SHA-256 в hex. |
| `hmac_sha256_hex` | key, data | str | HMAC-SHA256 в hex. |
| `random_bytes` | n | bytes | 1..65536 байт. |
| `emit_client_bytes` | player_id, event, data | int | Событие с двоичным телом. |
| `emit_all_bytes` | event, data | None | То же всем. |
| `send_module` | player_id, channel, data | int | Канал модуля. |
| `resource_name` | — | str | None | Поле. Имя ресурса, чей код сейчас выполняется. |
| `resource_dir` | — | str | None | Поле. Папка ресурса. |
| `trusted` | — | bool | Поле. Ресурс есть в server.toml [resources] trusted. |
| `mojo` | — | module | Поле. Загрузка библиотеки Mojo из доверенного ресурса. |

## NodeApi, которые сервер 1.12.0 отдаёт

Строковые геттеры (в заголовке это пара `buf, buflen`) здесь уже без буфера: они возвращают `str` или `None`.

| Имя | Аргументы | Возврат | В SDK |
|---|---|---|---|
| `log_info` | msg | None | — |
| `log_warn` | msg | None | — |
| `log_error` | msg | None | — |
| `log_tag` | tag, msg | None | — |
| `log_custom` | tag, rgb, msg | None | — |
| `log_raw` | text | None | — |
| `set_console_title` | title | None | — |
| `get_player_count` | — | int | — |
| `get_player_name` | id | str \| None | — |
| `is_player_connected` | id | int | — |
| `get_player_ping` | id | int | — |
| `get_player_current_vehicle` | id | int | — |
| `kick_player` | id, reason | int | — |
| `get_vehicle_count` | — | int | — |
| `get_vehicle_damage_counter` | global_id | int | — |
| `get_vehicle_electrics_json` | global_id | str \| None | — |
| `delete_vehicle` | global_id | int | — |
| `delete_all_vehicles` | — | int | — |
| `set_vehicle_coupler` | global_id, name, open | int | — |
| `seat_player` | player_id, global_id, seat_kind | int | — |
| `unseat_player` | player_id | int | — |
| `swap_players` | player_a, player_b | int | — |
| `resync_vehicle` | global_id, player_id | int | — |
| `resync_player` | player_id | int | — |
| `set_vehicle_tag` | global_id, key, value | int | — |
| `get_vehicle_tag` | global_id, key | str \| None | — |
| `remove_vehicle_tag` | global_id, key | int | — |
| `get_vehicle_tags_json` | global_id | str \| None | — |
| `emit_client` | id, event, data | int | — |
| `emit_all` | event, data | None | — |
| `register_client_event` | event, cb, user | None | — |
| `unregister_client_event` | event, cb, user | None | — |
| `register_builtin_event` | name, cb, user | None | — |
| `unregister_builtin_event` | name, cb, user | None | — |
| `register_cancellable_event` | name, cb, user | None | — |
| `unregister_cancellable_event` | name, cb, user | None | — |
| `register_vehicle_event` | name, cb, user | None | — |
| `unregister_vehicle_event` | name, cb, user | None | — |
| `clear_timer` | timer_id | None | — |
| `get_server_name` | — | str \| None | — |
| `get_map` | — | str \| None | — |
| `get_server_version` | — | str \| None | — |
| `get_port` | — | int | — |
| `get_max_players` | — | int | — |
| `get_max_cars` | — | int | — |
| `get_player_ip` | id | str \| None | — |
| `get_player_port` | id | int | — |
| `set_player_role` | id, role | int | — |
| `get_player_role` | id | str \| None | — |
| `ban_player` | id, reason | int | — |
| `ban_ip` | ip, reason | int | — |
| `unban_ip` | ip | int | — |
| `is_ip_banned` | ip | int | — |
| `get_bans_json` | — | str \| None | 2.5 |
| `get_vehicle_config_json` | global_id | str \| None | — |
| `get_vehicle_couplers_json` | global_id | str \| None | — |
| `get_vehicle_powertrain_json` | global_id | str \| None | — |
| `get_vehicle_engine_json` | global_id | str \| None | — |
| `get_vehicle_controllers_json` | global_id | str \| None | — |
| `get_world_snapshot_json` | — | str \| None | — |
| `spawn_vehicle` | config_json, owner_name | int | — |
| `register_relay_filter` | cb, user | None | — |
| `unregister_relay_filter` | cb, user | None | — |
| `invalidate_relay_cache` | — | None | — |
| `storage_set` | store, key, value_json | int | — |
| `storage_get` | store, key | str \| None | — |
| `storage_delete` | store, key | int | — |
| `get_server_uptime` | — | float | — |
| `get_unix_time` | — | int | — |
| `set_max_cars` | n | int | — |
| `set_max_players` | n | int | — |
| `set_server_name` | name | int | — |
| `emit_resource_event` | name, data | None | — |
| `register_resource_event` | name, cb, user | None | — |
| `unregister_resource_event` | name, cb, user | None | — |
| `get_metrics_json` | — | str \| None | 2.3 |
| `register_module_channel` | channel, cb, user | None | — |
| `unregister_module_channel` | channel, cb, user | None | — |
| `trigger_vehicle` | global_id, call_json | int | — |
| `is_node_grab_enabled` | — | int | — |
| `register_language_host` | host | int | — |
| `unregister_language_host` | type_name | int | — |
| `set_resource_owner` | resource_name | None | — |
| `get_vehicle_json` | global_id | str \| None | 2.2 |
| `get_vehicles_json` | — | str \| None | — |
| `get_players_json` | — | str \| None | — |
| `get_player_endpoint_json` | id | str \| None | — |
| `get_player_position_json` | id | str \| None | — |
| `get_player_vehicle_json` | id | str \| None | — |
| `get_vehicle_damage_json` | global_id | str \| None | — |
| `get_vehicle_occupants_json` | global_id | str \| None | — |
| `reload_resource` | name | int | — |
| `get_vehicle_transform_json` | global_id | str \| None | — |
| `get_vehicle_controls_json` | global_id | str \| None | — |
| `get_vehicle_lock_json` | global_id | str \| None | — |
| `set_vehicle_lock_named` | global_id, mode, whitelist_json | int | — |
| `seat_player_role` | player_id, global_id, role | int | — |
| `set_player_group` | player_id, group | None | — |
| `set_vehicle_group` | global_id, group | None | — |
| `get_player_group` | player_id | int | — |
| `get_vehicle_group` | global_id | int | — |
| `emit_others` | except_player_id, event, data | int | 1.8 |
| `get_resource_manifest_json` | resource | str \| None | 1.8 |
| `get_vehicle_inputs_json` | global_id | str \| None | 1.9 |
| `get_player_inputs_json` | player_id | str \| None | 1.9 |
| `get_vehicle_nodes_json` | global_id | str \| None | 1.9 |
| `get_vehicle_break_groups_json` | global_id | str \| None | 1.9 |
| `get_vehicle_paints_json` | global_id | str \| None | 1.9 |
| `get_vehicle_sync_json` | global_id | str \| None | 1.9 |
| `get_vehicle_transforms_json` | — | str \| None | 1.9 |
| `get_player_focus_json` | player_id | str \| None | — |
| `get_player_head_pose_json` | player_id | str \| None | 1.9 |
| `get_player_camera_json` | player_id | str \| None | 1.12 |
| `get_player_session_json` | player_id | str \| None | 1.9 |
| `ban_account` | account_id, reason | int | 1.10 |
| `unban_account` | account_id | int | 1.10 |
| `is_account_banned` | account_id | int | 1.10 |
| `player_verify` | player_id, level | int | 1.12 |
| `player_set_strict` | player_id, on | int | 1.12 |
| `get_proto_version` | — | int | 2.1 |
| `release_vehicle_lock` | global_id | int | 2.2 |
| `get_player_policy_json` | player_id | str \| None | 2.2 |
| `reset_vehicle` | global_id, transform_json | int | 2.2 |
| `spawn_vehicle_for_player` | player_id, config_json, transform_json | int | — |
| `get_memory_json` | — | str \| None | 2.3 |
| `get_platform_json` | — | str \| None | 2.3 |
| `get_handler_stats_json` | reset | str \| None | 2.3 |
| `get_vehicle_transforms_select_json` | select | str \| None | 2.4 |
| `ban_player_until` | id, reason, until_unix | int | 2.5 |
| `ban_ip_until` | ip, reason, until_unix | int | — |
| `ban_account_until` | account_id, reason, until_unix | int | — |
| `shutdown_server` | reason, delay_ms | int | 2.5 |
| `teleport_vehicle` | global_id, transform_json | int | 2.5 |
| `freeze_player` | player_id, on | int | 2.5 |
| `call_resource_event` | name, data, cb, user | int | 2.6 |
| `is_resource_trusted` | name | int | 2.7 |

## Нет на сервере 1.12.0

Эти имена объявлены в SDK 2.8. Сервер 1.12.0 их не содержит.

| Имя | Аргументы | Возврат | В SDK |
|---|---|---|---|
| `set_vehicle_queue` | player_id, mode | int | 2.8 |
| `set_vehicle_queue_max_delay` | seconds | int | 2.8 |
| `apply_scheduled_vehicles` | player_id | int | 2.8 |
| `get_queue_json` | — | str \| None | 2.8 |
| `queue_move` | account_id, position | int | 2.8 |
| `queue_remove` | account_id, reason | int | 2.8 |
| `queue_admit` | account_id | int | 2.8 |
| `queue_pause` | paused, note | int | 2.8 |
| `get_queue_limits_json` | — | str \| None | 2.8 |
| `set_queue_limits_json` | limits_json | int | 2.8 |
