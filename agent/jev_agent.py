#!/usr/bin/env python3
"""JEV-driven Minecraft player for the mc-remote mod.

Loop: read compact world state -> generate candidate actions -> ask the JEV
decision model (OpenRouter Decisions API, typesafe/jev-*) which action to take
-> execute it via the mod's HTTP API -> repeat until JEV says the task is done.

The agent never generates free-form text; every step is a calibrated `choice`
(plus a `noul` completion check) answered by JEV, and the mod executes the
picked action — actions are literally chosen from JEV's probabilities.

Env vars:
    OPENROUTER_API_KEY   (required) OpenRouter key
    JEV_MODEL            default "typesafe/jev-1.13" (or "~typesafe/jev-latest")
    MCREMOTE_URL         default http://127.0.0.1:25587
    MCREMOTE_TOKEN       auth token; auto-read from <mc dir>/config/mcremote.json
    MAX_STEPS            default 150

Usage: python3 jev_agent.py "Chop a bamboo block and pick it up"
"""
import json
import math
import os
import sys
import time
import urllib.request
import urllib.error

OR_URL = "https://openrouter.ai/api/alpha/decisions"
MODEL = os.environ.get("JEV_MODEL", "typesafe/jev-1.13")
BASE = os.environ.get("MCREMOTE_URL", "http://127.0.0.1:25587").rstrip("/")
MAX_STEPS = int(os.environ.get("MAX_STEPS", "150"))
DONE_THRESH = 0.85

TOKEN = os.environ.get("MCREMOTE_TOKEN")
if not TOKEN:
    for p in (os.path.join(os.path.dirname(__file__), "..", "run", "config", "mcremote.json"),
              os.path.expanduser("~/.minecraft/config/mcremote.json")):
        if os.path.exists(p):
            with open(p) as f:
                TOKEN = json.load(f).get("token")
            if TOKEN:
                break
assert TOKEN, "no MCREMOTE_TOKEN and no config file found"
assert os.environ.get("OPENROUTER_API_KEY"), "OPENROUTER_API_KEY required"

INTERACTABLE = ("crafting_table", "furnace", "chest", "door", "bed", "lever",
                "button", "anvil", "enchanting", "loom", "smoker", "blast_furnace",
                "cartography", "grindstone", "smithing", "barrel")
CRAFTABLES = [
    "oak_planks", "spruce_planks", "birch_planks", "jungle_planks", "stick",
    "crafting_table", "wooden_pickaxe", "wooden_axe", "wooden_shovel", "wooden_sword",
    "stone_pickaxe", "stone_axe", "stone_shovel", "stone_sword", "furnace",
    "iron_pickaxe", "iron_axe", "iron_sword", "bucket", "torch", "chest",
    "ladder", "shield", "shears", "boat", "oak_boat", "campfire", "diamond_pickaxe",
]


def feasible_crafts(inv, table_open):
    """Recipes actually craftable right now given inventory counts and grid size.
    inv: {base_name: count}; table_open: a 3x3 crafting menu is open."""
    n = lambda pred: sum(c for k, c in inv.items() if pred(k))
    pl = n(lambda k: k.endswith("_planks"))
    log = n(lambda k: k.endswith(("_log", "_stem", "_hyphae")))
    cob = n(lambda k: k in ("cobblestone", "cobbled_deepslate", "blackstone"))
    coal = n(lambda k: k in ("coal", "charcoal"))
    iron = inv.get("iron_ingot", 0)
    dia = inv.get("diamond", 0)
    st_ = inv.get("stick", 0)
    ok = []

    def add(name, table, *reqs):
        if table and not table_open:
            return
        if all(c >= m for c, m in reqs):
            ok.append(name)

    for p in CRAFTABLES:
        if p.endswith("_planks"):
            wood = p[:-7]  # oak_planks -> oak_log
            add(p, False, (inv.get(wood + "_log", 0), 1))
    add("stick", False, (pl, 2))
    add("crafting_table", False, (pl, 4))
    add("wooden_pickaxe", True, (pl, 3), (st_, 2))
    add("wooden_axe", True, (pl, 3), (st_, 2))
    add("wooden_shovel", True, (pl, 1), (st_, 2))
    add("wooden_sword", True, (pl, 2), (st_, 1))
    add("stone_pickaxe", True, (cob, 3), (st_, 2))
    add("stone_axe", True, (cob, 3), (st_, 2))
    add("stone_shovel", True, (cob, 1), (st_, 2))
    add("stone_sword", True, (cob, 2), (st_, 1))
    add("furnace", True, (cob, 8))
    add("iron_pickaxe", True, (iron, 3), (st_, 2))
    add("iron_axe", True, (iron, 3), (st_, 2))
    add("iron_sword", True, (iron, 2), (st_, 1))
    add("bucket", True, (iron, 3))
    add("torch", False, (coal, 1), (st_, 1))
    add("chest", True, (pl, 8))
    add("ladder", True, (st_, 7))
    add("shield", True, (pl, 6), (iron, 1))
    add("shears", True, (iron, 2))
    add("boat", True, (pl, 5))
    add("oak_boat", True, (pl, 5))
    add("campfire", True, (st_, 3), (log, 3), (coal, 1))
    add("diamond_pickaxe", True, (dia, 3), (st_, 2))
    return [i for i in CRAFTABLES if i in ok]


