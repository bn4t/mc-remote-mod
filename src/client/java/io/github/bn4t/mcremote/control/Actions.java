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
import net.minecraft.world.inventory.AbstractFurnaceMenu;
import net.minecraft.world.inventory.ContainerInput;
import net.minecraft.world.item.BlockItem;
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
			case "smelt" -> new Smelt(id, req);
			case "pillar" -> new Pillar(id, req);
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

	/** Current server tick rate — action budgets are wall-clock, not ticks. */
	private static float tps() {
		var l = Minecraft.getInstance().level;
		return l == null ? 20f : Math.max(1f, l.tickRateManager().tickrate());
	}

	/** seconds param -> tick budget at the CURRENT tick rate. */
	private static int secs(JsonObject o, String k, double def) {
		return (int) Math.ceil(getD(o, k, def) * tps());
	}

	private static LocalPlayer requirePlayer(Minecraft mc) {
		if (mc.player == null || mc.level == null) return null;
		return mc.player;
	}

	static boolean nameMatches(String id, String want) {
		String base = id.contains(":") ? id.substring(id.indexOf(':') + 1) : id;
		return base.equals(want) || id.equals(want);
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
			ticksLeft = seconds <= 0 ? Integer.MAX_VALUE : secs(r, "seconds", 0);
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
			ticksLeft = secs(r, "seconds", 10);
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
			ticksLeft = Math.max(1, secs(r, "seconds", 0.35));
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
			ticksLeft = secs(r, "seconds", 30);
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
			ticksLeft = secs(r, "seconds", 90);
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
					repathCd = (int) (0.75 * tps());
					if (++repaths > 10) { clearInput(); fail("stuck"); return true; }
					path = Pathfinder.findPath(mc,
							BlockPos.containing(p.getX(), p.getY(), p.getZ()), tx, tz, arrive, ty);
					if (path != null && path.isEmpty()) path = null;
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
			double moved = Double.isNaN(lastX) ? 1
					: Math.hypot(p.getX() - lastX, p.getZ() - lastZ);
			stuckTicks = moved < 0.02 ? stuckTicks + 1 : 0;
			lastX = p.getX(); lastZ = p.getZ();
			if (stuckTicks > (int) (3 * tps())) {
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

	/**
	 * Towers up: holds jump and places the held block into the feet cell on
	 * each hop. Used for pit escapes and climbing. Params: blocks (count),
	 * seconds (timeout).
	 */
	static class Pillar extends GameAction {
		private final int want;
		private int ticksLeft, noProgress, placeCalls;
		private String lastWhy = "?", lastRes = "?";
		private double startY;
		private boolean started;
		Pillar(long id, JsonObject r) {
			super(id, "pillar");
			want = getI(r, "blocks", 3);
			ticksLeft = secs(r, "seconds", 20);
		}
		@Override protected boolean tick(Minecraft mc) {
			LocalPlayer p = requirePlayer(mc);
			if (p == null) { fail("not in world"); return true; }
			if (--ticksLeft <= 0) { fail("timeout"); return true; }
			if (!(p.getMainHandItem().getItem() instanceof BlockItem)) {
				fail(started ? "out of blocks" : "hold a placeable block");
				return true;
			}
			if (!started) { started = true; startY = p.getY(); }
			if (p.getY() - startY >= want) { clearInput(); return true; }
			var input = McRemoteHolder.actions().input();
			// clear headroom first: the cells the head occupies at jump apex.
			// Only dig while grounded — at apex the same check would catch the
			// NEXT level's ceiling and steal the placement window.
			if (p.onGround()) for (int up : new int[]{2, 3}) {
				BlockPos cell = BlockPos.containing(p.getX(), p.getY() + up, p.getZ());
				var cs = mc.level.getBlockState(cell);
				if (!cs.isAir() && !cs.getCollisionShape(mc.level, cell).isEmpty()) {
					float[] yp = aimAt(p.getEyePosition(), Vec3.atCenterOf(cell));
					p.setYRot(yp[0]); p.setXRot(yp[1]); p.setYHeadRot(yp[0]);
					input.attack = true;
					input.set(0f, 0f, false, false, false);
					noProgress++;
					if (noProgress > (int) (10 * tps())) { input.attack = false; fail("can't clear headroom"); return true; }
					return false;
				}
			}
			p.setXRot(85f);
			// place into the cell directly beneath the feet; it's only
			// free of the player's bounding box mid-jump. The block attaches to
			// whatever solid face is adjacent — the block below (top face) or a
			// horizontal neighbour (side face), which covers jagged shafts.
			BlockPos target = BlockPos.containing(p.getX(), p.getY() - 1, p.getZ());
			var tgtState = mc.level.getBlockState(target);
			if (!(tgtState.isAir() || tgtState.canBeReplaced())) {
				// below-feet is solid (standing on a ledge / shallow spot):
				// climb by filling the feet cell itself
				target = BlockPos.containing(p.getX(), p.getY(), p.getZ());
				tgtState = mc.level.getBlockState(target);
			}
			boolean placed = false;
			if (tgtState.isAir() || tgtState.canBeReplaced()) {
				if (p.getY() < target.getY() + 1 - 0.001) {
					lastWhy = "waiting apex y=" + String.format("%.2f", p.getY())
							+ " need>" + (target.getY() + 1);
				} else {
				BlockHitResult hit = null;
				BlockPos support = target.below();
				if (!mc.level.getBlockState(support).getCollisionShape(mc.level, support).isEmpty()) {
					hit = new BlockHitResult(Vec3.atCenterOf(support).add(0, 0.5, 0),
							Direction.UP, support, false);
				} else {
					for (Direction dir : Direction.Plane.HORIZONTAL) {
						BlockPos nb = target.relative(dir);
						if (!mc.level.getBlockState(nb).getCollisionShape(mc.level, nb).isEmpty()) {
							Direction face = dir.getOpposite();
							hit = new BlockHitResult(Vec3.atCenterOf(nb).add(
									face.getStepX() * 0.5, face.getStepY() * 0.5,
									face.getStepZ() * 0.5), face, nb, false);
							break;
						}
					}
				}
				if (hit == null) {
					lastWhy = "no face near " + target;
				} else if (hit.getLocation().distanceTo(p.getEyePosition())
						> p.blockInteractionRange() + 1) {
					lastWhy = "hit too far";
				} else {
					var res = mc.gameMode.useItemOn(p, InteractionHand.MAIN_HAND, hit);
					p.swing(InteractionHand.MAIN_HAND);
					lastRes = res + "@" + target;
					placeCalls++;
					placed = true;
				}
				}
			} else {
				lastWhy = "target solid " + tgtState.getBlock();
			}
			// hold jump unconditionally — wedged players may never report
			// on_ground, and held jump re-fires on each landing anyway
			input.set(0f, 0f, true, false, false);
			noProgress = placed ? 0 : noProgress + 1;
			if (noProgress > (int) (4 * tps())) { clearInput();
				fail("can't place: " + lastWhy + " | calls=" + placeCalls + " res=" + lastRes);
				return true; }
			return false;
		}
		private void clearInput() {
			var in = McRemoteHolder.actions().input();
			in.set(0f, 0f, false, false, false);
			in.attack = false;
		}
		@Override void succeed() { clearInput(); super.succeed(); }
		@Override void fail(String e) { clearInput(); super.fail(e); }
	}

	/**
	 * Opens a furnace-family block, deposits `input` items and `fuel`, waits
	 * for smelted output, collects it, closes the screen.
	 * Params: x,y,z (furnace), input (item name), fuel (item name),
	 *         count (default = all deposited), seconds (timeout).
	 */
	static class Smelt extends GameAction {
		private final BlockPos pos;
		private final String input, fuel;
		private final int wantCount;
		private int ticksLeft, collected, target = -1, openWait;
		private boolean openSent, deposited;
		Smelt(long id, JsonObject r) {
			super(id, "smelt");
			pos = new BlockPos(getI(r, "x", 0), getI(r, "y", 0), getI(r, "z", 0));
			input = (r.has("input") ? r.get("input").getAsString() : "").toLowerCase();
			fuel = (r.has("fuel") ? r.get("fuel").getAsString() : "").toLowerCase();
			wantCount = getI(r, "count", 64);
			ticksLeft = secs(r, "seconds", 240);
		}
		@Override protected boolean tick(Minecraft mc) {
			LocalPlayer p = requirePlayer(mc);
			if (p == null) { fail("not in world"); return true; }
			if (--ticksLeft <= 0) { close(mc); fail("timeout"); return true; }
			if (!(p.containerMenu instanceof AbstractFurnaceMenu fm)) {
				if (!openSent) {
					Vec3 eye = p.getEyePosition();
					if (Vec3.atCenterOf(pos).distanceTo(eye) > p.blockInteractionRange() + 1) {
						fail("furnace out of reach"); return true;
					}
					Vec3 hit = Vec3.atCenterOf(pos);
					mc.gameMode.useItemOn(p, InteractionHand.MAIN_HAND,
							new BlockHitResult(hit, Direction.UP, pos, false));
					openSent = true;
					return false;
				}
				if (++openWait > 30) { fail("furnace did not open"); return true; }
				return false;
			}
			if (!deposited) {
				deposited = true;
				int moved = 0;
				// input items route to the ingredient slot on quick-move
				for (int i = AbstractFurnaceMenu.SLOT_COUNT; i < fm.slots.size(); i++) {
					ItemStack s = fm.slots.get(i).getItem();
					if (!s.isEmpty()
							&& nameMatches(BuiltInRegistries.ITEM.getKey(s.getItem()).toString(), input)
							&& moved < wantCount) {
						moved += s.getCount();
						mc.gameMode.handleContainerInput(fm.containerId, i, 0,
								ContainerInput.QUICK_MOVE, p);
					}
				}
				target = Math.min(wantCount, moved);
				if (target <= 0) { close(mc); fail("no " + input + " in inventory"); return true; }
				// fuel may also be smeltable (logs) — place onto the fuel slot
				// explicitly instead of quick-moving
				int needFuel = Math.min(8, target / 8 + 1);
				for (int i = AbstractFurnaceMenu.SLOT_COUNT; i < fm.slots.size()
						&& needFuel > 0; i++) {
					ItemStack s = fm.slots.get(i).getItem();
					if (!s.isEmpty()
							&& nameMatches(BuiltInRegistries.ITEM.getKey(s.getItem()).toString(), fuel)) {
						int n = Math.min(s.getCount(), needFuel);
						mc.gameMode.handleContainerInput(fm.containerId, i, 0,
								ContainerInput.PICKUP, p);
						mc.gameMode.handleContainerInput(fm.containerId,
								AbstractFurnaceMenu.FUEL_SLOT, n < s.getCount() ? 1 : 0,
								ContainerInput.PICKUP, p);
						// return leftovers to their slot
						mc.gameMode.handleContainerInput(fm.containerId, i, 0,
								ContainerInput.PICKUP, p);
						needFuel -= n;
					}
				}
				return false;
			}
			ItemStack out = fm.getSlot(AbstractFurnaceMenu.RESULT_SLOT).getItem();
			if (!out.isEmpty()) {
				collected += out.getCount();
				mc.gameMode.handleContainerInput(fm.containerId,
						AbstractFurnaceMenu.RESULT_SLOT, 0, ContainerInput.QUICK_MOVE, p);
			}
			if (collected >= target) { close(mc); return true; }
			return false;
		}
		private void close(Minecraft mc) { mc.gui.setScreen(null); }
		@Override void fail(String e) { close(Minecraft.getInstance()); super.fail(e); }
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
			ticksLeft = Math.max(1, secs(r, "seconds", 1));
		}
		@Override protected boolean tick(Minecraft mc) {
			return --ticksLeft <= 0;
		}
	}
}
