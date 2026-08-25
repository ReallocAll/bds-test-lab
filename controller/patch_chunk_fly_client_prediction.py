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

    neutral_tick = '''func (s *playerState) nextInputTick() uint64 {
\ts.mu.Lock()
\tdefer s.mu.Unlock()
\t// PlayerAuthInput.Tick is a server tick, not a client-local frame
\t// counter. Until a packet carrying a server tick establishes the
\t// clock, keep the protocol's neutral zero value rather than sending
\t// an ever-growing unrelated tick sequence.
\tif !s.tickSynced {
\t\treturn 0
\t}
\ttick := s.serverTick
\ts.serverTick++
\treturn tick
}
'''
    client_tick = '''func (s *playerState) nextInputTick() uint64 {
\ts.mu.Lock()
\tdefer s.mu.Unlock()
\t// PlayerAuthInput carries the client's monotonically advancing movement
\t// frame id. Server packets may move this clock forward when they refer to
\t// a later prediction, but initial movement starts at frame zero.
\ttick := s.serverTick
\ts.serverTick++
\treturn tick
}
'''
    if neutral_tick in text:
        text = text.replace(neutral_tick, client_tick, 1)
    elif "client's monotonically advancing movement" not in text:
        raise SystemExit("client movement tick target not found")

    server_driven = '''\t} else if s.control.fly {
\t\tif !s.flyingConfirmed || !s.positionReady {
\t\t\t// StartGame may contain the Bedrock placeholder altitude around
\t\t\t// Y=32768. Wait for stable server-owned publisher coordinates and
\t\t\t// a flight acknowledgement before sending movement intent.
\t\t\tsnapshot.moveVector = mgl32.Vec2{}
\t\t} else if s.position[1] < s.control.flightTargetY-0.75 {
\t\t\t// Server-authoritative movement is driven by input state. Do not
\t\t\t// fabricate a client position/delta for flight: request ascent and
\t\t\t// let BDS advance the player, then consume publisher feedback.
\t\t\tsnapshot.moveVector = mgl32.Vec2{}
\t\t\tsnapshot.verticalDirection = 1
\t\t}
'''
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
    if server_driven in text:
        text = text.replace(server_driven, client_predicted, 1)
    elif "Bedrock server-authoritative movement is still client-predicted" not in text:
        raise SystemExit("client prediction target not found")

    ungated = '''\t\tcase <-ticker.C:
\t\t\ttick := state.nextInputTick()
\t\t\tif err := runner.Tick(ctx, action.TickContext{Tick: tick}); err != nil {
\t\t\t\treturn err
\t\t\t}
\t\t\tif err := writer.WritePacket(authInputPacket(state, tick)); err != nil {
'''
    gated = '''\t\tcase <-ticker.C:
\t\t\t// Do not establish PlayerAuthInput movement history using the
\t\t\t// temporary StartGame altitude. RequestAbility is already sent from
\t\t\t// FlyAction.Start; PlayerAuthInput begins only after BDS publishes a
\t\t\t// stable, valid player position that we can acknowledge.
\t\t\tif cfg.Scenario == ScenarioChunkFly && !state.positionReadySnapshot() {
\t\t\t\tcontinue
\t\t\t}
\t\t\ttick := state.nextInputTick()
\t\t\tif err := runner.Tick(ctx, action.TickContext{Tick: tick}); err != nil {
\t\t\t\treturn err
\t\t\t}
\t\t\tif err := writer.WritePacket(authInputPacket(state, tick)); err != nil {
'''
    if ungated in text:
        text = text.replace(ungated, gated, 1)
    elif "PlayerAuthInput begins only after BDS publishes" not in text:
        raise SystemExit("pre-authoritative auth-input gate target not found")

    path.write_text(text)


