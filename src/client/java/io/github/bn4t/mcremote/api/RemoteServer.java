package io.github.bn4t.mcremote.api;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import io.github.bn4t.mcremote.McRemoteClient;
import io.github.bn4t.mcremote.RemoteConfig;
import io.github.bn4t.mcremote.control.ActionRunner;
import io.github.bn4t.mcremote.control.Actions;
import io.github.bn4t.mcremote.control.GameAction;
import io.github.bn4t.mcremote.state.EventLog;
import io.github.bn4t.mcremote.state.StateCollector;
import net.minecraft.client.Minecraft;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

/**
 * Local JSON/HTTP API + MCP (streamable HTTP) endpoint for remote control.
 * All game-state access is marshalled onto the client thread.
 */
public class RemoteServer {
	private static final Gson GSON = new Gson();
	private final RemoteConfig config;
	private final StateCollector collector;
	private final EventLog events;
	private final ActionRunner actions;
	private HttpServer server;

	public RemoteServer(RemoteConfig config, StateCollector collector, EventLog events, ActionRunner actions) {
		this.config = config;
		this.collector = collector;
		this.events = events;
		this.actions = actions;
	}

	public void start() {
		try {
			server = HttpServer.create(new InetSocketAddress(config.bindAddress(), config.port()), 0);
		} catch (IOException e) {
			McRemoteClient.LOGGER.error("Could not bind API server", e);
			return;
		}
		server.setExecutor(Executors.newCachedThreadPool(r -> {
			Thread t = new Thread(r, "mcremote-http");
			t.setDaemon(true);
			return t;
		}));
		server.createContext("/v1/health", this::health);
		server.createContext("/v1/state", this::state);
		server.createContext("/v1/blocks", this::blocks);
		server.createContext("/v1/events", this::events);
		server.createContext("/v1/action", this::action);
		server.createContext("/v1/actions", this::listActions);
		server.createContext("/v1/stop", this::stop);
		server.createContext("/mcp", new McpHandler(this)::handle);
		server.start();
	}

	// --- helpers ---

	private boolean authorized(HttpExchange ex) {
		if (config.token() == null || config.token().isEmpty()) return true;
		String auth = ex.getRequestHeaders().getFirst("Authorization");
		return ("Bearer " + config.token()).equals(auth);
	}

	private static Map<String, String> query(HttpExchange ex) {
		Map<String, String> out = new HashMap<>();
		String q = ex.getRequestURI().getRawQuery();
		if (q == null) return out;
		for (String pair : q.split("&")) {
			int eq = pair.indexOf('=');
			if (eq < 0) continue;
			out.put(urlDecode(pair.substring(0, eq)), urlDecode(pair.substring(eq + 1)));
		}
		return out;
	}

	private static String urlDecode(String s) {
		try {
			return java.net.URLDecoder.decode(s, StandardCharsets.UTF_8);
		} catch (Exception e) {
			return s;
		}
	}

	private static String body(HttpExchange ex) throws IOException {
		return new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
	}

	private static void respond(HttpExchange ex, int code, JsonObject payload) throws IOException {
		byte[] bytes = GSON.toJson(payload).getBytes(StandardCharsets.UTF_8);
		ex.getResponseHeaders().set("Content-Type", "application/json");
		ex.sendResponseHeaders(code, bytes.length);
		try (OutputStream os = ex.getResponseBody()) {
			os.write(bytes);
		}
	}

	private static void error(HttpExchange ex, int code, String msg) throws IOException {
		JsonObject o = new JsonObject();
		o.addProperty("ok", false);
		o.addProperty("error", msg);
		respond(ex, code, o);
	}

	private static JsonObject ok() {
		JsonObject o = new JsonObject();
		o.addProperty("ok", true);
		return o;
	}

	/** Run fn on the client thread, wait up to timeoutMs. */
	private JsonObject onClient(java.util.function.Supplier<JsonObject> fn, long timeoutMs) {
		CompletableFuture<JsonObject> fut = new CompletableFuture<>();
		actions.runOnClient(() -> {
			try {
				fut.complete(fn.get());
			} catch (Throwable t) {
				fut.completeExceptionally(t);
			}
		});
		try {
			return fut.get(timeoutMs, TimeUnit.MILLISECONDS);
		} catch (Exception e) {
			JsonObject o = new JsonObject();
			o.addProperty("ok", false);
			o.addProperty("error", "client not ready or busy: " + e.getClass().getSimpleName());
			return o;
		}
	}

	public JsonObject runOnClientThread(java.util.function.Supplier<JsonObject> fn, long timeoutMs) {
		return onClient(fn, timeoutMs);
	}

	public StateCollector collector() { return collector; }
	public EventLog events() { return events; }

	// --- routes ---

	private void health(HttpExchange ex) throws IOException {
		if (!"GET".equals(ex.getRequestMethod())) { error(ex, 405, "GET only"); return; }
		JsonObject o = ok();
		o.addProperty("mod", "mcremote");
		respond(ex, 200, o);
	}

	private void state(HttpExchange ex) throws IOException {
		if (!authorized(ex)) { error(ex, 401, "unauthorized"); return; }
		if (!"GET".equals(ex.getRequestMethod())) { error(ex, 405, "GET only"); return; }
		Map<String, String> q = query(ex);
		int radius = Math.min(parseInt(q.get("blocks"), 0), config.maxBlockRadius());
		int below = parseInt(q.get("below"), Math.min(4, radius));
		int above = parseInt(q.get("above"), radius > 0 ? radius : 0);
		JsonObject result = onClient(() -> collector.snapshot(Minecraft.getInstance(), radius, below, above), 15000);
		respond(ex, result.has("ok") ? 200 : 503, result);
	}

