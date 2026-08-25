#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import sys


def replace_function(text: str, name: str, next_name: str, new_body: str) -> str:
    start = text.find(f"func {name}(")
    end = text.find(f"func {next_name}(", start)
    if start < 0 or end < 0:
        raise SystemExit(f"test function range not found: {name}")
    return text[:start] + new_body.rstrip() + "\n\n" + text[end:]


def patch_tick(root: pathlib.Path) -> None:
    path = root / "internal/bot/tick.go"
    text = path.read_text()
    old = '''\t// StartGame may contain a temporary Y≈32768 position while BDS already
\t// publishes the actual player location. Treat the first stable publisher
\t// position as the completion of that initial server teleport and acknowledge
\t// it in the next PlayerAuthInput before predicting any movement.
\ts.position = position
\ts.positionReady = true
\ts.handledTeleport = true
\treturn true
'''
    new = '''\t// StartGame may contain a temporary Y≈32768 position while BDS already
\t// publishes the actual player location. NetworkChunkPublisherUpdate is not a
\t// teleport packet, so use it only to seed prediction history. Emitting a
\t// synthetic HandledTeleport here can acknowledge a teleport BDS never sent.
\ts.position = position
\ts.positionReady = true
\treturn true
'''
    if old in text:
        text = text.replace(old, new, 1)
    elif "synthetic HandledTeleport here" not in text:
        raise SystemExit("publisher seed acknowledgement target not found")
    path.write_text(text)


def patch_tests(root: pathlib.Path) -> None:
    path = root / "internal/bot/flight_test.go"
    text = path.read_text()
    if "TestChunkFlyAcknowledgesPublisherSpawnBeforeHorizontalPrediction" in text:
        text = replace_function(text, "TestChunkFlyAcknowledgesPublisherSpawnBeforeHorizontalPrediction", "TestPublisherEyePositionUsesAuthoritativeBlockPosition", r'''func TestChunkFlySeedsPublisherWithoutSyntheticTeleportAck(t *testing.T) {
	state := newPlayerState(mgl32.Vec3{12.5, 32769.625, -7.5}, 0, 0)
	writer := &recordingPacketWriter{}
	fly := NewChunkFlyAction(state, writer, 0)
	if state.positionReadySnapshot() {
		t.Fatal("placeholder StartGame altitude must not be position-ready")
	}
	if err := fly.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	state.setFlyingConfirmed(true)
	blocked := authInputPacket(state, 1)
	if blocked.MoveVector != (mgl32.Vec2{}) || blocked.Delta != (mgl32.Vec3{}) {
		t.Fatalf("placeholder position generated movement before publisher evidence: %+v", blocked)
	}

	publisherPosition := mgl32.Vec3{12.5, 68 + playerEyeHeight, -7.5}
	if !state.acceptPublisherPosition(publisherPosition) {
		t.Fatal("stable server publisher position was not accepted")
	}
	input := authInputPacket(state, 2)
	if input.InputData.Load(packet.InputFlagHandledTeleport) {
		t.Fatalf("chunk publisher must not synthesize HandledTeleport: %+v", input.InputData)
	}
	if !input.InputData.Load(packet.InputFlagStartFlying) {
		t.Fatal("publisher-seeded flight must keep StartFlying asserted")
	}
	if input.MoveVector != (mgl32.Vec2{0, 1}) || input.Delta[2] <= 0 || input.Delta[1] != 0 {
		t.Fatalf("publisher-seeded horizontal prediction = move %v delta %v", input.MoveVector, input.Delta)
	}
	if input.Position[1] != publisherPosition[1] {
		t.Fatalf("horizontal diagnostic changed altitude: %v", input.Position)
	}
}''')
    elif "TestChunkFlySeedsPublisherWithoutSyntheticTeleportAck" not in text:
        raise SystemExit("publisher seed test target not found")
    path.write_text(text)


def main() -> int:
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    patch_tick(root)
    patch_tests(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
