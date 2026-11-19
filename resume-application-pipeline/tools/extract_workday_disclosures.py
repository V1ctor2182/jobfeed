"""Copy Workday disclosure rules verbatim from an installed Simplify release."""
import argparse
import hashlib
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('extension_directory', type=Path)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
root = args.extension_directory
raw = (root / 'remoteConfig.json').read_bytes()
config = json.loads(raw)
manifest = json.loads((root / 'manifest.json').read_text())
keys = {'gender', 'ethnicity', 'hispanic', 'veteran_v2', 'disability_v2'}
rows = [row for row in config['ATS']['Workday']['inputSelectors'] if row[0] in keys]
assert {row[0] for row in rows} == keys
output = {'source': {'extensionId': root.parent.name, 'extensionVersion': manifest['version'],
          'remoteConfigSha256': hashlib.sha256(raw).hexdigest(),
          'pageScriptSha256': hashlib.sha256((root / 'pageScript.js').read_bytes()).hexdigest()},
          'inputSelectors': rows}
args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
print(f'Copied {len(rows)} fields, {sum(len(row[1]) for row in rows)} variants')
