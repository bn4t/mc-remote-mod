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
import collections
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
# per-stage step budget in milestone mode (env-tunable)
STAGE_STEPS = int(os.environ.get("STAGE_STEPS", "60"))

# full playthrough ladder — JEV answers "done" per stage, not for the
# whole run, so it can't lose the plot over a hundred reactive steps
PLAYTHROUGH_STAGES = [
    "Collect at least 5 logs from trees",
    "Craft a crafting table and a wooden pickaxe",
    "Mine at least 12 cobblestone/stone blocks",
    "Craft a stone pickaxe and a stone sword or stone axe",
    "Craft a furnace",
    "Hunt animals (sheep/cow/pig/chicken) until you have at least 3 raw meat",
    "Smelt the raw meat in the furnace and eat until your food bar is above 15",
    "Find iron ore (caves or dig down) and mine at least 8 raw iron",
    "Smelt the raw iron into iron ingots",
    "Craft an iron pickaxe and an iron sword; craft a bucket if you can",
    "Dig down to deep levels (below y=0) and mine at least 3 diamonds",
    "Craft a diamond pickaxe",
    "Obtain at least 10 obsidian (mine with the diamond pickaxe)",
    "Build a nether portal frame and light it (flint and steel = iron + flint from gravel)",
    "Enter the nether",
    "In the nether, find a nether fortress and collect at least 7 blaze rods from blazes",
    "Return to the overworld and collect at least 12 ender pearls (kill endermen; they spawn at night — or barter with piglins in the nether)",
    "Craft at least 12 eyes of ender (blaze powder + ender pearls)",
    "Throw eyes of ender and follow them until you find the stronghold",
    "In the stronghold, find the end portal and fill any empty frames with eyes of ender",
    "Go through the end portal; destroy the end crystals on the obsidian towers",
    "Kill the ender dragon",
]

def inv_count(inv, *names):
    """Total count across inventory for any of the given (base) item names."""
    n = 0
    for k, v in inv.items():
        bk = k.split(":")[-1]
        if any(bk == nm or bk.endswith(nm) for nm in names):
            n += v
    return n


def stage_done(idx, inv, summ):
    """Deterministic per-stage completion check. JEV's 'done' vote proved
    unreliable (kept answering ~0.25 with the goal already met), so stage
    advancement is gated on measurable state. Returns True/False, or None
    when the stage can't be checked mechanically (falls back to JEV vote)."""
    food = summ.get("food", 0)
    dim = summ.get("dimension", "minecraft:overworld")
    placed = summ.get("placed", [])  # item names placed this run
    checks = {
        0:  inv_count(inv, "_log") >= 5,
        1:  inv.get("wooden_pickaxe", 0) >= 1 and (
            inv.get("crafting_table", 0) >= 1 or "crafting_table" in placed),
        2:  inv_count(inv, "cobblestone", "stone") >= 12
            or inv_count(inv, "cobblestone") >= 8,
        3:  inv.get("stone_pickaxe", 0) >= 1 and inv_count(
            inv, "stone_sword", "stone_axe") >= 1,
        4:  inv.get("furnace", 0) >= 1 or "furnace" in placed,
        5:  inv_count(inv, "raw_porkchop", "raw_beef", "raw_mutton",
                    "raw_chicken", "raw_rabbit") >= 3,
        6:  food > 15,
        7:  inv.get("raw_iron", 0) >= 8,
        8:  inv.get("iron_ingot", 0) >= 5,
        9:  inv.get("iron_pickaxe", 0) >= 1 and inv.get("iron_sword", 0) >= 1,
        10: inv.get("diamond", 0) >= 3,
        11: inv.get("diamond_pickaxe", 0) >= 1,
        12: inv.get("obsidian", 0) >= 10,
        13: inv.get("flint_and_steel", 0) >= 1,   # portal lit itself is JEV-voted
        14: "nether" in dim,
        15: inv.get("blaze_rod", 0) >= 7,
        16: inv.get("ender_pearl", 0) >= 12,
        17: inv.get("ender_eye", 0) >= 12
            or inv.get("eye_of_ender", 0) >= 12,
        18: None, 19: None,          # stronghold/portal frames — JEV votes
        20: "the_end" in dim,
        21: None,                    # dragon dead — JEV votes (boss_bars helps)
    }
    return checks.get(idx)


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
    "diamond_axe", "diamond_sword", "flint_and_steel", "bow", "arrow",
    "iron_helmet", "iron_chestplate", "iron_leggings", "iron_boots",
    "diamond_helmet", "diamond_chestplate", "diamond_leggings", "diamond_boots",
    "white_bed", "fishing_rod",
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
    add("diamond_axe", True, (dia, 3), (st_, 2))
    add("diamond_sword", True, (dia, 2), (st_, 1))
    flint = inv.get("flint", 0)
    string = inv.get("string", 0)
    feather = inv.get("feather", 0)
    wool = n(lambda k: k.endswith("_wool"))
    add("flint_and_steel", True, (iron, 1), (flint, 1))
    add("bow", True, (st_, 3), (string, 3))
    add("arrow", False, (flint, 1), (st_, 1), (feather, 1))
    add("iron_helmet", True, (iron, 5))
    add("iron_chestplate", True, (iron, 8))
    add("iron_leggings", True, (iron, 7))
    add("iron_boots", True, (iron, 4))
    add("diamond_helmet", True, (dia, 5))
    add("diamond_chestplate", True, (dia, 8))
    add("diamond_leggings", True, (dia, 7))
    add("diamond_boots", True, (dia, 4))
    add("white_bed", True, (pl, 3), (wool, 3))
    add("fishing_rod", True, (st_, 3), (string, 2))
    # one-of furniture / utility items; consumables uncapped. Stops the model
    # shredding all logs into five crafting tables.
    caps = {"crafting_table": 1, "furnace": 1, "chest": 2, "white_bed": 1,
            "oak_boat": 1, "boat": 1, "campfire": 1, "shield": 1,
            "bucket": 1, "shears": 1, "flint_and_steel": 1,
            "fishing_rod": 1, "bow": 1,
            "wooden_pickaxe": 2, "wooden_axe": 1, "wooden_shovel": 1,
            "wooden_sword": 2,
            "stone_pickaxe": 2, "stone_axe": 1, "stone_shovel": 1,
            "stone_sword": 2,
            "iron_pickaxe": 2, "iron_axe": 1, "iron_sword": 2,
            "diamond_pickaxe": 2, "diamond_axe": 1, "diamond_sword": 2,
            "iron_helmet": 1, "iron_chestplate": 1, "iron_leggings": 1,
            "iron_boots": 1,
            "diamond_helmet": 1, "diamond_chestplate": 1,
            "diamond_leggings": 1, "diamond_boots": 1}
    # consumables have sane caps too — JEV once turned every log into 256
    # sticks and starved. Planks needed in bulk; keep a reserve of logs.
    caps.update({"stick": 16, "torch": 32, "arrow": 24, "ladder": 8})
    ok = [i for i in ok
          if (i.endswith("_planks") and inv.get(i, 0) < 24)
          or (not i.endswith("_planks") and inv.get(i, 0) < caps.get(i, 2))]
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
    for _ in range(8):
        try:
            return req("GET", BASE + path,
                       headers={"Authorization": "Bearer " + TOKEN})
        except Exception:
            time.sleep(2)
    return {"ok": False, "error": "unreachable"}


def action(payload, wait=25):
    for _ in range(8):
        try:
            return req("POST", BASE + "/v1/action?wait=" + str(wait),
                       body=payload,
                       headers={"Authorization": "Bearer " + TOKEN})
        except Exception:
            time.sleep(2)
    return {"ok": False, "status": "failed", "error": "unreachable"}


_jev_errs = [0]


def jev(state_obj, questions):
    r = req("POST", OR_URL, timeout=60, body={
        "model": MODEL,
        "state": state_obj,
        "questions": questions,
    }, headers={"Authorization": "Bearer " + os.environ["OPENROUTER_API_KEY"],
                "X-OpenRouter-Title": "mc-remote-jev-agent"})
    if not r.get("answers") and _jev_errs[0] < 5:
        _jev_errs[0] += 1
        print(f"[jev-agent] JEV call returned no answers: "
              f"{json.dumps(r)[:300]}", flush=True)
    return r.get("answers", r)


def dist2d(ax, az, bx, bz):
    return round(math.hypot(ax - bx, az - bz), 1)


def base_name(s):
    return s.split(":")[-1].split("[")[0]


def pos_of(st):
    p = st.get("player", {})
    return p.get("x", 0), p.get("y", 0), p.get("z", 0)


