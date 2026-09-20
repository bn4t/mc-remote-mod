package io.github.bn4t.mcremote.control;

import com.google.gson.JsonObject;
import net.minecraft.client.Minecraft;
import net.minecraft.client.Options;
import net.minecraft.client.gui.screens.inventory.InventoryScreen;
import net.minecraft.client.gui.screens.recipebook.RecipeCollection;
import net.minecraft.client.player.LocalPlayer;
import net.minecraft.core.BlockPos;
import net.minecraft.core.Direction;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.InteractionHand;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.player.StackedItemContents;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.crafting.display.RecipeDisplayEntry;
import net.minecraft.world.item.crafting.display.SlotDisplayContext;
import net.minecraft.world.level.ClipContext;
import net.minecraft.world.inventory.AbstractCraftingMenu;
import net.minecraft.world.inventory.ContainerInput;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.HitResult;
import net.minecraft.world.phys.Vec3;

/** Parses action JSON into GameAction instances executed on the client thread. */
public final class Actions {

	public static GameAction parse(long id, JsonObject req) {
		String type = req.has("type") ? req.get("type").getAsString() : "";
		return switch (type) {
			case "look" -> new Look(id, req);
			case "move" -> new Move(id, req);
			case "jump" -> new TimedInput(id, "jump", 2);
			case "attack" -> new Attack(id, req);
			case "use" -> new Use(id, req);
			case "interact_entity" -> new InteractEntity(id, req);
			case "mine" -> new Mine(id, req);
			case "use_on_block" -> new UseOnBlock(id, req);
			case "walk_to" -> new WalkTo(id, req);
			case "select_slot" -> new SelectSlot(id, req);
			case "drop" -> new Drop(id, req);
			case "swap_hands" -> new KeyPress(id, o -> o.keySwapOffhand);
			case "say" -> new Say(id, req);
			case "respawn" -> new Respawn(id);
			case "close_screen" -> new CloseScreen(id);
			case "click_slot" -> new ClickSlot(id, req);
			case "craft" -> new Craft(id, req);
			case "open_inventory" -> new OpenInventory(id);
			case "wait" -> new Wait(id, req);
			default -> null;
		};
	}

	private static double getD(JsonObject o, String k, double def) {
		return o.has(k) ? o.get(k).getAsDouble() : def;
	}

	private static int getI(JsonObject o, String k, int def) {
		return o.has(k) ? o.get(k).getAsInt() : def;
	}

	private static boolean getB(JsonObject o, String k, boolean def) {
		return o.has(k) ? o.get(k).getAsBoolean() : def;
	}

	private static LocalPlayer requirePlayer(Minecraft mc) {
		if (mc.player == null || mc.level == null) return null;
		return mc.player;
	}

	// --- actions ---

	static class Look extends GameAction {
		private final float yaw, pitch;
		Look(long id, JsonObject r) {
			super(id, "look");
			yaw = (float) getD(r, "yaw", 0);
			pitch = (float) getD(r, "pitch", 0);
		}
		@Override protected boolean tick(Minecraft mc) {
			LocalPlayer p = requirePlayer(mc);
			if (p == null) { fail("not in world"); return true; }
			p.setYRot(yaw);
			p.setXRot(pitch);
			p.setYHeadRot(yaw);
			return true;
		}
	}

	static class Move extends GameAction {
		private final float forward, strafe;
		private final boolean sprint, sneak, jump;
		private final Float yaw, pitch;
		private int ticksLeft;
		Move(long id, JsonObject r) {
			super(id, "move");
			forward = (float) getD(r, "forward", 0);
			strafe = (float) getD(r, "strafe", 0);
			sprint = getB(r, "sprint", false);
			sneak = getB(r, "sneak", false);
			jump = getB(r, "jump", false);
			yaw = r.has("yaw") ? (float) getD(r, "yaw", 0) : null;
			pitch = r.has("pitch") ? (float) getD(r, "pitch", 0) : null;
			double seconds = getD(r, "seconds", 0);
			ticksLeft = seconds <= 0 ? Integer.MAX_VALUE : (int) (seconds * 20);
		}
		@Override protected boolean tick(Minecraft mc) {
			LocalPlayer p = requirePlayer(mc);
			if (p == null) { fail("not in world"); return true; }
			if (yaw != null) { p.setYRot(yaw); p.setYHeadRot(yaw); }
			if (pitch != null) p.setXRot(pitch);
			RemoteInput in = McRemoteHolder.actions().input();
			in.set(forward, strafe, jump, sneak, sprint);
			return --ticksLeft <= 0;
		}
	}

