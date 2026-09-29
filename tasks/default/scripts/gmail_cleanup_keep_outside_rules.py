#!/srv/pylon/venv/bin/python
import importlib.util
import json
from pathlib import Path

SCRIPT_PATH = "/srv/pylon/profiles/default/scripts/gmail_cleanup_delete.py"
JSON_PATH = Path("/srv/pylon/profiles/default/cache/reports/gmail_cleanup_keep_outside_rules.json")
MD_PATH = Path("/srv/pylon/profiles/default/cache/reports/gmail_cleanup_keep_outside_rules.md")


def load_cleanup_module():
    spec = importlib.util.spec_from_file_location("gmail_cleanup_delete", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def list_all_message_ids(service):
    ids = []
    page_token = None
    while True:
        resp = (
            service.users()
            .messages()
            .list(
                userId="me",
                includeSpamTrash=False,
                maxResults=500,
                pageToken=page_token,
            )
            .execute()
        )
        for item in resp.get("messages", []):
            ids.append(item["id"])
        page_token = resp.get("nextPageToken")
        if not page_token:
            return ids


def normalize_rule_query(mod, query: str) -> str:
    q = query.strip()
    if mod.PROTECTION and q.startswith(mod.PROTECTION):
        q = q[len(mod.PROTECTION):].strip()
    return q


def main():
    mod = load_cleanup_module()
    service = mod.build_gmail_service()
    metadata_cache = {}

    def meta(mid: str):
        if mid not in metadata_cache:
            metadata_cache[mid] = mod.get_message_metadata(service, mid)
        return metadata_cache[mid]

    delete_union = set()
    protected_union = set()
    rule_summaries = []

    for rule in mod.RULES:
        delete_ids = mod.list_message_ids(service, rule["query"])
        delete_union.update(delete_ids)
        base_query = normalize_rule_query(mod, rule["query"])
        protected_query = f"is:starred {base_query}" if base_query else "is:starred"
        protected_ids = mod.list_message_ids(service, protected_query)
        protected_union.update(protected_ids)
        rule_summaries.append(
            {
                "name": rule["name"],
                "description": rule["description"],
                "delete_count": len(delete_ids),
                "protected_count": len(protected_ids),
            }
        )

    all_ids = list_all_message_ids(service)
    all_set = set(all_ids)
    outside_ids = [mid for mid in all_ids if mid not in delete_union and mid not in protected_union]
    protected_ids = [mid for mid in all_ids if mid in protected_union]

    outside_items = [meta(mid) for mid in outside_ids]
    protected_items = [meta(mid) for mid in protected_ids]

    report = {
        "scope": "All mail excluding spam/trash",
        "protected_filters": ["is:starred"],
        "all_message_count": len(all_ids),
        "delete_rule_match_unique_count": len(delete_union),
        "protected_match_unique_count": len(protected_union),
        "outside_rule_unique_count": len(outside_ids),
        "rule_summaries": rule_summaries,
        "protected_items": protected_items,
        "outside_items": outside_items,
    }

    JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    lines = []
    lines.append("# Gmail 未命中清理规则的邮件清单")
    lines.append("")
    lines.append("- 范围：**全部邮件（不含 Spam/Trash）**")
    lines.append(f"- 当前保护条件：`{', '.join(report['protected_filters'])}`")
    lines.append(f"- 全部邮件数：**{report['all_message_count']}**")
    lines.append(f"- 命中删除规则（去重后）：**{report['delete_rule_match_unique_count']}**")
    lines.append(f"- 命中规则但因保护被保留（去重后）：**{report['protected_match_unique_count']}**")
    lines.append(f"- 完全在规则之外、当前不会删：**{report['outside_rule_unique_count']}**")
    lines.append("")
    lines.append("## 各删除规则命中情况")
    lines.append("")
    for item in report["rule_summaries"]:
        lines.append(f"- {item['description']}：删除 {item['delete_count']} / 因保护保留 {item['protected_count']}")
    lines.append("")

    lines.append("## 命中规则但因保护被保留的邮件")
    lines.append("")
    if protected_items:
        for i, item in enumerate(protected_items, 1):
            subj = item.get("subject", "").replace("\n", " ").strip()
            sender = item.get("from", "").replace("\n", " ").strip()
            date = item.get("date", "").replace("\n", " ").strip()
            labels = ", ".join(item.get("labels", []))
            snippet = item.get("snippet", "").replace("\n", " ").strip()
            if len(snippet) > 160:
                snippet = snippet[:157] + "..."
            lines.append(f"{i}. 日期：{date}")
            lines.append(f"   发件人：{sender}")
            lines.append(f"   主题：{subj}")
            lines.append(f"   Labels：{labels}")
            lines.append(f"   片段：{snippet}")
            lines.append("")
    else:
        lines.append("- 无")
        lines.append("")

    lines.append("## 完全在规则之外、当前不会删的邮件")
    lines.append("")
    for i, item in enumerate(outside_items, 1):
        subj = item.get("subject", "").replace("\n", " ").strip()
        sender = item.get("from", "").replace("\n", " ").strip()
        date = item.get("date", "").replace("\n", " ").strip()
        labels = ", ".join(item.get("labels", []))
        snippet = item.get("snippet", "").replace("\n", " ").strip()
        if len(snippet) > 160:
            snippet = snippet[:157] + "..."
        lines.append(f"{i}. 日期：{date}")
        lines.append(f"   发件人：{sender}")
        lines.append(f"   主题：{subj}")
        lines.append(f"   Labels：{labels}")
        lines.append(f"   片段：{snippet}")
        lines.append("")

    MD_PATH.write_text("\n".join(lines))
    print(json.dumps({
        "json_path": str(JSON_PATH),
        "md_path": str(MD_PATH),
        "all_message_count": len(all_ids),
        "delete_rule_match_unique_count": len(delete_union),
        "protected_match_unique_count": len(protected_union),
        "outside_rule_unique_count": len(outside_ids),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
