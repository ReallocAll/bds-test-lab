#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import sys


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise SystemExit(f"{label}: target not found")
    return text.replace(old, new, 1)


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: patch_chunk_fly_matrix_variant.py BOT_ROOT VARIANT")
    root = pathlib.Path(sys.argv[1]).resolve()
    variant = sys.argv[2]
    allowed = {
        "tick1-predicted",
        "tick1-no-block",
        "tick0-predicted",
        "tick0-transition-fly",
        "tick0-camera-relative",
        "tick0-play-normal",
        "tick0-zero-delta",
        "tick0-client-ack",
        "tick0-no-climb",
        "tick0-server-driven",
    }
    if variant not in allowed:
        raise SystemExit(f"unknown variant: {variant}")

    tick = root / "internal/bot/tick.go"
    text = tick.read_text()
    if variant.startswith("tick1-"):
        text = replace_once(text, "\t\tTick:               tick,\n", "\t\tTick:               tick + 1,\n", "Tick field")
        if variant == "tick1-no-block":
            text = replace_once(text, "\tflags.Set(packet.InputFlagBlockBreakingDelayEnabled)\n", "", "block-breaking-delay flag")
    else:
        text = replace_once(text, "\t\tTick:               tick,\n", "\t\tTick:               0,\n", "Tick field")

    if variant == "tick0-transition-fly":
        text = replace_once(
            text,
            "\t\tflightRequested: s.control.fly,\n",
            "\t\tflightRequested: s.control.fly && !s.flyingConfirmed,\n",
            "flightRequested",
        )
    elif variant == "tick0-camera-relative":
        old = "\tflags.Set(packet.InputFlagBlockBreakingDelayEnabled)\n"
        text = replace_once(text, old, old + "\tflags.Set(packet.InputFlagCameraRelativeMovementEnabled)\n", "camera-relative flag")
    elif variant == "tick0-play-normal":
        text = replace_once(
            text,
            "\t\tPlayMode:           packet.PlayModeScreen,\n",
            "\t\tPlayMode:           packet.PlayModeNormal,\n",
            "PlayMode",
        )
    elif variant == "tick0-zero-delta":
        text = replace_once(
            text,
            "\t\tDelta:              snapshot.delta,\n",
            "\t\tDelta:              mgl32.Vec3{},\n",
            "Delta",
        )
    elif variant == "tick0-client-ack":
        old = "\tflags.Set(packet.InputFlagBlockBreakingDelayEnabled)\n"
        text = replace_once(text, old, old + "\tflags.Set(packet.InputFlagClientAckServerData)\n", "client-ack flag")
    elif variant == "tick0-no-climb":
        text = replace_once(
            text,
            "\t\t\tdiff := s.control.flightTargetY - s.position[1]\n",
            "\t\t\tdiff := float32(0) // diagnostic: cruise immediately at authoritative spawn height\n",
            "flight diff",
        )
    elif variant == "tick0-server-driven":
        start_marker = "\t} else if s.control.fly {\n"
        end_marker = "\t} else if snapshot.moveVector != (mgl32.Vec2{})"
        start = text.find(start_marker)
        if start < 0:
            raise SystemExit("server-driven start marker not found")
        end = text.find(end_marker, start)
        if end < 0:
            raise SystemExit("server-driven end marker not found")
        replacement = (
            "\t} else if s.control.fly {\n"
            "\t\tif !s.flyingConfirmed || !s.positionReady {\n"
            "\t\t\tsnapshot.moveVector = mgl32.Vec2{}\n"
            "\t\t} else if s.position[1] < s.control.flightTargetY-0.75 {\n"
            "\t\t\tsnapshot.moveVector = mgl32.Vec2{}\n"
            "\t\t\tsnapshot.verticalDirection = 1\n"
            "\t\t}\n"
        )
        text = text[:start] + replacement + text[end:]

    tick.write_text(text)

    if variant == "tick0-server-driven":
        bot = root / "internal/bot/bot.go"
        b = bot.read_text()
        needle = "\t\t\tpublisherX, publisherY, publisherZ = x, y, z\n"
        insertion = (
            "\t\t\tif cfg.Scenario == ScenarioChunkFly && state.positionReadySnapshot() {\n"
            "\t\t\t\tstate.observePublisherPosition(publisherEyePosition(p.Position))\n"
            "\t\t\t}\n"
            + needle
        )
        b = replace_once(b, needle, insertion, "publisher observation")
        bot.write_text(b)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