	/** Pulse or hold a single input flag for N ticks. */
	static class TimedInput extends GameAction {
		private final String flag;
		private int ticksLeft;
		TimedInput(long id, String flag, int ticks) {
			super(id, flag);
			this.flag = flag;
			this.ticksLeft = ticks;
		}
		@Override protected boolean tick(Minecraft mc) {
			RemoteInput in = McRemoteHolder.actions().input();
			switch (flag) {
				case "jump" -> in.jump = true;
			}
			return --ticksLeft <= 0;
		}
	}

	static class Attack extends GameAction {
		private final Integer entityId;
		private final int times;
		private int done, ticksLeft;
		Attack(long id, JsonObject r) {
			super(id, "attack");
			entityId = r.has("entity_id") ? getI(r, "entity_id", -1) : null;
			times = Math.max(1, getI(r, "times", 1));
			ticksLeft = (int) (getD(r, "seconds", 10) * 20);
		}
		@Override protected boolean tick(Minecraft mc) {
			LocalPlayer p = requirePlayer(mc);
			if (p == null) { fail("not in world"); return true; }
			if (--ticksLeft <= 0) { fail("timeout"); return true; }
			if (entityId == null) {
				// swing at whatever we're looking at
				McRemoteHolder.actions().input().attack = true;
				return ++done >= times;
			}
			Entity e = mc.level.getEntity(entityId);
			if (e == null || !e.isAlive()) { fail("entity gone"); return true; }
			if (!e.isPickable() || p.distanceTo(e) > p.entityInteractionRange() + 1) {
				fail("entity out of reach"); return true;
			}
			float[] yp = aimAt(p.getEyePosition(), e.getEyePosition());
			p.setYRot(yp[0]); p.setXRot(yp[1]); p.setYHeadRot(yp[0]);
			if (p.getAttackStrengthScale(0f) >= 1f) {
				mc.gameMode.attack(p, e);
				p.swing(InteractionHand.MAIN_HAND);
				done++;
			}
			return done >= times;
		}
	}

	static class Use extends GameAction {
		private int ticksLeft;
		Use(long id, JsonObject r) {
			super(id, "use");
			double seconds = getD(r, "seconds", 0.35);
			ticksLeft = Math.max(1, (int) (seconds * 20));
		}
		@Override protected boolean tick(Minecraft mc) {
			if (requirePlayer(mc) == null) { fail("not in world"); return true; }
			McRemoteHolder.actions().input().use = true;
			return --ticksLeft <= 0;
		}
	}

	static class InteractEntity extends GameAction {
		private final int entityId;
		InteractEntity(long id, JsonObject r) {
			super(id, "interact_entity");
			entityId = getI(r, "entity_id", -1);
		}
		@Override protected boolean tick(Minecraft mc) {
			LocalPlayer p = requirePlayer(mc);
			if (p == null) { fail("not in world"); return true; }
			Entity e = mc.level.getEntity(entityId);
			if (e == null) { fail("no such entity"); return true; }
			mc.gameMode.interact(p, e, new net.minecraft.world.phys.EntityHitResult(e), InteractionHand.MAIN_HAND);
			return true;
		}
	}

	static class Mine extends GameAction {
		private final BlockPos pos;
		private int ticksLeft;
		Mine(long id, JsonObject r) {
			super(id, "mine");
			pos = new BlockPos(getI(r, "x", 0), getI(r, "y", 0), getI(r, "z", 0));
			ticksLeft = (int) (getD(r, "seconds", 30) * 20);
		}
		private boolean started;
		@Override protected boolean tick(Minecraft mc) {
			LocalPlayer p = requirePlayer(mc);
			if (p == null) { fail("not in world"); return true; }
			if (--ticksLeft <= 0) { fail("timeout"); return true; }
			if (mc.level.getBlockState(pos).isAir()) {
				if (!started) { fail("nothing to mine (air)"); return true; }
				return true;
			}
			started = true;
			double dist = p.getEyePosition().distanceTo(Vec3.atCenterOf(pos));
			if (dist > p.blockInteractionRange() + 1) {
				fail("block out of reach (" + String.format("%.1f", dist) + ")"); return true;
			}
			float[] yp = aimAt(p.getEyePosition(), Vec3.atCenterOf(pos));
			p.setYRot(yp[0]); p.setXRot(yp[1]); p.setYHeadRot(yp[0]);
			McRemoteHolder.actions().input().attack = true;
			return false;
		}
		@Override void succeed() {
			McRemoteHolder.actions().input().attack = false;
			super.succeed();
		}
		@Override void fail(String error) {
			McRemoteHolder.actions().input().attack = false;
			super.fail(error);
		}
	}

