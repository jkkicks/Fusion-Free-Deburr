"""Tool catalog: document tools (API), local libraries (read straight from
their JSON files, which is far faster than the API), and cloud libraries (API
query, slow, only on request). All tool types are loaded; commands filter to
the types they support.

Entries are plain dicts so they can be sent to the tool picker HTML as-is.
Fusion Tool objects are only created for the tool the user actually picks.
"""

import math
import glob
import json
import os
import sys
import time

import adsk.core
import adsk.cam

BALL_END_MILL = 'ball end mill'
LOLLIPOP_MILL = 'lollipop mill'
CHAMFER_MILL = 'chamfer mill'
THREAD_MILL = 'thread mill'
# Tool types read from their full JSON profile, with every move clearance-checked.
PROFILE_TYPES = (LOLLIPOP_MILL, CHAMFER_MILL, THREAD_MILL)
_UNIT_TO_CM = {'inches': 2.54, 'millimeters': 0.1}


def _param_value(tool: adsk.cam.Tool, name: str):
    param = tool.parameters.itemByName(name)
    return param.value.value if param else None


def tool_type(tool: adsk.cam.Tool) -> str:
    return _param_value(tool, 'tool_type') or ''


def diameter_cm(tool: adsk.cam.Tool) -> float:
    """Tool diameter in internal units (cm)."""
    return _param_value(tool, 'tool_diameter')


def tool_from_json(text):
    """Import a tool from JSON, undoing Fusion's thread-mill neck rewrite.

    On import Fusion derives a thread mill's shoulder (neck) diameter from its
    diameter and thread pitch. A pitch that differs from the cutter width, such
    as one normalized to 1 in, yields a wrong or even negative neck, so Fusion
    draws and simulates a different tool. Restore the neck the JSON specified.
    """
    tool = adsk.cam.Tool.createFromJson(text)
    raw = json.loads(text)
    want = raw.get('geometry', {}).get('shoulder-diameter')
    unit = {'inches': 'in', 'millimeters': 'mm'}.get(raw.get('unit'))
    if (raw.get('type') == THREAD_MILL and unit and isinstance(want, (int, float))
            and math.isfinite(want) and want > 0):
        got = json.loads(tool.toJson()).get('geometry', {}).get('shoulder-diameter')
        if not isinstance(got, (int, float)) or abs(got - want) > 1e-9:
            tool.parameters.itemByName('tool_shoulderDiameter').expression = f'{want} {unit}'
    return tool


def validate_ball_tool(tool):
    diameter = diameter_cm(tool) if tool else None
    if (not tool or tool_type(tool) != BALL_END_MILL or
            not isinstance(diameter, (int, float)) or not math.isfinite(diameter) or diameter <= 0):
        raise ValueError('Select a ball endmill with a positive finite diameter.')
    return diameter / 2


def diameter_expression(tool: adsk.cam.Tool) -> str:
    param = tool.parameters.itemByName('tool_diameter')
    return param.expression if param else '?'


def local_library_dir():
    if sys.platform == 'darwin':
        base = os.path.expanduser('~/Library/Application Support')
    else:
        base = os.environ.get('APPDATA', '')
    return os.path.join(base, 'Autodesk', 'CAM360', 'libraries', 'Local')


class Catalog:
    """Tools from all sources, keyed by a string id."""

    def __init__(self, log=print):
        self._log = log
        self.entries = []          # dicts sent to the picker
        self._tools = {}           # id -> adsk.cam.Tool (document/cloud)
        self._raw = {}             # id -> tool JSON dict (local libraries)
        self.cloud_loaded = False

    def _add(self, id_, source, library, type_, description, number=None, diameter=None,
             flute=None, flutes=None, oal=None, vendor='', product_id=''):
        entry = {
            'id': id_, 'source': source, 'library': library,
            'type': type_, 'description': description, 'number': number,
            'diameterCm': diameter, 'fluteCm': flute, 'flutes': flutes, 'oalCm': oal,
            'vendor': vendor, 'productId': product_id,
        }
        self.entries.append(entry)
        return entry

    def _add_tool(self, id_, source, library, tool: adsk.cam.Tool):
        self._tools[id_] = tool
        self._add(id_, source, library, tool_type(tool), tool.description,
                  number=_param_value(tool, 'tool_number'),
                  diameter=diameter_cm(tool),
                  flute=_param_value(tool, 'tool_fluteLength'),
                  flutes=_param_value(tool, 'tool_numberOfFlutes'),
                  oal=_param_value(tool, 'tool_overallLength'))

    def load_document(self, cam: adsk.cam.CAM):
        start = time.perf_counter()
        for i, tool in enumerate(cam.documentToolLibrary):
            self._add_tool(f'doc:{i}', 'document', 'Document', tool)
        self._log(f'Document tools loaded in {time.perf_counter() - start:.2f}s')

    def load_local(self):
        start = time.perf_counter()
        folder = local_library_dir()
        for path in sorted(glob.glob(os.path.join(folder, '*.json'))):
            library = os.path.splitext(os.path.basename(path))[0]
            try:
                with open(path, encoding='utf-8') as f:
                    tools = json.load(f).get('data', [])
            except (OSError, ValueError) as e:
                self._log(f'Could not read tool library {path}: {e}')
                continue
            for i, raw in enumerate(tools):
                scale = _UNIT_TO_CM.get(raw.get('unit'))
                g = raw.get('geometry', {})
                if scale is None or 'DC' not in g or raw.get('type') in ('holder', 'probe'):
                    continue
                id_ = f'local:{library}:{i}'
                self._raw[id_] = raw
                self._add(id_, 'local', library, raw.get('type', ''), raw.get('description', ''),
                          number=raw.get('post-process', {}).get('number'),
                          diameter=g['DC'] * scale,
                          flute=g['LCF'] * scale if 'LCF' in g else None,
                          flutes=g.get('NOF'),
                          oal=g['OAL'] * scale if 'OAL' in g else None,
                          vendor=raw.get('vendor', ''), product_id=raw.get('product-id', ''))
        self._log(f'Local libraries loaded from {folder} in {time.perf_counter() - start:.2f}s')

    def load_cloud(self):
        """Slow: queries cloud libraries through the API."""
        start = time.perf_counter()
        libraries = adsk.cam.CAMManager.get().libraryManager.toolLibraries
        query = libraries.createQuery(adsk.cam.LibraryLocations.CloudLibraryLocation)
        for i, result in enumerate(query.execute()):
            library = result.toolLibraryURL.toString().rstrip('/').split('/')[-1]
            self._add_tool(f'cloud:{i}', 'cloud', library, result.tool)
        self.cloud_loaded = True
        self._log(f'Cloud libraries loaded in {time.perf_counter() - start:.2f}s')

    def entry(self, id_):
        return next((e for e in self.entries if e['id'] == id_), None)

    def tool(self, id_) -> adsk.cam.Tool:
        """The Fusion Tool for an entry, created from JSON on first use for local tools."""
        if id_ not in self._tools and id_ in self._raw:
            self._tools[id_] = tool_from_json(json.dumps(self._raw[id_]))
        return self._tools.get(id_)
