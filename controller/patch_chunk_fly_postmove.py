#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import sys


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise SystemExit(f"{label}: target not found")
    return text.replace(old, new, 1)


def main() -> int:
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
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

    bot.write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
