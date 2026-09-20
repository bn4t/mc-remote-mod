package io.github.bn4t.mcremote.state;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.screens.Screen;
import net.minecraft.client.multiplayer.ClientLevel;
import net.minecraft.client.multiplayer.ServerData;
import net.minecraft.client.player.LocalPlayer;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.effect.MobEffectInstance;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.LivingEntity;
import net.minecraft.world.entity.item.ItemEntity;
import net.minecraft.world.entity.monster.Enemy;
import net.minecraft.world.entity.player.Player;
import net.minecraft.world.inventory.AbstractCraftingMenu;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.block.state.properties.Property;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.EntityHitResult;
import net.minecraft.world.phys.HitResult;

import java.util.ArrayList;
import java.util.List;

/**
 * Builds the structured world snapshot from the client-side view:
 * everything here is exactly what the connected server has sent the client.
 */
public class StateCollector {
	private final EventLog events;
	private boolean wasAlive = true;

	public StateCollector(EventLog events) {
		this.events = events;
	}

	/** Runs every client tick: detects lifecycle transitions worth reporting. */
	public void onTick(Minecraft mc) {
		LocalPlayer p = mc.player;
		boolean alive = p != null && p.isAlive();
		if (p != null && wasAlive && !alive) {
			events.add("died", new JsonObject());
		}
		if (p != null && !wasAlive && alive) {
			events.add("respawned", new JsonObject());
		}
		wasAlive = alive;
	}

	/** Must be called on the client thread. */
	public JsonObject snapshot(Minecraft mc, int blockRadius, int blocksBelow, int blocksAbove) {
		JsonObject out = new JsonObject();
		out.addProperty("ok", true);
		if (mc.player == null || mc.level == null) {
			out.addProperty("in_world", false);
			Screen screen = mc.gui.screen();
			out.addProperty("screen", screen != null ? screen.getClass().getSimpleName() : null);
			return out;
		}
		out.addProperty("in_world", true);
		out.add("player", player(mc));
		out.add("world", world(mc));
		out.add("inventory", inventory(mc));
		out.add("entities", entities(mc, blockRadius > 0 ? Math.max(blockRadius, 32) : 32));
		out.add("looking_at", lookingAt(mc));
		if (blockRadius > 0) {
			out.add("blocks", blocks(mc.level, mc.player.blockPosition(), blockRadius, blocksBelow, blocksAbove));
		}
		JsonObject open = openContainer(mc);
		if (open != null) out.add("open_container", open);
		out.addProperty("latest_event_seq", events.latestSeq());
		return out;
	}

	private JsonObject player(Minecraft mc) {
		LocalPlayer p = mc.player;
		JsonObject o = new JsonObject();
		o.addProperty("name", p.getName().getString());
		o.addProperty("uuid", p.getUUID().toString());
		o.addProperty("x", p.getX());
		o.addProperty("y", p.getY());
		o.addProperty("z", p.getZ());
		o.addProperty("yaw", p.getYRot());
		o.addProperty("pitch", p.getXRot());
		o.addProperty("on_ground", p.onGround());
		o.addProperty("health", p.getHealth());
		o.addProperty("max_health", p.getMaxHealth());
		o.addProperty("food", p.getFoodData().getFoodLevel());
		o.addProperty("saturation", p.getFoodData().getSaturationLevel());
		o.addProperty("air", p.getAirSupply());
		o.addProperty("max_air", p.getMaxAirSupply());
		o.addProperty("xp_level", p.experienceLevel);
		o.addProperty("xp_progress", p.experienceProgress);
		o.addProperty("alive", p.isAlive());
		o.addProperty("sprinting", p.isSprinting());
		o.addProperty("sneaking", p.isCrouching());
		o.addProperty("swimming", p.isSwimming());
		o.addProperty("in_water", p.isInWater());
		o.addProperty("on_fire", p.isOnFire());
		o.addProperty("fall_distance", p.fallDistance);
		o.addProperty("gamemode", mc.gameMode != null ? mc.gameMode.getPlayerMode().getName() : "unknown");
		o.addProperty("selected_slot", p.getInventory().getSelectedSlot());
		int surfY = mc.level.getHeight(net.minecraft.world.level.levelgen.Heightmap.Types.MOTION_BLOCKING,
				p.getBlockX(), p.getBlockZ());
		o.addProperty("surface_y", surfY);
		o.addProperty("sky_above", p.getBlockY() >= surfY);
		o.add("terrain", terrain(mc, p));
		JsonArray effects = new JsonArray();
		for (MobEffectInstance e : p.getActiveEffects()) {
			JsonObject eff = new JsonObject();
			eff.addProperty("id", BuiltInRegistries.MOB_EFFECT.getKey(e.getEffect().value()).toString());
			eff.addProperty("amplifier", e.getAmplifier());
			eff.addProperty("ticks_left", e.getDuration());
			effects.add(eff);
		}
		o.add("effects", effects);
		ItemStack off = p.getOffhandItem();
		if (!off.isEmpty()) o.add("offhand", item(off, -1));
		o.add("boss_bars", bossBars(mc));
		return o;
	}