def req(method, url, body=None, headers=None, timeout=40):
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        return {"ok": False, "http_error": e.code,
                "body": e.read()[:300].decode("utf-8", "replace")}


def api(path):
    return req("GET", BASE + path, headers={"Authorization": "Bearer " + TOKEN})


def action(payload, wait=25):
    return req("POST", BASE + "/v1/action?wait=" + str(wait),
               body=payload, headers={"Authorization": "Bearer " + TOKEN})


def jev(state_obj, questions):
    r = req("POST", OR_URL, timeout=60, body={
        "model": MODEL,
        "state": state_obj,
        "questions": questions,
    }, headers={"Authorization": "Bearer " + os.environ["OPENROUTER_API_KEY"],
                "X-OpenRouter-Title": "mc-remote-jev-agent"})
    return r.get("answers", r)


def dist2d(ax, az, bx, bz):
    return round(math.hypot(ax - bx, az - bz), 1)


def base_name(s):
    return s.split(":")[-1].split("[")[0]


def pos_of(st):
    p = st.get("player", {})
    return p.get("x", 0), p.get("y", 0), p.get("z", 0)


def summarize(st, history, task):
    p = st.get("player", {})
    px, py, pz = pos_of(st)
    items = st.get("inventory", {}).get("items", [])
    inv = {}
    hotbar = {}
    for it in items:
        n = base_name(it.get("id", ""))
        inv[n] = inv.get(n, 0) + it.get("count", 1)
        if 0 <= it.get("slot", -1) <= 8:
            hotbar[it["slot"]] = f"{n}x{it.get('count', 1)}"
    ents = []
    for e in st.get("entities", []) or []:
        et = base_name(e.get("type", ""))
        ent = {"type": et, "pos": [round(e["x"]), round(e["y"]), round(e["z"])],
               "d": round(e.get("distance", 0), 1)}
        if e.get("item"):
            ent["item"] = base_name(e["item"])
        if e.get("hostile"):
            ent["hostile"] = True
        ents.append(ent)
    ents.sort(key=lambda e: e["d"])
    notable = sorted(
        ({"block": base_name(b["block"]), "pos": [b["x"], b["y"], b["z"]],
          "d": round(math.dist((px, py, pz), (b["x"], b["y"], b["z"])), 1)}
         for b in st.get("blocks", {}).get("notable", [])),
        key=lambda b: b["d"])[:16]
    la = st.get("looking_at")
    looking = None
    if la:
        looking = {"type": la.get("type"),
                   "what": base_name(la.get("block") or la.get("entity_type") or ""),
                   "pos": [la.get("x"), la.get("y"), la.get("z")]}
    w = st.get("world", {})
    sel = p.get("selected_slot", 0)
    return {
        "task": task,
        "pos": [round(px, 1), round(py, 1), round(pz, 1)],
        "health": p.get("health"), "food": p.get("food"),
        "gamemode": p.get("gamemode"), "on_ground": p.get("on_ground"),
        "alive": p.get("alive"), "sky_above": p.get("sky_above"),
        "biome": w.get("biome"), "day_time": w.get("day_time"),
        "hotbar": hotbar, "selected_slot": sel,
        "held": hotbar.get(sel),
        "inventory_counts": inv,
        "looking_at": looking,
        "open_container": (st.get("open_container") or {}).get("title"),
        "spawn": (st.get("world") or {}).get("spawn"),
        "nearest_entities": ents[:10],
        "notable_blocks": notable,
        "block_palette": [base_name(n) for n in
                          (st.get("blocks") or {}).get("palette", [])][:40],
        "recent_actions": history[-10:],
    }


