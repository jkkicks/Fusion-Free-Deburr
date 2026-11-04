"""Build the downloadable Fusion add-in archive using only the standard library."""

import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def main():
    """Package the add-in and required resources, excluding development caches."""
    root = Path(__file__).resolve().parents[1]
    addin = root / 'FusionFreeDeburr'
    manifest = json.loads((addin / 'FusionFreeDeburr.manifest').read_text(encoding='utf-8'))
    version = manifest['version']
    output = root / 'dist' / f'Fusion-Free-Deburr-v{version}.zip'
    output.parent.mkdir(exist_ok=True)
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        for path in sorted(addin.rglob('*')):
            relative = path.relative_to(addin)
            if any(part.startswith('.') or part == '__pycache__' for part in relative.parts):
                continue
            if path.is_file() and path.suffix not in ('.pyc', '.pyo'):
                write_file(archive, path, path.relative_to(root).as_posix())
        for name in ('README.md', 'LICENSE'):
            write_file(archive, root / name, name)
    print(output)


def write_file(archive, path, name):
    """Keep retry builds identical regardless of checkout timestamps/platform."""
    from zipfile import ZipInfo
    info = ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    archive.writestr(info, path.read_bytes())


if __name__ == '__main__':
    main()
