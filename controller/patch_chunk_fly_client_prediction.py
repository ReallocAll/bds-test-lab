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

    client_predicted = '''\t} else if s.control.fly {
\t\tif !s.flyingConfirmed || !s.positionReady {
\t\t\t// Never predict from StartGame's temporary Y≈32768 position. The
\t\t\t// first valid publisher position seeds the movement history below.
\t\t\tsnapshot.moveVector = mgl32.Vec2{}
\t\t} else {
\t\t\tdiff := s.control.flightTargetY - s.position[1]
\t\t\tif float32(math.Abs(float64(diff))) > 0.05 {
\t\t\t\t// Bedrock server-authoritative movement is still client-predicted:
\t\t\t\t// send the input state together with the resulting predicted
\t\t\t\t// position/delta for this movement frame.
\t\t\t\tsnapshot.moveVector = mgl32.Vec2{}
\t\t\t\tstep := s.control.verticalStep
\t\t\t\tif step <= 0 || float32(math.Abs(float64(diff))) < step {
\t\t\t\t\tstep = float32(math.Abs(float64(diff)))
\t\t\t\t}
\t\t\t\tif diff < 0 {
\t\t\t\t\tstep = -step
\t\t\t\t}
\t\t\t\tsnapshot.delta[1] = step
\t\t\t\tsnapshot.verticalDirection = step
\t\t\t\ts.position[1] += step
\t\t\t} else {
\t\t\t\tapplyHorizontalMovement(s, &snapshot, s.control.moveStep)
\t\t\t}
\t\t}
'''
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
    if client_predicted in text:
        text = text.replace(client_predicted, server_driven, 1)
    elif "Match a real client heartbeat" not in text:
        raise SystemExit("server-driven flight target not found")

    old_tick = '''\t\t\ttick := state.nextInputTick()
\t\t\tif err := runner.Tick(ctx, action.TickContext{Tick: tick}); err != nil {
'''
    new_tick = '''\t\t\ttick := state.nextInputTick()
\t\t\tif cfg.Scenario == ScenarioChunkFly {
\t\t\t\t// Current Bedrock clients use the neutral movement tick for this
\t\t\t\t// server-driven heartbeat path; keep other scenarios unchanged.
\t\t\t\ttick = 0
\t\t\t}
\t\t\tif err := runner.Tick(ctx, action.TickContext{Tick: tick}); err != nil {
'''
    if old_tick in text:
        text = text.replace(old_tick, new_tick, 1)
    elif "neutral movement tick for this" not in text:
        raise SystemExit("chunk-fly neutral tick target not found")

    old_flags = '''\tflags := protocol.NewInputFlags(packet.InputFlagCount)
\tflags.Set(packet.InputFlagBlockBreakingDelayEnabled)
'''
    new_flags = '''\tflags := protocol.NewInputFlags(packet.InputFlagCount)
\tif !snapshot.flightRequested {
\t\tflags.Set(packet.InputFlagBlockBreakingDelayEnabled)
\t}
'''
    if old_flags in text:
        text = text.replace(old_flags, new_flags, 1)
    elif "if !snapshot.flightRequested" not in text:
        raise SystemExit("chunk-fly baseline flags target not found")

    old_return = '''\treturn &packet.PlayerAuthInput{
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
    new_return = '''\tinput := &packet.PlayerAuthInput{
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
    if old_return in text:
        text = text.replace(old_return, new_return, 1)
    elif "input := &packet.PlayerAuthInput" not in text:
        raise SystemExit("minimal flight packet target not found")

    path.write_text(text)


def patch_bot(root: pathlib.Path) -> None:
    path = root / "internal/bot/bot.go"
    text = path.read_text()
    marker = '''\t\t\tif cfg.Scenario == ScenarioChunkFly {
\t\t\t\tstate.observePublisherPosition(publisherEyePosition(p.Position))
\t\t\t}
'''
    if marker not in text:
        anchor = '''\t\t\tpublisherX, publisherY, publisherZ = x, y, z
'''
        text = replace_once(text, anchor, marker + anchor, "publisher feedback")
    path.write_text(text)


def patch_tests(root: pathlib.Path) -> None:
    path = root / "internal/bot/flight_test.go"
    text = path.read_text()

    text = text.replace(
        '''\tif climb.Delta[1] <= 0 || climb.MoveVector != (mgl32.Vec2{}) {
\t\tt.Fatalf("post-ack climb did not include client prediction: %+v", climb)
\t}
''',
        '''\tif climb.Delta != (mgl32.Vec3{}) || climb.MoveVector != (mgl32.Vec2{}) {
\t\tt.Fatalf("post-ack climb must be server-driven with zero client delta: %+v", climb)
\t}
''', 1)

    old_observe = '''\tinput := authInputPacket(state, 7)
\twantPosition := serverPos
\twantPosition[2] += chunkFlyStepPerTick
\tif input.Position != wantPosition {
\t\tt.Fatalf("auth input position = %v, want predicted position %v", input.Position, wantPosition)
\t}
\tif input.MoveVector != (mgl32.Vec2{0, 1}) || math.Abs(float64(input.Delta[2]-chunkFlyStepPerTick)) > 1e-5 {
\t\tt.Fatalf("client-predicted cruise = move %v delta %v", input.MoveVector, input.Delta)
\t}
'''
    new_observe = '''\tinput := authInputPacket(state, 0)
\tif input.Position != serverPos {
\t\tt.Fatalf("auth input position = %v, want server position %v", input.Position, serverPos)
\t}
\tif input.MoveVector != (mgl32.Vec2{0, 1}) || input.Delta != (mgl32.Vec3{}) {
\t\tt.Fatalf("server-driven cruise = move %v delta %v", input.MoveVector, input.Delta)
\t}
\tif input.Tick != 0 || input.RawMoveVector != (mgl32.Vec2{}) || input.AnalogueMoveVector != (mgl32.Vec2{}) {
\t\tt.Fatalf("flight heartbeat must keep tick/raw/analogue neutral: %+v", input)
\t}
'''
    if old_observe in text:
        text = text.replace(old_observe, new_observe, 1)
    elif "flight heartbeat must keep tick/raw/analogue neutral" not in text:
        raise SystemExit("publisher cruise test target not found")

    text = text.replace(
        '''\tif climb.Delta[1] <= 0 || climb.MoveVector != (mgl32.Vec2{}) {
\t\tt.Fatalf("climb packet = delta %v move %v", climb.Delta, climb.MoveVector)
\t}

\tstate.update(mgl32.Vec3{0, fly.targetY, 0}, 0, 0, 0, false)
''',
        '''\tif climb.Delta != (mgl32.Vec3{}) || climb.MoveVector != (mgl32.Vec2{}) {
\t\tt.Fatalf("climb packet must carry ascent intent without client delta: delta %v move %v", climb.Delta, climb.MoveVector)
\t}

\tif !state.observePublisherPosition(mgl32.Vec3{0, fly.targetY, 0}) {
\t\tt.Fatal("server publisher update did not advance flight state")
\t}
''', 1)

    text = text.replace(
        '''\tif math.Abs(float64(cruise.Delta[2]-chunkFlyStepPerTick)) > 1e-5 || math.Abs(float64(cruise.Delta[1])) > 1e-5 {
\t\tt.Fatalf("cruise delta = %v", cruise.Delta)
\t}
''',
        '''\tif cruise.Delta != (mgl32.Vec3{}) {
\t\tt.Fatalf("server-driven cruise must not fabricate client delta: %v", cruise.Delta)
\t}
''', 1)

    text = text.replace(
        '''\tif input.Delta[1] <= 0 || input.MoveVector != (mgl32.Vec2{}) {
\t\tt.Fatalf("corrected flight should recover altitude before horizontal traversal: %+v", input)
\t}
''',
        '''\tif input.Delta != (mgl32.Vec3{}) || input.MoveVector != (mgl32.Vec2{}) ||
\t\t!input.InputData.Load(packet.InputFlagAscend) || !input.InputData.Load(packet.InputFlagWantUp) {
\t\tt.Fatalf("corrected flight should recover altitude through server-driven ascent intent: %+v", input)
\t}
''', 1)

    path.write_text(text)


def main() -> int:
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    patch_tick(root)
    patch_bot(root)
    patch_tests(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
