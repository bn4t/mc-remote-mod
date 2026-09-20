# mc-remote-mod

Client-side remote control for Minecraft Java Edition 26.2 (Fabric).

The mod runs an HTTP JSON API and an MCP (streamable HTTP) endpoint inside the
game client. An external agent — an LLM tool caller, a bot script, a Devin
session — can observe exactly what the client received from the server (blocks,
entities, player state, chat) and drive the player with real client inputs.

## Building

```
./gradlew build   # produces build/libs/mc-remote-mod-<version>.jar
```

Requires Java 25 (auto-provisioned via the Gradle toolchain). Drop the jar into
`.minecraft/mods` together with Fabric Loader ≥0.19.5 and Fabric API, targeting
Minecraft 26.2.

For development: `./gradlew runClient` launches a dev client with the mod
enabled (no Mojang account needed). `./gradlew runClient -PjoinWorld="<name>"`
auto-joins the named singleplayer world via quick play.

## Configuration

On first launch the mod writes `.minecraft/config/mcremote.json`:

```json
{
  "bind": "127.0.0.1",
  "port": 25587,
  "token": "<random uuid>",
  "max_block_radius": 24
}
```

- `token` — bearer token required on all endpoints except `/v1/health`.
  Keep the default `127.0.0.1` bind unless you know what you're doing; to let a
  remote agent in, prefer an SSH tunnel or Tailscale over `bind: "0.0.0.0"`.
- `max_block_radius` — caps the block-grid radius an agent can request.

## HTTP API

All endpoints under `http://<bind>:<port>`. Auth: `Authorization: Bearer <token>`.

| Route | Description |
|---|---|
| `GET /v1/health` | liveness (no auth) |
| `GET /v1/state?blocks=<r>&below=<b>&above=<a>` | full snapshot; `blocks=r` includes the block grid |
| `GET /v1/blocks?radius=&below=&above=` | block grid only |
| `GET /v1/events?since=<seq>&limit=<n>` | chat/system/death/join events, seq-cursor |
| `POST /v1/action` | enqueue an action; `?wait=<sec>` blocks until done/timeout |
| `GET /v1/action?id=<n>` | action status |
| `GET /v1/actions` | queued count + recent action results |
| `POST /v1/stop` | release all inputs, cancel queue |

### State snapshot

```jsonc
{
  "in_world": true,
  "player": {"x": ..., "y": ..., "z": ..., "yaw": ..., "pitch": ...,
             "health": ..., "food": ..., "xp_level": ..., "gamemode": ...,
             "on_ground": ..., "selected_slot": 0, "effects": [...],
             "surface_y": 70,     // heightmap top of the player's column
             "sky_above": true},  // player at/above surface_y (roughly "outdoors")
  "world":  {"dimension": "minecraft:overworld", "day_time": ..., "biome": ...,
             "raining": false, "difficulty": "normal", "server": "singleplayer",
             "spawn": {"x":..,"y":..,"z":..}},
  "inventory": {"items": [{"slot":0,"id":"minecraft:iron_pickaxe","count":1,"name":"..."}]},
  "entities": [{"id":12,"type":"minecraft:zombie","x":...,"distance":4.2,
                "health":20,"hostile":true}],
  "looking_at": {"type":"block","x":..,"y":..,"z":..,"face":"up","block":"minecraft:stone"},
  "blocks": {"origin_x":..,"origin_y":..,"origin_z":..,
             "size_x":..,"size_y":..,"size_z":..,
             "palette":["minecraft:air","minecraft:stone", ...],
             "data":[0,0,1,...],              // flat y,z,x order; idx=(y*sizeZ+z)*sizeX+x
             "notable":[{"x":..,"y":..,"z":..,"block":"minecraft:chest"}, ...]},
  "open_container": {"container_id":1,"title":"Chest","slots":[...],
                     "crafting_grid":"3x3"},  // present for crafting menus (2x2 inventory / 3x3 table)
  "latest_event_seq": 42
}
```

### Actions (`POST /v1/action`)

