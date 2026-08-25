#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import sys


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise SystemExit(f"{label}: target not found")
    return text.replace(old, new, 1)


def patch_bot_go(root: pathlib.Path) -> None:
    bot = root / "internal/bot/bot.go"
    text = bot.read_text()

    if "postMoveUpdates uint64" not in text:
        text = replace_once(
            text,
            "\tvar publisherUpdates uint64\n\tvar publisherX, publisherY, publisherZ int32\n",
            "\tvar publisherUpdates uint64\n\tvar postMoveUpdates uint64\n\tvar postMoveX, postMoveY, postMoveZ float32\n\tpostMoveSet := false\n\tvar publisherX, publisherY, publisherZ int32\n",
            "post-move counters",
        )

    if "case *packet.ServerPlayerPostMovePosition:" not in text:
        text = replace_once(
            text,
            "\t\tcase *packet.MovePlayer:\n",
            "\t\tcase *packet.ServerPlayerPostMovePosition:\n"
            "\t\t\tpostMoveUpdates++\n"
            "\t\t\tpostMoveX, postMoveY, postMoveZ = p.Position[0], p.Position[1], p.Position[2]\n"
            "\t\t\tpostMoveSet = true\n"
            "\t\t\tif cfg.Scenario == ScenarioChunkFly && (postMoveUpdates <= 3 || postMoveUpdates%100 == 0) {\n"
            "\t\t\t\tif err := out.Emit(\"server_post_move\", map[string]any{\n"
            "\t\t\t\t\t\"position\": []float32{postMoveX, postMoveY, postMoveZ},\n"
            "\t\t\t\t\t\"updates\":  postMoveUpdates,\n"
            "\t\t\t\t}); err != nil {\n"
            "\t\t\t\t\treturn stageError(ExitRuntime, \"output\", err)\n"
            "\t\t\t\t}\n"
            "\t\t\t}\n"
            "\t\tcase *packet.MovePlayer:\n",
            "post-move packet handler",
        )

    if '"server_post_move_updates"' not in text:
        text = replace_once(
            text,
            "\t\t\t\t\"publisher_updates\":    publisherUpdates,\n\t\t\t}\n",
            "\t\t\t\t\"publisher_updates\":       publisherUpdates,\n"
            "\t\t\t\t\"server_post_move_updates\": postMoveUpdates,\n"
            "\t\t\t}\n"
            "\t\t\tif postMoveSet {\n"
            "\t\t\t\tfields[\"server_post_move_position\"] = []float32{postMoveX, postMoveY, postMoveZ}\n"
            "\t\t\t}\n",
            "post-move progress fields",
        )

    if "input.InputData.Load(packet.InputFlagWantUp)" not in text:
        old = """\t\tif input.MoveVector[0] != 0 || input.MoveVector[1] != 0 ||
\t\t\tinput.Delta[0] != 0 || input.Delta[1] != 0 || input.Delta[2] != 0 {
\t\t\tw.movementCount.Add(1)
\t\t}
"""
        new = """\t\tmoving := input.MoveVector[0] != 0 || input.MoveVector[1] != 0 ||
\t\t\tinput.Delta[0] != 0 || input.Delta[1] != 0 || input.Delta[2] != 0 ||
\t\t\tinput.InputData.Load(packet.InputFlagAscend) || input.InputData.Load(packet.InputFlagDescend) ||
\t\t\tinput.InputData.Load(packet.InputFlagWantUp) || input.InputData.Load(packet.InputFlagWantDown)
\t\tif moving {
\t\t\tw.movementCount.Add(1)
\t\t}
"""
        text = replace_once(text, old, new, "movement intent counter")

    if "state.observePublisherPosition(publisherEyePosition(p.Position))" not in text:
        needle = """\t\t\tpublisherX, publisherY, publisherZ = x, y, z
"""
        insertion = """\t\t\tif cfg.Scenario == ScenarioChunkFly {
\t\t\t\tstate.observePublisherPosition(publisherEyePosition(p.Position))
\t\t\t}
\t\t\tpublisherX, publisherY, publisherZ = x, y, z
"""
        text = replace_once(text, needle, insertion, "publisher state observation")

    if "stats.StartPosition = candidate" not in text:
        needle = """\t\t\t\tif state.acceptPublisherPosition(candidate) {
\t\t\t\t\tif err := out.Emit("authoritative_position", map[string]any{
"""
        insertion = """\t\t\t\tif state.acceptPublisherPosition(candidate) {
\t\t\t\t\tstats.StartPosition = candidate
\t\t\t\t\tif err := out.Emit("authoritative_position", map[string]any{
"""
        text = replace_once(text, needle, insertion, "authoritative start position")

    bot.write_text(text)


def patch_tick_go(root: pathlib.Path) -> None:
    tick = root / "internal/bot/tick.go"
    text = tick.read_text()

    if "func (s *playerState) observePublisherPosition(" not in text:
        marker = """func (s *playerState) positionReadySnapshot() bool {
"""
        addition = """func (s *playerState) observePublisherPosition(position mgl32.Vec3) bool {
\ts.mu.Lock()
\tdefer s.mu.Unlock()
\tif !s.positionReady || !validPlayerPositionY(position[1]) {
\t\treturn false
\t}
\tchanged := s.position != position
\ts.position = position
\treturn changed
}

func (s *playerState) positionReadySnapshot() bool {
"""
        text = replace_once(text, marker, addition, "publisher observation method")

    old_fly = """\t} else if s.control.fly {
\t\tif !s.flyingConfirmed || !s.positionReady {
\t\t\t// StartGame may contain the Bedrock placeholder altitude around
\t\t\t// Y=32768. Do not fabricate movement from it. Wait until BDS has
\t\t\t// acknowledged flying and published a stable authoritative position.
\t\t\tsnapshot.moveVector = mgl32.Vec2{}
\t\t} else {
\t\t\tdiff := s.control.flightTargetY - s.position[1]
\t\t\tif float32(math.Abs(float64(diff))) > 0.05 {
\t\t\t\tsnapshot.moveVector = mgl32.Vec2{}
\t\t\t\tstep := s.control.verticalStep
\t\t\t\tif step <= 0 {
\t\t\t\t\tstep = float32(math.Abs(float64(diff)))
\t\t\t\t}
\t\t\t\tif float32(math.Abs(float64(diff))) < step {
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
"""
    new_fly = """\t} else if s.control.fly {
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
"""
    if "let BDS advance the player" not in text:
        text = replace_once(text, old_fly, new_fly, "server-driven flight snapshot")

    tick.write_text(text)


