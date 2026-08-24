import json
import pathlib


def save_metadata(data):
    pathlib.Path('artifact-metadata.json').write_text(json.dumps(data, indent=2))


def resolve_artifacts():
    raise NotImplementedError('GitHub Actions artifact resolver requires API token in workflow environment')
