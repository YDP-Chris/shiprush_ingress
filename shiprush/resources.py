"""Registry of ShipRush resources that expose a listable collection.

One Cloud Run Job instance pulls exactly one resource, selected via the
SHIPRUSH_ENDPOINT env var and validated here (Source Pipeline Standards section
1 & 4). Adding a resource is a code change here, not a free-form env string.

=============================================================================
UNCONFIRMED -- BLOCKED ON DOCS. The ShipRush "Web Non-Visual API" is a SOAP/XML
web service (DeveloperToken + UserToken headers, Content-Type application/xml),
not the REST/JSON collection model the standards skeleton assumes. The exact
list-operation names, XML request-body schema, response element names, the
"wrapper" element that holds the record list (record_key equivalent), and the
incremental-filter field are all defined in:

  - My.ShipRush.Shipping - Web Non-Visual API Guide
    https://docs.shiprush.com/en/for-developers/shipping/my-shiprush-shipping-web-non-visual-api-guide~7395985101005094591

That page returns 403 to automated fetchers and the API itself requires an
enabled DeveloperToken, so these values could not be confirmed from a live
sample. Per standards sections 5 & 6, they are intentionally NOT guessed here.
RESOURCES is left empty until a live sample or the doc text pins down, per
resource: (a) the SOAP operation name, (b) the request XML shape, (c) the
response record wrapper element (record_key), (d) whether it supports an
incremental "updated since" filter and under what field name.

Fill in the commented example below once confirmed, then delete this banner.
=============================================================================
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Resource:
    name: str
    # For a SOAP source, `path` is the request path/operation appended to
    # Config.base_url (or the SOAP action name). Confirm against the API guide.
    path: str
    updated_since_param: str | None = "updated_since"  # None = no incremental support
    record_key: str | None = None  # CONFIRMED response wrapper key/element -- see banner


RESOURCES: tuple[Resource, ...] = (
    # Resource(
    #     name="orders",                 # SHIPRUSH_ENDPOINT value
    #     path="<confirmed operation>",  # from Web Non-Visual API Guide
    #     updated_since_param="<confirmed field or None>",
    #     record_key="<confirmed response wrapper element>",
    # ),
)

_BY_NAME = {r.name: r for r in RESOURCES}


def get_resource(name: str) -> Resource:
    if name not in _BY_NAME:
        known = ", ".join(_BY_NAME) or "(none registered yet -- see resources.py banner)"
        raise ValueError(f"Unknown SHIPRUSH_ENDPOINT {name!r}. Known resources: {known}")
    return _BY_NAME[name]
