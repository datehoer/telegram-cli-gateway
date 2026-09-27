from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXTENSION = REPO / "extensions" / "bilibili"

# 这个扩展只依赖标准库，但装在 extensions/ 下，不在包的 import 路径里。
# 直接按文件路径加载，测试不需要网关、不需要网络。
sys.path.insert(0, str(EXTENSION))
import bili

MAIN_SPEC = importlib.util.spec_from_file_location("bili_main", EXTENSION / "main.py")
assert MAIN_SPEC and MAIN_SPEC.loader
main_module = importlib.util.module_from_spec(MAIN_SPEC)
# dataclasses 处理注解时要在 sys.modules 里按 __module__ 找到这个模块，
# 用 spec 直接 exec 必须先手动登记。
sys.modules["bili_main"] = main_module
MAIN_SPEC.loader.exec_module(main_module)


class WbiTests(unittest.TestCase):
    def test_mixin_key_matches_the_documented_golden_value(self) -> None:
        """混入密钥表是硬编码的常量，必须与 B站文档一致，否则所有签名都会错。"""
        img_key = "7cd084941338484aae1ad9425b84077c"
        sub_key = "4932caff0ff746eab6f01bf08b70ac45"
        mixin = "".join((img_key + sub_key)[index] for index in bili.MIXIN_KEY_ENC_TAB[:32])
        self.assertEqual(mixin, "ea1db124af3c7062474693fa704f4ff8")

    def test_fnval_combination_is_the_accepted_value(self) -> None:
        """少 OR 任何一位都会被 B站拒为 -400 或静默降档。"""
        self.assertEqual(bili.FNVAL_DEFAULT, 4048)

    def test_wbi_query_is_deterministic_and_sorted(self) -> None:
        img_key = "7cd084941338484aae1ad9425b84077c"
        sub_key = "4932caff0ff746eab6f01bf08b70ac45"
        query = bili.wbi_query({"bvid": "BV1xx411c7mD", "cid": 62131}, img_key, sub_key, now=1700000000)
        self.assertTrue(query.startswith("bvid=BV1xx411c7mD&cid=62131&wts=1700000000&w_rid="))
        self.assertEqual(len(query.rsplit("w_rid=", 1)[1]), 32)
        same = bili.wbi_query({"cid": 62131, "bvid": "BV1xx411c7mD"}, img_key, sub_key, now=1700000000)
        self.assertEqual(query, same, "参数顺序不应影响签名")

    def test_wbi_encode_filters_special_characters(self) -> None:
        self.assertEqual(bili._wbi_encode("a b"), "a%20b")
        self.assertEqual(bili._wbi_encode("a!b'c(d)e*f"), "abcdef")
        self.assertEqual(bili._wbi_encode("a-b_c.d~e"), "a-b_c.d~e")


class ParseRefTests(unittest.TestCase):
    def test_accepts_bv_av_ep_ss_and_bare_numbers(self) -> None:
        cases = {
            "BV1xx411c7mD": ("video", {"bvid": "BV1xx411c7mD"}),
            "https://www.bilibili.com/video/BV1xx411c7mD?p=2": ("video", {"bvid": "BV1xx411c7mD"}),
            "av2": ("video", {"aid": 2}),
            "12345": ("video", {"aid": 12345}),
            "ep123": ("bangumi", {"ep_id": 123}),
            "https://www.bilibili.com/bangumi/play/ss456": ("bangumi", {"season_id": 456}),
        }
        for raw, (kind, fields) in cases.items():
            with self.subTest(raw=raw):
                ref = bili.parse_ref(raw)
                self.assertEqual(ref.kind, kind)
                for name, expected in fields.items():
                    self.assertEqual(getattr(ref, name), expected)

    def test_rejects_unrecognized_input(self) -> None:
        with self.assertRaises(bili.BiliError):
            bili.parse_ref("https://example.com/nothing")

    def test_page_argument_is_carried_through(self) -> None:
        self.assertEqual(bili.parse_ref("BV1xx411c7mD", page=3).page, 3)


