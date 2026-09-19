package io.github.bn4t.mcremote.control;

import io.github.bn4t.mcremote.McRemoteClient;
import net.minecraft.client.Minecraft;

import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentLinkedDeque;
import com.google.gson.JsonObject;

/**
 * Serial queue of GameActions executed on the client thread, one tick at a
 * time, plus a channel for arbitrary client-thread tasks (state snapshots).
 */
public class ActionRunner {
	private final ConcurrentLinkedQueue<GameAction> queue = new ConcurrentLinkedQueue<>();
	private final ConcurrentLinkedQueue<Runnable> tickTasks = new ConcurrentLinkedQueue<>();
	private final Map<Long, GameAction> recent = new ConcurrentHashMap<>();
	private final ConcurrentLinkedDeque<Long> recentOrder = new ConcurrentLinkedDeque<>();
	private final RemoteInput input = new RemoteInput();
	private volatile GameAction current;
	private long nextId = 1;

	public RemoteInput input() { return input; }

	/** Enqueue an action; returns its id. */
	public long submit(GameAction action) {
		queue.add(action);
		return action.id;
	}

	/**
	 * Run a single-tick action on the client thread immediately, bypassing the
	 * serial queue (so a long-running move/walk can't delay instant commands).
	 * If the action isn't done after one tick it falls back into the queue.
	 */
	public void submitInstant(GameAction a) {
		runOnClient(() -> {
			a.markRunning();
			boolean done;
			try {
				done = a.tick(Minecraft.getInstance());
			} catch (Throwable t) {
				a.fail("internal error: " + t.getMessage());
				recent.put(a.id, a);
				recentOrder.addLast(a.id);
				return;
			}
			if (done) {
				if (a.status() == GameAction.Status.RUNNING) a.succeed();
				recent.put(a.id, a);
				recentOrder.addLast(a.id);
			} else {
				queue.add(a);
			}
		});
	}

	public long nextId() { return nextId++; }

	/** Run a task on the client thread at the next tick. */
	public void runOnClient(Runnable task) {
		tickTasks.add(task);
	}

	public GameAction find(long id) {
		GameAction a = recent.get(id);
		if (a != null) return a;
		if (current != null && current.id == id) return current;
		for (GameAction q : queue) if (q.id == id) return q;
		return null;
	}

	public int queuedCount() { return queue.size() + (current != null ? 1 : 0); }

	public void stopAll() {
		GameAction a;
		while ((a = queue.poll()) != null) a.cancel();
		if (current != null) current.cancel();
		current = null;
		input.clear();
	}

	/** Called every client tick. */
	public void tick(Minecraft mc) {
		Runnable task;
		while ((task = tickTasks.poll()) != null) {
			try {
				task.run();
			} catch (Throwable t) {
				McRemoteClient.LOGGER.warn("tick task failed", t);
			}
		}

		if (mc.player == null || mc.level == null) {
			// Not in a world: release keys so they don't latch on join.
			input.clear();
			input.apply(mc);
			return;
		}

		if (current == null) {
			current = queue.poll();
			if (current != null) current.markRunning();
		}
		// Inputs are per-tick intent: actions assert the flags they want each
		// tick, and flags auto-release when no action is asserting them, so a
		// completed action can never leave a key latched.
		input.clear();
		try {
			if (current != null) {
				boolean done = current.tick(mc);
				if (done || current.status() == GameAction.Status.FAILED) {
					if (current.status() == GameAction.Status.RUNNING) current.succeed();
					recent.put(current.id, current);
					recentOrder.addLast(current.id);
					while (recentOrder.size() > 200) {
						Long old = recentOrder.pollFirst();
						if (old != null) recent.remove(old);
					}
					current = null;
				}
			}
		} catch (Throwable t) {
			McRemoteClient.LOGGER.warn("action {} failed", current != null ? current.id : -1, t);
			if (current != null) {
				current.fail("internal error: " + t.getMessage());
				recent.put(current.id, current);
				current = null;
			}
		}
		input.apply(mc);
	}

	public List<JsonObject> listRecent() {
		return recent.values().stream().map(GameAction::describe).toList();
	}
}
