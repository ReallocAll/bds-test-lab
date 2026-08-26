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
        raise SystemExit("usage: patch_gophertunnel_spawn.py <minecraft/conn.go>")
    path = pathlib.Path(sys.argv[1])
    text = path.read_text()

    import_old = '\t"github.com/coreos/go-oidc/v3/oidc"\n'
    text = replace_once(text, import_old, import_old + '\t"github.com/go-gl/mathgl/mgl32"\n', "gophertunnel import")

    field_old = "\tadditional chan packet.Packet\n}\n"
    field_new = (
        "\tadditional chan packet.Packet\n\n"
        "\t// Track login-period server packets so RequestChunkRadius is emitted only\n"
        "\t// after the same state bootstrap observed by the vanilla client.\n"
        "\treceivedPackets sync.Map\n}\n"
    )
    text = replace_once(text, field_old, field_new, "received packet field")

    handle_old = "func (conn *Conn) handle(pkData *packetData) error {\n\tfor _, id := range conn.expectedIDs.Load().([]uint32) {"
    handle_new = "func (conn *Conn) handle(pkData *packetData) error {\n\tconn.receivedPackets.Store(pkData.h.PacketID, true)\n\n\tfor _, id := range conn.expectedIDs.Load().([]uint32) {"
    text = replace_once(text, handle_old, handle_new, "login packet tracking")

    item_old = (
        "\t_ = conn.WritePacket(&packet.RequestChunkRadius{ChunkRadius: 16, MaxChunkRadius: 16})\n"
        "\tconn.expect(packet.IDChunkRadiusUpdated, packet.IDPlayStatus)\n"
        "\treturn nil\n}\n\n// handleRequestChunkRadius"
    )
    item_new = (
        "\tif conn.haveRequiredSpawnPacketsArrived() {\n"
        "\t\tconn.sendInitialChunkRadiusRequest()\n"
        "\t\treturn nil\n"
        "\t}\n\n"
        "\tgo func() {\n"
        "\t\tticker := time.NewTicker(50 * time.Millisecond)\n"
        "\t\tdefer ticker.Stop()\n"
        "\t\ttimer := time.NewTimer(5 * time.Second)\n"
        "\t\tdefer timer.Stop()\n\n"
        "\t\tfor {\n"
        "\t\t\tselect {\n"
        "\t\t\tcase <-ticker.C:\n"
        "\t\t\t\tif conn.haveRequiredSpawnPacketsArrived() {\n"
        "\t\t\t\t\tconn.sendInitialChunkRadiusRequest()\n"
        "\t\t\t\t\treturn\n"
        "\t\t\t\t}\n"
        "\t\t\tcase <-timer.C:\n"
        "\t\t\t\tconn.sendInitialChunkRadiusRequest()\n"
        "\t\t\t\treturn\n"
        "\t\t\tcase <-conn.ctx.Done():\n"
        "\t\t\t\treturn\n"
        "\t\t\t}\n"
        "\t\t}\n"
        "\t}()\n"
        "\treturn nil\n}\n\n"
        "func (conn *Conn) sendInitialChunkRadiusRequest() {\n"
        "\t_ = conn.WritePacket(&packet.RequestChunkRadius{ChunkRadius: 16, MaxChunkRadius: 16})\n"
        "\tconn.expect(packet.IDChunkRadiusUpdated, packet.IDPlayStatus)\n"
        "}\n\n"
        "func (conn *Conn) haveRequiredSpawnPacketsArrived() bool {\n"
        "\trequired := [...]uint32{\n"
        "\t\tpacket.IDAvailableActorIdentifiers,\n"
        "\t\tpacket.IDBiomeDefinitionList,\n"
        "\t\tpacket.IDUpdateAttributes,\n"
        "\t\tpacket.IDAvailableCommands,\n"
        "\t\tpacket.IDUpdateAbilities,\n"
        "\t\tpacket.IDSetActorData,\n"
        "\t\tpacket.IDInventoryContent,\n"
        "\t\tpacket.IDMobEquipment,\n"
        "\t\tpacket.IDPlayerList,\n"
        "\t}\n"
        "\tfor _, id := range required {\n"
        "\t\tif _, ok := conn.receivedPackets.Load(id); !ok {\n"
        "\t\t\treturn false\n"
        "\t\t}\n"
        "\t}\n"
        "\treturn true\n}\n\n"
        "// handleRequestChunkRadius"
    )
    text = replace_once(text, item_old, item_new, "RequestChunkRadius ordering")

    final_old = (
        "func (conn *Conn) tryFinaliseClientConn() {\n"
        "\tif conn.waitingForSpawn.Load() && conn.gameDataReceived.Load() {\n"
        "\t\tconn.waitingForSpawn.Store(false)\n"
        "\t\tconn.gameDataReceived.Store(false)\n\n"
        "\t\tclose(conn.spawn)\n"
        "\t\tconn.loggedIn = true\n"
        "\t\t_ = conn.WritePacket(&packet.SetLocalPlayerAsInitialised{EntityRuntimeID: conn.gameData.EntityRuntimeID})\n"
        "\t}\n}\n"
    )
    final_new = (
        "func (conn *Conn) tryFinaliseClientConn() {\n"
        "\tif conn.waitingForSpawn.Load() && conn.gameDataReceived.Load() {\n"
        "\t\tconn.waitingForSpawn.Store(false)\n"
        "\t\tconn.gameDataReceived.Store(false)\n\n"
        "\t\t_ = conn.WritePacket(&packet.Interact{ActionType: packet.InteractActionMouseOverEntity, TargetEntityRuntimeID: 0, Position: protocol.Option(mgl32.Vec3{})})\n"
        "\t\t_ = conn.WritePacket(&packet.PlayerAuthInput{Pitch: conn.gameData.Pitch, Yaw: conn.gameData.Yaw, Position: conn.gameData.PlayerPosition, HeadYaw: conn.gameData.Yaw, InputData: protocol.NewInputFlags(packet.InputFlagCount), InputMode: packet.InputModeTouch, PlayMode: packet.PlayModeNormal, InteractionModel: packet.InteractionModelTouch, InteractPitch: conn.gameData.Pitch, InteractYaw: conn.gameData.Yaw})\n\n"
        "\t\tclose(conn.spawn)\n"
        "\t\tconn.loggedIn = true\n"
        "\t\t_ = conn.WritePacket(&packet.SetLocalPlayerAsInitialised{EntityRuntimeID: conn.gameData.EntityRuntimeID})\n"
        "\t}\n}\n"
    )
    path.write_text(replace_once(text, final_old, final_new, "spawn finalisation"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