	private static java.lang.reflect.Field bossEventsField;
	private static boolean bossFieldTried;

	private static JsonArray bossBars(Minecraft mc) {
		JsonArray arr = new JsonArray();
		try {
			if (!bossFieldTried) {
				bossFieldTried = true;
				bossEventsField = net.minecraft.client.gui.components.BossHealthOverlay.class
						.getDeclaredField("events");
				bossEventsField.setAccessible(true);
			}
			if (bossEventsField == null) return arr;
			Object overlay = mc.gui.hud.getBossOverlay();
			java.util.Map<?, ?> events =
					(java.util.Map<?, ?>) bossEventsField.get(overlay);
			for (Object ev : events.values()) {
				net.minecraft.world.BossEvent be = (net.minecraft.world.BossEvent) ev;
				JsonObject b = new JsonObject();
				b.addProperty("name", be.getName().getString());
				b.addProperty("progress", be.getProgress());
				arr.add(b);
			}
		} catch (Throwable ignored) {}
		return arr;
	}

	/** Surface-height slice around the player: an 9x9 grid of surface_y - player_y
	 * (negative = ground drops away) plus per-compass-direction drops at +4/+8. */
	private JsonObject terrain(Minecraft mc, LocalPlayer p) {
		JsonObject t = new JsonObject();
		int px = p.getBlockX(), py = p.getBlockY(), pz = p.getBlockZ();
		JsonArray hmap = new JsonArray();
		for (int dz = -4; dz <= 4; dz++) {
			JsonArray row = new JsonArray();
			for (int dx = -4; dx <= 4; dx++) {
				int h = mc.level.getHeight(
						net.minecraft.world.level.levelgen.Heightmap.Types.MOTION_BLOCKING,
						px + dx, pz + dz);
				row.add(h - py);
			}
			hmap.add(row);
		}
		t.add("hmap", hmap);
		JsonObject dirs = new JsonObject();
		int[][] vec = {{0, -1}, {0, 1}, {1, 0}, {-1, 0}};
		String[] names = {"north", "south", "east", "west"};
		for (int i = 0; i < 4; i++) {
			JsonObject d = new JsonObject();
			for (int dist : new int[]{4, 8}) {
				int h = mc.level.getHeight(
						net.minecraft.world.level.levelgen.Heightmap.Types.MOTION_BLOCKING,
						px + vec[i][0] * dist, pz + vec[i][1] * dist);
				d.addProperty("y" + dist, h - py);
			}
			dirs.add(names[i], d);
		}
		t.add("dirs", dirs);
		return t;
	}

	private JsonObject world(Minecraft mc) {
		ClientLevel level = mc.level;
		JsonObject o = new JsonObject();
		o.addProperty("dimension", level.dimension().identifier().toString());
		o.addProperty("game_time", level.getLevelData().getGameTime());
		o.addProperty("day_time", level.getOverworldClockTime() % 24000L);
		o.addProperty("raining", level.isRaining());
		o.addProperty("thundering", level.isThundering());
		o.addProperty("difficulty", level.getDifficulty().name().toLowerCase());
		ServerData server = mc.getCurrentServer();
		o.addProperty("server", mc.hasSingleplayerServer() ? "singleplayer"
				: (server != null ? server.ip : "unknown"));
		BlockPos pos = mc.player.blockPosition();
		level.getBiome(pos).unwrapKey().ifPresent(k -> o.addProperty("biome", k.identifier().toString()));
		BlockPos sp = level.getRespawnData().pos();
		if (sp != null) {
			JsonObject spawn = new JsonObject();
			spawn.addProperty("x", sp.getX());
			spawn.addProperty("y", sp.getY());
			spawn.addProperty("z", sp.getZ());
			o.add("spawn", spawn);
		}
		return o;
	}

