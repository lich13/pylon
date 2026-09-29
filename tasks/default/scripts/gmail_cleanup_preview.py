#!/srv/hermes/venvs/google-workspace/bin/python
import importlib.util
import json
from pathlib import Path

SCRIPT_PATH = "/srv/hermes/scripts/gmail_cleanup_delete.py"
JSON_PATH = Path("/srv/hermes/cache/reports/gmail_cleanup_preview.json")
MD_PATH = Path("/srv/hermes/cache/reports/gmail_cleanup_preview.md")


def load_cleanup_module():
    spec = importlib.util.spec_from_file_location("gmail_cleanup_delete", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def normalize_rule_query(mod, query: str) -> str:
    q = query.strip()
    if mod.PROTECTION and q.startswith(mod.PROTECTION):
        q = q[len(mod.PROTECTION):].strip()
    return q


def build_report():
    mod = load_cleanup_module()
    service = mod.build_gmail_service()
    metadata_cache = {}

    def meta(mid: str):
        if mid not in metadata_cache:
            metadata_cache[mid] = mod.get_message_metadata(service, mid)
        return metadata_cache[mid]

    report = {
        "protected_filters": ["is:starred"],
        "rules": [],
        "total_delete_unique": 0,
        "total_keep_unique": 0,
    }

    seen_delete = set()
    seen_keep = set()

    for rule in mod.RULES:
        delete_ids = mod.list_message_ids(service, rule["query"])
        base_query = normalize_rule_query(mod, rule["query"])
        keep_query = f"is:starred {base_query}" if base_query else "is:starred"
        keep_ids = mod.list_message_ids(service, keep_query)

        delete_items = [meta(mid) for mid in delete_ids]
        keep_items = [meta(mid) for mid in keep_ids]

        report["rules"].append(
            {
                "name": rule["name"],
                "description": rule["description"],
                "delete_query": rule["query"],
                "keep_query": keep_query,
                "delete_count": len(delete_ids),
                "keep_count": len(keep_ids),
                "delete_items": delete_items,
                "keep_items": keep_items,
            }
        )
        seen_delete.update(delete_ids)
        seen_keep.update(keep_ids)

    report["total_delete_unique"] = len(seen_delete)
    report["total_keep_unique"] = len(seen_keep)
    return report


def write_markdown(report: dict):
    lines = []
    lines.append("# Gmail 自动清理预估报告")
    lines.append("")
    lines.append(f"- 当前保护条件：`{', '.join(report['protected_filters'])}`")
    lines.append(f"- 预计会永久删除（去重后）：**{report['total_delete_unique']}** 封")
    lines.append(f"- 因保护条件而保留（去重后）：**{report['total_keep_unique']}** 封")
    lines.append("- 说明：下文“保留”只列出**本来会命中清理规则、但因为当前保护条件而被保留**的邮件。")
    lines.append("- 其他未列出的邮件 = 当前**未命中任何删除规则**，因此这次不会删。")
    lines.append("")

    for rule in report["rules"]:
        lines.append(f"## {rule['description']}")
        lines.append("")
        lines.append(f"- 删除查询：`{rule['delete_query']}`")
        lines.append(f"- 保留查询：`{rule['keep_query']}`")
        lines.append(f"- 预计删除：**{rule['delete_count']}** 封")
        lines.append(f"- 预计保留：**{rule['keep_count']}** 封")
        lines.append("")
        lines.append("### 预计删除明细")
        if rule["delete_items"]:
            for i, item in enumerate(rule["delete_items"], 1):
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

        lines.append("### 因保护而保留的明细")
        if rule["keep_items"]:
            for i, item in enumerate(rule["keep_items"], 1):
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

    MD_PATH.parent.mkdir(parents=True, exist_ok=True)
    MD_PATH.write_text("\n".join(lines))


def main():
    report = build_report()
    JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    write_markdown(report)
    print(
        json.dumps(
            {
                "json_path": str(JSON_PATH),
                "md_path": str(MD_PATH),
                "total_delete_unique": report["total_delete_unique"],
                "total_keep_unique": report["total_keep_unique"],
                "rule_counts": [
                    {
                        "description": r["description"],
                        "delete_count": r["delete_count"],
                        "keep_count": r["keep_count"],
                    }
                    for r in report["rules"]
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
