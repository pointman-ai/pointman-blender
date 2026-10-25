"""The extension's process as a node runs it, through the SDK's stand-in node, with no Blender: it speaks the typed
protocol, says Blender isn't installed, and its commands ask to be tried again later. Skipped where
$POINTMAN_EXTENSIONS_SDK doesn't name the node's Python SDK."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SDK = os.environ.get("POINTMAN_EXTENSIONS_SDK")
if SDK:
    sys.path.insert(0, SDK)
try:
    import pointman_extension_types as types
    from pointman_extension_testing import ExtensionFailed, StandInNode
except ImportError:
    StandInNode = None


@unittest.skipUnless(StandInNode, "needs the node's Python SDK ($POINTMAN_EXTENSIONS_SDK)")
class NoBlenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.node = StandInNode(ROOT, settings={"blender_path": str(Path(self.tmp.name) / "no-blender")},
                                state_dir=self.tmp.name, roots=[self.tmp.name])
        self.node.start()

    def tearDown(self):
        self.node.stop()
        self.tmp.cleanup()

    def test_it_speaks_the_typed_protocol(self):
        self.assertEqual(self.node.info.protocol, types.Protocol.PROTOCOL_TYPED)
        self.assertEqual(self.node.info.id, "blender")

    def test_its_status_says_blender_isnt_installed(self):
        for _ in range(100):
            if "blender.status" in self.node.events:
                break
            import time
            time.sleep(0.05)
        self.assertEqual(self.node.events["blender.status"],
                         {"installed": False, "reason": "Blender isn't installed here"})

    def test_a_render_is_worth_asking_again_once_blender_is_there(self):
        blend = Path(self.tmp.name) / "scene.blend"
        blend.write_bytes(b"BLENDER")
        with self.assertRaises(ExtensionFailed) as failed:
            self.node.run("blender.render", {"file": str(blend)})
        self.assertTrue(failed.exception.retry)
        self.assertIn("Blender isn't installed", str(failed.exception))


if __name__ == "__main__":
    unittest.main()
