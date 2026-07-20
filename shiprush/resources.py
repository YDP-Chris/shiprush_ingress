"""Registry of ShipRush data-read resources.

One Cloud Run Job instance pulls exactly one resource, selected via the
SHIPRUSH_ENDPOINT env var and validated here (Source Pipeline Standards sections
1 & 4).

Everything below is CONFIRMED from the ShipRush SDK command catalog + the API
XSD (GetShipmentsRequest/Response, GetCatalog*, GetInventory*, DataPaging):

  - Pagination is page-number based: the request carries ItemsPerPage +
    PageNumber (1-based); the response carries a <Paging> (DataPaging) block with
    <HasMoreData>. Loop pages until HasMoreData is false (implemented in client).
  - The incremental "updated since" filter is an xs:dateTime request field --
    ModifiedFrom/ModifiedTo for shipments, ModifiedAtFrom/ModifiedAtTo for
    catalog/inventory/locations. shippingaccounts has no filter and no paging.
  - Records live under a container element; the per-record item is the
    container's DIRECT child (TShipTransaction also appears nested inside a
    shipment, so we must take direct children of the container, not any
    descendant -- exactly the wrong-list trap standards section 5 warns about).

Server-behavior details NOT nailed by the XSD (verify against one live sandbox
response before production, per standards section 6): PageNumber base (assumed
1), whether wide sentinel dates on the unused date filters mean "no filter"
(assumed yes), and max ItemsPerPage. These are isolated in the request builders
below and flagged with TODO(verify-live).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from xml.sax.saxutils import escape

# xs:dateTime sentinels for date filters we are not filtering on.
_EPOCH_START = "0001-01-01T00:00:00"
_FAR_FUTURE = "9999-12-31T23:59:59"

_XMLNS = (
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xmlns:xsd="http://www.w3.org/2001/XMLSchema"'
)


def _shipments_request(*, page: int, page_size: int, since: str, until: str) -> str:
    # ShipmentType=History pulls completed/shipped records (the analytical data).
    # Pending/Favorites/etc. would be separate jobs if ever wanted (standards §1).
    # TODO(verify-live): PageNumber base and ShipDate sentinel handling.
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f"<GetShipmentsRequest {_XMLNS}>"
        f"<ModifiedFrom>{escape(since)}</ModifiedFrom>"
        f"<ModifiedTo>{escape(until)}</ModifiedTo>"
        f"<ShipDateFrom>{_EPOCH_START}</ShipDateFrom>"
        f"<ShipDateTo>{_FAR_FUTURE}</ShipDateTo>"
        "<DetailLevel>Full</DetailLevel>"
        "<ShipmentType>History</ShipmentType>"
        "<Carrier>7</Carrier>"  # 7 = Unknown; ignored because CarrierIsSet=false
        "<CarrierIsSet>false</CarrierIsSet>"
        f"<ItemsPerPage>{page_size}</ItemsPerPage>"
        f"<PageNumber>{page}</PageNumber>"
        "<AmazonPrimeSearchType>CouldBeAnyValue</AmazonPrimeSearchType>"
        "<ReturnExtendedShipmentFields>True</ReturnExtendedShipmentFields>"
        "<ReturnExtendedInternationalFields>True</ReturnExtendedInternationalFields>"
        "<ReturnExtendedPackageFields>True</ReturnExtendedPackageFields>"
        "</GetShipmentsRequest>"
    )


def _catalog_request(*, page: int, page_size: int, since: str, until: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f"<GetCatalogRequest {_XMLNS}>"
        f"<ItemsPerPage>{page_size}</ItemsPerPage>"
        f"<PageNumber>{page}</PageNumber>"
        f"<ModifiedAtFrom>{escape(since)}</ModifiedAtFrom>"
        f"<ModifiedAtTo>{escape(until)}</ModifiedAtTo>"
        "</GetCatalogRequest>"
    )


def _inventory_request(*, page: int, page_size: int, since: str, until: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f"<GetInventoryRequest {_XMLNS}>"
        f"<ItemsPerPage>{page_size}</ItemsPerPage>"
        f"<PageNumber>{page}</PageNumber>"
        f"<InventoryItemModifiedAtFrom>{escape(since)}</InventoryItemModifiedAtFrom>"
        f"<InventoryItemModifiedAtTo>{escape(until)}</InventoryItemModifiedAtTo>"
        f"<CatalogItemModifiedAtFrom>{_EPOCH_START}</CatalogItemModifiedAtFrom>"
        f"<CatalogItemModifiedAtTo>{_FAR_FUTURE}</CatalogItemModifiedAtTo>"
        "</GetInventoryRequest>"
    )


def _inventory_locations_request(*, page: int, page_size: int, since: str, until: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f"<GetInventoryLocationsRequest {_XMLNS}>"
        f"<ItemsPerPage>{page_size}</ItemsPerPage>"
        f"<PageNumber>{page}</PageNumber>"
        f"<ModifiedAtFrom>{escape(since)}</ModifiedAtFrom>"
        f"<ModifiedAtTo>{escape(until)}</ModifiedAtTo>"
        "</GetInventoryLocationsRequest>"
    )


def _shipping_accounts_request(*, page: int, page_size: int, since: str, until: str) -> str:
    # No filters, no paging.
    return f'<?xml version="1.0" encoding="utf-8"?><GetShippingAccountsRequest {_XMLNS} />'


@dataclass(frozen=True)
class Resource:
    name: str
    # SDK "{service}.svc/{command}", appended to Config.base_url, POSTed as XML.
    path: str
    # Response wrapper element; records are this element's DIRECT children.
    container: str
    # Builds the XML request body for one page.
    build_request: Callable[..., str]
    # False => single non-paged call (no <Paging> loop).
    paged: bool = True
    # True => supports a Modified* date-window filter -> watermarking (§9).
    incremental: bool = True


RESOURCES: tuple[Resource, ...] = (
    Resource(
        name="shipments",
        path="shipmentservice.svc/shipments/get",
        container="ShipTransactions",
        build_request=_shipments_request,
    ),
    Resource(
        name="catalog",
        path="catalogservice.svc/catalog/get",
        container="CatalogItems",
        build_request=_catalog_request,
    ),
    Resource(
        name="inventory",
        path="catalogservice.svc/inventory/get",
        container="InventoryItems",
        build_request=_inventory_request,
    ),
    Resource(
        name="inventory_locations",
        path="catalogservice.svc/inventory/locations/get",
        container="MerchantLocations",
        build_request=_inventory_locations_request,
    ),
    Resource(
        name="shippingaccounts",
        path="accountservice.svc/shippingaccounts/get",
        container="ShippingAccounts",
        build_request=_shipping_accounts_request,
        paged=False,
        incremental=False,
    ),
)

_BY_NAME = {r.name: r for r in RESOURCES}


def get_resource(name: str) -> Resource:
    if name not in _BY_NAME:
        raise ValueError(
            f"Unknown SHIPRUSH_ENDPOINT {name!r}. Known resources: {', '.join(_BY_NAME)}"
        )
    return _BY_NAME[name]