def block_at(st, x, y, z):
    """Decode the palette grid to get the block name at a world position."""
    b = st.get("blocks") or {}
    try:
        ox, oy, oz = b["origin_x"], b["origin_y"], b["origin_z"]
        sx, sy, sz = b["size_x"], b["size_y"], b["size_z"]
        lx, ly, lz = x - ox, y - oy, z - oz
        if not (0 <= lx < sx and 0 <= ly < sy and 0 <= lz < sz):
            return None
        return b["palette"][b["data"][(ly * sz + lz) * sx + lx]]
    except Exception:
        return None


NEEDS_PICKAXE = ("stone", "deepslate", "andesite", "granite", "diorite",
                 "tuff", "ore", "cobblestone", "obsidian", "netherrack",
                 "sandstone", "calcite", "iron_block", "gold_block", "diamond_block")


def candidates(st, home=None):
    """Generate candidate actions from live state -> {key: (desc, payload)}."""
    px, py, pz = pos_of(st)
    yaw = math.radians(st.get("player", {}).get("yaw", 0))
    out = {"done": ("Declare the task complete.", None)}

    inv_names = {base_name(i.get("id", "")) for i in
                 st.get("inventory", {}).get("items", [])}
    has_pick = any("pickaxe" in n for n in inv_names)
    pick_warn = "" if has_pick else " (WARNING: no pickaxe held — drops nothing without one)"

    notable = sorted(
        (b for b in st.get("blocks", {}).get("notable", [])),
        key=lambda b: math.dist((px, py, pz), (b["x"], b["y"], b["z"])))
    seen, mine_n = set(), 0
    for b in notable[:30]:
        name, bx, by, bz = base_name(b["block"]), b["x"], b["y"], b["z"]
        d = math.dist((px, py, pz), (bx + 0.5, by + 0.5, bz + 0.5))
        if name in seen and d > 6:
            continue
        seen.add(name)
        if d <= 5.2:
            if mine_n < 4:
                warn = pick_warn if any(k in name for k in NEEDS_PICKAXE) else ""
                out[f"mine {name} @{bx},{by},{bz}"] = (
                    f"Mine the {name} at ({bx},{by},{bz}), {d:.0f} blocks away." + warn,
                    {"type": "mine", "x": bx, "y": by, "z": bz})
                mine_n += 1
        elif d <= 45:
            out[f"goto {name} @{bx},{by},{bz}"] = (
                f"Walk toward {name} at ({bx},{by},{bz}), {d:.0f} blocks away.",
                {"type": "walk_to", "x": bx, "z": bz})
        if len(out) > 15:
            break

    for e in sorted(st.get("entities", []) or [], key=lambda e: e.get("distance", 99))[:4]:
        et = base_name(e.get("type", ""))
        ex, ey, ez = round(e["x"]), round(e["y"]), round(e["z"])
        d = e.get("distance", 99)
        if et == "item":
            what = base_name(e.get("item", "item"))
            out[f"pickup {what}"] = (
                f"Walk to the dropped {what} at ({ex},{ez}), {d:.0f} blocks away.",
                {"type": "walk_to", "x": ex, "z": ez, "radius": 0.8,
                 "_pickup": what, "_ey": ey})
        elif d <= 4.5:
            out[f"attack {et}"] = (
                f"Attack the {et} ({d:.0f} blocks away).",
                {"type": "attack", "entity": e.get("id")})

    # interactable blocks in reach (use_on_block aims automatically);
    # suppressed while a container is already open to avoid open/close thrash
    if not st.get("open_container"):
        for b in notable[:30]:
            n = base_name(b["block"])
            if any(k in n for k in INTERACTABLE):
                d = math.dist((px, py, pz), (b["x"] + 0.5, b["y"] + 0.5, b["z"] + 0.5))
                if d <= 5.0:
                    out[f"use {n} @{b['x']},{b['y']},{b['z']}"] = (
                        f"Open/interact with the {n} at ({b['x']},{b['y']},{b['z']}), {d:.0f} blocks away.",
                        {"type": "use_on_block", "x": b["x"], "y": b["y"], "z": b["z"]})

    # place the held block onto the ground ahead of the player
    held = (st.get("inventory", {}).get("items") or [])
    sel = st.get("player", {}).get("selected_slot", 0)
    held_item = next((base_name(i["id"]) for i in held if i.get("slot") == sel), None)
    if held_item and any(k in held_item for k in
            ("table", "furnace", "chest", "torch", "block", "planks", "log",
             "ice", "dirt", "stone", "bed", "ladder", "boat", "sapling")):
        gx, gz = math.floor(px - math.sin(yaw) * 1.6), math.floor(pz + math.cos(yaw) * 1.6)
        gy = math.floor(py) - 1
        gb = base_name(block_at(st, gx, gy, gz) or "")
        gab = base_name(block_at(st, gx, gy + 1, gz) or "")
        # the cell above must be air or the ray clips the cover (leaf litter etc)
        if gb and gb != "air" and gab == "air":
            out[f"place {held_item}"] = (
                f"Place the held {held_item} on top of the {gb} at ({gx},{gy},{gz}) "
                f"in front of you.",
                {"type": "use_on_block", "x": gx, "y": gy, "z": gz, "face": "up"})
        # also try the ground blocks adjacent to the player's feet
        fx, fy, fz = math.floor(px), math.floor(py) - 1, math.floor(pz)
        for nx, nz in ((fx + 1, fz), (fx - 1, fz), (fx, fz + 1), (fx, fz - 1)):
            nb = base_name(block_at(st, nx, fy, nz) or "")
            above = base_name(block_at(st, nx, fy + 1, nz) or "")
            if nb not in ("", "air", "water") and above == "air":
                out[f"place {held_item} beside you"] = (
                    f"Place the held {held_item} on the {nb} at ({nx},{fy},{nz}) "
                    f"next to you.",
                    {"type": "use_on_block", "x": nx, "y": fy, "z": nz,
                     "face": "up"})
                break

    for label, dx, dz in (("north", 0, -32), ("south", 0, 32),
                          ("east", 32, 0), ("west", -32, 0)):
        out[f"walk {label}"] = (f"Walk ~32 blocks {label} to explore.",
                                {"type": "walk_to", "x": round(px + dx),
                                 "z": round(pz + dz)})
    # digging: block ahead at feet level, and the block under feet
    ax, az = math.floor(px - math.sin(yaw) * 1.6), math.floor(pz + math.cos(yaw) * 1.6)
    fy = math.floor(py)
    n_ahead = base_name(block_at(st, ax, fy, az) or "block")
    if n_ahead != "air":
        out[f"mine {n_ahead} ahead @{ax},{fy},{az}"] = (
            f"Mine the {n_ahead} straight ahead at feet level ({ax},{fy},{az}).",
            {"type": "mine", "x": ax, "y": fy, "z": az})
    n_up = base_name(block_at(st, ax, fy + 1, az) or "block")
    if n_up not in ("air", "water") and n_ahead != "air":
        warn = pick_warn if any(k in n_up for k in NEEDS_PICKAXE) else ""
        out[f"mine {n_up} above-ahead @{ax},{fy + 1},{az}"] = (
            f"Mine the {n_up} ahead at head level ({ax},{fy + 1},{az}) — "
            f"clearing feet+head ahead digs an upward staircase." + warn,
            {"type": "mine", "x": ax, "y": fy + 1, "z": az})
    bx, by, bz = math.floor(px), fy - 1, math.floor(pz)
    n_below = base_name(block_at(st, bx, by, bz) or "block")
    if n_below not in ("air", "water"):
        warn = pick_warn if any(k in n_below for k in NEEDS_PICKAXE) else ""
        out[f"mine {n_below} below @{bx},{by},{bz}"] = (
            f"Mine the {n_below} under your feet ({bx},{by},{bz})." + warn,
            {"type": "mine", "x": bx, "y": by, "z": bz})
    if home:
        hx, hy, hz = home
        buried = not st.get("player", {}).get("sky_above", True)
        if buried or dist2d(px, pz, hx, hz) > 60:
            out["return to surface"] = (
                f"Pathfind back to the open surface at ({hx:.0f},{hy:.0f},{hz:.0f}) "
                f"(you are at y={py:.0f}, {dist2d(px, pz, hx, hz):.0f} blocks away).",
                {"type": "walk_to", "x": hx, "y": hy, "z": hz, "radius": 2.5})

    for it in st.get("inventory", {}).get("items", []):
        n = base_name(it.get("id", ""))
        if 0 <= it.get("slot", -1) <= 8 and any(k in n for k in
                ("pickaxe", "_axe", "shovel", "sword", "hoe", "torch", "boat",
                 "table", "furnace", "chest", "block", "planks", "log")):
            out[f"select {n}"] = (
                f"Select {n} in hotbar slot {it['slot']} (right tool speeds up matching blocks).",
                {"type": "select_slot", "slot": it["slot"]})
    if st.get("player", {}).get("in_water") or st.get("player", {}).get("swimming"):
        out["swim up"] = ("Swim upward toward the surface while drifting forward.",
                          {"type": "move", "forward": 0.4, "jump": True, "seconds": 3})
    # only offer crafting for recipes whose ingredients are actually in inventory
    inv_counts = {}
    for i in st.get("inventory", {}).get("items", []):
        bn = base_name(i.get("id", ""))
        inv_counts[bn] = inv_counts.get(bn, 0) + i.get("count", 1)
    oc = st.get("open_container") or {}
    grid3 = "3x3" in str(oc.get("crafting_grid", ""))
    feas = feasible_crafts(inv_counts, table_open=grid3)
    if feas:
        out["craft"] = (
            f"Craft one of: {', '.join(feas)} (all have ingredients ready"
            + ("" if grid3 else "; recipes needing 3x3 are hidden until a "
               "crafting table is open") + ").",
            {"type": "craft", "ask": True, "items": feas})
    if st.get("open_container"):
        out["close screen"] = ("Close the currently open screen/container.",
                               {"type": "close_screen"})
    out["wait"] = ("Wait one second.", {"type": "wait", "seconds": 1})
    return out