class QualityTests(unittest.TestCase):
    def test_never_exceeds_the_requested_quality(self) -> None:
        accepted = [120, 80, 64, 32, 16]
        self.assertEqual(bili.pick_quality(accepted, 80), 80)
        self.assertEqual(bili.pick_quality(accepted, 64), 64)
        self.assertEqual(bili.pick_quality(accepted, 120), 120)

    def test_falls_back_to_the_best_available_when_request_is_too_high(self) -> None:
        # 未登录：只有 480P/360P，请求 4K 时应拿到 480P 而不是报错
        self.assertEqual(bili.pick_quality([32, 16], 120), 32)

    def test_falls_back_upwards_when_request_is_too_low(self) -> None:
        # 只有 1080P 可用而用户要 360P 时，给 1080P 比直接失败更好用
        self.assertEqual(bili.pick_quality([80], 16), 80)

    def test_unknown_and_empty_input_is_tolerated(self) -> None:
        self.assertEqual(bili.pick_quality([], 80), 80)
        self.assertEqual(bili.pick_quality([9999], 80), 80)


class StreamSelectionTests(unittest.TestCase):
    def dash(self, qualities=(32, 16), codecs=("avc1.64001E", "hev1.1.6.L120.90")) -> dict:
        video = []
        for quality in qualities:
            for index, codec in enumerate(codecs):
                video.append(
                    {
                        "id": quality,
                        "baseUrl": f"https://cdn.example/{quality}-{index}.m4s",
                        "codecs": codec,
                        "bandwidth": 1000 + index,
                        "mimeType": "video/mp4",
                    }
                )
        return {
            "dash": {
                "video": video,
                "audio": [
                    {"id": 30216, "baseUrl": "https://cdn.example/a64.m4s", "codecs": "mp4a.40.2", "bandwidth": 10},
                    {"id": 30280, "baseUrl": "https://cdn.example/a192.m4s", "codecs": "mp4a.40.2", "bandwidth": 30},
                ],
            }
        }

    def test_prefers_avc_for_compatibility(self) -> None:
        video, _audio = bili.streams_from_dash(self.dash(), 80)
        self.assertEqual(len(video), 1)
        self.assertTrue(video[0].codec.startswith("avc"), video[0].codec)

    def test_picks_the_highest_audio_bandwidth(self) -> None:
        _video, audio = bili.streams_from_dash(self.dash(), 80)
        self.assertEqual(audio[0].quality, 30280)

    def test_missing_dash_returns_empty(self) -> None:
        self.assertEqual(bili.streams_from_dash({}, 80), ([], []))

    def test_durl_is_supported_as_a_single_file_stream(self) -> None:
        data = {
            "quality": 80,
            "durl": [{"url": "https://cdn.example/whole.mp4", "size": 12345}],
        }
        stream = bili.durl_stream(data, 80)
        self.assertIsNotNone(stream)
        assert stream is not None
        self.assertEqual(stream.url, "https://cdn.example/whole.mp4")
        self.assertEqual(stream.quality_name, "1080P")
        self.assertEqual(bili.durl_stream({}, 80), None)


