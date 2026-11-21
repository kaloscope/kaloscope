"""Test user authorization and persistent filters for assistant subscriptions."""

import asyncio
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.core.ai.tools import execute_tool
from app.core.dl import syncer
from app.core.exceptions import BadRequestException, ForbiddenException
from app.models.assistant import (
    AssistantConversation,
    AssistantToolCall,
    AssistantTurn,
    ConversationCreate,
)
from app.models.assistant_questions import AnswerGroup
from app.models.download import Downloader, DownloadPlan, DownloadPlanQuery
from app.models.media import MediaLib
from app.models.review_decision import AssistantReviewDecision
from app.models.user import User
from app.services import download
from app.services.ai import AIProviderService
from app.services.assistant import AssistantService
from app.services.assistant_questions import answer_card, continuation_prompt
from app.services.assistant_reviews import assistant_review
from app.services.assistant_sessions import AssistantSessionService as Sessions
from app.services.subscription_choices import (
    matches_release_rule,
    release_choices,
    release_matches,
)
from app.services.subscription_intent import (
    SubscriptionIntent,
    accepts_offer,
    explicit_subscription,
    requested_title,
    revised_subscription_plan,
)
from app.utils.json import dumps
from tests.test_assistant_persistence import MODEL, database, request
from tests.test_download_plan_authoring import fixture_plan
from tests.test_download_plan_authoring import isolate as isolate
from tests.test_download_plan_conversation import engine_fixture
from tests.test_services_assistant import call_delta, chunk, configure, response
from tests.test_services_download import DraftDriver

TITLE = "学生会也有洞"
TERM = "喵萌奶茶屋"


@pytest.mark.parametrize(
    "prompt,allowed,title",
    [
        ("帮我订阅「为什么是我？」", True, "为什么是我？"),
        ("Subscribe me to Lantern with a total limit of 20", True, "Lantern"),
        (
            "《Lantern》我准备追了，有更新就自动帮我下，字幕组我自己挑。",
            True,
            "Lantern",
        ),
        ("《Lantern》有更新就自动帮我下载吗？", False, ""),
        ("请问怎么订阅？", False, ""),
        ("帮我订阅 Lantern，但先给我预览，不要创建", False, ""),
        ("帮我订阅《旧剧》，等我确认后创建", False, ""),
        ("帮我删除后重建下载计划", False, ""),
        ("告诉我能不能订阅", False, ""),
    ],
)
def test_order(prompt, allowed, title):
    assert explicit_subscription(prompt) is allowed
    if allowed:
        assert requested_title(prompt) == title


@pytest.mark.parametrize(
    "prompt", ["这个目录有多少空间", "想了解字幕组", "谢谢", "继续", "只要看一下"]
)
def test_unrelated_reply(prompt):
    assert not accepts_offer(prompt)


def test_release_tags():
    items = [
        {"title": "【悠哈璃羽字幕社】 Lantern 07 [CHS&CHT][简繁内封][1080p]"},
        {"title": "【悠哈璃羽字幕社】 Lantern 07 [CHS][简体内嵌][1080p]"},
        {"title": "[S01E07] Lantern", "uploader": "Tea"},
        {"title": "[简体内嵌] Lantern"},
        {"title": "[Lantern] 第七集"},
    ]
    assert release_choices(items, "Lantern") == [
        ["悠哈璃羽字幕社", "简繁内封"],
        ["Tea"],
        ["悠哈璃羽字幕社", "简体内嵌"],
    ]
    assert release_matches(items[0], "Lantern", ["悠哈璃羽字幕社", "CHS&CHT", "1080p"])
    assert not release_matches(items[0], "Lantern", ["悠哈璃羽字幕社", "CHS"])
    assert not release_matches({"title": "[TeaHouse] Lantern Tea"}, "Lantern", ["Tea"])
    missing_tags = [*items, {"title": "【悠哈璃羽字幕社】 Lantern 06"}]
    assert release_choices(missing_tags, "Lantern") == [
        ["悠哈璃羽字幕社", "简繁内封", "1080p"],
        ["Tea"],
        ["悠哈璃羽字幕社", "简体内嵌", "1080p"],
    ]


