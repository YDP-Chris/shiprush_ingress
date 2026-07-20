"""Registry of ShipRush data-read resources.

One Cloud Run Job instance pulls exactly one resource, selected via the
SHIPRUSH_ENDPOINT env var and validated here (Source Pipeline Standards sections
1 & 4). Adding a resource is a code change here, not a free-form env string.

Endpoints below are CONFIRMED from the ShipRush SDK command catalog
(ShipRush.SDK.Transport.APICommands). Each `path` is the SDK's
"{service}.svc/{command}" appended to Config.base_url, POSTed as XML.

STILL UNCONFIRMED per resource (blocked on the kit's XSD / ShipRush.SDK.Proxies,
which define the request/response schema -- not in the pasted docs): the request
body filter fields (incl. any "updated since"/date-window filter -> drives
`updated_since_param`), the paging scheme, and the response element that wraps
the record list (`record_key`). Per standards sections 5 & 6 these are left as
None (which makes extraction return zero rows -- visible, not a silent wrong
guess) until pinned down from the XSD or one live response.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Resource:
    name: str
    # SDK "{service}.svc/{command}", appended to Config.base_url. Confirmed.
    path: str
    # None = no incremental filter (or not yet confirmed) -> full pull.
    updated_since_param: str | None = None
    # Confirmed response wrapper element (localname) -> set from XSD/live sample.
    record_key: str | None = None


RESOURCES: tuple[Resource, ...] = (
    # Primary target: shipment history/records.  GET_SHIPMENTS in the SDK.
    Resource(name="shipments", path="shipmentservice.svc/shipments/get"),
    # Configured shipping accounts.  GET_SHIPPINGACCOUNTS in the SDK.
    Resource(name="shippingaccounts", path="accountservice.svc/shippingaccounts/get"),
    # Product catalog.  CATALOG_GET_CATALOG in the SDK.
    Resource(name="catalog", path="catalogservice.svc/catalog/get"),
    # Inventory levels.  INVENTORY_GET_INVENTORY in the SDK.
    Resource(name="inventory", path="catalogservice.svc/inventory/get"),
    # Inventory locations.  INVENTORY_GET_INVENTORYLOCATIONS in the SDK.
    Resource(name="inventory_locations", path="catalogservice.svc/inventory/locations/get"),
    # TODO(blocked-on-xsd): set updated_since_param + record_key per resource
    # from the GetShipmentsRequest/Response (and peers) schema before shipping.
)

_BY_NAME = {r.name: r for r in RESOURCES}


def get_resource(name: str) -> Resource:
    if name not in _BY_NAME:
        raise ValueError(
            f"Unknown SHIPRUSH_ENDPOINT {name!r}. Known resources: {', '.join(_BY_NAME)}"
        )
    return _BY_NAME[name]
