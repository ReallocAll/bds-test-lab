#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import sys


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise SystemExit(f"{label}: target not found")
    return text.replace(old, new, 1)


def patch_tick(root: pathlib.Path) -> None:
    path = root / "internal/bot/tick.go"
    text = path.read_text()

    server_driven = '''\t} else if s.control.fly {
\t\tif !s.flyingConfirmed || !s.positionReady {
\t\t\t// Wait for BDS to publish the real spawn position before movement.
\t\t\tsnapshot.moveVector = mgl32.Vec2{}
\t\t} else if s.position[1] < s.control.flightTargetY-0.75 {
\t\t\t// Match a real client heartbeat: movement intent is authoritative,
\t\t\t// while Position remains the last server-observed position and Delta
\t\t\t// stays zero. BDS advances flight and publisher updates feed it back.
\t\t\tsnapshot.moveVector = mgl32.Vec2{}
\t\t\tsnapshot.verticalDirection = 1
\t\t}
'''
    horizontal_predicted = '''\t} else if s.control.fly {
\t\tif !s.flyingConfirmed || !s.positionReady {
\t\t\t// Never predict from StartGame's temporary Y≈32768 position. Wait
\t\t\t// until the first stable server publisher position seeds the player.
\t\t\tsnapshot.moveVector = mgl32.Vec2{}
\t\t} else {
\t\t\t// Diagnostic path: keep the server-owned spawn altitude unchanged and
\t\t\t// exercise the same client-predicted horizontal movement model that
\t\t\t// already works for chunk-walk. This isolates flying+horizontal input
\t\t\t// from the previously rejected vertical ascent prediction.
\t\t\tapplyHorizontalMovement(s, &snapshot, s.control.moveStep)
\t\t}
'''
    if server_driven in text:
        text = text.replace(server_driven, horizontal_predicted, 1)
    elif "Diagnostic path: keep the server-owned spawn altitude" not in text:
        raise SystemExit("horizontal flight diagnostic target not found")

    flight_baseline = '''\tflags := protocol.NewInputFlags(packet.InputFlagCount)
\tif !snapshot.flightRequested {
\t\tflags.Set(packet.InputFlagBlockBreakingDelayEnabled)
\t}
'''
    normal_baseline = '''\tflags := protocol.NewInputFlags(packet.InputFlagCount)
\tflags.Set(packet.InputFlagBlockBreakingDelayEnabled)
'''
    if flight_baseline in text:
        text = text.replace(flight_baseline, normal_baseline, 1)
    elif normal_baseline not in text:
        raise SystemExit("normal PlayerAuthInput baseline target not found")

    minimal_return = '''\tinput := &packet.PlayerAuthInput{
\t\tPitch:            snapshot.pitch,
\t\tYaw:              snapshot.yaw,
\t\tPosition:         snapshot.position,
\t\tMoveVector:       snapshot.moveVector,
\t\tHeadYaw:          snapshot.headYaw,
\t\tInputData:        flags,
\t\tInputMode:        packet.InputModeMouse,
\t\tPlayMode:         packet.PlayModeScreen,
\t\tInteractionModel: packet.InteractionModelCrosshair,
\t\tTick:             tick,
\t\tDelta:            snapshot.delta,
\t}
\tif !snapshot.flightRequested {
\t\tinput.InteractPitch = snapshot.pitch
\t\tinput.InteractYaw = snapshot.yaw
\t\tinput.AnalogueMoveVector = snapshot.moveVector
\t\tinput.CameraOrientation = camera
\t\tinput.RawMoveVector = snapshot.moveVector
\t}
\treturn input
'''
    full_return = '''\treturn &packet.PlayerAuthInput{
\t\tPitch:              snapshot.pitch,
\t\tYaw:                snapshot.yaw,
\t\tPosition:           snapshot.position,
\t\tMoveVector:         snapshot.moveVector,
\t\tHeadYaw:            snapshot.headYaw,
\t\tInputData:          flags,
\t\tInputMode:          packet.InputModeMouse,
\t\tPlayMode:           packet.PlayModeScreen,
\t\tInteractionModel:   packet.InteractionModelCrosshair,
\t\tInteractPitch:      snapshot.pitch,
\t\tInteractYaw:        snapshot.yaw,
\t\tTick:               tick,
\t\tDelta:              snapshot.delta,
\t\tAnalogueMoveVector: snapshot.moveVector,
\t\tCameraOrientation:  camera,
\t\tRawMoveVector:      snapshot.moveVector,
\t}
'''
    if minimal_return in text:
        text = text.replace(minimal_return, full_return, 1)
    elif full_return not in text:
        raise SystemExit("full PlayerAuthInput target not found")

    forced_zero_tick = '''\t\t\ttick := state.nextInputTick()
\t\t\tif cfg.Scenario == ScenarioChunkFly {
\t\t\t\t// Current Bedrock clients use the neutral movement tick for this
\t\t\t\t// server-driven heartbeat path; keep other scenarios unchanged.
\t\t\t\ttick = 0
\t\t\t}
'''
    normal_tick = '''\t\t\ttick := state.nextInputTick()
'''
    if forced_zero_tick in text:
        text = text.replace(forced_zero_tick, normal_tick, 1)
    elif "server-driven heartbeat path" in text:
        raise SystemExit("chunk-fly tick reset target not found")

    path.write_text(text)