@pytest.mark.parametrize("reverse", [False, True])
def test_release_title_scripts(reverse):
    title = "一觉醒来就有了最强装备跟太空船"
    published = "一覺醒來就有了最強裝備跟太空船"
    if reverse:
        title, published = published, title
    rule = {
        "title": title,
        "terms": ["ANi", "CHT"],
        "observed": ["ANi", "CHT"],
        "confirmed": True,
    }
    release = {"title": f"[ANi] {published} - 07 [CHT]", "uploader": "ANi"}

    assert matches_release_rule(release, rule)
    assert not matches_release_rule({"title": f"[ANi] {published} - 07 [CHS]"}, rule)
    assert not matches_release_rule(
        {"title": f"[Other] {published} - 07 [CHT]", "uploader": "ANi"}, rule
    )
    assert not matches_release_rule({"title": "[ANi] 另一部太空船 - 07 [CHT]"}, rule)


async def saved(turn, call_id, name, data):
    """Record a successful tool result for the fixture.

    Args:
        turn: Originating turn.
        call_id: Server call identifier.
        name: Registered tool name.
        data: Synthetic domain facts.

    Returns:
        Persisted fixture call.
    """
    return await AssistantToolCall.create(
        turn_id=turn.id,
        call_id=call_id,
        name=name,
        arguments="{}",
        status="complete",
        result={"ok": True, "data": data},
    )


async def invoke(turn, owner, name, args, call_id):
    """Run the real assistant adapter with a server-owned call record.

    Args:
        turn: Current conversation turn.
        owner: Current administrator.
        name: Registered tool name.
        args: Model arguments to validate.
        call_id: Server call identity.

    Returns:
        Domain data recorded for this tool call.
    """
    arguments = dumps(args).decode()
    record = await AssistantToolCall.create(
        turn_id=turn.id, call_id=call_id, name=name, arguments=arguments
    )
    result = await execute_tool(
        name, arguments, owner, conversation_id=turn.conversation_id, call_id=call_id
    )
    assert result["ok"], result
    record.status, record.result = "complete", result
    await record.save()
    return result["data"]


