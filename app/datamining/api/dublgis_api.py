import argparse
import csv
import json
import logging
from dataclasses import dataclass
from time import sleep
from typing import Any
from typing import Optional
from urllib.parse import urlencode
from urllib.request import urlopen

logger = logging.getLogger(__name__)

ITEMS_API_URL = "https://catalog.api.2gis.com/3.0/items"
REGION_SEARCH_API_URL = "https://catalog.api.2gis.com/2.0/region/search"
RUBRIC_SEARCH_API_URL = "https://catalog.api.2gis.com/2.0/catalog/rubric/search"


class DublgisApiError(Exception):
    def __init__(self, status_code: Any, payload: dict[str, Any]):
        self.status_code = status_code
        self.payload = payload
        super().__init__(f"2GIS API returned code={status_code}: {payload}")


@dataclass
class DublgisApiClient:
    api_url: str = ITEMS_API_URL
    region_search_url: str = REGION_SEARCH_API_URL
    rubric_search_url: str = RUBRIC_SEARCH_API_URL
    timeout_seconds: int = 20
    max_retries: int = 3
    retry_delay_seconds: float = 1.0

    def _fetch_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        request_url = f"{url}?{urlencode(params)}"
        with urlopen(request_url, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))

        meta = payload.get("meta", {})
        status_code = meta.get("code")
        if status_code == 200:
            return payload
        raise DublgisApiError(status_code=status_code, payload=payload)

    def search_regions(
        self,
        query: str,
        api_key: str,
        page: int | None = None,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        if not query.strip():
            raise ValueError("query must not be empty")
        params = {
            "q": query,
            "key": api_key,
        }
        if page is not None:
            params["page"] = page
        if page_size is not None:
            params["page_size"] = page_size
        return self._fetch_json(self.region_search_url, params)

    def search_rubrics(
        self,
        query: str,
        region_id: int,
        api_key: str,
        page: int | None = None,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        if not query.strip():
            raise ValueError("query must not be empty")
        params = {
            "q": query,
            "region_id": region_id,
            "key": api_key,
        }
        if page is not None:
            params["page"] = page
        if page_size is not None:
            params["page_size"] = page_size
        return self._fetch_json(self.rubric_search_url, params)

    def fetch_items_page(
        self,
        rubric_ids: list[int],
        region_id: int,
        api_key: str,
        page: int = 1,
        page_size: int = 50,
        search_type: str = "one_branch",
    ) -> dict[str, Any]:
        params = {
            "rubric_id": ",".join(str(rubric_id) for rubric_id in rubric_ids),
            "region_id": region_id,
            "search_type": search_type,
            "key": api_key,
            "page_size": page_size,
            "page": page,
        }

        url = f"{self.api_url}?{urlencode(params)}"
        attempt = 0

        while True:
            attempt += 1
            with urlopen(url, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))

            meta = payload.get("meta", {})
            status_code = meta.get("code")
            if status_code == 200:
                return payload

            error_type = meta.get("error", {}).get("type") if isinstance(meta, dict) else None
            is_item_not_found = status_code == 404 and error_type == "itemNotFound"
            can_retry = attempt <= self.max_retries

            if can_retry and not is_item_not_found:
                logger.warning(
                    "2GIS API returned code=%s (page=%s), retrying %s/%s after %ss",
                    status_code,
                    page,
                    attempt,
                    self.max_retries,
                    self.retry_delay_seconds,
                )
                sleep(self.retry_delay_seconds)
                continue

            raise DublgisApiError(status_code=status_code, payload=payload)


def iter_all_pages(
    rubric_ids: list[int],
    region_id: int,
    api_key: str,
    page_size: int = 50,
    search_type: str = "one_branch",
    client: Optional[DublgisApiClient] = None,
):
    if not rubric_ids:
        raise ValueError("rubric_ids must not be empty")

    client = client or DublgisApiClient()
    page = 1
    expected_total: Optional[int] = None
    collected = 0

    logger.info(
        "Start 2GIS pagination: rubric_ids=%s region_id=%s page_size=%s search_type=%s",
        rubric_ids,
        region_id,
        page_size,
        search_type,
    )

    while True:
        logger.info("Requesting page %s", page)
        try:
            payload = client.fetch_items_page(
                rubric_ids=rubric_ids,
                region_id=region_id,
                api_key=api_key,
                page=page,
                page_size=page_size,
                search_type=search_type,
            )
        except DublgisApiError as exc:
            error_type = (
                exc.payload.get("meta", {}).get("error", {}).get("type")
                if isinstance(exc.payload, dict)
                else None
            )
            if exc.status_code == 404 and error_type == "itemNotFound":
                logger.info(
                    "Stopping pagination: page %s returned itemNotFound (404)", page
                )
                if expected_total is not None and collected != expected_total:
                    logger.warning(
                        "API total mismatch: collected=%s, expected_total=%s",
                        collected,
                        expected_total,
                    )
                break
            raise
        result = payload.get("result", {})
        page_items = result.get("items", [])

        if expected_total is None:
            expected_total = result.get("total")
            logger.info("Total items reported by API: %s", expected_total)

        yield payload

        collected += len(page_items)
        logger.info(
            "Fetched page %s: %s items, collected=%s/%s",
            page,
            len(page_items),
            collected,
            expected_total if expected_total is not None else "?",
        )
        if not page_items:
            logger.info("Stopping pagination: page %s returned no items", page)
            break

        if expected_total is not None and collected >= expected_total:
            logger.info("Stopping pagination: collected all %s items", expected_total)
            break

        page += 1


def fetch_all_items(
    rubric_ids: list[int],
    region_id: int,
    api_key: str,
    page_size: int = 50,
    search_type: str = "one_branch",
    client: Optional[DublgisApiClient] = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for payload in iter_all_pages(
        rubric_ids=rubric_ids,
        region_id=region_id,
        api_key=api_key,
        page_size=page_size,
        search_type=search_type,
        client=client,
    ):
        result = payload.get("result", {})
        page_items = result.get("items", [])
        items.extend(page_items)

    return items


def items_to_csv_rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        rows.append({key: _serialize_csv_value(value) for key, value in item.items()})
    return rows


def fetch_all_item_rows(
    rubric_ids: list[int],
    region_id: int,
    api_key: str,
    page_size: int = 50,
    search_type: str = "one_branch",
    client: Optional[DublgisApiClient] = None,
) -> list[dict[str, Any]]:
    return items_to_csv_rows(
        fetch_all_items(
            rubric_ids=rubric_ids,
            region_id=region_id,
            api_key=api_key,
            page_size=page_size,
            search_type=search_type,
            client=client,
        )
    )


def search_regions(
    query: str,
    api_key: str,
    page: int | None = None,
    page_size: int | None = None,
    client: Optional[DublgisApiClient] = None,
) -> dict[str, Any]:
    client = client or DublgisApiClient()
    return client.search_regions(
        query=query,
        api_key=api_key,
        page=page,
        page_size=page_size,
    )


def search_rubrics(
    query: str,
    region_id: int,
    api_key: str,
    page: int | None = None,
    page_size: int | None = None,
    client: Optional[DublgisApiClient] = None,
) -> dict[str, Any]:
    client = client or DublgisApiClient()
    return client.search_rubrics(
        query=query,
        region_id=region_id,
        api_key=api_key,
        page=page,
        page_size=page_size,
    )


def _serialize_csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def write_items_to_csv(items: list[dict[str, Any]], output_csv: str) -> None:
    rows = items_to_csv_rows(items)
    logger.info("Writing %s items to CSV: %s", len(rows), output_csv)
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)

    if not fieldnames:
        fieldnames = ["item_json"]

    with open(output_csv, "w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        for item in rows:
            row = {key: item.get(key) for key in fieldnames}
            writer.writerow(row)
    logger.info("CSV saved: %s", output_csv)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch paginated items from 2GIS API")
    parser.add_argument("--rubric-id", type=int, action="append", required=True)
    parser.add_argument("--region-id", type=int, required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--search-type", default="one_branch")
    parser.add_argument("--output-csv", default="dublgis_items.csv")
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    args = _parse_args()
    all_items = fetch_all_item_rows(
        rubric_ids=args.rubric_id,
        region_id=args.region_id,
        api_key=args.api_key,
        page_size=args.page_size,
        search_type=args.search_type,
    )
    write_items_to_csv(all_items, args.output_csv)
    print(f"Saved {len(all_items)} items to {args.output_csv}")
