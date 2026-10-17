"""The publication manifest must cover, and match, the deployment sources."""
import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SourceManifestTests(unittest.TestCase):
    def test_all_published_deployment_files_have_current_hashes(self):
        manifest = ROOT / 'SOURCE_MANIFEST.json'
        hashes = json.loads(manifest.read_text())['published_file_sha256']
        files = {str(path.relative_to(ROOT)) for path in ROOT.rglob('*')
                 if path.is_file() and path != manifest and '__pycache__' not in path.parts
                 and path.suffix != '.pyc'}
        self.assertEqual(set(hashes), files)
        for relative, digest in hashes.items():
            with self.subTest(path=relative):
                self.assertEqual(hashlib.sha256((ROOT / relative).read_bytes()).hexdigest(), digest)