@pytest.mark.parametrize(
    "case, expected",
    [
        ("direct", "needs_confirmation"),
        ("count_only", "needs_confirmation"),
        ("answer", "needs_confirmation"),
        ("native", "needs_confirmation"),
        ("wrong_native", "needs_settings"),
        ("wrong_limit", "needs_settings"),
        ("wrong_title", "needs_settings"),
        ("only_category", "needs_settings"),
        ("wrong_search", "needs_search"),
        ("empty", "no_matches"),
        ("mixed", "unverified_term"),
        ("short_page", "needs_search"),
        ("two_terms", "needs_preferences"),
        ("old_review", "needs_confirmation"),
        ("cancelled", "needs_confirmation"),
        ("already_created", "needs_confirmation"),
    ],
)
def test_submission(case, expected):
    async def run():
        async with database() as (owner, _):
            graph, _, body = await fixture_plan(keyword=TITLE, total_limit=20)
            native = case in {"native", "wrong_native"}
            if native:
                graph.definition["nodes"][0]["data"]["config"] += (
                    "  uploader:\n    type: select\n"
                    "    options: {tea: 喵萌奶茶屋, ani: ANi}\n"
                )
                await graph.save(update_fields=["definition"])
            conversation = await AssistantConversation.create(
                user_id=owner.id, title="Subscription"
            )
            is_answer = case != "direct"
            choice = "只要喵萌奶茶屋、总计上限20"
            first = await AssistantTurn.create(
                conversation_id=conversation.id,
                request_id="order",
                status="complete",
                prompt="帮我订阅「学生会也有洞」"
                + ("" if is_answer else "，" + choice),
            )
            body.plan.keyword = (
                TITLE
                if native or case in {"only_category", "count_only"}
                else f"{TITLE} {TERM}"
            )
            if case == "wrong_title":
                body.plan.keyword = f"学生会 {TERM}"
            if native:
                body.plan.filters = {
                    "uploader": "ani" if case == "wrong_native" else "tea"
                }
            if case == "wrong_limit":
                body.plan.total_limit = 1
            await saved(
                first,
                "search",
                "search_indexer",
                {
                    "workflow_id": graph.id,
                    "query": {
                        "keyword": TITLE
                        if case == "wrong_search"
                        else body.plan.keyword,
                        "filters": body.plan.filters,
                        **({"page_size": 1} if case == "short_page" else {}),
                    },
                    "status": "ok",
                    "omitted_count": 2 if case == "short_page" else 0,
                    "items": []
                    if case == "empty"
                    else [{"title": TITLE, "uploader": g} for g in ["Tea", "ANi"]]
                    if case == "count_only"
                    else [
                        {"title": TITLE, "uploader": "ANi" if case == "mixed" else TERM}
                    ],
                },
            )
            old = await invoke(
                first,
                owner,
                "validate_download_plan",
                {"plan": body.plan.model_dump(mode="json")},
                "first-review",
            )
            if case == "cancelled":
                await AssistantReviewDecision.claim(
                    owner.id, UUID(old["request_id"]), False
                )
            if case == "already_created":
                from app.services.download import DownloadPlanService

                await DownloadPlanService.create_once(
                    body.model_copy(update={"request_id": UUID(old["request_id"])}),
                    owner,
                )
            turn = first
            if is_answer:
                values = [{"title": "订阅偏好", "values": [choice], "skipped": False}]
                if case == "count_only":
                    values = [{"title": "总上限", "values": ["不限"], "skipped": False}]
                if case == "two_terms":
                    values.append(
                        {"title": "画质", "values": ["1080p"], "skipped": False}
                    )
                await saved(
                    first,
                    "ask",
                    "ask_user",
                    {
                        "state": "answered",
                        "questions": [{"format": "text"}],
                        "summary": values,
                    },
                )
                turn = await AssistantTurn.create(
                    conversation_id=conversation.id,
                    request_id="answer-ask",
                    prompt=dumps(values).decode(),
                )
            latest = await invoke(
                turn,
                owner,
                "validate_download_plan",
                {"plan": body.plan.model_dump(mode="json")},
                "latest",
            )
            assert latest.get("assistant_submission", {}).get("status") == expected, (
                latest
            )
            selected = old if case == "old_review" else latest
            args = {"request_id": selected["request_id"], "confirmed": True}
            result = await invoke(turn, owner, "create_download_plan", args, "create")
            assert not result["created"]
            assert await DownloadPlan.all().count() == int(case == "already_created")

    asyncio.run(run())


@pytest.mark.parametrize(
    "case", ["consult", "declined", "generic_card", "old_card", "stale_retry"]
)
def test_answer_scope(case):
    async def run():
        async with database() as (owner, _):
            from app.services.subscription_intent import subscription_intent

            conv = await AssistantConversation.create(
                user_id=owner.id, title="Subscription"
            )
            turn = await AssistantTurn.create(
                conversation_id=conv.id,
                request_id="origin",
                status="complete",
                prompt="如何订阅《片名》？"
                if case == "consult"
                else "帮我订阅《片名》",
            )
            values = [
                {
                    "title": "喜欢什么颜色" if case == "generic_card" else "字幕组",
                    "values": ["不要创建" if case == "declined" else "蓝色"],
                    "skipped": False,
                }
            ]
            await saved(turn, "q", "ask_user", {"state": "answered", "summary": values})
            if case == "old_card":
                await AssistantTurn.create(
                    conversation_id=conv.id,
                    request_id="other",
                    status="complete",
                    prompt="看看媒体库",
                )
            answer = await AssistantTurn.create(
                conversation_id=conv.id, request_id="answer-q", prompt="继续"
            )
            if case == "stale_retry":
                await AssistantTurn.create(
                    conversation_id=conv.id,
                    request_id="later",
                    status="complete",
                    prompt="算了",
                )
                answer = await AssistantTurn.create(
                    conversation_id=conv.id,
                    request_id="retry",
                    prompt="继续",
                    retry_of=answer.id,
                )
            assert await subscription_intent(answer) is None

    asyncio.run(run())


