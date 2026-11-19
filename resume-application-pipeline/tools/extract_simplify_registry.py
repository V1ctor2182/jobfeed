#!/usr/bin/env python3
"""Generate an ATS registry from a local Simplify remoteConfig.json; no network."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    parser.add_argument('--version', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    raw = args.config.read_bytes()
    data = json.loads(raw)
    ats = {name: obj
           for name, obj in data['ATS'].items()
           if isinstance(obj, dict) and ('urls' in obj or 'inputSelectors' in obj)}
    result = {
        'source': {'extensionId': 'pbanhockgagggenencehbnadejlgchfc',
                   'extensionVersion': args.version,
                   'remoteConfigSha256': hashlib.sha256(raw).hexdigest()},
        'ats': ats,
        'boards': data['Boards'],
        'render': data.get('render', {}),
        'atsConstants': {name: obj for name, obj in data['ATS'].items()
                         if name not in ats},
        'valueMaps': {key: value for key, value in data.items()
                      if isinstance(value, dict) and ('To' in key or key == 'valueMaps')},
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, separators=(',', ':')) + '\n')
    print(f'{len(ats)} ATS configurations; {len(result["boards"])} board descriptors; no live-site verification implied')


if __name__ == '__main__':
    main()
