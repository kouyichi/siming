"""P4 声明式 v6.2 CLI 子命令注册表（cli_registry.py）：让新领域能力按域自注册，不再往 cli.py/cli_v5.py 堆巨型分支。"""

from __future__ import annotations

import argparse
import json
from typing import Any, Callable

from siming.db import SimingDB

HandlerFn = Callable[[SimingDB, argparse.Namespace], dict[str, Any]]


def register_v6_subcommands(sub: Any) -> dict[str, HandlerFn]:
    handlers: dict[str, HandlerFn] = {}

    p_act = sub.add_parser("v6-activity", help="查看「今日替我做了什么」真实收据与活动流")
    p_act.add_argument("--day", default="", help="日期 YYYY-MM-DD（默认逻辑日）")

    def _h_activity(db: SimingDB, args: argparse.Namespace) -> dict[str, Any]:
        from siming.learning_loop import get_today_activity_feed

        return get_today_activity_feed(db, day_prefix=args.day or None)

    handlers["v6-activity"] = _h_activity

    p_undo = sub.add_parser("v6-undo", help="逐项补偿撤销（queued_notice / correction / policy_version / pending_approval / sent_notice / order）")
    p_undo.add_argument("undo_kind")
    p_undo.add_argument("target_id")
    p_undo.add_argument("--note", default="")

    def _h_undo(db: SimingDB, args: argparse.Namespace) -> dict[str, Any]:
        from siming.learning_loop import undo_activity_item

        return undo_activity_item(db, undo_kind=args.undo_kind, target_id=args.target_id, note=args.note)

    handlers["v6-undo"] = _h_undo

    p_cor = sub.add_parser("v6-correct", help="录入一条持久化人工纠偏（跨重启与再采集生效）")
    p_cor.add_argument("target_object")
    p_cor.add_argument("field_name")
    p_cor.add_argument("new_value")
    p_cor.add_argument("--scope", default="exact")
    p_cor.add_argument("--intent", default="")

    def _h_correct(db: SimingDB, args: argparse.Namespace) -> dict[str, Any]:
        from siming.learning_loop import add_correction

        return add_correction(
            db,
            user_intent=args.intent or f"{args.target_object}.{args.field_name}={args.new_value}",
            target_object=args.target_object,
            field_name=args.field_name,
            new_value=args.new_value,
            scope=args.scope,
        )

    handlers["v6-correct"] = _h_correct

    p_cap = sub.add_parser("v6-capabilities", help="查看三维能力生命周期状态（user_intent × readiness × notify_policy）")

    def _h_caps(db: SimingDB, _args: argparse.Namespace) -> dict[str, Any]:
        from siming.tower_projections import capabilities_projection

        return capabilities_projection(db)

    handlers["v6-capabilities"] = _h_caps

    p_rec = sub.add_parser("v6-recall", help="按用途（food / shopping / dev）召回收藏夹候选")
    p_rec.add_argument("purpose", choices=["food", "shopping", "dev"])
    p_rec.add_argument("--context", default="")
    p_rec.add_argument("--limit", type=int, default=5)

    def _h_recall(db: SimingDB, args: argparse.Namespace) -> dict[str, Any]:
        from siming.bookmark_recall import retrieve_candidates

        cands = retrieve_candidates(db, purpose=args.purpose, context=args.context, limit=args.limit)
        return {"ok": True, "purpose": args.purpose, "count": len(cands), "candidates": cands}

    handlers["v6-recall"] = _h_recall

    p_pay = sub.add_parser("v6-reconcile-pay", help="对账处于 RECONCILING 的美团支付意图（绝不重复提交）")
    p_pay.add_argument("intent_id")

    def _h_reconcile(db: SimingDB, args: argparse.Namespace) -> dict[str, Any]:
        from siming.meituan_pay import reconcile_payment_intent

        return reconcile_payment_intent(db, args.intent_id)

    handlers["v6-reconcile-pay"] = _h_reconcile

    p_ics = sub.add_parser("v6-ics-export", help="导出司命活跃义务与日程的标准只读 .ics")

    def _h_ics(db: SimingDB, _args: argparse.Namespace) -> dict[str, Any]:
        from siming.caldav_sync import export_siming_ics

        return {"ok": True, "ics": export_siming_ics(db)}

    handlers["v6-ics-export"] = _h_ics

    return handlers


def try_dispatch_v6_command(db: SimingDB, args: argparse.Namespace, handlers: dict[str, HandlerFn]) -> int | None:
    fn = handlers.get(str(getattr(args, "cmd", "")))
    if fn is None:
        return None
    res = fn(db, args)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok", True) else 1