# what each inventory item can become / is used for — the dynamic part of
# the manual, keyed to actual item names the model sees in its inventory
ITEM_USES = {
    "_log": "craft into planks (then sticks, crafting_table, tools, chest, "
            "boat); or smelt into charcoal fuel; or use as furnace fuel",
    "_planks": "craft into sticks, crafting_table, tools (pickaxe/axe/sword/"
               "hoe/shovel heads? no — planks are TOOL HEADS), chest, boat, "
               "bed base; weak furnace fuel",
    "stick": "tool handles, torches (with coal/charcoal), ladders, arrows",
    "crafting_table": "place it to unlock the 3x3 crafting grid (tools, "
                      "furnace, armor, everything beyond 2x2)",
    "cobblestone": "craft furnace (8), stone tools, brewing stand, walls; "
                   "smelt to smooth stone",
    "stone": "smelted cobblestone — decorative/stone tools need cobblestone",
    "coal": "torches (with sticks), furnace fuel",
    "charcoal": "torches, furnace fuel (smelted from logs)",
    "raw_iron": "smelt in furnace -> iron_ingot (needs stone+ pickaxe mined)",
    "iron_ingot": "craft iron tools/armor/bucket/shield/flint_and_steel/"
                  "shears; flint_and_steel = iron + flint",
    "raw_copper": "smelt -> copper_ingot",
    "raw_gold": "smelt -> gold_ingot (needs iron+ pickaxe mined)",
    "diamond": "craft diamond tools/armor — pickaxe needed for obsidian",
    "obsidian": "build nether portal frame (10-14), light with "
                "flint_and_steel",
    "flint": "flint_and_steel (with iron) to light portals; arrows",
    "flint_and_steel": "light a nether portal frame of obsidian",
    "gravel": "dig for flint drops",
    "furnace": "place it — smelts ores->ingots, meat->food, sand->glass "
               "(needs fuel)",
    "torch": "place for light (prevents mob spawns, visibility)",
    "ladder": "place on walls to climb",
    "sand": "smelt -> glass",
    "clay_ball": "smelt -> brick",
    "netherrack": "smelt -> nether_brick",
    "potato": "smelt -> baked_potato",
    "kelp": "smelt -> dried_kelp",
    "ancient_debris": "smelt -> netherite_scrap (diamond pickaxe to mine)",
    "ender_pearl": "craft with blaze_powder -> eye_of_ender (kills: throw "
                   "to teleport)",
    "blaze_rod": "craft -> 2 blaze_powder; also strong furnace fuel",
    "blaze_powder": "craft with ender_pearl -> eye_of_ender",
    "eye_of_ender": "throw to locate the stronghold; fills end portal frames",
    "bucket": "carry water/lava; water+lava -> obsidian",
    "wool": "craft bed (3 wool + 3 planks) — sleep to skip night",
    "leather": "craft leather armor",
    "bone": "craft -> bone_meal (instant crop growth)",
    "string": "craft wool (4), bow, fishing_rod",
    "gunpowder": "craft TNT / firework",
    "slime_ball": "craft sticky piston / slime block",
    "rotten_flesh": "edible (hunger risk); smelt nothing",
    "porkchop": "EAT raw (weak) or smelt -> cooked_porkchop (better)",
    "beef": "EAT raw or smelt -> cooked_beef",
    "mutton": "EAT raw or smelt -> cooked_mutton",
    "chicken": "EAT raw or smelt -> cooked_chicken (raw risks hunger)",
    "apple": "EAT",
    "bread": "EAT",
    "carrot": "EAT / farm",
    "cooked_beef": "EAT — restores a lot",
    "cooked_porkchop": "EAT — restores a lot",
    "cooked_mutton": "EAT", "cooked_chicken": "EAT",
    "baked_potato": "EAT",
    "sweet_berries": "EAT (pick from bushes)",
    "melon_slice": "EAT",
    "netherrack_tools": None,
    "dirt": "placeable — tower up, fill holes, block off mobs",
    "gravel2": None,
}

# surrounding-block uses — what the blocks in view can give the model
BLOCK_USES = {
    "_log": "mine -> logs (planks/tools/fuel) — bare hands work",
    "stone": "mine -> cobblestone (needs ANY pickaxe held)",
    "cobblestone": "mine -> cobblestone (any pickaxe)",
    "coal_ore": "mine -> coal (any pickaxe)",
    "iron_ore": "mine -> raw_iron (needs STONE+ pickaxe)",
    "deepslate_iron_ore": "mine -> raw_iron (needs STONE+ pickaxe)",
    "copper_ore": "mine -> raw_copper (STONE+ pickaxe)",
    "gold_ore": "mine -> raw_gold (IRON+ pickaxe)",
    "diamond_ore": "mine -> diamond (IRON+ pickaxe)",
    "deepslate_diamond_ore": "mine -> diamond (IRON+ pickaxe)",
    "lapis_ore": "mine -> lapis (STONE+ pickaxe)",
    "redstone_ore": "mine -> redstone (IRON+ pickaxe)",
    "obsidian": "mine ONLY with diamond pickaxe — portal frames",
    "gravel": "mine -> flint sometimes",
    "sand": "mine (shovel fastest) -> smelt to glass",
    "clay": "mine -> clay_ball -> smelt to brick",
    "netherrack": "mine -> smelt to nether_brick",
    "soul_sand": "slows you; nether wart farms",
    "glowstone": "mine -> glowstone_dust",
    "ancient_debris": "mine ONLY with diamond pickaxe -> smelt to "
                      "netherite_scrap",
    "crafting_table": "use -> 3x3 crafting grid",
    "furnace": "use -> smelting grid (input+fuel)",
    "chest": "use -> storage",
    "water": "swim (jump to rise); drowns you when air runs out",
    "lava": "DEADLY — burns to death; water+lava -> obsidian",
    "melon": "mine -> melon_slice food",
    "pumpkin": "mine -> jack_o_lantern/pumpkin pie",
    "sweet_berry_bush": "use/punch -> sweet_berries",
    "cactus": "mine -> cactus; hurts on touch",
}


def item_uses_for(inv):
    """Map each inventory item to its uses — suffix-match so oak_log,
    spruce_log etc. all hit '_log'."""
    out = {}
    for name in inv:
        for pat, use in ITEM_USES.items():
            if use and (name == pat or (pat.startswith("_")
                                        and name.endswith(pat))):
                out[name] = use
                break
    return out


def block_uses_for(notable):
    """Map each notable nearby block to what it provides."""
    out = {}
    for b in notable:
        n = b.get("block", "")
        for pat, use in BLOCK_USES.items():
            if n == pat or (pat.startswith("_") and n.endswith(pat)) \
                    or n == pat.replace("_ore", "") + "_ore":
                out[n] = use
                break
    return out


GAME_RULES = (
    "Minecraft mechanics: "
    "CRAFTING CHAINS: log -> 4 planks; 2 planks -> 4 sticks; 4 planks -> "
    "crafting_table. Tools (pickaxe/axe/sword/shovel/hoe) = 3 head-material "
    "(planks/cobblestone/iron_ingot/diamond) over 2 sticks at a 3x3 table. "
    "Furnace = 8 cobblestone ring. Torch = coal/charcoal over stick. "
    "Chest = 8 planks. Boat = 5 planks. Bed = 3 wool + 3 planks. "
    "Bucket = 3 iron. Shield = 6 planks + 1 iron. Flint_and_steel = "
    "iron_ingot + flint (flint drops from gravel). "
    "PICKAXE TIERS: stone/coal/ores need any pickaxe; iron ore & lapis need "
    "STONE+; diamonds/gold/redstone/emerald need IRON+; obsidian & ancient "
    "debris need DIAMOND+. Wrong tier = no drop. Bare hands get wood only "
    "slowly, stone-family drops nothing. "
    "SMELTING: furnace = input + fuel (coal/charcoal best; logs/planks/"
    "blaze_rod work). Recipes: raw_iron/iron_ore -> iron_ingot, "
    "raw_copper/copper_ore -> copper_ingot, raw_gold/gold_ore -> "
    "gold_ingot, any log -> charcoal (fuel!), raw meat -> cooked food, "
    "cobblestone -> stone -> smooth_stone, sand -> glass, clay_ball -> "
    "brick, netherrack -> nether_brick, potato -> baked_potato, kelp -> "
    "dried_kelp, ancient_debris -> netherite_scrap. HOW TO SMELT: use the "
    "furnace, then 'furnace load <ore>' (input), 'furnace fuel <item>' "
    "(fuel — one coal smelts ~8 items, a stick barely 1), then 'wait for "
    "smelting' until 'furnace take <ingot>' appears — take it. A furnace "
    "keeps whatever you loaded even after you close it: reopen the SAME "
    "furnace to collect output. Never leave ingots sitting inside. "
    "FOOD & SURVIVAL: eat until the bar is FULL (20) — health only regenerates while food is high and saturation is up; one bite is not enough. food < 7 "
    "means no sprint; food = 0 starves you to death. Cooked meat restores "
    "far more — smelt when you have a furnace — but raw meat is edible "
    "and better than starving. Hostile mobs (zombie, "
    "skeleton, spider, creeper) attack on sight — kill them or keep >8 "
    "blocks away; creepers explode at close range. Water: you have ~15s "
    "of air, then drown — swim up (jump) to breathe. Lava and fire kill "
    "fast — avoid. Falls over ~3 blocks deal damage; deep holes kill. "
    "At night hostiles spawn outdoors — daylight is safer. "
    "MOVEMENT: jump reaches exactly 1 block up. A 2-high wall needs tower "
    "up (place blocks under you) or a staircase/dug ramp. Hold a placeable "
    "block to tower. "
    "DIMENSIONS/PORTALS: nether portal = 10-14 obsidian frame lit with "
    "flint_and_steel (obsidian = water on lava, needs diamond pickaxe to "
    "mine). Nether has fortresses (blazes -> blaze_rod) and bastions. "
    "blaze_powder = blaze rod; eye_of_ender = blaze_powder + ender_pearl "
    "(enderman drop). Throw eyes to find the stronghold; fill portal "
    "frames with eyes -> the End; destroy crystals, kill the dragon. "
    "GENERAL: drops vanish after ~5 min. Items stack to 64. Only hotbar "
    "slots 0-8 are directly selectable; backpack items need equip/"
    "select_item first. Death drops your items unless keep_inventory is "
    "on — re-collecting wastes time, so avoid dying."
)


