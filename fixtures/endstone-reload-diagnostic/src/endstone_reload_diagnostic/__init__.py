import faulthandler
import sys
from endstone.plugin import Plugin

class ReloadDiagnostic(Plugin):
    api_version = "0.11"
    def on_enable(self):
        if sys.platform == "win32":
            faulthandler.enable()
            faulthandler.dump_traceback_later(20, repeat=True)
