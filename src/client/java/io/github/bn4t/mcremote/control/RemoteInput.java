package io.github.bn4t.mcremote.control;

import net.minecraft.client.KeyMapping;
import net.minecraft.client.Minecraft;
import net.minecraft.client.Options;

/**
 * Desired input state driven by remote actions. Applied to the real key
 * mappings every client tick so the game treats it like physical input.
 */
public class RemoteInput {
	public volatile float forward;   // -1..1, positive = forward
	public volatile float strafe;    // -1..1, positive = right
	public volatile boolean jump;
	public volatile boolean sneak;
	public volatile boolean sprint;
	public volatile boolean attack;  // held: mines what you're looking at
	public volatile boolean use;     // held: eats/places/interacts

	public synchronized void set(float forward, float strafe, boolean jump, boolean sneak, boolean sprint) {
		this.forward = forward;
		this.strafe = strafe;
		this.jump = jump;
		this.sneak = sneak;
		this.sprint = sprint;
	}

	public synchronized void clear() {
		forward = 0;
		strafe = 0;
		jump = sneak = sprint = attack = use = false;
	}

	public synchronized void apply(Minecraft mc) {
		Options o = mc.options;
		o.keyUp.setDown(forward > 0);
		o.keyDown.setDown(forward < 0);
		o.keyRight.setDown(strafe > 0);
		o.keyLeft.setDown(strafe < 0);
		o.keyJump.setDown(jump);
		o.keyShift.setDown(sneak);
		o.keySprint.setDown(sprint);
		o.keyAttack.setDown(attack);
		o.keyUse.setDown(use);
	}
}