def stage_hint(task, inv, st):
    """Concrete next-step hint for the CURRENT stage, derived from inventory.
    JEV's planner is weak on multi-step chains — it crafted planks then
    wandered back to mining logs — so when materials are ready, say exactly
    what to do."""
    t = task.lower()
    pl = sum(c for k, c in inv.items() if k.endswith("_planks"))
    logs = sum(c for k, c in inv.items() if k.endswith("_log"))
    notable0 = st.get("notable_blocks") or []
    pal0 = (st.get("blocks") or {}).get("palette") or []
    has_table = (inv.get("crafting_table", 0) > 0
                 or any("crafting_table" in base_name(b.get("block", ""))
                        for b in notable0)
                 or any("crafting_table" in base_name(x) for x in pal0))
    table_open = "3x3" in str(
        (st.get("open_container") or {}).get("crafting_grid", ""))
    sticks = inv.get("stick", 0)
    if "wooden pickaxe" in t or "crafting table" in t:
        if not has_table and pl < 4 and logs > 0:
            return "Craft planks first (craft *_planks), then craft crafting_table."
        if not has_table and pl >= 4:
            return ("You have enough planks — craft crafting_table NOW, "
                    "then place it (place crafting_table), open it, craft "
                    "the wooden_pickaxe.")
        if has_table and not table_open:
            return ("Place the crafting_table (place candidate), then "
                    "interact with it, then craft wooden_pickaxe in the "
                    "3x3 grid.")
        if table_open and inv.get("wooden_pickaxe", 0) == 0:
            if pl >= 3 and sticks >= 2:
                return "Crafting table is open — craft wooden_pickaxe now."
            return (f"Need 3 planks + 2 sticks (have {pl} planks, "
                    f"{sticks} sticks) — craft what's missing.")
    # no pickaxe anywhere yet — the chain always starts wood->table->wooden pickaxe
    has_any_pick = any("pickaxe" in k for k in inv)
    if not has_any_pick:
        # a placed table with no wood is useless — gather first
        if pl == 0 and logs == 0:
            return ("No pickaxe and no wood — punch tree logs bare-handed "
                    "(mine *_log), then craft planks.")
        if pl < 4 and logs > 0:
            return "Craft planks from your logs first (craft *_planks)."
        if not has_table:
            return "You have planks — craft crafting_table, place it, open it."
        if pl < 5:
            return ("A crafting_table is placed but you need 5 planks "
                    f"(have {pl}) — punch more logs, craft planks.")
        if not table_open:
            return ("A crafting_table is placed — 'use' it to open the 3x3 "
                    "grid, then craft sticks (2 planks) and wooden_pickaxe "
                    "(3 planks + 2 sticks).")
        return "Craft sticks (2 planks) then wooden_pickaxe (3 planks + 2 sticks)."
    # craft-stages must be checked before the generic "stone" miner hint —
    # "craft a stone pickaxe" also contains the word stone
    if "stone pickaxe" in t or "stone sword" in t or "stone axe" in t:
        cob = inv.get("cobblestone", 0) + inv.get("cobbled_deepslate", 0)
        if inv.get("stone_pickaxe", 0) > 0 and (
                inv.get("stone_sword", 0) or inv.get("stone_axe", 0)):
            return None
        if cob < 3:
            return f"Mine more cobblestone first ({cob}/3+)."
        if inv.get("crafting_table", 0) == 0:
            return ("Craft a crafting_table from planks first, then place it.")
        if not table_open:
            return ("Place your crafting_table, interact with it, then craft "
                    "stone_pickaxe (3 cobblestone + 2 sticks) and stone_sword "
                    "or stone_axe.")
        return "Craft stone_pickaxe now, then a stone sword or axe."
    if "cobblestone" in t:
        return ("Equip a pickaxe (select or equip candidate), then mine "
                "stone blocks — bare hands drop nothing.")
    if "furnace" in t:
        cob = inv.get("cobblestone", 0) + inv.get("cobbled_deepslate", 0) \
            + inv.get("blackstone", 0)
        if cob < 8:
            return (f"Mine more cobblestone first ({cob}/8) — andesite, "
                    "dirt and gravel do NOT work for a furnace.")
        if not table_open and inv.get("crafting_table", 0) == 0:
            return ("Place your crafting table, interact with it, then "
                    "craft furnace in the 3x3 grid (needs 8 cobblestone).")
        if table_open:
            return "Craft furnace now — you have the cobblestone."
        return "Crafting table is open — craft furnace (8 cobblestone ring)."
    if "raw meat" in t or "animals" in t:
        prey = any(base_name(e.get("type", "")) in
                   ("pig", "cow", "sheep", "chicken")
                   for e in (st.get("entities") or []))
        if prey:
            return "Find a pig/cow/sheep and use 'attack' repeatedly until it dies."
        return ("No animals visible — walk in any direction to explore until "
                "a pig/cow/sheep/chicken appears in entities, then attack it.")
    if "smelt" in t and "food" in t:
        return ("Place your furnace, pick 'smelt <meat>' (uses planks/logs "
                "as fuel), then eat the cooked food.")
    if "smelt" in t and "iron" in t:
        return ("Place furnace, pick 'smelt raw_iron' (coal or planks as "
                "fuel). Needs a placed furnace within reach.")
    if "iron" in t and "ore" in t:
        return ("Dig/staircase down below y~16 and mine iron_ore with a "
                "stone pickaxe — never bare hands.")
    if "diamond" in t and "mine" in t:
        return ("Dig/staircase down to y=-50 or lower, mine diamond_ore with "
                "the IRON pickaxe — wooden/stone breaks it with no drop.")
    if "obsidian" in t:
        return ("Find lava pools (often near y=-50..10 or on the surface), "
                "pour water on them or find natural obsidian, and mine it "
                "with the DIAMOND pickaxe — it takes ~9s per block.")
    if "blaze" in t:
        return "In the nether: find a fortress (nether_bricks), kill blazes for rods."
    if "ender pearl" in t:
        return ("Kill endermen at night for pearls (dodge their hits), or "
                "barter with piglins in the nether.")
    if "eye" in t or "eyes of ender" in t:
        return "Craft ender_eye (blaze powder + ender pearl) — no table needed."
    if "stronghold" in t or "end portal" in t:
        return ("Throw 'eye of ender', follow its flight direction, repeat "
                "until it dives down — then dig down to the stronghold.")
    if "crystal" in t or "dragon" in t:
        return ("Shoot/hit the caged end crystals on tower tops (climb or "
                "tower up), then attack the dragon when it perches.")
    return None


def summarize(st, history, task, placed_cols=None):
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
        if e.get("health") is not None:
            ent["hp"] = round(e["health"])
            ent["max_hp"] = round(e.get("max_health") or 0)
        if e.get("item"):
            ent["item"] = base_name(e["item"])
        if e.get("hostile"):
            ent["hostile"] = True
        if et in ENTITY_DROPS:
            ent["drops"] = ENTITY_DROPS[et]
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
    warnings = []
    food = p.get("food", 20)
    if food <= 0:
        warnings.append("STARVING — food bar is empty; you are taking damage. "
                        "Eat any food, or kill an animal for meat NOW.")
    elif food <= 6:
        warnings.append("VERY HUNGRY — food at %d/20; you cannot sprint and "
                        "will starve soon. Find food." % food)
    if p.get("air", 300) < p.get("max_air", 300):
        warnings.append("DROWNING — air at %s; swim up for air NOW."
                        % p.get("air"))
    if p.get("on_fire"):
        warnings.append("ON FIRE — move to water or keep moving.")
    hostiles = [e["type"] for e in ents if e.get("hostile")]
    if hostiles:
        warnings.append("HOSTILE MOBS NEARBY: " + ", ".join(hostiles[:4])
                        + " — fight or flee.")
    if not p.get("sky_above", True) and p.get("surface_y", py) - py >= 3:
        warnings.append("UNDERGROUND/BURIED — no sky above; surface is ~%d "
                        "blocks up (y=%d)." % (p["surface_y"] - py,
                                               p["surface_y"]))
    return {
        "task": task,
        "how_to_do_it": TASK_WALKTHROUGH,
        "stage_hint": stage_hint(task, inv, st),
        "game_rules": GAME_RULES,
        "warnings": warnings,
        "pos": [round(px, 1), round(py, 1), round(pz, 1)],
        "health": p.get("health"), "food": p.get("food"),
        "dimension": p.get("dimension"),
        "gamemode": p.get("gamemode"), "on_ground": p.get("on_ground"),
        "alive": p.get("alive"), "sky_above": p.get("sky_above"),
        "biome": w.get("biome"), "day_time": w.get("day_time"),
        "hotbar": hotbar, "selected_slot": sel,
        "held": hotbar.get(sel),
        "inventory_counts": inv,
        "looking_at": looking,
        "open_container": (st.get("open_container") or {}).get("title"),
        "craftable_now": (st.get("craftable") or
                          {"2x2": feasible_crafts(inv, table_open=False),
                           "3x3": feasible_crafts(inv, table_open=True)}),
        "item_uses": item_uses_for(inv),
        "block_yields": block_uses_for(notable),
        "spawn": (st.get("world") or {}).get("spawn"),
        "nearest_entities": ents[:24],
        "ground_items": [e for e in ents if "item" in e][:12],
        "inventory_items": [
            {"slot": i.get("slot"), "item": base_name(i.get("id", "")),
             "count": i.get("count", 1)}
            for i in items],
        "blocks_near": blocks_near(st, px, py, pz),
        "terrain_dirs": (p.get("terrain") or {}).get("dirs"),
        "terrain_hmap": (p.get("terrain") or {}).get("hmap"),
        "notable_blocks": notable,
        "block_palette": [base_name(n) for n in
                          (st.get("blocks") or {}).get("palette", [])][:40],
        "recent_actions": history[-10:],
        "pos_trail": [h.get("pos") for h in history[-8:] if h.get("pos")],
        "times_here": sum(
            1 for h in history if h.get("pos")
            and abs(h["pos"][0] - px) <= 4 and abs(h["pos"][2] - pz) <= 4),
        "steps_elapsed": len(history),
        "deaths": sum(1 for h in history if h.get("act") == "respawn"),
        "stats": {"y": round(py), "surface_y": p.get("surface_y"),
                  "xp_level": p.get("xp_level")},
        "world_map": world_map(st, history, placed_cols or []),
    }


MAP_CHARS = [
    (("water", "river", "ocean"), "~"),
    (("lava",), "!"),
    (("leaf", "leaves", "vine"), "f"),
    (("log", "stem", "hyphae", "wood"), "T"),
    (("sand", "sandstone"), ","),
    (("snow", "ice"), "*"),
    (("stone", "deepslate", "tuff", "andesite", "diorite", "granite",
      "gravel", "ore", "obsidian", "bedrock"), "^"),
    (("dirt", "grass", "mud", "podzol", "mycelium", "clay"), "."),
]


def map_char(name):
    for keys, ch in MAP_CHARS:
        if any(k in name for k in keys):
            return ch
    return "?"


