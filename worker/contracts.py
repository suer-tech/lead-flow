"""Small strict model schemas and independently checked source grounding."""
from datetime import date
import hashlib
import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


STR = {"type": "string"}
BOOL = {"type": "boolean"}
STRINGS = {"type": "array", "items": STR}
DISCOVERY = obj({"companies": {"type": "array", "items": obj({
    "company_name": STR, "company_domain": STR,
    "segment": {"type": "string", "enum": ["bankruptcy_legal", "crm_integrator"]},
    "source_urls": STRINGS,
})}})
FOLLOWUP = obj({"source_urls": STRINGS})
QUOTE_REPAIR = obj({"identity_quote": STR, "signal_quote": STR, "contact_quote": STR})
ASSESSMENT = obj({
    "company_name": STR, "identity_url": STR, "identity_quote": STR,
    "signal_url": STR, "signal_quote": STR,
    "signal_kind": {"type": "string", "enum": ["explicit_need", "manual_work", "hiring", "integration_project", "profile_process", "existing_solution", "none"]},
    "signal_date": STR, "date_quote": STR, "current_unmet_need": BOOL,
    "use_case": STR, "why_fit": STR, "existing_solution": STR,
    "remaining_manual_step": STR, "budget_evidence": STR,
    "contact_email": STR, "contact_url": STR, "contact_quote": STR,
    "unknowns": STRINGS,
})
CRITIQUE = obj({"company_attribution_valid": BOOL, "need_quote_supports_claim": BOOL,
    "need_is_unmet": BOOL, "contact_official": BOOL, "fit_valid": BOOL,
    "process_quote_supports_workflow": BOOL,
    "reason": STR, "unknowns": STRINGS})
LETTER = obj({"subject": STR, "observation": STR, "question": STR,
    "pilot_focus": STR, "offer_kind": {"type": "string", "enum": ["direct_pilot", "partner_integration"]},
    "source_url": STR, "source_quote": STR})
LETTER_REVIEW_FLAGS = ("observation_grounded", "company_attribution_valid", "offer_supported",
    "no_invented_claims", "no_unverified_buying_intent", "no_misleading_relationship",
    "respectful_and_specific", "one_clear_question")
LETTER_REVIEW = obj({**{name: BOOL for name in LETTER_REVIEW_FLAGS},
    "approved": BOOL, "reason": STR, "revision_feedback": STR})