@pytest.mark.parametrize(
    "choice",
    [
        "edition",
        "unrestricted",
        "custom",
        "mixed",
        "mixed_native",
        "wrong_media",
        "empty",
        "deferred",
        "duplicate",
        "stale_downloader",
    ],
)
def test_runtime_confirmation(monkeypatch, choice):
    fixture = {}
    calls = iter(("navigate", "search_indexer", "validate_download_plan"))

    def handler(upstream):
        name = next(calls, None)
        if name is None:
            return response(
                [chunk({"role": "assistant", "content": "Plan created."}, "stop")]
            )
        args = {
            "navigate": {"page": "download_plans"},
            "search_indexer": {
                "workflow_id": fixture["graph_id"],
                "keyword": "Lantern",
            },
            "validate_download_plan": {"plan": fixture["plan"]},
        }[name]
        return response(
            [
                chunk(
                    {
                        "role": "assistant",
                        "tool_calls": [call_delta(name, dumps(args).decode())],
                    },
                    "tool_calls",
                )
            ]
        )

    configure(monkeypatch, handler)
    search = engine_fixture(
        monkeypatch,
        {
            "items": [
                {"id": "one", "title": "【喵萌奶茶屋】 Lantern [07][简日双语]"},
                {"id": "two", "title": "[悠哈璃羽字幕社] Lantern [07][简体内嵌]"},
                {"id": "three", "title": "[悠哈璃羽字幕社] Lantern [07][简繁内封]"},
            ],
            "total": 3,
        },
    )
    if choice == "wrong_media":
        search.return_value["items"].append(
            {"id": "other", "title": "[ANi] Another Show"}
        )
        search.return_value["total"] += 1

    async def run():
        async with database() as (owner, _):
            graph, _, body = await fixture_plan(filters={})
            if choice in {"edition", "custom"}:
                library = await MediaLib.create(
                    name="番剧", lib_type="tv_show", dir="/library/anime", priority=1
                )
            if choice == "mixed_native":
                graph.definition["nodes"][0]["data"]["config"] += (
                    "  uploader:\n    type: select\n"
                    "    options: {tea: 喵萌奶茶屋, uha: 悠哈璃羽字幕社}\n"
                )
                await graph.save(update_fields=["definition"])
            fixture.update(
                graph_id=graph.id,
                plan=body.plan.model_dump(
                    mode="json", exclude_unset=choice != "custom"
                ),
            )
            conv = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            events = [
                event
                async for event in AssistantService.persistent_stream(
                    conv["id"], request("ordered", prompt="帮我订阅《Lantern》"), owner
                )
            ]
            assert events[-1].event == "done"
            results = [e.data for e in events if e.event == "tool_result"]
            assert [(r["name"], r["ok"]) for r in results] == [
                ("navigate", False),
                ("search_indexer", True),
                ("validate_download_plan", True),
                ("ask_user", True),
            ]
            assert not await DownloadPlan.all().exists()
            card = results[-1]["data"]
            question, destination = card["questions"]
            assert destination["field"] == (
                "destination" if choice in {"edition", "custom"} else "directory"
            )
            assert question["required"] and not question["multiple"]
            assert {option["label"] for option in question["options"]} == {
                "喵萌奶茶屋",
                "悠哈璃羽字幕社 · 简体内嵌",
                "悠哈璃羽字幕社 · 简繁内封",
                "不限字幕组或版本",
            }
            assert card["state"] == "pending"
            download.notify_download_changes.assert_not_called()
            card_id = results[-1]["id"]

            location = {"selected": [destination["options"][-1]["id"]]}
            if choice == "custom":
                location = {"custom": "/downloads/自填 | 番剧"}

            async def answer(**value):
                return await answer_card(
                    conv["id"],
                    card_id,
                    AnswerGroup.model_validate(
                        {
                            "version": card["version"],
                            "items": [
                                {
                                    "id": destination["id"],
                                    **location,
                                },
                                {"id": question["id"], **value},
                            ],
                        }
                    ),
                    owner,
                )

            if choice == "custom":
                location["custom"] = "/downloads/自填 | 不存在"
                assert await answer(skipped=True) == {
                    "issue": "destination",
                    "id": destination["id"],
                }
                location["custom"] = "/downloads/自填 | 番剧"
            deferred = await answer(skipped=True)
            assert deferred["card"]["state"] == "deferred"
            with pytest.raises(BadRequestException):
                await continuation_prompt(conv["id"], card_id, owner)
            preview = results[-2]
            assert (await assistant_review(preview["id"], owner))[
                "status"
            ] == "superseded"
            if choice == "deferred":
                turn = await AssistantTurn.create(
                    conversation_id=conv["id"], request_id="text", prompt="确认创建"
                )
                bypass = await invoke(
                    turn,
                    owner,
                    "create_download_plan",
                    {"request_id": preview["data"]["request_id"], "confirmed": True},
                    "create",
                )
                assert not bypass["created"] and not await DownloadPlan.exists()
                download.notify_download_changes.assert_not_called()
                return
            choices = {o["label"]: o["id"] for o in question["options"]}
            if choice in {"custom", "empty"}:
                selected = {
                    "custom": "喵萌奶茶屋" if choice == "custom" else "星夜字幕社"
                }
            else:
                label = (
                    "不限字幕组或版本"
                    if choice in {"unrestricted", "duplicate"}
                    else "悠哈璃羽字幕社 · 简繁内封"
                )
                selected = {"selected": [choices[label]]}
            search.reset_mock()
            driver = DraftDriver()
            driver.supports_version = False
            driver.close = AsyncMock()
            monkeypatch.setattr(syncer, "load_driver", lambda _: driver)
            await User.filter(id=owner.id).update(role="user")
            with pytest.raises(ForbiddenException):
                await answer(**selected)
            await User.filter(id=owner.id).update(role="admin")
            assert await answer(custom="取消") == {
                "issue": "invalid",
                "id": question["id"],
            }
            assert not await DownloadPlan.exists()

            if choice == "custom":
                for cap in (0, 10**30):
                    assert await answer(custom=f"喵萌奶茶屋，总上限{cap}") == {
                        "issue": "invalid",
                        "id": question["id"],
                    }
            if choice == "duplicate":
                existing = await download.DownloadPlanService.create_once(body, owner)
                download.notify_download_changes.reset_mock()
            if choice == "stale_downloader":
                await Downloader.filter(id=body.plan.downloader_id).delete()
            if choice in {"duplicate", "stale_downloader"}:
                answered = await answer(**selected)
                submission = answered["card"]["subscription"]["submission"]
                assert not submission["created"] and not submission["valid"]
                assert await answer(**selected) == answered
                events = [
                    event
                    async for event in AssistantService.persistent_stream(
                        conv["id"],
                        request(f"answer-{card_id}", answer_to=card_id),
                        owner,
                    )
                ]
                assert events[-1].event == "done"
                summary = "".join(
                    e.data["text"] for e in events if e.event == "text_delta"
                )
                if choice == "duplicate":
                    assert submission["duplicate_plan_id"] == existing["plan_id"]
                    assert await DownloadPlan.all().count() == 1
                    assert "未重复创建" in summary
                else:
                    assert not await DownloadPlan.exists() and "参数已失效" in summary
                search.assert_not_awaited()
                download.notify_download_changes.assert_not_called()
                return

            answered, replay = await asyncio.gather(
                answer(**selected), answer(**selected)
            )
            assert replay == answered
            plan = await DownloadPlan.get()
            submission = answered["card"]["subscription"]["submission"]
            assert submission["created"] and submission["plan_id"] == plan.id
            assert (await assistant_review(submission["review_id"], owner))[
                "status"
            ] == "applied"
            download.notify_download_changes.assert_called_once()
            search.assert_not_awaited()
            assert plan.creation_receipt["first_execution"] == {"status": "queued"}
            expected = {
                "unrestricted": "Lantern",
                "custom": "Lantern 喵萌奶茶屋",
                "mixed_native": "Lantern 简繁内封",
                "empty": "Lantern 星夜字幕社",
            }.get(choice, "Lantern 悠哈璃羽字幕社 简繁内封")
            assert plan.keyword == expected
            if choice == "mixed_native":
                assert plan.filters == {"uploader": "uha"}
            if choice in {"custom", "edition"}:
                assert plan.transfer_lib_id == library.id
                assert plan.transfer_method == "hardlink"
                assert (plan.sub_pattern, plan.sub_repl) == ("^(.+/)?", "{{title}}/")
            else:
                assert (plan.sub_pattern, plan.sub_repl) == (None, None)
            assert plan.batch_limit == body.plan.batch_limit
            assert plan.dir == (
                "/downloads/自填" if choice == "custom" else body.plan.dir
            )
            assert plan.total_limit == body.plan.total_limit

            work = []
            if choice != "edition":
                rows = {
                    "unrestricted": [{"link": "a" * 40}],
                    "mixed": [
                        {
                            "title": "[悠哈璃羽字幕社] Lantern [简繁内封]",
                            "link": "a" * 40,
                        },
                        {"title": "[Other] Lantern", "link": "b" * 40},
                    ],
                    "mixed_native": [
                        {
                            "title": "[悠哈璃羽字幕社] Lantern [简繁内封]",
                            "link": "a" * 40,
                        }
                    ],
                    "wrong_media": [
                        {
                            "title": "[悠哈璃羽字幕社] Another Show [简繁内封]",
                            "link": "a" * 40,
                        }
                    ],
                }.get(choice, [])
                search.side_effect = [{"items": rows}, {"items": []}]
                work.append(
                    asyncio.create_task(syncer.check_download_plans(first_only=True))
                )

            if choice == "edition":
                monkeypatch.setattr(
                    AIProviderService,
                    "resolve_model",
                    AsyncMock(
                        side_effect=AssertionError(
                            "Receipt reporting must work offline."
                        )
                    ),
                )
            events = [
                e
                async for e in AssistantService.persistent_stream(
                    conv["id"], request(f"answer-{card_id}", answer_to=card_id), owner
                )
            ]
            assert events[-1].event == "done", events[-1]
            assert [e.data["name"] for e in events if e.event == "tool_result"] == [
                "create_download_plan"
            ]
            summary = "".join(e.data["text"] for e in events if e.event == "text_delta")
            await asyncio.gather(*work)
            assert f"已创建下载计划 #{plan.id}" in summary
            if work:
                assert search.await_count >= 1
                assert all(
                    call.kwargs["bootparams"]["keyword"] == expected
                    for call in search.await_args_list
                )
                await plan.refresh_from_db()
                outcome = plan.creation_receipt["first_execution"]
                if choice in {"unrestricted", "mixed", "mixed_native"}:
                    assert "首轮已提交 1 个下载" in summary and outcome["added"] == 1
                else:
                    assert "首轮已提交 0 个下载" in summary and outcome["added"] == 0
                    assert outcome["stage"] == (
                        "release_filtered"
                        if choice == "wrong_media"
                        else "no_search_results"
                    )
                    assert (
                        "发布条件" if choice == "wrong_media" else "没有搜到资源"
                    ) in summary
            else:
                search.assert_not_awaited()
            if choice == "edition":
                search.side_effect = [
                    {
                        "items": [
                            {
                                "title": (
                                    "[Other] Lantern [简体内嵌] "
                                    "(悠哈璃羽字幕社 简繁内封)"
                                ),
                                "link": "a" * 40,
                            },
                            {
                                "title": "[悠哈璃羽字幕社] Lantern [简繁内封]",
                                "link": "b" * 40,
                            },
                        ]
                    },
                    {"items": []},
                ]
                await syncer.check_download_plans(first_only=True)
                await plan.refresh_from_db()
                outcome = plan.creation_receipt["first_execution"]
                assert outcome["added"] == outcome["skipped_release"] == 1
                assert await download.DownloadTask.all().values_list(
                    "info_hash", flat=True
                ) == ["b" * 40]
                search.side_effect = [{"items": []}]
                await syncer.execute_download_plan(plan, driver)
                item = (
                    await download.DownloadPlanService.list_plans(
                        DownloadPlanQuery(), owner
                    )
                )["items"][0]
                assert item["last_execution"]["stage"] == "no_search_results"
                assert item["last_execution"]["added"] == 0
                for _ in range(2):
                    restored = (await Sessions.turns(conv["id"], owner))["items"][-1]
                    assert restored["content"].count("首轮已提交 1 个下载") == 1
                    assert (
                        restored["tools"][-1]["result"]["data"]["follow_first_check"]
                        is False
                    )

    asyncio.run(run())