	private JsonObject inventory(Minecraft mc) {
		LocalPlayer p = mc.player;
		JsonObject inv = new JsonObject();
		JsonArray items = new JsonArray();
		var inventory = p.getInventory();
		for (int i = 0; i < inventory.getContainerSize(); i++) {
			ItemStack stack = inventory.getItem(i);
			if (stack.isEmpty()) continue;
			items.add(item(stack, i));
		}
		inv.add("items", items);
		return inv;
	}

	private JsonObject item(ItemStack stack, int slot) {
		JsonObject o = new JsonObject();
		o.addProperty("slot", slot);
		o.addProperty("id", BuiltInRegistries.ITEM.getKey(stack.getItem()).toString());
		o.addProperty("count", stack.getCount());
		o.addProperty("name", stack.getHoverName().getString());
		if (stack.isDamageableItem()) {
			o.addProperty("damage", stack.getDamageValue());
			o.addProperty("max_damage", stack.getMaxDamage());
		}
		return o;
	}

	private JsonObject openContainer(Minecraft mc) {
		LocalPlayer p = mc.player;
		if (p.containerMenu == p.inventoryMenu) return null;
		JsonObject o = new JsonObject();
		o.addProperty("container_id", p.containerMenu.containerId);
		if (p.containerMenu instanceof AbstractCraftingMenu acm)
			o.addProperty("crafting_grid", acm.getGridWidth() + "x" + acm.getGridHeight());
		Screen screen = mc.gui.screen();
		if (screen != null) o.addProperty("title", screen.getTitle().getString());
		JsonArray slots = new JsonArray();
		for (int i = 0; i < p.containerMenu.slots.size(); i++) {
			ItemStack stack = p.containerMenu.slots.get(i).getItem();
			if (stack.isEmpty()) continue;
			slots.add(item(stack, i));
		}
		o.add("slots", slots);
		return o;
	}

	private JsonArray entities(Minecraft mc, double radius) {
		LocalPlayer p = mc.player;
		JsonArray arr = new JsonArray();
		double r2 = radius * radius;
		for (Entity e : mc.level.entitiesForRendering()) {
			if (e == p) continue;
			double d2 = e.distanceToSqr(p);
			if (d2 > r2) continue;
			JsonObject o = new JsonObject();
			o.addProperty("id", e.getId());
			o.addProperty("type", BuiltInRegistries.ENTITY_TYPE.getKey(e.getType()).toString());
			String name = e.hasCustomName() ? e.getCustomName().getString() : null;
			if (name != null) o.addProperty("name", name);
			o.addProperty("x", e.getX());
			o.addProperty("y", e.getY());
			o.addProperty("z", e.getZ());
			o.addProperty("distance", Math.sqrt(d2));
			if (e instanceof LivingEntity le) {
				o.addProperty("health", le.getHealth());
				o.addProperty("max_health", le.getMaxHealth());
			}
			if (e instanceof Player) o.addProperty("player", true);
			if (e instanceof Enemy) o.addProperty("hostile", true);
			if (e instanceof ItemEntity ie) {
				ItemStack stack = ie.getItem();
				o.addProperty("item", BuiltInRegistries.ITEM.getKey(stack.getItem()).toString());
				o.addProperty("item_count", stack.getCount());
			}
			arr.add(o);
		}
		return arr;
	}

	private JsonObject lookingAt(Minecraft mc) {
		HitResult hit = mc.hitResult;
		if (hit == null || hit.getType() == HitResult.Type.MISS) return null;
		JsonObject o = new JsonObject();
		if (hit instanceof BlockHitResult bh) {
			o.addProperty("type", "block");
			BlockPos pos = bh.getBlockPos();
			o.addProperty("x", pos.getX());
			o.addProperty("y", pos.getY());
			o.addProperty("z", pos.getZ());
			o.addProperty("face", bh.getDirection().getName());
			BlockState state = mc.level.getBlockState(pos);
			o.addProperty("block", stateName(state));
		} else if (hit instanceof EntityHitResult eh) {
			o.addProperty("type", "entity");
			o.addProperty("entity_id", eh.getEntity().getId());
			o.addProperty("entity_type", BuiltInRegistries.ENTITY_TYPE.getKey(eh.getEntity().getType()).toString());
		}
		return o;
	}