	static class UseOnBlock extends GameAction {
		private final BlockPos pos;
		private final Direction face;
		UseOnBlock(long id, JsonObject r) {
			super(id, "use_on_block");
			pos = new BlockPos(getI(r, "x", 0), getI(r, "y", 0), getI(r, "z", 0));
			Direction f = Direction.UP;
			if (r.has("face")) {
				Direction parsed = Direction.byName(r.get("face").getAsString());
				if (parsed != null) f = parsed;
			}
			face = f;
		}
		@Override protected boolean tick(Minecraft mc) {
			LocalPlayer p = requirePlayer(mc);
			if (p == null) { fail("not in world"); return true; }
			Vec3 eye = p.getEyePosition();
			// Aim slightly off-center toward the requested face; the ray hits the
			// face without grazing coplanar neighbours (which a face-centre aim
			// does on flat ground).
			Vec3 target = Vec3.atCenterOf(pos).add(face.getStepX() * 0.25, face.getStepY() * 0.25, face.getStepZ() * 0.25);
			float[] yp = aimAt(eye, target);
			p.setYRot(yp[0]); p.setXRot(yp[1]); p.setYHeadRot(yp[0]);
			HitResult hit = mc.level.clip(new ClipContext(eye, target, ClipContext.Block.OUTLINE, ClipContext.Fluid.NONE, p));
			if (!(hit instanceof BlockHitResult bh) || !bh.getBlockPos().equals(pos)) {
				fail("block face not reachable"); return true;
			}
			mc.gameMode.useItemOn(p, InteractionHand.MAIN_HAND, bh);
			p.swing(InteractionHand.MAIN_HAND);
			return true;
		}
	}

	static class WalkTo extends GameAction {
		private final double tx, tz;
		private final Integer ty;
		private final double arrive;
		private int ticksLeft;
		private java.util.List<BlockPos> path;
		private int idx, repaths, repathCd;
		private double lastX = Double.NaN, lastZ;
		private double goalY = Double.NaN;
		private int stuckTicks;
		WalkTo(long id, JsonObject r) {
			super(id, "walk_to");
			tx = getD(r, "x", 0);
			tz = getD(r, "z", 0);
			ty = r.has("y") ? getI(r, "y", 0) : null;
			arrive = getD(r, "radius", 1.5);
			ticksLeft = (int) (getD(r, "seconds", 90) * 20);
		}
		@Override protected boolean tick(Minecraft mc) {
			LocalPlayer p = requirePlayer(mc);
			if (p == null) { fail("not in world"); return true; }
			if (--ticksLeft <= 0) { clearInput(); fail("timeout"); return true; }
			double dx = tx - p.getX();
			double dz = tz - p.getZ();
			boolean near = dx * dx + dz * dz <= arrive * arrive;
			if (near && ty != null) {
				double want = Double.isNaN(goalY) ? ty : goalY;
				near = Math.abs(p.getY() - want) <= 2.5;
			}
			if (near) { clearInput(); return true; }
			if (path == null) {
				if (repathCd <= 0) {
					repathCd = 15;
					if (++repaths > 10) { clearInput(); fail("stuck"); return true; }
					path = Pathfinder.findPath(mc,
							BlockPos.containing(p.getX(), p.getY(), p.getZ()), tx, tz, arrive, ty);
					goalY = path == null || ty == null ? Double.NaN
							: path.get(path.size() - 1).getY();
					idx = 0;
				}
				repathCd--;
			}
			if (path == null) {
				// no route found — fall back to straight-line steering + auto-jump
				float yaw = (float) Math.toDegrees(Math.atan2(-dx, dz));
				p.setYRot(yaw);
				p.setYHeadRot(yaw);
				McRemoteHolder.actions().input().set(1f, 0f,
						p.horizontalCollision && p.onGround(), false, true);
			} else {
				// advance past reached nodes; bail if we fell off the path
				while (idx < path.size()) {
					BlockPos n = path.get(idx);
					if (Math.abs(p.getY() - n.getY()) > 1.5) break;
					if (Math.hypot(n.getX() + 0.5 - p.getX(), n.getZ() + 0.5 - p.getZ()) < 0.6) {
						idx++;
						continue;
					}
					break;
				}
				if (idx >= path.size()) { path = null; return false; }
				BlockPos n = path.get(idx);
				float yaw = (float) Math.toDegrees(
						Math.atan2(-(n.getX() + 0.5 - p.getX()), n.getZ() + 0.5 - p.getZ()));
				p.setYRot(yaw);
				p.setYHeadRot(yaw);
				boolean jump = n.getY() > Math.floor(p.getY())
						|| (p.horizontalCollision && p.onGround());
				McRemoteHolder.actions().input().set(1f, 0f, jump, false, true);
			}
			if (Double.isNaN(lastX) || p.getX() != lastX || p.getZ() != lastZ) {
				double moved = Double.isNaN(lastX) ? 1
						: Math.hypot(p.getX() - lastX, p.getZ() - lastZ);
				stuckTicks = moved < 0.02 ? stuckTicks + 1 : 0;
				lastX = p.getX(); lastZ = p.getZ();
			}
			if (stuckTicks > 60) {
				path = null;
				stuckTicks = 0;
				if (++repaths > 6) { clearInput(); fail("stuck"); return true; }
			}
			return false;
		}
		@Override void succeed() { clearInput(); super.succeed(); }
		@Override void fail(String e) { clearInput(); super.fail(e); }
		private void clearInput() { McRemoteHolder.actions().input().set(0, 0, false, false, false); }
	}

