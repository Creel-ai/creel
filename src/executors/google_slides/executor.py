#!/usr/bin/env python3
"""Google Slides executor - read and write presentations via the Slides API v1.

Requires GOOGLE_ACCESS_TOKEN env var containing a short-lived OAuth2 access token.
Outputs JSON to stdout.
"""

from __future__ import annotations

import json
import os
import sys

from googleapiclient.discovery import build

try:
    from executors.google_creds import get_credentials
except ModuleNotFoundError:
    from google_creds import get_credentials  # type: ignore[no-redef]


def _extract_slide_text(slide: dict) -> str:
    """Extract text from all shapes in a slide."""
    parts: list[str] = []
    for element in slide.get("pageElements", []):
        shape = element.get("shape")
        if not shape:
            continue
        text_content = shape.get("text")
        if not text_content:
            continue
        for text_elem in text_content.get("textElements", []):
            text_run = text_elem.get("textRun")
            if text_run:
                parts.append(text_run.get("content", ""))
    return "".join(parts)


def read_presentation(presentation_id: str) -> dict:
    """Get presentation metadata and slide text content.

    Args:
        presentation_id: The Google Slides presentation ID.

    Returns:
        Dict with title, slide count, and per-slide text.
    """
    creds = get_credentials()
    service = build("slides", "v1", credentials=creds, cache_discovery=False)

    pres = service.presentations().get(presentationId=presentation_id).execute()

    slides = []
    for i, slide in enumerate(pres.get("slides", []), 1):
        slides.append(
            {
                "slideNumber": i,
                "objectId": slide["objectId"],
                "text": _extract_slide_text(slide),
            }
        )

    return {
        "presentationId": pres["presentationId"],
        "title": pres.get("title", ""),
        "slideCount": len(slides),
        "slides": slides,
    }


def create_presentation(title: str) -> dict:
    """Create a new presentation.

    Args:
        title: Presentation title.

    Returns:
        Dict with presentation id and url.
    """
    creds = get_credentials()
    service = build("slides", "v1", credentials=creds, cache_discovery=False)

    pres = service.presentations().create(body={"title": title}).execute()
    presentation_id = pres["presentationId"]

    return {
        "presentationId": presentation_id,
        "url": f"https://docs.google.com/presentation/d/{presentation_id}",
    }


def add_slide(
    presentation_id: str,
    title: str = "",
    body: str = "",
    layout: str = "BLANK",
) -> dict:
    """Add a new slide to a presentation.

    Args:
        presentation_id: The presentation ID.
        title: Optional slide title text.
        body: Optional slide body text.
        layout: Predefined layout (default BLANK).

    Returns:
        Dict with the new slide's object ID.
    """
    creds = get_credentials()
    service = build("slides", "v1", credentials=creds, cache_discovery=False)

    requests: list[dict] = [
        {
            "createSlide": {
                "slideLayoutReference": {"predefinedLayout": layout},
            }
        }
    ]

    result = (
        service.presentations()
        .batchUpdate(presentationId=presentation_id, body={"requests": requests})
        .execute()
    )

    slide_id = result["replies"][0]["createSlide"]["objectId"]

    # Insert title and body text if provided
    text_requests: list[dict] = []
    if title or body:
        # Get the newly created slide to find placeholder shape IDs
        pres = service.presentations().get(presentationId=presentation_id).execute()
        new_slide = None
        for slide in pres.get("slides", []):
            if slide["objectId"] == slide_id:
                new_slide = slide
                break

        if new_slide:
            for element in new_slide.get("pageElements", []):
                shape = element.get("shape")
                if not shape:
                    continue
                placeholder = shape.get("placeholder")
                if not placeholder:
                    continue
                ph_type = placeholder.get("type", "")
                if ph_type in ("TITLE", "CENTERED_TITLE") and title:
                    text_requests.append(
                        {
                            "insertText": {
                                "objectId": element["objectId"],
                                "text": title,
                            }
                        }
                    )
                elif ph_type in ("BODY", "SUBTITLE") and body:
                    text_requests.append(
                        {
                            "insertText": {
                                "objectId": element["objectId"],
                                "text": body,
                            }
                        }
                    )

    if text_requests:
        service.presentations().batchUpdate(
            presentationId=presentation_id,
            body={"requests": text_requests},
        ).execute()

    return {
        "presentationId": presentation_id,
        "slideId": slide_id,
    }


def replace_text(
    presentation_id: str,
    find: str,
    replace_with: str,
    match_case: bool = True,
) -> dict:
    """Replace text across all slides in a presentation.

    Args:
        presentation_id: The presentation ID.
        find: Text to find.
        replace_with: Replacement text.
        match_case: Whether the search is case-sensitive.

    Returns:
        Dict with the number of occurrences changed.
    """
    creds = get_credentials()
    service = build("slides", "v1", credentials=creds, cache_discovery=False)

    result = (
        service.presentations()
        .batchUpdate(
            presentationId=presentation_id,
            body={
                "requests": [
                    {
                        "replaceAllText": {
                            "containsText": {
                                "text": find,
                                "matchCase": match_case,
                            },
                            "replaceText": replace_with,
                        }
                    }
                ]
            },
        )
        .execute()
    )

    replies = result.get("replies", [{}])
    occurrences = replies[0].get("replaceAllText", {}).get("occurrencesChanged", 0)

    return {
        "presentationId": presentation_id,
        "occurrencesChanged": occurrences,
    }


def main() -> None:
    action = os.environ.get("ACTION", "").lower()

    if not action:
        print(
            json.dumps(
                {"error": "ACTION is required (read, create, add_slide, replace_text)"}
            ),
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        if action == "read":
            presentation_id = os.environ.get("PRESENTATION_ID", "")
            if not presentation_id:
                raise ValueError("PRESENTATION_ID is required for read")
            result = read_presentation(presentation_id)
        elif action == "create":
            title = os.environ.get("TITLE", "")
            if not title:
                raise ValueError("TITLE is required for create")
            result = create_presentation(title)
        elif action == "add_slide":
            presentation_id = os.environ.get("PRESENTATION_ID", "")
            if not presentation_id:
                raise ValueError("PRESENTATION_ID is required for add_slide")
            title = os.environ.get("TITLE", "")
            body = os.environ.get("BODY", "")
            layout = os.environ.get("LAYOUT", "BLANK")
            result = add_slide(presentation_id, title, body, layout)
        elif action == "replace_text":
            presentation_id = os.environ.get("PRESENTATION_ID", "")
            find = os.environ.get("FIND", "")
            replace_with = os.environ.get("REPLACE_WITH", "")
            if not presentation_id or not find:
                raise ValueError(
                    "PRESENTATION_ID and FIND are required for replace_text"
                )
            match_case = os.environ.get("MATCH_CASE", "true").lower() in (
                "true",
                "1",
                "yes",
            )
            result = replace_text(presentation_id, find, replace_with, match_case)
        else:
            print(
                json.dumps({"error": f"Unknown action: {action}"}),
                file=sys.stderr,
            )
            sys.exit(1)

        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
