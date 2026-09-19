package io.github.bn4t.mcremote;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import net.fabricmc.loader.api.FabricLoader;

import java.io.IOException;
import java.io.Reader;
import java.io.Writer;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.UUID;

/**
 * Mod configuration, stored at .minecraft/config/mcremote.json.
 * Auto-created on first launch with a random bearer token.
 */
public record RemoteConfig(String bindAddress, int port, String token, int maxBlockRadius) {
	private static final Gson GSON = new Gson();

	public static RemoteConfig load() {
		Path path = FabricLoader.getInstance().getConfigDir().resolve("mcremote.json");
		if (Files.exists(path)) {
			try (Reader r = Files.newBufferedReader(path)) {
				JsonObject o = JsonParser.parseReader(r).getAsJsonObject();
				return new RemoteConfig(
						o.has("bind") ? o.get("bind").getAsString() : "127.0.0.1",
						o.has("port") ? o.get("port").getAsInt() : 25587,
						o.has("token") ? o.get("token").getAsString() : "",
						o.has("max_block_radius") ? o.get("max_block_radius").getAsInt() : 24);
			} catch (Exception e) {
				McRemoteClient.LOGGER.warn("Failed to read {}, using defaults", path, e);
			}
		}
		RemoteConfig cfg = new RemoteConfig("127.0.0.1", 25587, UUID.randomUUID().toString(), 24);
		try {
			Files.createDirectories(path.getParent());
			try (Writer w = Files.newBufferedWriter(path)) {
				JsonObject o = new JsonObject();
				o.addProperty("bind", cfg.bindAddress());
				o.addProperty("port", cfg.port());
				o.addProperty("token", cfg.token());
				o.addProperty("max_block_radius", cfg.maxBlockRadius());
				GSON.toJson(o, w);
			}
			McRemoteClient.LOGGER.info("Wrote default config to {}", path);
		} catch (IOException e) {
			McRemoteClient.LOGGER.warn("Could not write {}", path, e);
		}
		return cfg;
	}
}
