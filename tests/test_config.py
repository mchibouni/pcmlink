"""Tests for the parts that need no audio stack.

Deliberately dependency-free: these run on any machine with Python, including
Windows CI runners where GStreamer is not installed.
"""
import argparse
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pcmlink  # noqa: E402


class Coerce(unittest.TestCase):
    def test_bool_words(self):
        for word in ("1", "true", "TRUE", "yes", "on"):
            self.assertIs(pcmlink.coerce(True, word), True)
        for word in ("0", "false", "no", "off", ""):
            self.assertIs(pcmlink.coerce(True, word), False)

    def test_numeric_types_are_preserved(self):
        self.assertEqual(pcmlink.coerce(5004, "6000"), 6000)
        self.assertIsInstance(pcmlink.coerce(5004, "6000"), int)
        self.assertAlmostEqual(pcmlink.coerce(1.0, "0.25"), 0.25)


class Precedence(unittest.TestCase):
    """defaults < config top level < config [role] < environment < CLI."""

    def setUp(self):
        for key in list(os.environ):
            if key.startswith("PCMLINK_"):
                del os.environ[key]

    tearDown = setUp

    def resolve(self, cfg, role="receive", **cli):
        ns = argparse.Namespace(**{k: None for k in pcmlink.DEFAULTS})
        for k, v in cli.items():
            setattr(ns, k, v)
        return pcmlink.resolve(ns, cfg, role)

    def test_default_when_nothing_set(self):
        self.assertEqual(self.resolve({})["port"], pcmlink.DEFAULTS["port"])

    def test_top_level_beats_default(self):
        self.assertEqual(self.resolve({"port": 6000})["port"], 6000)

    def test_role_table_beats_top_level(self):
        cfg = {"port": 6000, "receive": {"port": 7000}}
        self.assertEqual(self.resolve(cfg)["port"], 7000)

    def test_other_role_table_is_ignored(self):
        cfg = {"port": 6000, "send": {"port": 7000}}
        self.assertEqual(self.resolve(cfg, role="receive")["port"], 6000)

    def test_env_beats_config(self):
        os.environ["PCMLINK_PORT"] = "8000"
        cfg = {"port": 6000, "receive": {"port": 7000}}
        self.assertEqual(self.resolve(cfg)["port"], 8000)

    def test_cli_beats_env(self):
        os.environ["PCMLINK_PORT"] = "8000"
        self.assertEqual(self.resolve({}, port=9000)["port"], 9000)

    def test_env_coerces_booleans(self):
        os.environ["PCMLINK_STATS"] = "false"
        self.assertIs(self.resolve({})["stats"], False)


class ConfigFiles(unittest.TestCase):
    def test_explicit_file_is_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.toml")
            with open(path, "w") as fh:
                fh.write('port = 4321\n[receive]\ndevice = "Thing"\n')
            cfg, used = pcmlink.load_config(path)
            self.assertEqual(cfg["port"], 4321)
            self.assertEqual(cfg["receive"]["device"], "Thing")
            self.assertEqual(used, [path])

    def test_search_path_is_ordered_least_important_first(self):
        paths = pcmlink.config_paths()
        self.assertTrue(paths, "search path must not be empty")
        # The working-directory file is the most important, so it comes last.
        self.assertEqual(paths[-1], os.path.join(os.getcwd(), pcmlink.PROJECT_BASENAME))


class Pipelines(unittest.TestCase):
    """String construction only; no elements are instantiated."""

    def cfg(self, **over):
        base = dict(pcmlink.DEFAULTS)
        base.update(over)
        return base

    def test_wire_format_is_big_endian(self):
        desc = pcmlink.build_send(self.cfg(host="10.0.0.1"), tone=True)
        self.assertIn("format=S16BE", desc)

    def test_gain_precedes_wire_conversion(self):
        # volume cannot process S16BE, so the gain must come first.
        desc = pcmlink.build_send(self.cfg(host="10.0.0.1"), tone=True)
        self.assertLess(desc.index("volume name=gain"), desc.index("format=S16BE"))

    def test_test_tone_replaces_capture(self):
        desc = pcmlink.build_send(self.cfg(host="10.0.0.1"), tone=True)
        self.assertIn("audiotestsrc", desc)
        self.assertNotIn("wasapi2src", desc)
        self.assertNotIn("pulsesrc", desc)

    def test_tone_volume_is_applied(self):
        desc = pcmlink.build_send(self.cfg(host="10.0.0.1", tone_volume=0.02), tone=True)
        self.assertIn("volume=0.02", desc)


if __name__ == "__main__":
    unittest.main()
