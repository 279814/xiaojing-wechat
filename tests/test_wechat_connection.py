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

REAL_OPEN_BY_WECHAT = wechat_cli_manager._open_by_wechat
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
            return self.manager.reset_connection()

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
        self.assertFalse(status.account_switched)
        self.assertIn("已连接当前微信账号", status.message)
        self.assertIn("提取到 22 个数据库密钥", status.message)

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
        self.xwechat = xwechat = root / "xwechat_files"
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
        self.open_dirs: list[Path] = []
        for patcher in (
            mock.patch.object(wechat_cli_manager, "_wechat_data_roots", return_value=[]),
            mock.patch.object(
                wechat_cli_manager,
                "_open_by_wechat",
                side_effect=lambda d: any(Path(d) == o for o in self.open_dirs) if self.open_dirs else None,
            ),
        ):
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

    def _successful_runner(self):
        """`wechat-cli init --force --db-dir <new>` that found the new account's keys."""
        runner = mock.MagicMock()

        def invoke(_cli, args):
            target = args[args.index("--db-dir") + 1]
            self.keys_file.write_text(
                json.dumps({rel: {"enc_key": self.new_key.hex(), "salt": "11"} for rel in self.RELS}),
                encoding="utf-8",
            )
            self.config_file.write_text(json.dumps({"db_dir": target}), encoding="utf-8")
            return SimpleNamespace(exit_code=0, output="结果: 2/2 salts 找到密钥")

        runner.return_value.invoke.side_effect = invoke
        return runner

    def _invoked_args(self, runner):
        return runner.return_value.invoke.call_args.args[1]

    def _configured_db_dir(self) -> Path:
        return Path(json.loads(self.config_file.read_text(encoding="utf-8"))["db_dir"])

    def _make_new_session_older(self):
        """WeChat just switched: the old account's session.db was written at logout."""
        os.utime(self.new_dir / "session" / "session.db", (500, 500))
        os.utime(self.old_dir / "session" / "session.db", (3000, 3000))

    def test_active_account_is_most_recently_written(self):
        self.assertEqual(Path(wechat_cli_manager.active_db_dir(str(self.old_dir))), self.new_dir)

    def test_active_account_is_the_one_wechat_holds_open(self):
        self._make_new_session_older()
        self.open_dirs = [self.new_dir]
        self.assertEqual(Path(wechat_cli_manager.active_db_dir(str(self.old_dir))), self.new_dir)

    def test_login_shm_marks_active_account_when_open_files_unknown(self):
        self._make_new_session_older()
        shm = self.new_dir / "session" / "session.db-shm"
        shm.write_bytes(b"\0")
        os.utime(shm, (4000, 4000))
        self.assertEqual(Path(wechat_cli_manager.active_db_dir(str(self.old_dir))), self.new_dir)

    def test_restart_manager_reports_open_file_without_touching_it(self):
        if os.name != "nt":
            self.skipTest("Windows only")
        path = self.new_dir / "contact" / "contact.db"
        self.assertFalse(wechat_cli_manager._file_open_by_other_process(str(path)))
        self.assertIsNone(wechat_cli_manager._file_open_by_other_process(str(path) + ".missing"))

    def test_status_reports_account_mismatch(self):
        status = self.manager.status()
        self.assertTrue(status.initialized)
        self.assertEqual(Path(status.active_db_dir), self.new_dir)
        self.assertIn("wxid_zpss75bi6rqv22", status.message)
        self.assertIn("wxid_wx5k8smuv34r22", status.message)

    def test_reset_scans_the_running_account_not_the_first_folder(self):
        runner = self._runner()
        with mock.patch.object(wechat_cli_manager, "CliRunner", runner):
            status = self.manager.reset_connection()
        args = runner.return_value.invoke.call_args.args[1]
        self.assertEqual(Path(args[args.index("--db-dir") + 1]), self.new_dir)
        self.assertTrue(status.init_failed)
        self.assertIn("仍在读取旧账号 wxid_wx5k8smuv34r22", status.message)

    def test_reset_skips_scan_when_saved_keys_still_decrypt(self):
        os.utime(self.new_dir / "session" / "session.db", (500, 500))
        runner = self._runner()
        with mock.patch.object(wechat_cli_manager, "CliRunner", runner):
            status = self.manager.reset_connection()
        runner.return_value.invoke.assert_not_called()
        self.assertFalse(status.init_failed)
        self.assertFalse(status.account_switched)
        self.assertTrue(status.initialized)
        self.assertIn("当前微信账号 wxid_wx5k8smuv34r22 已连接（2/2", status.message)
        self.assertNotIn(self.old_key.hex(), status.message + status.log)

    def test_switched_account_is_not_reported_valid_with_old_keys(self):
        runner = self._runner()
        with mock.patch.object(wechat_cli_manager, "CliRunner", runner):
            status = self.manager.reset_connection()
        args = self._invoked_args(runner)
        self.assertIn("--force", args)
        self.assertEqual(Path(args[args.index("--db-dir") + 1]), self.new_dir)
        self.assertNotIn("有效", status.message)
        self.assertNotIn("已连接", status.message)
        self.assertTrue(status.init_failed)
        self.assertFalse(status.account_switched)
        self.assertTrue(status.message.startswith("顾客列表没有切换"))
        self.assertIn("微信当前登录的是 wxid_zpss75bi6rqv22", status.message)
        self.assertIn("没有拿到该账号的密钥", status.message)
        self.assertIn("wxid_wx5k8smuv34r22", status.message)
        self.assertEqual(Path(status.active_db_dir), self.new_dir)

    def test_first_connection_scans_the_running_account(self):
        self.config_file.unlink()
        self.keys_file.unlink()
        roots = mock.patch.object(wechat_cli_manager, "_wechat_data_roots", return_value=[str(self.xwechat)])
        roots.start()
        self.addCleanup(roots.stop)
        status = self.manager.status()
        self.assertFalse(status.initialized)
        self.assertIn("wxid_zpss75bi6rqv22", status.message)
        self.assertIn("重置连接", status.message)
        runner = self._successful_runner()
        with mock.patch.object(wechat_cli_manager, "CliRunner", runner):
            status = self.manager.reset_connection()
        args = self._invoked_args(runner)
        self.assertEqual(Path(args[args.index("--db-dir") + 1]), self.new_dir)
        self.assertFalse(status.init_failed)
        self.assertFalse(status.account_switched)
        self.assertIn("已连接当前微信账号 wxid_zpss75bi6rqv22", status.message)

    def test_open_check_uses_any_main_database(self):
        answers = {"session.db": False, "contact.db": True, "message_0.db": None}
        with mock.patch.object(
            wechat_cli_manager,
            "_file_open_by_other_process",
            side_effect=lambda p: answers[os.path.basename(p)],
        ):
            self.assertTrue(REAL_OPEN_BY_WECHAT(str(self.new_dir)))
            answers["contact.db"] = None
            self.assertFalse(REAL_OPEN_BY_WECHAT(str(self.new_dir)))
            answers["session.db"] = None
            self.assertIsNone(REAL_OPEN_BY_WECHAT(str(self.new_dir)))

    def test_failed_switch_keeps_old_account_keys_and_backup(self):
        old_keys = self.keys_file.read_bytes()
        with mock.patch.object(wechat_cli_manager, "CliRunner", self._runner()):
            status = self.manager.reset_connection()
        self.assertTrue(status.initialized)
        self.assertEqual(self.keys_file.read_bytes(), old_keys)
        self.assertEqual(self._configured_db_dir(), self.old_dir)
        backup = self.keys_file.with_name("all_keys.wxid_wx5k8smuv34r22.bak.json")
        self.assertEqual(backup.read_bytes(), old_keys)

    def test_successful_switch_retargets_new_account(self):
        runner = self._successful_runner()
        with mock.patch.object(wechat_cli_manager, "CliRunner", runner):
            status = self.manager.reset_connection()
        self.assertFalse(status.init_failed)
        self.assertTrue(status.account_switched)
        self.assertEqual(self._configured_db_dir(), self.new_dir)
        self.assertEqual(Path(status.db_dir), self.new_dir)
        self.assertEqual(status.active_db_dir, "")
        self.assertIn("已切换到当前微信账号 wxid_zpss75bi6rqv22", status.message)
        self.assertEqual(wechat_cli_manager.verify_saved_keys(self.keys_file, str(self.new_dir)), (2, 2))
        self.assertNotIn(self.new_key.hex(), status.message + status.log)
        # Once switched, the next reset validates the new account and skips the scan.
        again = self._runner()
        with mock.patch.object(wechat_cli_manager, "CliRunner", again):
            status = self.manager.reset_connection()
        again.return_value.invoke.assert_not_called()
        self.assertIn("当前微信账号 wxid_zpss75bi6rqv22 已连接", status.message)

    def test_successful_switch_drops_cached_contacts_of_old_account(self):
        from wechat_cli.core import contacts

        contacts._contact_names = {"wxid_old": "顾客-旧账号"}
        contacts._contact_full = [{"username": "wxid_old", "nick_name": "", "remark": "顾客-旧账号"}]
        contacts._self_username = "wxid_wx5k8smuv34r22"
        with mock.patch.object(wechat_cli_manager, "CliRunner", self._successful_runner()):
            self.manager.reset_connection()
        self.assertIsNone(contacts._contact_names)
        self.assertIsNone(contacts._contact_full)
        self.assertIsNone(contacts._self_username)


