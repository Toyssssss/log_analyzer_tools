import json
import os
import tempfile
import unittest
from unittest.mock import patch

from log_analyzer import settings
from log_analyzer.paths import default_config_dir


class _SettingsFixture(unittest.TestCase):
    """Point settings_path() at a temp file so the real profile is untouched."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='la-settings-')
        self.cfg = os.path.join(self.tmp, 'settings.json')
        patcher = patch.object(settings, 'settings_path', lambda: self.cfg)
        self.addCleanup(patcher.stop)
        patcher.start()

    def write_raw(self, text):
        with open(self.cfg, 'w', encoding='utf-8') as f:
            f.write(text)


class SettingsPathTest(unittest.TestCase):
    def test_settings_path_is_inside_the_config_dir(self):
        self.assertEqual(settings.settings_path(),
                         os.path.join(default_config_dir(), 'settings.json'))

    def test_default_config_dir_is_absolute(self):
        self.assertTrue(os.path.isabs(default_config_dir()))


class LoadSaveTest(_SettingsFixture):
    def test_missing_file_loads_as_empty(self):
        self.assertEqual(settings.load_settings(), {})

    def test_roundtrip(self):
        # Built from the temp dir rather than '/tmp/...': a leading slash is
        # not an absolute path on Windows, and saved_workspace() rejects those.
        target = os.path.join(self.tmp, 'wherever')
        self.assertTrue(settings.save_workspace(target))
        self.assertEqual(settings.saved_workspace(), os.path.abspath(target))

    def test_corrupt_file_is_ignored(self):
        self.write_raw('}{"nope"')
        self.assertEqual(settings.load_settings(), {})
        self.assertIsNone(settings.saved_workspace())

    def test_a_json_list_is_not_a_settings_object(self):
        self.write_raw('[1, 2, 3]')
        self.assertEqual(settings.load_settings(), {})

    def test_save_preserves_unknown_keys(self):
        old = os.path.join(self.tmp, 'old')
        new = os.path.join(self.tmp, 'new')
        self.write_raw(json.dumps({'workspace': old, 'last_keyword': 'abc'}))
        settings.save_workspace(new)
        with open(self.cfg, encoding='utf-8') as f:
            obj = json.loads(f.read())
        self.assertEqual(obj['last_keyword'], 'abc')
        self.assertEqual(obj['workspace'], new)

    def test_relative_saved_workspace_is_ignored(self):
        # A relative value would resolve against whatever directory the app was
        # launched from, which is not a preference anyone can reason about.
        self.write_raw(json.dumps({'workspace': 'logs/ws'}))
        self.assertIsNone(settings.saved_workspace())

    def test_blank_saved_workspace_is_ignored(self):
        self.write_raw(json.dumps({'workspace': '   '}))
        self.assertIsNone(settings.saved_workspace())

    def test_non_string_saved_workspace_is_ignored(self):
        self.write_raw(json.dumps({'workspace': 42}))
        self.assertIsNone(settings.saved_workspace())

    def test_save_failure_returns_false_instead_of_raising(self):
        # A path whose parent is a regular file can never be written, which
        # stands in for a locked or AV-scanned settings file.
        blocker = os.path.join(self.tmp, 'blocker')
        with open(blocker, 'w') as f:
            f.write('x')
        with patch.object(settings, 'settings_path',
                          lambda: os.path.join(blocker, 'settings.json')):
            self.assertFalse(settings.save_workspace(os.path.join(self.tmp, 'new')))


class ResolvePrecedenceTest(_SettingsFixture):
    def setUp(self):
        super().setUp()
        # Real absolute paths for the current platform; '/tmp/x' is relative on
        # Windows and would be rejected by saved_workspace().
        self.p_explicit = os.path.join(self.tmp, 'explicit')
        self.p_env = os.path.join(self.tmp, 'env')
        self.p_saved = os.path.join(self.tmp, 'saved')

    def test_explicit_wins_over_everything(self):
        settings.save_workspace(self.p_saved)
        with patch.dict(os.environ, {settings.ENV_VAR: self.p_env}):
            root, origin = settings.resolve_workspace(self.p_explicit)
        self.assertEqual(root, os.path.abspath(self.p_explicit))
        self.assertEqual(origin, 'cli')

    def test_env_wins_over_the_saved_preference(self):
        # Deliberate: the env var is the documented deployment override, and a
        # stale settings file must not defeat it.
        settings.save_workspace(self.p_saved)
        with patch.dict(os.environ, {settings.ENV_VAR: self.p_env}):
            root, origin = settings.resolve_workspace()
        self.assertEqual(root, os.path.abspath(self.p_env))
        self.assertEqual(origin, 'env')

    def test_saved_wins_over_the_default(self):
        settings.save_workspace(self.p_saved)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(settings.ENV_VAR, None)
            root, origin = settings.resolve_workspace()
        self.assertEqual(root, os.path.abspath(self.p_saved))
        self.assertEqual(origin, 'settings')

    def test_default_is_the_last_resort(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(settings.ENV_VAR, None)
            root, origin = settings.resolve_workspace()
        self.assertEqual(origin, 'default')
        self.assertTrue(os.path.isabs(root))

class UsableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='la-usable-')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_missing_directory_is_created(self):
        target = os.path.join(self.tmp, 'a', 'b')
        ok, why = settings.workspace_is_usable(target)
        self.assertTrue(ok, why)
        self.assertTrue(os.path.isdir(target))

    def test_an_existing_writable_directory_is_usable(self):
        ok, why = settings.workspace_is_usable(self.tmp)
        self.assertTrue(ok, why)

    def test_the_probe_file_does_not_survive(self):
        settings.workspace_is_usable(self.tmp)
        self.assertEqual(os.listdir(self.tmp), [])

    @unittest.skipIf(os.name == 'nt', 'Windows ACLs do not follow the mode bits')
    def test_a_readonly_directory_is_rejected(self):
        target = os.path.join(self.tmp, 'ro')
        os.makedirs(target)
        os.chmod(target, 0o500)
        self.addCleanup(os.chmod, target, 0o700)
        ok, why = settings.workspace_is_usable(target)
        self.assertFalse(ok)
        self.assertTrue(why)


if __name__ == '__main__':
    unittest.main()
