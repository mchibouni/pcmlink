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


def _have_toml() -> bool:
    import importlib.util
    return any(importlib.util.find_spec(m) for m in ("tomllib", "tomli"))


class ConfigFiles(unittest.TestCase):
    @unittest.skipUnless(_have_toml(), "needs Python 3.11+ or tomli")
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

    def test_jack_taps_the_named_ports(self):
        desc = pcmlink.build_send(self.cfg(host="10.0.0.1", jack="crone:output_[12]"), tone=False)
        self.assertTrue(desc.startswith("jackaudiosrc "))
        # Without the pattern, connect=auto would wire up the physical inputs.
        self.assertIn('port-pattern="crone:output_[12]"', desc)
        self.assertNotIn("pulsesrc", desc)

    def test_jack_wins_over_a_named_device(self):
        # build_send must not try to resolve the device through GStreamer.
        cfg = self.cfg(host="10.0.0.1", jack="system:capture_.*", device="Anything")
        self.assertIn("jackaudiosrc", pcmlink.build_send(cfg, tone=False))

    def test_tone_wins_over_jack(self):
        desc = pcmlink.build_send(self.cfg(host="10.0.0.1", jack="crone:output_[12]"), tone=True)
        self.assertIn("audiotestsrc", desc)
        self.assertNotIn("jackaudiosrc", desc)


    def test_receiver_plays_into_the_named_jack_ports(self):
        desc = pcmlink.build_receive(self.cfg(jack="crone:input_[12]"))
        self.assertIn('jackaudiosink connect=auto port-pattern="crone:input_[12]"', desc)
        self.assertNotIn("autoaudiosink", desc)


class Toml(unittest.TestCase):
    def test_no_config_file_needs_no_toml_module(self):
        # Python < 3.11 has no tomllib; with nothing to read, it must not matter.
        with tempfile.TemporaryDirectory() as d:
            env = {"XDG_CONFIG_DIRS": d, "XDG_CONFIG_HOME": d, "HOME": d}
            cwd = os.getcwd()
            old = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            os.chdir(d)
            try:
                saved = sys.modules.get("tomllib")
                sys.modules["tomllib"] = None  # type: ignore[assignment]  # makes "import tomllib" fail
                try:
                    self.assertEqual(pcmlink.load_config(None), ({}, []))
                finally:
                    if saved is None:
                        del sys.modules["tomllib"]
                    else:
                        sys.modules["tomllib"] = saved
            finally:
                os.chdir(cwd)
                for k, v in old.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
