"""
Bot ki HAR file load hoti hai ya nahi — deploy se pehle chalao.
Koi naam import karna bhool gaye (NameError) to bot start hote hi crash hota hai;
ye test wahi pakadta hai.

    python -m unittest tests/test_imports.py -v
"""
import importlib
import os
import pkgutil
import sys
import unittest

os.environ.setdefault("BOT_TOKEN", "1:x")
BOT_DIR = os.path.join(os.path.dirname(__file__), "..", "bot")
sys.path.insert(0, BOT_DIR)


class ImportAllTest(unittest.TestCase):
    def test_every_bot_module_imports(self):
        for mod in pkgutil.iter_modules([BOT_DIR]):
            with self.subTest(module=mod.name):
                importlib.import_module(mod.name)


if __name__ == "__main__":
    unittest.main()
