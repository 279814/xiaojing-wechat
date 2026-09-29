import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app import wechat_cli_manager
from app.settings import AppSettings
from app.wechat_cli_manager import (
    WechatCliManager,
    redact_key_material,
    summarize_init_failure,
)
from app.wechat_cli_bundle import ensure_wechat_cli_import_path
from app.wechat_reader import WechatReader

ensure_wechat_cli_import_path()

FAKE_KEY = "ab" * 32
SCAN_LOG_ZERO = (
    "WeChat CLI 初始化\n"
    "[+] Weixin.exe PID=26124 (532MB)\n"
    "[+] Weixin.exe PID=18064 (158MB)\n"
    "  [91.3%] 0/22 salts matched, 82 hex patterns, 0.4s\n"
    "结果: 0/22 salts 找到密钥\n"
    "[!] 未提取到任何密钥\n"
    "[!] 密钥提取失败: 未能从任何微信进程中提取到密钥\n"
)


CONTACT_COLUMNS = (
    "id", "username", "local_type", "alias", "encrypt_username", "flag", "delete_flag",
    "verify_flag", "remark", "remark_quan_pin", "remark_pin_yin_initial", "nick_name",
    "pin_yin_initial", "quan_pin", "big_head_url", "small_head_url", "head_img_md5",
    "chat_room_notify", "is_in_chat_room", "description", "extra_buffer", "chat_room_type",
)


def _contact_db(path, rows):
    """A contact.db with the WeChat 4 ``contact`` schema, filled with ``rows``."""
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(path)) as conn:
        conn.execute(f"CREATE TABLE contact ({', '.join(CONTACT_COLUMNS)})")
        for row in rows:
            values = [row.get(col) for col in CONTACT_COLUMNS]
            conn.execute(f"INSERT INTO contact VALUES ({', '.join('?' * len(values))})", values)
        conn.commit()


class CustomerFilterTests(unittest.TestCase):
    def _reader(self, rows):
        reader = WechatReader(AppSettings())
        reader._run = lambda args: rows
        return reader

    def test_keeps_contact_whose_chat_list_name_has_keyword(self):
        # Rows as stored in account wxid_zpss75bi6rqv22's contact.db: the chat list
        # and chat title show 顾客-黑大帅, which is the remark over nickname "l".
        from wechat_cli.core.contacts import _load_contacts_from

        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "contact.db")
            _contact_db(db, [
                {"id": 1, "username": "wxid_zpss75bi6rqv22", "local_type": 1, "flag": 1, "nick_name": "ll"},
                {"id": 2, "username": "wxid_wx5k8smuv34r22", "local_type": 1, "alias": "f6ry7546849i",
                 "flag": 3, "remark": "顾客-黑大帅", "remark_quan_pin": "gukeheidashuai",
                 "remark_pin_yin_initial": "GKHDS", "nick_name": "l", "quan_pin": "l"},
                {"id": 3, "username": "wxid_byz54uedsozm22", "local_type": 3, "flag": 4,
                 "nick_name": "潇洒的黑大帅"},
                {"id": 4, "username": "wxid_renamed", "local_type": 1, "flag": 3,
                 "remark": "老王", "nick_name": "顾客A"},
                {"id": 5, "username": "filehelper", "local_type": 1, "nick_name": "文件传输助手"},
                {"id": 6, "username": "gh_fb5c364b7f6a", "local_type": 1, "nick_name": "顾客服务号"},
                {"id": 7, "username": "1234@chatroom", "local_type": 2, "remark": "顾客群"},
            ])
            _names, full = _load_contacts_from(db)

        customers = self._reader(full).refresh_customer_contacts()
        self.assertEqual([c.username for c in customers], ["wxid_wx5k8smuv34r22"])
        self.assertEqual(customers[0].display_name, "顾客-黑大帅")

    def test_matches_keyword_in_remark_or_nickname(self):
        rows = [
            {"username": "wxid_remark", "nick_name": "ll", "remark": "水木投资销售员-顾客"},
            {"username": "wxid_nick", "nick_name": "顾客-黑大帅", "remark": ""},
            {"username": "wxid_plain", "nick_name": "潇洒的黑大帅", "remark": ""},
            {"username": "123@chatroom", "nick_name": "顾客群", "remark": ""},
            {"username": "gh_abc", "nick_name": "顾客服务号", "remark": ""},
            {"username": "", "nick_name": "顾客", "remark": ""},
        ]
        customers = self._reader(rows).refresh_customer_contacts()
        self.assertEqual([c.username for c in customers], ["wxid_remark", "wxid_nick"])
        self.assertEqual(customers[1].display_name, "顾客-黑大帅")

    def test_nickname_hidden_by_remark_does_not_match(self):
        rows = [
            {"username": "wxid_a", "nick_name": "顾客A", "remark": "老王"},
            {"username": "wxid_b", "nick_name": "小李", "remark": "顾客-小李"},
        ]
        customers = self._reader(rows).refresh_customer_contacts()
        self.assertEqual([c.display_name for c in customers], ["顾客-小李"])


