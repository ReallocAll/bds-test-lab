from endstone.command import Command, CommandSender
from endstone.plugin import Plugin


class CiLifecycleControl(Plugin):
    """CI-only lifecycle command with no scheduled or event-driven work."""

    api_version = "0.11"
    commands = {  # noqa: RUF012 - Endstone discovers command metadata from the plugin class.
        "cishutdown": {
            "description": "Gracefully shut down Endstone for CI lifecycle validation",
            "usages": ["/cishutdown"],
        }
    }

    def on_enable(self) -> None:
        self.logger.info("CI lifecycle control enabled")

    def on_command(self, sender: CommandSender, command: Command, args: list[str]) -> bool:
        del sender, args
        if command.name != "cishutdown":
            return False
        self.logger.info("CI lifecycle shutdown requested")
        self.server.shutdown()
        return True
