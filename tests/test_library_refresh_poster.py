# -*- coding: utf-8 -*-
"""Page-scoped library refresh, versioned posters and Plex poster routing."""

import json
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, Mock, patch

if not os.environ.get("NASTOOL_CONFIG"):
    os.environ["NASTOOL_CONFIG"] = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "config", "config.yaml")

# plexapi is optional in the offline test environment.
for _name, _attrs in (("plexapi", {}), ("plexapi.myplex", {"MyPlexAccount": object}),
                      ("plexapi.server", {"PlexServer": object})):
    try:
        __import__(_name)
    except Exception:
        _module = types.ModuleType(_name)
        for _key, _value in _attrs.items():
            setattr(_module, _key, _value)
        sys.modules[_name] = _module

from app.db.transactions import DatabaseWriteError
from app.library import MediaLibrary
import app.mediaserver.client.emby as emby_module
import app.mediaserver.client.jellyfin as jellyfin_module
import app.mediaserver.client.plex as plex_module
from app.mediaserver.client.emby import Emby
from app.mediaserver.client.jellyfin import Jellyfin
from app.mediaserver.client.plex import Plex


def _row(item_id, **overrides):
    values = dict(SERVER="Emby", ITEM_ID=item_id, LIBRARY="lib", ITEM_TYPE="Movie",
                  TITLE="Movie", ORGIN_TITLE="", YEAR="2020", TMDBID="1", IMDBID="",
                  PATH="/media/Movie", JSON='{"Name": "Movie"}')
    values.update(overrides)
    return types.SimpleNamespace(**values)


def _fresh(item_id, **overrides):
    values = dict(id=item_id, library="lib", type="Movie", title="Movie", originalTitle="",
                  year=2020, tmdbid="1", imdbid="", path="/media/Movie", json='{"Name": "Movie"}')
    values.update(overrides)
    return values


