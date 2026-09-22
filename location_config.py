"""Location-mode validation for ZTB site deployment payloads."""

from typing import Any, Callable, Dict, Optional


VALID_LOCATION_TYPES = {"auto", "new", "existing", "none"}


def normalize_location_type(value: Any) -> str:
    """Normalize friendly CSV values to an API payload location mode."""
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "": "auto",
        "new_location": "new",
        "existing_location": "existing",
        "no_location": "none",
    }
    mode = aliases.get(raw, raw)
    if mode not in VALID_LOCATION_TYPES:
        valid = ", ".join(sorted(VALID_LOCATION_TYPES))
        raise ValueError(f"invalid location_type '{value}'. Expected one of: {valid}")
    return mode


def prepare_location_context(
    row: Dict[str, Any],
    resolve_existing: Callable[[str], Optional[int]],
    resolve_location_template: Optional[Callable[[str], Optional[int]]] = None,
) -> Dict[str, Any]:
    """Resolve a CSV row into the context consumed by site_payload.py.

    ``auto`` preserves the legacy behavior: reuse a matching ZIA location, or
    create a new one if no match exists. New locations require the Location
    Template ID now selected by the ZTB Add Site workflow.
    """
    requested_mode = normalize_location_type(row.get("location_type"))
    zia_location_name = str(row.get("zia_location_name") or "").strip()
    country = str(row.get("country") or "").strip()

    existing_location_id: Optional[int] = None
    mode = requested_mode

    if mode == "auto":
        existing_location_id = resolve_existing(zia_location_name) if zia_location_name else None
        mode = "existing" if existing_location_id else "new"
    elif mode == "existing":
        if not zia_location_name:
            raise ValueError("location_type=existing requires zia_location_name")
        existing_location_id = resolve_existing(zia_location_name)
        if not existing_location_id:
            raise ValueError(
                f"location_type=existing could not resolve zia_location_name='{zia_location_name}'"
            )

    context: Dict[str, Any] = {
        "location_type": mode,
        "existing_location_id": existing_location_id,
    }

    if mode == "new":
        if not country:
            raise ValueError("location_type=new requires country")
        raw_template_id = row.get("location_template_id") or ""
        location_template_name = (
            str(row.get("location_template_name") or "").strip()
            or "Default Location Template"
        )

        if str(raw_template_id).strip():
            try:
                location_template_id = int(str(raw_template_id).strip())
            except (TypeError, ValueError):
                raise ValueError("location_template_id must be numeric") from None
        else:
            if not resolve_location_template:
                raise ValueError(
                    "location_type=new requires a location-template resolver when "
                    "location_template_id is not supplied"
                )
            resolved_template_id = resolve_location_template(location_template_name)
            if not resolved_template_id:
                raise ValueError(
                    f"could not resolve location_template_name='{location_template_name}'"
                )
            location_template_id = int(resolved_template_id)

        if location_template_id <= 0:
            raise ValueError("location_template_id must be a positive integer")
        context["location_template_id"] = location_template_id
        context["location_template_name"] = location_template_name

    return context
