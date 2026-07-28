"""Built-in non-JIT family templates for the packaged verifier."""

from __future__ import annotations

from typing import Mapping

from .contract_router import FamilyTemplateRouteCandidate
from .family_template import (
    FamilyTemplateG1BindingIR,
    FamilyTemplateIR,
    FamilyTemplateParameterIR,
    FamilyTemplateParameterKind,
    FamilyTemplateParameterValue,
    FamilyTemplateRoiIR,
)
from .models import (
    CriterionIR,
    EvidenceCapability,
    G1CheckerKind,
    TemporalSemantics,
)
from .task_spec import TaskSpec


FAMILY_TEMPLATE_VERSION = "mobiagent-built-in-task-families-v3"


_PROCESS_IDS = {
    "search_results": "process.query_submitted",
    "open_detail": "process.detail_requested",
    "select_control": "process.control_requested",
    "creator_homepage": "process.creator_requested",
    "playback": "process.playback_requested",
    "location_search": "process.location_requested",
    "route_preview": "process.route_requested",
    "cross_app_entity_transfer": "process.cross_app_transfer_requested",
    "composite_workflow": "process.workflow_requested",
}


def _criterion_set(task_family: str) -> tuple[CriterionIR, ...]:
    screenshot = (EvidenceCapability.SCREENSHOT,)
    actions = (EvidenceCapability.ACTIONS,)
    return (
        CriterionIR(
            "trace.integrity",
            TemporalSemantics.PROCESS_OBLIGATION,
            required=True,
            description="Trace acquisition is intact enough for verification",
        ),
        CriterionIR(
            _PROCESS_IDS[task_family],
            TemporalSemantics.PROCESS_OBLIGATION,
            required=True,
            required_capabilities=actions,
            description="Required observable interaction was attempted",
        ),
        CriterionIR(
            "outcome.page_domain_semantics",
            TemporalSemantics.PERSISTENT_STATE,
            required=True,
            required_capabilities=screenshot,
            description=(
                "The terminal UI is in the task-appropriate generic page domain "
                "and not in an unrelated vertical or surface"
            ),
        ),
        CriterionIR(
            "outcome.task_semantics",
            TemporalSemantics.PERSISTENT_STATE,
            required=True,
            required_capabilities=screenshot,
            description="The stable terminal UI visibly satisfies the task",
        ),
        CriterionIR(
            "negative.forbidden_state_absent",
            TemporalSemantics.PERSISTENT_STATE,
            required=True,
            required_capabilities=screenshot,
            description="No task-prohibited terminal state is visible",
        ),
        CriterionIR(
            "termination.done_after_outcome",
            TemporalSemantics.PROCESS_OBLIGATION,
            required=True,
            required_capabilities=actions,
            description="Done is declared only after observable task evidence",
        ),
        CriterionIR(
            "quality.not_loading",
            TemporalSemantics.PERSISTENT_STATE,
            required=False,
            required_capabilities=screenshot,
        ),
        CriterionIR(
            "quality.no_blocking_overlay",
            TemporalSemantics.PERSISTENT_STATE,
            required=False,
            required_capabilities=screenshot,
        ),
    )