	static class SelectSlot extends GameAction {
		private final int slot;
		SelectSlot(long id, JsonObject r) {
			super(id, "select_slot");
			slot = getI(r, "slot", 0);
		}
		@Override protected boolean tick(Minecraft mc) {
			LocalPlayer p = requirePlayer(mc);
			if (p == null) { fail("not in world"); return true; }
			if (slot < 0 || slot > 8) { fail("slot must be 0-8"); return true; }
			p.getInventory().setSelectedSlot(slot);
			return true;
		}
	}

	static class Drop extends GameAction {
		private final boolean all;
		Drop(long id, JsonObject r) {
			super(id, "drop");
			all = getB(r, "all", false);
		}
		@Override protected boolean tick(Minecraft mc) {
			LocalPlayer p = requirePlayer(mc);
			if (p == null) { fail("not in world"); return true; }
			p.drop(all);
			p.swing(InteractionHand.MAIN_HAND);
			return true;
		}
	}

	static class KeyPress extends GameAction {
		private final java.util.function.Function<Options, net.minecraft.client.KeyMapping> key;
		private int ticks = 2;
		KeyPress(long id, java.util.function.Function<Options, net.minecraft.client.KeyMapping> key) {
			super(id, "key_press");
			this.key = key;
		}
		@Override protected boolean tick(Minecraft mc) {
			key.apply(mc.options).setDown(true);
			return --ticks <= 0;
		}
	}

	static class Say extends GameAction {
		private final String message;
		Say(long id, JsonObject r) {
			super(id, "say");
			message = r.has("message") ? r.get("message").getAsString() : "";
		}
		@Override protected boolean tick(Minecraft mc) {
			LocalPlayer p = requirePlayer(mc);
			if (p == null) { fail("not in world"); return true; }
			if (message.isEmpty()) { fail("empty message"); return true; }
			p.connection.sendChat(message);
			return true;
		}
	}

	static class Respawn extends GameAction {
		Respawn(long id) { super(id, "respawn"); }
		@Override protected boolean tick(Minecraft mc) {
			LocalPlayer p = requirePlayer(mc);
			if (p == null) { fail("not in world"); return true; }
			p.respawn();
			return true;
		}
	}

	static class CloseScreen extends GameAction {
		CloseScreen(long id) { super(id, "close_screen"); }
		@Override protected boolean tick(Minecraft mc) {
			if (mc.gui.screen() != null) mc.gui.setScreen(null);
			return true;
		}
	}

	static class ClickSlot extends GameAction {
		private final int slot, button;
		private final ContainerInput clickType;
		ClickSlot(long id, JsonObject r) {
			super(id, "click_slot");
			slot = getI(r, "slot", 0);
			button = getI(r, "button", 0);
			String t = r.has("click") ? r.get("click").getAsString() : "pickup";
			clickType = switch (t) {
				case "quick_move" -> ContainerInput.QUICK_MOVE;
				case "swap" -> ContainerInput.SWAP;
				case "clone" -> ContainerInput.CLONE;
				case "throw" -> ContainerInput.THROW;
				case "pickup_all" -> ContainerInput.PICKUP_ALL;
				default -> ContainerInput.PICKUP;
			};
		}
		@Override protected boolean tick(Minecraft mc) {
			LocalPlayer p = requirePlayer(mc);
			if (p == null) { fail("not in world"); return true; }
			mc.gameMode.handleContainerInput(p.containerMenu.containerId, slot, button, clickType, p);
			return true;
		}
	}