	private void blocks(HttpExchange ex) throws IOException {
		if (!authorized(ex)) { error(ex, 401, "unauthorized"); return; }
		Map<String, String> q = query(ex);
		int radius = Math.min(parseInt(q.get("radius"), 8), config.maxBlockRadius());
		int below = parseInt(q.get("below"), 4);
		int above = parseInt(q.get("above"), 8);
		JsonObject result = onClient(() -> {
			Minecraft mc = Minecraft.getInstance();
			JsonObject o = ok();
			if (mc.player == null || mc.level == null) {
				o.addProperty("ok", false);
				o.addProperty("error", "not in world");
				return o;
			}
			o.add("blocks", collector.blocks(mc.level, mc.player.blockPosition(), radius, below, above));
			return o;
		}, 10000);
		respond(ex, result.get("ok").getAsBoolean() ? 200 : 503, result);
	}

	private void events(HttpExchange ex) throws IOException {
		if (!authorized(ex)) { error(ex, 401, "unauthorized"); return; }
		Map<String, String> q = query(ex);
		long since = parseLong(q.get("since"), 0);
		int limit = parseInt(q.get("limit"), 200);
		JsonObject o = ok();
		com.google.gson.JsonArray arr = new com.google.gson.JsonArray();
		for (EventLog.Event e : events.since(since, limit)) {
			JsonObject je = new JsonObject();
			je.addProperty("seq", e.seq());
			je.addProperty("type", e.type());
			je.addProperty("game_time", e.gameTime());
			je.add("data", e.data());
			arr.add(je);
		}
		o.add("events", arr);
		o.addProperty("latest_seq", events.latestSeq());
		respond(ex, 200, o);
	}

	private void action(HttpExchange ex) throws IOException {
		if (!authorized(ex)) { error(ex, 401, "unauthorized"); return; }
		if ("GET".equals(ex.getRequestMethod())) {
			long id = parseLong(query(ex).get("id"), -1);
			GameAction a = id >= 0 ? actions.find(id) : null;
			if (a == null) { error(ex, 404, "no such action"); return; }
			respond(ex, 200, a.describe());
			return;
		}
		if (!"POST".equals(ex.getRequestMethod())) { error(ex, 405, "GET/POST only"); return; }
		JsonObject req;
		try {
			req = JsonParser.parseString(body(ex)).getAsJsonObject();
		} catch (Exception e) {
			error(ex, 400, "invalid json body");
			return;
		}
		JsonObject out = submitAction(req, parseDouble(query(ex).get("wait"), 0));
		respond(ex, out.get("ok").getAsBoolean() ? 200 : 400, out);
	}

	private void listActions(HttpExchange ex) throws IOException {
		if (!authorized(ex)) { error(ex, 401, "unauthorized"); return; }
		JsonObject o = ok();
		com.google.gson.JsonArray arr = new com.google.gson.JsonArray();
		for (JsonObject d : actions.listRecent()) arr.add(d);
		o.add("recent", arr);
		o.addProperty("queued", actions.queuedCount());
		respond(ex, 200, o);
	}

	private void stop(HttpExchange ex) throws IOException {
		if (!authorized(ex)) { error(ex, 401, "unauthorized"); return; }
		actions.stopAll();
		respond(ex, 200, ok());
	}

	/** Single-tick actions that bypass the serial action queue. */
	private static final java.util.Set<String> INSTANT_TYPES = java.util.Set.of(
			"look", "say", "select_slot", "drop", "swap_hands",
			"respawn", "open_inventory", "close_screen", "click_slot",
			"interact_entity", "use_on_block");

	/** Shared by REST and MCP: enqueue action, optionally wait for terminal state. */
	public JsonObject submitAction(JsonObject req, double waitSeconds) {
		GameAction a;
		try {
			a = Actions.parse(actions.nextId(), req);
		} catch (Exception e) {
			JsonObject o = new JsonObject();
			o.addProperty("ok", false);
			o.addProperty("error", "bad action: " + e.getMessage());
			return o;
		}
		if (a == null) {
			JsonObject o = new JsonObject();
			o.addProperty("ok", false);
			o.addProperty("error", "unknown action type: " + (req.has("type") ? req.get("type").getAsString() : ""));
			return o;
		}
		if (INSTANT_TYPES.contains(a.type)) {
			actions.submitInstant(a);
		} else {
			actions.submit(a);
		}
		if (waitSeconds > 0) {
			try {
				JsonObject done = a.result().get((long) (waitSeconds * 1000), TimeUnit.MILLISECONDS);
				done.addProperty("ok", true);
				return done;
			} catch (Exception ignored) {
			}
		}
		JsonObject o = ok();
		o.addProperty("action_id", a.id);
		o.addProperty("status", a.status().name().toLowerCase());
		return o;
	}

	private static int parseInt(String s, int def) {
		try { return s == null ? def : Integer.parseInt(s); } catch (Exception e) { return def; }
	}

	private static long parseLong(String s, long def) {
		try { return s == null ? def : Long.parseLong(s); } catch (Exception e) { return def; }
	}

	private static double parseDouble(String s, double def) {
		try { return s == null ? def : Double.parseDouble(s); } catch (Exception e) { return def; }
	}
}
