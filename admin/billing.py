"""Bounded background billing collection with explicit completeness metadata."""
from datetime import datetime, timedelta, timezone
import json
import time

PAID_CODES = ["TCACA_code_002_AkiJS3ZHF5", "TCACA_code_023_4xbGhMrE6q",
              "TCACA_code_026_BaESVICNoi", "TCACA_code_027_0FCGVA6vSa",
              "TCACA_code_009_0XmEQc2xOf", "TCACA_code_038_OhvqZtiPKr",
              "TCACA_code_003_FAnt7lcmRT", "TCACA_code_036_lupO5WgNdG"]
FREE_CODES = ["TCACA_code_008_cfWoLwvjU4", "TCACA_code_007_nzdH5h4Nl0",
              "TCACA_code_028_NtpWi0jzXs", "TCACA_code_029_6wCGEWquYy",
              "TCACA_code_030_BjSt89qTvr", "TCACA_code_001_PqouKr6QWV",
              "TCACA_code_006_DbXS0lrypC", "TCACA_code_035_ArVxJcGDsm",
              "TCACA_code_037_WxOD3MpI2o", "TCACA_code_039_KRcQj7wUat",
              "TCACA_code_040_mi9rCYg46x"]


class BillingPackages(list):
    def __init__(self, rows, metadata):
        super().__init__(rows)
        self.metadata = metadata


def _body(response):
    node = response
    for _ in range(5):
        if not isinstance(node, dict) or node.get("Error") or node.get("code") not in (None, 0, "0"):
            raise ValueError("billing_error_envelope")
        if any(k in node for k in ("Accounts", "Packages")):
            return node
        node = next((node[k] for k in ("data", "Response", "Data") if isinstance(node.get(k), dict)), None)
    raise ValueError("billing_missing_resources")


def _identity(row):
    for field in ("ResourceId", "AccountId", "PackageId"):
        if row.get(field) not in (None, ""):
            return (field, str(row[field]))
    # A code alone is not an identity: users can own multiple copies of a package.
    return ("fields", row.get("PackageCode"), row.get("DeductionStartTime"),
            row.get("DeductionEndTime"), row.get("CycleStartTime"), row.get("CycleEndTime"),
            json.dumps(row, sort_keys=True, ensure_ascii=True))


def collect_billing(request):
    sources = {}
    summary = []
    details = []
    deadline = time.monotonic() + 45
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    for name in ("summary", "paid", "free"):
        state = {"ok": False, "complete": False, "pages": 0, "rows": 0}
        sources[name] = state
        collected = []
        try:
            seen_pages = set()
            for page in range(1, 11):
                if time.monotonic() > deadline:
                    raise ValueError("collection_budget_exhausted")
                body = {} if name == "summary" else {
                    "PageNumber": page, "PageSize": 200, "Status": [0, 3],
                    "PackageCodes": PAID_CODES if name == "paid" else FREE_CODES}
                if name == "paid":
                    body["NeedRenewInfo"] = True
                elif name == "free":
                    body.update(SlicePeriodStartTime=today + " 00:00:00",
                                SlicePeriodEndTime=today + " 23:59:59")
                suffix = "summary" if name == "summary" else name + "-packages"
                data = _body(request("POST", "/billing/meter/get-user-resource-" + suffix, body))
                rows = data.get("Packages" if name == "summary" else "Accounts")
                if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                    raise ValueError("invalid_resource_list")
                fingerprint = json.dumps(rows, sort_keys=True)
                if rows and fingerprint in seen_pages:
                    raise ValueError("repeated_page")
                seen_pages.add(fingerprint)
                collected.extend(rows)
                state.update(ok=True, pages=page, rows=len(collected))
                total = data.get("TotalCount")
                if name == "summary" or (total is not None and len(collected) >= int(total)) or (total is None and len(rows) < 200):
                    state["complete"] = True
                    break
                if not rows:
                    raise ValueError("truncated_page")
            if not state["complete"]:
                raise ValueError("page_budget_exhausted")
        except Exception as exc:
            # Never persist upstream bodies or exception strings containing credentials.
            state["error"] = str(exc) if isinstance(exc, ValueError) and str(exc) in {
                "billing_error_envelope", "billing_missing_resources", "collection_budget_exhausted",
                "invalid_resource_list", "repeated_page", "truncated_page", "page_budget_exhausted"} else type(exc).__name__
        if name == "summary":
            summary = collected
        else:
            details.extend(collected)
    complete = all(s["complete"] for s in sources.values())
    fallback = False
    if not complete:
        try:
            data = _body(request("POST", "/v2/billing/meter/get-user-resource", {}))
            legacy = data.get("Accounts")
            if not isinstance(legacy, list) or any(not isinstance(r, dict) for r in legacy):
                raise ValueError("invalid_resource_list")
            # Legacy has no independent pagination proof; mark compatibility explicitly.
            sources["legacy"] = {"ok": True, "complete": data.get("TotalCount") is None or len(legacy) >= int(data["TotalCount"]),
                                 "pages": 1, "rows": len(legacy)}
            if sources["legacy"]["complete"]:
                details = legacy
                summary = []
                complete = True
                fallback = True
        except Exception as exc:
            sources["legacy"] = {"ok": False, "complete": False, "error": type(exc).__name__}
    merged = {}
    for row in details:
        if row.get("CapacityUnit", "credits") != "credits":
            continue
        identity = _identity(row)
        merged[identity] = {**merged.get(identity, {}), **row}
    detail_codes = {r.get("PackageCode") for r in merged.values()}
    missing_codes = []
    for row in summary:
        if row.get("PackageCode") and row.get("PackageCode") in detail_codes:
            continue
        missing_codes.append(row.get("PackageCode"))
        merged[_identity(row)] = row
    from admin.credits import normalize_packages
    summary_remaining = sum(r["remaining"] for r in normalize_packages(summary)) if summary else None
    detail_remaining = sum(r["remaining"] for r in normalize_packages(list(merged.values())))
    return BillingPackages(list(merged.values()), {
        "collected_at": datetime.utcnow().isoformat() + "Z", "sources": sources,
        "complete": complete, "compatibility_fallback": fallback,
        "all_sources_failed": not any(s["ok"] for s in sources.values()),
        "failed_sources": [k for k, s in sources.items() if not s["complete"]],
        "summary_only_codes": missing_codes,
        "summary_remaining": summary_remaining, "merged_remaining": detail_remaining,
        "remaining_difference": round(detail_remaining - summary_remaining, 4) if summary_remaining is not None else None})