class CookieTests(unittest.TestCase):
    def test_parses_a_pasted_cookie_header(self) -> None:
        cookies = bili.parse_cookie_header("SESSDATA=a%2Cb; bili_jct=xyz; DedeUserID=42; buvid3=q")
        self.assertEqual(cookies.sessdata, "a%2Cb")
        self.assertEqual(cookies.bili_jct, "xyz")
        self.assertEqual(cookies.dedeuserid, "42")
        self.assertTrue(cookies.logged_in)
        self.assertIn("SESSDATA=a%2Cb", cookies.as_header())

    def test_rejects_a_cookie_without_sessdata(self) -> None:
        with self.assertRaises(bili.BiliError):
            bili.parse_cookie_header("bili_jct=xyz")

    def test_parses_set_cookie_from_the_qr_poll_response(self) -> None:
        header = (
            "SESSDATA=abc%2Cdef; Path=/; Domain=.bilibili.com; HttpOnly; "
            "Expires=Wed, 09 Jun 2027 10:18:14 GMT, "
            "bili_jct=token; Path=/; Domain=.bilibili.com, "
            "DedeUserID=12345; Path=/"
        )
        cookies = bili.parse_set_cookie(header)
        self.assertEqual(cookies.sessdata, "abc%2Cdef")
        self.assertEqual(cookies.bili_jct, "token")
        self.assertEqual(cookies.dedeuserid, "12345")

    def test_save_and_load_round_trip_uses_0600(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cookie.json"
            cookies = bili.Cookies(sessdata="s", bili_jct="j", dedeuserid="1", buvid3="b")
            bili.save_cookies(path, cookies)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            loaded = bili.load_cookies(path)
            self.assertEqual(loaded.sessdata, "s")
            self.assertEqual(loaded.bili_jct, "j")
            self.assertTrue(bili.clear_cookies(path))
            self.assertFalse(bili.clear_cookies(path))
            self.assertEqual(bili.load_cookies(path).sessdata, "")

    def test_quality_cap_reflects_the_account(self) -> None:
        self.assertIn("480P", bili.quality_cap(bili.Account(logged_in=False)))
        self.assertIn("1080P", bili.quality_cap(bili.Account(logged_in=True)))
        self.assertIn("4K", bili.quality_cap(bili.Account(logged_in=True, vip=True)))


class ArgParsingTests(unittest.TestCase):
    def parse(self, argv: list[str]):
        return main_module.parse_args(argv)

    def test_quality_words_and_defaults(self) -> None:
        args = self.parse(["BV1xx411c7mD"])
        self.assertEqual(args.ref, "BV1xx411c7mD")
        self.assertEqual(args.page, 1)
        self.assertEqual(args.quality, 80)
        self.assertFalse(args.audio_only)

        for word in ("4k", "1080p", "720p"):
            with self.subTest(word=word):
                parsed = self.parse(["BV1xx411c7mD", word])
                self.assertEqual(parsed.quality, bili.QUALITY_ALIASES[word])
                self.assertEqual(parsed.quality_text, word)

    def test_page_and_audio_only_flags(self) -> None:
        args = self.parse(["-p", "2", "--audio-only", "BV1xx411c7mD"])
        self.assertEqual(args.page, 2)
        self.assertTrue(args.audio_only)

    def test_rejects_bad_input(self) -> None:
        with self.assertRaises(bili.BiliError):
            self.parse(["-p", "x", "BV1xx411c7mD"])
        with self.assertRaises(bili.BiliError):
            self.parse(["--nope", "BV1xx411c7mD"])
        with self.assertRaises(bili.BiliError):
            self.parse(["-p", "2"])

    def test_help_returns_none(self) -> None:
        self.assertIsNone(self.parse(["--help"]))

    def test_sanitize_strips_path_separators_and_control_characters(self) -> None:
        self.assertNotIn("/", main_module.sanitize("a/b:c*d?e\"f<g>h|i"))
        self.assertEqual(main_module.sanitize("   "), "bilibili")
        self.assertEqual(main_module.sanitize("x" * 200), "x" * 80)


class ManifestTests(unittest.TestCase):
    def test_bundled_manifest_declares_the_bili_command(self) -> None:
        import json

        payload = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["command"], "bili")
        self.assertEqual(payload["exec"], ["python3", "main.py"])
        self.assertTrue((EXTENSION / "main.py").is_file())
        self.assertTrue((EXTENSION / "bili.py").is_file())


if __name__ == "__main__":
    unittest.main()


class ShortLinkTests(unittest.TestCase):
    """b23.tv 短链里没有 BV 号，必须先跟随跳转。"""

    def test_recognizes_short_link_hosts(self) -> None:
        for raw in (
            "https://b23.tv/dfBSIGr",
            "http://b23.tv/xyz",
            "https://bili2233.cn/abc",
        ):
            with self.subTest(raw=raw):
                self.assertTrue(bili.is_short_link(raw))

    def test_does_not_treat_normal_urls_or_bare_ids_as_short_links(self) -> None:
        for raw in (
            "https://www.bilibili.com/video/BV1xx411c7mD",
            "https://evil.example/b23.tv/x",
            "BV1xx411c7mD",
            "https://b23.tv.evil.example/x",
        ):
            with self.subTest(raw=raw):
                self.assertFalse(bili.is_short_link(raw))

    def test_short_link_is_resolved_to_a_bv_id(self) -> None:
        """真网络调用；失败只跳过，不让离线环境挂掉整个测试。"""
        try:
            resolved = bili.resolve_short_link("https://b23.tv/dfBSIGr")
        except bili.BiliError as exc:
            self.skipTest(f"network unavailable or short link changed: {exc}")
        self.assertIn("bilibili.com", resolved)
        ref = bili.parse_ref(resolved)
        self.assertEqual(ref.kind, "video")
        self.assertTrue(ref.bvid.startswith("BV"))

    def test_page_number_from_the_redirect_query_is_honored(self) -> None:
        query = "https://www.bilibili.com/video/BV1xx411c7mD?p=3&share_source=COPY"
        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(query).query)
        self.assertEqual(parsed.get("p"), ["3"])
        self.assertEqual(bili.parse_ref(query, page=3).page, 3)


