package io.github.bn4t.mcremote.control;

/** Static access to the session's ActionRunner. */
public final class McRemoteHolder {
	private static ActionRunner actions;

	public static void init(ActionRunner a) {
		actions = a;
	}

	public static ActionRunner actions() {
		return actions;
	}
}
