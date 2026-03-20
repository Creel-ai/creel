#!/usr/bin/env python3
"""Google Contacts executor - search and retrieve contacts (read-only).

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

# Fields to request from the People API
_PERSON_FIELDS = "names,emailAddresses,phoneNumbers,organizations,addresses,birthdays,biographies"


def register_skill():
    """Register the google_contacts skill with the skill registry."""
    import json
    from typing import TYPE_CHECKING

    from creel.skills.models import Param, SkillMeta, ToolSpec

    if TYPE_CHECKING:
        from creel.models import ExecutorConfig

    meta = SkillMeta(
        id="google_contacts",
        label="Google Contacts",
        tools=(
            ToolSpec(
                name="search_contacts",
                description="Search contacts by name, email, or phone",
                params=(
                    Param(
                        name="query",
                        type="string",
                        description="Search query (name, email, or phone number)",
                        required=True,
                    ),
                ),
            ),
            ToolSpec(
                name="get_contact",
                description="Get full contact details by name or email",
                params=(
                    Param(
                        name="identifier",
                        type="string",
                        description="Contact name or email address to look up",
                        required=True,
                    ),
                ),
            ),
        ),
        needs_network=True,
    )

    def execute(config: ExecutorConfig) -> str:
        tool = config.tool
        if tool == "get_contact":
            identifier = config.args.get("identifier", "")
            contact = get_contact(identifier)
            return json.dumps(contact, indent=2)

        # Default: search_contacts
        query = config.args.get("query", "")
        contacts = search_contacts(query)
        return json.dumps(contacts, indent=2)

    return meta, execute


def _build_service():
    """Build the People API service client."""
    creds = get_credentials()
    return build("people", "v1", credentials=creds, cache_discovery=False)


def _format_contact(person: dict) -> dict:
    """Extract structured contact info from a People API person resource."""
    names = person.get("names", [])
    emails = person.get("emailAddresses", [])
    phones = person.get("phoneNumbers", [])
    orgs = person.get("organizations", [])
    addresses = person.get("addresses", [])
    birthdays = person.get("birthdays", [])
    bios = person.get("biographies", [])

    return {
        "name": names[0].get("displayName", "") if names else "",
        "emails": [e.get("value", "") for e in emails],
        "phones": [p.get("value", "") for p in phones],
        "organization": orgs[0].get("name", "") if orgs else "",
        "title": orgs[0].get("title", "") if orgs else "",
        "addresses": [a.get("formattedValue", "") for a in addresses if a.get("formattedValue")],
        "birthday": (birthdays[0].get("text", "") if birthdays else ""),
        "bio": bios[0].get("value", "") if bios else "",
        "resource_name": person.get("resourceName", ""),
    }


def search_contacts(query: str) -> list[dict]:
    """Search contacts by name, email, or phone number.

    Args:
        query: Search string to match against contacts.

    Returns:
        List of matching contacts with name, emails, phones, etc.
    """
    service = _build_service()

    results = (
        service.people()
        .searchContacts(
            query=query,
            readMask=_PERSON_FIELDS,
            pageSize=25,
        )
        .execute()
    )

    contacts = []
    for result in results.get("results", []):
        person = result.get("person", {})
        contacts.append(_format_contact(person))

    return contacts


def get_contact(identifier: str) -> dict | list[dict]:
    """Get full contact details by name or email.

    Uses searchContacts to find the contact, then returns the first exact
    match or all close matches.

    Args:
        identifier: Contact name or email to look up.

    Returns:
        Single contact dict if exactly one match, or list of matches.
    """
    contacts = search_contacts(identifier)

    if not contacts:
        return {"error": f"No contact found matching '{identifier}'"}

    # Try exact match on name or email
    for contact in contacts:
        if contact["name"].lower() == identifier.lower():
            return contact
        if identifier.lower() in [e.lower() for e in contact["emails"]]:
            return contact

    # Return first match if no exact match
    if len(contacts) == 1:
        return contacts[0]

    return contacts


def main() -> None:
    action = os.environ.get("ACTION", "search")
    query = os.environ.get("QUERY", "")
    identifier = os.environ.get("IDENTIFIER", "")

    if len(sys.argv) > 1:
        action = sys.argv[1]
    if len(sys.argv) > 2:
        query = sys.argv[2]
        identifier = sys.argv[2]

    try:
        if action == "get" and identifier:
            result = get_contact(identifier)
        elif query:
            result = search_contacts(query)
        else:
            result = {"error": "No query or identifier provided"}
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