class MultiUrlTests(unittest.TestCase):
    """主 CDN 实测会中途静默截断，必须保留备用地址。"""

    def test_collects_base_and_backup_urls_in_order(self) -> None:
        item = {
            "baseUrl": "https://cdn.example/base.m4s",
            "backupUrl": ["https://cdn.example/backup0.m4s", "https://cdn.example/backup1.m4s"],
        }
        self.assertEqual(
            bili.stream_urls(item),
            [
                "https://cdn.example/base.m4s",
                "https://cdn.example/backup0.m4s",
                "https://cdn.example/backup1.m4s",
            ],
        )

    def test_supports_snake_case_fields_and_durl_url(self) -> None:
        self.assertEqual(
            bili.stream_urls({"base_url": "https://a", "backup_url": ["https://b"]}),
            ["https://a", "https://b"],
        )
        self.assertEqual(bili.stream_urls({"url": "https://d"}), ["https://d"])

    def test_empty_when_no_url_present(self) -> None:
        self.assertEqual(bili.stream_urls({}), [])

    def test_stream_all_urls_includes_primary_first(self) -> None:
        stream = bili.Stream(
            url="https://primary",
            quality=32,
            codec="avc1",
            bandwidth=1,
            mime="video/mp4",
            kind="video",
            alt_urls=["https://backup"],
        )
        self.assertEqual(stream.all_urls(), ["https://primary", "https://backup"])

    def test_dash_selection_keeps_backup_urls(self) -> None:
        data = {
            "dash": {
                "video": [
                    {
                        "id": 32,
                        "baseUrl": "https://primary",
                        "backupUrl": ["https://backup"],
                        "codecs": "avc1.64001F",
                        "bandwidth": 100,
                    }
                ],
                "audio": [
                    {
                        "id": 30280,
                        "baseUrl": "https://aprimary",
                        "backupUrl": ["https://abackup"],
                        "codecs": "mp4a.40.2",
                        "bandwidth": 50,
                    }
                ],
            }
        }
        video, audio = bili.streams_from_dash(data, 32)
        self.assertEqual(video[0].all_urls(), ["https://primary", "https://backup"])
        self.assertEqual(audio[0].all_urls(), ["https://aprimary", "https://abackup"])

    def test_durl_stream_keeps_backup_urls_and_real_size(self) -> None:
        data = {
            "quality": 64,
            "durl": [
                {
                    "url": "https://primary.mp4",
                    "backup_url": ["https://backup.mp4"],
                    "size": 48490687,
                }
            ],
        }
        stream = bili.durl_stream(data, 64)
        assert stream is not None
        self.assertEqual(stream.known_bytes, 48490687)
        self.assertEqual(stream.all_urls(), ["https://primary.mp4", "https://backup.mp4"])


class PlayurlDurlTests(unittest.TestCase):
    def test_accept_quality_is_documented_as_availability_not_permission(self) -> None:
        """accept_quality 列的是视频有哪些档；实际下发看 data.quality。

        未登录时 accept_quality 可能是 [126,120,116,80,64,32,16]，但
        data.quality 恒为 32/16。这个差异是用户报"分辨率不对"的根因，
        所以在这里钉住。
        """
        data = {"quality": 32, "accept_quality": [120, 80, 64, 32, 16]}
        videos, _audios = bili.streams_from_dash(
            {
                "dash": {
                    "video": [
                        {
                            "id": 32,
                            "baseUrl": "https://a",
                            "codecs": "avc1.64001F",
                            "bandwidth": 840293,
                        },
                        {
                            "id": 16,
                            "baseUrl": "https://b",
                            "codecs": "avc1.64001E",
                            "bandwidth": 610773,
                        },
                    ]
                }
            },
            want_quality=80,
        )
        self.assertEqual(videos[0].quality, 32)
        self.assertEqual(
            bili.quality_name(data["quality"]),
            videos[0].quality_name,
            "delivered quality must match data.quality, not accept_quality's maximum",
        )