def patch_bot(root: pathlib.Path) -> None:
    path = root / "internal/bot/bot.go"
    text = path.read_text()
    publisher_overwrite = '''\t\t\tif cfg.Scenario == ScenarioChunkFly {
\t\t\t\tstate.observePublisherPosition(publisherEyePosition(p.Position))
\t\t\t}
'''
    if publisher_overwrite in text:
        text = text.replace(publisher_overwrite, "", 1)
    path.write_text(text)


def replace_function(text: str, name: str, next_name: str, new_body: str) -> str:
    start = text.find(f"func {name}(")
    end = text.find(f"func {next_name}(", start)
    if start < 0 or end < 0:
        raise SystemExit(f"test function range not found: {name}")
    return text[:start] + new_body.rstrip() + "\n\n" + text[end:]


def patch_tests(root: pathlib.Path) -> None:
    path = root / "internal/bot/flight_test.go"
    text = path.read_text()

    old_baseline = '''\tif input.InputData.Load(packet.InputFlagBlockBreakingDelayEnabled) {
\t\tt.Fatal("flight heartbeat must not include the block-breaking-delay baseline flag")
\t}
'''
    new_baseline = '''\tif !input.InputData.Load(packet.InputFlagBlockBreakingDelayEnabled) {
\t\tt.Fatal("auth input must include the normal Bedrock block-breaking-delay baseline flag")
\t}
'''
    if old_baseline in text:
        text = text.replace(old_baseline, new_baseline, 1)

    text = replace_function(text, "TestChunkFlyAcknowledgesPublisherSpawnBeforeMovement", "TestPublisherEyePositionUsesAuthoritativeBlockPosition", r'''func TestChunkFlyAcknowledgesPublisherSpawnBeforeHorizontalPrediction(t *testing.T) {
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
	ack := authInputPacket(state, 2)
	if !ack.InputData.Load(packet.InputFlagHandledTeleport) || !ack.InputData.Load(packet.InputFlagStartFlying) {
		t.Fatalf("publisher spawn acknowledgement flags = %+v", ack.InputData)
	}
	if ack.Position != publisherPosition || ack.MoveVector != (mgl32.Vec2{}) || ack.Delta != (mgl32.Vec3{}) {
		t.Fatalf("publisher spawn acknowledgement must be stationary: %+v", ack)
	}

	cruise := authInputPacket(state, 3)
	if cruise.InputData.Load(packet.InputFlagHandledTeleport) {
		t.Fatal("HandledTeleport must only be sent for the acknowledgement frame")
	}
	if cruise.InputData.Load(packet.InputFlagAscend) || cruise.InputData.Load(packet.InputFlagWantUp) {
		t.Fatalf("horizontal-only diagnostic unexpectedly requested ascent: %+v", cruise.InputData)
	}
	if cruise.MoveVector != (mgl32.Vec2{0, 1}) || cruise.Delta[2] <= 0 || cruise.Delta[1] != 0 {
		t.Fatalf("horizontal prediction = move %v delta %v", cruise.MoveVector, cruise.Delta)
	}
	if cruise.Position[1] != publisherPosition[1] {
		t.Fatalf("horizontal diagnostic changed altitude: %v", cruise.Position)
	}
}''')

    text = replace_function(text, "TestPublisherObservationDrivesAuthoritativeCruisePosition", "TestChunkFlyClimbsThenTraversesAtSafeAltitude", r'''func TestPublisherTelemetryDoesNotOverwritePredictedFlightPosition(t *testing.T) {
	state := newPlayerState(mgl32.Vec3{4.5, 80, 8.5}, 0, 0)
	writer := &recordingPacketWriter{}
	fly := NewChunkFlyAction(state, writer, 0)
	if err := fly.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	state.setFlyingConfirmed(true)
	first := authInputPacket(state, 7)
	if first.Delta[2] <= 0 {
		t.Fatalf("first predicted flight delta = %v", first.Delta)
	}
	before, _, _ := state.telemetrySnapshot()
	// NetworkChunkPublisherUpdate remains server evidence only after the initial
	// spawn seed. The bot read loop no longer calls observePublisherPosition here.
	after, _, _ := state.telemetrySnapshot()
	if after != before {
		t.Fatalf("publisher telemetry overwrote prediction: before=%v after=%v", before, after)
	}
}''')

    text = replace_function(text, "TestChunkFlyClimbsThenTraversesAtSafeAltitude", "TestChunkFlyResumesFromServerCorrection", r'''func TestChunkFlyHorizontalDiagnosticKeepsAuthoritativeAltitude(t *testing.T) {
	state := newPlayerState(mgl32.Vec3{0, 64, 0}, 0, 0)
	writer := &recordingPacketWriter{}
	fly := NewChunkFlyAction(state, writer, 0)
	if err := fly.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	state.setFlyingConfirmed(true)

	input := authInputPacket(state, 1)
	if input.InputData.Load(packet.InputFlagAscend) || input.InputData.Load(packet.InputFlagWantUp) {
		t.Fatalf("horizontal diagnostic requested ascent: %+v", input.InputData)
	}
	if input.MoveVector != (mgl32.Vec2{0, 1}) || input.Delta[2] <= 0 || input.Delta[1] != 0 {
		t.Fatalf("horizontal diagnostic = move %v delta %v", input.MoveVector, input.Delta)
	}
	if input.Position[1] != 64 {
		t.Fatalf("horizontal diagnostic altitude = %f, want 64", input.Position[1])
	}
	if input.RawMoveVector != input.MoveVector || input.AnalogueMoveVector != input.MoveVector {
		t.Fatalf("flight packet missing normal raw/analogue movement fields: %+v", input)
	}
}''')

    text = replace_function(text, "TestChunkFlyResumesFromServerCorrection", "TestServerTickSyncDrivesNextAuthInputTick", r'''func TestChunkFlyHorizontalPredictionResumesFromServerCorrection(t *testing.T) {
	state := newPlayerState(mgl32.Vec3{0, 64, 0}, 0, 0)
	writer := &recordingPacketWriter{}
	fly := NewChunkFlyAction(state, writer, 90)
	if err := fly.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	state.setFlyingConfirmed(true)
	corrected := mgl32.Vec3{10, 90, 20}
	state.correct(corrected, 0, 90, 90)

	input := authInputPacket(state, 1)
	if input.Delta[0] >= 0 || input.Delta[1] != 0 {
		t.Fatalf("corrected horizontal flight delta = %v", input.Delta)
	}
	if input.Position[1] != corrected[1] || input.Position[0] >= corrected[0] {
		t.Fatalf("flight did not resume horizontally from correction: %v", input.Position)
	}
	if input.InputData.Load(packet.InputFlagAscend) || input.InputData.Load(packet.InputFlagWantUp) {
		t.Fatalf("horizontal correction recovery requested ascent: %+v", input.InputData)
	}
	_, _, corrections := state.telemetrySnapshot()
	if corrections != 1 {
		t.Fatalf("server corrections = %d, want 1", corrections)
	}
}''')

    path.write_text(text)


def main() -> int:
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    patch_tick(root)
    patch_bot(root)
    patch_tests(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