class InitFailureMessageTests(unittest.TestCase):
    def test_zero_keys_is_short_chinese_summary(self):
        msg = summarize_init_failure(SCAN_LOG_ZERO, 1)
        self.assertIn("未能从微信进程提取到密钥", msg)
        self.assertIn("0/22", msg)
        self.assertNotIn("\n", msg)
        self.assertLess(len(msg), 80)

    def test_wechat_not_running(self):
        msg = summarize_init_failure("[!] 密钥提取失败: Weixin.exe 未运行", 1)
        self.assertIn("微信未运行", msg)

    def test_generic_failure_mentions_exit_code(self):
        self.assertIn("exit_code=2", summarize_init_failure("boom", 2))

    def test_redacts_key_material(self):
        log = f"  [FOUND] salt={'cd' * 16}\n    enc_key={FAKE_KEY}\n  x'{FAKE_KEY}{'cd' * 16}'"
        redacted = redact_key_material(log)
        self.assertNotIn(FAKE_KEY, redacted)
        self.assertIn("enc_key=<redacted>", redacted)
        self.assertIn("salt=" + "cd" * 16, redacted)


class FakeRunner:
    """Stands in for click's CliRunner; simulates what `wechat-cli init` writes."""

    def __init__(self, keys_file, config_file, exit_code, output, new_keys=None):
        self.keys_file = keys_file
        self.config_file = config_file
        self.exit_code = exit_code
        self.output = output
        self.new_keys = new_keys

    def __call__(self):
        return self

    def invoke(self, _cli, _args):
        if self.new_keys is not None:
            self.keys_file.write_text(json.dumps(self.new_keys), encoding="utf-8")
        return SimpleNamespace(exit_code=self.exit_code, output=self.output)


def _keys(n):
    return {f"db\\{i}.db": {"enc_key": FAKE_KEY, "salt": f"{i:032x}"} for i in range(n)}


class InitializeKeepsWorkingKeysTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config_file = root / "config.json"
        self.keys_file = root / "all_keys.json"
        self.config_file.write_text(json.dumps({"db_dir": str(root / "db_storage")}), encoding="utf-8")
        self.good_keys = _keys(22)
        self.keys_file.write_text(json.dumps(self.good_keys), encoding="utf-8")
        self.manager = WechatCliManager(AppSettings(wechat_cli_config=str(self.config_file)))
        for patcher in (
            mock.patch("wechat_cli.core.config.auto_detect_db_dir", return_value=None),
            mock.patch.object(wechat_cli_manager, "active_db_dir", return_value=""),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def _run(self, runner):
        with mock.patch.object(wechat_cli_manager, "CliRunner", runner):
            return self.manager.initialize(force=True)

    def test_zero_key_rescan_keeps_previous_keys_and_reports_failure(self):
        # Simulate a scanner that left an empty key file behind.
        status = self._run(FakeRunner(self.keys_file, self.config_file, 1, SCAN_LOG_ZERO, new_keys={}))
        self.assertTrue(status.init_failed)
        self.assertTrue(status.initialized)
        self.assertEqual(json.loads(self.keys_file.read_text(encoding="utf-8")), self.good_keys)
        self.assertIn("未能从微信进程提取到密钥", status.message)
        self.assertIn("已保留上次可用的连接", status.message)
        self.assertIn("0/22 salts matched", status.log)

    def test_partial_rescan_does_not_replace_complete_keys(self):
        status = self._run(FakeRunner(self.keys_file, self.config_file, 0, "结果: 3/22 salts 找到密钥", new_keys=_keys(3)))
        self.assertTrue(status.init_failed)
        self.assertEqual(len(json.loads(self.keys_file.read_text(encoding="utf-8"))), 22)
        self.assertIn("3/22", status.message)

    def test_successful_rescan_is_reported(self):
        status = self._run(FakeRunner(self.keys_file, self.config_file, 0, "结果: 22/22 salts 找到密钥", new_keys=_keys(22)))
        self.assertFalse(status.init_failed)
        self.assertTrue(status.initialized)
        self.assertEqual(status.message, "微信连接初始化完成")

    def test_empty_keys_file_is_not_initialized(self):
        self.keys_file.write_text("{}", encoding="utf-8")
        self.assertFalse(self.manager.status().initialized)


def _page1(enc_key: bytes) -> bytes:
    """SQLCipher page 1 whose HMAC verifies for ``enc_key`` (content is random)."""
    import hashlib
    import hmac
    import struct

    body = os.urandom(4096 - 64)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, bytes(b ^ 0x3A for b in body[:16]), 2, dklen=32)
    mac = hmac.new(mac_key, body[16: 4096 - 80 + 16], hashlib.sha512)
    mac.update(struct.pack("<I", 1))
    return body + mac.digest()


