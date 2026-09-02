from pathlib import Path

path = Path("controller/combined_pack_gamerule_fleet_exact_runner.py")
text = path.read_text(encoding="utf-8")
old = '    if self.platform != "windows":\n'
new = '    if getattr(self, "platform", None) != "windows":\n'
if old not in text:
    raise SystemExit("public phase platform guard anchor not found")
text = text.replace(old, new, 1)
path.write_text(text, encoding="utf-8")