@pytest.mark.parametrize(
    "preference,expected",
    [
        ("", "needs_approval"),
        ("无需入库", "needs_approval"),
        ("无需转存到 Anime", "needs_approval"),
        ("转存 Movies", "needs_approval"),
        ("看看 Anime，转存到 Anime Archives", "needs_approval"),
        ("转存", "needs_approval"),
        ("转存到 Anime 媒体库", "needs_confirmation"),
        ("媒体库选用 Anime", "needs_confirmation"),
    ],
)
def test_optional_transfer(preference, expected):
    async def run():
        async with database() as (owner, _):
            lib = await MediaLib.create(
                name="Anime", lib_type="tv_show", dir="/synthetic/Anime", priority=1
            )
            graph, _, body = await fixture_plan(
                transfer_lib_id=lib.id, transfer_method="hardlink"
            )
            conv = await AssistantConversation.create(
                user_id=owner.id, title="Subscription"
            )
            turn = await AssistantTurn.create(
                conversation_id=conv.id,
                request_id="transfer",
                prompt=f"帮我订阅《Lantern》。{preference}",
            )
            await saved(
                turn,
                "search",
                "search_indexer",
                {
                    "workflow_id": graph.id,
                    "query": {"keyword": "Lantern", "filters": body.plan.filters},
                    "status": "ok",
                    "items": [{"id": "one", "title": "Lantern"}],
                },
            )
            checked = await invoke(
                turn,
                owner,
                "validate_download_plan",
                {"plan": body.plan.model_dump(mode="json")},
                "review",
            )
            assert checked["assistant_submission"]["status"] == expected
            result = await invoke(
                turn,
                owner,
                "create_download_plan",
                {"request_id": checked["request_id"], "confirmed": True},
                "create",
            )
            assert result["created"] is (expected == "ready")
            assert await DownloadPlan.all().count() == int(expected == "ready")

    asyncio.run(run())


