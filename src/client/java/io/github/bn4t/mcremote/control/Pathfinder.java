package io.github.bn4t.mcremote.control;

import it.unimi.dsi.fastutil.longs.Long2ObjectOpenHashMap;
import it.unimi.dsi.fastutil.longs.LongOpenHashSet;
import net.minecraft.client.Minecraft;
import net.minecraft.client.multiplayer.ClientLevel;
import net.minecraft.core.BlockPos;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.PriorityQueue;

/**
 * A* over the voxel grid. Nodes are player-feet cells: a cell (x,y,z) is usable
 * when it and the cell above are passable (empty collision shape) and the cell
 * below is standable (non-empty collision shape, or liquid to swim/land on).
 * Neighbors: walk to an adjacent standable cell, jump up one block (needs
 * headroom at both ends), or drop down up to MAX_DROP blocks.
 */
final class Pathfinder {
	private static final int MAX_EXPAND = 12000;
	private static final int MAX_DROP = 4;
	private static final int MAX_RANGE = 64; // horizontal distance from start

	private static final class Node {
		final long key;
		final double g;
		final double f;
		final long parent;

		Node(long key, double g, double f, long parent) {
			this.key = key;
			this.g = g;
			this.f = f;
			this.parent = parent;
		}
	}

	static List<BlockPos> findPath(Minecraft mc, BlockPos start,
			double tx, double tz, double arrive, Integer ty) {
		ClientLevel level = mc.level;
		int gx = (int) Math.floor(tx);
		int gz = (int) Math.floor(tz);
		int goalY = ty != null ? ty
				: findStandableY(level, gx, gz, start.getY());
		if (goalY == Integer.MIN_VALUE) {
			io.github.bn4t.mcremote.McRemoteClient.LOGGER.info(
					"findPath: no standable column at {},{} near {}", gx, gz, start);
			return null;
		}

		Long2ObjectOpenHashMap<Node> nodes = new Long2ObjectOpenHashMap<>();
		LongOpenHashSet closed = new LongOpenHashSet();
		PriorityQueue<Node> open = new PriorityQueue<>(Comparator.comparingDouble(n -> n.f));
		long sKey = start.asLong();
		Node s = new Node(sKey, 0, h(start.getX(), start.getY(), start.getZ(), tx, tz, goalY), Long.MIN_VALUE);
		nodes.put(sKey, s);
		open.add(s);

		int[] dxs = {1, -1, 0, 0};
		int[] dzs = {0, 0, 1, -1};
		int expanded = 0;
		Node best = s;

		while (!open.isEmpty() && expanded++ < MAX_EXPAND) {
			Node cur = open.poll();
			if (!closed.add(cur.key)) continue;
			BlockPos cp = BlockPos.of(cur.key);
			if (h(cp.getX(), cp.getY(), cp.getZ(), tx, tz, goalY)
					< h(BlockPos.of(best.key).getX(), BlockPos.of(best.key).getY(), BlockPos.of(best.key).getZ(), tx, tz, goalY)) {
				best = cur;
			}
			double cx = cp.getX() + 0.5, cz = cp.getZ() + 0.5;
			if (Math.hypot(cx - tx, cz - tz) <= arrive && Math.abs(cp.getY() - goalY) <= 1) {
				return reconstruct(nodes, cur);
			}
			if (Math.hypot(cx - start.getX(), cz - start.getZ()) > MAX_RANGE) continue;

			int x = cp.getX(), y = cp.getY(), z = cp.getZ();
			for (int i = 0; i < 4; i++) {
				int nx = x + dxs[i], nz = z + dzs[i];
				if (passable(level, nx, y, nz) && passable(level, nx, y + 1, nz)) {
					if (standable(level, nx, y, nz)) {
						push(nodes, open, closed, nx, y, nz, cur, 1.0, tx, tz, goalY);
						continue;
					}
					// drop down up to MAX_DROP, but not into a pit with no exit
					for (int dy = 1; dy <= MAX_DROP; dy++) {
						int ny = y - dy;
						if (!passable(level, nx, ny, nz) || !passable(level, nx, ny + 1, nz)) break;
						if (standable(level, nx, ny, nz)) {
							if (hasExit(level, nx, ny, nz))
								push(nodes, open, closed, nx, ny, nz, cur, 1.0 + 0.4 * dy, tx, tz, goalY);
							break;
						}
					}
				} else if (passable(level, x, y + 2, z)
						&& passable(level, nx, y + 1, nz) && passable(level, nx, y + 2, nz)
						&& standable(level, nx, y + 1, nz)) {
					// jump up one
					push(nodes, open, closed, nx, y + 1, nz, cur, 1.6, tx, tz, goalY);
				}
			}
		}
		// no exact goal: walk toward the closest node found (best effort)
		if (best != s) return reconstruct(nodes, best);
		io.github.bn4t.mcremote.McRemoteClient.LOGGER.info(
				"findPath: zero expansions from {}", start);
		return null;
	}