def world_map(st, history, placed_cols):
    """Coarse ASCII sketch of the loaded area: top solid block class per
    4-block column, trail of visited cells, placed columns, player, N up."""
    p = st.get("player", {})
    px, py, pz = pos_of(st)
    cx, cz = math.floor(px), math.floor(pz)
    stride, half = 4, 8            # 17x17 cells covering +-64 blocks
    cells = []
    for gz in range(-half, half + 1):
        row = []
        for gx in range(-half, half + 1):
            wx, wz = cx + gx * stride, cz + gz * stride
            # topmost solid block in this column inside the fetched grid
            ch = " "
            for wy in range(math.floor(py) + 12, math.floor(py) - 40, -1):
                n = block_at(st, wx, wy, wz)
                if n and n not in ("air", "cave_air", "void_air", "water"):
                    ch = map_char(n)
                    break
                if n is None:
                    break
            row.append(ch)
        cells.append(row)
    # overlays: visited trail '.', placed columns 'B', player '@'
    trail = set()
    for h in history:
        hp = h.get("pos")
        if hp:
            trail.add((hp[0], hp[2]))
    for gx in range(-half, half + 1):
        for gz in range(-half, half + 1):
            wx, wz = cx + gx * stride, cz + gz * stride
            if any(abs(wx - tx) <= 2 and abs(wz - tz) <= 2 for tx, tz in trail):
                if cells[gz + half][gx + half] != " ":
                    cells[gz + half][gx + half] = "v"  # visited
    for pcx, pcz, _, _ in placed_cols:
        gx, gz = (pcx - cx) // stride + half, (pcz - cz) // stride + half
        if 0 <= gx < 2 * half + 1 and 0 <= gz < 2 * half + 1:
            cells[gz][gx] = "B"
    cells[half][half] = "@"
    sp = (st.get("world") or {}).get("spawn") or {}
    if sp:
        gx = int((sp.get("x", cx) - cx) / stride) + half
        gz = int((sp.get("z", cz) - cz) / stride) + half
        if 0 <= gx < 2 * half + 1 and 0 <= gz < 2 * half + 1:
            cells[gz][gx] = "S"
    return {
        "legend": "@ you, S spawn, v visited trail, B your tower/build, "
                  "~ water, ! lava, f leaves, T tree, ^ stone/rock, . dirt/grass, "
                  ", sand, * snow, ? other, ' ' unloaded. "
                  "Row 0 is NORTH (-z); center cell is you. "
                  "Each char = a 4x4-block column's surface block.",
        "grid": ["".join(r) for r in cells],
    }