_PAGE_DOMAIN_RULES = {
    "search_results": {
        "expected": (
            {
                "domain_id": "in_app_search_results",
                "description": "an in-app search results or discovery results surface",
                "markers_any": ("搜索", "综合", "相关", "结果"),
            },
            {
                "domain_id": "matched_result_landing_surface",
                "description": (
                    "a content, item, place, creator, or media detail/playback "
                    "surface reached as the selected result for the requested search"
                ),
                "markers_any": (
                    "详情",
                    "播放",
                    "暂停",
                    "评论",
                    "点赞",
                    "收藏",
                    "关注",
                    "简介",
                    "价格",
                    "地址",
                    "路线",
                    "电话",
                    "主页",
                    "作品",
                    "笔记",
                    "视频",
                ),
            },
        ),
        "prohibited": (
            {
                "domain_id": "unrelated_vertical",
                "description": "a vertical unrelated to the requested search intent",
                "markers_any": (),
            },
        ),
    },
    "open_detail": {
        "expected": (
            {
                "domain_id": "item_detail",
                "description": (
                    "a normal item/product/content detail surface with title, price "
                    "or primary action affordance as appropriate to the app"
                ),
                "markers_any": ("详情", "价格", "￥", "¥", "加入购物车", "立即购买", "去购买"),
            },
        ),
        "prohibited": (
            {
                "domain_id": "local_service_or_delivery_vertical",
                "description": (
                    "food delivery, instant commerce, local services, taxi hailing, "
                    "or another transactional vertical unrelated to ordinary item detail"
                ),
                "markers_any": ("外卖", "美食", "闪购", "买菜", "附近", "打车", "用车"),
            },
            {
                "domain_id": "checkout_or_order_commitment",
                "description": "cart, checkout, payment, or order placement surface",
                "markers_any": ("提交订单", "确认支付", "支付", "收银台", "结算"),
            },
        ),
    },
    "select_control": {
        "expected": (
            {
                "domain_id": "results_with_selectable_controls",
                "description": (
                    "a loaded results surface exposing filters, tabs, sort controls, "
                    "or selectable result controls relevant to the task"
                ),
                "markers_any": ("筛选", "排序", "销量", "综合", "天猫", "价格", "商品"),
            },
        ),
        "prohibited": (
            {
                "domain_id": "local_service_or_delivery_vertical",
                "description": (
                    "food delivery, instant commerce, local services, taxi hailing, "
                    "or another vertical where the requested result controls do not apply"
                ),
                "markers_any": ("外卖", "美食", "闪购", "买菜", "附近", "打车", "用车"),
            },
            {
                "domain_id": "detail_or_checkout_surface",
                "description": "a detail, cart, checkout, payment, or order placement surface",
                "markers_any": ("商品详情", "提交订单", "确认支付", "支付", "收银台", "结算"),
            },
        ),
    },
    "creator_homepage": {
        "expected": (
            {
                "domain_id": "creator_profile_home",
                "description": "a creator/user profile home surface",
                "markers_any": ("主页", "动态", "投稿", "作品", "笔记", "关注", "粉丝"),
            },
        ),
        "prohibited": (),
    },
    "playback": {
        "expected": (
            {
                "domain_id": "media_playback",
                "description": "a media playback surface or system media capsule",
                "markers_any": ("播放", "暂停", "歌曲", "单曲", "正在播放"),
            },
        ),
        "prohibited": (),
    },
    "location_search": {
        "expected": (
            {
                "domain_id": "place_result_or_detail",
                "description": "a place search result or place detail surface",
                "markers_any": ("地点", "地址", "路线", "导航", "电话", "营业"),
            },
        ),
        "prohibited": (
            {
                "domain_id": "active_navigation_or_transport_booking",
                "description": "active navigation, taxi hailing, ride booking, or order commitment",
                "markers_any": ("开始导航", "退出导航", "打车", "呼叫", "叫车", "立即用车"),
            },
        ),
    },
    "route_preview": {
        "expected": (
            {
                "domain_id": "route_preview",
                "description": "a route planning preview with loaded route options",
                "markers_all": ("路线",),
                "markers_any": ("公里", "分钟", "驾车", "公交", "步行", "骑行"),
            },
        ),
        "prohibited": (
            {
                "domain_id": "taxi_hailing_or_active_navigation",
                "description": "taxi hailing, ride booking, or already-started navigation",
                "markers_any": ("打车", "叫车", "呼叫", "立即用车", "退出导航", "开始导航"),
            },
        ),
    },
    "cross_app_entity_transfer": {
        "expected": (
            {
                "domain_id": "target_app_search_results",
                "description": (
                    "the target app's final surface is a loaded search/results surface "
                    "for the transferred entity or category"
                ),
                "markers_any": ("搜索", "综合", "相关", "结果", "商品", "笔记", "视频"),
            },
        ),
        "prohibited": (
            {
                "domain_id": "unrelated_target_vertical",
                "description": (
                    "an unrelated target-app vertical such as delivery, local services, "
                    "taxi hailing, checkout, payment, or another non-requested workflow"
                ),
                "markers_any": ("外卖", "美食", "闪购", "买菜", "附近", "打车", "叫车", "支付", "结算"),
            },
            {
                "domain_id": "opened_detail_or_commitment",
                "description": "a detail page or commitment surface when the task asks only to search/read",
                "markers_any": ("商品详情", "提交订单", "确认支付", "收银台"),
            },
        ),
    },
    "composite_workflow": {
        "expected": (
            {
                "domain_id": "workflow_terminal_state",
                "description": (
                    "the terminal surface matches the final requested workflow step, "
                    "such as a result list, selected control state, detail surface, "
                    "cart confirmation, playback state, place page, or route preview"
                ),
                "markers_any": ("结果", "商品", "详情", "已选", "购物车", "播放", "路线"),
            },
        ),
        "prohibited": (
            {
                "domain_id": "unrequested_commitment_or_unrelated_vertical",
                "description": (
                    "payment, order submission, active navigation, or an unrelated "
                    "vertical not requested by the workflow"
                ),
                "markers_any": ("支付", "收银台", "提交订单", "确认订单", "退出导航", "外卖", "打车"),
            },
        ),
    },
}