def patch_flight_tests(root: pathlib.Path) -> None:
    test = root / "internal/bot/flight_test.go"
    text = test.read_text()
    text = text.replace('\n\t"math"', "", 1)

    text = text.replace(
        """\tif climb.Delta[1] <= 0 || climb.MoveVector != (mgl32.Vec2{}) {
\t\tt.Fatalf("post-ack climb did not resume prediction: %+v", climb)
\t}
""",
        """\tif climb.Delta != (mgl32.Vec3{}) || climb.MoveVector != (mgl32.Vec2{}) {
\t\tt.Fatalf("post-ack climb must be server-driven with zero client delta: %+v", climb)
\t}
""",
        1,
    )
    text = text.replace(
        """\tif climb.Delta[1] <= 0 || climb.MoveVector != (mgl32.Vec2{}) {
\t\tt.Fatalf("climb packet = delta %v move %v", climb.Delta, climb.MoveVector)
\t}

\tstate.update(mgl32.Vec3{0, fly.targetY, 0}, 0, 0, 0, false)
""",
        """\tif climb.Delta != (mgl32.Vec3{}) || climb.MoveVector != (mgl32.Vec2{}) {
\t\tt.Fatalf("climb packet must carry input intent without client prediction: delta %v move %v", climb.Delta, climb.MoveVector)
\t}

\tif !state.observePublisherPosition(mgl32.Vec3{0, fly.targetY, 0}) {
\t\tt.Fatal("server publisher update did not advance flight state")
\t}
""",
        1,
    )
    text = text.replace(
        """\tif math.Abs(float64(cruise.Delta[2]-chunkFlyStepPerTick)) > 1e-5 || math.Abs(float64(cruise.Delta[1])) > 1e-5 {
\t\tt.Fatalf("cruise delta = %v", cruise.Delta)
\t}
""",
        """\tif cruise.Delta != (mgl32.Vec3{}) {
\t\tt.Fatalf("server-driven cruise must not fabricate client delta: %v", cruise.Delta)
\t}
""",
        1,
    )
    text = text.replace(
        """\tif input.Delta[1] <= 0 || input.MoveVector != (mgl32.Vec2{}) {
\t\tt.Fatalf("corrected flight should recover altitude before horizontal traversal: %+v", input)
\t}
""",
        """\tif input.Delta != (mgl32.Vec3{}) || input.MoveVector != (mgl32.Vec2{}) ||
\t\t!input.InputData.Load(packet.InputFlagAscend) || !input.InputData.Load(packet.InputFlagWantUp) {
\t\tt.Fatalf("corrected flight should recover altitude through server-driven ascent intent: %+v", input)
\t}
""",
        1,
    )

    if "TestPublisherObservationDrivesAuthoritativeCruisePosition" not in text:
        marker = """func TestChunkFlyClimbsThenTraversesAtSafeAltitude(t *testing.T) {
"""
        addition = """func TestPublisherObservationDrivesAuthoritativeCruisePosition(t *testing.T) {
\tstate := newPlayerState(mgl32.Vec3{4.5, 80, 8.5}, 0, 0)
\twriter := &recordingPacketWriter{}
\tfly := NewChunkFlyAction(state, writer, 0)
\tif err := fly.Start(context.Background()); err != nil {
\t\tt.Fatal(err)
\t}
\tstate.setFlyingConfirmed(true)
\tserverPos := mgl32.Vec3{7.5, fly.targetY, 11.5}
\tif !state.observePublisherPosition(serverPos) {
\t\tt.Fatal("publisher position was not observed")
\t}
\tinput := authInputPacket(state, 7)
\tif input.Position != serverPos {
\t\tt.Fatalf("auth input position = %v, want server position %v", input.Position, serverPos)
\t}
\tif input.MoveVector != (mgl32.Vec2{0, 1}) || input.Delta != (mgl32.Vec3{}) {
\t\tt.Fatalf("server-driven cruise = move %v delta %v", input.MoveVector, input.Delta)
\t}
}

func TestChunkFlyClimbsThenTraversesAtSafeAltitude(t *testing.T) {
"""
        text = replace_once(text, marker, addition, "publisher cruise test")

    old_writer = """\tif err := writer.WritePacket(&packet.PlayerAuthInput{Delta: mgl32.Vec3{0, 0.4, 0}}); err != nil {
\t\tt.Fatal(err)
\t}
"""
    new_writer = """\tflags := protocol.NewInputFlags(packet.InputFlagCount)
\tflags.Set(packet.InputFlagAscend)
\tflags.Set(packet.InputFlagWantUp)
\tif err := writer.WritePacket(&packet.PlayerAuthInput{InputData: flags}); err != nil {
\t\tt.Fatal(err)
\t}
"""
    if old_writer in text:
        text = text.replace(old_writer, new_writer, 1)

    test.write_text(text)


def main() -> int:
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    patch_bot_go(root)
    patch_tick_go(root)
    patch_flight_tests(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
