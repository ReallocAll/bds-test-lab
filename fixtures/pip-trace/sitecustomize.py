import faulthandler
import os
import sys
from pathlib import Path

if os.environ.get("ENDKEEP_INSTALLER_TRACE"):
    folder = Path(os.environ["ENDKEEP_INSTALLER_TRACE"])
    folder.mkdir(parents=True, exist_ok=True)
    _trace_file = (folder / f"python-{os.getpid()}.log").open("a", buffering=1)
    print({"pid": os.getpid(), "argv": sys.argv, "executable": sys.executable,
           "prefix": sys.prefix, "base_prefix": sys.base_prefix}, file=_trace_file, flush=True)
    faulthandler.enable(file=_trace_file)
    faulthandler.dump_traceback_later(10, repeat=True, file=_trace_file)
