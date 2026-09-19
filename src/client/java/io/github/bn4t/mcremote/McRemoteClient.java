package io.github.bn4t.mcremote;

import io.github.bn4t.mcremote.api.RemoteServer;
import io.github.bn4t.mcremote.control.ActionRunner;
import io.github.bn4t.mcremote.state.EventLog;
import io.github.bn4t.mcremote.state.StateCollector;
import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.fabricmc.fabric.api.client.message.v1.ClientReceiveMessageEvents;
import net.fabricmc.fabric.api.client.networking.v1.ClientPlayConnectionEvents;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import com.google.gson.JsonObject;

public class McRemoteClient implements ClientModInitializer {
	public static final String MOD_ID = "mcremote";
	public static final Logger LOGGER = LoggerFactory.getLogger(MOD_ID);

	@Override
	public void onInitializeClient() {
		RemoteConfig config = RemoteConfig.load();
		EventLog events = new EventLog();
		ActionRunner actions = new ActionRunner();
		io.github.bn4t.mcremote.control.McRemoteHolder.init(actions);
		StateCollector collector = new StateCollector(events);

		ClientTickEvents.END_CLIENT_TICK.register(mc -> {
			actions.tick(mc);
			collector.onTick(mc);
		});

		ClientReceiveMessageEvents.CHAT.register((message, signedMessage, sender, params, receptionTimestamp) -> {
			JsonObject data = new JsonObject();
			data.addProperty("content", message.getString());
			data.addProperty("sender", sender != null ? sender.name() : null);
			events.add("chat", data);
		});
		ClientReceiveMessageEvents.GAME.register((message, overlay) -> {
			JsonObject data = new JsonObject();
			data.addProperty("content", message.getString());
			data.addProperty("overlay", overlay);
			events.add("game_message", data);
		});

		ClientPlayConnectionEvents.JOIN.register((handler, sender, client) -> {
			JsonObject data = new JsonObject();
			data.addProperty("world", client.hasSingleplayerServer() ? "singleplayer" : "multiplayer");
			events.add("joined_world", data);
		});
		ClientPlayConnectionEvents.DISCONNECT.register((handler, client) -> {
			actions.stopAll();
			events.add("left_world", new JsonObject());
		});

		RemoteServer server = new RemoteServer(config, collector, events, actions);
		server.start();
		LOGGER.info("mc-remote API listening on http://{}:{} (MCP at /mcp)", config.bindAddress(), config.port());
	}
}
