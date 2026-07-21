import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llm_codex import CodexLLM


class Completed:
    returncode = 0
    stderr = ""
    stdout = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "thread-123"}),
        json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": "hello from codex"}}),
        json.dumps({"type": "turn.completed"}),
    ])


class CodexLLMTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "workspace").mkdir()
        (self.base / "hooks").mkdir()
        self.env = patch.dict(os.environ, {
            "OGMA_WORKDIR": str(self.base / "workspace"),
            "OGMA_MEMORY_DIR": str(self.base / "memory"),
            "CODEX_BIN": "/test/codex",
        }, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    @patch("llm_codex.subprocess.run", return_value=Completed())
    def test_new_turn_records_thread_and_returns_final_agent_message(self, run):
        llm = CodexLLM(self.base)
        reply = llm.run_turn("chat", "hello")
        self.assertEqual(reply, "hello from codex")
        self.assertEqual(llm.sessions["chat"], "thread-123")
        self.assertEqual(json.loads((self.base / "sessions.codex.json").read_text())["chat"],
                         "thread-123")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["/test/codex", "exec"])
        self.assertIn("--json", argv)

    @patch("llm_codex.subprocess.run", return_value=Completed())
    def test_existing_turn_uses_exec_resume(self, run):
        (self.base / "sessions.codex.json").write_text('{"chat": "old-thread"}')
        llm = CodexLLM(self.base)
        llm.run_turn("chat", "continue")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["/test/codex", "exec", "resume"])
        self.assertIn("old-thread", argv)

    def test_reset_does_not_touch_other_provider_sessions(self):
        (self.base / "sessions.json").write_text('{"chat": "claude-thread"}')
        (self.base / "sessions.codex.json").write_text('{"chat": "codex-thread"}')
        llm = CodexLLM(self.base)
        llm.reset("chat")
        self.assertEqual(json.loads((self.base / "sessions.json").read_text())["chat"],
                         "claude-thread")
        self.assertNotIn("chat", llm.sessions)


if __name__ == "__main__":
    unittest.main()