def _merged_page_domain_rules(task: TaskSpec, base: dict) -> dict:
    intent = task.parsed_intent if isinstance(task.parsed_intent, Mapping) else {}
    steps = tuple(str(item) for item in intent.get("workflow_steps", ()) if str(item))
    forbidden = tuple(str(item) for item in intent.get("forbidden_states", ()) if str(item))
    expected = list(base.get("expected", ()))
    prohibited = list(base.get("prohibited", ()))
    if steps:
        final_step = steps[-1]
        final_domains = {
            "add_to_cart": {
                "domain_id": "cart_addition_confirmed",
                "description": "a cart addition confirmation, cart badge update, or cart state after adding an item",
                "markers_any": ("已加入购物车", "加入购物车成功", "购物车", "已加购"),
            },
            "open_detail": {
                "domain_id": "item_detail",
                "description": "a normal item/product/content detail surface",
                "markers_any": ("详情", "价格", "￥", "¥", "加入购物车", "立即购买", "去购买"),
            },
            "apply_rank_or_filter": {
                "domain_id": "results_with_selected_controls",
                "description": "a loaded result list with the requested sort/filter controls selected",
                "markers_any": ("销量", "排序", "筛选", "综合", "已选", "商品"),
            },
            "search_query": {
                "domain_id": "in_app_search_results",
                "description": "a loaded in-app search results surface",
                "markers_any": ("搜索", "综合", "相关", "结果", "商品"),
            },
            "media_playback": {
                "domain_id": "media_playback",
                "description": "a media playback surface or system media capsule",
                "markers_any": ("播放", "暂停", "歌曲", "单曲", "正在播放"),
            },
            "place_lookup": {
                "domain_id": "place_result_or_detail",
                "description": "a place search result or place detail surface",
                "markers_any": ("地点", "地址", "路线", "导航", "电话", "营业"),
            },
            "route_preview": {
                "domain_id": "route_preview",
                "description": "a route planning preview with loaded route options",
                "markers_all": ("路线",),
                "markers_any": ("公里", "分钟", "驾车", "公交", "步行", "骑行"),
            },
        }
        if final_step in final_domains:
            expected = [final_domains[final_step]]
    if any(item in forbidden for item in ("checkout", "payment", "order_submission")):
        prohibited.append(
            {
                "domain_id": "checkout_payment_or_order_submission",
                "description": "checkout, payment, or order submission surfaces",
                "markers_any": ("支付", "收银台", "提交订单", "确认订单", "结算", "付款"),
            }
        )
    if "active_navigation" in forbidden:
        prohibited.append(
            {
                "domain_id": "active_navigation",
                "description": "already-started navigation state",
                "markers_any": ("退出导航", "继续导航", "开始导航", "导航中"),
            }
        )
    if "detail_page" in forbidden:
        prohibited.append(
            {
                "domain_id": "detail_page",
                "description": "a detail page explicitly forbidden by the task",
                "markers_any": ("详情", "商品详情", "加入购物车", "立即购买"),
            }
        )
    return {"expected": tuple(expected), "prohibited": tuple(prohibited)}