	private static List<BlockPos> reconstruct(Long2ObjectOpenHashMap<Node> nodes, Node end) {
		List<BlockPos> out = new ArrayList<>();
		for (Node n = end; n != null && n.parent != Long.MIN_VALUE; n = nodes.get(n.parent))
			out.add(BlockPos.of(n.key));
		java.util.Collections.reverse(out);
		return out;
	}

	private static void push(Long2ObjectOpenHashMap<Node> nodes, PriorityQueue<Node> open,
			LongOpenHashSet closed, int x, int y, int z, Node cur, double cost,
			double tx, double tz, int goalY) {
		long key = BlockPos.asLong(x, y, z);
		if (closed.contains(key)) return;
		double g = cur.g + cost;
		Node old = nodes.get(key);
		if (old != null && old.g <= g) return;
		Node n = new Node(key, g, g + h(x, y, z, tx, tz, goalY), cur.key);
		nodes.put(key, n);
		open.add(n);
	}

	private static double h(int x, int y, int z, double tx, double tz, int ty) {
		return Math.hypot(x + 0.5 - tx, z + 0.5 - tz) + 0.6 * Math.abs(y - ty);
	}

	private static int findStandableY(ClientLevel level, int x, int z, int nearY) {
		// probe the target column and its neighbours — the exact column may be a
		// trunk, a bush, or buried in a hillside while ground is right beside it
		for (int[] off : new int[][]{{0, 0}, {1, 0}, {-1, 0}, {0, 1}, {0, -1},
				{1, 1}, {-1, -1}, {1, -1}, {-1, 1}}) {
			for (int y = Math.min(nearY + 24, level.getMaxY());
					y > Math.max(nearY - 24, level.getMinY()); y--) {
				int cx = x + off[0], cz = z + off[1];
				if (standable(level, cx, y, cz) && passable(level, cx, y, cz)
						&& passable(level, cx, y + 1, cz))
					return y;
			}
		}
		return Integer.MIN_VALUE;
	}

	/** A landing cell you can walk or single-jump out of in some direction. */
	private static boolean hasExit(ClientLevel level, int x, int y, int z) {
		int[] dxs = {1, -1, 0, 0};
		int[] dzs = {0, 0, 1, -1};
		for (int i = 0; i < 4; i++) {
			int nx = x + dxs[i], nz = z + dzs[i];
			if (passable(level, nx, y, nz) && passable(level, nx, y + 1, nz)
					&& (standable(level, nx, y, nz) || passable(level, nx, y - 1, nz)))
				return true;
			if (passable(level, x, y + 2, z) && passable(level, nx, y + 1, nz)
					&& passable(level, nx, y + 2, nz) && standable(level, nx, y + 1, nz))
				return true;
		}
		return false;
	}

	private static boolean passable(ClientLevel level, int x, int y, int z) {
		BlockPos p = new BlockPos(x, y, z);
		var s = level.getBlockState(p);
		if (s.isPathfindable(net.minecraft.world.level.pathfinder.PathComputationType.LAND))
			return true;
		var shape = s.getCollisionShape(level, p);
		if (shape.isEmpty()) return true;                    // air / grass / torches
		// walk over thin cover like leaf litter / carpet / snow layers
		if (shape.max(net.minecraft.core.Direction.Axis.Y) <= 0.3)
			return true;
		// wading / swimming through water (not lava)
		return s.getFluidState().is(net.minecraft.tags.FluidTags.WATER);
	}

	private static boolean standable(ClientLevel level, int x, int y, int z) {
		BlockPos below = new BlockPos(x, y - 1, z);
		if (!level.getBlockState(below).getCollisionShape(level, below).isEmpty()) return true;
		var fBelow = level.getFluidState(below);
		if (fBelow.is(net.minecraft.tags.FluidTags.WATER)) return true;
		return level.getFluidState(new BlockPos(x, y, z)).is(net.minecraft.tags.FluidTags.WATER);
	}
}
