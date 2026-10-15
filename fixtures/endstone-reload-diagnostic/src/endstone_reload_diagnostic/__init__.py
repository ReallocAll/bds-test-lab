import faulthandler
import os
from pathlib import Path
import sys
from endstone.plugin import Plugin

class ReloadDiagnostic(Plugin):
    api_version = "0.11"
    def on_enable(self):
        if sys.platform == "win32":
            trace = os.environ.get("ENDKEEP_INSTALLER_TRACE")
            if trace:
                path = str(Path(trace).parent / "fixtures" / "pip-trace")
                os.environ["PYTHONPATH"] = path + os.pathsep + os.environ.get("PYTHONPATH", "")
            faulthandler.enable()
            faulthandler.dump_traceback_later(20, repeat=True)
