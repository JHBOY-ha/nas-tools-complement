"""Offline route/template regression: python3 -m unittest tests.test_recommend_rendering -v."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from flask import Flask, render_template, request


ROOT = Path(__file__).resolve().parents[1]


def create_app():
    # Execute real route bodies without starting NAS services or loading user data.
    app = Flask(__name__, template_folder=str(ROOT / "web/templates"))
    app.testing = True
    names = ("downloaded", "recommend", "douban_movie", "douban_tv", "tmdb_movie", "tmdb_tv")
    tree = ast.parse((ROOT / "web/main.py").read_text())
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    for node in functions:
        node.decorator_list = []
    namespace = dict(render_template=render_template, request=request, json=json,
                     ModuleConf=SimpleNamespace(DISCOVER_FILTER_CONF={}))
    exec(compile(ast.Module(body=functions, type_ignores=[]), "web/main.py", "exec"), namespace)
    for name in names:
        app.add_url_rule("/" + name, view_func=namespace[name], methods=["GET", "POST"])
    return app


class RecommendRenderingTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()

    def test_routes_without_filters_render_repeatedly(self):
        with self.app.test_client() as client:
            for _ in range(2):
                for name in ("downloaded", "douban_movie", "douban_tv", "tmdb_movie", "tmdb_tv"):
                    with self.subTest(route=name):
                        response = client.post("/" + name)
                        self.assertEqual(response.status_code, 200)
                        self.assertIn("Params = {};", response.get_data(as_text=True))

    def test_explicit_filters_keep_json_types_and_escape_script_delimiters(self):
        params = {"enabled": True, "optional": None, "query": "</script><script>alert(1)</script>"}
        with self.app.test_client() as client:
            response = client.get("/recommend", query_string={"params": json.dumps(params)})
            self.assertEqual(response.status_code, 200)
            serialized = response.get_data(as_text=True).split("Params = ", 1)[1].split(";", 1)[0]
            self.assertEqual(json.loads(serialized), params)
            self.assertNotIn("</script>", serialized)


if __name__ == "__main__":
    unittest.main()
