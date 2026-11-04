"""Bundle only hash-verified, explicitly synthetic packaged samples for local demo buttons."""
from pathlib import Path
import base64
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
TITLES = {
    'synthetic_ppg_active_tabs': 'PPG: known values agree',
    'synthetic_ppg_source_mismatch': 'PPG: find one source mismatch',
    'synthetic_pratt_template_union': 'Pratt: audit-template checks',
    'synthetic_plain_numeric_issues': 'CSV: find count issues',
}

def build():
    folder = ROOT / 'samples/development_examples'
    index = json.loads((folder / 'index.json').read_text())
    if index.get('synthetic') is not True:
        raise ValueError('Only explicitly synthetic examples can be bundled')
    output = []
    def read(entry):
        path = folder / entry['path']
        if path.parent != folder or not path.is_file():
            raise ValueError('Sample path must be a file inside development_examples')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != entry['sha256'] or len(data) != entry['size_bytes']:
            raise ValueError('Sample hash/size changed: ' + path.name)
        return data
    for entry in index['examples']:
        if entry.get('synthetic') is not True or entry['id'] not in TITLES:
            raise ValueError('Unexpected example')
        output.append(dict(id=entry['id'], title=TITLES[entry['id']], description=entry['purpose'],
                           filename=entry['artifact']['path'], content_base64=base64.b64encode(read(entry['artifact'])).decode(),
                           source=json.loads(read(entry['source'])) if entry['source'] else None))
    content = "'use strict';\n// Generated only from packaged synthetic fixtures; no client data.\n" + 'globalThis.CI_DEVELOPMENT_EXAMPLES = ' + json.dumps(output, separators=(',', ':')) + ';\n'
    (ROOT / 'web/development-examples.js').write_text(content)
    return len(output)

if __name__ == '__main__':
    print('Bundled', build(), 'synthetic examples')