	/**
	 * Compact block grid centred on the player, palette-encoded.
	 * data is a flat index array in y,z,x order: idx = (y*sizeZ + z)*sizeX + x.
	 */
	public JsonObject blocks(ClientLevel level, BlockPos center, int radius, int below, int above) {
		int sizeX = radius * 2 + 1;
		int sizeZ = radius * 2 + 1;
		int sizeY = below + above + 1;
		int ox = center.getX() - radius;
		int oy = center.getY() - below;
		int oz = center.getZ() - radius;

		List<String> palette = new ArrayList<>();
		palette.add("minecraft:air"); // index 0 always air-ish
		java.util.Map<String, Integer> paletteIdx = new java.util.HashMap<>();
		paletteIdx.put("minecraft:air", 0);
		paletteIdx.put("minecraft:cave_air", 0);
		paletteIdx.put("minecraft:void_air", 0);

		JsonArray data = new JsonArray();
		List<JsonObject> notable = new ArrayList<>();
		BlockPos.MutableBlockPos pos = new BlockPos.MutableBlockPos();
		for (int y = 0; y < sizeY; y++) {
			for (int z = 0; z < sizeZ; z++) {
				for (int x = 0; x < sizeX; x++) {
					pos.set(ox + x, oy + y, oz + z);
					BlockState state = level.getBlockState(pos);
					String name = state.isAir() ? "minecraft:air" : stateName(state);
					Integer idx = paletteIdx.get(name);
					if (idx == null) {
						idx = palette.size();
						palette.add(name);
						paletteIdx.put(name, idx);
					}
					if (isNotable(name)) {
						JsonObject n = new JsonObject();
						n.addProperty("x", pos.getX());
						n.addProperty("y", pos.getY());
						n.addProperty("z", pos.getZ());
						n.addProperty("block", name);
						notable.add(n);
					}
					data.add(idx);
				}
			}
		}
		JsonObject o = new JsonObject();
		o.addProperty("origin_x", ox);
		o.addProperty("origin_y", oy);
		o.addProperty("origin_z", oz);
		o.addProperty("size_x", sizeX);
		o.addProperty("size_y", sizeY);
		o.addProperty("size_z", sizeZ);
		// keep the closest interesting blocks: scan order is bottom-up, so
		// without this deep ores would starve surface features under the cap
		notable.sort((a, b) -> Double.compare(distSq(a, center), distSq(b, center)));
		while (notable.size() > 256) notable.remove(notable.size() - 1);

		JsonArray pal = new JsonArray();
		for (String s : palette) pal.add(s);
		o.add("palette", pal);
		o.add("data", data);
		JsonArray notableArr = new JsonArray();
		for (JsonObject n : notable) notableArr.add(n);
		o.add("notable", notableArr);
		return o;
	}

	private static double distSq(JsonObject b, BlockPos c) {
		double dx = b.get("x").getAsDouble() - c.getX();
		double dy = b.get("y").getAsDouble() - c.getY();
		double dz = b.get("z").getAsDouble() - c.getZ();
		return dx * dx + dy * dy + dz * dz;
	}

	public static String stateName(BlockState state) {
		StringBuilder sb = new StringBuilder();
		sb.append(BuiltInRegistries.BLOCK.getKey(state.getBlock()));
		var props = state.getProperties();
		if (!props.isEmpty()) {
			sb.append('[');
			boolean first = true;
			for (Property<?> prop : props) {
				if (!first) sb.append(',');
				first = false;
				sb.append(prop.getName()).append('=').append(valueName(state, prop));
			}
			sb.append(']');
		}
		return sb.toString();
	}

	private static <T extends Comparable<T>> String valueName(BlockState state, Property<T> prop) {
		return prop.getName(state.getValue(prop));
	}

	private static boolean isNotable(String name) {
		for (String k : NOTABLE_KEYS) {
			if (name.contains(k)) return true;
		}
		return false;
	}

	private static final String[] NOTABLE_KEYS = {
			"log", "stem", "hyphae", "ore", "chest", "barrel", "furnace", "smoker", "blast", "table", "anvil",
			"door", "bed", "portal", "spawner", "torch", "ladder",
			"vine", "fire", "tnt", "sign", "bell", "campfire", "loom", "smithing",
			"brewing", "enchanting", "grindstone", "stonecutter", "cartography",
			"composter", "beacon", "conduit", "rail", "button", "lever", "trapdoor",
			"fence_gate", "wheat", "carrot", "potato", "beetroot", "cocoa", "cactus",
			"sugar_cane", "magma", "obsidian", "ice", "snow_block", "powder_snow",
			"stone", "deepslate", "andesite", "granite", "diorite", "gravel",
			"sand", "clay", "terracotta", "netherrack", "tuff", "calcite",
			"cobweb", "sweet_berry", "pointed_dripstone", "sculk", "end_rod", "hopper",
			"dropper", "dispenser", "observer", "piston", "redstone_wire", "repeater",
			"comparator", "lectern", "jukebox", "note_block", "dragon_egg", "respawn_anchor"
	};
}