class LibraryRefreshTest(unittest.TestCase):
    def library(self, rows, fresh, syncing=False, lock_ok=True, insert_ok=True):
        events = []
        library = MediaLibrary.__new__(MediaLibrary)
        library.media_server = Mock()
        library.media_server.get_type.return_value = types.SimpleNamespace(value="Emby")
        library.media_server.is_syncing.return_value = syncing

        def begin():
            events.append("lock")
            return lock_ok

        library.media_server.begin_incremental_write.side_effect = begin
        library.media_server.end_incremental_write.side_effect = lambda: events.append("unlock")

        def read(server, item_id):
            events.append("read:%s" % item_id)
            value = fresh[item_id]
            if isinstance(value, Exception):
                raise value
            return value

        library.media_server.get_sync_item.side_effect = read
        library.mediadb = MagicMock()
        library.mediadb.find_items.return_value = rows

        def insert(server, record):
            events.append("write:%s" % record["id"])
            return insert_ok

        library.mediadb.insert.side_effect = insert
        return library, events

    def test_updates_changed_items_after_all_reads_and_keeps_failures(self):
        rows = [_row("1"), _row("2"), _row("3")]
        fresh = {"1": _fresh("1", title="Renamed"), "2": _fresh("2"),
                 "3": RuntimeError("down")}
        library, events = self.library(rows, fresh)
        MediaLibrary._poster_cache[("emby", "1")] = (10 ** 12, "/old/poster.jpg")
        self.addCleanup(MediaLibrary._poster_cache.clear)
        result = library.refresh_items({"item_ids": ["1", "2", "3", "4", "1"]})
        self.assertEqual(result["code"], 0)
        self.assertEqual((result["updated"], result["unchanged"], result["failed"]), (1, 1, 2))
        self.assertEqual(result["requested"], 4)
        # Reads are serial, finish before the lock is taken, and one short write follows.
        self.assertEqual(events, ["read:1", "read:2", "read:3", "lock", "write:1", "unlock"])
        library.mediadb.insert.assert_called_once()
        self.assertEqual(library.mediadb.insert.call_args.args[0], "Emby")
        self.assertNotIn(("emby", "1"), MediaLibrary._poster_cache)

    def test_numeric_server_values_do_not_count_as_changes(self):
        library, events = self.library([_row("1", YEAR="2020", LIBRARY="2")],
                                       {"1": _fresh("1", year=2020, library=2)})
        result = library.refresh_items({"item_ids": ["1"]})
        self.assertEqual((result["updated"], result["unchanged"]), (0, 1))
        self.assertNotIn("lock", events)
        library.mediadb.write_transaction.assert_not_called()

    def test_rejects_oversized_malformed_and_syncing_requests_without_reading(self):
        library, events = self.library([], {})
        too_many = [str(index) for index in range(MediaLibrary.refresh_item_limit + 1)]
        for payload in ({}, {"item_ids": "1"}, {"item_ids": []}, {"item_ids": too_many}):
            with self.subTest(payload=payload):
                self.assertEqual(library.refresh_items(payload)["code"], -1)
        busy, busy_events = self.library([_row("1")], {"1": _fresh("1")}, syncing=True)
        self.assertEqual(busy.refresh_items({"item_ids": ["1"]})["code"], -1)
        self.assertEqual(events + busy_events, [])

    def test_never_starts_a_full_sync_or_server_rescrape(self):
        library, _ = self.library([_row("1")], {"1": _fresh("1", title="New")})
        library.refresh_items({"item_ids": ["1"]})
        self.assertFalse(library.media_server.sync_mediaserver.called)
        self.assertFalse(library.media_server.refresh_root_library.called)

    def test_full_sync_started_during_reads_blocks_the_write(self):
        library, events = self.library([_row("1")], {"1": _fresh("1", title="New")}, lock_ok=False)
        result = library.refresh_items({"item_ids": ["1"]})
        self.assertEqual(result["code"], -1)
        self.assertEqual(events, ["read:1", "lock"])
        library.mediadb.insert.assert_not_called()

    def test_failed_write_reports_error_and_releases_the_lock(self):
        library, events = self.library([_row("1")], {"1": _fresh("1", title="New")}, insert_ok=False)
        result = library.refresh_items({"item_ids": ["1"]})
        self.assertEqual(result["code"], -1)
        self.assertEqual(events[-1], "unlock")

    def test_mismatched_server_item_is_not_written(self):
        library, _ = self.library([_row("1")], {"1": _fresh("other")})
        result = library.refresh_items({"item_ids": ["1"]})
        self.assertEqual((result["updated"], result["failed"]), (0, 1))
        library.mediadb.insert.assert_not_called()

    def test_poster_version_and_url(self):
        tag = MediaLibrary._MediaLibrary__poster_tag
        url = MediaLibrary._MediaLibrary__poster_url
        self.assertEqual(tag({"ImageTags": {"Primary": "abc123"}}), "abc123")
        self.assertEqual(tag({"Thumb": "/library/metadata/5/thumb/1700000000"}), "1700000000")
        self.assertEqual(tag({"ImageTags": {"Primary": "bad tag&"}}), "")
        self.assertEqual(tag({"ImageTags": []}), "")
        self.assertEqual(url("item1", ""), "/library/image/item1")
        self.assertEqual(url("item1", "abc"), "/library/image/item1?tag=abc")
        self.assertEqual(url("/library/metadata/5", "170"), "/library/image/library/metadata/5?tag=170")


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def __bool__(self):
        return self.status_code < 400