class ReaderReloadsContactsTests(unittest.TestCase):
    def test_refresh_reloads_contacts_instead_of_process_cache(self):
        from wechat_cli.core import contacts

        contacts._contact_full = [{"username": "wxid_old", "nick_name": "", "remark": "顾客-旧账号"}]
        seen = []

        def fake_run(_args):
            seen.append(contacts._contact_full)
            return [{"username": "wxid_new", "nick_name": "l", "remark": "顾客-黑大帅"}]

        reader = WechatReader(AppSettings())
        reader._run = fake_run
        customers = reader.refresh_customer_contacts()
        self.assertEqual(seen, [None])
        self.assertEqual([c.display_name for c in customers], ["顾客-黑大帅"])


class MainPageAccountSwitchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from app import main
        from app.state_store import StateStore, StoredUser

        self.workers = []
        test = self

        class FakeRefreshWorker:
            def __init__(self, _reader):
                self.callback = None
                test.workers.append(self)

            @property
            def finished_result(self):
                return SimpleNamespace(connect=lambda cb, *_a: setattr(self, "callback", cb))

            def start(self):
                pass

            def isRunning(self):
                return False

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.message_boxes = mock.MagicMock()
        for patcher in (
            mock.patch.object(main.WechatStatusWorker, "start"),
            mock.patch.object(main.WechatResetWorker, "start"),
            mock.patch.object(main, "CustomerRefreshWorker", FakeRefreshWorker),
            mock.patch.object(main, "QMessageBox", self.message_boxes),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.page = main.MainPage(
            AppSettings(), StateStore(Path(tmp.name) / "state.json"), StoredUser(username="u"), lambda: None
        )
        self.addCleanup(self.page.deleteLater)

    def _customers(self):
        return [self.page.customer_list.item(i).text() for i in range(self.page.customer_list.count())]

    def _button_texts(self):
        from PySide6.QtWidgets import QPushButton

        return [b.text() for b in self.page.findChildren(QPushButton)]

    def test_only_reset_button_is_shown(self):
        texts = self._button_texts()
        self.assertIn("重置连接", texts)
        self.assertNotIn("初始化微信连接", texts)

    def test_switch_during_running_reload_reloads_again(self):
        from app.wechat_reader import WechatContact

        self.page._start_customer_refresh()
        self.page.on_wechat_reset_finished(
            wechat_cli_manager.WechatCliStatus(True, True, "已切换到当前微信账号", account_switched=True)
        )
        self.assertEqual(len(self.workers), 1)
        self.workers[0].callback([WechatContact("wxid_old", "", "顾客-旧账号")], "")
        self.assertEqual(len(self.workers), 2)
        self.assertEqual(self._customers(), [])
        self.workers[1].callback([WechatContact("wxid_new", "l", "顾客-黑大帅")], "")
        self.assertEqual(self._customers(), ["顾客-黑大帅"])
        self.assertTrue(self.page.refresh_btn.isEnabled())

    def test_reset_stays_busy_until_customers_of_new_account_are_loaded(self):
        from app.wechat_reader import WechatContact

        btn = self.page.reinit_wechat_btn
        self.page.reset_wechat_connection()
        self.assertFalse(btn.isEnabled())
        self.assertEqual(btn.text(), "正在切换…")
        self.assertIn("正在切换微信并识别顾客", self.page.wechat_status_label.text())
        self.assertFalse(self.page.refresh_btn.isEnabled())

        done = "已切换到当前微信账号 wxid_zpss75bi6rqv22（提取到 2 个数据库密钥）。"
        self.page.on_wechat_reset_finished(
            wechat_cli_manager.WechatCliStatus(True, True, done, account_switched=True)
        )
        # The success message must not appear while the old list could still be shown.
        self.assertFalse(btn.isEnabled())
        self.assertEqual(btn.text(), "正在识别顾客…")
        self.assertNotIn("已切换", self.page.wechat_status_label.text())
        self.assertIn("正在切换微信并识别顾客", self.page.status_label.text())
        self.assertEqual(len(self.workers), 1)

        self.workers[0].callback([WechatContact("wxid_new", "l", "顾客-黑大帅")], "")
        self.assertTrue(btn.isEnabled())
        self.assertEqual(btn.text(), "重置连接")
        self.assertEqual(self.page.wechat_status_label.text(), done)
        self.assertEqual(self._customers(), ["顾客-黑大帅"])
        self.assertIn("已加载 1 个", self.page.status_label.text())

    def test_failed_switch_reports_it_and_frees_the_button(self):
        from app.wechat_reader import WechatContact

        self.page.reset_wechat_connection()
        failed = "顾客列表没有切换：微信当前登录的是 wxid_new，但没有拿到该账号的密钥；仍在读取旧账号 wxid_old 的数据。"
        self.page.on_wechat_reset_finished(
            wechat_cli_manager.WechatCliStatus(True, True, failed, init_failed=True, active_db_dir="x")
        )
        self.message_boxes.return_value.open.assert_called_once()
        self.workers[0].callback([WechatContact("wxid_old", "", "顾客-旧账号")], "")
        self.assertTrue(self.page.reinit_wechat_btn.isEnabled())
        self.assertEqual(self.page.reinit_wechat_btn.text(), "重置连接")
        self.assertTrue(self.page.wechat_status_label.text().startswith("顾客列表没有切换"))

    def test_uninitialized_reset_failure_frees_the_button(self):
        self.page.reset_wechat_connection()
        self.page.on_wechat_reset_finished(wechat_cli_manager.WechatCliStatus(True, False, "微信未运行"))
        self.assertTrue(self.page.reinit_wechat_btn.isEnabled())
        self.assertTrue(self.page.refresh_btn.isEnabled())
        self.assertEqual(self.workers, [])

    def test_refresh_switches_account_when_wechat_changed(self):
        with mock.patch.object(self.page, "reset_wechat_connection") as reset:
            self.page._on_refresh_status_ready(
                wechat_cli_manager.WechatCliStatus(True, True, "不一致", active_db_dir="x"), True
            )
        reset.assert_called_once_with()
        self.assertEqual(self.workers, [])


class LoginPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _page(self, api_base):
        from app import main
        from app.state_store import StateStore

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        page = main.LoginPage(AppSettings(backend_api_base=api_base), StateStore(Path(tmp.name) / "s.json"), None)
        self.addCleanup(page.deleteLater)
        return page

    def _items(self, page):
        combo = page.backend_input
        return [combo.itemText(i) for i in range(combo.count())]

    def test_backend_is_a_dropdown_of_hosts_without_api(self):
        page = self._page("https://www.jujingbuluo123.com/api")
        self.assertEqual(self._items(page), ["https://www.jujingbuluo123.com", "http://127.0.0.1:3001"])
        self.assertEqual(page.backend_input.currentText(), "https://www.jujingbuluo123.com")
        self.assertFalse(page.backend_input.isEditable())

    def test_login_saves_selected_host_with_api(self):
        from app import main

        page = self._page("https://www.jujingbuluo123.com/api")
        page.backend_input.setCurrentIndex(1)
        with mock.patch.object(AppSettings, "save"), mock.patch.object(main.AuthWorker, "start"):
            page.login()
        self.assertEqual(page.settings.backend_api_base, "http://127.0.0.1:3001/api")
        self.assertEqual(page.settings.auth_login_url, "http://127.0.0.1:3001/api/auth/login")

    def test_unknown_saved_host_is_kept_as_extra_choice(self):
        page = self._page("http://10.0.0.5:8080/api")
        self.assertEqual(
            self._items(page), ["https://www.jujingbuluo123.com", "http://127.0.0.1:3001", "http://10.0.0.5:8080"]
        )
        self.assertEqual(page.backend_input.currentText(), "http://10.0.0.5:8080")
        self.assertEqual(page.backend_input.currentData(), "http://10.0.0.5:8080/api")

    def test_form_has_email_label_and_no_agent_fallback_field(self):
        from PySide6.QtWidgets import QCheckBox, QLabel, QLineEdit

        page = self._page("http://127.0.0.1:3001/api")
        labels = [label.text() for label in page.findChildren(QLabel)]
        self.assertIn("邮箱/用户名", labels)
        self.assertNotIn("用户名", labels)
        self.assertFalse(any("兜底" in text for text in labels))
        self.assertEqual(len(page.findChildren(QLineEdit)), 2)  # 邮箱/用户名 + 密码
        self.assertEqual([c.text() for c in page.findChildren(QCheckBox)], ["Agent 生成后自动发送到微信"])


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