def main():
    task = sys.argv[1] if len(sys.argv) > 1 else "Collect a log of wood."
    print(f"[jev-agent] model={MODEL} task={task!r}", flush=True)
    history = []

    home = None
    for step in range(1, MAX_STEPS + 1):
        st = api("/v1/state?blocks=32")
        pl = st.get("player", {})
        if st.get("in_world") and pl.get("alive"):
            if pl.get("sky_above"):
                # remember the last outdoor position — the way back to the surface
                home = pos_of(st)
            elif home is None and pl.get("surface_y"):
                # started underground: aim for open ground above this column
                home = (pl.get("x", 0), pl["surface_y"], pl.get("z", 0))
        if not st.get("in_world"):
            why = st.get("error") or st.get("http_error") or st.get("screen") or st
            print(f"[{step}] not in world ({why}), waiting", flush=True)
            time.sleep(2)
            continue
        if st.get("player", {}).get("alive") is False:
            print(f"[{step}] dead — respawning", flush=True)
            action({"type": "respawn"}, wait=10)
            history.append({"act": "respawn", "status": "done", "ok": True})
            continue
        summ = summarize(st, history, task)
        cands = candidates(st, home)
        # suppress candidates that already failed recently (stop retry loops)
        recent_fails = [h["act"] for h in history[-6:] if not h.get("ok")]
        cands = {k: v for k, v in cands.items()
                 if not any(k == rf or (k.startswith(rf.split(" @")[0] + " @") and rf.startswith(k.split(" @")[0]))
                            for rf in recent_fails)} or cands
        # a select changes nothing but the held slot — don't allow two in a row,
        # and never offer re-selecting the item already held (JEV ping-pong fix)
        if history and history[-1].get("act", "").startswith("select "):
            cands = {k: v for k, v in cands.items()
                     if not k.startswith("select ")} or cands
        held0 = (summ.get("held") or "").rsplit("x", 1)[0]
        if held0:
            cands = {k: v for k, v in cands.items()
                     if k != "select " + held0} or cands
        ans = jev(summ, {
            "act": {"type": "choice",
                    "instructions": "Pick the single best next action to progress the task. "
                                    "Avoid repeating an action that just failed; chain the steps "
                                    "needed (gather -> craft -> use).",
                    "criteria": {k: v[0] for k, v in cands.items()}},
            "done": {"type": "noul",
                     "instructions": "Is the task fully complete given the inventory and world state?",
                     "true": "Task complete", "false": "More steps needed"},
        })
        done_p = ans.get("done", {}).get("noul", 0)
        pick = ans.get("act", {}).get("choice")
        probs = ans.get("act", {}).get("probabilities", {})
        print(f"[{step}] done={done_p:.2f} conf={ans.get('act', {}).get('confidence')} "
              f"pick={pick} top={sorted(probs.items(), key=lambda x: -x[1])[:3]}", flush=True)

        if pick == "done":
            if done_p >= DONE_THRESH:
                print(f"[jev-agent] task complete (done_p={done_p:.2f})", flush=True)
                print("inventory:", json.dumps(summ["inventory_counts"]))
                return
            # JEV's two answers disagree — record the premature 'done' as a
            # failure so the next step suppresses it and keeps working
            history.append({"act": "done", "status": "failed", "ok": False,
                            "error": f"declared done but done_p={done_p:.2f}"})
            print(f"      -> ignored premature done (done_p={done_p:.2f})", flush=True)
            continue

        desc, payload = cands.get(pick, (None, None))
        if payload is None:
            payload = {"type": "wait", "seconds": 1}
        if payload.get("type") == "craft" and payload.get("ask"):
            items = payload.get("items") or CRAFTABLES
            ans2 = jev(summ, {"item": {"type": "choice",
                       "instructions": "Which item should be crafted right now to advance the task?",
                       "criteria": {n: f"craft {n}" for n in items}}})
            item = ans2.get("item", {}).get("choice")
            if not item:
                history.append({"act": "craft", "result": "no item chosen"})
                continue
            payload = {"type": "craft", "item": item}
            pick = f"craft {item}"

        # pickups claim success when the walk finishes — verify the item count grew
        before_ct = None
        if payload.get("_pickup"):
            inv0 = api("/v1/state").get("inventory", {}).get("items", [])
            before_ct = sum(i.get("count", 1) for i in inv0
                            if base_name(i.get("id", "")) == payload["_pickup"])
        res = action(payload, wait=25)
        # if still queued/running (queue busy), poll to a terminal state
        aid = res.get("action_id") or res.get("id")
        for _ in range(30):
            if res.get("status") not in ("queued", "running"):
                break
            time.sleep(1)
            res = api("/v1/action?id=" + str(aid))
        if before_ct is not None and res.get("status") == "done":
            inv1 = api("/v1/state").get("inventory", {}).get("items", [])
            after_ct = sum(i.get("count", 1) for i in inv1
                           if base_name(i.get("id", "")) == payload["_pickup"])
            if after_ct <= before_ct:
                res = {"status": "failed",
                       "error": f"walked to drop but no {payload['_pickup']} collected"}
        ok = bool(res.get("ok")) and res.get("status") not in ("failed", "cancelled")
        # a bare-handed pickaxe-block mine "succeeds" but drops nothing — say so
        has_pick = any("pickaxe" in k for k in summ["inventory_counts"])
        if ok and payload.get("type") == "mine" and not has_pick and pick and (
                any(k in pick.split(" @")[0] for k in NEEDS_PICKAXE)):
            ok = False
            res["status"] = "failed"
            res["error"] = "broke it bare-handed — no drop without a pickaxe"
        note = {"act": pick, "status": res.get("status"), "ok": ok}
        if res.get("message"):
            note["msg"] = res["message"]
        if res.get("error"):
            note["error"] = res["error"]
        history.append(note)
        print(f"      -> {res.get('status')} {res.get('message', '')} {res.get('error', '')}", flush=True)

    print(f"[jev-agent] hit MAX_STEPS={MAX_STEPS}", flush=True)


if __name__ == "__main__":
    main()