def bearing(dx, dz):
    """8-way compass label for an offset (MC: -z=north)."""
    a = math.degrees(math.atan2(dx, -dz)) % 360
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dirs[int((a + 22.5) // 45) % 8]


def blocks_near(st, px, py, pz):
    """Per-type digest of every block in the fetched grid: count plus the
    nearest instance's distance/bearing/height. This is the '360 look-around'
    — every visible block kind is represented even when far from a candidate."""
    b = st.get("blocks") or {}
    data, pal = b.get("data"), b.get("palette")
    if not data or not pal:
        return {}
    ox, oy, oz = b["origin_x"], b["origin_y"], b["origin_z"]
    sx, sz = b["size_x"], b["size_z"]
    stats = {}
    for i, pi in enumerate(data):
        name = base_name(pal[pi])
        if name in ("air", "cave_air", "void_air"):
            continue
        dy = i // (sx * sz)
        rem = i % (sx * sz)
        wx, wy, wz = ox + rem % sx, oy + dy, oz + rem // sx
        dx, dyy, dz = wx - px, wy - py, wz - pz
        d = dx * dx + dyy * dyy + dz * dz
        e = stats.get(name)
        if e is None:
            stats[name] = e = {"count": 0, "d2": 1e18}
        e["count"] += 1
        if d < e["d2"]:
            e["d2"] = d
            e["nearest"] = {"d": round(math.sqrt(d)),
                            "dir": bearing(dx, dz),
                            "dy": round(dyy)}
    for e in stats.values():
        e.pop("d2", None)
    return stats


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


TASK_WALKTHROUGH = (
    "You spawned EMPTY-HANDED in a new world. From scratch: "
    "1) punch a tree log (bare hands work on wood) for 4+ logs; "
    "2) craft planks (2x2), craft a crafting_table, place it; "
    "3) craft a wooden_pickaxe; 4) mine 3+ stone for cobblestone, craft a "
    "stone_pickaxe (stone tier unlocks iron ore); also stone axe/sword; "
    "5) hunt sheep (attack) for 3 wool + animals for food; eat until full; "
    "6) craft+place a furnace (8 cobblestone), smelt any iron_ore you find "
    "with fuel (logs/charcoal); iron_ore needs stone+ pickaxe and shows as "
    "raw_iron; 7) craft iron armor (helmet 5, chestplate 8, leggings 7, "
    "boots 4 ingots) + iron_pickaxe/sword/axe/shovel (24+ ingots total); "
    "8) craft bed = 3 wool + 3 planks; 9) house = pillar/place blocks into "
    "enclosed walls then a roof, place the bed inside. "
    "Stone/ore without a pickaxe drops NOTHING — never mine stone "
    "bare-handed. You jump only 1 block — tower up (place at feet) for "
    "bigger rises.")
NEEDS_PICKAXE = ("stone", "deepslate", "andesite", "granite", "diorite",
                 "tuff", "ore", "cobblestone", "obsidian", "netherrack",
                 "sandstone", "calcite", "iron_block", "gold_block", "diamond_block")


FOOD_ITEMS = {"cooked_beef", "cooked_porkchop", "cooked_chicken",
              "cooked_mutton", "bread", "apple", "golden_apple", "carrot",
              "baked_potato", "beetroot", "melon_slice", "sweet_berries",
              "glow_berries", "dried_kelp", "cookie", "pumpkin_pie",
              "mushroom_stew", "rabbit_stew", "cooked_cod", "cooked_salmon",
              "cooked_rabbit", "honey_bottle", "rotten_flesh", "beetroot_soup",
              # raw meat is edible — weak + may cause hunger effect, but beats starving
              "porkchop", "beef", "mutton", "chicken", "rabbit",
              "cod", "salmon"}
SMELTABLES = {"raw_iron", "raw_copper", "raw_gold", "iron_ore",
              "copper_ore", "gold_ore", "deepslate_iron_ore",
              "deepslate_copper_ore", "deepslate_gold_ore", "cobblestone",
              "sand", "red_sand", "clay_ball", "netherrack", "potato",
              "raw_beef", "porkchop", "raw_chicken", "raw_mutton",
              "raw_rabbit", "cod", "salmon", "kelp", "oak_log",
              "spruce_log", "birch_log", "chorus_fruit", "cactus"}
FUELS_PREFERRED = ("coal", "charcoal", "coal_block", "blaze_rod",
                   "dried_kelp_block", "oak_log", "spruce_log", "birch_log",
                   "oak_planks", "spruce_planks", "birch_planks")
APPROACH_ENTITIES = {"blaze", "enderman", "piglin", "cow", "pig", "sheep",
                     "chicken", "rabbit", "villager", "iron_golem",
                     "wither_skeleton", "magma_cube", "zombified_piglin",
                     "hoglin", "eye_of_ender"}
ENTITY_DROPS = {
    "pig": ["porkchop"], "cow": ["beef", "leather"],
    "sheep": ["mutton", "wool"], "chicken": ["chicken", "feather"],
    "rabbit": ["rabbit", "rabbit_hide"],
    "zombie": ["rotten_flesh"], "skeleton": ["bone", "arrow"],
    "spider": ["string", "spider_eye"], "creeper": ["gunpowder"],
    "enderman": ["ender_pearl"], "blaze": ["blaze_rod"],
    "wither_skeleton": ["wither_skeleton_skull", "bone", "coal"],
    "magma_cube": ["magma_cream"], "zombified_piglin": ["rotten_flesh", "gold_nugget"],
    "hoglin": ["porkchop", "leather"], "piglin": ["gold_ingot (barter)"],
    "slime": ["slime_ball"], "witch": ["glowstone_dust", "redstone"],
    "iron_golem": ["iron_ingot", "poppy"],
}


_TABLE_POS_FILE = "/tmp/jev_table_pos.json"

def _load_table_pos():
    try:
        return json.load(open(_TABLE_POS_FILE))
    except Exception:
        return {}

def _save_table_pos(tp):
    try:
        json.dump(tp, open(_TABLE_POS_FILE, "w"))
    except Exception:
        pass

# each run starts fresh — a remembered table from a previous run/world sends
# the agent walking to a position that no longer exists
try:
    os.remove(_TABLE_POS_FILE)
except OSError:
    pass
_table_pos = {}

# short-lived commitment to one walk target per block type — otherwise two
# same-type blocks equidistant flip the "nearest" each step and JEV ping-pongs
_walk_lock = {}          # name -> [bx, bz, expiry_step]
_use_cd = {}             # "use X @..." label -> step when it's offered again
_use_pending = [None]    # last `use X` pick awaiting a productive follow-up

def candidates(st, home=None, bad_drops=None, step_n=0):
    """Generate candidate actions from live state -> {key: (desc, payload)}."""
    bad_drops = bad_drops or set()
    px, py, pz = pos_of(st)
    yaw = math.radians(st.get("player", {}).get("yaw", 0))
    out = {"done": ("Declare the task complete.", None)}

    inv_list0 = st.get("inventory", {}).get("items", [])
    sel0 = st.get("player", {}).get("selected_slot", 0)
    held0 = next((base_name(i["id"]) for i in inv_list0
                  if i.get("slot") == sel0), "")
    inv_names0 = {base_name(i.get("id","")) for i in inv_list0}
    has_pickaxe = any("pickaxe" in n for n in inv_names0)
    pick_warn = ("" if "pickaxe" in (held0 or "")
                 else " (needs a pickaxe HELD — select one first, or it drops nothing)")

    # best melee weapon in the hotbar — attacks select it first
    weapon_slot = None
    for rank, names in enumerate(
            (("netherite_sword", "diamond_sword", "iron_sword", "stone_sword",
              "golden_sword", "wooden_sword", "netherite_axe", "diamond_axe",
              "iron_axe", "stone_axe", "wooden_axe"),)):
        for it in inv_list0:
            n = base_name(it.get("id", ""))
            if n in names and 0 <= it.get("slot", -1) <= 8 and n != held0:
                weapon_slot = (it["slot"], n)
                break
        if weapon_slot:
            break

    notable = sorted(
        (b for b in st.get("blocks", {}).get("notable", [])),
        key=lambda b: math.dist((px, py, pz), (b["x"], b["y"], b["z"])))
    # vegetation is movement noise, not a target — the ahead/above/below dig
    # candidates still cover the case where it actually blocks the path
    SKIP_MINE = ("leaves", "leaf_litter", "grass", "fern", "sapling",
                 "poppy", "dandelion", "flower", "vine", "bush", "litter")
    seen, mine_n = set(), 0
    # the nearest tree always earns a directional walk candidate, even when
    # nearer filler (dirt/snow/ore) would crowd it out of the menu
    if not has_pickaxe:
        log = next((b for b in notable
                    if "log" in base_name(b["block"]) or "stem" in base_name(b["block"])),
                   None)
        if log:
            name = base_name(log["block"])
            br = bearing(log["x"] + 0.5 - px, log["z"] + 0.5 - pz)
            d = math.dist((px, pz), (log["x"] + 0.5, log["z"] + 0.5))
            out[f"walk {br.lower()} toward {name}"] = (
                f"Walk {br} toward the {name} at ({log['x']},{log['y']},{log['z']}), "
                f"{d:.0f} blocks away — punch it for wood.",
                {"type": "move", "bearing": br,
                 "seconds": max(1.0, min(6.0, d / 5.5 + 0.5)),
                 "sprint": True, "tx": log["x"] + 0.5, "tz": log["z"] + 0.5})
            seen.add(f"walked_{name}")
    for b in notable:
        name, bx, by, bz = base_name(b["block"]), b["x"], b["y"], b["z"]
        d = math.dist((px, py, pz), (bx + 0.5, by + 0.5, bz + 0.5))
        if name in seen and d > 6:
            continue
        if any(k in name for k in SKIP_MINE):
            continue
        if not has_pickaxe and any(k in name for k in NEEDS_PICKAXE):
            continue  # never surface unbreakable-without-pickaxe targets at all
        seen.add(name)
        if d <= 5.2:
            # only the NEAREST instance of each type — a cluster of same-name
            # blocks (placed tables, ore veins) would otherwise eat the whole
            # mine menu and crowd out everything else
            if mine_n < 4 and not any(
                    k.startswith(f"mine {name} ") for k in out):
                warn = pick_warn if any(k in name for k in NEEDS_PICKAXE) else ""
                out[f"mine {name} @{bx},{by},{bz}"] = (
                    f"Mine the {name} at ({bx},{by},{bz}), {d:.0f} blocks away." + warn,
                    {"type": "mine", "x": bx, "y": by, "z": bz})
                mine_n += 1
        elif d <= 45:
            # one walk candidate per block type — multiple same-name targets in
            # opposite directions flip the "nearest" each step and pin JEV in a
            # walk-e/walk-w oscillation without ever reaching either tree
            if f"walked_{name}" in seen:
                continue
            seen.add(f"walked_{name}")
            lock = _walk_lock.get(name)
            if lock and step_n < lock[2]:
                bx2, bz2 = lock[0], lock[1]   # keep aiming at the same block
            else:
                bx2, bz2 = bx, bz
                _walk_lock[name] = [bx, bz, step_n + 3]
            br = bearing(bx2 + 0.5 - px, bz2 + 0.5 - pz)
            d2 = math.dist((px, pz), (bx2 + 0.5, bz2 + 0.5))
            # sprint covers ~5.5 b/s — cap walk time so we don't overshoot
            walk_secs = max(1.0, min(6.0, d2 / 5.5 + 0.5))
            out[f"walk {br.lower()} toward {name}"] = (
                f"Walk {br} toward the {name} at ({bx2},{by},{bz2}), "
                f"{d2:.0f} blocks away.",
                {"type": "move", "bearing": br, "seconds": walk_secs,
                 "sprint": True, "tx": bx2 + 0.5, "tz": bz2 + 0.5})
        if len(out) > 15:
            break

    approach_n = 0
    _pl0 = st.get("player", {})
    _buried0 = (not _pl0.get("sky_above", True)
                and _pl0.get("surface_y", py) - py >= 3)
    for e in sorted(st.get("entities", []) or [], key=lambda e: e.get("distance", 99))[:6]:
        et = base_name(e.get("type", ""))
        ex, ey, ez = round(e["x"]), round(e["y"]), round(e["z"])
        d = e.get("distance", 99)
        if et == "item":
            what = base_name(e.get("item", "item"))
            if ((ex, ez) not in bad_drops
                    and not (_buried0 and ey > py + 2)
                    and ey <= py + 3):  # canopy-stuck drops are unreachable
                br = bearing(ex + 0.5 - px, ez + 0.5 - pz)
                out[f"walk {br.lower()} to dropped {what}"] = (
                    f"Walk {br} to the dropped {what} at ({ex},{ey},{ez}), "
                    f"{d:.0f} blocks away.",
                    {"type": "move", "bearing": br,
                     "seconds": max(1.0, min(4.0, d / 5.5 + 0.4)),
                     "sprint": True, "tx": ex + 0.5, "tz": ez + 0.5,
                     "_pickup": what, "_ey": ey, "x": ex, "z": ez})
        elif et == "eye_of_ender":
            # eyes fly toward the stronghold — follow it
            out[f"walk {bearing(ex - px, ez - pz).lower()} after eye of ender"] = (
                f"Walk toward the thrown eye of ender at ({ex},{ez}), "
                f"{d:.0f} blocks away — it points to the stronghold.",
                {"type": "move", "bearing": bearing(ex - px, ez - pz),
                 "seconds": 6, "sprint": True})
        elif d <= 4.5:
            w = f" with the held {held0}" if held0 else ""
            out[f"attack {et}"] = (
                f"Attack the {et}{w} ({d:.0f} blocks away) — swings "
                f"whatever is in your hand.",
                {"type": "attack", "entity": e.get("id"), "times": 12,
                 "seconds": 20, "_verify_kill": e.get("id")})
        elif et in APPROACH_ENTITIES and d <= 45 and approach_n < 3 \
                and not (_buried0 and ey > py + 2) \
                and f"walked_e_{et}" not in seen:
            seen.add(f"walked_e_{et}")   # one walk target per entity type —
            # multiple same-type mobs in opposite directions oscillate forever
            br = bearing(ex + 0.5 - px, ez + 0.5 - pz)
            out[f"walk {br.lower()} toward {et}"] = (
                f"Walk {br} toward the {et} at ({ex},{ez}), "
                f"{d:.0f} blocks away.",
                {"type": "move", "bearing": br, "seconds": 5,
                 "sprint": True})
            approach_n += 1

    # placed crafting table out of reach? offer a bearing-walk back to it.
    # remember its last-seen pos so it stays offered even outside the scan.
    if _table_pos.get("p") is None:
        _table_pos.update(_load_table_pos())
    if _table_pos.get("p") is not None and not st.get("open_container"):
        tb = None
        bd = st.get("blocks") or {}
        data, pal = bd.get("data"), bd.get("palette")
        if data and pal:
            ox, oy, oz = bd["origin_x"], bd["origin_y"], bd["origin_z"]
            sx, sz = bd["size_x"], bd["size_z"]
            import math as _m
            bst, bxx, byy, bzz = 1e9, 0, 0, 0
            for i, pi2 in enumerate(data):
                if "crafting_table" not in base_name(pal[pi2]):
                    continue
                x = ox + i % sx; yy = oy + (i // (sx * sz))
                z = oz + (i // sx) % sz
                dd = _m.dist((px, py, pz), (x + 0.5, yy + 0.5, z + 0.5))
                if dd < bst:
                    bst, bxx, byy, bzz = dd, x, yy, z
            if bst < 1e9:
                _table_pos["p"] = (bxx, byy, bzz)
                _save_table_pos(_table_pos)
        tp = _table_pos.get("p")
        if tp is not None:
            bxx, byy, bzz = tp
            best = math.dist((px, py, pz), (bxx + 0.5, byy + 0.5, bzz + 0.5))
            if best > 4.0:
                br = bearing(bxx + 0.5 - px, bzz + 0.5 - pz)
                if byy > py + 1:
                    # table is uphill — move+jump toward it to climb
                    out[f"hop {br.lower()} back to crafting_table"] = (
                        f"Hop {br} uphill toward your placed crafting_table "
                        f"at ({bxx},{byy},{bzz}), {best:.0f} blocks away.",
                        {"type": "move", "bearing": br, "seconds": 5,
                         "jump": True})
                else:
                    out[f"walk {br.lower()} back to crafting_table"] = (
                        f"Walk {br} back to your placed crafting_table at "
                        f"({bxx},{byy},{bzz}), {best:.0f} blocks away.",
                        {"type": "move", "bearing": br, "seconds": 5,
                         "sprint": True})
            else:
                label = f"use crafting_table @{bxx},{byy},{bzz}"
                if (_use_cd.get(label, 0) <= step_n
                        and _use_cd.get("use crafting_table", 0) <= step_n):
                    out[label] = (
                        f"Open your crafting_table at ({bxx},{byy},{bzz}) — the 3x3 "
                        f"grid you need to craft pickaxes, tools and a furnace.",
                        {"type": "use_on_block", "x": bxx, "y": byy, "z": bzz})
    # interactable blocks in reach (use_on_block aims automatically);
    # suppressed while a container is already open to avoid open/close thrash
    if not st.get("open_container"):
        for b in notable[:30]:
            n = base_name(b["block"])
            if any(k in n for k in INTERACTABLE):
                d = math.dist((px, py, pz), (b["x"] + 0.5, b["y"] + 0.5, b["z"] + 0.5))
                if d <= 5.0:
                    label = f"use {n} @{b['x']},{b['y']},{b['z']}"
                    cd = _use_cd.get(label)
                    if cd is not None and step_n < cd:
                        continue
                    cd2 = _use_cd.get(f"use {n}")
                    if cd2 is not None and step_n < cd2:
                        continue
                    # nearest only — a cluster of same-type blocks shouldn't
                    # flood the menu
                    if any(k.startswith(f"use {n} ") for k in out):
                        continue
                    out[label] = (
                        f"Open/interact with the {n} at ({b['x']},{b['y']},{b['z']}), {d:.0f} blocks away.",
                        {"type": "use_on_block", "x": b["x"], "y": b["y"], "z": b["z"]})

    # place the held block onto the ground ahead of the player
    held = (st.get("inventory", {}).get("items") or [])
    sel = st.get("player", {}).get("selected_slot", 0)
    held_item = next((base_name(i["id"]) for i in held if i.get("slot") == sel), None)
    # only blocks that genuinely attach to a flat floor — ladder needs a
    # wall, door/bed need 2 cells, torch blocks your own feet cell
    PLACEABLE_UTILS = ("crafting_table", "furnace", "smoker", "blast_furnace",
                       "chest", "campfire", "anvil", "loom", "cartography",
                       "grindstone", "smithing", "barrel", "torch")
    # utility blocks sitting in the backpack are just as placeable — offer a
    # select_item-then-place macro so the option exists without holding them
    if not held_item or not any(k in held_item for k in PLACEABLE_UTILS):
        for it in held:
            n = base_name(it.get("id", ""))
            if it.get("slot", -1) >= 0 and any(k in n for k in PLACEABLE_UTILS):
                # target an ADJACENT floor cell — the cell under your feet is
                # inside your own bounding box, placing there always fails
                fx, fy, fz = math.floor(px), math.floor(py) - 1, math.floor(pz)
                tgt = None
                for nx, nz in ((fx + 1, fz), (fx - 1, fz), (fx, fz + 1),
                               (fx, fz - 1)):
                    nb = base_name(block_at(st, nx, fy, nz) or "")
                    above = base_name(block_at(st, nx, fy + 1, nz) or "")
                    if nb not in ("", "air", "water") and above == "air":
                        tgt = (nx, fy, nz)
                        break
                if tgt is None:
                    break
                out[f"equip {n} and place it"] = (
                    f"Take the {n} from your inventory and place it on the "
                    f"ground beside you at ({tgt[0]},{tgt[1]},{tgt[2]}).",
                    {"_macro": [{"type": "select_item", "item": n},
                                {"type": "use_on_block", "x": tgt[0],
                                 "y": tgt[1], "z": tgt[2], "face": "up"}]})
                break
    if held_item and any(k in held_item for k in PLACEABLE_UTILS):
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

    dirs = (st.get("player", {}).get("terrain") or {}).get("dirs") or {}
    COMPASS = {"N": (0, -32), "NE": (23, -23), "E": (32, 0),
               "SE": (23, 23), "S": (0, 32), "SW": (-23, 23),
               "W": (-32, 0), "NW": (-23, -23)}
    CARD4 = {"north": "N", "east": "E", "south": "S", "west": "W"}
    for label, br in CARD4.items():
        dx, dz = COMPASS[br]
        d8 = (dirs.get(label) or {}).get("y8")
        d4 = (dirs.get(label) or {}).get("y4")
        cliff = (f" (DANGER: ground drops ~{-d8} blocks 8 ahead)"
                 if d8 is not None and d8 <= -5 else "")
        wall = (f" (WALL ~{d4} up 4 ahead — can't jump 2+; "
                "detour, mine it, or tower up)"
                if d4 is not None and d4 >= 2 else "")
        out[f"walk {label}"] = (f"Walk {label} to explore." + cliff + wall,
                                {"type": "move", "bearing": br,
                                 "seconds": 8, "sprint": True})
        if d4 is not None and d4 == 1:
            out[f"hop {label}"] = (
                f"Jump-walk {label} — climb the 1-block rise ahead.",
                {"type": "move", "bearing": br, "seconds": 3,
                 "jump": True})
    # digging: block ahead at feet level, and the block under feet
    ax, az = math.floor(px - math.sin(yaw) * 1.6), math.floor(pz + math.cos(yaw) * 1.6)
    fy = math.floor(py)
    n_ahead = base_name(block_at(st, ax, fy, az) or "block")
    if n_ahead != "air" and (has_pickaxe or not any(
            k in n_ahead for k in NEEDS_PICKAXE)):
        out[f"mine {n_ahead} ahead @{ax},{fy},{az}"] = (
            f"Mine the {n_ahead} straight ahead at feet level ({ax},{fy},{az}).",
            {"type": "mine", "x": ax, "y": fy, "z": az})
    n_up = base_name(block_at(st, ax, fy + 1, az) or "block")
    if (n_up not in ("air", "water") and n_ahead != "air"
            and (has_pickaxe or not any(k in n_up for k in NEEDS_PICKAXE))):
        warn = pick_warn if any(k in n_up for k in NEEDS_PICKAXE) else ""
        out[f"mine {n_up} above-ahead @{ax},{fy + 1},{az}"] = (
            f"Mine the {n_up} ahead at head level ({ax},{fy + 1},{az}) — "
            f"clearing feet+head ahead digs an upward staircase." + warn,
            {"type": "mine", "x": ax, "y": fy + 1, "z": az})
    bx, by, bz = math.floor(px), fy - 1, math.floor(pz)
    n_below = base_name(block_at(st, bx, by, bz) or "block")
    if (n_below not in ("air", "water")
            and (has_pickaxe or not any(k in n_below for k in NEEDS_PICKAXE))):
        warn = pick_warn if any(k in n_below for k in NEEDS_PICKAXE) else ""
        out[f"mine {n_below} below @{bx},{by},{bz}"] = (
            f"Mine the {n_below} under your feet ({bx},{by},{bz})." + warn,
            {"type": "mine", "x": bx, "y": by, "z": bz})
    if home:
        pl = st.get("player", {})
        surf = pl.get("surface_y")
        # genuinely buried = solid ground above: tree canopy alone doesn't count
        buried = surf is not None and not pl.get("sky_above", True) \
            and surf - py >= 3
        # buried state is already surfaced in the state summary's warnings[] —
        # a fake "return to surface" wait candidate just burns steps
        _ = buried

    inv_list = st.get("inventory", {}).get("items", [])
    inv_counts2 = {}
    for i in inv_list:
        bn = base_name(i.get("id", ""))
        inv_counts2[bn] = inv_counts2.get(bn, 0) + i.get("count", 1)

    # eat: hotbar food -> select+use; backpack-only food -> select_item+use
    food_level = st.get("player", {}).get("food", 20)
    if food_level < 20 and not st.get("open_container"):
        held_n = (held_item or "").rsplit("x", 1)[0]
        if held_n in FOOD_ITEMS:
            out["eat held food"] = (
                f"Eat the {held_n} you're holding "
                f"(hunger at {food_level}/20).",
                {"type": "use", "seconds": 2.2})
        for it in inv_list:
            n = base_name(it.get("id", ""))
            if n in FOOD_ITEMS and it.get("slot", -1) >= 0:
                verb = "select_slot" if it.get("slot", -1) <= 8 else "select_item"
                out[f"hold {n} to eat"] = (
                    f"Put {n} in your hand (then 'use' it to eat; hunger "
                    f"at {food_level}/20).",
                    {"type": verb,
                     **({"slot": it["slot"]} if verb == "select_slot"
                        else {"item": n})})
                break

    # throwables: ender pearl teleport, eye of ender stronghold-finding
    held_n2 = (held_item or "").rsplit("x", 1)[0]
    for n, label in (("ender_pearl", "ender pearl"), ("eye_of_ender", "eye of ender")):
        for it in inv_list:
            if base_name(it.get("id", "")) != n:
                continue
            if it.get("slot", -1) <= 8 and held_n2 == n:
                out[f"throw {label}"] = (
                    f"Throw the held {label} ('use').",
                    {"type": "use", "seconds": 0.5})
            elif it.get("slot", -1) > 8:
                out[f"equip {label}"] = (
                    f"Move the {label} to your hand (then 'use' to throw).",
                    {"type": "select_item", "item": n})
            break

    # tower up out of holes when carrying placeable blocks — only when
    # genuinely buried (a tree canopy overhead is not a pit)
    pl_terr = st.get("player", {})
    _surf = pl_terr.get("surface_y")
    # deep pit below local surface = can tower out even if a 1-wide chimney
    # overhead still reports sky_above
    _buried = (_surf is not None and _surf - py >= 3
               and (not pl_terr.get("sky_above", True)
                    or _surf - py >= 5))
    if not _buried:
        # walled pit / steep notch: neighbors whose headroom-tall wall can't
        # be cleared by a single jump — tower up is the way out
        _fx, _fz, _fy = math.floor(px), math.floor(pz), math.floor(py)
        _solid = lambda bx_, by_, bz_: base_name(
            block_at(st, bx_, by_, bz_) or "air") not in (
                "air", "cave_air", "void_air", "water")
        _walled = sum(
            1 for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1))
            if _solid(_fx + dx, _fy + 1, _fz + dz)
            and _solid(_fx + dx, _fy + 2, _fz + dz))
        _buried = _walled >= 2 or all(
            _solid(_fx + dx, _fy + 1, _fz + dz)
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)))
    if _buried:
        PLACEABLE = ("dirt", "cobblestone", "planks", "stone", "netherrack",
                     "sand", "gravel", "deepslate", "andesite", "diorite",
                     "granite", "tuff", "basalt", "blackstone", "cobbled")
        surf_lbl = st.get('player', {}).get('surface_y', '?')
        done_tower = False
        held_terr = (held_item or "").rsplit("x", 1)[0]
        if any(k in held_terr for k in PLACEABLE):
            out["tower up"] = (
                f"Jump-place your held {held_terr} under you to pillar up "
                f"toward the surface (y={py:.0f}, surface ~y={surf_lbl}).",
                {"type": "pillar", "blocks": 8, "seconds": 40})
            done_tower = True
        if not done_tower:
            for it in inv_list:
                n = base_name(it.get("id", ""))
                if any(k in n for k in PLACEABLE):
                    sl = it.get("slot", -1)
                    pay = ({"type": "select_slot", "slot": sl}
                           if 0 <= sl <= 8 else
                           {"type": "select_item", "item": n})
                    out[f"hold {n} to tower up"] = (
                        f"Put {n} in your hand, then 'tower up' to pillar "
                        f"out (y={py:.0f}, surface ~y={surf_lbl}).", pay)
                    break

    # smelt: furnace nearby + smeltable input + fuel in inventory
    if not st.get("open_container"):
        furn = next((b for b in notable[:30]
                     if any(k in base_name(b["block"]) for k in
                            ("furnace", "smoker"))), None)
        if furn:
            fd = math.dist((px, py, pz), (furn["x"] + 0.5, furn["y"] + 0.5, furn["z"] + 0.5))
            fuel = next((f for f in FUELS_PREFERRED if inv_counts2.get(f)), None)
            smelt_in = next((s for s in SMELTABLES if inv_counts2.get(s)), None)
            if fuel and smelt_in:
                out[f"smelt {smelt_in}"] = (
                    f"Smelt {inv_counts2[smelt_in]}x {smelt_in} in the {base_name(furn['block'])} "
                    f"at ({furn['x']},{furn['y']},{furn['z']}) using {fuel} as fuel "
                    f"({fd:.0f} blocks away).",
                    {"type": "smelt", "x": furn["x"], "y": furn["y"],
                     "z": furn["z"], "input": smelt_in, "fuel": fuel})

    # portal: walk in (primitive move) — teleport is automatic
    portal = next((b for b in notable[:30]
                   if "portal" in base_name(b["block"])), None)
    if portal:
        br = bearing(portal["x"] + 0.5 - px, portal["z"] + 0.5 - pz)
        out[f"enter portal (walk {br.lower()})"] = (
            f"Walk {br} into the portal at ({portal['x']},{portal['z']}) — "
            f"standing in it teleports you.",
            {"type": "move", "bearing": br, "seconds": 6})

    hotbar_has_tool = False
    for it in st.get("inventory", {}).get("items", []):
        n = base_name(it.get("id", ""))
        if 0 <= it.get("slot", -1) <= 8 and any(k in n for k in
                ("pickaxe", "_axe", "shovel", "sword", "hoe", "torch", "boat",
                 "table", "furnace", "chest", "block", "planks", "log")):
            if "pickaxe" in n:
                hotbar_has_tool = True
            out[f"select {n}"] = (
                f"Select {n} in hotbar slot {it['slot']} (right tool speeds up matching blocks).",
                {"type": "select_slot", "slot": it["slot"]})
    # tools hiding in the backpack are invisible to 'select' — offer a swap
    # into hotbar slot 0 (JEV mined stone bare-handed for 40 steps owning
    # seven pickaxes because none were reachable)
    if not hotbar_has_tool:
        for it in st.get("inventory", {}).get("items", []):
            n = base_name(it.get("id", ""))
            if it.get("slot", -1) > 8 and "pickaxe" in n:
                out[f"equip {n}"] = (
                    f"Equip the {n} from your backpack into your hand "
                    f"(mining without it drops nothing).",
                    {"type": "select_item", "item": "pickaxe"})
                break
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
    # each craftable item is its own candidate — the two-step craft->choose
    # flow hid the real choices behind a generic 'craft' JEV rarely picked
    # say what each item unlocks so JEV can connect "craft planks" to
    # "stage wants a pickaxe" instead of dismissing it as unrelated
    UNLOCKS = {
        "oak_planks": "needed for crafting table, sticks, all tools",
        "spruce_planks": "needed for crafting table, sticks, all tools",
        "birch_planks": "needed for crafting table, sticks, all tools",
        "jungle_planks": "needed for crafting table, sticks, all tools",
        "crafting_table": "unlocks 3x3 recipes: pickaxes, swords, furnace",
        "stick": "needed for every tool and torch",
        "wooden_pickaxe": "mines stone -> unlocks stone tier",
        "stone_pickaxe": "mines iron ore -> unlocks iron tier",
        "iron_pickaxe": "mines diamond ore",
        "diamond_pickaxe": "mines obsidian -> nether portal",
        "furnace": "smelts ores and cooks food",
        "torch": "light; keeps mobs from spawning",
        "iron_sword": "strong melee weapon",
        "stone_sword": "better melee weapon",
        "stone_axe": "better melee/chopping",
        "bucket": "carries water/lava; obsidian + portal help",
        "shield": "blocks attacks and arrows",
        "flint_and_steel": "lights the nether portal",
        "boat": "fast safe travel on water",
        "white_bed": "sets spawn point, skips night",
        "ladder": "climb out of pits",
        "chest": "stores items",
    }
    for item in feas[:8]:
        why = UNLOCKS.get(item, "useful crafting output")
        out[f"craft {item}"] = (
            f"Craft a {item.replace('_', ' ')} — {why} "
            f"(ingredients ready{'' if grid3 else '; 2x2 grid OK'}).",
            {"type": "craft", "item": item})
    if st.get("open_container"):
        out["close screen"] = ("Close the currently open screen/container.",
                               {"type": "close_screen"})
        # the world doesn't tick for the player while a screen is open —
        # walk/mine/use would just time out, so only screen-safe options
        title = str(oc.get("title", "")).lower()
        # furnace-like menu: titled so, OR a non-crafting container whose
        # occupied slots touch 0-2 (input/fuel/output) or whose slots list
        # is empty — furnace actions offered inside a chest are harmless
        _slots = oc.get("slots") or []
        is_furnace = any(k in title for k in ("furnace", "smoker", "blast")) \
            or (not oc.get("crafting_grid")
                and (not _slots or any(s.get("slot", 9) <= 2 for s in _slots)))
        if is_furnace:
            SMELTS = {"raw_iron": "iron_ingot", "iron_ore": "iron_ingot",
                      "raw_gold": "gold_ingot", "gold_ore": "gold_ingot",
                      "raw_copper": "copper_ingot", "copper_ore": "copper_ingot",
                      "cobblestone": "stone", "sand": "glass",
                      "clay_ball": "brick", "netherrack": "nether_brick",
                      "oak_log": "charcoal", "birch_log": "charcoal",
                      "spruce_log": "charcoal", "jungle_log": "charcoal",
                      "porkchop": "cooked_porkchop", "beef": "cooked_beef",
                      "chicken": "cooked_chicken", "mutton": "cooked_mutton",
                      "cod": "cooked_cod", "salmon": "cooked_salmon",
                      "potato": "baked_potato", "kelp": "dried_kelp",
                      "ancient_debris": "netherite_scrap"}
            FUEL = ("coal", "charcoal", "blaze_rod", "lava_bucket",
                    "dried_kelp_block", "oak_log", "birch_log",
                    "spruce_log", "jungle_log", "oak_planks", "stick")
            occ = {s.get("slot") for s in oc.get("slots", [])}
            has_output = 2 in occ
            for s in oc.get("slots", []):
                sn = base_name(s.get("id", ""))
                sl = s.get("slot", -1)
                if sl == 2:
                    out[f"furnace take {sn}"] = (
                        f"TAKE the smelted {sn.replace('_', ' ')} out of the "
                        f"furnace output — it's yours to keep.",
                        {"type": "click_slot", "slot": 2,
                         "click": "quick_move"})
                elif not has_output and sl >= 3 and sn in SMELTS and 0 not in occ:
                    out[f"furnace load {sn}"] = (
                        f"Smelt the {sn.replace('_', ' ')} -> "
                        f"{SMELTS[sn].replace('_', ' ')} (shift-click into "
                        f"the furnace input).", {"type": "click_slot",
                        "slot": sl, "click": "quick_move"})
                elif not has_output and sl >= 3 and sn in FUEL and 1 not in occ:
                    out[f"furnace fuel {sn}"] = (
                        f"Shift-click {sn.replace('_', ' ')} into the furnace "
                        f"as fuel.", {"type": "click_slot", "slot": sl,
                        "click": "quick_move"})
            out["wait for smelting"] = (
                "Wait while the furnace smelts (~2.5s per item at this tick "
                "rate).", {"type": "wait", "seconds": 3})
        ok = {k: v for k, v in out.items()
              if k.startswith(("equip ", "select ", "hold ",
                               "close screen", "drop ", "furnace ",
                               "wait for smelting"))
              # craft only works while a crafting menu is open — the plain
              # inventory (2x2) is never reported as open_container
              or (k.startswith("craft ") and oc.get("crafting_grid"))}
        out = ok
    out["wait"] = ("Wait one second.", {"type": "wait", "seconds": 1})
    return out


