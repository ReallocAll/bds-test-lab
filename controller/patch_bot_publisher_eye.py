#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import sys


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise SystemExit(f"{label} target not found")
    return text.replace(old, new, 1)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_bot_publisher_eye.py <bot-src>")
    root = pathlib.Path(sys.argv[1])

    bot = root / "internal/bot/bot.go"
    text = bot.read_text()
    text = replace_once(
        text,
        "candidate := publisherEyePosition(p.Position)",
        "candidate := publisherEyePosition(p.Position, game.PlayerPosition[1])",
        "publisher eye call",
    )
    bot.write_text(text)

    flight = root / "internal/bot/flight.go"
    text = flight.read_text()
    old = '''func publisherEyePosition(position protocol.BlockPos) mgl32.Vec3 {
\treturn mgl32.Vec3{
\t\tfloat32(position[0]) + 0.5,
\t\tfloat32(position[1]) + playerEyeHeight,
\t\tfloat32(position[2]) + 0.5,
\t}
}
'''
    new = '''func publisherEyePosition(position protocol.BlockPos, startGameY float32) mgl32.Vec3 {
\t// NetworkChunkPublisherUpdate contains a BlockPos, so its Y loses the
\t// player's fractional feet offset. StartGame's temporary Y is unusable as
\t// an absolute coordinate, but its fractional component remains the actual
\t// spawn fraction (BDS querytarget reports the same .62 in this build).
\t// PlayerAuthInput.Position is the eye position, so reconstruct feet Y first
\t// and then add the 1.62 eye offset.
\t_, fraction := math.Modf(float64(startGameY))
\tif fraction < 0 {
\t\tfraction += 1
\t}
\treturn mgl32.Vec3{
\t\tfloat32(position[0]) + 0.5,
\t\tfloat32(position[1]) + float32(fraction) + playerEyeHeight,
\t\tfloat32(position[2]) + 0.5,
\t}
}
'''
    text = replace_once(text, old, new, "publisher eye implementation")
    flight.write_text(text)

    flight_test = root / "internal/bot/flight_test.go"
    text = flight_test.read_text()
    old = '''func TestPublisherEyePositionUsesAuthoritativeBlockPosition(t *testing.T) {
\tgot := publisherEyePosition(protocol.BlockPos{266, 70, 159})
\twant := mgl32.Vec3{266.5, 70 + playerEyeHeight, 159.5}
\tif got != want {
\t\tt.Fatalf("publisher eye position = %v, want %v", got, want)
\t}
}
'''
    new = '''func TestPublisherEyePositionUsesAuthoritativeBlockPosition(t *testing.T) {
\tgot := publisherEyePosition(protocol.BlockPos{266, 70, 159}, 32769.62)
\twant := mgl32.Vec3{266.5, 70 + 0.62 + playerEyeHeight, 159.5}
\tif math.Abs(float64(got[1]-want[1])) > 0.002 || got[0] != want[0] || got[2] != want[2] {
\t\tt.Fatalf("publisher eye position = %v, want %v", got, want)
\t}
}
'''
    text = replace_once(text, old, new, "publisher eye test")
    flight_test.write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