class ClientImageAndSyncTest(unittest.TestCase):
    ITEM = {"Id": "1", "Type": "Movie", "ParentId": "lib", "Name": "Movie", "OriginalTitle": "Orig",
            "ProductionYear": 2020, "ProviderIds": {"Tmdb": "9", "Imdb": "tt9"}, "Path": "/m/Movie",
            "ImageTags": {"Primary": "tag1"}}

    def emby(self):
        with patch.object(Emby, "get_admin_user", return_value={}), \
                patch.object(Emby, "_Emby__get_emby_librarys", return_value=[]):
            return Emby(config={"host": "http://emby:8096", "api_key": "KEY"})

    def jellyfin(self):
        with patch.object(Jellyfin, "get_admin_user", return_value={}):
            return Jellyfin(config={"host": "http://jf:8096", "api_key": "KEY"})

    def capture(self, module):
        seen = []

        class Fake:
            def __init__(self, **kwargs):
                pass

            def get_res(self, url, **kwargs):
                seen.append(url)
                return FakeResponse({"Items": [{"Id": "1", "Type": "Movie"}]})

        return seen, patch.object(module, "RequestUtils", Fake)

    def test_thumbnail_requests_are_sized_versioned_and_encoded(self):
        for client, module, prefix in ((self.emby(), emby_module, "http://emby:8096/emby/"),
                                       (self.jellyfin(), jellyfin_module, "http://jf:8096/")):
            seen, patched = self.capture(module)
            with patched:
                client.get_item_image("it 1", "Primary", max_width=320, quality=85, tag="t1")
                client.get_item_image("it1")
            self.assertEqual(
                seen[0], prefix + "Items/it%201/Images/Primary?api_key=KEY&maxWidth=320&quality=85&tag=t1")
            self.assertEqual(seen[1], prefix + "Items/it1/Images/Primary?api_key=KEY")

    def test_refresh_record_matches_full_sync_record(self):
        for client, module in ((self.emby(), emby_module), (self.jellyfin(), jellyfin_module)):
            _, patched = self.capture(module)
            with patched, patch.object(type(client), "get_iteminfo", return_value=dict(self.ITEM)):
                listed = next(iter(client.get_items("parent", strict=True)))
                single = client.get_sync_item("1")
            self.assertEqual(single, listed)
            self.assertEqual(single["id"], "1")
            self.assertEqual(json.loads(single["json"])["ImageTags"]["Primary"], "tag1")

    def plex(self):
        client = Plex(config={"host": ""})
        item = types.SimpleNamespace(
            key="/library/metadata/5", type="movie", librarySectionID=2, title="Movie", year=2020,
            thumb="/library/metadata/5/thumb/170", guid="plex://movie/abc",
            guids=[types.SimpleNamespace(id="tmdb://99")], locations=["/media/Movie"], media=[])
        section = types.SimpleNamespace(all=lambda: [item])
        server = Mock()
        server.library.sectionByID.return_value = section
        server.fetchItem.return_value = item
        server.url.side_effect = lambda key, includeToken=None: "http://plex:32400%s&X-Plex-Token=T" % key \
            if "?" in key else "http://plex:32400%s?X-Plex-Token=T" % key
        client._plex = server
        return client, server

    def test_plex_refresh_record_matches_full_sync_and_carries_poster_version(self):
        client, server = self.plex()
        listed = next(iter(client.get_items("2", strict=True)))
        single = client.get_sync_item("/library/metadata/5")
        self.assertEqual(single, listed)
        server.fetchItem.assert_called_with("/library/metadata/5")
        self.assertEqual(json.loads(single["json"])["Thumb"], "/library/metadata/5/thumb/170")
        self.assertEqual(MediaLibrary._MediaLibrary__poster_tag(json.loads(single["json"])), "170")

    def test_plex_poster_uses_server_side_token_and_rejects_other_ids(self):
        client, server = self.plex()
        seen = []

        class Fake:
            def get_res(self, url, **kwargs):
                seen.append(url)
                return FakeResponse()

        with patch.object(plex_module, "RequestUtils", Fake):
            client.get_item_image("/library/metadata/5", "Primary", max_width=320, tag="170")
            client.get_item_image("/library/metadata/5")
            self.assertIsNone(client.get_item_image("/library/metadata/5", "Backdrop"))
            self.assertIsNone(client.get_item_image("/library/sections/1"))
            self.assertIsNone(client.get_item_image("/library/metadata/5/../../x"))
        self.assertEqual(len(seen), 2)
        self.assertIn("photo/:/transcode?", seen[0])
        self.assertIn("url=%2Flibrary%2Fmetadata%2F5%2Fthumb%2F170", seen[0])
        self.assertIn("width=320", seen[0])
        self.assertEqual(seen[1], "http://plex:32400/library/metadata/5/thumb?X-Plex-Token=T")


class LibraryImageRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import web.main as web_main
        cls.web_main = web_main

    def setUp(self):
        self.app = self.web_main.App
        previous = self.app.config.get("LOGIN_DISABLED")
        self.app.config["LOGIN_DISABLED"] = True
        self.addCleanup(self.app.config.__setitem__, "LOGIN_DISABLED", previous)
        self.client = self.app.test_client()
        self.server = Mock()
        self.server.get_type.return_value = types.SimpleNamespace(value="Emby")
        self.server.get_item_image.return_value = types.SimpleNamespace(
            status_code=200, content=b"jpeg", headers={"Content-Type": "image/jpeg"})
        self.library = Mock()
        self.library.get_local_poster_file.return_value = ""
        for name, value in (("MediaServer", Mock(return_value=self.server)),
                            ("MediaLibrary", Mock(return_value=self.library))):
            patcher = patch.object(self.web_main, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_versioned_thumbnail_is_cached_long_and_sized(self):
        response = self.client.get("/library/image/abc123?tag=t1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "private, max-age=31536000, immutable")
        self.assertEqual(response.headers["Content-Type"], "image/jpeg")
        self.server.get_item_image.assert_called_once_with(
            "abc123", "Primary", max_width=320, quality=85, tag="t1")

    def test_unversioned_or_invalid_tag_keeps_the_short_cache(self):
        for query in ("", "?tag=bad%20tag%21"):
            with self.subTest(query=query):
                self.server.get_item_image.reset_mock()
                response = self.client.get("/library/image/abc123" + query)
                self.assertEqual(response.headers["Cache-Control"], "private, max-age=3600")
                self.assertIsNone(self.server.get_item_image.call_args.kwargs["tag"])

    def test_missing_upstream_poster_falls_back_to_local_file_without_long_cache(self):
        self.server.get_item_image.return_value = types.SimpleNamespace(
            status_code=200, content=b"<html>", headers={"Content-Type": "text/html"})
        with tempfile.NamedTemporaryFile(suffix=".jpg") as poster:
            poster.write(b"local")
            poster.flush()
            self.library.get_local_poster_file.return_value = poster.name
            response = self.client.get("/library/image/abc123?tag=t1")
            self.assertEqual(response.data, b"local")
            self.assertEqual(response.headers["Cache-Control"], "private, no-cache")
            response.close()
        self.library.get_local_poster_file.return_value = ""
        missing = self.client.get("/library/image/abc123?tag=t1")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.headers["Cache-Control"], "private, max-age=300")

    def test_server_exception_still_tries_the_local_poster(self):
        self.server.get_item_image.side_effect = RuntimeError("boom")
        with tempfile.NamedTemporaryFile(suffix=".jpg") as poster:
            poster.write(b"local")
            poster.flush()
            self.library.get_local_poster_file.return_value = poster.name
            response = self.client.get("/library/image/abc123")
            self.assertEqual(response.data, b"local")
            response.close()

    def test_invalid_ids_and_image_types_never_reach_the_media_server(self):
        for path in ("/library/image/a/b", "/library/image/a%20b", "/library/image/abc?type=../x",
                     "/library/image/abc?type=Chapter"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)
        self.server.get_item_image.assert_not_called()

    def test_plex_ids_are_restored_from_the_route_path(self):
        self.server.get_type.return_value = types.SimpleNamespace(value="Plex")
        response = self.client.get("/library/image/library/metadata/12?tag=170")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.server.get_item_image.call_args.args[0], "/library/metadata/12")
        self.server.get_item_image.reset_mock()
        self.assertEqual(self.client.get("/library/image/library/../etc").status_code, 404)
        self.server.get_item_image.assert_not_called()

    def test_refresh_route_delegates_to_the_page_scoped_method(self):
        self.library.refresh_items.return_value = {"code": 0, "updated": 1}
        response = self.client.post("/library/refresh", json={"item_ids": ["1"]})
        self.assertEqual(response.get_json(), {"code": 0, "updated": 1})
        self.library.refresh_items.assert_called_once_with({"item_ids": ["1"]})


class SubtitlePreviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import web.main as web_main
        cls.web_main = web_main

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.media = os.path.join(self.temp.name, "Movie (2024).mkv")
        open(self.media, "wb").close()
        open(os.path.join(self.temp.name, "Movie (2024).zh-CN.srt"), "w").close()
        self.app = self.web_main.App
        previous = self.app.config.get("LOGIN_DISABLED")
        self.app.config["LOGIN_DISABLED"] = True
        self.addCleanup(self.app.config.__setitem__, "LOGIN_DISABLED", previous)
        self.client = self.app.test_client()

    def preview(self, names, server="emby", media=None):
        with patch.object(self.web_main, "_online_subtitle_media_path", side_effect=lambda value: value):
            return self.client.post("/library/subtitle/preview", json={
                "media_path": media or self.media, "filenames": names, "server": server}).get_json()

    def test_predicts_language_and_numbers_existing_names_without_reading_content(self):
        rows = self.preview(["Movie.chs.srt", "Movie.eng.srt", "plain.srt", "bad.txt"])["items"]
        by_name = {row["name"]: row for row in rows}
        self.assertEqual(by_name["Movie.chs.srt"]["language"], "zh-CN")
        # zh-CN already exists beside the video, so the new file is numbered, never overwritten.
        self.assertEqual(by_name["Movie.chs.srt"]["target"], "Movie (2024).zh-CN(1).srt")
        self.assertEqual(by_name["Movie.eng.srt"]["target"], "Movie (2024).eng.srt")
        self.assertEqual(by_name["plain.srt"]["language"], "")
        self.assertIn("正文识别", by_name["plain.srt"]["note"])
        self.assertIn("自动编号", by_name["plain.srt"]["note"])
        self.assertEqual(by_name["bad.txt"]["note"], "不支持的字幕格式")
        self.assertEqual(sorted(os.listdir(self.temp.name)),
                         ["Movie (2024).mkv", "Movie (2024).zh-CN.srt"])

    def test_server_naming_rules_apply_and_vobsub_pairs_are_explained(self):
        plex = {row["name"]: row for row in self.preview(["Movie.eng.srt"], "plex")["items"]}
        jellyfin = {row["name"]: row for row in self.preview(["Movie.eng.srt"], "jellyfin")["items"]}
        self.assertEqual(plex["Movie.eng.srt"]["target"], "Movie (2024).en.srt")
        self.assertEqual(jellyfin["Movie.eng.srt"]["target"], "Movie (2024).eng.srt")
        pair = {row["name"]: row for row in self.preview(["a.sub", "a.idx", "lonely.idx"])["items"]}
        self.assertTrue(pair["a.sub"]["target"].endswith(".sub"))
        self.assertEqual(pair["a.idx"]["note"], "与同名 .sub 成对上传")
        self.assertEqual(pair["lonely.idx"]["note"], "缺少同名 .sub 文件")

    def test_rejects_malformed_requests_and_unauthorised_media(self):
        for names in ([], "x.srt", [""], ["x.srt"] * 41, [1]):
            with self.subTest(names=names):
                self.assertEqual(self.preview(names)["code"], -1)
        # Only the base name is used, so a path in the file name cannot steer the target.
        row = self.preview(["../../evil.eng.srt"])["items"][0]
        self.assertEqual(row["name"], "evil.eng.srt")
        self.assertNotIn("/", row["target"])
        response = self.client.post("/library/subtitle/preview", json={
            "media_path": "/etc/passwd", "filenames": ["x.srt"]}).get_json()
        self.assertEqual(response["code"], -1)


if __name__ == "__main__":
    unittest.main()