	/** Opens the player inventory screen (shows the 2x2 grid + recipe book). */
	static class OpenInventory extends GameAction {
		OpenInventory(long id) { super(id, "open_inventory"); }
		@Override protected boolean tick(Minecraft mc) {
			LocalPlayer p = requirePlayer(mc);
			if (p == null) { fail("not in world"); return true; }
			mc.gui.setScreen(new InventoryScreen(p));
			return true;
		}
	}

	/**
	 * Craft an item via the recipe book: finds a craftable recipe whose result
	 * matches the requested item, places it into the current crafting grid and
	 * shift-clicks the result. Uses the always-open inventory 2x2 grid, or an
	 * open crafting-table container (3x3) if one is on screen.
	 * Params: item (name, e.g. "oak_planks" or "minecraft:stick"),
	 *         all (bool, place all matching ingredients for max crafts).
	 */
	static class Craft extends GameAction {
		private final String want;
		private final boolean all;
		private int stage = 0, ticks = 0, retries = 0;
		private int before;
		private String foundName;
		Craft(long id, JsonObject r) {
			super(id, "craft");
			want = (r.has("item") ? r.get("item").getAsString() : "").toLowerCase();
			all = getB(r, "all", true);
		}
		private static int countItem(LocalPlayer p, String want) {
			int n = 0;
			var inv = p.getInventory();
			for (int i = 0; i < inv.getContainerSize(); i++) {
				ItemStack s = inv.getItem(i);
				if (!s.isEmpty() && nameMatches(BuiltInRegistries.ITEM.getKey(s.getItem()).toString(), want))
					n += s.getCount();
			}
			return n;
		}
		private static boolean nameMatches(String id, String want) {
			String base = id.contains(":") ? id.substring(id.indexOf(':') + 1) : id;
			return base.equals(want) || id.equals(want);
		}
		@Override protected boolean tick(Minecraft mc) {
			LocalPlayer p = requirePlayer(mc);
			if (p == null) { fail("not in world"); return true; }
			switch (stage) {
				case 0 -> {
					var menu = p.containerMenu;
					if (!(menu instanceof AbstractCraftingMenu cm)) {
						fail("current container is not a crafting menu (close_screen first)");
						return true;
					}
					int grid = cm.getGridWidth() * cm.getGridHeight();
					StackedItemContents sic = new StackedItemContents();
					var inv = p.getInventory();
					for (int i = 0; i < inv.getContainerSize(); i++)
						sic.accountStack(inv.getItem(i));
					cm.fillCraftSlotsStackedContents(sic);
					var ctx = SlotDisplayContext.fromLevel(mc.level);
					RecipeDisplayEntry found = null;
					outer:
					for (RecipeCollection col : p.getRecipeBook().getCollections()) {
						for (RecipeDisplayEntry e : col.getRecipes()) {
							int need = e.craftingRequirements().map(java.util.List::size).orElse(0);
							if (need > grid || !e.canCraft(sic)) continue;
							for (ItemStack r : e.resultItems(ctx)) {
								String nm = BuiltInRegistries.ITEM.getKey(r.getItem()).toString();
								if (nameMatches(nm, want)) {
									found = e; foundName = nm; break outer;
								}
							}
						}
					}
					if (found == null) {
						fail("no craftable recipe for '" + want + "' (missing ingredients, "
								+ "or needs a " + (grid >= 9 ? "bigger" : "3x3 crafting table") + " grid)");
						return true;
					}
					before = countItem(p, want);
					mc.gameMode.handlePlaceRecipe(menu.containerId, found.id(), all);
					stage = 1; ticks = 0;
				}
				case 1 -> {
					if (++ticks >= 4) {
						var menu = p.containerMenu;
						int resultIdx = (menu instanceof AbstractCraftingMenu cm)
								? cm.getResultSlot().index : 0;
						mc.gameMode.handleContainerInput(menu.containerId, resultIdx, 0,
								ContainerInput.QUICK_MOVE, p);
						stage = 2; ticks = 0;
					}
				}
				case 2 -> {
					if (++ticks >= 8) {
						int after = countItem(p, want);
						if (after > before) {
							succeed("crafted " + (after - before) + "x " + foundName);
							return true;
						}
						if (++retries < 2) { stage = 0; return false; }
						fail("craft produced no " + want);
						return true;
					}
				}
			}
			return false;
		}
	}

	static class Wait extends GameAction {
		private int ticksLeft;
		Wait(long id, JsonObject r) {
			super(id, "wait");
			ticksLeft = Math.max(1, (int) (getD(r, "seconds", 1) * 20));
		}
		@Override protected boolean tick(Minecraft mc) {
			return --ticksLeft <= 0;
		}
	}
}
