package io.github.bn4t.mcremote.state;

import com.google.gson.JsonObject;
import net.minecraft.client.Minecraft;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.List;

/** Ring buffer of notable client-side events (chat, joins, deaths...). */
public class EventLog {
	public record Event(long seq, String type, long gameTime, JsonObject data) {}

	private static final int CAPACITY = 2048;
	private final ArrayDeque<Event> events = new ArrayDeque<>();
	private long seq = 0;

	public synchronized void add(String type, JsonObject data) {
		long gameTime = -1;
		Minecraft mc = Minecraft.getInstance();
		if (mc != null && mc.level != null) {
			gameTime = mc.level.getLevelData().getGameTime();
		}
		events.addLast(new Event(++seq, type, gameTime, data));
		while (events.size() > CAPACITY) {
			events.removeFirst();
		}
	}

	/** Events with seq > since, oldest first, at most limit. */
	public synchronized List<Event> since(long since, int limit) {
		List<Event> out = new ArrayList<>();
		for (Event e : events) {
			if (e.seq() > since) {
				out.add(e);
				if (out.size() >= limit) break;
			}
		}
		return out;
	}

	public synchronized long latestSeq() {
		return seq;
	}
}