def patch_bot(root: pathlib.Path) -> None:
    path = root / "internal/bot/bot.go"
    text = path.read_text()
    overwrite = '''\t\t\tif cfg.Scenario == ScenarioChunkFly {
\t\t\t\tstate.observePublisherPosition(publisherEyePosition(p.Position))
\t\t\t}
'''
    if overwrite in text:
        text = text.replace(overwrite, "", 1)
    path.write_text(text)


def patch_tests(root: pathlib.Path) -> None:
    path = root / "internal/bot/flight_test.go"
    text = path.read_text()

    text = text.replace(
        '''\tif got := state.nextInputTick(); got != 0 {
\t\tt.Fatalf("initial input tick = %d, want 0", got)
\t}
\tif got := state.nextInputTick(); got != 0 {
\t\tt.Fatalf("unsynced input tick = %d, want neutral 0", got)
\t}
\tstate.syncServerTick(240)
''',
        '''\tif got := state.nextInputTick(); got != 0 {
\t\tt.Fatalf("initial input tick = %d, want 0", got)
\t}
\tif got := state.nextInputTick(); got != 1 {
\t\tt.Fatalf("second input tick = %d, want 1", got)
\t}
\tstate.syncServerTick(240)
''',
        1,
    )

    text = text.replace(
        '''\tif climb.Delta != (mgl32.Vec3{}) || climb.MoveVector != (mgl32.Vec2{}) {
\t\tt.Fatalf("post-ack climb must be server-driven with zero client delta: %+v", climb)
\t}
''',
        '''\tif climb.Delta[1] <= 0 || climb.MoveVector != (mgl32.Vec2{}) {
\t\tt.Fatalf("post-ack climb did not include client prediction: %+v", climb)
\t}
''',
        1,
    )
    text = text.replace(
        '''\tif climb.Delta != (mgl32.Vec3{}) || climb.MoveVector != (mgl32.Vec2{}) {
\t\tt.Fatalf("climb packet must carry input intent without client prediction: delta %v move %v", climb.Delta, climb.MoveVector)
\t}

\tif !state.observePublisherPosition(mgl32.Vec3{0, fly.targetY, 0}) {
\t\tt.Fatal("server publisher update did not advance flight state")
\t}
''',
        '''\tif climb.Delta[1] <= 0 || climb.MoveVector != (mgl32.Vec2{}) {
\t\tt.Fatalf("climb packet = delta %v move %v", climb.Delta, climb.MoveVector)
\t}

\tstate.update(mgl32.Vec3{0, fly.targetY, 0}, 0, 0, 0, false)
''',
        1,
    )
    text = text.replace(
        '''\tif cruise.Delta != (mgl32.Vec3{}) {
\t\tt.Fatalf("server-driven cruise must not fabricate client delta: %v", cruise.Delta)
\t}
''',
        '''\tif math.Abs(float64(cruise.Delta[2]-chunkFlyStepPerTick)) > 1e-5 || math.Abs(float64(cruise.Delta[1])) > 1e-5 {
\t\tt.Fatalf("cruise delta = %v", cruise.Delta)
\t}
''',
        1,
    )
    text = text.replace(
        '''\tif input.Delta != (mgl32.Vec3{}) || input.MoveVector != (mgl32.Vec2{}) ||
\t\t!input.InputData.Load(packet.InputFlagAscend) || !input.InputData.Load(packet.InputFlagWantUp) {
\t\tt.Fatalf("corrected flight should recover altitude through server-driven ascent intent: %+v", input)
\t}
''',
        '''\tif input.Delta[1] <= 0 || input.MoveVector != (mgl32.Vec2{}) {
\t\tt.Fatalf("corrected flight should recover altitude before horizontal traversal: %+v", input)
\t}
''',
        1,
    )

    if '"math"' not in text.split(')', 1)[0]:
        text = text.replace('import (\n\t"context"\n', 'import (\n\t"context"\n\t"math"\n', 1)

    path.write_text(text)


def main() -> int:
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    patch_tick(root)
    patch_bot(root)
    patch_tests(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