```jsonc
{"type":"look",   "yaw":90, "pitch":-10}        // yaw 0=S 90=W 180=N -90=E; pitch -90=up 90=down
{"type":"move",   "forward":1,"strafe":0,"seconds":3,"sprint":true,"jump":false,"yaw":90}
{"type":"walk_to","x":-635,"z":744,"radius":1.5,"seconds":60} // A* pathfind then steer;
                                   // optional "y": target height; fails "stuck"/"timeout"
{"type":"jump"}
{"type":"attack", "entity_id":12,"times":3}     // or omit entity_id to swing at crosshair
{"type":"use",    "seconds":0.35}               // hold right click (eat/place/interact)
{"type":"interact_entity","entity_id":12}
{"type":"mine",   "x":-650,"y":76,"z":746,"seconds":30}
{"type":"use_on_block","x":-650,"y":76,"z":746,"face":"up"} // right-click that block face
{"type":"select_slot","slot":0}
{"type":"drop",   "all":false}
{"type":"swap_hands"}
{"type":"click_slot","slot":10,"button":0,"click":"pickup"} // pickup|quick_move|swap|throw|clone|pickup_all
{"type":"craft", "item":"wooden_pickaxe","all":true} // recipe-book craft; uses the open 2x2/3x3 grid
{"type":"open_inventory"}                              // opens the inventory screen (2x2 craft grid)
{"type":"close_screen"}
{"type":"say",    "message":"hello"}
{"type":"respawn"}
{"type":"wait",   "seconds":1}
{"type":"stop"}
```

Actions are serialized: one runs per tick, in order. Single-tick actions
(`look`, `say`, `select_slot`, `drop`, `swap_hands`, `respawn`,
`open_inventory`, `close_screen`, `click_slot`, `interact_entity`,
`use_on_block`) bypass the queue and run immediately, so a long-held `move`
can't starve them. Every action returns
`{"id":N,"type":...,"status":"queued|running|done|failed|cancelled",...}`; `?wait=`
makes the HTTP call block until terminal status or timeout.

## MCP endpoint

`POST /mcp` implements streamable-HTTP MCP (`initialize`, `tools/list`,
`tools/call`, `ping`; `GET /mcp` serves SSE heartbeats). Every action above is a
tool plus `get_state`, `get_blocks`, `get_events`, `get_action`, `stop`.
`tools/call` waits up to `timeout_seconds` (default 25) for the action to
finish and returns its final status as JSON text.

Point an MCP client at `http://<bind>:<port>/mcp` with bearer auth, e.g. a
Devin session MCP config:

```json
{
  "mcremote": {
    "url": "https://<tunnel-to-host>:25587/mcp",
    "headers": {"Authorization": "Bearer <token>"}
  }
}
```

## JEV agent

`agent/jev_agent.py` is a stdlib-only driver that plays the game through
[JEV](https://openrouter.ai/typesafe/jev-1.13) (OpenRouter Decisions API):
each step it summarizes live state, generates candidate actions, asks JEV for a
probability distribution over them, and executes the argmax. `craft` choices
go through a second JEV question restricted to recipes whose ingredients are
actually in inventory (`crafting_grid` tells it whether a 3x3 table is open).

```
OPENROUTER_API_KEY=... MCREMOTE_TOKEN=<token> \
  python3 agent/jev_agent.py "Craft a wooden pickaxe"
```

`MAX_STEPS` (default 150) bounds the loop; the agent prints each decision with
its probability and confidence.

## Design notes

- Everything an agent sees is data the server already sent the client — no
  server-side cheats, the mod just exposes the client's own view.
- Movement/mining/use go through the vanilla input path (`Options` key
  mappings) or `MultiPlayerGameMode`, so anticheat-visible behavior matches a
  human at the keyboard.
- `walk_to` runs A* over the voxel grid (walk, +1 jumps, drops ≤4, headroom
  and dead-end-pit checks, ≤64-block range, ~12k expansions). When no full
  route exists it walks toward the closest reachable node and falls back to
  straight-line steering; repeated failures report `stuck`.
- There is deliberately no `command`/chat-execution action: an agent playing
  the game can't `/give` or teleport its way out of a task.