def test_destination_access():
    from app.models.user import Permissions, UserRole
    from app.services.subscription_destinations import (
        destination_questions,
        resolve_destination,
    )

    async def run():
        async with database() as (owner, _):
            visible = await MediaLib.create(
                name="TV", dir="/tv", lib_type="tv_show", priority=1
            )
            hidden = await MediaLib.create(
                name="Other", dir="/other", lib_type="movie", priority=2
            )
            owner.role = UserRole.USER
            owner.perms = Permissions(media_lib_ids=[visible.id])
            plan = {"dir": "/downloads"}
            (q,) = await destination_questions(
                plan, "save to /remote/downloads", owner, chinese=False
            )
            assert q["field"] == "destination"
            assert [o["value"] for o in q["options"]] == [None, visible.id]
            q["options"] = [{**o, "id": str(i)} for i, o in enumerate(q["options"])]
            for reference in ("Other", f"#{hidden.id}", "relative | TV"):
                answer = {"custom": reference, "selected": []}
                assert await resolve_destination(q, answer, plan, owner) is None
            for value in ("TV", f"#{visible.id}"):
                answer = {"custom": f"/target | {value}", "selected": []}
                patch = await resolve_destination(q, answer, plan, owner)
                assert (
                    patch["transfer_lib_id"] == visible.id and patch["dir"] == "/target"
                )
                assert (patch["sub_pattern"], patch["sub_repl"]) == (
                    "^(.+/)?",
                    "{{title}}/",
                )
            answer = {"custom": "", "selected": ["0"]}
            assert not (await resolve_destination(q, answer, plan, owner)).get(
                "sub_pattern"
            )
            legacy = {k: v for k, v in q.items() if k != "naming"}
            patch = await resolve_destination(
                legacy, {"custom": "TV", "selected": []}, plan, owner
            )
            assert (patch["sub_pattern"], patch["sub_repl"]) == (None, None)
            (disabled,) = await destination_questions(
                {**plan, "sub_pattern": ""}, "subscribe", owner, chinese=False
            )
            assert "naming" not in disabled
            await visible.delete()
            assert (
                await resolve_destination(
                    q, {"custom": "", "selected": ["1"]}, plan, owner
                )
                is None
            )

    asyncio.run(run())


