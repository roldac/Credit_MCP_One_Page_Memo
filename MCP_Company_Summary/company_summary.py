#!/usr/bin/env python3
"""Create a one-page company profile PDF using Moody's Credit MCP.

Requires the `mcp` and `reportlab` Python packages. Configure the Moody's MCP
server in VS Code's user MCP configuration (`~/Library/Application
Support/Code/User/mcp.json`). The program reads the `MCP-Credit` server URL and
Authorization header from that file. You can override these settings with:

    export MOODYS_MCP_CONFIG='/path/to/mcp.json'
    export MOODYS_MCP_URL='https://mcp.moodys.com/genai-ready-data/Credit/mcp'
    export MOODYS_MCP_AUTHORIZATION='SSWS <your-token>'
    python company_summary.py

The authorization value is sent as the HTTP Authorization header. Keep the
config file and token private, and rotate the token if it has been exposed.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import (
    create_mcp_http_client,
    streamable_http_client,
)
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepInFrame,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


def _tool_name(tools: list[Any], suffix: str) -> str:
    matches = [
        tool.name
        for tool in tools
        if tool.name == suffix or tool.name.rsplit("-", 1)[-1] == suffix
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one Moody's MCP tool ending in {suffix!r}; "
            f"found {len(matches)}."
        )
    return matches[0]


def _result_data(result: Any) -> Any:
    if getattr(result, "isError", False):
        details = "\n".join(
            item.text for item in getattr(result, "content", []) if hasattr(item, "text")
        )
        raise RuntimeError(f"Moody's MCP call failed: {details or 'unspecified error'}")
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    texts = [
        item.text for item in getattr(result, "content", []) if hasattr(item, "text")
    ]
    if not texts:
        raise RuntimeError("Moody's MCP returned no usable content.")
    joined = "\n".join(texts)
    try:
        return json.loads(joined)
    except json.JSONDecodeError:
        return joined


def _as_entity_list(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        entities = data.get("entities")
        if entities is None and isinstance(data.get("results"), list):
            entities = data["results"]
        if entities is None:
            entities = [data]
        if isinstance(entities, list):
            resolved = [entity for entity in entities if isinstance(entity, dict)]
            matches: list[dict[str, Any]] = []
            for entity in resolved:
                matches.append({key: value for key, value in entity.items() if key != "alternatives"})
                alternatives = entity.get("alternatives", [])
                if isinstance(alternatives, list):
                    matches.extend(
                        alternative
                        for alternative in alternatives
                        if isinstance(alternative, dict)
                    )
            return matches
    if isinstance(data, list):
        return [entity for entity in data if isinstance(entity, dict)]
    raise RuntimeError(
        "Could not read entity-resolution results. "
        "Check that findEntity returns its standard structured response."
    )


def _field(entity: dict[str, Any], *names: str) -> str:
    for name in names:
        value = entity.get(name)
        if value not in (None, ""):
            return str(value)
    return ""


def _field_from_text(text: str, *keys: str) -> str:
    for key in keys:
        match = re.search(
            rf"(?im)^-\s*{re.escape(key)}\s*:\s*(.+?)\s*$", text
        )
        if match:
            return match.group(1).strip()
    return ""


def _summary_from_opinion(text: str) -> str:
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        decoded = None
    if decoded is not None:
        summaries: list[str] = []

        def find_summaries(value: Any) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    if str(key).lower() == "summary" and isinstance(nested, str):
                        summaries.append(nested.strip())
                    else:
                        find_summaries(nested)
            elif isinstance(value, list):
                for nested in value:
                    find_summaries(nested)

        find_summaries(decoded)
        if summaries:
            return "\n".join(dict.fromkeys(summaries))
    match = re.search(
        r"(?ims)^#+\s*Summary\s*$\s*(.*?)(?=^#+\s|\Z)", text
    )
    if match:
        return match.group(1).strip()
    return text.strip()


def _rating_summary(data: Any) -> str:
    if isinstance(data, dict):
        facts: list[str] = []

        def collect(value: Any, prefix: str = "") -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    label = f"{prefix}.{key}" if prefix else str(key)
                    normalized = label.lower().replace("_", "").replace(" ", "")
                    if (
                        any(term in normalized for term in ("longtermrating", "outlook"))
                        and "implied" not in normalized
                        and not isinstance(nested, (dict, list))
                        and nested not in (None, "")
                    ):
                        facts.append(f"{key}: {nested}")
                    else:
                        collect(nested, label)
            elif isinstance(value, list):
                for nested in value:
                    collect(nested, prefix)

        collect(data)
        return "; ".join(dict.fromkeys(facts))
    if isinstance(data, str):
        lines = [
            " ".join(line.strip().split())
            for line in data.splitlines()
            if re.search(r"long.?term.?rating|outlook", line, re.I)
            and not re.search(r"implied", line, re.I)
        ]
        return "; ".join(dict.fromkeys(lines))
    return ""


def _plain_text(data: Any) -> str:
    if isinstance(data, str):
        return data.strip()
    pieces: list[str] = []

    def render(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            if isinstance(value.get("text"), str):
                pieces.append(value["text"].strip())
                return
            for name, nested in value.items():
                render(nested, str(name))
        elif isinstance(value, list):
            for nested in value:
                render(nested, key)
        elif value not in (None, ""):
            pieces.append(f"- {key}: {value}" if key else str(value))

    render(data)
    if pieces:
        return "\n".join(pieces)
    return json.dumps(data, ensure_ascii=False, indent=2)


def _escape(value: str) -> str:
    from xml.sax.saxutils import escape

    return escape(value).replace("\n", "<br/>")


def _one_line(value: str, limit: int = 180) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"


def _mcp_connection_settings() -> tuple[str, str]:
    url = os.environ.get("MOODYS_MCP_URL", "").strip()
    authorization = os.environ.get("MOODYS_MCP_AUTHORIZATION", "").strip()
    if url and authorization:
        return url, authorization

    config_path = Path(
        os.environ.get(
            "MOODYS_MCP_CONFIG",
            str(Path.home() / "Library/Application Support/Code/User/mcp.json"),
        )
    ).expanduser()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RuntimeError(
            f"Moody's MCP config not found at {config_path}. Set "
            "MOODYS_MCP_CONFIG to its location or set both "
            "MOODYS_MCP_URL and MOODYS_MCP_AUTHORIZATION."
        ) from error
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"Invalid JSON in Moody's MCP config at {config_path}."
        ) from error

    servers = config.get("servers") if isinstance(config, dict) else None
    server = servers.get("MCP-Credit") if isinstance(servers, dict) else None
    if not isinstance(server, dict):
        raise RuntimeError(
            f"Moody's MCP config at {config_path} has no 'servers.MCP-Credit' entry."
        )
    headers = server.get("headers")
    config_authorization = (
        next(
            (
                value
                for key, value in headers.items()
                if key.lower() == "authorization" and isinstance(value, str)
            ),
            "",
        )
        if isinstance(headers, dict)
        else ""
    )
    config_url = server.get("url")
    if not url:
        url = config_url.strip() if isinstance(config_url, str) else ""
    if not authorization:
        authorization = config_authorization.strip()
    if not url or not authorization:
        raise RuntimeError(
            f"Moody's MCP config at {config_path} must provide the server URL "
            "and an Authorization header for 'MCP-Credit'."
        )
    return url, authorization


async def _fetch_company(
    company_name: str,
) -> tuple[dict[str, Any], str, str, str]:
    url, authorization = _mcp_connection_settings()

    async with create_mcp_http_client(
        headers={"Authorization": authorization}
    ) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (
            read_stream,
            write_stream,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                find_entity = _tool_name(tools, "findEntity")
                get_profile = _tool_name(tools, "getEntityProfile")
                get_ratings = _tool_name(tools, "getEntityRatings")
                get_opinion = _tool_name(tools, "getCreditOpinion")

                found = _result_data(
                    await session.call_tool(
                        find_entity,
                        {
                            "data": company_name,
                            "entityType": "organizations",
                            "getAlternatives": True,
                        },
                    )
                )
                entities = _as_entity_list(found)
                if not entities:
                    raise RuntimeError(
                        f"No Moody's entity match found for {company_name!r}."
                    )
                if len(entities) == 1:
                    chosen = entities[0]
                else:
                    print("\nMoody's entity matches:")
                    for index, entity in enumerate(entities, start=1):
                        name = _field(entity, "orgName", "name") or "(unnamed)"
                        country = (
                            _field(entity, "country", "countryName")
                            or "country unknown"
                        )
                        rating = _field(entity, "longTermRating", "rating")
                        extra = f" — Long Term Rating: {rating}" if rating else ""
                        print(f"  {index}. {name} ({country}){extra}")
                    while True:
                        answer = input("Choose the matching entity number: ").strip()
                        if answer.isdigit() and 1 <= int(answer) <= len(entities):
                            chosen = entities[int(answer) - 1]
                            break
                        print(f"Enter a number from 1 to {len(entities)}.")

                entity_id = _field(chosen, "entityId")
                if not entity_id:
                    raise RuntimeError(
                        "Moody's entity resolution did not return an entityId."
                    )
                name = _field(chosen, "orgName", "name") or company_name
                is_unrated = bool(chosen.get("isUnrated", False))
                entity_arg = {
                    "entityId": entity_id,
                    "orgName": name,
                    "isUnrated": is_unrated,
                }
                args = {"entities": [entity_arg]}
                profile_args = {
                    "entities": [{"entityId": entity_id, "orgName": name}]
                }
                profile = _result_data(
                    await session.call_tool(get_profile, profile_args)
                )
                ratings = _result_data(await session.call_tool(get_ratings, args))
                opinion = _result_data(await session.call_tool(get_opinion, args))
                return (
                    chosen,
                    _plain_text(profile),
                    _rating_summary(ratings),
                    _summary_from_opinion(_plain_text(opinion)),
                )


def _write_pdf(
    output: Path,
    entity: dict[str, Any],
    profile_text: str,
    rating_text: str,
    opinion_text: str,
) -> None:
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="ReportTitle",
            parent=styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=19,
            leading=22,
            textColor=colors.HexColor("#102a43"),
            alignment=TA_CENTER,
            spaceAfter=4,
        )
    )
    styles.add(
        ParagraphStyle(
            name="SectionHeading",
            parent=styles["Heading2"],
            fontSize=10,
            leading=12,
            textColor=colors.HexColor("#176b87"),
            spaceBefore=7,
            spaceAfter=3,
        )
    )
    styles.add(
        ParagraphStyle(
            name="Compact",
            parent=styles["BodyText"],
            fontSize=8.5,
            leading=11,
            spaceAfter=3,
        )
    )
    styles.add(
        ParagraphStyle(
            name="Small",
            parent=styles["BodyText"],
            fontSize=7.5,
            leading=9,
            textColor=colors.HexColor("#52606d"),
        )
    )

    name = _field(entity, "orgName", "name") or "Company"
    country = _field(entity, "country", "countryName")
    rating = _field(entity, "longTermRating") or "Not returned"
    rating_outlook = ""
    if rating_text:
        rating_outlook = rating_text
    overview = _field_from_text(
        profile_text, "trade_description_en", "primary_business_line", "full_overview"
    )
    address = _field_from_text(profile_text, "entity_address")
    city = _field_from_text(profile_text, "city")
    state = _field_from_text(profile_text, "state")
    postcode = _field_from_text(profile_text, "postcode")
    if city or state or postcode:
        address = ", ".join(part for part in (address, city, state, postcode) if part)
    website = _field_from_text(profile_text, "website_address")
    phone = _field_from_text(profile_text, "phone_number", "hq_phone_number")
    employees = _field_from_text(profile_text, "number_of_employees")
    industry = _field_from_text(
        profile_text, "naics_core_code_description", "nace_rev_2_core_code_description"
    )
    legal_form = _field_from_text(profile_text, "national_legal_form", "legal_form")
    org_id = _field_from_text(profile_text, "moodys_org_id", "preferred_org_id")
    lei = _field_from_text(profile_text, "lei_legal_entity_identifier")

    story: list[Any] = [
        Paragraph(_escape(name), styles["ReportTitle"]),
        Paragraph(
            _escape("Moody's Credit Company Summary")
            + (f" &nbsp;|&nbsp; {_escape(country)}" if country else ""),
            styles["Small"],
        ),
        Spacer(1, 7),
    ]
    info = [
        [
            Paragraph("<b>Moody's Long Term Rating</b>", styles["Compact"]),
            Paragraph("<b>Employees</b>", styles["Compact"]),
            Paragraph("<b>Industry</b>", styles["Compact"]),
            Paragraph("<b>Legal form</b>", styles["Compact"]),
        ],
        [
            Paragraph(_escape(rating or "Not returned"), styles["Compact"]),
            Paragraph(_escape(employees or "Not returned"), styles["Compact"]),
            Paragraph(_escape(industry or "Not returned"), styles["Compact"]),
            Paragraph(_escape(legal_form or "Not returned"), styles["Compact"]),
        ],
    ]
    table = Table(info, colWidths=[1.42 * inch, 1.0 * inch, 2.1 * inch, 1.45 * inch])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#edf4f8")),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5df")),
                ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#d9e2ec")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(table)

    story.append(Paragraph("Company profile", styles["SectionHeading"]))
    if overview:
        story.append(Paragraph(_escape(_one_line(overview, 650)), styles["Compact"]))
    else:
        story.append(
            Paragraph(
                "Moody's profile did not return a business description.",
                styles["Compact"],
            )
        )

    story.append(Paragraph("Credit view", styles["SectionHeading"]))
    if opinion_text.strip():
        story.append(Paragraph(_escape(_one_line(opinion_text, 1150)), styles["Compact"]))
    else:
        story.append(
            Paragraph(
                "Moody's did not return a credit opinion summary.",
                styles["Compact"],
            )
        )
    if rating_outlook:
        story.append(
            Paragraph(
                "<b>Rating details from Moody's ratings history:</b> "
                + _escape(_one_line(rating_outlook, 450)),
                styles["Compact"],
            )
        )

    details = [
        item
        for item in (
            f"Address: {address}" if address else "",
            f"Website: {website}" if website else "",
            f"Phone: {phone}" if phone else "",
            f"Moody's Org ID: {org_id}" if org_id else "",
            f"LEI: {lei}" if lei else "",
        )
        if item
    ]
    if details:
        story.append(Paragraph("Company details", styles["SectionHeading"]))
        story.append(Paragraph(_escape("  |  ".join(details)), styles["Compact"]))

    story.extend(
        [
            Spacer(1, 5),
            Paragraph(
                "Source: Moody's Credit MCP (entity profile, ratings, and latest "
                "credit opinion). Generated "
                + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                + ". Ratings shown are Moody's ratings, not implied ratings.",
                styles["Small"],
            ),
        ]
    )

    document = SimpleDocTemplate(
        str(output),
        pagesize=letter,
        rightMargin=0.55 * inch,
        leftMargin=0.55 * inch,
        topMargin=0.45 * inch,
        bottomMargin=0.45 * inch,
        title=f"{name} - Moody's Credit Summary",
        author="Moody's Credit MCP",
    )
    available_width = letter[0] - document.leftMargin - document.rightMargin
    available_height = letter[1] - document.topMargin - document.bottomMargin
    document.build(
        [KeepInFrame(available_width, available_height, story, mode="shrink")]
    )


def main() -> int:
    company_name = input("Enter a company name: ").strip()
    if not company_name:
        print("A company name is required.", file=sys.stderr)
        return 2
    try:
        entity, profile_text, rating_text, opinion_text = asyncio.run(
            _fetch_company(company_name)
        )
        company = _field(entity, "orgName", "name") or company_name
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", company).strip("._")
        output = Path(__file__).resolve().parent / f"{safe_name or 'company'}_summary.pdf"
        _write_pdf(output, entity, profile_text, rating_text, opinion_text)
    except (RuntimeError, OSError, ValueError) as error:
        print(f"Could not create the company summary: {error}", file=sys.stderr)
        return 1
    print(f"Created {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