def main():
    task = sys.argv[1] if len(sys.argv) > 1 else "Collect a log of wood."
    stages = (PLAYTHROUGH_STAGES if task == "playthrough"
              else [task])
    print(f"[jev-agent] model={MODEL} task={task!r} stages={len(stages)}",
          flush=True)
    history = []
    # cells/columns the agent itself placed — never offer to mine or chase
    # drops at them again (that's the build-up / mine-down oscillation)
    placed_cells = set()          # exact (x,y,z)
    placed_cols = []              # (cx, cz, y_from, y_to) for towers
    bad_drops = set()             # (x,z) of item drops that failed pickup twice
    drop_fails = {}               # (x,z) -> consecutive pickup failures
    placed_names = set()          # item names we've placed (table/furnace...)

    def at_placed(key):
        # only strip destructive candidates on our own builds — using a
        # crafting_table we placed must stay available
        if not key.startswith(("mine ", "walk to dropped ", "hop ")):
            return False
        if " @" not in key:
            return False
        try:
            x, y, z = map(int, key.rsplit(" @", 1)[1].split(","))
        except ValueError:
            return False
        if (x, y, z) in placed_cells:
            return True
        return any(x == cx and z == cz and yf - 1 <= y <= yt + 1
                   for cx, cz, yf, yt in placed_cols)

    home = None
    stage_idx = 0
    stage_start = 1
    stuck_warned = False
    for step in range(1, MAX_STEPS + 1):
        cur_task = stages[stage_idx]
        if len(stages) > 1:
            task = (f"STAGE {stage_idx + 1}/{len(stages)}: {cur_task}. "
                    f"Overall goal: {sys.argv[1]}.")
        else:
            task = cur_task
        if not stuck_warned and step - stage_start >= STAGE_STEPS:
            print(f"[jev-agent] stage {stage_idx + 1} exceeded "
                  f"{STAGE_STEPS} steps — still trying", flush=True)
            stuck_warned = True
        st = api("/v1/state?blocks=" + os.environ.get("BLOCK_RADIUS", "40")
                 + "&below=28&above=10")
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
        px, py, pz = pos_of(st)
        summ = summarize(st, history, task, placed_cols)
        summ["placed"] = sorted(placed_names)
        # deterministic stage gate: JEV's done-vote can't be trusted to notice
        # that the goal is already met (it sat at ~0.25 with 17 logs in inv)
        if len(stages) > 1:
            chk = stage_done(stage_idx, summ["inventory_counts"], summ)
            if chk is True:
                print(f"[jev-agent] STAGE {stage_idx + 1} COMPLETE "
                      f"(objective met) -> next: "
                      f"{stages[stage_idx + 1]!r}", flush=True)
                stage_idx += 1
                stage_start = step
                stuck_warned = False
                continue
            stage_objective = chk            # False = unmet, None = JEV votes
        else:
            stage_objective = None
        cands = candidates(st, home, bad_drops, step)
        # suppress candidates that already failed recently (stop retry loops)
        recent_fails = [h["act"] for h in history[-6:] if not h.get("ok")]
        cands = {k: v for k, v in cands.items()
                 if not any(k == rf or (k.startswith(rf.split(" @")[0] + " @") and rf.startswith(k.split(" @")[0]))
                            for rf in recent_fails)} or cands
        # also suppress anything already picked 4+ times in the last 8 —
        # that loop is winning by repetition, not by progress
        recent_picks = collections.Counter(
            h["act"] for h in history[-8:] if h.get("act"))
        cands = {k: v for k, v in cands.items()
                 if recent_picks[k] < 4} or cands
        # a select changes nothing but the held slot — don't allow two in a row,
        # and never offer re-selecting the item already held (JEV ping-pong fix)
        if history and (history[-1].get("act") or "").startswith("select "):
            cands = {k: v for k, v in cands.items()
                     if not k.startswith("select ")} or cands
        held0 = (summ.get("held") or "").rsplit("x", 1)[0]
        if held0:
            cands = {k: v for k, v in cands.items()
                     if k != "select " + held0} or cands
        # never re-mine / re-chase cells we built ourselves
        cands = {k: v for k, v in cands.items()
                 if not at_placed(k)} or cands
        # focus filter: when the stage hint says materials are ready, strip
        # pure-gather noise (mine/pickup/walk/goto/return) so the menu only
        # holds progress actions — JEV still picks, just can't get distracted
        hint = summ.get("stage_hint") or ""
        if any(w in hint for w in ("craft", "Craft", "place", "Place")) and \
                any(k.startswith(("craft ", "place ", "use ", "select ",
                                  "equip ", "smelt ")) for k in cands):
            focus = {k: v for k, v in cands.items()
                     if k.split(" @")[0].split(" ")[0] in
                     ("craft", "place", "use", "select", "equip", "smelt",
                      "eat", "close", "wait", "done", "tower", "fetch",
                      "throw", "open", "enter")}
            if len(focus) >= 2:
                cands = focus
        ans = jev(summ, {
            "act": {"type": "choice",
                    "instructions": "Pick the single best next action to progress the task. "
                                    "If state.stage_hint is present, prefer the action that "
                                    "directly performs it. Avoid repeating an action that "
                                    "just failed; chain the steps needed (gather -> craft -> use).",
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

        # suboptimal-action log: flag picks worth investigating so we can see
        # *why* the model chose badly (missing context? bad candidates? hint
        # ignored?) and improve the harness
        sub = []
        hint = summ.get("stage_hint")
        if hint and pick and not any(
                w in pick for w in ("craft", "place", "use", "equip",
                                    "select", "smelt", "eat", "mine",
                                    "attack", "throw", "enter", "tower")):
            sub.append(f"hint ignored: '{hint}' vs pick '{pick}'")
        if pick and probs:
            ranked = sorted(probs.items(), key=lambda x: -x[1])
            better = [k for k, v in ranked[:5]
                      if k != pick and v >= probs.get(pick, 0) * 0.8
                      and k.split(" @")[0].split(" ")[0] in
                      ("craft", "place", "use", "equip", "smelt", "eat",
                       "attack", "mine", "throw", "enter")]
            if pick.split(" ")[0] in ("wait", "walk", "goto", "done") \
                    and better:
                sub.append(f"idled '{pick}' while progress opts {better} had mass")
        if len(history) >= 4 and all(h.get("act") == pick
                                     for h in history[-3:]):
            sub.append(f"4th+ consecutive '{pick}'")
        sublog = os.environ.get("JEV_SUBLOG", "/tmp/jev_suboptimal.log")
        if sub:
            with open(sublog, "a") as f:
                f.write(f"[{step}] stage={stage_idx+1} pick={pick} "
                        f"conf={ans.get('act', {}).get('confidence')} "
                        f"pos={[round(px),round(py),round(pz)]} "
                        f"top3={sorted(probs.items(), key=lambda x:-x[1])[:3]} "
                        f"| {'; '.join(sub)}\n")
            print(f"      -> suboptimal: {'; '.join(sub)}", flush=True)

        if pick == "done":
            # only the JEV-voted stages (stage_objective is None) can advance
            # on done_p; checkable stages advance via stage_done() instead
            if done_p >= DONE_THRESH and stage_objective is not False:
                if stage_idx + 1 < len(stages):
                    print(f"[jev-agent] STAGE {stage_idx + 1} COMPLETE "
                          f"(done_p={done_p:.2f}) -> next: "
                          f"{stages[stage_idx + 1]!r}", flush=True)
                    stage_idx += 1
                    stage_start = step
                    stuck_warned = False
                    continue
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
        # crafting mutates inventory — keep the deterministic stage gate honest
        # by letting the next loop iteration re-check with fresh state

        # pickups claim success when the walk finishes — verify the item count grew
        before_ct = None
        if payload.get("_pickup"):
            inv0 = api("/v1/state").get("inventory", {}).get("items", [])
            before_ct = sum(i.get("count", 1) for i in inv0
                            if base_name(i.get("id", "")) == payload["_pickup"])
        # a macro expands one JEV pick into a sequence of mod actions
        steps = payload.get("_macro") or [payload]
        for sub in steps:
            res = action(sub, wait=25)
            aid = res.get("action_id") or res.get("id")
            for _ in range(30):
                if res.get("status") not in ("queued", "running"):
                    break
                time.sleep(1)
                res = api("/v1/action?id=" + str(aid))
            if res.get("status") in ("failed", "cancelled"):
                break
        # attack verification: did the target actually die?
        vid = steps[-1].get("_verify_kill") or payload.get("_verify_kill")
        if vid is not None and res.get("status") == "done":
            # dying entities linger ~20 ticks in the entity list during the
            # death animation — "still listed" != "still alive"; check hp
            time.sleep(0.5)
            alive = any(e.get("id") == vid and (e.get("health") or 0) > 0
                        for e in api("/v1/state").get("entities", []) or [])
            if alive:
                res["status"] = "failed"
                res["error"] = "target still alive — keep attacking"
            else:
                res["message"] = "killed"
        if before_ct is not None and res.get("status") == "done":
            inv1 = api("/v1/state").get("inventory", {}).get("items", [])
            after_ct = sum(i.get("count", 1) for i in inv1
                           if base_name(i.get("id", "")) == payload["_pickup"])
            if after_ct <= before_ct:
                res = {"status": "failed",
                       "error": f"walked to drop but no {payload['_pickup']} collected"}
                dk = (int(payload.get("x", 0)), int(payload.get("z", 0)))
                drop_fails[dk] = drop_fails.get(dk, 0) + 1
                if drop_fails[dk] >= 2:
                    bad_drops.add(dk)
                    print(f"      -> blacklisting unreachable drop @{dk}", flush=True)
            else:
                drop_fails[(int(payload.get("x", 0)), int(payload.get("z", 0)))] = 0
        ok = bool(res.get("ok")) and res.get("status") not in ("failed", "cancelled")
        # a bare-handed pickaxe-block mine "succeeds" but drops nothing — say so
        held_now = (summ.get("held") or "").rsplit("x", 1)[0]
        if ok and payload.get("type") == "mine" and "pickaxe" not in (held_now or "") and pick and (
                any(k in pick.split(" @")[0] for k in NEEDS_PICKAXE)):
            ok = False
            res["status"] = "failed"
            res["error"] = "broke it bare-handed — no drop without a pickaxe"
        note = {"act": pick, "status": res.get("status"), "ok": ok,
                "pos": [round(px), round(py), round(pz)]}
        if res.get("message"):
            note["msg"] = res["message"]
        if res.get("error"):
            note["error"] = res["error"]
        history.append(note)
        print(f"      -> {res.get('status')} {res.get('message', '')} {res.get('error', '')}", flush=True)

        # full decision log: one JSON line per step for offline analysis —
        # what the model saw (state summary), the menu offered, its probs,
        # what it picked, and what happened
        try:
            with open(os.environ.get("JEV_LOG", "/tmp/jev_decisions.jsonl"),
                      "a") as f:
                f.write(json.dumps({
                    "step": step, "stage": stage_idx + 1, "stage_text": task,
                    "pos": [round(px), round(py), round(pz)],
                    "hint": summ.get("stage_hint"),
                    "warnings": summ.get("warnings"),
                    "inv": summ.get("inventory_counts"),
                    "held": summ.get("held"),
                    "table_pos": _table_pos,
                    "open_container": st.get("open_container"),
                    "candidates": {k: v[0] for k, v in cands.items()},
                    "probs": probs, "pick": pick,
                    "conf": ans.get("act", {}).get("confidence"),
                    "done_p": round(done_p, 3),
                    "result": res.get("status"),
                    "error": res.get("error"),
                    "msg": res.get("message")}) + "\n")
        except Exception:
            pass

        # remember cells we built so they never come back as mine/pickup bait
        if ok:
            if pick and pick.startswith("tower up"):
                cx, cz = math.floor(px), math.floor(pz)
                placed_cols.append((cx, cz, math.floor(py) - 1,
                                    math.floor(py) + 9))
            elif pick and pick.startswith("place ") and payload.get("face") == "up":
                placed_cells.add((int(payload["x"]), int(payload["y"]) + 1,
                                  int(payload["z"])))
                if "crafting_table" in pick:
                    _table_pos["p"] = (int(payload["x"]),
                                       int(payload["y"]) + 1,
                                       int(payload["z"]))
                    _save_table_pos(_table_pos)
                placed_names.add(pick.split(" ", 1)[1].split(" @")[0]
                                 .replace(" beside you", ""))
            elif pick and pick.startswith("use "):
                _use_pending[0] = pick
            elif pick and (pick.startswith("furnace ") or pick.startswith("craft ")):
                _use_pending[0] = None
            elif pick == "close screen" and _use_pending[0]:
                _use_cd[_use_pending[0]] = step + 15
                # type-level cooldown too — stops ping-ponging between two
                # tables/screens when the real goal isn't container use
                _pre = _use_pending[0].split(" @")[0]
                _use_cd[_pre] = step + 15
                _use_pending[0] = None

    print(f"[jev-agent] hit MAX_STEPS={MAX_STEPS}", flush=True)


if __name__ == "__main__":
    main()