class AccountSwitchTests(unittest.TestCase):
    """WeChat switched from the connected account to another one on the same PC."""

    RELS = ("contact\\contact.db", "session\\session.db")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        xwechat = root / "xwechat_files"
        self.old_key, self.new_key = os.urandom(32), os.urandom(32)
        self.old_dir = self._account(xwechat / "wxid_wx5k8smuv34r22_aefe" / "db_storage", self.old_key, 1000)
        self.new_dir = self._account(xwechat / "wxid_zpss75bi6rqv22_0f4c" / "db_storage", self.new_key, 2000)

        self.config_file = root / "config.json"
        self.keys_file = root / "all_keys.json"
        self.config_file.write_text(json.dumps({"db_dir": str(self.old_dir)}), encoding="utf-8")
        self.keys_file.write_text(
            json.dumps({rel: {"enc_key": self.old_key.hex(), "salt": "00"} for rel in self.RELS}),
            encoding="utf-8",
        )
        self.manager = WechatCliManager(AppSettings(wechat_cli_config=str(self.config_file)))
        patcher = mock.patch.object(wechat_cli_manager, "_wechat_data_roots", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def _account(self, db_dir: Path, key: bytes, mtime: int) -> Path:
        for rel in self.RELS:
            path = db_dir / rel.replace("\\", os.sep)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_page1(key) + b"\0" * 4096)
            os.utime(path, (mtime, mtime))
        return db_dir

    def _runner(self):
        runner = mock.MagicMock()
        runner.return_value.invoke.return_value = SimpleNamespace(exit_code=1, output=SCAN_LOG_ZERO)
        return runner

    def test_active_account_is_most_recently_written(self):
        self.assertEqual(Path(wechat_cli_manager.active_db_dir(str(self.old_dir))), self.new_dir)

    def test_status_reports_account_mismatch(self):
        status = self.manager.status()
        self.assertTrue(status.initialized)
        self.assertEqual(Path(status.active_db_dir), self.new_dir)
        self.assertIn("wxid_zpss75bi6rqv22", status.message)
        self.assertIn("wxid_wx5k8smuv34r22", status.message)

    def test_reset_scans_the_running_account_not_the_first_folder(self):
        runner = self._runner()
        with mock.patch.object(wechat_cli_manager, "CliRunner", runner):
            status = self.manager.initialize(force=True)
        args = runner.return_value.invoke.call_args.args[1]
        self.assertEqual(Path(args[args.index("--db-dir") + 1]), self.new_dir)
        self.assertTrue(status.init_failed)
        self.assertIn("仍在读取旧账号 wxid_wx5k8smuv34r22", status.message)

    def test_reset_skips_scan_when_saved_keys_still_decrypt(self):
        os.utime(self.new_dir / "session" / "session.db", (500, 500))
        runner = self._runner()
        with mock.patch.object(wechat_cli_manager, "CliRunner", runner):
            status = self.manager.initialize(force=True)
        runner.return_value.invoke.assert_not_called()
        self.assertFalse(status.init_failed)
        self.assertTrue(status.initialized)
        self.assertIn("仍然有效（2/2", status.message)
        self.assertNotIn(self.old_key.hex(), status.message + status.log)


class WindowLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_long_status_text_does_not_force_tall_sidebar(self):
        from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

        from app.main import _cap_label_height, short_status_text

        long_log = SCAN_LOG_ZERO * 200

        def sidebar_min_height(cap: bool) -> int:
            panel = QWidget()
            panel.setFixedWidth(300)
            layout = QVBoxLayout(panel)
            label = QLabel()
            if cap:
                _cap_label_height(label)
                label.setText(short_status_text(long_log))
            else:
                label.setWordWrap(True)
                label.setText(long_log)
            layout.addWidget(label)
            return layout.totalMinimumSize().height()

        self.assertGreater(sidebar_min_height(cap=False), 2000)
        self.assertLess(sidebar_min_height(cap=True), 120)

    def test_short_status_text_takes_first_line_and_truncates(self):
        from app.main import short_status_text

        self.assertEqual(short_status_text("\n  第一行  \n第二行"), "第一行")
        self.assertLessEqual(len(short_status_text("字" * 500)), 120)

    def test_fit_size_to_screen_clamps_height(self):
        from PySide6.QtCore import QRect

        from app.main import fit_size_to_screen

        self.assertEqual(fit_size_to_screen(980, 680, QRect(0, 0, 1280, 672)), (980, 592))
        self.assertEqual(fit_size_to_screen(980, 680, QRect(0, 0, 2560, 1400)), (980, 680))
        self.assertEqual(fit_size_to_screen(980, 680, None), (980, 680))


if __name__ == "__main__":
    unittest.main()
