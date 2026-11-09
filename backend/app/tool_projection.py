"""Model-only projection of duplicate rule text from trusted tool results."""


def _rule_text_key(data, rule):
    return (
        data["company_id"],
        data["period"],
        rule["rule_version_id"],
        rule["content_revision"],
        rule.get("full_text"),
    )


def project_rule_repeats(outputs: list[dict]) -> list[dict]:
    """Keep rule text in the explicit rule result and preserve summary references."""
    detailed = {
        _rule_text_key(data, rule)
        for output in outputs
        if output.get("ok") and (data := output.get("data", {})).get("kind") == "rules"
        for rule in data["rules"]
        if rule.get("full_text")
    }
    if not detailed:
        return outputs

    projected = []
    for output in outputs:
        data = output.get("data", {})
        if output.get("ok") and data.get("kind") == "monthly_summary":
            refs = [
                {key: value for key, value in ref.items() if key != "full_text"}
                if _rule_text_key(data, ref) in detailed
                else ref
                for ref in data["rule_refs"]
            ]
            output = {**output, "data": {**data, "rule_refs": refs}}
        projected.append(output)
    return projected
