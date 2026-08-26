#!/usr/bin/env python3
from pathlib import Path

root = Path("bot-src")

tick = root / "internal/bot/tick.go"
text = tick.read_text()

replacements = [
    (
        "type playerState struct {\n\tmu                  sync.Mutex\n",
        "type predictionFrame struct {\n\ttick  uint64\n\tdelta mgl32.Vec3\n}\n\nconst predictionHistoryLimit = 256\n\ntype playerState struct {\n\tmu                  sync.Mutex\n",
    ),
    (
        "\tserverTick          uint64\n\ttickSynced          bool\n\tcontrol             inputControl\n",
        "\tserverTick          uint64\n\ttickSynced          bool\n\tpredictionHistory   []predictionFrame\n\tcontrol             inputControl\n",
    ),
    (
        "\tverticalDirection float32\n}\n",
        "\tverticalDirection float32\n\tcommittedDelta    mgl32.Vec3\n}\n",
    ),
]
for old, new in replacements:
    if old not in text:
        raise SystemExit(f"tick.go patch target missing: {old!r}")
    text = text.replace(old, new, 1)

correct = """func (s *playerState) correct(position mgl32.Vec3, pitch, yaw, headYaw float32) {
\ts.mu.Lock()
\tdefer s.mu.Unlock()
\ts.position = position
\tif validPlayerPositionY(position[1]) {
\t\ts.positionReady = true
\t}
\ts.pitch = pitch
\ts.yaw = yaw
\ts.headYaw = headYaw
\ts.serverCorrections++
}
"""
extra = """
func (s *playerState) recordPrediction(tick uint64, delta mgl32.Vec3) {
\ts.mu.Lock()
\tdefer s.mu.Unlock()
\ts.predictionHistory = append(s.predictionHistory, predictionFrame{tick: tick, delta: delta})
\tif len(s.predictionHistory) > predictionHistoryLimit {
\t\ts.predictionHistory = append([]predictionFrame(nil), s.predictionHistory[len(s.predictionHistory)-predictionHistoryLimit:]...)
\t}
}

func (s *playerState) correctPrediction(position mgl32.Vec3, pitch, yaw, headYaw float32, tick uint64) {
\ts.mu.Lock()
\tdefer s.mu.Unlock()
\tcurrent := position
\tkeep := s.predictionHistory[:0]
\tfor _, frame := range s.predictionHistory {
\t\tif frame.tick > tick {
\t\t\tcurrent = current.Add(frame.delta)
\t\t\tkeep = append(keep, frame)
\t\t}
\t}
\ts.predictionHistory = keep
\ts.position = current
\tif validPlayerPositionY(position[1]) {
\t\ts.positionReady = true
\t}
\ts.pitch = pitch
\ts.yaw = yaw
\ts.headYaw = headYaw
\ts.serverCorrections++
}
"""
if correct not in text:
    raise SystemExit("tick.go correct() target missing")
text = text.replace(correct, correct + extra, 1)

old = "\ts.position[0] += snapshot.delta[0]\n\ts.position[2] += snapshot.delta[2]\n}\n"
new = "\ts.position[0] += snapshot.delta[0]\n\ts.position[2] += snapshot.delta[2]\n\tsnapshot.committedDelta[0] = snapshot.delta[0]\n\tsnapshot.committedDelta[2] = snapshot.delta[2]\n}\n"
if old not in text:
    raise SystemExit("horizontal movement target missing")
text = text.replace(old, new, 1)

old = "\t// Match the coherent input tuple used by go-test-bds, a current BDS test\n"
new = "\ts.recordPrediction(tick, snapshot.committedDelta)\n\n" + old
if old not in text:
    raise SystemExit("recordPrediction insertion target missing")
text = text.replace(old, new, 1)
tick.write_text(text)