def compact(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def public_url(value):
    """Syntax gate; Iva's reader also pins public DNS on every redirect."""
    parsed = urlsplit(str(value))
    host = (parsed.hostname or "").lower().rstrip(".").encode("idna").decode("ascii")
    if parsed.scheme not in ("http", "https") or not host or parsed.username or parsed.password:
        raise ValueError("invalid_public_url")
    if host == "localhost" or "." not in host or host.endswith((".local", ".internal")):
        raise ValueError("invalid_public_host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("private_address")
    if parsed.port not in (None, 80, 443):
        raise ValueError("nonstandard_port")
    if re.search(r"(?:^|&)(?:token|auth|session|password|sig|key|code)=", parsed.query, re.I):
        raise ValueError("private_query")
    return urlunsplit((parsed.scheme, host, parsed.path or "/", parsed.query, ""))


def domain(value):
    host = urlsplit(public_url(value if "://" in value else "https://" + value)).hostname
    return host.removeprefix("www.")


def official(url, company_domain):
    host = domain(url)
    return host == company_domain or host.endswith("." + company_domain)


def exact_quote(quote, text):
    quoted = compact(quote)
    return 15 <= len(quoted) <= 400 and len(quoted.split()) <= 25 and quoted in compact(text)


def citation_matches(quote, source, prefix):
    text = str(source.get("text") or "")
    if exact_quote(quote, text):
        return True
    if prefix == "contact":
        # Header/footer contact fragments are a separate evidence class. They
        # cannot establish identity/intent or be joined into an invented quote.
        fragments = source.get("contact_fragments", [])
        if isinstance(fragments, list):
            return any(exact_quote(quote, item) for item in fragments[:4] if isinstance(item, str) and len(item) <= 400)
    return False


def source_readable(source, allow_contact=False):
    if source.get("ok") is True and source.get("text"):
        return True
    return (allow_contact and source.get("contact_readable") is True
        and isinstance(source.get("contact_fragments"), list)
        and any(isinstance(value, str) and value for value in source["contact_fragments"][:4]))


def validate_assessment(candidate, assessment, sources, today, max_age=180):
    """Every claim must refer to a fetched body, not a search result snippet."""
    errors = []
    by_url = {row["url"]: row for row in sources if source_readable(row, allow_contact=True)}
    company_domain = domain(candidate["company_domain"])
    selected = {}
    for prefix in ("identity", "signal", "contact"):
        try:
            url = public_url(assessment[prefix + "_url"])
        except (ValueError, KeyError, TypeError):
            errors.append(prefix + "_url_invalid")
            continue
        source = by_url.get(url)
        if not source or not source_readable(source, allow_contact=prefix == "contact") or not citation_matches(assessment.get(prefix + "_quote"), source, prefix):
            errors.append(prefix + "_quote_unverified")
            continue
        selected[prefix] = source
        if prefix in ("identity", "contact") and not official(url, company_domain):
            errors.append(prefix + "_not_official")
    email = str(assessment.get("contact_email", "")).strip().lower()
    if not re.fullmatch(r"[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+", email):
        errors.append("email_invalid")
    elif email not in compact(assessment.get("contact_quote", "")).lower():
        errors.append("email_not_in_official_quote")
    if compact(assessment.get("company_name", "")) != compact(candidate["company_name"]):
        errors.append("company_name_changed")
    if assessment.get("signal_kind") in ("none", "existing_solution") or not assessment.get("current_unmet_need"):
        errors.append("no_current_unmet_need")
    if len(compact(assessment.get("remaining_manual_step", ""))) < 10:
        errors.append("remaining_workflow_gap_unknown")
    date_value = assessment.get("signal_date", "")
    date_verified = False
    if date_value:
        try:
            parsed = date.fromisoformat(date_value)
            source = selected.get("signal", {})
            published = str(source.get("published_at") or "")[:10]
            date_verified = (published == date_value or
                (date_value in assessment.get("date_quote", "") and
                 exact_quote(assessment["date_quote"], source.get("text", ""))))
            if parsed > today or (today - parsed).days > max_age:
                errors.append("signal_outside_freshness_window")
        except ValueError:
            errors.append("signal_date_invalid")
    if not date_verified:
        errors.append("signal_date_unverified")
    # Missing date is a reviewable gap, never permission for a contact-ready claim.
    # A grounded company/process can remain a research candidate even when its
    # need, date or contact is unknown. None of those gaps authorises outreach.
    fatal = [x for x in errors if x.startswith(("identity_", "signal_url_", "signal_quote_")) or x == "company_name_changed"]
    contact_valid = not any(x.startswith("contact_") or x.startswith("email_") for x in errors)
    return {"valid": not fatal, "eligible": not errors, "errors": errors,
        "email": email if contact_valid else "", "domain": company_domain,
        "date_verified": date_verified, "contact_valid": contact_valid}


def accepted_review(review):
    return all(review.get(k) is True for k in ("company_attribution_valid", "need_quote_supports_claim",
        "need_is_unmet", "contact_official", "fit_valid", "process_quote_supports_workflow"))


def accepted_research_review(review):
    return all(review.get(k) is True for k in ("company_attribution_valid", "fit_valid", "process_quote_supports_workflow"))


def validate_letter(candidate, assessment, proposal, sources):
    """Check literal provenance/format before the independent semantic reviewer."""
    errors = []
    for field, minimum, maximum in (("subject", 8, 120), ("observation", 20, 360),
            ("question", 15, 280), ("pilot_focus", 10, 240)):
        value = proposal.get(field)
        if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
            errors.append(field + "_invalid_length")
            continue
        if any(ord(c) < 32 for c in value) or re.search(r"https?://|www\.|[<>]|\S+@\S+", value, re.I):
            errors.append(field + "_contains_external_address_or_markup")
    if str(proposal.get("question", "")).count("?") != 1 or any("?" in str(proposal.get(f, "")) for f in ("observation", "pilot_focus")):
        errors.append("one_question_required")
    if re.match(r"^\s*(?:re|fwd?|ответ|пересылка)\s*:", str(proposal.get("subject", "")), re.I):
        errors.append("misleading_reply_subject")
    expected_kind = "partner_integration" if candidate["segment"] == "crm_integrator" else "direct_pilot"
    if proposal.get("offer_kind") != expected_kind:
        errors.append("offer_kind_mismatches_segment")
    by_url = {source["url"]: source for source in sources if source.get("ok") and source.get("text")}
    source = by_url.get(proposal.get("source_url"))
    if proposal.get("source_url") != assessment["signal_url"] or not source or not exact_quote(proposal.get("source_quote"), source["text"]):
        errors.append("letter_source_unverified")
    return errors


def accepted_letter_review(review):
    return review.get("approved") is True and all(review.get(flag) is True for flag in LETTER_REVIEW_FLAGS)


def make_draft(candidate, proposal):
    # The writer cannot choose the sender/recipient, invent a signature, add a
    # tracking URL, or turn uncertainty into a guarantee. Claims are reviewed.
    offer = ("Предлагаю обсудить совместный пилот для одного клиента: " if proposal["offer_kind"] == "partner_integration"
        else "Предлагаю проверить на небольшой выборке ваших документов: ")
    return {"subject": proposal["subject"].strip(), "body": (
        "Здравствуйте!\n\n" + proposal["observation"].strip() + "\n\n"
        "Я разрабатываю модуль извлечения данных из судебных решений и определений. "
        + offer + proposal["pilot_focus"].strip().rstrip(". ") + ". "
        "Точность результата и время проверки специалистом измерим на пилоте.\n\n"
        + proposal["question"].strip() + "\n\n"
        "Если это не к вам или уже решено, сообщите — больше не буду беспокоить.\n\nAI Studio")}


def stable_key(campaign, company_domain):
    return "ocr:" + hashlib.sha256((campaign + "|" + company_domain).encode()).hexdigest()
