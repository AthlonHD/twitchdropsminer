import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("gateway_contract", ROOT / "integrations/hdtool/gateway.py")
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


class UpstreamContractTests(unittest.TestCase):
    def test_real_upstream_helper_can_be_adapted_after_merge(self):
        code = gateway.helper_source(ROOT)
        compile(code, "login-helper.py", "exec")
        self.assertIn('"Authorization": "Bearer " + password', code)
        self.assertIn("/portal/api/login", code)
        self.assertNotIn('"X-Fleet-Password": password', code)

    def test_real_upstream_header_has_one_fixed_chinese_control(self):
        source = (ROOT / "web/index.html").read_text("utf-8")
        self.assertIn('class="language-selector"', source)
        for admin in (False, True):
            with self.subTest(admin=admin):
                page = gateway.render_dashboard(source, {"admin": admin})
                self.assertNotIn('class="language-selector"', page)
                self.assertIn('id="language" hidden disabled', page)
                self.assertNotIn("position:fixed;right:12px", page)
                header = page.split('class="header-brand-right"', 1)[1].split("</header>", 1)[0]
                self.assertIn("HD Tool 用户中心" if admin else "退出登录", header)

    def test_chinese_login_instructions_do_not_fall_back_to_english(self):
        import json
        chinese = json.loads((ROOT / "lang/简体中文.json").read_text("utf-8"))
        for key in ("helper_intro", "helper_copy", "helper_download", "helper_http_warning", "helper_py_hint"):
            self.assertTrue(chinese["login"]["browser_login"][key])
