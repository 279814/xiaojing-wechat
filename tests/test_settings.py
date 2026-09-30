import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import agent_client, settings
from app.settings import AppSettings, backend_api_base_for_host, backend_host, backend_host_options

JUJING = "https://www.jujingbuluo123.com"
LOCAL = "http://127.0.0.1:3001"


class BackendHostTests(unittest.TestCase):
    def test_host_strips_one_trailing_api(self):
        self.assertEqual(backend_host(f"{JUJING}/api"), JUJING)
        self.assertEqual(backend_host(f"{LOCAL}/api/"), LOCAL)
        self.assertEqual(backend_host(LOCAL), LOCAL)
        self.assertEqual(backend_host("http://x/api/api"), "http://x/api")

    def test_api_base_appends_api(self):
        self.assertEqual(backend_api_base_for_host(JUJING), f"{JUJING}/api")
        self.assertEqual(backend_api_base_for_host(f"{LOCAL}/"), f"{LOCAL}/api")

    def test_saved_values_select_matching_host(self):
        for saved, index in ((f"{JUJING}/api", 0), (f"{LOCAL}/api", 1), (LOCAL, 1)):
            with self.subTest(saved=saved):
                options, selected = backend_host_options(saved)
                self.assertEqual(options, [(JUJING, f"{JUJING}/api"), (LOCAL, f"{LOCAL}/api")])
                self.assertEqual(selected, index)

    def test_empty_saved_value_defaults_to_local(self):
        options, selected = backend_host_options("")
        self.assertEqual(options[selected][0], LOCAL)

    def test_unknown_host_is_kept_with_its_exact_api_base(self):
        options, selected = backend_host_options("http://10.0.0.5:8080/custom")
        self.assertEqual([label for label, _ in options], [JUJING, LOCAL, "http://10.0.0.5:8080/custom"])
        self.assertEqual(options[selected], ("http://10.0.0.5:8080/custom", "http://10.0.0.5:8080/custom"))

    def test_default_settings_use_local_api(self):
        self.assertEqual(AppSettings().backend_api_base, f"{LOCAL}/api")
        self.assertEqual(AppSettings().backend_chat_url, f"{LOCAL}/api/autosale/chat")


class AgentFallbackTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(settings.AGENT_CHAT_URL_ENV, None)

    def test_no_fallback_without_env(self):
        s = AppSettings()
        self.assertEqual(s.agent_chat_url, "")
        self.assertFalse(agent_client.agent_fallback_allowed(s))
        with mock.patch.object(agent_client.requests, "post") as post:
            claim = agent_client.AgentClient(s).claim_message_send("m1")
            agent_client.AgentClient(s).mark_message_sent("m1", ok=True)
        post.assert_not_called()
        self.assertTrue(claim.ok)

    def test_legacy_saved_agent_url_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(
                json.dumps({"agent_chat_url": "http://39.107.230.22:3000/api/chat",
                            "allow_production_agent_fallback": True}),
                encoding="utf-8",
            )
            with mock.patch.object(settings, "settings_path", return_value=path):
                s = AppSettings.load()
                s.save()
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(s.agent_chat_url, "")
        self.assertFalse(agent_client.agent_fallback_allowed(s))
        self.assertNotIn("agent_chat_url", saved)
        self.assertNotIn("allow_production_agent_fallback", saved)

    def test_env_enables_local_dev_fallback(self):
        os.environ[settings.AGENT_CHAT_URL_ENV] = "http://127.0.0.1:9010/api/chat"
        s = AppSettings()
        self.assertTrue(agent_client.agent_fallback_allowed(s))


if __name__ == "__main__":
    unittest.main()
