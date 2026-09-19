package io.github.bn4t.mcremote.control;

import com.google.gson.JsonObject;
import net.minecraft.client.Minecraft;
import net.minecraft.world.phys.Vec3;

import java.util.concurrent.CompletableFuture;

/** One remote-control action, ticked on the client thread until done. */
public abstract class GameAction {
	public enum Status { QUEUED, RUNNING, DONE, FAILED, CANCELLED }

	public final long id;
	public final String type;
	private volatile Status status = Status.QUEUED;
	private volatile String error;
	private final CompletableFuture<JsonObject> result = new CompletableFuture<>();

	protected GameAction(long id, String type) {
		this.id = id;
		this.type = type;
	}

	/** Called once per client tick while running. Return true when finished. */
	protected abstract boolean tick(Minecraft mc);

	public Status status() { return status; }
	public String error() { return error; }
	public CompletableFuture<JsonObject> result() { return result; }

	void fail(String error) {
		this.error = error;
		this.status = Status.FAILED;
		result.complete(describe());
	}

	void succeed() {
		this.status = Status.DONE;
		result.complete(describe());
	}

	void cancel() {
		this.status = Status.CANCELLED;
		result.complete(describe());
	}

	void markRunning() {
		this.status = Status.RUNNING;
	}

	public JsonObject describe() {
		JsonObject o = new JsonObject();
		o.addProperty("id", id);
		o.addProperty("type", type);
		o.addProperty("status", status.name().toLowerCase());
		if (error != null) o.addProperty("error", error);
		return o;
	}

	/** yaw/pitch so that the eye at `from` looks at `target`. */
	protected static float[] aimAt(Vec3 from, Vec3 target) {
		double dx = target.x - from.x;
		double dy = target.y - from.y;
		double dz = target.z - from.z;
		double h = Math.sqrt(dx * dx + dz * dz);
		float yaw = (float) Math.toDegrees(Math.atan2(-dx, dz));
		float pitch = (float) Math.toDegrees(Math.atan2(-dy, h));
		return new float[]{yaw, pitch};
	}
}