def family_template(task_family: str) -> FamilyTemplateIR:
    if task_family not in _PROCESS_IDS:
        raise ValueError(f"no built-in family template for {task_family!r}")
    return FamilyTemplateIR(
        template_id=f"mobiagent.{task_family}",
        version=FAMILY_TEMPLATE_VERSION,
        task_family=task_family,
        parameters=(
            FamilyTemplateParameterIR(
                "terminal_roi", FamilyTemplateParameterKind.NORMALIZED_ROI
            ),
        ),
        criteria=_criterion_set(task_family),
        required_capabilities=(
            EvidenceCapability.SCREENSHOT,
            EvidenceCapability.ACTIONS,
        ),
        g1_bindings=(
            FamilyTemplateG1BindingIR(
                "quality.not_loading",
                G1CheckerKind.NOT_LOADING,
                (FamilyTemplateRoiIR("terminal", "terminal_roi"),),
            ),
            FamilyTemplateG1BindingIR(
                "quality.no_blocking_overlay",
                G1CheckerKind.NO_BLOCKING_OVERLAY,
                (FamilyTemplateRoiIR("terminal-overlay", "terminal_roi"),),
            ),
        ),
        metadata={
            **(
                {
                    "state_evidence": {
                        "outcome.task_semantics": {
                            "desired_state": "selected",
                            "anchor_source": "task_control_phrase",
                            "frame_scope": "terminal",
                            "allow_vlm": True,
                        }
                    }
                }
                if task_family == "select_control"
                else {}
            ),
            "page_domain_semantics": {
                "outcome.page_domain_semantics": _PAGE_DOMAIN_RULES[task_family],
            },
        },
    )


def route_candidate(task: TaskSpec) -> FamilyTemplateRouteCandidate:
    task.validate()
    template = family_template(task.task_family)
    metadata = dict(template.metadata)
    if task.parsed_intent:
        metadata["task_intent"] = dict(task.parsed_intent)
        page_domains = dict(metadata.get("page_domain_semantics", {}))
        criterion_id = "outcome.page_domain_semantics"
        if task.task_family == "composite_workflow" and criterion_id in page_domains:
            page_domains[criterion_id] = _merged_page_domain_rules(
                task, dict(page_domains[criterion_id])
            )
            metadata["page_domain_semantics"] = page_domains
    template = FamilyTemplateIR(
        template_id=template.template_id,
        version=template.version,
        task_family=template.task_family,
        parameters=template.parameters,
        criteria=template.criteria,
        required_capabilities=template.required_capabilities,
        g1_bindings=template.g1_bindings,
        metadata=metadata,
    )
    return FamilyTemplateRouteCandidate(
        selection_key=task.selection_key,
        template=template,
        parameters=(
            FamilyTemplateParameterValue(
                "terminal_roi",
                FamilyTemplateParameterKind.NORMALIZED_ROI,
                (0.0, 0.0, 1.0, 1.0),
            ),
        ),
    )


__all__ = ["FAMILY_TEMPLATE_VERSION", "family_template", "route_candidate"]
