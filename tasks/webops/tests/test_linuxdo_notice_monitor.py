import importlib.util
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "linuxdo_notice_monitor.py"

spec = importlib.util.spec_from_file_location("linuxdo_notice_monitor", SCRIPT_PATH)
linuxdo_notice_monitor = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = linuxdo_notice_monitor
assert spec.loader is not None
spec.loader.exec_module(linuxdo_notice_monitor)


def message_html(
    channel: str,
    message_id: int,
    text_html: str,
    *,
    posted_at: str = "2026-06-07T12:00:00+00:00",
    photo_urls: list[str] | None = None,
) -> str:
    photos = "\n".join(
        f'<a class="tgme_widget_message_photo_wrap" style="background-image:url(\\\'{url}\\\')"></a>' for url in (photo_urls or [])
    )
    return f'''
<div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="{channel}/{message_id}">
  {photos}
  <div class="tgme_widget_message_text js-message_text" dir="auto">{text_html}</div>
  <time datetime="{posted_at}"></time>
</div>
'''


class LinuxDoNoticeMonitorTests(unittest.TestCase):
    def make_post(
        self,
        *,
        title: str,
        summary: str = "",
        section: str = "#搞七捻三",
        author: str = "#tester",
        channel: str = "LinuxDoNew",
        message_id: int = 1,
        topic_id: str = "2322593",
        topic_post_no: str = "",
    ):
        return linuxdo_notice_monitor.LinuxDoPost(
            channel=channel,
            message_id=message_id,
            telegram_url=f"https://t.me/{channel}/{message_id}",
            posted_at="2026-06-07T12:00:00+00:00",
            topic_id=topic_id,
            topic_url=f"https://linux.do/t/topic/{topic_id}" + (f"/{topic_post_no}" if topic_post_no else ""),
            topic_post_no=topic_post_no,
            title=title,
            author=author,
            section=section,
            topic_created_at="2026-06-07 20:00:00",
            summary=summary,
            source_role="primary" if channel == "LinuxDoNew" else "supplement",
        )

    def test_parse_primary_channel_structured_post(self):
        html_text = message_html(
            "LinuxDoNew",
            445375,
            """
标题: RawChat codex公益站火力全开活动，从现在开始到明天早上8点，所有人不限额度，不限并发数量，不限速！<br>
作者: #RawChat<br>
板块: #福利羊毛<br>
编号: 2322593<br>
帖子: <a href="https://linux.do/t/topic/2322593">https://linux.do/t/topic/2322593</a><br>
时间: 2026-06-07 18:30:00<br>
摘要: RawChat codex公益站火力全开限时活动，活动时间：2026.6.7 18:30 - 2026.6.8 8:00，网址：https://new.sharedchat.cc/
""",
        )
        posts = linuxdo_notice_monitor.parse_channel_posts(html_text, "LinuxDoNew", "primary")
        self.assertEqual(len(posts), 1)
        post = posts[0]
        self.assertEqual(post.message_id, 445375)
        self.assertEqual(post.topic_id, "2322593")
        self.assertEqual(post.title, "RawChat codex公益站火力全开活动，从现在开始到明天早上8点，所有人不限额度，不限并发数量，不限速！")
        self.assertEqual(post.author, "#RawChat")
        self.assertEqual(post.section, "#福利羊毛")
        self.assertIn("new.sharedchat.cc", post.summary)
        self.assertEqual(post.source_role, "primary")

    def test_parse_supplement_channel_unstructured_post_and_reply_number(self):
        html_text = message_html(
            "linux_do_channel",
            406812,
            '''
欣欣|林可欣 ( <a href="https://t.me/StellaFortuna">@StellaFortuna</a> ) 在 议题:虫洞投票需要反对票吗 中发帖
你或许觉得更合适的议题应该是……
<a href="https://linux.do/t/topic/2322947/4">原帖</a>
''',
        )
        posts = linuxdo_notice_monitor.parse_channel_posts(html_text, "linux_do_channel", "supplement")
        self.assertEqual(len(posts), 1)
        post = posts[0]
        self.assertEqual(post.channel, "linux_do_channel")
        self.assertEqual(post.topic_id, "2322947")
        self.assertEqual(post.topic_post_no, "4")
        self.assertEqual(post.title, "议题:虫洞投票需要反对票吗")
        self.assertTrue(post.is_reply)

    def test_deduplicate_prefers_primary_structured_post(self):
        primary = self.make_post(title="主源标题", channel="LinuxDoNew", message_id=20, topic_id="100")
        supplement = self.make_post(title="补充源标题", channel="linux_do_channel", message_id=30, topic_id="100", topic_post_no="1")
        deduped = linuxdo_notice_monitor.dedupe_posts([supplement, primary])
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0].title, "主源标题")
        self.assertEqual(deduped[0].source_role, "primary")

    def test_public_site_activity_reaches_model_before_push(self):
        post = self.make_post(
            title="RawChat codex公益站火力全开活动，从现在开始到明天早上8点，所有人不限额度，不限速",
            summary="活动时间：2026.6.7 18:30 - 2026.6.8 8:00，codex公益站网址：https://new.sharedchat.cc/",
            section="#开发调优",
            author="#RawChat",
        )
        decision = linuxdo_notice_monitor.evaluate_post(post)
        self.assertEqual(decision.route, "candidate")
        self.assertIn("public_site_activity", decision.matched_categories)

    def test_priority_author_reaches_model_candidate(self):
        config = linuxdo_notice_monitor.default_config()
        config["priority_authors"] = ["#RawChat"]
        post = self.make_post(title="普通技术分享", summary="没有明确公益站活动，但作者值得关注", author="#RawChat")
        decision = linuxdo_notice_monitor.evaluate_post(post, config=config)
        self.assertEqual(decision.route, "candidate")
        self.assertIn("priority_author", decision.matched_categories)

    def test_generic_help_question_is_silent(self):
        post = self.make_post(
            title="ccswitch cli 提问",
            summary="windows想要在wsl中使用codex的中转，但是已经配置了，请问有什么解决办法嘛？",
            section="#搞七捻三",
        )
        decision = linuxdo_notice_monitor.evaluate_post(post)
        self.assertEqual(decision.route, "silent")
        self.assertIn("generic_help_question", decision.matched_categories)

    def test_http_proxy_troubleshooting_is_not_public_site_activity(self):
        post = self.make_post(
            title="stream disconnected before completion: stream closed before response.completed",
            summary="codex桌面端+ccswitch，换了中转站、公益站都会遇到。已经尝试在 ~/.codex/.env 里面加入 http_proxy，更换代理节点。",
            section="#开发调优",
        )
        decision = linuxdo_notice_monitor.evaluate_post(post)
        self.assertNotEqual(decision.route, "push")
        self.assertNotIn("public_site_activity", decision.matched_categories)

    def test_github_timeline_chat_is_silent(self):
        post = self.make_post(
            title="大家来晒晒github时间线",
            summary="",
            section="#搞七捻三",
        )
        decision = linuxdo_notice_monitor.evaluate_post(post)
        self.assertEqual(decision.route, "silent")

    def test_linuxdo_bookmark_question_is_silent(self):
        post = self.make_post(
            title="Linux Do 加书签有数量限制吗",
            summary="我在刷开源板块，看到不错的就加个书签，加了一会就一直提示报错了。",
            section="#搞七捻三",
        )
        decision = linuxdo_notice_monitor.evaluate_post(post)
        self.assertEqual(decision.route, "silent")

    def test_openai_subscription_question_is_silent(self):
        post = self.make_post(
            title="关于的OpenAI订阅问题",
            summary="无visa卡，无电话卡，想问安卓上有没有方便开通gpt订阅的方式，或者买成品号和找代充封号风险大吗",
            section="#搞七捻三",
        )
        decision = linuxdo_notice_monitor.evaluate_post(post)
        self.assertEqual(decision.route, "silent")

    def test_public_site_availability_chatter_2327693_is_silent(self):
        post = self.make_post(
            title="今天的公益站没一个能顺畅蹬20分钟的",
            summary="基本上十几分钟就挂了，有些几分钟就挂了，额度都很多，根本用不出去",
            section="#搞七捻三",
            topic_id="2327693",
        )
        topic_evidence = {
            "topic_text": "基本上十几分钟就挂了，有些几分钟就挂了，额度都很多，根本用不出去",
            "topic_category": "搞七捻三",
            "topic_tags": ["纯水", "人工智能", "ChatGPT"],
            "topic_metrics": {"views": 626, "like_count": 2},
            "resource_url": "",
            "access_method": "",
            "frontline_model_verdict": "unknown",
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")
        self.assertIn("public_site_chatter_low_value", audit[0]["post"]["routing_context"]["matched_categories"])

    def test_public_site_403_chatter_2327866_is_silent(self):
        post = self.make_post(
            title="今天的codex公益站都是显示403报错",
            summary="基本上都是报403，有些官网领取后都是显示空白，额度变60了",
            section="#搞七捻三",
            topic_id="2327866",
        )
        topic_evidence = {
            "topic_text": "基本上都是报403，有些官网领取后都是显示空白，额度变60了",
            "topic_category": "搞七捻三",
            "topic_tags": ["纯水", "人工智能", "ChatGPT"],
            "resource_url": "",
            "access_method": "",
            "frontline_model_verdict": "unknown",
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")
        self.assertIn("public_site_chatter_low_value", audit[0]["post"]["routing_context"]["matched_categories"])

    def test_public_site_shutdown_2423599_is_silent_without_actionable_resource(self):
        post = self.make_post(
            title="公益站开的太仓促了，反思一下",
            summary="公益站已经关闭，已注册用户补了一些额度，后续可能维护现有模型",
            section="#搞七捻三",
            topic_id="2423599",
        )
        topic_evidence = {
            "topic_text": "公益站已经关闭，已注册用户补了一些额度，后续可能维护现有模型。没有新的 Base URL、迁移地址或兑换码。",
            "topic_category": "搞七捻三",
            "topic_tags": ["纯水", "人工智能"],
            "topic_metrics": {"views": 800, "like_count": 4},
            "resource_url": "",
            "access_method": "",
            "frontline_model_verdict": "unknown",
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")
        categories = audit[0]["post"]["routing_context"]["matched_categories"]
        self.assertIn("public_site_shutdown_low_value", categories)
        profile = audit[0]["post"]["evidence_facts"]["post_value_profile"]
        self.assertEqual(profile["domain"], "public_site")
        self.assertEqual(profile["actionability"], "negative_terminal")
        self.assertEqual(profile["value_tier"], "silent")
        self.assertTrue(profile["loss_only"])

    def test_public_site_shutdown_with_replacement_url_is_not_hard_silent(self):
        post = self.make_post(
            title="旧公益站关闭，迁移到新 Base URL",
            summary="旧站关闭，但新地址 https://new.example/v1 已开放，key填任意值，支持 GPT 和 Claude",
            section="#福利羊毛",
            topic_id="2423600",
        )
        topic_evidence = {
            "topic_text": "旧公益站关闭，但迁移到新 Base URL https://new.example/v1，key填任意值，支持 GPT 和 Claude，不限额度。",
            "resource_url": "https://new.example/v1",
            "access_method": "key填任意值",
            "model_names": ["GPT", "Claude"],
            "frontline_model_verdict": "frontline",
            "model_coverage": {
                "present_top_families": ["gpt", "claude"],
                "present_non_frontline_families": [],
                "domestic_only": False,
                "top_model_absent_explicit": False,
                "confidence": "explicit",
            },
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(len(push_messages), 1)
        self.assertEqual(audit[0]["decision"], "push")
        categories = audit[0]["post"]["routing_context"]["matched_categories"]
        self.assertNotIn("public_site_shutdown_low_value", categories)
        profile = audit[0]["post"]["evidence_facts"]["post_value_profile"]
        self.assertIn(profile["actionability"], {"direct_use", "replacement_available"})
        self.assertTrue(profile["resource_available"])

    def test_research_academic_content_2327036_is_silent(self):
        post = self.make_post(
            title="分享一个科研论文 PDF 和实验报告整理工具",
            summary="科研方向，包含论文阅读、学术报告、数据集整理和 PDF 文献管理，对科研绘图也有帮助",
            section="#资源荟萃",
            topic_id="2327036",
            topic_post_no="1",
        )
        topic_evidence = {
            "topic_text": "科研论文 PDF、学术报告、实验数据集和文献整理工具资源，适合做 research workflow。",
            "topic_category": "资源荟萃",
            "topic_tags": ["科研", "论文", "PDF"],
            "resource_url": "https://example.com/research-paper-tool",
            "reference_url": "",
            "link_titles": ["Research paper toolkit"],
            "frontline_model_verdict": "unknown",
            "why": "发现外部资源",
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")
        self.assertIn("research_academic_low_value", audit[0]["post"]["routing_context"]["matched_categories"])

    def test_actionable_public_site_recovery_is_not_chatter_silent(self):
        post = self.make_post(
            title="RawChat codex公益站恢复并扩容",
            summary="站长公告：已恢复，新增 Base URL: https://new.sharedchat.cc/v1，key填任意值，GPT-5.4/Codex 可用，不限速",
            section="#福利羊毛",
            author="#RawChat",
            topic_id="2327867",
        )
        topic_evidence = {
            "topic_text": "站长公告：RawChat codex公益站已恢复并扩容，Base URL: https://new.sharedchat.cc/v1，key填任意值，支持 GPT-5.4/Codex，不限速。",
            "topic_category": "福利羊毛",
            "topic_tags": ["福利羊毛", "人工智能"],
            "resource_url": "https://new.sharedchat.cc/v1",
            "access_method": "key填任意值",
            "model_names": ["GPT-5.4", "Codex"],
            "frontline_model_verdict": "frontline",
            "limits": "不限速",
            "why": "公益站恢复扩容且给出 Base URL",
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        categories = audit[0]["post"]["routing_context"]["matched_categories"]
        self.assertNotIn("public_site_chatter_low_value", categories)
        self.assertNotEqual(audit[0]["decision"], "silent")

    def test_research_term_in_codex_developer_project_is_not_research_silent(self):
        post = self.make_post(
            title="开源一个 arXiv MCP，给 Codex/Claude 做论文检索",
            summary="GitHub repo，提供 MCP 和 API，让 Codex/Claude 读取论文摘要后辅助代码和引用管理",
            section="#开发调优",
            topic_id="2327037",
        )
        topic_evidence = {
            "topic_text": "开源 arXiv MCP，GitHub 仓库，给 Codex/Claude/GPT 接入论文检索 API。",
            "topic_category": "开发调优",
            "topic_tags": ["科研", "开源"],
            "resource_url": "https://github.com/example/arxiv-mcp",
            "provider": "github.com",
            "model_names": ["Codex", "Claude", "GPT"],
            "frontline_model_verdict": "frontline",
            "why": "Codex/Claude 开发工具",
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        categories = audit[0]["post"]["routing_context"]["matched_categories"]
        self.assertNotIn("research_academic_low_value", categories)
        self.assertEqual(audit[0]["decision"], "silent")
        self.assertIn("insufficient_actionable_evidence", categories)

    def test_generic_invite_code_request_is_silent(self):
        post = self.make_post(
            title="有没有佬可以给一个SAVO的邀请码",
            summary="要有uid和邀请码，我注册的时候没看提示，客服说事后绑定要这俩",
            section="#搞七捻三",
        )
        decision = linuxdo_notice_monitor.evaluate_post(post)
        self.assertEqual(decision.route, "silent")

    def test_supplement_reply_is_silent_without_high_value_signal(self):
        post = self.make_post(
            title="议题:虫洞投票需要反对票吗",
            summary="只是旧帖下的新回复",
            channel="linux_do_channel",
            topic_post_no="4",
        )
        decision = linuxdo_notice_monitor.evaluate_post(post)
        self.assertEqual(decision.route, "silent")
        self.assertIn("supplement_reply", decision.matched_categories)


    def test_format_push_message(self):
        post = self.make_post(
            title="RawChat codex公益站火力全开活动",
            summary="不限额度，不限速，网址 https://new.sharedchat.cc/",
            section="#福利羊毛",
        )
        evidence = {
            "why": "Codex 公益站限时火力全开",
            "model_names": ["Codex", "GPT-5.4"],
            "resource_url": "https://new.sharedchat.cc/",
            "limits": "不限额度，不限速",
            "risk_notes": "",
            "frontline_model_verdict": "frontline",
        }
        message = linuxdo_notice_monitor.format_push_message(post, ["public_site_activity"], evidence)
        self.assertIn("[RawChat codex公益站火力全开活动](https://linux.do/t/topic/2322593)", message)
        self.assertIn("#福利羊毛", message)
        self.assertIn("Codex 公益站限时火力全开", message)
        self.assertIn("不限额度，不限速", message)

    def test_parse_channel_post_extracts_telegram_photo_urls(self):
        html_text = message_html(
            "LinuxDoNew",
            445450,
            """
标题: 逆了一下某瑞士大厂的模型，给需要的佬<br>
作者: #纵横<br>
板块: #福利羊毛<br>
编号: 2322931<br>
帖子: <a href="https://linux.do/t/topic/2322931">https://linux.do/t/topic/2322931</a><br>
摘要: 做成workers了，key填任意值
""",
            photo_urls=["https://cdn4.telesco.pe/file/example.jpg"],
        )
        posts = linuxdo_notice_monitor.parse_channel_posts(html_text, "LinuxDoNew", "primary")
        self.assertEqual(posts[0].photo_urls, ["https://cdn4.telesco.pe/file/example.jpg"])

    def test_parse_topic_json_extracts_first_post_links_and_images(self):
        topic = {
            "title": "逆了一下某瑞士大厂的模型，给需要的佬",
            "category_id": 36,
            "views": 507,
            "like_count": 8,
            "image_url": "https://cdn3.ldstatic.com/original/4X/e/e/8/image.png",
            "post_stream": {
                "posts": [
                    {
                        "post_number": 1,
                        "cooked": '<p>rt，做成workers了<br>地址是:<a href="https://lumo2api.example">https://lumo2api.example</a><br>key填任意值<br><img src="https://cdn3.ldstatic.com/original/4X/e/e/8/image.png" width="981" height="102"></p>',
                        "link_counts": [{"url": "https://lumo2api.example", "title": "Lumo2API — Proton AI Gateway", "clicks": 45}],
                        "reads": 510,
                        "score": 218.2,
                    }
                ]
            },
            "thumbnails": [{"url": "https://cdn3.ldstatic.com/optimized/4X/e/e/8/image_2_400x41.png", "width": 400, "height": 41}],
        }
        evidence = linuxdo_notice_monitor.parse_topic_json(topic)
        self.assertIn("key填任意值", evidence["topic_text"])
        self.assertEqual(evidence["resource_url"], "https://lumo2api.example")
        self.assertEqual(evidence["link_titles"], ["Lumo2API — Proton AI Gateway"])
        self.assertIn("https://cdn3.ldstatic.com/original/4X/e/e/8/image.png", evidence["image_urls"])
        self.assertEqual(evidence["image_dimensions"][0], {"url": "https://cdn3.ldstatic.com/original/4X/e/e/8/image.png", "width": 981, "height": 102})

    def test_fetch_json_falls_back_to_curl_for_linuxdo_403(self):
        err = urllib.error.HTTPError(
            "https://linux.do/t/topic/2322931.json",
            403,
            "Forbidden",
            {},
            io.BytesIO(b"forbidden"),
        )
        with mock.patch.object(linuxdo_notice_monitor, "fetch", side_effect=err), \
            mock.patch.object(linuxdo_notice_monitor, "fetch_with_curl", return_value='{"ok": true}'):
            data = linuxdo_notice_monitor.fetch_json("https://linux.do/t/topic/2322931.json", phase="topic_json")
        self.assertEqual(data, {"ok": True})


    def test_non_frontline_model_resource_is_silent_after_enrichment(self):
        post = self.make_post(
            title="逆了一下某瑞士大厂的模型，给需要的佬",
            summary="rt，做成workers了，地址是 https://lumo2api.example ，key填任意值",
            section="#福利羊毛",
            author="#纵横",
            topic_id="2322931",
        )
        evidence = {
            "topic_text": "rt，做成workers了，地址是 https://lumo2api.example key填任意值",
            "resource_url": "https://lumo2api.example",
            "access_method": "key填任意值",
            "model_names": ["Proton Lumo", "Qwen", "Kimi", "Ernie 4.5 VL"],
            "provider": "Proton AI Gateway",
            "limits": "",
            "risk_notes": "图片说明这是 Lumo model router，会路由到 Qwen/Kimi/Ernie 等非一线模型",
            "frontline_model_verdict": "non_frontline",
            "why": "非一线模型资源",
        }
        decision = linuxdo_notice_monitor.apply_enriched_gate(post, linuxdo_notice_monitor.evaluate_post(post), evidence, linuxdo_notice_monitor.default_config())
        self.assertEqual(decision.route, "silent")
        self.assertIn("non_frontline_model", decision.matched_categories)


    def test_domestic_only_welfare_public_site_is_silent(self):
        post = self.make_post(
            title="一个新的 AI 公益站",
            summary="公益站，不限额度，key填任意值，给需要的佬",
            section="#福利羊毛",
            topic_id="2325001",
        )
        topic_evidence = {
            "topic_text": "新的公益中转站，仅支持国产模型：DeepSeek、Qwen、Kimi、GLM、豆包、文心。Base URL: https://domestic-only.example/v1，key填任意值。",
            "resource_url": "https://domestic-only.example/v1",
            "access_method": "key填任意值",
            "model_names": ["DeepSeek", "Qwen", "Kimi", "GLM", "豆包", "文心"],
            "frontline_model_verdict": "non_frontline",
            "limits": "不限额度",
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")
        categories = audit[0]["post"]["routing_context"]["matched_categories"]
        self.assertTrue({"domestic_only_model", "non_frontline_model"} & set(categories))


    def test_model_coverage_mixed_top_tier_wins(self):
        evidence = linuxdo_notice_monitor.build_evidence_from_text(
            "公益站模型列表：GPT-5.4、Claude Sonnet、Qwen、Kimi，Base URL: https://mixed.example/v1，key填任意值。",
            linuxdo_notice_monitor.default_config(),
            resource_url="https://mixed.example/v1",
        )
        self.assertEqual(evidence["frontline_model_verdict"], "frontline")
        coverage = evidence["model_coverage"]
        self.assertIn("gpt", coverage["present_top_families"])
        self.assertIn("claude", coverage["present_top_families"])
        self.assertIn("qwen", coverage["present_non_frontline_families"])
        self.assertFalse(coverage["domestic_only"])

    def test_openai_compatible_without_concrete_top_model_is_unknown(self):
        evidence = linuxdo_notice_monitor.build_evidence_from_text(
            "公益中转站提供 OpenAI-compatible 接口，Base URL: https://compat.example/v1，key填任意值，暂未列模型。",
            linuxdo_notice_monitor.default_config(),
            resource_url="https://compat.example/v1",
        )
        self.assertEqual(evidence["frontline_model_verdict"], "unknown")
        coverage = evidence["model_coverage"]
        self.assertEqual(coverage["present_top_families"], [])
        self.assertEqual(coverage["confidence"], "unknown")

    def test_mixed_top_and_domestic_models_are_not_silenced(self):
        post = self.make_post(
            title="公益站支持 GPT 和国产模型",
            summary="公益站，key填任意值，同时有 GPT 和 Qwen/Kimi",
            section="#福利羊毛",
            topic_id="2325003",
        )
        evidence = {
            "topic_text": "公益站模型列表：GPT-5.4、Claude Sonnet、Qwen、Kimi。Base URL: https://mixed.example/v1，key填任意值。",
            "resource_url": "https://mixed.example/v1",
            "access_method": "key填任意值",
            "model_names": ["GPT-5.4", "Claude Sonnet", "Qwen", "Kimi"],
            "limits": "",
            "frontline_model_verdict": "unknown",
            "why": "同时支持顶级模型和国产模型",
        }
        decision = linuxdo_notice_monitor.apply_enriched_gate(
            post,
            linuxdo_notice_monitor.evaluate_post(post),
            evidence,
            linuxdo_notice_monitor.default_config(),
        )
        self.assertNotEqual(decision.route, "silent")
        self.assertNotIn("non_frontline_model", decision.matched_categories)

    def test_unknown_model_ai_public_site_goes_to_model_not_welfare_push(self):
        post = self.make_post(
            title="一个新的 AI 公益站",
            summary="公益站，不限额度，Base URL: https://unknown-ai.example/v1，key填任意值",
            section="#福利羊毛",
            topic_id="2325004",
        )
        topic_evidence = {
            "topic_text": "AI 公益中转站开放，Base URL: https://unknown-ai.example/v1，key填任意值，暂未列模型。",
            "resource_url": "https://unknown-ai.example/v1",
            "access_method": "key填任意值",
            "model_names": [],
            "frontline_model_verdict": "unknown",
            "limits": "不限额度",
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")
        evidence_facts = audit[0]["post"]["evidence_facts"]
        self.assertIn("model_coverage", evidence_facts)
        self.assertEqual(evidence_facts["model_coverage"]["confidence"], "unknown")

    def test_feedback_qianwen_37_plus_2413607_is_silent(self):
        post = self.make_post(
            title="千问3.7 plus",
            summary="",
            section="#福利羊毛",
            topic_id="2413607",
        )
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value={}):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")
        categories = audit[0]["post"]["routing_context"]["matched_categories"]
        self.assertIn("domestic_or_non_top_model_welfare", categories)
        self.assertIn("non_frontline_model", categories)

    def test_domestic_model_family_aliases_are_normalised(self):
        cases = [
            ("千问3.7 plus", "qwen"),
            ("通义千问 QwQ 模型体验卡", "qwen"),
            ("Moonshot Kimi K2 key 分享", "kimi"),
            ("智谱清言 GLM Coding Plan", "glm"),
            ("豆包 Seed API 额度", "doubao"),
            ("百度千帆 文心一言 API", "ernie"),
            ("腾讯混元 key", "hunyuan"),
            ("MiniMax 海螺 token plan", "minimax"),
            ("科大讯飞星火 API", "xunfei"),
            ("零一万物 Yi-Coder 套餐", "yi"),
            ("百川 Baichuan2 体验", "baichuan"),
            ("阶跃星辰 StepFun key", "stepfun"),
            ("商汤日日新 SenseNova 公测", "sensenova"),
            ("InternLM 书生浦语 API", "internlm"),
            ("天工 Skywork 模型额度", "skywork"),
            ("华为盘古 PanguLM API", "pangu"),
            ("硅基流动 SiliconFlow 余额", "siliconflow"),
            ("魔搭 ModelScope API-Inference", "modelscope"),
        ]
        for text, family in cases:
            with self.subTest(text=text):
                evidence = linuxdo_notice_monitor.build_evidence_from_text(
                    f"{text}，#福利羊毛，免费领取，key 填任意值",
                    linuxdo_notice_monitor.default_config(),
                )
                self.assertIn(family, evidence["model_coverage"]["present_non_frontline_families"])
                self.assertEqual(evidence["frontline_model_verdict"], "non_frontline")

    def test_loaded_config_contains_qianwen_domestic_family_alias(self):
        config = linuxdo_notice_monitor.load_config()
        families = config.get("domestic_model_families", {})
        self.assertIn("qwen", families)
        self.assertIn("千问", families["qwen"])
        evidence = linuxdo_notice_monitor.build_evidence_from_text(
            "千问3.7 plus，#福利羊毛，免费领取，key 填任意值",
            config,
        )
        self.assertIn("qwen", evidence["model_coverage"]["present_non_frontline_families"])
        self.assertEqual(evidence["frontline_model_verdict"], "non_frontline")

    def test_feedback_domestic_model_resource_offers_are_silent(self):
        cases = [
            ("千问3.7 plus", "", "#福利羊毛"),
            ("送3张GLM Coding Plan 7天体验卡", "免费送 GLM Coding Plan 体验卡，先到先得", "#福利羊毛"),
            ("智谱GLM Coding Plan体验卡三张", "新人分享智谱 GLM Coding Plan 体验卡", "#福利羊毛"),
            ("小米mimo分享给大家！", "MIMO Pro 套餐分享，token-plan-cn.xiaomimimo.com/v1", "#福利羊毛"),
            ("【庆祝升LV2】MIMOPro套餐已使用14%", "", "#福利羊毛"),
            ("分享一个 MIMO Pro账户 6月27到期", "MIMO Pro 账户还有额度，6月27到期", "#福利羊毛"),
            ("qoder每天200的qwen3.7max,国内外账号都适用", "qoder 账号送 qwen3.7max 每天 200 次额度", "#福利羊毛"),
            ("【小喵喵TTS公益】在线免费使用Mimo-TTS公益站点", "Mimo-TTS 公益站，声音克隆，文字转语音，每日点数限制", "#福利羊毛"),
            ("opencode go套餐 用不完了 还有三天", "opencode go 套餐剩余额度分享，三天后到期", "#福利羊毛"),
            ("分享一个mimo pro订阅", "MIMO Pro 订阅套餐还有额度，给需要的佬", ""),
            ("【分享】新人分享小米Mimo Max月度订阅套餐，6.26过期", "token-plan-cn.xiaomimimo.com/anthropic", "#资源荟萃"),
        ]
        for index, (title, summary, section) in enumerate(cases, start=1):
            post = self.make_post(title=title, summary=summary, section=section, topic_id=f"23291{index}")
            with self.subTest(title=title), mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value={}):
                push_messages, audit = linuxdo_notice_monitor._candidate_items(
                    [post],
                    linuxdo_notice_monitor.default_config(),
                    {},
                    dry_run=True,
                )
            self.assertEqual(push_messages, [])
            self.assertEqual(audit[0]["decision"], "silent")
            categories = audit[0]["post"]["routing_context"]["matched_categories"]
            self.assertIn("domestic_or_non_top_model_welfare", categories)

    def test_feedback_token_usage_question_is_silent_even_with_gpt_pro_context(self):
        post = self.make_post(
            title="大家现在用 token 都在干什么事儿呢",
            summary="gpt pro 额度根本用不完，想问问大家都在干什么",
            section="#搞七捻三",
            author="@stupidChipMunk",
            topic_id="2470100",
        )
        topic_evidence = {
            "topic_text": "大家现在用 token 都在干什么事儿呢？gpt pro 额度根本用不完，想问问大家平时都拿 token 做什么。",
            "model_names": ["gpt pro"],
            "frontline_model_verdict": "frontline",
            "model_coverage": {
                "present_top_families": ["gpt"],
                "present_non_frontline_families": [],
                "domestic_only": False,
                "top_model_absent_explicit": False,
                "confidence": "explicit",
            },
            "limits": "额度根本用不完",
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")
        categories = audit[0]["post"]["routing_context"]["matched_categories"]
        self.assertIn("question_discussion_low_value", categories)
        self.assertNotIn("frontline_public_resource", categories)

    def test_feedback_token_usage_poll_discussion_is_silent(self):
        post = self.make_post(
            title="投票 你在用什么方式购买token",
            summary="国外官方订阅 国内官方api调用 小型中转站订阅 签到公益站 只用免费 四处薅羊毛 如果还有其他的方式提出来我再补上去",
            section="#搞七捻三",
            author="#a12908",
            topic_id="2477670",
        )
        topic_evidence = {
            "topic_text": (
                "投票 你在用什么方式购买token 国外官方订阅 国外官方api调用 国内官方订阅 国内官方api调用 "
                "拼车订阅 小型中转站订阅 小型中转站api调用 正规大中转站订阅 正规大中转站api调用 "
                "自己组号池 签到公益站 LDC购买公益站 只用免费 四处薅羊毛 Click to view the poll. "
                "如果还有其他的方式提出来我再补上去"
            ),
            "topic_tags": ["纯水", "人工智能"],
            "price": "免费",
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")
        categories = audit[0]["post"]["routing_context"]["matched_categories"]
        self.assertIn("question_discussion_low_value", categories)

    def test_feedback_unicom_coding_plan_gateway_is_domestic_silent(self):
        post = self.make_post(
            title="给大家分享一个联通codingplan 还剩一万次。速蹬！",
            summary="联通 codingplan 还剩一万次，接口 https://aigw-gzgy2.cucloud.cn:8443/v1",
            section="#福利羊毛",
            topic_id="2429101",
        )
        topic_evidence = {
            "topic_text": "给大家分享一个联通 codingplan，还剩一万次。Base URL https://aigw-gzgy2.cucloud.cn:8443/v1",
            "resource_url": "https://aigw-gzgy2.cucloud.cn:8443/v1",
            "provider": "aigw-gzgy2.cucloud.cn",
            "limits": "还剩一万次",
            "frontline_model_verdict": "unknown",
            "why": "发现可直接访问资源",
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")
        categories = audit[0]["post"]["routing_context"]["matched_categories"]
        self.assertIn("domestic_or_non_top_model_welfare", categories)
        coverage = audit[0]["post"]["evidence_facts"]["model_coverage"]
        self.assertIn("china_unicom", coverage.get("present_non_frontline_families", []))

    def test_claude_code_or_cursor_mentions_do_not_make_domestic_plan_frontline(self):
        cases = [
            (
                "火山引擎coding plan 怎么突然 9.9一个月",
                "支持 Claude Code、Cursor 等主流编程工具，模型 Doubao-Seed-2.0-pro、MiniMax-M3、DeepSeek、GLM、Kimi。",
                "#前沿快讯",
            ),
            (
                "火山方舟又出9.9的token plan了，这次加入了MiniMax-M3",
                "Lite/Pro 套餐限时优惠，模型列表为 Doubao、MiniMax、DeepSeek、GLM、Kimi，支持 Claude Code、Cursor。",
                "#前沿快讯",
            ),
        ]
        for index, (title, summary, section) in enumerate(cases, start=1):
            post = self.make_post(title=title, summary=summary, section=section, topic_id=f"23292{index}")
            with self.subTest(title=title), mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value={}):
                push_messages, audit = linuxdo_notice_monitor._candidate_items(
                    [post],
                    linuxdo_notice_monitor.default_config(),
                    {},
                    dry_run=True,
                )
            self.assertEqual(push_messages, [])
            self.assertEqual(audit[0]["decision"], "silent")
            categories = audit[0]["post"]["routing_context"]["matched_categories"]
            self.assertIn("domestic_or_non_top_model_welfare", categories)

    def test_feedback_welfare_question_posts_are_silent(self):
        cases = [
            ("bug team是不是彻底没了", "还有能用的 bug team 吗", "welfare_question_low_value"),
            ("20260610今天还有GPT的公站吗", "求一个还能用的 GPT 公站", "welfare_question_low_value"),
            ("我自己搭建的网站sub2api的，为啥总是容易提示首次访问延迟过大", "sub2api 首次访问延迟过大，怎么解决", "welfare_question_low_value"),
            ("关于土区苹果订阅gpt plus涨价的一些问题", "请问土区苹果订阅 GPT Plus 涨价后怎么处理", "welfare_question_low_value"),
        ]
        for index, (title, summary, expected_category) in enumerate(cases, start=1):
            post = self.make_post(title=title, summary=summary, section="#福利羊毛", topic_id=f"23293{index}")
            with self.subTest(title=title), mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value={}):
                push_messages, audit = linuxdo_notice_monitor._candidate_items(
                    [post],
                    linuxdo_notice_monitor.default_config(),
                    {},
                    dry_run=True,
                )
            self.assertEqual(push_messages, [])
            self.assertEqual(audit[0]["decision"], "silent")
            self.assertIn(expected_category, audit[0]["post"]["routing_context"]["matched_categories"])

    def test_feedback_non_beijing_local_and_wps_welfare_are_silent(self):
        cases = [
            ("上海地区全家免费领咖啡", "上海门店到店领取，全家咖啡免费核销", "non_beijing_local_welfare"),
            ("山西移动领流量 0元5G 6个月", "山西移动营业厅 0 元领取 5G 流量 6 个月", "non_beijing_local_welfare"),
            ("免费WPS 大会员", "免费领取 WPS 大会员权益", "user_excluded_service_welfare"),
        ]
        for index, (title, summary, expected_category) in enumerate(cases, start=1):
            post = self.make_post(title=title, summary=summary, section="#福利羊毛", topic_id=f"23294{index}")
            with self.subTest(title=title), mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value={}):
                push_messages, audit = linuxdo_notice_monitor._candidate_items(
                    [post],
                    linuxdo_notice_monitor.default_config(),
                    {},
                    dry_run=True,
                )
            self.assertEqual(push_messages, [])
            self.assertEqual(audit[0]["decision"], "silent")
            self.assertIn(expected_category, audit[0]["post"]["routing_context"]["matched_categories"])

    def test_beijing_local_welfare_is_not_silenced_by_region_gate(self):
        post = self.make_post(
            title="北京地区全家免费领咖啡",
            summary="北京门店到店领取，全家咖啡免费核销",
            section="#福利羊毛",
            topic_id="2329499",
        )
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value={}):
            _push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertNotIn("non_beijing_local_welfare", audit[0]["post"]["routing_context"]["matched_categories"])

    def test_forum_level_invite_chatter_2324951_is_silent(self):
        post = self.make_post(
            title="马上要三级了...",
            summary="但突然知道邀请码没了，导致有点落差（ 嘿呀，还差400个帖子，估计一周后就能看完吧",
            section="#搞七捻三",
            author="#StaringF",
            topic_id="2324951",
        )
        topic_evidence = {
            "topic_text": "但突然知道邀请码没了，导致有点落差（ 嘿呀，还差400个帖子，估计一周后就能看完吧 截屏2026-06-08 00.08.201776×1466 235 KB",
            "resource_url": "/uploads/short-url/1mZNRGXoftqTQQqN9o9NXZJmOWz.png?dl=1",
            "image_urls": ["https://cdn3.ldstatic.com/optimized/4X/0/9/9/screenshot_605x500.png"],
            "frontline_model_verdict": "unknown",
            "why": "发现可直接访问资源",
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")
        self.assertIn("forum_chatter_low_value", audit[0]["post"]["routing_context"]["matched_categories"])

    def test_internal_upload_image_is_not_resource_url(self):
        topic = {
            "title": "马上要三级了...",
            "post_stream": {
                "posts": [
                    {
                        "post_number": 1,
                        "cooked": '<p>邀请码没了，截屏如下 <a class="attachment" href="/uploads/short-url/1mZNRGXoftqTQQqN9o9NXZJmOWz.png?dl=1">screenshot.png</a><img src="https://cdn3.ldstatic.com/original/4X/0/9/9/screenshot.png" width="605" height="500"></p>',
                        "link_counts": [{"url": "/uploads/short-url/1mZNRGXoftqTQQqN9o9NXZJmOWz.png?dl=1", "title": "screenshot.png"}],
                    }
                ]
            },
        }
        evidence = linuxdo_notice_monitor.parse_topic_json(topic, linuxdo_notice_monitor.default_config())
        self.assertEqual(evidence["resource_url"], "")
        self.assertIn("https://cdn3.ldstatic.com/original/4X/0/9/9/screenshot.png", evidence["image_urls"])

    def test_forum_registration_chatter_is_silent(self):
        post = self.make_post(
            title="邀请码注册赶上末班车了。发帖被拒一点消息都没有吗？",
            summary="第一次发帖要审核被拒了，一点消息都没有，难道是0级的问题还是太水了",
            section="#搞七捻三",
            topic_id="2324963",
        )
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value={}):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")
        self.assertIn("forum_chatter_low_value", audit[0]["post"]["routing_context"]["matched_categories"])

    def test_welfare_invite_code_action_still_pushes(self):
        post = self.make_post(
            title="送几个邀请码",
            summary="福利羊毛，邀请码可领取，先到先得",
            section="#福利羊毛",
            topic_id="2325010",
        )
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value={}):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(len(push_messages), 1)
        self.assertEqual(audit[0]["decision"], "push")

    def test_design_resource_figma_2325456_is_silent(self):
        post = self.make_post(
            title="【福利快讯】Figma 10000 Ai积分免费领取",
            summary="免费获取10,000个Figma制作积分 Figma Login | Figma 不存在aff哈。",
            section="#福利羊毛",
            topic_id="2325456",
        )
        topic_evidence = {
            "topic_text": "免费获取10,000个Figma制作积分 Figma Login | Figma 不存在aff哈。看到了消息，我就帮大家收集来了。",
            "resource_url": "https://www.figma.com/promo/ai/CONFIG-MAKEATHON-HACKATHON-CREDITS_5F1D1E84",
            "provider": "www.figma.com",
            "price": "免费",
            "frontline_model_verdict": "unknown",
            "why": "发现可直接访问资源",
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")
        self.assertIn("design_resource_low_value", audit[0]["post"]["routing_context"]["matched_categories"])

    def test_design_template_and_credit_welfare_is_silent(self):
        cases = [
            ("Canva Pro 模板素材限时免费", "免费领取 Canva 设计模板和素材包", "https://www.canva.com/templates/free"),
            ("Adobe Firefly credits 免费领", "Adobe 设计积分和素材福利，free credits", "https://www.adobe.com/firefly"),
            ("Framer UI kit 免费下载", "Framer 设计模板 UI kit 图标素材", "https://www.framer.com/templates"),
        ]
        for index, (title, summary, url) in enumerate(cases, start=1):
            post = self.make_post(title=title, summary=summary, section="#福利羊毛", topic_id=f"23255{index}")
            topic_evidence = {
                "topic_text": summary,
                "resource_url": url,
                "provider": url.split("/")[2],
                "price": "免费",
                "frontline_model_verdict": "unknown",
            }
            with self.subTest(title=title), mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
                push_messages, audit = linuxdo_notice_monitor._candidate_items(
                    [post],
                    linuxdo_notice_monitor.default_config(),
                    {},
                    dry_run=True,
                )
            self.assertEqual(push_messages, [])
            self.assertEqual(audit[0]["decision"], "silent")
            self.assertIn("design_resource_low_value", audit[0]["post"]["routing_context"]["matched_categories"])

    def test_design_word_in_developer_project_is_not_design_silent(self):
        post = self.make_post(
            title="开源一个 Figma MCP 插件",
            summary="GitHub repo，给 Codex/Claude 调 Figma 设计稿转代码的开发工具",
            section="#开发调优",
            topic_id="2325457",
        )
        topic_evidence = {
            "topic_text": "开源 Figma MCP plugin，GitHub 仓库，可让 Codex/Claude 读取 Figma 设计稿生成代码。",
            "resource_url": "https://github.com/example/figma-mcp-plugin",
            "provider": "github.com",
            "frontline_model_verdict": "frontline",
            "model_names": ["Codex", "Claude"],
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        categories = audit[0]["post"]["routing_context"]["matched_categories"]
        self.assertNotIn("design_resource_low_value", categories)
        self.assertEqual(audit[0]["decision"], "silent")
        self.assertIn("insufficient_actionable_evidence", categories)

    def test_game_resource_epic_2325872_is_silent(self):
        post = self.make_post(
            title="羊毛~ 免费获取 Epic 游戏 Songs of Conquest 征服之歌 立省148元",
            summary="官方地址:https://store.epicgames.com/p/songs-of-conquest 薅到的 给个互动",
            section="#福利羊毛",
            topic_id="2325872",
        )
        topic_evidence = {
            "topic_text": "《征服之歌》是一款回合制策略游戏。官方地址:https://store.epicgames.com/p/songs-of-conquest 薅到的 给个互动",
            "resource_url": "https://store.epicgames.com/p/songs-of-conquest",
            "provider": "store.epicgames.com",
            "frontline_model_verdict": "unknown",
            "why": "发现可直接访问资源",
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")
        self.assertIn("game_resource_low_value", audit[0]["post"]["routing_context"]["matched_categories"])

    def test_game_store_freebie_welfare_is_silent(self):
        cases = [
            ("Steam 免费游戏限时领取", "Steam 免费获取独立游戏，限免入库", "https://store.steampowered.com/app/123456/free-game"),
            ("GOG 游戏 DLC 免费领", "GOG 限时 DLC 礼包免费领取", "https://www.gog.com/game/free_dlc"),
            ("手游礼包码分享", "TapTap 手游礼包、道具、皮肤兑换码", "https://www.taptap.cn/app/123"),
        ]
        for index, (title, summary, url) in enumerate(cases, start=1):
            post = self.make_post(title=title, summary=summary, section="#福利羊毛", topic_id=f"23258{index}")
            topic_evidence = {
                "topic_text": summary,
                "resource_url": url,
                "provider": url.split("/")[2],
                "price": "免费",
                "frontline_model_verdict": "unknown",
            }
            with self.subTest(title=title), mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
                push_messages, audit = linuxdo_notice_monitor._candidate_items(
                    [post],
                    linuxdo_notice_monitor.default_config(),
                    {},
                    dry_run=True,
                )
            self.assertEqual(push_messages, [])
            self.assertEqual(audit[0]["decision"], "silent")
            self.assertIn("game_resource_low_value", audit[0]["post"]["routing_context"]["matched_categories"])

    def test_game_word_in_developer_project_is_not_game_silent(self):
        post = self.make_post(
            title="开源一个 Unity 游戏资源自动化脚本",
            summary="GitHub repo，Docker 部署，给 Codex/Claude 调用的开发工具 API",
            section="#开发调优",
            topic_id="2325873",
        )
        topic_evidence = {
            "topic_text": "开源 Unity 游戏资源自动化脚本，GitHub 仓库，提供 API 和 CLI，适合开发流程。",
            "resource_url": "https://github.com/example/unity-asset-automation",
            "provider": "github.com",
            "frontline_model_verdict": "frontline",
            "model_names": ["Codex", "Claude"],
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        categories = audit[0]["post"]["routing_context"]["matched_categories"]
        self.assertNotIn("game_resource_low_value", categories)
        self.assertEqual(audit[0]["decision"], "silent")
        self.assertIn("insufficient_actionable_evidence", categories)

    def test_frontline_model_public_resource_can_push_after_enrichment(self):
        post = self.make_post(
            title="RawChat codex公益站火力全开活动",
            summary="codex公益站不限额度，不限速，网址：https://new.sharedchat.cc/",
            section="#福利羊毛",
            author="#RawChat",
        )
        evidence = {
            "topic_text": "Codex 公益站火力全开，支持 GPT-5.4/Codex，不限额度，不限速",
            "resource_url": "https://new.sharedchat.cc/",
            "access_method": "直接访问",
            "model_names": ["Codex", "GPT-5.4"],
            "provider": "RawChat",
            "limits": "不限额度，不限速",
            "risk_notes": "",
            "frontline_model_verdict": "frontline",
            "why": "Codex 公益站限时活动",
        }
        decision = linuxdo_notice_monitor.apply_enriched_gate(post, linuxdo_notice_monitor.evaluate_post(post), evidence, linuxdo_notice_monitor.default_config())
        self.assertEqual(decision.route, "push")

    def test_model_payload_uses_compact_evidence_without_html_or_base64(self):
        post = self.make_post(title="候选公益站", summary="<p>原始 HTML 不应进入模型</p>")
        decision = linuxdo_notice_monitor.RouteDecision("candidate", "needs judge", ["public_site"])
        evidence = {
            "topic_text": "压缩后的正文",
            "resource_url": "https://example.com",
            "model_names": ["Claude Sonnet"],
            "raw_html": "<p>very long html</p>",
            "image_base64": "AAAA",
        }
        payload = linuxdo_notice_monitor.post_payload(post, decision, evidence)
        dumped = json.dumps(payload, ensure_ascii=False)
        self.assertIn("evidence_facts", payload)
        self.assertIn("压缩后的正文", dumped)
        self.assertNotIn("very long html", dumped)
        self.assertNotIn("AAAA", dumped)


    def test_welfare_new_topic_gpt_team_suffix_pushes(self):
        post = self.make_post(
            title="gpt团队新后缀",
            summary="送福利，GPT team 新后缀，能上的佬先上",
            section="#福利羊毛",
            topic_id="2323772",
        )
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value={}):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(len(push_messages), 1)
        self.assertEqual(audit[0]["decision"], "push")

    def test_welfare_new_topic_bug_team_zip_pushes(self):
        post = self.make_post(
            title="自产Bug Team，再送100",
            summary="zip 打包，Bug Team 福利继续送",
            section="#福利羊毛",
            topic_id="2323747",
        )
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value={}):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(len(push_messages), 1)
        self.assertEqual(audit[0]["decision"], "push")

    def test_welfare_new_topic_team_bug_sub2_su2api_pushes(self):
        post = self.make_post(
            title="送福利 一堆team子号 cpa的 su2api 好像是4500+",
            summary="第一次送福利，100个 team bug 子号。sub2格式",
            section="#福利羊毛",
            topic_id="2323658",
        )
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value={}):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(len(push_messages), 1)
        self.assertEqual(audit[0]["decision"], "push")

    def test_feedback_glm_experience_card_welfare_is_silent(self):
        for title, summary in [
            ("新人送glm七天体验卡，剩余4", "GLM Coding Plan 七天体验卡，剩余4个，先到先得"),
            ("送GLM Coding Plan七天体验卡，还剩4个", "新人福利，GLM coding plan 体验卡"),
            ("商汤大模型公测，可以免费体验了", "商汤大模型公测，免费体验国产模型"),
        ]:
            post = self.make_post(title=title, summary=summary, section="#福利羊毛", topic_id="2329101")
            with self.subTest(title=title), mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value={}):
                push_messages, audit = linuxdo_notice_monitor._candidate_items(
                    [post],
                    linuxdo_notice_monitor.default_config(),
                    {},
                    dry_run=True,
                )
            self.assertEqual(push_messages, [])
            self.assertEqual(audit[0]["decision"], "silent")
            self.assertIn("domestic_or_non_top_model_welfare", audit[0]["post"]["routing_context"]["matched_categories"])

    def test_feedback_guomo_model_welfare_short_forms_are_silent(self):
        for title, summary in [
            ("国模GLM5.2 DEEPSEEKV4 PRO", ""),
            ("国模PRO账号分享", "国产模型 pro 账号，给需要的佬"),
            ("国模体验卡分享", "国产大模型体验卡，先到先得"),
        ]:
            post = self.make_post(title=title, summary=summary, section="#福利羊毛", topic_id="2477492")
            with self.subTest(title=title), mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value={}):
                push_messages, audit = linuxdo_notice_monitor._candidate_items(
                    [post],
                    linuxdo_notice_monitor.default_config(),
                    {},
                    dry_run=True,
                )
            self.assertEqual(push_messages, [])
            self.assertEqual(audit[0]["decision"], "silent")
            categories = audit[0]["post"]["routing_context"]["matched_categories"]
            self.assertTrue({"domestic_only_model", "non_frontline_model"} & set(categories))

    def test_guomo_mentions_do_not_silence_clear_top_model_resource(self):
        post = self.make_post(
            title="公益站支持 GPT 和国模",
            summary="同时支持 GPT 和国产大模型，Base URL: https://mixed.example/v1，key填任意值",
            section="#福利羊毛",
            topic_id="2477493",
        )
        topic_evidence = {
            "topic_text": "公益站支持 GPT 和国模，同时支持 GPT 和国产大模型，Base URL: https://mixed.example/v1，key填任意值",
            "resource_url": "https://mixed.example/v1",
            "access_method": "key填任意值",
            "model_names": ["GPT", "GLM"],
            "frontline_model_verdict": "frontline",
            "model_coverage": {
                "present_top_families": ["gpt"],
                "present_non_frontline_families": ["glm"],
                "domestic_only": False,
                "top_model_absent_explicit": False,
                "confidence": "explicit",
                "source": ["text"],
            },
            "model_resource_profile": {
                "is_model_resource": True,
                "domestic_or_non_top_subject": True,
                "top_model_is_core_resource": True,
                "top_model_resource_families": ["gpt"],
            },
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(len(push_messages), 1)
        self.assertEqual(audit[0]["decision"], "push")

    def test_feedback_mimo_key_welfare_is_silent_without_top_model_resource(self):
        post = self.make_post(
            title="分享一个小米mimo的max套餐的key",
            summary="小米 MiMo max 套餐 key，额度重置了，base url https://token-plan-sgp.xiaomimimo.com/v1",
            section="#福利羊毛",
            topic_id="2329102",
        )
        topic_evidence = {
            "topic_text": "分享一个小米mimo的max套餐的key，剩余额度很多，Base URL https://token-plan-sgp.xiaomimimo.com/v1",
            "resource_url": "https://token-plan-sgp.xiaomimimo.com/v1",
            "access_method": "key填任意值",
            "model_names": ["MiMo"],
            "provider": "xiaomimimo.com",
            "limits": "额度重置了",
            "frontline_model_verdict": "non_frontline",
            "model_coverage": {
                "present_top_families": [],
                "present_non_frontline_families": ["mimo"],
                "domestic_only": False,
                "top_model_absent_explicit": False,
                "confidence": "explicit_absent",
            },
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")
        self.assertIn("non_frontline_model", audit[0]["post"]["routing_context"]["matched_categories"])

    def test_feedback_low_value_welfare_domains_are_silent(self):
        cases = [
            ("搬瓦工流量快捷指令", "搬瓦工 VPS 流量查询快捷指令，服务器流量提醒", "infrastructure_hosting_welfare"),
            ("喜马拉雅7天体验卡 5张", "喜马拉雅会员体验卡，免费送", "media_subscription_welfare"),
            ("波点音乐会员 7 天体验卡", "波点音乐会员权益免费领取", "media_subscription_welfare"),
            ("发PTTIME*6", "PTTIME 邀请码 6 个，PT 相关", "private_tracker_welfare"),
            ("新人发现 LDC 比工资还难赚", "LDC 太难赚了，论坛货币闲聊", "forum_currency_chatter"),
        ]
        for title, summary, category in cases:
            post = self.make_post(title=title, summary=summary, section="#福利羊毛", topic_id="2329103")
            with self.subTest(title=title), mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value={}):
                push_messages, audit = linuxdo_notice_monitor._candidate_items(
                    [post],
                    linuxdo_notice_monitor.default_config(),
                    {},
                    dry_run=True,
                )
            self.assertEqual(push_messages, [])
            self.assertEqual(audit[0]["decision"], "silent")
            self.assertIn(category, audit[0]["post"]["routing_context"]["matched_categories"])

    def test_feedback_domain_registration_welfare_is_silent(self):
        post = self.make_post(
            title="netcup 1.32欧/年！de域名活动限时返场",
            summary="netcup .de 域名活动限时返场，官方 deals 页面可购买",
            section="#福利羊毛",
            topic_id="2329601",
        )
        topic_evidence = {
            "topic_text": "netcup 1.32欧/年 .de domain deal 限时返场，域名注册优惠",
            "resource_url": "https://www.netcup.com/en/deals",
            "provider": "www.netcup.com",
            "price": "1.32欧/年",
            "frontline_model_verdict": "unknown",
            "why": "发现可直接访问资源",
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")
        self.assertIn("domain_registration_welfare", audit[0]["post"]["routing_context"]["matched_categories"])

    def test_feedback_opencode_key_is_domestic_or_non_top_model_silent(self):
        post = self.make_post(
            title="分享自用opencode key",
            summary="OpenCode key 分享，自用额度还有，key 在图片里",
            section="#福利羊毛",
            topic_id="2329602",
        )
        topic_evidence = {
            "topic_text": "分享自用 OpenCode key，额度还有，key 在图片中，非 GPT/Claude/Grok/Gemini 资源",
            "resource_url": "#far-image",
            "access_method": "key 在图片里",
            "model_names": ["OpenCode"],
            "provider": "OpenCode",
            "limits": "还有额度",
            "frontline_model_verdict": "unknown",
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")
        self.assertIn("domestic_or_non_top_model_welfare", audit[0]["post"]["routing_context"]["matched_categories"])

    def test_feedback_ambiguous_consumer_welfare_water_post_is_silent(self):
        post = self.make_post(
            title="袋鼠送雪王啦",
            summary="袋鼠送雪王啦，看图领取",
            section="#福利羊毛",
            topic_id="2329603",
        )
        topic_evidence = {
            "topic_text": "袋鼠送雪王啦，只有截图，没有明确金额、兑换码、链接或技术资源",
            "resource_url": "#far-image",
            "image_urls": ["https://cdn.example/kangaroo.png"],
            "frontline_model_verdict": "unknown",
            "why": "发现可直接访问资源",
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")
        self.assertIn("ambiguous_consumer_welfare_low_value", audit[0]["post"]["routing_context"]["matched_categories"])

    def test_feedback_azure_welfare_question_is_silent(self):
        post = self.make_post(
            title="azure200号现在还能白嫖一年吗",
            summary="请问 azure200 号现在还能不能白嫖一年，有没有办法",
            section="#福利羊毛",
            topic_id="2329604",
        )
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value={}):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")
        self.assertIn("welfare_question_low_value", audit[0]["post"]["routing_context"]["matched_categories"])

    def test_feedback_super_grok_subscription_question_is_silent(self):
        post = self.make_post(
            title="印度super grok问题",
            summary="在网页上显示700卢比3个月，但是在app store又变成1300卢比3个月，太奇怪了",
            section="#福利羊毛",
            topic_id="2397519",
        )
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value={}):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")
        self.assertIn("welfare_subscription_question_low_value", audit[0]["post"]["routing_context"]["matched_categories"])

    def test_feedback_xunfei_model_resource_is_domestic_silent(self):
        post = self.make_post(
            title="讯飞明天到期还剩下12000次",
            summary="讯飞接口明天到期，还剩下 12000 次，给需要的佬",
            section="#福利羊毛",
            topic_id="2400912",
        )
        topic_evidence = {
            "topic_text": "讯飞明天到期还剩下12000次，接口 https://maas-coding-api.cn-huabei-1.xf-yun.com/anthropic",
            "resource_url": "https://maas-coding-api.cn-huabei-1.xf-yun.com/anthropic",
            "provider": "maas-coding-api.cn-huabei-1.xf-yun.com",
            "limits": "还剩下12000次，明天到期",
            "frontline_model_verdict": "unknown",
            "why": "发现可直接访问资源",
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")
        self.assertIn("domestic_or_non_top_model_welfare", audit[0]["post"]["routing_context"]["matched_categories"])

    def test_feedback_welfare_question_about_gpt_degradation_is_silent(self):
        post = self.make_post(
            title="gpt5.5大幅降智是中转站的问题还是官方问题？",
            summary="想问问这是中转站的问题还是官方问题，有没有佬知道",
            section="#搞七捻三",
            topic_id="2329104",
        )
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value={}):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")
        self.assertTrue(
            {"question_discussion_low_value", "welfare_question_low_value", "support_troubleshooting_help", "generic_help_question", "public_site_chatter_low_value"}
            & set(audit[0]["post"]["routing_context"]["matched_categories"])
        )

    def test_feedback_20260615_welfare_question_negative_and_water_posts_are_silent(self):
        cases = [
            (
                "求助，codex要二验，现在有办法解决吗？目前正能登录网页端",
                "codex 要二验，有没有办法解决，目前网页端还能登录",
                {
                    "topic_text": "求助，codex要二验，现在有办法解决吗？目前正能登录网页端",
                    "resource_url": "#far-image",
                    "image_urls": ["https://cdn.example/codex-verify.png"],
                    "topic_tags": ["快问快答"],
                    "why": "发现可直接访问资源",
                },
                {"low_value_welfare_intent", "welfare_question_low_value"},
            ),
            (
                "bybit被反薅了",
                "不符合领取此奖励的条件，错误代码403003，空投领取失败",
                {
                    "topic_text": "bybit 被反薅了，不符合领取此奖励的条件，错误代码403003，领取失败",
                    "resource_url": "#far-image",
                    "image_urls": ["https://cdn.example/bybit-failed.png"],
                    "limits": ["不符合领取此奖励的条件", "错误代码403003"],
                    "risk_notes": ["空投", "领取失败"],
                    "price": "10 USDT",
                    "why": "发现可直接访问资源",
                },
                {"low_value_welfare_intent", "negative_welfare_status_low_value"},
            ),
            (
                "你们的any，codex一直能用吗",
                "想问问大家 any 的 codex 一直能不能用",
                {},
                {"low_value_welfare_intent", "welfare_question_low_value"},
            ),
            (
                "any蹬上了，但它说它是肥波5？",
                "截图里显示 Opus 4.8 / Claude Fable 5 / Claude 5，API Error 400，Request rejected (429)，Service Unavailable",
                {
                    "topic_text": "any蹬上了，但它说它是肥波5？截图显示 Opus 4.8 Claude Fable 5 Claude 5，API Error 400，Request rejected (429)，Service Unavailable",
                    "resource_url": "#far-image",
                    "image_urls": ["https://cdn.example/any-fable.png"],
                    "topic_tags": ["纯水"],
                    "model_names": ["Opus 4.8", "Claude Fable 5", "Claude 5"],
                    "frontline_model_verdict": "frontline",
                    "model_coverage": {
                        "present_top_families": ["claude"],
                        "domestic_only": False,
                        "top_model_absent_explicit": False,
                        "confidence": "explicit",
                    },
                    "risk_notes": ["API Error 400", "Request rejected (429)", "Service Unavailable"],
                    "why": "发现可直接访问资源",
                },
                {"low_value_welfare_intent", "welfare_water_or_screenshot_low_value"},
            ),
        ]
        for index, (title, summary, topic_evidence, expected_categories) in enumerate(cases, start=1):
            post = self.make_post(title=title, summary=summary, section="#福利羊毛", topic_id=f"2409{index}")
            with self.subTest(title=title), mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
                push_messages, audit = linuxdo_notice_monitor._candidate_items(
                    [post],
                    linuxdo_notice_monitor.default_config(),
                    {},
                    dry_run=True,
                )
            self.assertEqual(push_messages, [])
            self.assertEqual(audit[0]["decision"], "silent")
            categories = set(audit[0]["post"]["routing_context"]["matched_categories"])
            self.assertTrue(expected_categories.issubset(categories))

    def test_far_image_resource_does_not_count_as_direct_resource(self):
        evidence = {
            "resource_url": "#far-image",
            "topic_text": "截图里显示领取失败，没有可用链接",
            "image_urls": ["https://cdn.example/screenshot.png"],
        }
        self.assertFalse(linuxdo_notice_monitor._has_direct_non_image_resource(evidence))
        post = self.make_post(
            title="截图显示领取失败",
            summary="只有截图，没有可用链接",
            section="#福利羊毛",
            topic_id="2409100",
        )
        profile = linuxdo_notice_monitor._low_value_intent_profile(post, evidence, linuxdo_notice_monitor.default_config())
        self.assertTrue(profile["resource_is_image_only"])
        self.assertFalse(profile["has_direct_resource"])

    def test_welfare_resource_requests_and_availability_questions_do_not_push(self):
        cases = [
            (
                "求稳定公益站 Base URL 最近卡顿",
                "求一个稳定的 GPT 公益站 Base URL，最近这个站卡顿严重",
            ),
            (
                "蹲一个能用的公益站 Base URL",
                "有没有大佬分享一个现在还能用的 Claude 公益站 Base URL",
            ),
            (
                "这个公益站现在能用不",
                "贴一下 https://any.example/v1，key 填任意值，想问现在还行不，有人测过没",
            ),
        ]
        for index, (title, summary) in enumerate(cases, start=1):
            post = self.make_post(title=title, summary=summary, section="#福利羊毛", topic_id=f"24092{index}")
            with self.subTest(title=title), mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value={}):
                push_messages, audit = linuxdo_notice_monitor._candidate_items(
                    [post],
                    linuxdo_notice_monitor.default_config(),
                    {},
                    dry_run=True,
                )
            self.assertEqual(push_messages, [])
            self.assertEqual(audit[0]["decision"], "silent")
            categories = set(audit[0]["post"]["routing_context"]["matched_categories"])
            self.assertTrue({"low_value_welfare_intent", "welfare_question_low_value"}.issubset(categories))

    def test_feedback_hub_configuration_question_is_silent_even_with_top_model_resource_terms(self):
        post = self.make_post(
            title="配置 Hub 站时产生的一些疑惑",
            summary="apikey 调用全部可用的模型，不需要在 ccs 里频繁切换。public-sub2api: APITimeoutError: Request timed out.",
            section="#开发调优",
            topic_id="2429102",
        )
        topic_evidence = {
            "topic_text": "配置 Hub 站时产生的一些疑惑。apikey 调用全部可用的模型，不需要在 ccs 里频繁切换 desktop 的模型选择。public-sub2api: APITimeoutError: Request timed out.",
            "resource_url": "https://hub.example",
            "access_method": "apikey 调用全部可用的模型",
            "model_names": ["gpt-5.5", "codex"],
            "frontline_model_verdict": "frontline",
            "model_coverage": {
                "present_top_families": ["gpt"],
                "present_non_frontline_families": [],
                "domestic_only": False,
                "top_model_absent_explicit": False,
                "confidence": "explicit",
            },
            "risk_notes": "public-sub2api: APITimeoutError: Request timed out.",
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")
        categories = set(audit[0]["post"]["routing_context"]["matched_categories"])
        self.assertTrue({"support_troubleshooting_help", "question_discussion_low_value"} & categories)
        self.assertNotIn("frontline_public_resource", categories)

    def test_top_model_welfare_still_pushes_when_resource_context_is_clear(self):
        post = self.make_post(
            title="GPT 200刀, 佬友们速蹬",
            summary="GPT-5.5 200刀额度福利，评论留 id 送额度",
            section="#福利羊毛",
            topic_id="2329105",
        )
        topic_evidence = {
            "topic_text": "GPT-5.5 200刀额度福利，支持 GPT，评论留 id 送额度",
            "model_names": ["GPT-5.5"],
            "limits": "200刀额度",
            "frontline_model_verdict": "frontline",
            "model_coverage": {
                "present_top_families": ["gpt"],
                "present_non_frontline_families": [],
                "domestic_only": False,
                "top_model_absent_explicit": False,
                "confidence": "explicit",
            },
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(len(push_messages), 1)
        self.assertEqual(audit[0]["decision"], "push")

    def test_email_account_welfare_giveaway_is_silent(self):
        post = self.make_post(
            title="放5个hotmail喵",
            summary="账号----密码，先到先得，只是 hotmail 邮箱账号",
            section="#福利羊毛",
            topic_id="2323873",
        )
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value={}):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")
        self.assertIn("email_account_giveaway", audit[0]["post"]["routing_context"]["matched_categories"])

    def test_batch_email_password_welfare_giveaway_is_silent(self):
        post = self.make_post(
            title="送一批邮箱账号",
            summary="user1@gmail.com----pass123 user2@outlook.com----pass456",
            section="#福利羊毛",
            topic_id="2323874",
        )
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value={}):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")
        self.assertIn("email_account_giveaway", audit[0]["post"]["routing_context"]["matched_categories"])

    def test_ripgrep_agent_technical_reference_is_silent(self):
        post = self.make_post(
            title="给 Agent 使用 ripgrep 时一定要加这个参数！",
            summary="分享教程：Dais Agent 在 gpt-5.4 和 1M 上下文里使用 rg --max-columns 300 --max-columns-preview 的经验帖，中转站余额不足且出现八十万 token 的请求",
            section="#开发调优",
            topic_id="2324288",
        )
        topic_evidence = {
            "topic_text": "给 Agent 使用 ripgrep 时一定要加这个参数。中转站的余额不足，排查后发现八十万 token 的请求来自 rg --max-columns 300 --max-columns-preview。",
            "resource_url": "https://bhznjns.github.io/#static/%E7%BC%96%E7%A8%8B/LLM/%E8%AE%B0%E4%B8%80%E4%B8%AA%E5%9C%A8%20Agent%20%E4%B8%AD%E6%8E%A5%E5%85%A5%20ripgrep%20%E7%9A%84%E4%BD%BF%E7%94%A8%E6%95%99%E8%AE%AD.md",
            "link_titles": ["BHznJNs' Blog", "我从零开发的 Dais agent 发布 0.11.3 版本，支持 Linux"],
            "model_names": ["gpt-5.4"],
            "frontline_model_verdict": "frontline",
            "why": "发现外部博客链接",
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")

    def test_topic_json_tutorial_link_is_reference_not_resource(self):
        topic = {
            "title": "给 Agent 使用 ripgrep 时一定要加这个参数！",
            "category_id": 4,
            "post_stream": {
                "posts": [
                    {
                        "post_number": 1,
                        "cooked": '<p>中转站余额不足后的经验帖：rg --max-columns 300 --max-columns-preview <a href="https://bhznjns.github.io/#static/%E7%BC%96%E7%A8%8B/LLM/%E8%AE%B0%E4%B8%80%E4%B8%AA%E5%9C%A8%20Agent%20%E4%B8%AD%E6%8E%A5%E5%85%A5%20ripgrep%20%E7%9A%84%E4%BD%BF%E7%94%A8%E6%95%99%E8%AE%AD.md">博客原文</a></p>',
                        "link_counts": [{"url": "https://bhznjns.github.io/#static/%E7%BC%96%E7%A8%8B/LLM/%E8%AE%B0%E4%B8%80%E4%B8%AA%E5%9C%A8%20Agent%20%E4%B8%AD%E6%8E%A5%E5%85%A5%20ripgrep%20%E7%9A%84%E4%BD%BF%E7%94%A8%E6%95%99%E8%AE%AD.md", "title": "BHznJNs' Blog"}],
                    }
                ]
            },
        }
        evidence = linuxdo_notice_monitor.parse_topic_json(topic)
        self.assertEqual(evidence["resource_url"], "")
        self.assertEqual(evidence["reference_url"], "https://bhznjns.github.io/#static/%E7%BC%96%E7%A8%8B/LLM/%E8%AE%B0%E4%B8%80%E4%B8%AA%E5%9C%A8%20Agent%20%E4%B8%AD%E6%8E%A5%E5%85%A5%20ripgrep%20%E7%9A%84%E4%BD%BF%E7%94%A8%E6%95%99%E8%AE%AD.md")

    def test_delivered_topic_suppresses_primary_supplement_duplicate(self):
        state = {
            "delivered_topics": {
                "2323772": {"delivered_at": linuxdo_notice_monitor.utc_now_iso(), "message_id": 406900}
            }
        }
        post = self.make_post(
            title="RawChat codex公益站火力全开活动",
            summary="codex公益站不限额度，不限速，网址：https://new.sharedchat.cc/",
            section="#福利羊毛",
            channel="LinuxDoNew",
            message_id=445600,
            topic_id="2323772",
        )
        topic_evidence = {
            "topic_text": "Codex 公益站火力全开，支持 GPT-5.4/Codex，不限额度，不限速",
            "resource_url": "https://new.sharedchat.cc/",
            "model_names": ["Codex", "GPT-5.4"],
            "limits": "不限额度，不限速",
            "frontline_model_verdict": "frontline",
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                state,
                dry_run=False,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")

    def test_support_troubleshooting_post_2324480_is_silent(self):
        post = self.make_post(
            title="求助 CPA 中 codex xhigh 报错",
            summary="CPA 配置如下，openai-compatibility，models gpt-5.5 thinking levels low medium high xhigh。已经按照自定义供应商 中转站 提示不支持 xhigh 设置，但在 codex 中 /model 选择 gpt-5.5 xhigh 时报错。",
            section="#搞七捻三",
            author="#TiansuiWu06",
            topic_id="2324480",
            topic_post_no="1",
        )
        topic_evidence = {
            "topic_text": "求助 CPA 中 codex xhigh 报错。CPA 配置如下 openai-compatibility，api-key-entries，models gpt-5.5 thinking levels low medium high xhigh。provider=openai model=gpt-5.5 error=level xhigh not supported, valid levels: low, medium, high。",
            "resource_url": "https://github.com/router-for-me/CLIProxyAPI/discussions/2564",
            "link_titles": ["自定义供应商 中转站 提示不支持 xhigh · router-for-me/CLIProxyAPI · Discussion #2564 · GitHub"],
            "model_names": ["gpt-5.5", "codex"],
            "frontline_model_verdict": "frontline",
            "why": "发现 GitHub discussion",
        }
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value=topic_evidence):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                linuxdo_notice_monitor.default_config(),
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")
        self.assertIn("support_troubleshooting_help", audit[0]["post"]["routing_context"]["matched_categories"])

    def test_support_github_discussion_is_reference_not_resource(self):
        topic = {
            "title": "求助 CPA 中 codex xhigh 报错",
            "category_id": 10,
            "post_stream": {
                "posts": [
                    {
                        "post_number": 1,
                        "cooked": '<p>求助 CPA 配置报错，provider=openai model=gpt-5.5 error=level xhigh not supported。参考 <a href="https://github.com/router-for-me/CLIProxyAPI/discussions/2564">GitHub discussion</a></p>',
                        "link_counts": [{"url": "https://github.com/router-for-me/CLIProxyAPI/discussions/2564", "title": "自定义供应商 中转站 提示不支持 xhigh · Discussion #2564 · GitHub"}],
                    }
                ]
            },
        }
        evidence = linuxdo_notice_monitor.parse_topic_json(topic)
        self.assertEqual(evidence["resource_url"], "")
        self.assertEqual(evidence["reference_url"], "https://github.com/router-for-me/CLIProxyAPI/discussions/2564")

    def test_priority_author_support_troubleshooting_is_still_silent(self):
        config = linuxdo_notice_monitor.default_config()
        config["priority_authors"] = ["#RawChat"]
        post = self.make_post(
            title="求助 Codex 自定义 provider 报错",
            summary="配置了 openai-compatibility 和 api-key，/model 选择 gpt-5.5 xhigh 后提示 not supported，怎么解决？",
            section="#开发调优",
            author="#RawChat",
            topic_id="2324481",
        )
        with mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value={}):
            push_messages, audit = linuxdo_notice_monitor._candidate_items(
                [post],
                config,
                {},
                dry_run=True,
            )
        self.assertEqual(push_messages, [])
        self.assertEqual(audit[0]["decision"], "silent")
        self.assertIn("support_troubleshooting_help", audit[0]["post"]["routing_context"]["matched_categories"])

    def test_append_decision_log_writes_compact_reportable_entries(self):
        post = self.make_post(
            title="RawChat codex公益站恢复并扩容",
            summary="<html>Base64 should not leak</html> data:image/png;base64,AAAA",
            section="#福利羊毛",
            author="#RawChat",
            topic_id="2329001",
        )
        payload = linuxdo_notice_monitor.post_payload(
            post,
            linuxdo_notice_monitor.RouteDecision(
                "push",
                "一线模型公益资源富化后确认",
                ["welfare_full_push", "frontline_public_resource"],
            ),
            {
                "topic_text": "<p>RawChat 公益站恢复，Base URL https://raw.example/v1，支持 GPT/Claude</p>",
                "resource_url": "https://raw.example/v1",
                "access_method": "key填任意值",
                "model_names": ["GPT-5.4", "Claude Sonnet"],
                "frontline_model_verdict": "frontline",
                "raw_html": "<html>full page</html>",
                "image_base64": "data:image/png;base64,AAAA",
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "decisions.jsonl"
            written = linuxdo_notice_monitor.append_decision_log_entries(
                [{"post": payload, "decision": "push"}],
                log_path=log_path,
                delivered_topic_ids={"2329001"},
            )
            self.assertEqual(written, 1)
            lines = log_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["topic_id"], "2329001")
        self.assertTrue(entry["delivered"])
        self.assertEqual(entry["routing_context"]["route"], "push")
        self.assertIn("frontline_public_resource", entry["matched_categories"])
        dumped = json.dumps(entry, ensure_ascii=False)
        self.assertNotIn("raw_html", dumped)
        self.assertNotIn("base64", dumped.lower())
        self.assertNotIn("<html>", dumped)

    def test_append_decision_log_skips_pure_low_signal_noise(self):
        post = self.make_post(
            title="随便水一下",
            summary="没有资源，也没有需要复盘的价值",
            topic_id="2329002",
        )
        payload = linuxdo_notice_monitor.post_payload(
            post,
            linuxdo_notice_monitor.RouteDecision("silent", "未命中 Linux.do 高价值边界", ["low_signal"]),
            {},
        )
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "decisions.jsonl"
            written = linuxdo_notice_monitor.append_decision_log_entries(
                [{"post": payload, "decision": "silent"}],
                log_path=log_path,
            )
            self.assertEqual(written, 0)
            self.assertFalse(log_path.exists())

    def test_decision_log_keeps_compact_post_value_profile(self):
        post = self.make_post(
            title="公益站关闭通知",
            summary="公益站已经关闭，不再维护，没有新地址",
            section="#搞七捻三",
            topic_id="2423599",
        )
        evidence = linuxdo_notice_monitor.build_evidence_from_text(
            "公益站已经关闭，不再维护，没有新地址",
            linuxdo_notice_monitor.default_config(),
        )
        linuxdo_notice_monitor.finalize_post_value_profile(post, evidence, linuxdo_notice_monitor.default_config())
        linuxdo_notice_monitor._append_low_value_context(evidence, "public_site_shutdown_low_value")
        payload = linuxdo_notice_monitor.post_payload(
            post,
            linuxdo_notice_monitor.RouteDecision(
                "silent",
                "公益站关闭/停运且无可执行替代资源默认静默",
                ["public_site_shutdown_low_value"],
            ),
            evidence,
        )
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "decisions.jsonl"
            written = linuxdo_notice_monitor.append_decision_log_entries(
                [{"post": payload, "decision": "silent"}],
                log_path=log_path,
            )
            self.assertEqual(written, 1)
            entry = json.loads(log_path.read_text(encoding="utf-8").strip())

        profile = entry["evidence_facts"]["post_value_profile"]
        self.assertEqual(profile["actionability"], "negative_terminal")
        self.assertEqual(profile["hard_silent_reason"], "public_site_shutdown_low_value")
        dumped = json.dumps(entry, ensure_ascii=False)
        self.assertNotIn("raw_html", dumped)
        self.assertNotIn("base64", dumped.lower())

    def test_run_once_persists_decision_log_without_dry_run(self):
        post = self.make_post(
            title="RawChat codex公益站火力全开活动",
            summary="codex公益站不限额度，不限速，网址：https://new.sharedchat.cc/",
            section="#福利羊毛",
            author="#RawChat",
            topic_id="2329003",
        )
        config = linuxdo_notice_monitor.default_config()
        with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(linuxdo_notice_monitor, "load_config", return_value=config), \
            mock.patch.object(linuxdo_notice_monitor, "load_state", return_value={"channels": {"LinuxDoNew": {"last_seen_id": 0}}}), \
            mock.patch.object(linuxdo_notice_monitor, "save_state"), \
            mock.patch.object(linuxdo_notice_monitor, "collect_recent_posts", return_value=([post], {"LinuxDoNew": {}})), \
            mock.patch.object(linuxdo_notice_monitor, "fetch_topic_evidence", return_value={
                "topic_text": "Codex 公益站火力全开，支持 GPT-5.4/Codex，不限额度，不限速",
                "resource_url": "https://new.sharedchat.cc/",
                "access_method": "直接访问",
                "model_names": ["GPT-5.4", "Codex"],
                "frontline_model_verdict": "frontline",
                "limits": "不限额度，不限速",
                "why": "Codex 公益站限时火力全开",
            }), \
            mock.patch.object(linuxdo_notice_monitor, "DECISION_LOG_FILE", Path(tmp) / "decisions.jsonl"):
            linuxdo_notice_monitor.run_once(dry_run=False)

            lines = (Path(tmp) / "decisions.jsonl").read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["topic_id"], "2329003")


if __name__ == "__main__":
    unittest.main()