@pytest.mark.parametrize(
    "naming, expected",
    [
        ({}, ("^(.+/)?", "{{title}}/")),
        ({"sub_pattern": None, "sub_repl": None}, (None, None)),
        ({"sub_pattern": "^Raw/", "sub_repl": ""}, ("^Raw/", "")),
    ],
)
def test_library_plan_defaults(naming, expected):
    async def run():
        async with database() as (owner, _):
            lib = await MediaLib.create(
                name="Anime", lib_type="tv_show", dir="/synthetic/Anime", priority=1
            )
            _, _, body = await fixture_plan(
                transfer_lib_id=lib.id, transfer_method="hardlink", **naming
            )
            conversation = await AssistantConversation.create(
                user_id=owner.id, title="Preview"
            )
            turn = await AssistantTurn.create(
                conversation_id=conversation.id,
                request_id="preview",
                prompt="预览下载计划",
            )
            settings = body.plan.model_dump(
                mode="json", exclude_unset=True, exclude={"batch_limit"}
            )
            checked = await invoke(
                turn, owner, "validate_download_plan", {"plan": settings}, "check"
            )

            assert checked["valid"]
            plan = checked["plan"]
            assert plan["batch_limit"] == 12
            assert (plan["sub_pattern"], plan["sub_repl"]) == expected

    asyncio.run(run())


