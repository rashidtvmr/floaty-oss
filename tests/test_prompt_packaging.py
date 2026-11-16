"""The audit copy in the Linux release must match its compiled system prompt."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PromptPackagingTests(unittest.TestCase):
    def test_release_copies_the_prompt_embedded_by_the_core(self):
        core = (ROOT / "crates/socket_stream_core/src/ai.rs").read_text()
        name = re.search(r'include_str!\("\.\./prompts/([^"/]+)"\)', core).group(1)
        self.assertTrue((ROOT / "crates/socket_stream_core/prompts" / name).is_file())
        script = (ROOT / "scripts/build-linux-release.sh").read_text()
        self.assertIn(f'cp "$ROOT/crates/socket_stream_core/prompts/{name}" "$PKG/AI-SYSTEM-PROMPT.md"', script)
        self.assertIn('test_provider_profiles_e2e.py" "$PKG/tests/acceptance/test_provider_profiles_e2e.py"', script)
