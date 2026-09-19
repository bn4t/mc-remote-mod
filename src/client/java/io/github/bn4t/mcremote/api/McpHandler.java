package io.github.bn4t.mcremote.api;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.sun.net.httpserver.HttpExchange;

import java.io.IOException;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;

/**
 * Minimal MCP (streamable HTTP) endpoint. POST accepts JSON-RPC; GET keeps an
 * SSE channel open with heartbeats so clients that expect a stream work too.
 */
public class McpHandler {
	private static final Gson GSON = new Gson();
	private static final String PROTOCOL_VERSION = "2025-06-18";
	private final RemoteServer server;

	public McpHandler(RemoteServer server) {
		this.server = server;
	}

	public void handle(HttpExchange ex) throws IOException {
		String method = ex.getRequestMethod();
		if ("GET".equals(method)) {
			sse(ex);
			return;
		}
		if (!"POST".equals(method)) {
			ex.sendResponseHeaders(405, -1);
			ex.close();
			return;
		}
		JsonElement msg;
		try {
			msg = JsonParser.parseString(new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
		} catch (Exception e) {
			respond(ex, 400, error(null, -32700, "parse error"));
			return;
		}
		JsonElement result = dispatch(msg);
		if (result == null) {
			// notification: no response body
			ex.sendResponseHeaders(202, -1);
			ex.close();
			return;
		}
		respond(ex, 200, result);
	}

	private JsonElement dispatch(JsonElement msg) {
		if (msg.isJsonArray()) {
			JsonArray out = new JsonArray();
			for (JsonElement m : msg.getAsJsonArray()) {
				JsonElement r = dispatchOne(m);
				if (r != null) out.add(r);
			}
			return out.isEmpty() ? null : out;
		}
		return dispatchOne(msg);
	}

	private JsonElement dispatchOne(JsonElement msg) {
		JsonObject req = msg.getAsJsonObject();
		JsonElement id = req.get("id");
		String method = req.has("method") ? req.get("method").getAsString() : "";
		JsonObject params = req.has("params") && req.get("params").isJsonObject()
				? req.getAsJsonObject("params") : new JsonObject();

		if (method.startsWith("notifications/")) return null;

		JsonObject result = switch (method) {
			case "initialize" -> initialize(params);
			case "ping" -> new JsonObject();
			case "tools/list" -> toolsList();
			case "tools/call" -> toolsCall(params);
			case "resources/list" -> empty("resources");
			case "prompts/list" -> empty("prompts");
			case "resources/templates/list" -> empty("resourceTemplates");
			case "logging/setLevel" -> new JsonObject();
			default -> null;
		};
		if (result == null) {
			return error(id, -32601, "method not found: " + method);
		}
		JsonObject resp = new JsonObject();
		resp.addProperty("jsonrpc", "2.0");
		resp.add("id", id);
		resp.add("result", result);
		return resp;
	}

	private JsonObject initialize(JsonObject params) {
		JsonObject r = new JsonObject();
		r.addProperty("protocolVersion",
				params.has("protocolVersion") ? params.get("protocolVersion").getAsString() : PROTOCOL_VERSION);
		JsonObject caps = new JsonObject();
		caps.add("tools", new JsonObject());
		r.add("capabilities", caps);
		JsonObject info = new JsonObject();
		info.addProperty("name", "mcremote");
		info.addProperty("version", "0.1.0");
		r.add("serverInfo", info);
		return r;
	}

	private JsonObject empty(String key) {
		JsonObject r = new JsonObject();
		r.add(key, new JsonArray());
		return r;
	}

	private JsonObject toolsCall(JsonObject params) {
		String name = params.has("name") ? params.get("name").getAsString() : "";
		JsonObject args = params.has("arguments") && params.get("arguments").isJsonObject()
				? params.getAsJsonObject("arguments") : new JsonObject();

		JsonObject payload;
		boolean isError = false;
		try {
			payload = runTool(name, args);
		} catch (Exception e) {
			payload = new JsonObject();
			payload.addProperty("error", e.getMessage());
			isError = true;
		}
		if (payload.has("ok") && !payload.get("ok").getAsBoolean()) isError = true;
		if (payload.has("status") && "failed".equals(payload.get("status").getAsString())) isError = true;

		JsonObject r = new JsonObject();
		JsonArray content = new JsonArray();
		JsonObject text = new JsonObject();
		text.addProperty("type", "text");
		text.addProperty("text", GSON.toJson(payload));
		content.add(text);
		r.add("content", content);
		if (isError) r.addProperty("isError", true);
		return r;
	}

	private JsonObject runTool(String name, JsonObject args) {
		double wait = args.has("timeout_seconds") ? args.get("timeout_seconds").getAsDouble() : 25;
		return switch (name) {
			case "get_state" -> server.runOnClientThread(() -> {
				JsonObject o = new JsonObject();
				int radius = args.has("blocks_radius") ? args.get("blocks_radius").getAsInt() : 0;
				o.add("state", stateSnapshot(radius));
				o.addProperty("ok", true);
				return o;
			}, 10000);
			case "get_blocks" -> server.runOnClientThread(() -> blocksJson(args), 10000);
			case "get_events" -> eventsJson(args);
			case "get_action" -> {
				long id = args.has("id") ? args.get("id").getAsLong() : -1;
				var a = io.github.bn4t.mcremote.control.McRemoteHolder.actions().find(id);
				yield a != null ? a.describe() : err("no such action");
			}
			case "stop" -> {
				io.github.bn4t.mcremote.control.McRemoteHolder.actions().stopAll();
				JsonObject o = new JsonObject();
				o.addProperty("ok", true);
				yield o;
			}
			default -> {
				JsonObject actionReq = args.deepCopy();
				actionReq.addProperty("type", toolToActionType(name));
				yield server.submitAction(actionReq, wait);
			}
		};
	}

	private JsonObject stateSnapshot(int radius) {
		net.minecraft.client.Minecraft mc = net.minecraft.client.Minecraft.getInstance();
		return collector().snapshot(mc, radius, Math.min(4, radius), radius);
	}

	private JsonObject blocksJson(JsonObject args) {
		net.minecraft.client.Minecraft mc = net.minecraft.client.Minecraft.getInstance();
		JsonObject o = new JsonObject();
		if (mc.player == null || mc.level == null) {
			o.addProperty("ok", false);
			o.addProperty("error", "not in world");
			return o;
		}
		int radius = args.has("radius") ? args.get("radius").getAsInt() : 8;
		int below = args.has("below") ? args.get("below").getAsInt() : 4;
		int above = args.has("above") ? args.get("above").getAsInt() : 8;
		o.addProperty("ok", true);
		o.add("blocks", collector().blocks(mc.level, mc.player.blockPosition(), radius, below, above));
		return o;
	}

	private JsonObject eventsJson(JsonObject args) {
		long since = args.has("since_seq") ? args.get("since_seq").getAsLong() : 0;
		int limit = args.has("limit") ? args.get("limit").getAsInt() : 200;
		JsonObject o = new JsonObject();
		JsonArray arr = new JsonArray();
		for (var e : events().since(since, limit)) {
			JsonObject je = new JsonObject();
			je.addProperty("seq", e.seq());
			je.addProperty("type", e.type());
			je.addProperty("game_time", e.gameTime());
			je.add("data", e.data());
			arr.add(je);
		}
		o.addProperty("ok", true);
		o.add("events", arr);
		o.addProperty("latest_seq", events().latestSeq());
		return o;
	}

	private static JsonObject err(String msg) {
		JsonObject o = new JsonObject();
		o.addProperty("ok", false);
		o.addProperty("error", msg);
		return o;
	}

	private String toolToActionType(String tool) {
		return switch (tool) {
			case "look", "move", "jump", "attack", "use", "interact_entity", "mine",
					"use_on_block", "walk_to", "select_slot", "drop", "say", "command",
					"respawn", "close_screen", "click_slot", "wait", "swap_hands",
					"craft", "open_inventory" -> tool;
			default -> tool; // unknown -> server returns error
		};
	}

	private io.github.bn4t.mcremote.state.StateCollector collector() {
		return server.collector();
	}

	private io.github.bn4t.mcremote.state.EventLog events() {
		return server.events();
	}

	private void sse(HttpExchange ex) throws IOException {
		ex.getResponseHeaders().set("Content-Type", "text/event-stream");
		ex.getResponseHeaders().set("Cache-Control", "no-cache");
		ex.getResponseHeaders().set("Mcp-Protocol-Version", PROTOCOL_VERSION);
		ex.sendResponseHeaders(200, 0);
		OutputStream os = ex.getResponseBody();
		try {
			for (int i = 0; i < 240; i++) { // ~1h then client reconnects
				os.write(": heartbeat\n\n".getBytes(StandardCharsets.UTF_8));
				os.flush();
				Thread.sleep(15000);
			}
		} catch (IOException | InterruptedException ignored) {
		} finally {
			try { os.close(); } catch (IOException ignored) {}
			ex.close();
		}
	}

	private JsonObject toolsList() {
		JsonObject r = new JsonObject();
		JsonArray tools = new JsonArray();

		tools.add(tool("get_state", "Snapshot of what the client currently sees: player position/rotation/health/hunger/xp, inventory, nearby entities, looking-at target, world time/weather, open screen/container. Optionally includes the block grid.", schema(
				prop("blocks_radius", "integer", "radius of the block grid to include (0 = omit blocks, default 0)"))));

		tools.add(tool("get_blocks", "Palette-encoded block grid around the player. data is flat in y,z,x order: index = (y*sizeZ + z)*sizeX + x, indexing into palette names. notable lists positions of interesting blocks (ores, chests, lava, doors, ...).", schema(
				prop("radius", "integer", "horizontal radius around player (default 8)"),
				prop("below", "integer", "blocks below player (default 4)"),
				prop("above", "integer", "blocks above player (default 8)"))));

		tools.add(tool("get_events", "Recent game events since a sequence number: chat, system messages, death/respawn, joins.", schema(
				prop("since_seq", "integer", "return events with seq greater than this (default 0)"),
				prop("limit", "integer", "max events to return (default 200)"))));

		tools.add(tool("look", "Instantly face a direction. yaw: 0=south(+Z), 90=west(-X), 180=north(-Z), -90=east(+X). pitch: -90=up, 0=level, 90=down.", schema(
				req("yaw", "number"), req("pitch", "number"))));

		tools.add(tool("move", "Hold movement inputs. forward/strafe -1..1 (strafe positive = right). seconds<=0 holds until 'stop'. Optional yaw/pitch to steer.", schema(
				prop("forward", "number", "forward speed -1..1 (default 0)"),
				prop("strafe", "number", "sideways speed -1..1, positive=right"),
				prop("seconds", "number", "how long to hold (default 0 = until stop)"),
				prop("sprint", "boolean", ""), prop("sneak", "boolean", ""), prop("jump", "boolean", "hold jump"),
				prop("yaw", "number", "optional facing"), prop("pitch", "number", ""))));

		tools.add(tool("walk_to", "Walk toward x,z: steers and auto-jumps over obstacles. Finishes within 'radius' or fails on timeout.", schema(
				req("x", "number"), req("z", "number"),
				prop("radius", "number", "arrival radius (default 1.5)"),
				prop("seconds", "number", "timeout (default 60)"))));

		tools.add(tool("jump", "Jump once.", schema()));

		tools.add(tool("attack", "Attack an entity (aim + swing with cooldown) or swing at whatever is in view when entity_id omitted.", schema(
				prop("entity_id", "integer", "entity id from get_state.entities"),
				prop("times", "integer", "swings (default 1)"),
				prop("seconds", "number", "timeout (default 10)"))));

		tools.add(tool("use", "Hold right-click/use (eat, drink, place held block on looked-at block, bow, etc.) for 'seconds'.", schema(
				prop("seconds", "number", "hold duration (default 0.35)"))));

		tools.add(tool("interact_entity", "Right-click interact with an entity (villager, minecart, ...).", schema(
				req("entity_id", "integer"))));

		tools.add(tool("mine", "Aim at and break the block at x,y,z. Fails if out of reach or timeout.", schema(
				req("x", "integer"), req("y", "integer"), req("z", "integer"),
				prop("seconds", "number", "timeout (default 30)"))));

		tools.add(tool("use_on_block", "Right-click the given face of the block at x,y,z with the held item (place a block against it, open a chest, press a button). face: up/down/north/south/east/west.", schema(
				req("x", "integer"), req("y", "integer"), req("z", "integer"),
				prop("face", "string", "face to click (default up)"))));

		tools.add(tool("select_slot", "Select hotbar slot 0-8.", schema(req("slot", "integer"))));
		tools.add(tool("drop", "Drop the held item stack. all=true drops the whole stack.", schema(prop("all", "boolean", ""))));
		tools.add(tool("swap_hands", "Swap main-hand and off-hand items.", schema()));

		tools.add(tool("click_slot", "Click a slot in the currently open container (or player inventory). click: pickup|quick_move|swap|throw|clone|pickup_all; button: 0 left, 1 right; swap uses button=hotbar slot 0-8.", schema(
				req("slot", "integer"),
				prop("button", "integer", "mouse button (default 0)"),
				prop("click", "string", "click type (default pickup)"))));

		tools.add(tool("craft", "Craft an item via the recipe book using the inventory 2x2 grid, or the open crafting-table grid (3x3) if a crafting table screen is open. Places the recipe and shift-clicks the result; succeeds only when the item lands in the inventory.", schema(
				req("item", "string"),
				prop("all", "boolean", "place all matching ingredients to craft the max (default true)"))));

		tools.add(tool("open_inventory", "Open the player inventory screen (2x2 crafting grid + recipe book).", schema()));

		tools.add(tool("close_screen", "Close any open screen/container.", schema()));
		tools.add(tool("say", "Send a chat message.", schema(req("message", "string"))));
		tools.add(tool("command", "Run a slash command.", schema(req("command", "string"))));
		tools.add(tool("respawn", "Respawn when dead.", schema()));
		tools.add(tool("wait", "Do nothing for 'seconds' (in the action queue).", schema(prop("seconds", "number", "default 1"))));
		tools.add(tool("stop", "Stop all inputs and cancel queued actions immediately.", schema()));
		tools.add(tool("get_action", "Check the status of an action returned by another tool.", schema(req("id", "integer"))));

		r.add("tools", tools);
		return r;
	}

	private static JsonObject tool(String name, String description, JsonObject inputSchema) {
		JsonObject t = new JsonObject();
		t.addProperty("name", name);
		t.addProperty("description", description);
		t.add("inputSchema", inputSchema);
		return t;
	}

	private static JsonObject schema(JsonObject... props) {
		JsonObject s = new JsonObject();
		s.addProperty("type", "object");
		JsonObject p = new JsonObject();
		JsonArray required = new JsonArray();
		for (JsonObject prop : props) {
			String name = prop.remove("name").getAsString();
			boolean req = prop.has("_required") && prop.remove("_required").getAsBoolean();
			if (req) required.add(name);
			p.add(name, prop);
		}
		s.add("properties", p);
		s.add("required", required);
		return s;
	}

	private static JsonObject prop(String name, String type, String description) {
		JsonObject p = new JsonObject();
		p.addProperty("name", name);
		p.addProperty("type", type);
		if (description != null && !description.isEmpty()) p.addProperty("description", description);
		return p;
	}

	private static JsonObject req(String name, String type) {
		JsonObject p = prop(name, type, null);
		p.addProperty("_required", true);
		return p;
	}

	private JsonObject error(JsonElement id, int code, String message) {
		JsonObject resp = new JsonObject();
		resp.addProperty("jsonrpc", "2.0");
		resp.add("id", id);
		JsonObject e = new JsonObject();
		e.addProperty("code", code);
		e.addProperty("message", message);
		resp.add("error", e);
		return resp;
	}

	private void respond(HttpExchange ex, int code, JsonElement payload) throws IOException {
		byte[] bytes = GSON.toJson(payload).getBytes(StandardCharsets.UTF_8);
		ex.getResponseHeaders().set("Content-Type", "application/json");
		ex.sendResponseHeaders(code, bytes.length);
		try (OutputStream os = ex.getResponseBody()) {
			os.write(bytes);
		}
	}
}