def test_legacy_release_card():
    from app.services.subscription_intent import confirmed_subscription_plan

    async def run():
        async with database() as (owner, _):
            _, _, body = await fixture_plan(filters={})
            card = {
                "state": "answered",
                "subscription": {
                    "plan": body.plan.model_dump(mode="json"),
                    "title": "Lantern",
                },
                "questions": [
                    {
                        "id": "q1",
                        "options": [{"id": "o1", "terms": ["Tea"], "observed": True}],
                    }
                ],
                "answers": [
                    {"id": "q1", "selected": ["o1"], "custom": "", "skipped": False}
                ],
            }

            resolved = await confirmed_subscription_plan(card, owner)
            assert resolved["plan"]["keyword"] == "Lantern Tea"
            assert resolved["plan"]["dir"] == body.plan.dir
            assert resolved["plan"]["transfer_lib_id"] is None

    asyncio.run(run())


def test_revised_settings():
    async def run():
        async with database() as (owner, _):
            _, _, body = await fixture_plan(
                filters={"category": "tv", "uploader": "old"}
            )
            base = {"title": "Lantern", "plan": body.plan.model_dump(mode="json")}
            intent = SubscriptionIntent(
                "帮我订阅《Lantern》", [], [], False, revision=base
            )
            draft = await revised_subscription_plan(
                intent,
                "我改主意了，只要 ANi 的简体内嵌，"
                "下载到 /changed，不用入库。请按新条件帮我订好。",
                owner,
            )
            assert draft["plan"]["keyword"] == "Lantern"
            assert draft["plan"]["dir"] == "/changed"
            assert draft["plan"]["filters"] == {"category": "tv"}
            assert draft["plan"]["transfer_lib_id"] is None
            assert base["plan"]["dir"] == body.plan.dir
            assert (
                await revised_subscription_plan(intent, "改成每六小时下载", owner)
                is None
            )
            assert await DownloadPlan.all().count() == 0

    asyncio.run(run())