flight = root / "internal/bot/flight_movement.go"
text = flight.read_text()
for old, new in [
    (
        "\t\tsnapshot.delta[1] = step\n\t\ts.position[1] += step\n\t\treturn\n",
        "\t\tsnapshot.delta[1] = step\n\t\ts.position[1] += step\n\t\tsnapshot.committedDelta[1] = step\n\t\treturn\n",
    ),
    (
        "\t\tsnapshot.delta[1] = -step\n\t\ts.position[1] -= step\n\t\treturn\n",
        "\t\tsnapshot.delta[1] = -step\n\t\ts.position[1] -= step\n\t\tsnapshot.committedDelta[1] = -step\n\t\treturn\n",
    ),
]:
    if old not in text:
        raise SystemExit(f"flight movement target missing: {old!r}")
    text = text.replace(old, new, 1)
flight.write_text(text)

bot = root / "internal/bot/bot.go"
text = bot.read_text()
old = """\t\tcase *packet.CorrectPlayerMovePrediction:
\t\t\tif p.PredictionType == packet.PredictionTypePlayer {
\t\t\t\tstate.correct(p.Position, p.Rotation[0], p.Rotation[1], p.Rotation[1])
\t\t\t\tstate.syncServerTick(p.Tick)
\t\t\t}
"""
new = """\t\tcase *packet.CorrectPlayerMovePrediction:
\t\t\tif p.PredictionType == packet.PredictionTypePlayer {
\t\t\t\tstate.correctPrediction(p.Position, p.Rotation[0], p.Rotation[1], p.Rotation[1], p.Tick)
\t\t\t\tstate.syncServerTick(p.Tick)
\t\t\t}
"""
if old not in text:
    raise SystemExit("bot.go correction handler target missing")
bot.write_text(text.replace(old, new, 1))

tests = root / "internal/bot/tick_test.go"
text = tests.read_text()
text += """

func TestCorrectionReplaysPredictionsAfterCorrectedTick(t *testing.T) {
\tstate := newPlayerState(mgl32.Vec3{10, 70, 20}, 0, 0)
\tstate.setMoveControl(mgl32.Vec2{0, 1}, chunkWalkStepPerTick, 0)
\t_ = authInputPacket(state, 40)
\t_ = authInputPacket(state, 41)
\t_ = authInputPacket(state, 42)

\tcorrected := mgl32.Vec3{10, 70, 20.25}
\tstate.correctPrediction(corrected, 0, 0, 0, 40)
\tposition, _, corrections := state.telemetrySnapshot()
\twantZ := corrected[2] + 2*chunkWalkStepPerTick
\tif math.Abs(float64(position[2]-wantZ)) > 1e-5 || position[0] != corrected[0] || position[1] != corrected[1] {
\t\tt.Fatalf("replayed position = %v, want x=%f y=%f z=%f", position, corrected[0], corrected[1], wantZ)
\t}
\tif corrections != 1 {
\t\tt.Fatalf("server corrections = %d, want 1", corrections)
\t}
}

func TestCorrectionReplayDoesNotCommitGroundGravityDelta(t *testing.T) {
\tstate := newPlayerState(mgl32.Vec3{0, 64, 0}, 0, 0)
\tstate.setMoveControl(mgl32.Vec2{0, 1}, chunkWalkStepPerTick, 0)
\tpk := authInputPacket(state, 2)
\tif pk.Delta[1] >= 0 {
\t\tt.Fatalf("walk packet should carry ground gravity delta: %v", pk.Delta)
\t}
\tstate.correctPrediction(mgl32.Vec3{0, 64, 0}, 0, 0, 0, 1)
\tposition, _, _ := state.telemetrySnapshot()
\tif position[1] != 64 {
\t\tt.Fatalf("gravity delta was replayed into prediction position: %v", position)
\t}
\tif math.Abs(float64(position[2]-chunkWalkStepPerTick)) > 1e-5 {
\t\tt.Fatalf("horizontal prediction was not replayed: %v", position)
\t}
}
"""
tests.write_text(text)
